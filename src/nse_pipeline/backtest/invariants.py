"""Independent replay from economic event facts, NOT kernel update helpers.

Recompute fees, recognition, settlement, units, and postings, then reconcile
against raw snapshot records and the exposed balance view. No core/fee/valuation
function is called. An invalid ledger cannot make a matching corrupt state pass.
"""
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

from .accounting_types import (ClaimKind, Due, Entitlement, EventKind as K, OpeningCondition, Phase,
                               sha256_digest)
from .flow_provenance import external_flow_citation_digest

ZERO = Decimal(0)


@dataclass(frozen=True)
class InvariantCheck:
    name: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class Reconciliation:
    checks: tuple[InvariantCheck, ...]
    events_checked: int

    @property
    def passed(self):
        return all(c.status == "PASS" for c in self.checks)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _round(amount):
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _claim(c):
    return (c.kind, c.amount, c.due, c.position_id, c.delivery_shares, c.basis, c.citation,
            c.state, c.unresolved_reason)


def _position(p):
    return dict(key=p.key, shares=p.shares, delivered=p.delivered_shares,
                opened=p.opened_on, blocked=p.blocked_reason)


def _order(o):
    return (o.reserved, o.key, o.quantity, o.capacity_notional, o.signal_at,
            o.signal_session_index, o.rank, o.condition)


def _condition_valid(condition):
    if condition is None:
        return True
    if not isinstance(condition, OpeningCondition):
        return False
    low, high = condition.min_price, condition.max_price
    if low is None and high is None:
        return False
    for bound in (low, high):
        if bound is not None and (type(bound) is not Decimal or not bound.is_finite() or bound <= 0):
            return False
    return low is None or high is None or low <= high


def _condition_outcome_matches(condition, raw_price, outcome):
    """Independently evaluate the frozen band; never call the production method."""
    if not _condition_valid(condition):
        return False
    if condition is None:
        return outcome is None
    if type(outcome) is not bool or type(raw_price) is not Decimal or not raw_price.is_finite() or raw_price <= 0:
        return False
    expected = ((condition.min_price is None or raw_price >= condition.min_price)
                and (condition.max_price is None or raw_price <= condition.max_price))
    return outcome == expected


def _effective_sell_cap(e):
    """Effective sell principal cap from the event, floored to paise.

    ``None`` means no per-order cap metadata (legacy sells); a Decimal—including
    zero—is an explicit frozen bound. The declared global hard cap is always
    applied, floored, and never rounded upward.
    """
    if e.sell_capacity_notional is None:
        return None
    _require(type(e.sell_capacity_notional) is Decimal and e.sell_capacity_notional.is_finite()
             and e.sell_capacity_notional >= 0, "Sell capacity is not an exact nonnegative Decimal")
    supplied = e.sell_capacity_notional.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    declared = (None if e.execution_spec.max_order_notional is None else
                Decimal(str(e.execution_spec.max_order_notional)).quantize(
                    Decimal("0.01"), rounding=ROUND_FLOOR))
    caps = tuple(cap for cap in (declared, supplied) if cap is not None)
    return min(caps) if caps else None


def _max_sell_quantity(requested, reference_price, costs, execution, cap):
    """Independent mirror of the cap-bounded sell quantity, without production helpers."""
    bps = -execution.adverse_slippage_bps_sell
    price = reference_price * (1 + Decimal(str(bps)) / 10000)
    def notional(q):
        return _round(q * price)
    if cap is None:
        low = requested
    else:
        low, high = 0, requested
        while low < high:
            mid = (low + high + 1) // 2
            if notional(mid) <= cap:
                low = mid
            else:
                high = mid - 1
    if (notional(low) == 0 or notional(low) < Decimal(str(execution.min_order_notional))
            or (execution.partial_fill_policy == "skip" and low != requested)):
        return 0
    return low


def _trade(e, side):
    _require(type(e.quantity) is int and e.quantity >= 0, "Noninteger fill quantity")
    _require(e.cost_spec is not None and e.execution_spec is not None and e.reference_price > 0,
             "Missing pinned fill assumptions")
    spec, execution = e.cost_spec, e.execution_spec
    bps = execution.adverse_slippage_bps_buy if side == "buy" else -execution.adverse_slippage_bps_sell
    _require(e.price == e.reference_price * (1 + Decimal(str(bps)) / 10000), "Slippage charged incorrectly")
    principal = _round(e.quantity * e.price)
    if e.quantity:
        _require(principal >= Decimal(str(execution.min_order_notional)), "Fill violates minimum order notional")
        # Independent D-01 replay: floor the declared hard cap to paise; never
        # use the half-up _round() path, which could admit a fill above the cap.
        declared_cap = (None if execution.max_order_notional is None else
                        Decimal(str(execution.max_order_notional)).quantize(
                            Decimal("0.01"), rounding=ROUND_FLOOR))
        _require(declared_cap is None or principal <= declared_cap, "Fill violates maximum order notional")
    brokerage = _round(min(principal * Decimal(str(spec.brokerage_bps)) / 10000,
                           Decimal(str(spec.brokerage_cap_per_order))))
    fees = (("brokerage", brokerage), ("gst", _round(brokerage * Decimal(str(spec.gst_on_brokerage_pct)) / 100)),
            ("stt", _round(principal * Decimal(str(spec.stt_bps_buy if side == "buy" else spec.stt_bps_sell)) / 10000)),
            ("stamp", _round(principal * Decimal(str(spec.stamp_bps_buy)) / 10000) if side == "buy" else ZERO),
            ("exchange", _round(principal * Decimal(str(spec.exchange_bps)) / 10000)))
    _require(e.fees == fees and e.fee == sum((v for _, v in fees), ZERO), "Fee components or fee total do not reconcile")
    _require(e.amount == principal + e.fee if side == "buy" else e.amount == principal - e.fee,
             "Trade recognition double-counts or omits a fee/principal")
    return principal


def _balances_valid(cash, claims, positions, orders, max_positions):
    amounts = [c[1] for c in claims.values() if c[1] is not None]
    _require(all(isinstance(a, Decimal) and a.is_finite() and a >= 0 for a in amounts), "Invalid financial claim amount")
    liabilities = [c[1] for c in claims.values() if c[1] is not None and c[0] in (ClaimKind.BUY_PAYABLE, ClaimKind.OTHER_PAYABLE)]
    _require(cash.is_finite() and cash - sum(liabilities, ZERO) - sum((o[0] for o in orders.values()), ZERO) >= 0,
             "Unfunded settled cash/reservations/payables")
    _require(sum(p["shares"] > 0 for p in positions.values()) <= max_positions, "Position cap exceeded")
    _require(len({p["key"] for p in positions.values() if p["shares"]}) == sum(p["shares"] > 0 for p in positions.values()),
             "Duplicate open security identity")
    _require(all(c[0] != ClaimKind.BUY_PAYABLE or c[3] in positions for c in claims.values()), "Orphan purchase delivery")
    for c in claims.values():
        state, reason = c[7], c[8]
        _require(state in ("unbound", "accrued_held", "scheduled", "confirmed", "voided"),
                 "Unknown claim lifecycle state")
        if state in ("unbound", "accrued_held"):
            _require(c[2] is None, "A held/unbound claim cannot carry a settlement schedule")
        if state == "scheduled":
            _require(c[2] is not None, "A scheduled claim needs a settlement schedule")
        if state == "accrued_held":
            _require(c[1] is not None and bool(reason), "A held claim needs a known amount and pending reason")
        if state == "confirmed":
            _require(c[2] is not None and c[5] == "verified" and not reason,
                     "A confirmed claim needs a verified due date and no pending reason")
    for pid, p in positions.items():
        pending = sum(c[4] for c in claims.values() if c[0] == ClaimKind.BUY_PAYABLE and c[3] == pid)
        _require(type(p["shares"]) is int and type(p["delivered"]) is int and 0 <= p["delivered"] <= p["shares"]
                 and p["delivered"] + pending == p["shares"], "Share/delivery conservation failed")


def _lifecycle_valid(claims):
    for c in claims.values():
        state, reason = c[7], c[8]
        if state not in ("unbound", "accrued_held", "scheduled", "confirmed", "voided"):
            return False
        if state in ("unbound", "accrued_held") and c[2] is not None:
            return False
        if state == "scheduled" and c[2] is None:
            return False
        if state == "accrued_held" and (c[1] is None or not reason):
            return False
        if state == "confirmed" and (c[2] is None or c[5] != "verified" or reason):
            return False
    return True


def _replay(initial, events, sessions, max_positions):
    _require(bool(sessions) and tuple(sorted(set(sessions))) == tuple(sessions), "Invalid observed-session stream")
    positions = {p.position_id: _position(p) for p in initial.positions}
    claims = {c.claim_id: _claim(c) for c in initial.claims}
    orders = {o.order_id: _order(o) for o in initial.orders}
    entitlements = {e.dividend_id: e for e in initial.entitlements}
    _require(len(positions) == len(initial.positions) and len(claims) == len(initial.claims)
             and len(orders) == len(initial.orders), "Duplicate opening record ID")
    _balances_valid(initial.cash, claims, positions, orders, max_positions)
    cash, ids, last, buys_seen = initial.cash, set(), None, set()
    withdrawals_under_unknown_liability = 0
    used_claims = set(claims)
    allowed = {K.EXTERNAL_FLOW: (10,), K.RESERVE: (70,), K.CANCEL: (30, 70, 80), K.BUY: (40,), K.BUY_SKIPPED: (40,),
               K.SELL: (40,), K.SELL_SKIPPED: (40,), K.SPLIT: (30,), K.BONUS: (30,), K.ACTION_BLOCKED: (30,),
               K.ACTION_ORDERS_CANCELED: (30,),
               K.DIVIDEND_ENTITLEMENT: (30,), K.DIVIDEND_ACCRUAL: (30,),
               K.DIVIDEND_PAYMENT_CONFIRMED: (10, 30, 60), K.DIVIDEND_VOIDED: (30,),
               K.DIVIDEND_CORRECTED: (30,), K.DIVIDEND_BLOCKED: (30,),
               K.REDEMPTION: (30,), K.LIABILITY: (30, 60), K.SETTLE_BUY: (20,),
               K.SETTLE_SALE: (20,), K.SETTLE_DIVIDEND: (50,), K.SETTLE_OTHER: (20, 50), K.SETTLE_LIABILITY: (20, 50)}
    for e in events:
        _require(e.event_id not in ids and (last is None or e.at > last), "Duplicate or noncausal event ordering")
        _require(e.kind in allowed and e.at.phase in allowed[e.kind] and e.at.session in sessions, "Invalid event phase/session")
        _require(e.kind == K.EXTERNAL_FLOW or not (e.flow_source_digest or e.flow_citation_digest),
                 "Unrelated event kind carries external-flow provenance digests")
        _require(e.kind in (K.RESERVE, K.BUY, K.BUY_SKIPPED, K.SELL, K.SELL_SKIPPED)
                 or (e.condition is None and e.condition_met is None),
                 "Unrelated event kind carries opening-condition metadata")
        _require(e.kind in (K.SELL, K.SELL_SKIPPED) or e.sell_capacity_notional is None,
                 "Unrelated event kind carries sell-capacity metadata")
        ids.add(e.event_id)
        last = e.at
        index = sessions.index(e.at.session)
        for c in claims.values():
            if c[1] is None or c[2] is None:
                continue
            due = c[2]
            matured = index >= due.session_index if due.session_index is not None else e.at.session >= due.on
            missed = index > 0 and (index > due.session_index if due.session_index is not None else sessions[index - 1] >= due.on)
            _require(not missed and not (matured and e.at.phase > due.phase), "A required earlier settlement phase was skipped")
        expected = Counter()
        def post(account, amount, pid=""):
            expected[(account, pid)] += amount
        def cancel_key_orders():
            canceled = tuple(oid for oid, o in orders.items() if o[1] == e.key)
            released = sum((orders[oid][0] for oid in canceled), ZERO)
            _require(canceled == e.canceled_orders and released == e.released_reserve, "Action order cancellations do not reconcile")
            for oid in canceled:
                del orders[oid]
            post("order_reserved", -released)
        def evidence():
            proof = e.evidence
            _require(proof is not None and proof.key == e.key and proof.effective_on == e.at.session
                     and proof.available_at <= e.at, "Unavailable or wrong-security documentary evidence")
            return proof
        def new_claim():
            _require(e.claim_id not in used_claims and bool(e.claim_id), "Financial claim ID reused across its lifetime")
            if e.due is not None:
                mature = index >= e.due.session_index if e.due.session_index is not None else e.at.session >= e.due.on
                _require(not (mature and e.at.phase >= e.due.phase), "Claim was scheduled into a past settlement phase")
            used_claims.add(e.claim_id)
        if e.kind == K.RESERVE:
            _require(e.order_id not in orders and e.amount > 0 and type(e.quantity) is int and e.quantity > 0,
                     "Invalid order reservation")
            _require(_condition_valid(e.condition) and e.condition_met is None,
                     "Invalid frozen opening condition on a reservation")
            orders[e.order_id] = (e.amount, e.key, e.quantity, e.capacity_notional, e.at, index, e.rank,
                                  e.condition)
            post("order_reserved", e.amount)
        elif e.kind == K.CANCEL:
            order = orders.pop(e.order_id)
            _require(e.amount == order[0], "Wrong reservation release")
            post("order_reserved", -order[0])
        elif e.kind == K.EXTERNAL_FLOW:
            _require(isinstance(e.event_id, str) and e.event_id.startswith("flow:")
                     and bool(e.event_id[len("flow:"):].strip()),
                     "External flow event ID must use the flow:<nonempty ID> form")
            _require(type(e.amount) is Decimal and e.amount.is_finite() and e.amount > 0
                     and e.amount == _round(e.amount), "External flow is not a positive monetary amount")
            _require(e.reason in ("deposit", "withdrawal") and isinstance(e.citation, str)
                     and bool(e.citation.strip()), "External flow lacks a known direction or citation")
            if e.flow_source_digest or e.flow_citation_digest:
                _require(bool(e.flow_source_digest) and bool(e.flow_citation_digest),
                         "External flow has an incomplete citation/source digest binding")
                sha256_digest(e.flow_source_digest, "External-flow source digest")
                expected_citation_digest = external_flow_citation_digest(
                    e.event_id[len("flow:"):], e.amount, e.reason, e.citation, e.at,
                    e.flow_source_digest)
                _require(e.flow_citation_digest == expected_citation_digest,
                         "External flow citation/source digest substitution detected")
            _require(not e.claim_id and not e.order_id and not e.position_id and not e.key
                     and not e.entitlement_id and e.due is None
                     and type(e.quantity) is int and e.quantity == 0,
                     "External flow fabricated a claim, order, position, or entitlement")
            _require(e.price == ZERO and e.fee == ZERO and e.released_reserve == ZERO
                     and e.delivery_quantity == 0 and not e.fees and e.factor == 1
                     and not e.canceled_orders and e.reference_price == ZERO
                     and e.cost_spec is None and e.execution_spec is None and e.evidence is None
                     and e.dividend_policy is None and e.payment_on is None
                     and e.payment_available_at is None and not e.payment_model
                     and e.capacity_notional == ZERO and e.rank == 0 and e.requested_quantity == 0
                     and e.condition is None and e.condition_met is None
                     and e.sell_capacity_notional is None,
                     "External flow contains unrelated economic event fields")
            if e.reason == "withdrawal":
                unknown_liability = any(c[0] in (ClaimKind.BUY_PAYABLE, ClaimKind.OTHER_PAYABLE)
                                        and c[1] is None for c in claims.values())
                if unknown_liability:
                    withdrawals_under_unknown_liability += 1
                _require(not unknown_liability,
                         "External withdrawal is not allowed while any payable is unknown")
            signed = e.amount if e.reason == "deposit" else -e.amount
            cash += signed
            post("cash", signed)
            post("external_flows", signed)
        elif e.kind in (K.BUY, K.BUY_SKIPPED):
            order = orders[e.order_id]
            live = [oid for oid, o in orders.items() if o[5] + 1 == index]
            _require(order[5] + 1 == index and e.order_id == min(live, key=lambda oid: (orders[oid][6], orders[oid][1], oid)),
                     "Opening buy did not follow frozen rank/session ordering")
            del orders[e.order_id]
            _require(e.condition == order[7], "Opening condition was replaced after freeze")
            _require(_condition_outcome_matches(e.condition, e.reference_price, e.condition_met),
                     "Buy condition outcome does not match its frozen band and raw opening")
            _require(not (e.condition is not None and e.condition_met is False and e.kind != K.BUY_SKIPPED),
                     "An unmet opening condition produced a purchase")
            _require(order[0] == e.released_reserve and order[1] == e.key and e.quantity <= order[2], "Fill exceeds its frozen order")
            post("order_reserved", -order[0])
            principal = _trade(e, "buy")
            _require(e.amount <= order[0] and principal <= order[3], "Fill exceeds reserved cash/as-of capacity")
            _require(e.quantity == 0 or e.execution_spec.partial_fill_policy != "skip" or e.quantity == order[2],
                     "Partial purchase violates the pinned skip policy")
            buys_seen.add(e.at.session)
            if e.kind == K.BUY:
                _require(e.quantity > 0 and e.claim_id not in claims, "Invalid purchase claim")
                new_claim()
                due = Due(Phase.PRE_OPEN_SETTLEMENT, session_index=index + e.execution_spec.trade_settlement_lag_sessions)
                _require(e.due == due, "Purchase settlement lag is wrong")
                if e.position_id not in positions:
                    positions[e.position_id] = dict(key=e.key, shares=0, delivered=0, opened=e.at.session, blocked="")
                p = positions[e.position_id]
                _require(p["key"] == e.key and not p["blocked"], "Identity or blocked-position fill mismatch")
                p["shares"] += e.quantity
                claims[e.claim_id] = (ClaimKind.BUY_PAYABLE, e.amount, due, e.position_id, e.quantity,
                                      "modeled", "opening execution proxy", "scheduled", "")
                post("shares", e.quantity, e.position_id)
                post("liabilities", e.amount)
            else:
                _require(e.quantity == 0 and e.amount == 0 and not e.claim_id,
                         "Skipped purchase changed economic shares, wealth, or claims")
        elif e.kind in (K.SELL, K.SELL_SKIPPED):
            _require(e.at.session not in buys_seen, "Sells must precede buys at the opening")
            _require(_condition_outcome_matches(e.condition, e.reference_price, e.condition_met),
                     "Sell condition outcome does not match its band and raw opening")
            _require(not (e.condition is not None and e.condition_met is False and e.kind != K.SELL_SKIPPED),
                     "An unmet opening condition produced a sale")
            p = positions[e.position_id]
            _require(type(e.requested_quantity) is int and 0 < e.requested_quantity <= p["delivered"]
                     and p["key"] == e.key and not p["blocked"], "Short/unsettled/blocked sale")
            _require(e.quantity <= e.requested_quantity, "Sale exceeds requested quantity")
            cap = _effective_sell_cap(e)
            principal = _trade(e, "sell")
            _require(cap is None or principal <= cap, "Fill exceeds the frozen per-order capacity")
            if e.sell_capacity_notional is not None and not (
                    e.condition is not None and e.condition_met is False):
                expected_quantity = _max_sell_quantity(e.requested_quantity, e.reference_price,
                                                       e.cost_spec, e.execution_spec, cap)
                if expected_quantity == 0:
                    _require(e.kind == K.SELL_SKIPPED and e.quantity == 0,
                             "Known sell capacity allowed no executable sale but the event filled")
                else:
                    _require(e.kind == K.SELL and e.quantity == expected_quantity,
                             "Sale quantity does not respect the frozen per-order capacity")
            if e.kind == K.SELL_SKIPPED:
                _require(e.quantity == 0 and e.amount == 0 and not e.claim_id
                         and e.delivery_quantity == 0 and e.released_reserve == ZERO,
                         "Skipped sale created an asset or reserve release")
            else:
                _require(e.quantity > 0 and (e.execution_spec.partial_fill_policy != "skip" or
                                            e.quantity == e.requested_quantity), "Partial sale violates the pinned policy")
                new_claim()
                due = Due(Phase.PRE_OPEN_SETTLEMENT, session_index=index + e.execution_spec.trade_settlement_lag_sessions)
                _require(e.due == due and e.claim_id not in claims, "Wrong sale settlement or duplicate receivable")
                p["shares"] -= e.quantity
                p["delivered"] -= e.quantity
                claims[e.claim_id] = (ClaimKind.SALE, e.amount, due, e.position_id, 0,
                                      "modeled", "opening execution proxy", "scheduled", "")
                post("shares", -e.quantity, e.position_id)
                post("delivered", -e.quantity, e.position_id)
                post("sale_receivable", e.amount)
        elif e.kind.value.startswith("settle_"):
            kind, amount, due, pid, delivery, basis, citation, state, unresolved = claims.pop(e.claim_id)
            _require(amount is not None and e.amount == amount and due is not None and e.due == due,
                     "Settlement amount or source claim is wrong")
            _require(state in ("scheduled", "confirmed") and not unresolved,
                     "An unheld or unsettled claim cannot be paid")
            reached = (index >= due.session_index if due.session_index is not None else e.at.session >= due.on)
            _require(e.at.phase == due.phase and reached, "Settlement is early or in the wrong phase")
            expected_kind = {ClaimKind.BUY_PAYABLE: K.SETTLE_BUY, ClaimKind.OTHER_PAYABLE: K.SETTLE_LIABILITY,
                             ClaimKind.SALE: K.SETTLE_SALE, ClaimKind.DIVIDEND: K.SETTLE_DIVIDEND, ClaimKind.OTHER: K.SETTLE_OTHER}[kind]
            _require(e.kind == expected_kind, "Wrong settlement event kind")
            payable = kind in (ClaimKind.BUY_PAYABLE, ClaimKind.OTHER_PAYABLE)
            cash_delta = -amount if payable else amount
            cash += cash_delta
            post("cash", cash_delta)
            post("liabilities" if payable else kind.value, -amount)
            if kind == ClaimKind.BUY_PAYABLE:
                _require(e.delivery_quantity == delivery, "Split-adjusted purchase delivery is wrong")
                positions[pid]["delivered"] += delivery
                post("delivered", delivery, pid)
        elif e.kind == K.ACTION_ORDERS_CANCELED:
            evidence()
            _require(e.canceled_orders and not any(p["key"] == e.key and p["shares"] for p in positions.values()),
                     "Order-only action is not scoped to an unheld security")
            cancel_key_orders()
        elif e.kind in (K.SPLIT, K.BONUS, K.ACTION_BLOCKED):
            proof = evidence()
            p = positions[e.position_id]
            _require(p["key"] == e.key and p["shares"] == e.quantity and p["delivered"] == e.delivery_quantity,
                     "Action did not use pre-action raw units")
            _require(not p["blocked"], "A blocked action was transformed again without resolution")
            cancel_key_orders()
            if e.kind == K.ACTION_BLOCKED:
                _require(bool(e.reason), "Blocked action needs a disclosed reason")
                p["blocked"] = e.reason
                for eid, ent in tuple(entitlements.items()):
                    if ent.position_id != e.position_id or ent.ex_date != e.at.session:
                        continue
                    entitlements[eid] = replace(ent, due=None, unresolved_reason=e.reason)
                    cid = f"dividend:{eid}"
                    if cid in claims:
                        c = claims[cid]
                        if c[1] is not None:
                            post("dividend_receivable", -c[1])
                            post("unknown_dividend_claims", 1)
                        claims[cid] = (c[0], None, None) + c[3:7] + ("unbound", e.reason)
            else:
                _require(proof.terms_verified and e.factor > 0, "Unverified share transformation")
                shares, delivered = p["shares"] * e.factor, p["delivered"] * e.factor
                _require(shares.denominator == delivered.denominator == 1, "Fractional shares were silently rounded")
                for cid, c in tuple(claims.items()):
                    if c[0] == ClaimKind.BUY_PAYABLE and c[3] == e.position_id:
                        qty = c[4] * e.factor
                        _require(qty.denominator == 1, "Fractional delivery was silently rounded")
                        claims[cid] = c[:4] + (int(qty),) + c[5:]
                post("shares", int(shares) - p["shares"], e.position_id)
                post("delivered", int(delivered) - p["delivered"], e.position_id)
                p["shares"], p["delivered"] = int(shares), int(delivered)
        elif e.kind == K.DIVIDEND_ENTITLEMENT:
            proof = evidence()
            p, policy = positions[e.position_id], e.dividend_policy
            _require(proof.terms_verified and p["opened"] < e.at.session and not p["blocked"]
                     and e.quantity == p["shares"] and policy is not None, "Dividend entitlement is not established")
            if policy.dividend_settlement_mode == "hold_receivable":
                due, basis = None, "modeled"
            elif e.payment_on is not None:
                _require(e.payment_available_at is not None and e.payment_available_at <= e.at and e.payment_on >= e.at.session,
                         "Future or conflicting payment-date evidence")
                due, basis = Due(Phase.POST_OPEN_SETTLEMENT, on=e.payment_on), "verified"
            elif policy.lag_unit == "calendar_days":
                due, basis = Due(Phase.POST_OPEN_SETTLEMENT, on=e.at.session + timedelta(days=policy.fallback_lag)), "modeled"
            else:
                due, basis = Due(Phase.POST_OPEN_SETTLEMENT, session_index=index + policy.fallback_lag), "modeled"
            _require(e.due == due and e.reason == basis and e.entitlement_id not in entitlements, "Dividend payment assumption differs from its schedule")
            entitlements[e.entitlement_id] = Entitlement(e.entitlement_id, e.position_id, e.key, e.quantity,
                                                        e.price, e.at.session, due, basis, e.citation)
        elif e.kind == K.DIVIDEND_ACCRUAL:
            ent = entitlements[e.entitlement_id]
            amount = _round(ent.shares * ent.per_share)
            _require(not ent.accrued and not ent.unresolved_reason and ent.ex_date == e.at.session
                     and e.amount == amount and e.quantity == ent.shares,
                     "Dividend was accrued twice or for the wrong frozen quantity")
            _require(e.claim_id not in claims, "Duplicate dividend claim")
            new_claim()
            if ent.due is None:
                claims[e.claim_id] = (ClaimKind.DIVIDEND, amount, None, ent.position_id, 0,
                                      ent.basis, ent.citation, "accrued_held", "hold_receivable")
            else:
                claims[e.claim_id] = (ClaimKind.DIVIDEND, amount, ent.due, ent.position_id, 0,
                                      ent.basis, ent.citation, "scheduled", "")
            entitlements[e.entitlement_id] = replace(ent, accrued=True)
            post("dividend_receivable", amount)
        elif e.kind == K.DIVIDEND_PAYMENT_CONFIRMED:
            cid = e.claim_id
            kind, amount, due, pid, delivery, basis, citation, state, unresolved = claims[cid]
            _require(kind == ClaimKind.DIVIDEND and amount is not None and due is None,
                     "Only an unscheduled dividend can be confirmed")
            _require(e.payment_model == "gross_dividend_no_withholding_model",
                     "Confirmation lacks the explicit gross/no-withholding label")
            ent = entitlements.get(e.entitlement_id) if e.entitlement_id else None
            if ent is not None:
                _require(state == "accrued_held" and bool(unresolved),
                         "Only an accrued held dividend entitlement can be confirmed")
                _require(ent.position_id == pid and ent.accrued and ent.key == e.key,
                         "Confirmation has no matching accrued entitlement")
                ex_date = ent.ex_date
            else:
                _require(basis == "opening_balance" and state == "unbound",
                         "A dividend without an entitlement must be an opening-balance claim")
                _require(pid in positions and positions[pid]["key"] == e.key,
                         "Opening-balance confirmation has an unproven security scope")
                ex_date = sessions[0]
            _require(e.amount == amount and bool(e.citation), "Confirmation restated or failed to cite the amount")
            _require(type(e.payment_on) is date and e.payment_available_at is not None
                     and e.payment_available_at <= e.at and e.payment_on >= ex_date,
                     "Unavailable, future or pre-ex-date payment evidence")
            if e.payment_on > e.at.session or (e.payment_on == e.at.session and e.at.phase < Phase.POST_OPEN_SETTLEMENT):
                expected_due = Due(Phase.POST_OPEN_SETTLEMENT, on=e.payment_on)
            else:
                expected_due = Due(Phase.POST_OPEN_SETTLEMENT, session_index=index + 1)
            _require(e.due == expected_due, "Confirmed due date is not causal")
            _require(not e.postings, "Confirmation moved cash or postings")
            claims[cid] = (kind, amount, expected_due, pid, delivery, "verified", e.citation,
                           "confirmed", "")
        elif e.kind == K.DIVIDEND_VOIDED:
            kind, amount, due, pid, delivery, basis, citation, state, unresolved = claims.pop(e.claim_id)
            _require(kind == ClaimKind.DIVIDEND and amount is not None and e.amount == amount,
                     "Only a dividend with a known amount can be voided")
            proof = evidence()
            _require(proof.terms_verified and bool(e.reason), "Void needs verified evidence and a disclosed reason")
            ent = entitlements.get(e.entitlement_id) if e.entitlement_id else None
            if ent is not None:
                _require(state in ("accrued_held", "scheduled"), "Only an accrued dividend can be voided")
                _require(ent.position_id == pid and ent.key == e.key and ent.accrued,
                         "Void has no matching accrued entitlement")
            else:
                _require(basis == "opening_balance" and state == "unbound" and due is None,
                         "Only an evidenced opening-balance receivable can be voided without an entitlement")
                _require(pid in positions and positions[pid]["key"] == e.key,
                         "Opening-balance void has an unproven security scope")
            post("dividend_receivable", -amount)
            post("dividend_writeoff", amount)
        elif e.kind == K.DIVIDEND_CORRECTED:
            cid = e.claim_id
            kind, amount, due, pid, delivery, basis, citation, state, unresolved = claims[cid]
            _require(kind == ClaimKind.DIVIDEND and basis == "opening_balance"
                     and state == "unbound" and due is None and amount is not None,
                     "Only an unbound opening-balance dividend can be corrected")
            proof = evidence()
            _require(proof.terms_verified and bool(e.reason) and e.amount != 0,
                     "Correction needs verified evidence, a disclosed reason and a real change")
            _require(pid in positions and positions[pid]["key"] == e.key,
                     "Opening-balance correction has an unproven security scope")
            corrected = amount + e.amount
            _require(corrected > 0, "Correction produced a nonpositive receivable")
            claims[cid] = (kind, corrected, due, pid, delivery, basis, e.citation, state, unresolved)
            post("dividend_receivable", e.amount)
            post("dividend_correction", -e.amount)
        elif e.kind == K.DIVIDEND_BLOCKED:
            _require(bool(e.reason), "Unresolved dividend needs a reason")
            positions[e.position_id]["blocked"] = e.reason
        elif e.kind == K.LIABILITY:
            _require(e.amount > 0 and e.claim_id not in claims and bool(e.citation), "Unverified/duplicate liability")
            new_claim()
            claims[e.claim_id] = (ClaimKind.OTHER_PAYABLE, e.amount, e.due, "", 0, "verified", e.citation,
                                  "scheduled", "")
            post("liabilities", e.amount)
        elif e.kind == K.REDEMPTION:
            p = positions[e.position_id]
            _require(evidence().terms_verified and e.quantity == p["shares"] == p["delivered"]
                     and e.amount == _round(e.quantity * e.price), "Cash consideration was fabricated or double counted")
            cancel_key_orders()
            new_claim()
            claims[e.claim_id] = (ClaimKind.OTHER, e.amount, e.due, e.position_id, 0, "verified", e.citation,
                                  "scheduled", "")
            post("shares", -p["shares"], e.position_id)
            post("delivered", -p["delivered"], e.position_id)
            post("other_receivable", e.amount)
            p["shares"], p["delivered"] = 0, 0
        else:
            raise ValueError("Event recipe not implemented")
        supplied = Counter()
        for p in e.postings:
            supplied[(p.account, p.position_id)] += p.amount
        _require({k: v for k, v in expected.items() if v} == {k: v for k, v in supplied.items() if v},
                 f"{e.event_id}: postings differ from the independent economic recipe")
        _balances_valid(cash, claims, positions, orders, max_positions)
    return (cash, claims, positions, orders, entitlements,
            withdrawals_under_unknown_liability)


def _funded_cash_admission(expected_available, current_available, *,
                           withdrawals_under_unknown_liability):
    """Judge only the funded-cash admission rule and explain the verdict.

    Known liabilities preserve the original rule exactly: replayed available cash
    must be nonnegative and equal the snapshot's available cash. When replay cannot
    know available cash because a payable amount is unknown, a deposit is admitted
    only if the snapshot is equally unknown and replay observed no withdrawal while
    any payable amount was unknown. A withdrawal in that state is already a replay
    integrity failure; this guard keeps that failure explicit here too.
    """
    if expected_available is None:
        if withdrawals_under_unknown_liability:
            return False, ("unknown_liability_admission: withdrawal occurred while a "
                           "payable amount was unknown")
        if current_available is not None:
            return False, ("unknown_liability_admission: replayed funded cash is unknown "
                           "but the snapshot reports a known amount")
        return True, ("unknown_liability_admission: replayed and snapshot funded cash are "
                      "both unknown; the deposit is admissible and no withdrawal occurred "
                      "under unknown liabilities")
    if expected_available < 0:
        return False, ("known_liability_funding: replayed funded cash is negative")
    if current_available != expected_available:
        return False, ("known_liability_funding: snapshot funded cash does not equal "
                       "replayed funded cash")
    return True, ""


def reconcile(initial, events, current, *, sessions, max_positions=20) -> Reconciliation:
    try:
        (cash, claims, positions, orders, entitlements,
         withdrawals_under_unknown_liability) = _replay(initial, events, sessions, max_positions)
    except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError) as error:
        return Reconciliation((InvariantCheck("event_integrity", "FAIL", str(error)),), len(events))
    actual_orders = {o.order_id: _order(o) for o in current.orders}
    liabilities = [c[1] for c in claims.values() if c[0] in (ClaimKind.BUY_PAYABLE, ClaimKind.OTHER_PAYABLE)]
    expected_available = None if None in liabilities else cash - sum(liabilities, ZERO) - sum((o[0] for o in orders.values()), ZERO)
    funded_cash, funded_detail = _funded_cash_admission(
        expected_available, current.balances.cash_available,
        withdrawals_under_unknown_liability=withdrawals_under_unknown_liability)
    def total(kinds):
        amounts = [c[1] for c in claims.values() if c[0] in kinds]
        return None if None in amounts else sum(amounts, ZERO)
    balances = current.balances
    expected_clock = next((e.at for e in reversed(events) if e.at.session == sessions[-1]), None)
    if expected_clock is None:
        from .accounting_types import EventTime
        expected_clock = EventTime(sessions[-1], Phase.PRE_OPEN_EVIDENCE)
    settlement_complete = True
    for c in claims.values():
        if c[1] is None or c[2] is None:
            continue
        due = c[2]
        matured = len(sessions) - 1 >= due.session_index if due.session_index is not None else sessions[-1] >= due.on
        missed_day = len(sessions) > 1 and (len(sessions) - 1 > due.session_index if due.session_index is not None
                                           else sessions[-2] >= due.on)
        if missed_day or (matured and expected_clock.phase > due.phase):
            settlement_complete = False
    outcomes = (("event_integrity", True), ("cash_conservation", cash == current.cash),
        ("claim_conservation", claims == {c.claim_id: _claim(c) for c in current.claims} and len(claims) == len(current.claims)),
        ("share_delivery_and_identity", positions == {p.position_id: _position(p) for p in current.positions} and len(positions) == len(current.positions)),
        ("reservation_conservation", orders == actual_orders and len(orders) == len(current.orders)
             and balances.order_reserved == sum((o[0] for o in orders.values()), ZERO)),
        ("receivable_rollforward", balances.sale_receivable == total((ClaimKind.SALE,))
             and balances.dividend_receivable == total((ClaimKind.DIVIDEND,)) and balances.other_receivable == total((ClaimKind.OTHER,))
             and balances.receivables == total((ClaimKind.SALE, ClaimKind.DIVIDEND, ClaimKind.OTHER))),
        ("payable_rollforward", balances.liabilities == total((ClaimKind.BUY_PAYABLE, ClaimKind.OTHER_PAYABLE))
             and balances.payable_reserved == balances.liabilities),
        ("entitlement_conservation", entitlements == {e.dividend_id: e for e in current.entitlements}
             and len(entitlements) == len(current.entitlements)),
        ("snapshot_clock", current.as_of == expected_clock),
        ("settlement_phase_completion", settlement_complete),
        ("funded_cash", funded_cash),
        ("position_cap", sum(p.shares > 0 for p in current.positions) <= max_positions),
        ("claim_lifecycle", _lifecycle_valid(claims)))
    details = {"funded_cash": funded_detail} if funded_detail else {}
    return Reconciliation(tuple(InvariantCheck(name, "PASS" if passed else "FAIL",
                                              details.get(name, ""))
                                for name, passed in outcomes), len(events))
