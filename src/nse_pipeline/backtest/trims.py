"""Explicit rupee-target trims reduced to frozen integer-share intents.

This module is a pure composition step, not a strategy and not an execution path.
Every economic intent is caller supplied: one explicit target holding value per
existing position. The module converts that rupee target into an integer target
share count and a proposed integer reduction. It never infers weights, never uses
whole-portfolio NAV, and never creates cash, orders, fills, financing or strategy
performance statistics.

The decision price is the raw same-session close supplied by the caller. A
missing, invalid, zero/negative, duplicate or stale-session bar for a requested
position is an error, never a silent skip. A missing position, an inactive
position, a blocked/unresolved position, a missing capacity for a positive
reduction, or an unknown (``None``) capacity for a positive reduction is likewise
an error. A supplied capacity of exactly zero is *known* no-executable-liquidity,
not unknown: the requested quantity is still reported with a zero cap and a
``known_zero_capacity`` reason so the runner cannot silently fill it. This module
does not shrink the requested quantity from capacity; it freezes both facts for
the runner, which owns partial fills, expiry and settlement.

The target share count is ``floor(target_holding_value / decision_close)`` using
exact ``Fraction`` arithmetic. Planning moves neither shares nor cash. A rounding
difference is reflected only in actual fills and their settled proceeds; it is
never credited separately. No new financing is created.

Caller-supplied bars and capacities are unauthenticated inputs, not verified
evidence.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from fractions import Fraction
from math import floor

from nse_pipeline.specs import PortfolioSpec

from .accounting_types import (BookState, EventTime, Position, decimal, identifier,
                               integer, money)
from .observations import Bar

AT_OR_BELOW_TARGET_REASON = "at_or_below_target"
NO_DELIVERED_SHARES_REASON = "no_delivered_shares"
PROPOSED_TRIM_REASON = "proposed_trim"
KNOWN_ZERO_CAPACITY_REASON = "known_zero_capacity"
DISABLED_TRIMS_REASON = "trim_overweight_targets_disabled"

TRIM_LIMITATIONS = (
    "This module computes explicit integer-share reductions only; it creates no "
    "orders, fills, cash, claims or financing.",
    "Target holding values, bars and capacities are unauthenticated caller inputs, "
    "not verified evidence.",
    "A missing or invalid close, an unknown or missing capacity for a positive "
    "reduction, or an unknown/inactive/blocked position raises; nothing is "
    "silently skipped.",
    "Known zero capacity means known absence of executable liquidity, not unknown "
    "capacity. The proposed quantity is still reported with a zero cap and the "
    "runner must not fill it.",
    "target_shares = floor(target_holding_value / decision_close). Planning does "
    "not move cash or shares; a rounding difference is realized only through actual "
    "fills and settlement, never an artificial cash credit.",
    "Sale proceeds, including any rounding remainder, become available only through "
    "the runner's settled-cash rules; this module creates no artificial capital.",
    "No whole-portfolio NAV, strategy weight, equal-weight rule or cash denominator "
    "is used; every target value is explicitly supplied per position.",
    "Partial fills, expiry, settled cash and share-action safety remain the "
    "runner's rules; this module adds no new financing.",
)


def _amount(value) -> Decimal:
    try:
        return money(value)
    except TypeError as error:
        raise ValueError(
            "Amounts must be exact decimals or integer currency amounts, not floats "
            "or bools") from error


def _price(value) -> Decimal:
    try:
        result = decimal(value)
    except TypeError as error:
        raise ValueError("Prices must be exact decimals, not floats or bools") from error
    if result <= 0:
        raise ValueError("Prices must be positive")
    return result


def _capacity(value) -> Decimal:
    try:
        return decimal(value).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    except (TypeError, InvalidOperation) as error:
        raise ValueError("Capacity must be a supported exact nonnegative amount") from error


def _request_tuple(value) -> tuple["TrimRequest", ...]:
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        raise ValueError("requests must be an iterable of TrimRequest records")
    requests = tuple(value)
    for request in requests:
        if not isinstance(request, TrimRequest):
            raise ValueError("requests must contain TrimRequest records only")
    order_ids = tuple(request.order_id for request in requests)
    if len(set(order_ids)) != len(order_ids):
        raise ValueError("Every trim request order_id must be unique")
    position_ids = tuple(request.position_id for request in requests)
    if len(set(position_ids)) != len(position_ids):
        raise ValueError("At most one trim target is allowed per position")
    return requests


def _bar_map(bars, on: date) -> dict[str, Bar]:
    if isinstance(bars, (str, bytes)) or not hasattr(bars, "__iter__"):
        raise ValueError("bars must be an iterable of Bar records")
    result: dict[str, Bar] = {}
    for bar in tuple(bars):
        if not isinstance(bar, Bar):
            raise ValueError("bars must contain Bar records only")
        if bar.session != on:
            raise ValueError(
                f"bars must belong to the requested session {on}; got {bar.session}")
        if bar.key in result:
            raise ValueError(f"Duplicate bar for security {bar.key!r} in the session")
        result[bar.key] = bar
    return result


def _position_map(book: BookState) -> dict[str, Position]:
    result: dict[str, Position] = {}
    for position in book.positions:
        if not isinstance(position, Position):
            raise ValueError("book.positions must contain Position records only")
        if position.position_id in result:
            raise ValueError(
                f"Duplicate position_id {position.position_id!r} in the book snapshot")
        result[position.position_id] = position
    return result


def _known_capacity(capacities: Mapping, key: str, *, required: bool) -> Decimal | None:
    if key not in capacities:
        if required:
            raise ValueError(
                f"A known nonnegative capacity is required for a positive trim of {key!r}")
        return None
    value = capacities[key]
    if value is None:
        if required:
            raise ValueError(
                f"A known nonnegative capacity is required for a positive trim of {key!r}; "
                "None is unknown capacity")
        return None
    return _capacity(value)


@dataclass(frozen=True)
class TrimRequest:
    """One explicit rupee target for one existing position."""

    order_id: str
    position_id: str
    target_holding_value: Decimal
    rank: int = 0

    def __post_init__(self):
        object.__setattr__(self, "order_id", identifier(self.order_id))
        object.__setattr__(self, "position_id", identifier(self.position_id))
        object.__setattr__(self, "target_holding_value",
                           _amount(self.target_holding_value))
        object.__setattr__(self, "rank", integer(self.rank))


@dataclass(frozen=True)
class TrimDecision:
    """A frozen integer-share reduction derived from one explicit rupee target."""

    request: TrimRequest
    key: str
    decision_close: Decimal
    target_shares: int
    requested_quantity: int
    capacity_notional: Decimal | None
    reason: str

    def __post_init__(self):
        if not isinstance(self.request, TrimRequest):
            raise ValueError("A trim decision requires a TrimRequest")
        object.__setattr__(self, "key", identifier(self.key))
        object.__setattr__(self, "decision_close", _price(self.decision_close))
        object.__setattr__(self, "target_shares", integer(self.target_shares))
        object.__setattr__(self, "requested_quantity",
                           integer(self.requested_quantity))
        if self.capacity_notional is not None:
            object.__setattr__(self, "capacity_notional",
                                _capacity(self.capacity_notional))
        if self.requested_quantity > 0 and self.capacity_notional is None:
            raise ValueError(
                "A positive trim requires a known nonnegative capacity; None is unknown")
        object.__setattr__(self, "reason", identifier(self.reason))

    @property
    def is_trim(self) -> bool:
        """True only when a positive reduction is proposed."""
        return self.requested_quantity > 0

    @property
    def notional_at_decision_close(self) -> Decimal:
        """The proposed principal value of the reduction, before runner fills/costs."""
        return _amount(self.decision_close * self.requested_quantity)

    @property
    def capacity_below_request(self) -> bool:
        """Whether the frozen capacity is strictly below the proposed principal."""
        return (self.capacity_notional is not None
                and self.notional_at_decision_close > self.capacity_notional)


@dataclass(frozen=True)
class TrimPlan:
    """Immutable, canonically ordered set of trim decisions for one session."""

    on: date
    decisions: tuple[TrimDecision, ...]
    limitations: tuple[str, ...] = TRIM_LIMITATIONS

    def __post_init__(self):
        if type(self.on) is not date:
            raise ValueError("A trim plan requires an explicit session date")
        decisions = tuple(self.decisions)
        for decision in decisions:
            if not isinstance(decision, TrimDecision):
                raise ValueError("decisions must contain TrimDecision records only")
        order_ids = tuple(decision.request.order_id for decision in decisions)
        if len(set(order_ids)) != len(order_ids):
            raise ValueError("Trim decision order_ids must be unique")
        position_ids = tuple(decision.request.position_id for decision in decisions)
        if len(set(position_ids)) != len(position_ids):
            raise ValueError("At most one trim decision is allowed per position")
        ordered = tuple(sorted(
            decisions, key=lambda d: (d.request.rank, d.key, d.request.order_id)))
        if decisions != ordered:
            raise ValueError(
                "Trim decisions must be ordered by rank, key, then order_id")
        for decision in decisions:
            expected = floor(
                Fraction(decision.request.target_holding_value)
                / Fraction(decision.decision_close))
            if decision.target_shares != expected:
                raise ValueError(
                    "target_shares must equal floor(target_holding_value / decision_close)")
        limitations = tuple(self.limitations)
        if not limitations or any(not isinstance(text, str) or not text.strip()
                                  for text in limitations):
            raise ValueError("A trim plan must carry explicit nonempty limitations")
        object.__setattr__(self, "decisions", decisions)
        object.__setattr__(self, "limitations", limitations)

    @property
    def ordering(self) -> tuple[str, ...]:
        return tuple(decision.request.order_id for decision in self.decisions)

    @property
    def is_empty(self) -> bool:
        return not self.decisions

    @property
    def total_requested_quantity(self) -> int:
        return sum((decision.requested_quantity for decision in self.decisions), 0)

    def decision_for(self, order_id: str) -> TrimDecision:
        wanted = identifier(order_id)
        for decision in self.decisions:
            if decision.request.order_id == wanted:
                return decision
        raise KeyError(wanted)

    def decision_for_position(self, position_id: str) -> TrimDecision:
        wanted = identifier(position_id)
        for decision in self.decisions:
            if decision.request.position_id == wanted:
                return decision
        raise KeyError(wanted)


def plan_trims(book: BookState, on: date, requests, *,
               bars, capacities: Mapping, portfolio: PortfolioSpec) -> TrimPlan:
    """Pure, causal rupee-target-to-share-reduction composition for one session.

    ``book`` must be a ``BookState`` snapshot whose ``as_of`` session equals
    ``on``. ``bars`` supplies the raw same-session observations; ``capacities``
    supplies the caller-known nonnegative rupee cap per security key. A missing
    or ``None`` capacity is an error only when a positive reduction is proposed.

    The function never mutates ``book`` or any input. It validates every requested
    position before returning a plan, so a missing close, missing capacity,
    unknown/inactive/blocked position or configuration error rejects the whole
    call atomically.

    A zero ``target_holding_value`` is an explicit full reduction, not a missing
    value. A zero requested quantity is an intentional no-op with reason
    ``at_or_below_target`` or ``no_delivered_shares``. A zero supplied capacity
    for a positive reduction is a known no-executable-liquidity cap, reported as
    ``known_zero_capacity`` without shrinking the requested quantity.
    """
    if not isinstance(book, BookState):
        raise ValueError("plan_trims requires a BookState snapshot")
    if type(on) is not date:
        raise ValueError("plan_trims requires an explicit session date")
    if not isinstance(portfolio, PortfolioSpec):
        raise ValueError("plan_trims requires an explicit PortfolioSpec")
    if not isinstance(book.as_of, EventTime) or book.as_of.session != on:
        raise ValueError(
            "The book snapshot session does not match the requested trim session")

    request_tuple = _request_tuple(requests)
    if not request_tuple:
        return TrimPlan(on, ())
    if not portfolio.trim_overweight_targets:
        raise ValueError(
            f"{DISABLED_TRIMS_REASON}: explicit trim requests were supplied but "
            "PortfolioSpec.trim_overweight_targets is False")

    if not isinstance(capacities, Mapping):
        raise ValueError("capacities must be a mapping from security key to capacity")
    bars_by_key = _bar_map(bars, on)
    positions = _position_map(book)

    candidates: list[TrimDecision] = []
    for request in request_tuple:
        position = positions.get(request.position_id)
        if position is None:
            raise ValueError(
                f"Trim request {request.order_id!r} names an unknown position "
                f"{request.position_id!r}")
        if position.shares <= 0:
            raise ValueError(
                f"Trim request {request.order_id!r} requires an existing active position; "
                f"{request.position_id!r} has no shares")
        if position.blocked_reason:
            raise ValueError(
                f"Trim request {request.order_id!r} targets a blocked/unresolved position "
                f"{request.position_id!r}: {position.blocked_reason}")
        bar = bars_by_key.get(position.key)
        if bar is None:
            raise ValueError(
                f"A raw same-session close is required for trim request "
                f"{request.order_id!r} security {position.key!r}; no bar was supplied")
        if not bar.valid_ohlcv or bar.close is None or bar.close <= 0:
            raise ValueError(
                f"A valid raw same-session close is required for trim request "
                f"{request.order_id!r} security {position.key!r}")
        close = Decimal(bar.close)
        target_shares = floor(
            Fraction(request.target_holding_value) / Fraction(close))
        excess = position.shares - target_shares
        if excess <= 0:
            requested_quantity = 0
            reason = AT_OR_BELOW_TARGET_REASON
        elif position.delivered_shares <= 0:
            requested_quantity = 0
            reason = NO_DELIVERED_SHARES_REASON
        else:
            requested_quantity = min(position.delivered_shares, excess)
            reason = PROPOSED_TRIM_REASON
        capacity = _known_capacity(
            capacities, position.key, required=requested_quantity > 0)
        if requested_quantity > 0 and capacity == 0:
            # Known zero is distinct from unknown: keep the proposed quantity and
            # freeze a zero cap. The runner must not fill it.
            reason = KNOWN_ZERO_CAPACITY_REASON
        candidates.append(TrimDecision(
            request=request,
            key=position.key,
            decision_close=close,
            target_shares=target_shares,
            requested_quantity=requested_quantity,
            capacity_notional=capacity,
            reason=reason,
        ))

    ordered = tuple(sorted(
        candidates, key=lambda d: (d.request.rank, d.key, d.request.order_id)))
    return TrimPlan(on, ordered)
