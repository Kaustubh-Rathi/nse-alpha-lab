"""Deterministic target composition over observations, signals, eligibility and sizing.

This module is composition, not strategy. Every economic intent is caller supplied:
desired notional per security, any explicit unresolved declarations, any P3 screen
value/price and the capacity assumptions. Nothing here infers weights, trims, sells
or orders, and nothing here publishes strategy returns or performance statistics.

Composition is deliberately one-way and causal:

1. Membership is the research-eligibility screen by default. Official membership is
   used only through ``build_membership`` with an explicit ``MembershipEvidence``
   port, and that answer is intersected with the same research screen. An official
   request without evidence fails closed in ``universe.build_membership``.
2. Momentum is ranked over every research-eligible key, not merely over requested
   keys, so a rank number is the same rank the signal module publishes.
3. Only keys with an explicit ``TargetRequest`` participate. A requested key that is
   ineligible or unrankable is an explicit ``TargetExclusion`` with its reasons; it
   is never a silent drop.
4. The decision price is the same-session valid close read from the ledger. A stale
   mark, an invalid bar or a later session's close is never substituted.
5. As-of capacity is ``adv.capacity_notional`` with the caller's explicit
   ``security_status`` and ``allow_partial_adv``. ``None`` is unknown and produces
   no target (an explicit ``capacity_unknown`` exclusion), never a zero target. A
   known zero capacity is likewise an explicit ``capacity_zero`` exclusion.
6. ``sizing.size_portfolio_orders`` is the only sizing entry point. Desired
   notionals are clipped to capacity and aligned one-to-one with the rank order;
   explicit unresolved keys are mapped to their zero-based rank positions, and P3
   receives the explicit screen value/price aligned to those positions. A P3 screen
   value replaces the desired notional as the sizing base for that rank and is also
   clipped to capacity.
7. This module allocates principal notional, while the execution runner consumes
   integer shares. The documented composition step is therefore
   ``requested_quantity = floor(allocated_notional / decision_close)``. A zero
   allocation produces no target; it remains visible as an intent with its sizing
   reason.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from math import floor
from typing import Iterable

from nse_pipeline.specs import (CANONICAL_POLICIES, CostSpec, ExecutionSpec,
                                MomentumSpec, PortfolioSpec, UniverseSpec,
                                ValuationPolicy, resolve_sizing_policy)

from .accounting_types import ZERO, decimal, identifier, integer, money
from .adv import SECURITY_STATUSES, capacity_notional
from .evidence import ObservationLedger
from .formulas import AdjustmentEvidence, AdjustmentEvidenceSet, Formula
from .signals import (MomentumRanks, RankedKey, rank_admitted_momentum,
                      rank_formula_momentum)
from .sizing import SizingResult, size_portfolio_orders
from .universe import (Membership, MembershipEvidence, OFFICIAL_MEMBERSHIP_KIND,
                       RESEARCH_ELIGIBILITY_KIND, build_membership, build_universe)

CAPACITY_UNKNOWN_REASON = "capacity_unknown"
CAPACITY_ZERO_REASON = "capacity_zero"
NO_VALID_CLOSE_REASON = "no_valid_close_on_session"
NOT_IN_UNIVERSE_REASON = "not_in_universe_as_of"

TARGET_LIMITATIONS = (
    "No strategy returns or performance statistics are produced; this plan is an "
    "explicit target composition only.",
    "Caller-supplied desired notionals, unresolved declarations, screen values, screen "
    "prices, security status and partial-ADV opt-in are unauthenticated inputs, not "
    "verified evidence.",
    "Capacity None means unknown, never zero. Unknown capacity and known zero capacity "
    "both produce no target and remain explicit exclusions.",
    "The decision price is the same-session valid close from the observation ledger; a "
    "missing, invalid, stale or future bar is never substituted.",
    "Research eligibility is not official index membership. Official membership requires "
    "explicit MembershipEvidence and is only ever intersected with the research screen.",
    "requested_quantity = floor(allocated_notional / decision_close) is a composition "
    "step: the canonical sizer allocates principal notional, not integer shares.",
    "Desired notional is clipped to as-of capacity before sizing. A P3 unresolved rank is "
    "sized from its explicit screen value, also clipped to capacity.",
    "Settled cash is a caller-supplied composition input; PortfolioSpec.initial_cash is "
    "reserved and is not treated as runtime cash. Existing positions, open orders, "
    "settlement and financing are outside this module.",
    "Weights, trims, sells and rebalance decisions are never inferred; only explicit buy "
    "requests can produce an intent.",
)


def _amount(value, *, positive=False) -> Decimal:
    try:
        return money(value, positive=positive)
    except TypeError as error:
        raise ValueError(
            "Amounts must be exact decimals or integer currency amounts, not floats or bools") from error


def _price(value) -> Decimal:
    try:
        return decimal(value, positive=True)
    except TypeError as error:
        raise ValueError("Prices must be exact decimals, not floats or bools") from error


def _decision_close(ledger: ObservationLedger, key: str, as_of: date) -> Decimal | None:
    """The same-session valid close, or None; never a stale or future bar."""
    bar = ledger.bar(key, as_of)
    if bar is None or not bar.valid_ohlcv or bar.close is None or bar.close <= 0:
        return None
    return Decimal(bar.close)


def _request_tuple(value) -> tuple[TargetRequest, ...]:
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        raise ValueError("requests must be an iterable of TargetRequest records")
    requests = tuple(value)
    for request in requests:
        if not isinstance(request, TargetRequest):
            raise ValueError("requests must contain TargetRequest records only")
    keys = tuple(request.key for request in requests)
    if len(set(keys)) != len(keys):
        raise ValueError("Every requested security key must appear once")
    return requests


def _key_tuple(value, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        raise ValueError(f"{name} must be an iterable of security keys")
    keys = tuple(identifier(key) for key in value)
    if len(set(keys)) != len(keys):
        raise ValueError(f"{name} must not repeat a security key")
    return keys


def _screen_mark_tuple(value) -> tuple[ScreenMark, ...]:
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        raise ValueError("screen_marks must be an iterable of ScreenMark records")
    marks = tuple(value)
    seen = set()
    for mark in marks:
        if not isinstance(mark, ScreenMark):
            raise ValueError("screen_marks must contain ScreenMark records only")
        if mark.key in seen:
            raise ValueError("A security key must have at most one screen mark")
        seen.add(mark.key)
    return marks


@dataclass(frozen=True)
class TargetRequest:
    """One explicit buy intent: a security key and its desired principal notional."""

    key: str
    desired_notional: Decimal

    def __post_init__(self):
        object.__setattr__(self, "key", identifier(self.key))
        object.__setattr__(self, "desired_notional",
                           _amount(self.desired_notional, positive=True))


@dataclass(frozen=True)
class ScreenMark:
    """Explicit P3 screen value/price for one unresolved security.

    The value is the caller's target notional at the screen mark and is clipped to
    as-of capacity. The price is used only by the canonical sizer for that rank; the
    plan's decision close remains the same-session ledger close.
    """

    key: str
    value: Decimal
    price: Decimal

    def __post_init__(self):
        object.__setattr__(self, "key", identifier(self.key))
        object.__setattr__(self, "value", _amount(self.value))
        object.__setattr__(self, "price", _price(self.price))


@dataclass(frozen=True)
class TargetIntent:
    """Per-key sizing result for one requested, ranked, capacity-known security."""

    key: str
    rank: int
    order_index: int
    desired_notional: Decimal
    capacity_notional: Decimal
    sizing_base_notional: Decimal
    allocated_notional: Decimal
    decision_close: Decimal
    requested_quantity: int
    sizing_reason: str

    def __post_init__(self):
        object.__setattr__(self, "key", identifier(self.key))
        integer(self.rank, minimum=1)
        integer(self.order_index)
        object.__setattr__(self, "desired_notional",
                           _amount(self.desired_notional, positive=True))
        object.__setattr__(self, "capacity_notional",
                           _amount(self.capacity_notional, positive=True))
        object.__setattr__(self, "sizing_base_notional", _amount(self.sizing_base_notional))
        object.__setattr__(self, "allocated_notional", _amount(self.allocated_notional))
        object.__setattr__(self, "decision_close", _price(self.decision_close))
        integer(self.requested_quantity)
        object.__setattr__(self, "sizing_reason", identifier(self.sizing_reason))
        if self.sizing_base_notional > self.capacity_notional:
            raise ValueError("A sizing base cannot exceed as-of capacity")
        if self.allocated_notional > self.sizing_base_notional:
            raise ValueError("An allocation cannot exceed its sizing base")
        expected_quantity = floor(Fraction(self.allocated_notional)
                                  / Fraction(self.decision_close))
        if self.requested_quantity != expected_quantity:
            raise ValueError(
                "requested_quantity must equal floor(allocated_notional / decision_close)")

    @property
    def capacity_clipped_notional(self) -> Decimal:
        """The requested notional after clipping to capacity (before P3 substitution)."""
        return min(self.desired_notional, self.capacity_notional)

    @property
    def is_target(self) -> bool:
        """A zero allocation, or a sub-share allocation, is not an order target."""
        return self.requested_quantity >= 1


@dataclass(frozen=True)
class TargetExclusion:
    """A requested key that cannot participate, with explicit reasons and evidence."""

    key: str
    desired_notional: Decimal
    reasons: tuple[str, ...]
    decision_close: Decimal | None = None
    capacity_notional: Decimal | None = None

    def __post_init__(self):
        object.__setattr__(self, "key", identifier(self.key))
        object.__setattr__(self, "desired_notional", _amount(self.desired_notional))
        reasons = tuple(identifier(reason) for reason in self.reasons)
        if not reasons:
            raise ValueError("An exclusion requires at least one explicit reason")
        if len(set(reasons)) != len(reasons):
            raise ValueError("Exclusion reasons must be unique")
        object.__setattr__(self, "reasons", reasons)
        if self.decision_close is not None:
            object.__setattr__(self, "decision_close", _price(self.decision_close))
        if self.capacity_notional is not None:
            object.__setattr__(self, "capacity_notional", _amount(self.capacity_notional))


@dataclass(frozen=True)
class TargetPlan:
    """Immutable as-of composition: membership, ranks, cash, intents and exclusions."""

    as_of: date
    membership: Membership
    ranks: MomentumRanks
    policy: str
    cash: Decimal
    security_status: str | None
    allow_partial_adv: bool
    unresolved_keys: tuple[str, ...]
    intents: tuple[TargetIntent, ...]
    exclusions: tuple[TargetExclusion, ...]
    sizing: SizingResult
    limitations: tuple[str, ...] = TARGET_LIMITATIONS

    def __post_init__(self):
        if type(self.as_of) is not date:
            raise ValueError("A plan requires an explicit as-of session")
        if not isinstance(self.membership, Membership) or not isinstance(self.ranks, MomentumRanks):
            raise ValueError("A plan requires membership and momentum evidence")
        if not isinstance(self.sizing, SizingResult):
            raise ValueError("A plan requires the canonical sizing result")
        if self.membership.as_of != self.as_of or self.ranks.as_of != self.as_of:
            raise ValueError("Membership and ranks must refer to the plan's as-of session")
        object.__setattr__(self, "policy", identifier(self.policy))
        if self.policy not in CANONICAL_POLICIES or self.policy != self.sizing.policy:
            raise ValueError("The plan policy must be the canonical sized policy")
        object.__setattr__(self, "cash", _amount(self.cash))
        if self.security_status not in SECURITY_STATUSES:
            raise ValueError(f"security_status must be one of {SECURITY_STATUSES}")
        if type(self.allow_partial_adv) is not bool:
            raise ValueError("allow_partial_adv must be a boolean")
        unresolved = _key_tuple(self.unresolved_keys, "unresolved_keys")
        intents = tuple(self.intents)
        exclusions = tuple(self.exclusions)
        limitations = tuple(self.limitations)
        for intent in intents:
            if not isinstance(intent, TargetIntent):
                raise ValueError("intents must contain TargetIntent records only")
        for exclusion in exclusions:
            if not isinstance(exclusion, TargetExclusion):
                raise ValueError("exclusions must contain TargetExclusion records only")
        if len(intents) != len(self.sizing.filled) or len(intents) != len(self.sizing.reasons):
            raise ValueError("Every sizing rank needs exactly one intent")
        if tuple(intent.order_index for intent in intents) != tuple(range(len(intents))):
            raise ValueError("Intent order_index must be the contiguous sizing rank order")
        intent_keys = tuple(intent.key for intent in intents)
        if len(set(intent_keys)) != len(intent_keys):
            raise ValueError("Intent keys must be unique")
        exclusion_keys = tuple(exclusion.key for exclusion in exclusions)
        if len(set(exclusion_keys)) != len(exclusion_keys):
            raise ValueError("Exclusion keys must be unique")
        if set(intent_keys).intersection(exclusion_keys):
            raise ValueError("A requested security cannot be both an intent and an exclusion")
        if not set(unresolved).issubset(set(intent_keys) | set(exclusion_keys)):
            raise ValueError("Every unresolved key must be a requested intent or exclusion")
        if not limitations or any(not isinstance(text, str) or not text.strip()
                                  for text in limitations):
            raise ValueError("A plan must carry explicit nonempty limitations")
        if self.cash != self.sizing.remaining_cash + sum(self.sizing.filled, ZERO) + self.sizing.fees:
            raise ValueError("Plan cash must reconcile to allocations, fees and remaining cash")
        object.__setattr__(self, "unresolved_keys", unresolved)
        object.__setattr__(self, "intents", intents)
        object.__setattr__(self, "exclusions", exclusions)
        object.__setattr__(self, "limitations", limitations)

    @property
    def order(self) -> tuple[str, ...]:
        """The sizing rank order, including intents that received zero allocation."""
        return tuple(intent.key for intent in self.intents)

    @property
    def targets(self) -> tuple[TargetIntent, ...]:
        """Only intents with at least one derived share; zero allocation is not a target."""
        return tuple(intent for intent in self.intents if intent.is_target)

    @property
    def target_keys(self) -> tuple[str, ...]:
        return tuple(intent.key for intent in self.targets)

    def intent_for(self, key: str) -> TargetIntent:
        wanted = identifier(key)
        for intent in self.intents:
            if intent.key == wanted:
                return intent
        raise KeyError(wanted)

    def exclusion_for(self, key: str) -> TargetExclusion:
        wanted = identifier(key)
        for exclusion in self.exclusions:
            if exclusion.key == wanted:
                return exclusion
        raise KeyError(wanted)


@dataclass(frozen=True)
class _Candidate:
    ranked: RankedKey
    request: TargetRequest
    decision_close: Decimal
    capacity: Decimal
    sizing_base: Decimal


def _membership_for(ledger: ObservationLedger, as_of: date, universe: UniverseSpec, *,
                    membership_kind: str,
                    membership_evidence: MembershipEvidence | None) -> Membership:
    if membership_kind == OFFICIAL_MEMBERSHIP_KIND:
        # The canonical fail-closed path: without an explicit evidence port this
        # raises UnsupportedMembershipKind rather than falling back to research.
        return build_membership(ledger, as_of, universe,
                                membership_evidence=membership_evidence)
    return build_universe(ledger, as_of, universe, kind=membership_kind,
                          membership_evidence=membership_evidence)


def compose_targets(
        ledger: ObservationLedger,
        as_of: date,
        *,
        universe: UniverseSpec,
        momentum: MomentumSpec,
        execution: ExecutionSpec,
        costs: CostSpec,
        portfolio: PortfolioSpec,
        valuation: ValuationPolicy,
        requests: Iterable[TargetRequest],
        settled_cash,
        security_status: str | None,
        allow_partial_adv: bool,
        membership_kind: str = RESEARCH_ELIGIBILITY_KIND,
        membership_evidence: MembershipEvidence | None = None,
        unresolved_keys: Iterable[str] = (),
        screen_marks: Iterable[ScreenMark] = (),
        formula: Formula | None = None,
        adjustment_evidence: AdjustmentEvidence | AdjustmentEvidenceSet | None = None,
) -> TargetPlan:
    """Compose an explicit, immutable as-of target plan; see the module docstring.

    ``settled_cash``, ``security_status`` and ``allow_partial_adv`` are required
    inputs so cash, capacity status and partial coverage cannot inherit an implicit
    assumption. ``unresolved_keys`` names securities whose caller-declared valuation
    is unresolved; the corresponding zero-based rank position is passed to
    ``size_portfolio_orders``. ``screen_marks`` supplies the explicit P3 value/price
    for those keys. A missing screen mark is passed as unavailable, which the sizer
    records as ``screen_value_unavailable`` rather than inventing a mark.

    An optional validated strict ``formula`` may shape signal ranks. It is the only
    non-builtin basis admitted here: no caller callable or ``basis_id`` override is
    accepted. The execution decision close is always the same-session valid raw
    ledger close, even when the ranks use adjusted formula prices. ``formula`` and
    ``adjustment_evidence`` are not used by ``size_portfolio_orders``.
    """
    if not isinstance(ledger, ObservationLedger):
        raise ValueError("Target composition needs an ObservationLedger")
    if type(as_of) is not date:
        raise ValueError("Target composition needs an explicit as-of date")
    for name, value, expected in (("universe", universe, UniverseSpec),
                                  ("momentum", momentum, MomentumSpec),
                                  ("execution", execution, ExecutionSpec),
                                  ("costs", costs, CostSpec),
                                  ("portfolio", portfolio, PortfolioSpec),
                                  ("valuation", valuation, ValuationPolicy)):
        if not isinstance(value, expected):
            raise ValueError(f"Target composition needs an explicit {expected.__name__}")
    if formula is not None and type(formula) is not Formula:
        raise ValueError("Target composition admits only a validated strict Formula")
    if adjustment_evidence is not None and type(adjustment_evidence) not in (
            AdjustmentEvidence, AdjustmentEvidenceSet):
        raise ValueError(
            "Target composition admits AdjustmentEvidence or AdjustmentEvidenceSet only")
    if formula is None and adjustment_evidence is not None:
        raise ValueError("adjustment_evidence requires an explicit strict formula")
    if formula is not None and formula.uses_raw and formula.uses_adjustment:
        raise ValueError(
            "Target composition cannot label a formula that mixes raw and adjusted "
            "price leaves")
    expected_basis = ("adj_close" if formula is not None and formula.uses_adjustment
                      else "close")
    if momentum.price_basis != expected_basis:
        raise ValueError(
            "Target composition validates momentum.price_basis consistently: "
            f"expected {expected_basis!r} for this formula, not {momentum.price_basis!r}; "
            "decision prices always remain the raw same-session close")
    if security_status not in SECURITY_STATUSES:
        raise ValueError(f"security_status must be one of {SECURITY_STATUSES}")
    if type(allow_partial_adv) is not bool:
        raise ValueError("allow_partial_adv must be a boolean")
    ledger.session_index(as_of)

    requested = _request_tuple(requests)
    request_by_key = {request.key: request for request in requested}

    unresolved = _key_tuple(unresolved_keys, "unresolved_keys")
    marks = _screen_mark_tuple(screen_marks)
    mark_by_key = {mark.key: mark for mark in marks}
    unknown_unresolved = set(unresolved) - set(request_by_key)
    if unknown_unresolved:
        raise ValueError("Every unresolved key must be explicitly requested: "
                         + ", ".join(sorted(unknown_unresolved)))
    extra_marks = set(mark_by_key) - set(unresolved)
    if extra_marks:
        raise ValueError("Screen marks require the key to be declared unresolved: "
                         + ", ".join(sorted(extra_marks)))

    # This validates the PortfolioSpec/ValuationPolicy combination before any market
    # work and supplies the canonical policy needed for P3 screen substitution.
    policy = resolve_sizing_policy(portfolio, valuation)
    if policy != "P3_size_including_unresolved_at_screen" and marks:
        raise ValueError("screen_marks are admitted only under the P3 screen-value policy")

    membership = _membership_for(ledger, as_of, universe,
                                 membership_kind=membership_kind,
                                 membership_evidence=membership_evidence)
    if formula is None:
        ranks = rank_admitted_momentum(ledger, as_of, membership.eligible, momentum)
    else:
        ranks = rank_formula_momentum(ledger, as_of, membership.eligible, formula,
                                      spec=momentum,
                                      adjustment_evidence=adjustment_evidence)

    eligible = set(membership.eligible)
    ranked_by_key = {row.key: row for row in ranks.ranked}
    unrankable = dict(ranks.unrankable)
    unresolved_set = frozenset(unresolved)
    cash = _amount(settled_cash)

    candidates: list[_Candidate] = []
    exclusions: list[TargetExclusion] = []
    for key in sorted(request_by_key):
        request = request_by_key[key]
        close = _decision_close(ledger, key, as_of)
        capacity = capacity_notional(ledger, key, as_of, execution,
                                     allow_partial_adv=allow_partial_adv,
                                     security_status=security_status)
        if key not in eligible:
            reasons = membership.reasons(key) or (NOT_IN_UNIVERSE_REASON,)
            exclusions.append(TargetExclusion(
                key, request.desired_notional, reasons, close, capacity))
            continue
        ranked = ranked_by_key.get(key)
        if ranked is None:
            reasons = (unrankable.get(key, "unranked_without_reason"),)
            exclusions.append(TargetExclusion(
                key, request.desired_notional, reasons, close, capacity))
            continue
        if close is None:
            exclusions.append(TargetExclusion(
                key, request.desired_notional, (NO_VALID_CLOSE_REASON,), None, capacity))
            continue
        if capacity is None:
            exclusions.append(TargetExclusion(
                key, request.desired_notional, (CAPACITY_UNKNOWN_REASON,), close, None))
            continue
        if capacity == 0:
            exclusions.append(TargetExclusion(
                key, request.desired_notional, (CAPACITY_ZERO_REASON,), close, capacity))
            continue
        if policy == "P3_size_including_unresolved_at_screen" and key in unresolved_set:
            mark = mark_by_key.get(key)
            sizing_base = ZERO if mark is None else min(mark.value, capacity)
        else:
            sizing_base = min(request.desired_notional, capacity)
        candidates.append(_Candidate(ranked, request, close, capacity, sizing_base))

    # The caller's rank positions are the canonical signal ranks. Candidate order is
    # rank then key, which is the allocation order the sizer and runner both use.
    candidates.sort(key=lambda candidate: (candidate.ranked.rank, candidate.ranked.key))
    position = {candidate.ranked.key: index for index, candidate in enumerate(candidates)}
    # Unresolved declarations for excluded/unrankable keys remain visible on the
    # plan but cannot be passed as ranks because no sizing position exists.
    unresolved_ordered = tuple(sorted(unresolved, key=lambda key: (
        position.get(key, len(position)), key)))
    unresolved_sizing_keys = tuple(key for key in unresolved_ordered if key in position)
    unresolved_ranks = tuple(position[key] for key in unresolved_sizing_keys)

    desired_in_rank_order = [min(candidate.request.desired_notional, candidate.capacity)
                             for candidate in candidates]
    prices = [candidate.decision_close for candidate in candidates]
    screen_values: list[Decimal | None] = []
    screen_prices: list[Decimal | None] = []
    for key in unresolved_sizing_keys:
        mark = mark_by_key.get(key)
        if mark is None:
            screen_values.append(None)
            screen_prices.append(None)
        else:
            screen_values.append(candidates[position[key]].sizing_base)
            screen_prices.append(mark.price)

    sizing = size_portfolio_orders(
        portfolio, valuation, cash, desired_in_rank_order, price=prices,
        costs=costs, execution=execution, universe=universe, momentum=momentum,
        unresolved_ranks=unresolved_ranks,
        screen_values=tuple(screen_values), screen_prices=tuple(screen_prices))

    intents = []
    for index, candidate in enumerate(candidates):
        allocated = sizing.filled[index]
        quantity = floor(Fraction(allocated) / Fraction(candidate.decision_close))
        intents.append(TargetIntent(
            key=candidate.ranked.key,
            rank=candidate.ranked.rank,
            order_index=index,
            desired_notional=candidate.request.desired_notional,
            capacity_notional=candidate.capacity,
            sizing_base_notional=candidate.sizing_base,
            allocated_notional=allocated,
            decision_close=candidate.decision_close,
            requested_quantity=quantity,
            sizing_reason=sizing.reasons[index]))

    return TargetPlan(
        as_of=as_of,
        membership=membership,
        ranks=ranks,
        policy=policy,
        cash=cash,
        security_status=security_status,
        allow_partial_adv=allow_partial_adv,
        unresolved_keys=unresolved_ordered,
        intents=tuple(intents),
        exclusions=tuple(exclusions),
        sizing=sizing,
    )
