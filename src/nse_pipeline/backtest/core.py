"""Small cash-account ledger. No market-data I/O, strategy, or simulation loop."""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import MappingProxyType

from .accounting_types import (BookState, BuyOrder, Claim, ClaimKind, Due, Entitlement, EventKind, EventTime,
                               Evidence, ExternalFlowDirection, LedgerEvent, OpeningCondition, OpeningPrint,
                               Phase, Position, Posting, ZERO, decimal, identifier, integer, money,
                               sha256_digest)
from .fees import FillQuote, Fees, affordable_buy, bounded_sell, floored_capacity, opening_quote
from .flow_provenance import external_flow_citation_digest


class Portfolio:
    def __init__(self, cash, *, on: date, claims=(), positions=(), max_positions=20):
        if type(on) is not date:
            raise ValueError("An initial observed session is required")
        claims, positions = tuple(claims), tuple(positions)
        if not all(isinstance(c, Claim) for c in claims) or not all(isinstance(p, Position) for p in positions):
            raise ValueError("Opening balances must use typed claims and positions")
        if len({c.claim_id for c in claims}) != len(claims):
            raise ValueError("Opening claims must have unique IDs")
        integer(max_positions, minimum=1)
        if (len({p.position_id for p in positions}) != len(positions)
                or len({p.key for p in positions if p.shares}) != sum(bool(p.shares) for p in positions)
                or any(p.opened_on >= on for p in positions)):
            raise ValueError("Opening positions need unique IDs/keys and prior-session ownership")
        self._state = BookState(money(cash), claims=tuple(claims), positions=tuple(positions),
                                as_of=EventTime(on, Phase.PRE_OPEN_EVIDENCE))
        self._max_positions = max_positions
        self._initial = self._state
        self._events: tuple[LedgerEvent, ...] = ()
        self._sessions = (on,)
        self._clock = EventTime(on, Phase.PRE_OPEN_EVIDENCE)
        self._validate_funding(self._state)
        self._closed_at = None

    @property
    def snapshot(self):
        return self._state

    @property
    def initial(self):
        return self._initial

    @property
    def balances(self):
        return self._state.balances

    @property
    def events(self):
        return self._events

    @property
    def positions(self):
        return MappingProxyType({p.position_id: p for p in self._state.positions})

    @property
    def session_index(self):
        return len(self._sessions) - 1

    @property
    def observed_sessions(self):
        return self._sessions

    def observe_session(self, on: date):
        """Caller supplies each actually observed exchange session, in order."""
        if type(on) is not date or on <= self._sessions[-1]:
            raise ValueError("Observed sessions must strictly increase")
        if any(self._matured(c) for c in self._state.claims):
            raise ValueError("Due settlements must be processed before advancing the observed session")
        self._sessions += (on,)
        self._clock = EventTime(on, Phase.PRE_OPEN_EVIDENCE)
        self._state = replace(self._state, as_of=self._clock)

    def _check(self, event_id, at, phases):
        identifier(event_id)
        if not isinstance(at, EventTime) or at.session != self._sessions[-1] or at < self._clock:
            raise ValueError("Event is outside the current observed session or out of order")
        if at.phase not in phases:
            raise ValueError("Event is not allowed in this session phase")
        if any(self._matured(c) and at.phase > c.due.phase for c in self._state.claims):
            raise ValueError("Due settlements must precede later session phases")
        if self._events and at == self._events[-1].at:
            raise ValueError("Each event needs a strictly ordered sequence within its phase")
        if any(e.event_id == event_id for e in self._events):
            raise ValueError("Event already applied")

    def _matured(self, claim):
        due = claim.due
        return (claim.amount is not None and due is not None and
                (self.session_index >= due.session_index if due.session_index is not None
                 else self._sessions[-1] >= due.on))

    def _future_settlement_phase(self, due, at):
        matured = (self.session_index >= due.session_index if due.session_index is not None else at.session >= due.on)
        if matured and at.phase >= due.phase:
            raise ValueError("New obligations cannot be scheduled into an already completed settlement phase")

    def _validate_funding(self, state):
        available = state.balances.cash_available
        known_payables = sum((c.amount for c in state.claims if c.amount is not None and
                              c.kind in (ClaimKind.BUY_PAYABLE, ClaimKind.OTHER_PAYABLE)), ZERO)
        if state.cash < known_payables + state.balances.order_reserved or (available is not None and available < 0):
            raise ValueError("Settled cash is insufficient to fully fund reservations and payables")
        position_ids = {p.position_id for p in state.positions}
        if any(c.kind == ClaimKind.BUY_PAYABLE and c.position_id not in position_ids for c in state.claims):
            raise ValueError("Purchase delivery has no corresponding economic position")
        if sum(p.shares > 0 for p in state.positions) > self._max_positions:
            raise ValueError("Open position cap exceeded")
        for position in state.positions:
            pending = sum(c.delivery_shares for c in state.claims
                          if c.kind == ClaimKind.BUY_PAYABLE and c.position_id == position.position_id)
            if position.delivered_shares + pending != position.shares:
                raise ValueError("Economic shares must reconcile with delivery and purchase obligations")

    def _record(self, event: LedgerEvent, state: BookState):
        self._validate_funding(state)
        self._state = replace(state, as_of=event.at)
        self._events += (event,)
        self._clock = event.at

    def record_external_flow(self, flow_id, amount, direction, citation, *, at: EventTime,
                             source_digest=None):
        """Book an evidenced deposit or withdrawal of settled cash exactly once.

        This is outside capital, not income or a claim: it moves settled cash and
        the external-flow account by the same signed amount. A withdrawal may use
        only cash that is not already reserved for an order or a known payable; a
        deposit remains admissible while a payable amount is unknown because it
        cannot consume protected cash.

        ``source_digest`` is optional for backward compatibility. When supplied,
        the kernel binds the citation and source digest into the event and replay
        verifies that binding. Authentication against an owner-owned source anchor
        is explicitly outside this rule.
        """
        flow_id = identifier(flow_id)
        event_id = f"flow:{flow_id}"
        self._check(event_id, at, (Phase.PRE_OPEN_EVIDENCE,))
        if not isinstance(direction, ExternalFlowDirection):
            raise ValueError("An explicit typed external-flow direction is required")
        identifier(citation)
        amount = money(amount, positive=True)
        source_digest = ("" if source_digest is None else
                         sha256_digest(source_digest, "External-flow source digest"))
        citation_digest = ("" if not source_digest else
                           external_flow_citation_digest(flow_id, amount, direction.value,
                                                         citation, at, source_digest))
        if direction == ExternalFlowDirection.WITHDRAWAL:
            available = self.balances.cash_available
            if available is None:
                raise ValueError("Unknown liabilities block an external withdrawal")
            if amount > available:
                raise ValueError("An external withdrawal cannot consume reserved or payable cash")
        signed = amount if direction == ExternalFlowDirection.DEPOSIT else -amount
        self._record(LedgerEvent(event_id, at, EventKind.EXTERNAL_FLOW, amount=amount,
                                 reason=direction.value, citation=citation,
                                 flow_source_digest=source_digest,
                                 flow_citation_digest=citation_digest,
                                 postings=(Posting("cash", signed), Posting("external_flows", signed))),
                     replace(self._state, cash=self._state.cash + signed))

    def reserve(self, order_id, key, quantity, budget, capacity_notional, *, at: EventTime, rank=0,
                condition=None):
        event_id = f"reserve:{identifier(order_id)}"
        self._check(event_id, at, (Phase.CLOSE_ORDERS,))
        identifier(key)
        integer(quantity, minimum=1)
        integer(rank)
        if condition is not None and not isinstance(condition, OpeningCondition):
            raise ValueError("A frozen order condition must be an OpeningCondition or None")
        budget, capacity = money(budget, positive=True), money(capacity_notional, positive=True)
        if self.balances.cash_available is None:
            raise ValueError("Unknown liabilities block new purchase reservations")
        order = BuyOrder(order_id, key, quantity, budget, capacity, at, self.session_index, rank, condition)
        after = replace(self._state, orders=self._state.orders + (order,))
        self._record(LedgerEvent(event_id, at, EventKind.RESERVE, amount=budget, order_id=order_id, quantity=quantity,
                                 capacity_notional=capacity, rank=rank, condition=condition,
                                 key=key, postings=(Posting("order_reserved", budget),)), after)

    def cancel(self, order_id, *, at: EventTime):
        event_id = f"cancel:{identifier(order_id)}"
        self._check(event_id, at, (Phase.PRE_OPEN_ACTIONS, Phase.CLOSE_ORDERS, Phase.CLOSE_AUDIT))
        order = next((o for o in self._state.orders if o.order_id == order_id), None)
        if order is None:
            raise ValueError("No outstanding order to cancel")
        after = replace(self._state, orders=tuple(o for o in self._state.orders if o != order))
        self._record(LedgerEvent(event_id, at, EventKind.CANCEL, amount=order.reserved, order_id=order_id,
                                 key=order.key, postings=(Posting("order_reserved", -order.reserved),)), after)

    def _position(self, position_id):
        position = self.positions.get(position_id)
        if position is None or position.shares == 0:
            raise ValueError("No open position")
        return position

    def _with_position(self, after):
        return tuple(p for p in self._state.positions if p.position_id != after.position_id) + (after,)

    @staticmethod
    def _opening(opening, key, at):
        if not isinstance(opening, OpeningPrint) or opening.key != key or opening.session != at.session:
            raise ValueError("Opening print must match the order's security and current session")

    def buy(self, order_id, opening, costs, execution, *, at: EventTime):
        event_id = f"buy:{identifier(order_id)}"
        self._check(event_id, at, (Phase.OPEN_FILLS,))
        order = next((o for o in self._state.orders if o.order_id == order_id), None)
        if order is None or self.session_index != order.signal_session_index + 1:
            raise ValueError("Order is not live at this next-session opening")
        self._opening(opening, order.key, at)
        live = [o for o in self._state.orders if o.signal_session_index + 1 == self.session_index]
        if order != min(live, key=lambda o: (o.rank, o.key, o.order_id)):
            raise ValueError("Buys must follow frozen signal rank/key order")
        position = next((p for p in self._state.positions if p.key == order.key and p.shares), None)
        position_id = position.position_id if position else f"position:{event_id}"
        condition_met = None if order.condition is None else order.condition.met(opening.price)
        if condition_met is False:
            # The predeclared band was false at the raw opening. Release the reserve
            # through this causal phase-40 skip event; never fabricate a fill/claim.
            quote = replace(opening_quote("buy", 0, opening.price, costs, execution),
                            reason="opening_condition_unmet")
            orders = tuple(o for o in self._state.orders if o != order)
            self._record(LedgerEvent(
                event_id, at, EventKind.BUY_SKIPPED, quantity=0, price=quote.fill_price,
                position_id=position_id, key=order.key, order_id=order_id,
                released_reserve=order.reserved,
                postings=(Posting("order_reserved", -order.reserved),),
                fees=quote.fees.components, reason="opening_condition_unmet",
                reference_price=quote.reference_price, cost_spec=costs, execution_spec=execution,
                condition=order.condition, condition_met=False),
                replace(self._state, orders=orders))
            return quote
        if self.balances.cash_available is None:
            raise ValueError("Unknown liabilities block purchases")
        if position and position.blocked_reason:
            raise ValueError("Unresolved position cannot be topped up")
        quote = affordable_buy(order.quantity, opening.price, order.reserved, order.capacity_notional, costs, execution)
        if position is None and sum(p.shares > 0 for p in self._state.positions) >= self._max_positions:
            quote = FillQuote(0, quote.reference_price, quote.fill_price, ZERO, Fees(), "position_limit",
                              quote.applied_principal_cap)
        orders = tuple(o for o in self._state.orders if o != order)
        postings = [Posting("order_reserved", -order.reserved)]
        state = replace(self._state, orders=orders)
        claim_id, due = "", None
        if position is None and any(p.position_id == position_id for p in self._state.positions):
            raise ValueError("A closed or pre-existing internal position ID cannot be reused")
        amount = quote.notional + quote.fees.total
        if quote.quantity:
            position = (replace(position, shares=position.shares + quote.quantity) if position else
                        Position(position_id, order.key, quote.quantity, at.session, delivered_shares=0))
            due = Due(Phase.PRE_OPEN_SETTLEMENT, session_index=self.session_index + execution.trade_settlement_lag_sessions)
            claim_id = f"trade:{event_id}"
            self._unused_claim(claim_id)
            claim = Claim(claim_id, ClaimKind.BUY_PAYABLE, amount, "opening execution proxy", due,
                          position_id, quote.quantity, "modeled")
            state = replace(state, positions=self._with_position(position), claims=state.claims + (claim,))
            postings += [Posting("liabilities", amount), Posting("shares", quote.quantity, position_id)]
        event = LedgerEvent(event_id, at, EventKind.BUY if quote.quantity else EventKind.BUY_SKIPPED,
                            amount=amount, quantity=quote.quantity, price=quote.fill_price, fee=quote.fees.total,
                            position_id=position_id, key=order.key, order_id=order_id, claim_id=claim_id,
                            released_reserve=order.reserved, postings=tuple(postings), fees=quote.fees.components,
                            due=due, reason=quote.reason, reference_price=quote.reference_price,
                            cost_spec=costs, execution_spec=execution, condition=order.condition,
                            condition_met=condition_met)
        self._record(event, state)
        return quote

    def sell(self, trade_id, position_id, quantity, opening, costs, execution, *, at: EventTime,
             condition=None, capacity_notional=None):
        event_id = f"sell:{identifier(trade_id)}"
        self._check(event_id, at, (Phase.OPEN_FILLS,))
        if condition is not None and not isinstance(condition, OpeningCondition):
            raise ValueError("A frozen sell condition must be an OpeningCondition or None")
        if capacity_notional is not None:
            # An explicit frozen per-order cap is floored to paise and never rounded
            # upward; None stays the disclosed legacy "no per-order capacity" state.
            capacity_notional = floored_capacity(capacity_notional)
        if any(e.at.session == at.session and e.kind in (EventKind.BUY, EventKind.BUY_SKIPPED) for e in self._events):
            raise ValueError("Opening sells must precede opening buys")
        position = self._position(position_id)
        self._opening(opening, position.key, at)
        integer(quantity, minimum=1)
        condition_met = None if condition is None else condition.met(opening.price)
        if condition_met is False:
            # Condition applies to the raw opening print, never to a slipped fill.
            quote = replace(opening_quote("sell", 0, opening.price, costs, execution),
                            reason="opening_condition_unmet")
            self._record(LedgerEvent(
                event_id, at, EventKind.SELL_SKIPPED, position_id=position_id,
                key=position.key, price=quote.fill_price, reference_price=quote.reference_price,
                cost_spec=costs, execution_spec=execution, fees=quote.fees.components,
                requested_quantity=quantity, reason="opening_condition_unmet",
                condition=condition, condition_met=False,
                sell_capacity_notional=capacity_notional), self._state)
            return quote
        if position.blocked_reason or quantity > position.delivered_shares:
            raise ValueError("Cannot sell blocked, short, or not-yet-delivered shares")
        quote = bounded_sell(quantity, opening.price, costs, execution, capacity_notional)
        if quote.quantity == 0:
            self._record(LedgerEvent(event_id, at, EventKind.SELL_SKIPPED, position_id=position_id,
                key=position.key, price=quote.fill_price, reference_price=quote.reference_price,
                cost_spec=costs, execution_spec=execution, fees=quote.fees.components,
                requested_quantity=quantity, reason=quote.reason,
                condition=condition, condition_met=condition_met,
                sell_capacity_notional=capacity_notional), self._state)
            return quote
        requested = quantity
        quantity = quote.quantity
        net = quote.notional - quote.fees.total
        if net < 0:
            raise ValueError("Fees exceed sale proceeds")
        due = Due(Phase.PRE_OPEN_SETTLEMENT, session_index=self.session_index + execution.trade_settlement_lag_sessions)
        claim_id = f"trade:{event_id}"
        self._unused_claim(claim_id)
        claim = Claim(claim_id, ClaimKind.SALE, net, "opening execution proxy", due, position_id, basis="modeled")
        changed = replace(position, shares=position.shares - quantity, delivered_shares=position.delivered_shares - quantity)
        state = replace(self._state, positions=self._with_position(changed), claims=self._state.claims + (claim,))
        self._record(LedgerEvent(event_id, at, EventKind.SELL, amount=net, quantity=quantity,
            price=quote.fill_price, fee=quote.fees.total, fees=quote.fees.components,
            position_id=position_id, key=position.key, claim_id=claim_id, due=due,
            reference_price=quote.reference_price, cost_spec=costs, execution_spec=execution,
            requested_quantity=requested, condition=condition, condition_met=condition_met,
            sell_capacity_notional=capacity_notional,
            postings=(Posting("shares", -quantity, position_id), Posting("delivered", -quantity, position_id),
                      Posting("sale_receivable", net))), state)
        return quote

    def settle(self, claim_id, *, at: EventTime):
        claim_id = identifier(claim_id)
        claim = next((c for c in self._state.claims if c.claim_id == claim_id), None)
        if claim is None:
            raise ValueError("claim_absent: there is no open claim with this ID")
        event_id = f"settle:{claim_id}"
        self._check(event_id, at, (Phase.PRE_OPEN_SETTLEMENT, Phase.POST_OPEN_SETTLEMENT))
        if claim.due is None:
            raise ValueError("unbound_payment_evidence: a claim without a causal due date cannot settle")
        if claim.amount is None or not claim.due.reached(at, self.session_index):
            raise ValueError("Claim is unresolved, already paid, or not due in this phase")
        payable = claim.kind in (ClaimKind.BUY_PAYABLE, ClaimKind.OTHER_PAYABLE)
        delta = -claim.amount if payable else claim.amount
        account = "liabilities" if payable else claim.kind.value
        kind = {ClaimKind.BUY_PAYABLE: EventKind.SETTLE_BUY, ClaimKind.OTHER_PAYABLE: EventKind.SETTLE_LIABILITY,
                ClaimKind.SALE: EventKind.SETTLE_SALE, ClaimKind.DIVIDEND: EventKind.SETTLE_DIVIDEND,
                ClaimKind.OTHER: EventKind.SETTLE_OTHER}[claim.kind]
        state = replace(self._state, cash=self._state.cash + delta,
                        claims=tuple(c for c in self._state.claims if c != claim))
        postings = [Posting("cash", delta), Posting(account, -claim.amount)]
        if claim.kind == ClaimKind.BUY_PAYABLE:
            position = self._position(claim.position_id)
            state = replace(state, positions=self._with_position(replace(position,
                        delivered_shares=position.delivered_shares + claim.delivery_shares)))
            postings.append(Posting("delivered", claim.delivery_shares, claim.position_id))
        self._record(LedgerEvent(event_id, at, kind, amount=claim.amount, claim_id=claim_id,
            position_id=claim.position_id, delivery_quantity=claim.delivery_shares, due=claim.due,
            citation=claim.citation, postings=tuple(postings)), state)

    def apply_share_action(self, position_id, action, *, at: EventTime):
        event_id = f"action:{action.action_id}"
        self._check(event_id, at, (Phase.PRE_OPEN_ACTIONS,))
        if position_id is None:
            key = action.evidence.key
            action.evidence.require_applicable(key, at)
            canceled = tuple(o for o in self._state.orders if o.key == key)
            if not canceled or any(p.key == key and p.shares for p in self._state.positions):
                raise ValueError("Order-only action requires an unheld security with pending orders")
            released = sum((o.reserved for o in canceled), ZERO)
            self._record(LedgerEvent(event_id, at, EventKind.ACTION_ORDERS_CANCELED, key=key,
                evidence=action.evidence, citation=action.evidence.citation, factor=action.factor,
                canceled_orders=tuple(o.order_id for o in canceled), released_reserve=released,
                postings=(Posting("order_reserved", -released),)),
                replace(self._state, orders=tuple(o for o in self._state.orders if o not in canceled)))
            return
        position = self._position(position_id)
        action.evidence.require_applicable(position.key, at)
        if position.blocked_reason:
            raise ValueError("An unresolved earlier action needs explicit resolution first")
        pending = [c for c in self._state.claims if c.kind == ClaimKind.BUY_PAYABLE and c.position_id == position_id]
        quantities = [position.shares, position.delivered_shares] + [c.delivery_shares for c in pending]
        transformed = [q * action.factor for q in quantities]
        reason = ("unverified_action_terms" if not action.evidence.terms_verified else
                  "simultaneous_share_dividend_units_unsupported" if any(
                      e.position_id == position_id and e.ex_date == at.session for e in self._state.entitlements) else
                  "fractional_entitlement_unsupported" if any(q.denominator != 1 for q in transformed) else "")
        canceled = tuple(o for o in self._state.orders if o.key == position.key)
        released = sum((o.reserved for o in canceled), ZERO)
        state = replace(self._state, orders=tuple(o for o in self._state.orders if o not in canceled))
        postings = [Posting("order_reserved", -released)] if released else []
        if reason:
            changed = replace(position, blocked_reason=reason)
            kind = EventKind.ACTION_BLOCKED
            affected = tuple(e for e in state.entitlements if e.position_id == position_id and e.ex_date == at.session)
            ids = {e.dividend_id for e in affected}
            if ids:
                state = replace(state, entitlements=tuple(replace(e, due=None, unresolved_reason=reason)
                    if e.dividend_id in ids else e for e in state.entitlements))
                changed_claims = []
                for claim in state.claims:
                    if claim.claim_id in {f"dividend:{i}" for i in ids}:
                        if claim.amount is not None:
                            postings += [Posting("dividend_receivable", -claim.amount), Posting("unknown_dividend_claims", 1)]
                        claim = replace(claim, amount=None, due=None, state="unbound",
                                        unresolved_reason=reason)
                    changed_claims.append(claim)
                state = replace(state, claims=tuple(changed_claims))
        else:
            changed = replace(position, shares=int(transformed[0]), delivered_shares=int(transformed[1]))
            replacements = {c.claim_id: replace(c, delivery_shares=int(q)) for c, q in zip(pending, transformed[2:])}
            state = replace(state, claims=tuple(replacements.get(c.claim_id, c) for c in state.claims))
            postings += [Posting("shares", changed.shares - position.shares, position_id),
                         Posting("delivered", changed.delivered_shares - position.delivered_shares, position_id)]
            kind = EventKind.SPLIT if action.kind == "split" else EventKind.BONUS
        state = replace(state, positions=self._with_position(changed))
        self._record(LedgerEvent(event_id, at, kind, quantity=position.shares,
            delivery_quantity=position.delivered_shares, position_id=position_id, key=position.key,
            released_reserve=released, canceled_orders=tuple(o.order_id for o in canceled),
            factor=action.factor, citation=action.evidence.citation, evidence=action.evidence,
            reason=reason, postings=tuple(postings)), state)

    def entitle_dividend(self, position_id, terms, policy, *, at: EventTime):
        from .dividends import payment_schedule
        event_id = f"entitle:{terms.dividend_id}"
        self._check(event_id, at, (Phase.PRE_OPEN_ACTIONS,))
        position = self._position(position_id)
        terms.evidence.require_applicable(position.key, at)
        if not terms.evidence.terms_verified or position.opened_on >= at.session or position.blocked_reason:
            raise ValueError("Entitlement is not established for this cum-dividend holding")
        if any(e.position_id == position_id and e.at.session == at.session and
               e.kind in (EventKind.SPLIT, EventKind.BONUS) for e in self._events):
            changed = replace(position, blocked_reason="simultaneous_share_dividend_units_unsupported")
            self._record(LedgerEvent(event_id, at, EventKind.DIVIDEND_BLOCKED,
                position_id=position_id, key=position.key, reason=changed.blocked_reason),
                replace(self._state, positions=self._with_position(changed)))
            return
        due, basis, citation = payment_schedule(terms, policy, self.session_index, at)
        entitlement = Entitlement(terms.dividend_id, position_id, position.key, position.shares,
                                  terms.per_share, at.session, due, basis, citation)
        self._record(LedgerEvent(event_id, at, EventKind.DIVIDEND_ENTITLEMENT, quantity=position.shares,
            price=terms.per_share, position_id=position_id, key=position.key, due=due, citation=citation,
            reason=basis, evidence=terms.evidence, dividend_policy=policy, entitlement_id=terms.dividend_id,
            payment_on=terms.payment.on if terms.payment is not None and terms.payment.available_at <= at else None,
            payment_available_at=terms.payment.available_at if terms.payment is not None and terms.payment.available_at <= at else None),
            replace(self._state, entitlements=self._state.entitlements + (entitlement,)))

    def accrue_dividend(self, dividend_id, *, at: EventTime):
        event_id = f"accrue:{identifier(dividend_id)}"
        self._check(event_id, at, (Phase.PRE_OPEN_ACTIONS,))
        entitlement = next((e for e in self._state.entitlements if e.dividend_id == dividend_id), None)
        if entitlement is None or entitlement.accrued or entitlement.ex_date != at.session:
            raise ValueError("Missing, already accrued, or incorrectly dated entitlement")
        if entitlement.unresolved_reason:
            raise ValueError("Dividend quantity/share units are unresolved")
        amount = money(entitlement.shares * entitlement.per_share)
        claim_id = f"dividend:{dividend_id}"
        self._unused_claim(claim_id)
        if entitlement.due is None:
            claim = Claim(claim_id, ClaimKind.DIVIDEND, amount, entitlement.citation, None,
                          entitlement.position_id, basis=entitlement.basis,
                          state="accrued_held", unresolved_reason="hold_receivable")
        else:
            claim = Claim(claim_id, ClaimKind.DIVIDEND, amount, entitlement.citation, entitlement.due,
                          entitlement.position_id, basis=entitlement.basis)
        state = replace(self._state, claims=self._state.claims + (claim,), entitlements=tuple(
            replace(e, accrued=True) if e == entitlement else e for e in self._state.entitlements))
        self._record(LedgerEvent(event_id, at, EventKind.DIVIDEND_ACCRUAL, amount=amount,
            quantity=entitlement.shares, price=entitlement.per_share, position_id=entitlement.position_id,
            key=entitlement.key, claim_id=claim_id, due=entitlement.due, citation=entitlement.citation,
            reason=entitlement.basis, entitlement_id=dividend_id,
            postings=(Posting("dividend_receivable", amount),)), state)

    def _unused_claim(self, claim_id):
        if any(c.claim_id == claim_id for c in self._state.claims) or any(e.claim_id == claim_id for e in self._events):
            raise ValueError("Financial claim ID has already been used")

    def _claim_key(self, claim):
        """Resolve a claim's security identity only when its position proves it."""
        if not claim.position_id:
            raise ValueError(
                "claim_position_absent: an opening-balance dividend claim needs a proven "
                "security position; identity is never inferred from its citation")
        position = self.positions.get(claim.position_id)
        if position is None or position.shares == 0:
            raise ValueError("claim_position_absent: the claim's security position is not open")
        return position.key

    def confirm_dividend_payment(self, dividend_id, payment, *, at: EventTime):
        """Attach verified payment evidence; cash and NAV do not move here.

        An accrued entitlement uses its ex-date. An opening-balance receivable has
        no inferred ex-date or schedule; its verified payment may be scheduled
        only from the first observed session, and only with full gross evidence.
        """
        from .dividends import GROSS_DIVIDEND_NO_WITHHOLDING_MODEL, VerifiedPayment, validate_confirmation
        dividend_id = identifier(dividend_id)
        if not isinstance(payment, VerifiedPayment):
            raise ValueError("verified_payment_required: a typed payment notice is required")
        if not isinstance(at, EventTime):
            raise ValueError("An explicit confirmation event time is required")
        if payment.settlement_model != GROSS_DIVIDEND_NO_WITHHOLDING_MODEL:
            raise ValueError("gross_dividend_no_withholding_model_required: unsupported payment model")
        claim_id = f"dividend:{dividend_id}"
        claim = next((c for c in self._state.claims if c.claim_id == claim_id), None)
        if claim is None:
            raise ValueError("claim_absent: there is no accrued dividend claim with this ID")
        if claim.state == "confirmed":
            raise ValueError("already_confirmed: this dividend payment was already confirmed")
        if claim.state == "voided":
            raise ValueError("not_confirmable: this dividend claim was voided")
        entitlement = next((e for e in self._state.entitlements if e.dividend_id == dividend_id), None)
        if entitlement is not None:
            if claim.kind != ClaimKind.DIVIDEND or claim.state != "accrued_held":
                raise ValueError("not_confirmable: only an accrued held dividend can be confirmed")
            key, entitlement_id, ex_date = entitlement.key, dividend_id, entitlement.ex_date
        else:
            if (claim.kind != ClaimKind.DIVIDEND or claim.basis != "opening_balance"
                    or claim.state != "unbound" or claim.due is not None):
                raise ValueError(
                    "not_confirmable: only an accrued held dividend or an evidenced "
                    "opening-balance receivable can be confirmed")
            key, entitlement_id, ex_date = self._claim_key(claim), "", self._initial.as_of.session
        due = validate_confirmation(claim, payment, ex_date, at, self.session_index)
        event_id = f"confirm-dividend:{dividend_id}"
        self._check(event_id, at, (Phase.PRE_OPEN_EVIDENCE, Phase.PRE_OPEN_ACTIONS, Phase.CLOSE_VALUATION))
        confirmed = replace(claim, due=due, state="confirmed", basis="verified",
                            unresolved_reason="", citation=payment.citation)
        state = replace(self._state, claims=tuple(
            confirmed if c.claim_id == claim_id else c for c in self._state.claims))
        self._record(LedgerEvent(event_id, at, EventKind.DIVIDEND_PAYMENT_CONFIRMED,
            amount=claim.amount, claim_id=claim_id, position_id=claim.position_id,
            key=key, due=due, citation=payment.citation, reason="confirmed",
            payment_on=payment.on, payment_available_at=payment.available_at,
            payment_model=payment.settlement_model, entitlement_id=entitlement_id), state)
        return confirmed

    def void_dividend(self, dividend_id, evidence, reason, *, at: EventTime):
        """Terminally write off an evidenced dividend receivable; never creates cash.

        An opening-balance receivable may be written off only when the claim itself
        discloses that basis and independent verified evidence is supplied. The
        opening snapshot and every earlier event remain unchanged.
        """
        dividend_id = identifier(dividend_id)
        if not isinstance(evidence, Evidence):
            raise ValueError("verified_evidence_required: a typed documentary proof is required")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("A nonempty written-off reason is required")
        if not isinstance(at, EventTime):
            raise ValueError("An explicit void event time is required")
        claim_id = f"dividend:{dividend_id}"
        claim = next((c for c in self._state.claims if c.claim_id == claim_id), None)
        if claim is None:
            raise ValueError("claim_absent: there is no accrued dividend claim with this ID")
        if claim.state == "confirmed":
            raise ValueError("not_voidable: a confirmed claim must be settled, not voided")
        if claim.kind != ClaimKind.DIVIDEND:
            raise ValueError("not_voidable: only a dividend claim can be written off")
        if claim.amount is None:
            raise ValueError("not_voidable: an unknown dividend amount cannot be written off")
        if not evidence.terms_verified:
            raise ValueError("verified_evidence_required: voiding requires verified evidence")
        entitlement = next((e for e in self._state.entitlements if e.dividend_id == dividend_id), None)
        if entitlement is not None:
            if claim.state not in ("accrued_held", "scheduled"):
                raise ValueError("not_voidable: only an accrued dividend claim can be voided")
            key, entitlement_id = entitlement.key, dividend_id
        else:
            if claim.basis != "opening_balance" or claim.state != "unbound" or claim.due is not None:
                raise ValueError(
                    "not_voidable: only an accrued dividend or an evidenced opening-balance "
                    "receivable can be written off")
            position_key = self._claim_key(claim)
            if position_key != evidence.key:
                raise ValueError("verified_evidence_required: evidence is for a different security")
            key, entitlement_id = evidence.key, ""
        evidence.require_applicable(key, at)
        event_id = f"void-dividend:{dividend_id}"
        self._check(event_id, at, (Phase.PRE_OPEN_ACTIONS,))
        amount = claim.amount
        state = replace(self._state, claims=tuple(c for c in self._state.claims if c.claim_id != claim_id))
        self._record(LedgerEvent(event_id, at, EventKind.DIVIDEND_VOIDED, amount=amount,
            claim_id=claim_id, position_id=claim.position_id, key=key,
            citation=evidence.citation, reason=reason, evidence=evidence,
            entitlement_id=entitlement_id,
            postings=(Posting("dividend_receivable", -amount), Posting("dividend_writeoff", amount))), state)

    def correct_opening_dividend(self, dividend_id, corrected_amount, evidence, reason, *, at: EventTime):
        """Correct an evidenced opening-balance receivable through a new event.

        The opening state and all prior events remain immutable. An accrued
        dividend is never restated; only a claim that explicitly discloses
        ``opening_balance`` provenance is eligible.
        """
        dividend_id = identifier(dividend_id)
        if not isinstance(evidence, Evidence):
            raise ValueError("verified_evidence_required: a typed documentary proof is required")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("A nonempty correction reason is required")
        if not isinstance(at, EventTime):
            raise ValueError("An explicit correction event time is required")
        corrected = money(corrected_amount, positive=True)
        claim_id = f"dividend:{dividend_id}"
        claim = next((c for c in self._state.claims if c.claim_id == claim_id), None)
        if claim is None:
            raise ValueError("claim_absent: there is no dividend claim with this ID")
        if (claim.kind != ClaimKind.DIVIDEND or claim.basis != "opening_balance"
                or claim.state != "unbound" or claim.due is not None or claim.amount is None):
            raise ValueError(
                "not_correctable: only an unbound opening-balance dividend receivable "
                "can be corrected")
        if any(e.dividend_id == dividend_id for e in self._state.entitlements):
            raise ValueError("not_correctable: an accrued dividend amount is never restated")
        if not evidence.terms_verified:
            raise ValueError("verified_evidence_required: correction requires verified evidence")
        position_key = self._claim_key(claim)
        if position_key != evidence.key:
            raise ValueError("verified_evidence_required: evidence is for a different security")
        evidence.require_applicable(evidence.key, at)
        delta = corrected - claim.amount
        if delta == 0:
            raise ValueError("correction_no_change: the corrected amount equals the current amount")
        sequence = 1 + sum(1 for e in self._events
                           if e.kind == EventKind.DIVIDEND_CORRECTED and e.claim_id == claim_id)
        event_id = f"correct-dividend:{dividend_id}:{sequence}"
        self._check(event_id, at, (Phase.PRE_OPEN_ACTIONS,))
        corrected_claim = replace(claim, amount=corrected, citation=evidence.citation)
        state = replace(self._state, claims=tuple(
            corrected_claim if c.claim_id == claim_id else c for c in self._state.claims))
        self._record(LedgerEvent(event_id, at, EventKind.DIVIDEND_CORRECTED, amount=delta,
            claim_id=claim_id, position_id=claim.position_id, key=evidence.key,
            citation=evidence.citation, reason=reason, evidence=evidence,
            postings=(Posting("dividend_receivable", delta),
                      Posting("dividend_correction", -delta))), state)

    def close(self, *, at: EventTime):
        """Authoritative terminal close; refuses any open claim or unpaid entitlement."""
        if self._closed_at is not None:
            raise ValueError("already_closed: this portfolio was already closed")
        self._check("close", at, (Phase.CLOSE_AUDIT,))
        unresolved = tuple(sorted(c.claim_id for c in self._state.claims if c.due is None))
        if unresolved:
            raise ValueError("open_unrealizable_claim: " + ", ".join(unresolved))
        scheduled = tuple(sorted(c.claim_id for c in self._state.claims))
        if scheduled:
            raise ValueError("open_scheduled_claim: " + ", ".join(scheduled))
        unaccrued = tuple(sorted(e.dividend_id for e in self._state.entitlements if not e.accrued))
        if unaccrued:
            raise ValueError("open_unaccrued_dividend: " + ", ".join(unaccrued))
        self._closed_at = at
        return True

    def recognize_liability(self, liability_id, amount, citation, due: Due, *, at: EventTime):
        event_id = f"liability:{identifier(liability_id)}"
        self._check(event_id, at, (Phase.PRE_OPEN_ACTIONS, Phase.CLOSE_VALUATION))
        identifier(citation)
        amount = money(amount, positive=True)
        if not isinstance(due, Due):
            raise ValueError("An explicit liability payment schedule is required")
        self._future_settlement_phase(due, at)
        self._unused_claim(event_id)
        claim = Claim(event_id, ClaimKind.OTHER_PAYABLE, amount, citation, due)
        self._record(LedgerEvent(event_id, at, EventKind.LIABILITY, amount=amount, claim_id=event_id,
            due=due, citation=citation, postings=(Posting("liabilities", amount),)),
            replace(self._state, claims=self._state.claims + (claim,)))

    def redeem(self, action_id, position_id, per_share, evidence, due: Due, *, at: EventTime):
        event_id = f"redeem:{identifier(action_id)}"
        self._check(event_id, at, (Phase.PRE_OPEN_ACTIONS,))
        position = self._position(position_id)
        evidence.require_applicable(position.key, at)
        if (not evidence.terms_verified or position.blocked_reason or position.delivered_shares != position.shares
                or not isinstance(due, Due) or due.phase != Phase.POST_OPEN_SETTLEMENT):
            raise ValueError("Cash consideration requires verified terms, explicit payment timing and delivered shares")
        price = decimal(per_share)
        self._future_settlement_phase(due, at)
        amount = money(position.shares * price)
        claim_id = f"consideration:{action_id}"
        self._unused_claim(claim_id)
        canceled = tuple(o for o in self._state.orders if o.key == position.key)
        released = sum((o.reserved for o in canceled), ZERO)
        claim = Claim(claim_id, ClaimKind.OTHER, amount, evidence.citation, due, position_id)
        changed = replace(position, shares=0, delivered_shares=0)
        state = replace(self._state, positions=self._with_position(changed), claims=self._state.claims + (claim,),
                        orders=tuple(o for o in self._state.orders if o not in canceled))
        self._record(LedgerEvent(event_id, at, EventKind.REDEMPTION, amount=amount, quantity=position.shares,
            price=price, position_id=position_id, key=position.key, claim_id=claim_id, due=due,
            evidence=evidence, citation=evidence.citation, released_reserve=released,
            canceled_orders=tuple(o.order_id for o in canceled),
            postings=(Posting("shares", -position.shares, position_id), Posting("delivered", -position.shares, position_id),
                      Posting("other_receivable", amount), Posting("order_reserved", -released))), state)
