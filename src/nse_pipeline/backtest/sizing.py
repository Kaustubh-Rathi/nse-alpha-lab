"""Rank-sequential fee-inclusive sizing under the canonical P1/P2/P3 policies.

Long names (``P1_freeze_on_unresolved``, ``P2_size_excluding_unresolved``,
``P3_size_including_unresolved_at_screen``) are canonical and are the only
values ever recorded in ``SizingResult.policy`` and serialized in fixtures.
Short ``P1``/``P2``/``P3`` aliases are accepted only by ``size_orders`` and are
normalized exactly once; ``size_orders`` rejects a ``PortfolioSpec`` and never
performs cross-spec mapping. ``size_portfolio_orders`` is the only
``PortfolioSpec`` entry point (D-13, D-15).
"""
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from math import floor

from nse_pipeline.specs import (ALIASES, CANONICAL_POLICIES, CostSpec, ExecutionSpec,
                                PortfolioSpec, resolve_sizing_policy)
from nse_pipeline.specs.validation import validate_run_specs
from .accounting_types import ZERO, decimal, identifier, integer, money
from .fees import affordable_buy, opening_quote

POLICIES = CANONICAL_POLICIES


@dataclass(frozen=True)
class SizingResult:
    policy: str
    filled: tuple[Decimal, ...]
    remaining_cash: Decimal
    reasons: tuple[str, ...]
    fees: Decimal = ZERO

    def __post_init__(self):
        identifier(self.policy)
        if self.policy not in POLICIES:
            raise ValueError(
                f"Unknown sizing policy; expected a canonical long name in {POLICIES}")
        filled = tuple(_amount(value) for value in self.filled)
        reasons = tuple(self.reasons)
        if len(filled) != len(reasons):
            raise ValueError("Every rank needs exactly one reason")
        object.__setattr__(self, "filled", filled)
        object.__setattr__(self, "remaining_cash", _amount(self.remaining_cash))
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "fees", _amount(self.fees))


def size_orders(policy, cash, desired_in_rank_order, *, price, slots, costs, execution,
                unresolved_ranks=(), screen_values=None, screen_prices=None):
    """Allocate settled cash to rank-ordered desired notionals under one policy.

    ``policy`` accepts a canonical long name or a short ``P1``/``P2``/``P3``
    alias, normalized once here. P1 freezes the whole sizing round while any
    rank's valuation is unresolved. P2 excludes unresolved ranks and sizes the
    remaining ranks. P3 includes unresolved ranks sized at their caller-supplied
    screen value and screen price. Unresolved ranks are named by their
    zero-based position in the rank order.

    ``screen_values`` and ``screen_prices`` are sequences aligned one-to-one
    with ``unresolved_ranks``. They are never inferred from an ordinary or stale
    mark. A missing value or price yields ``screen_value_unavailable`` and
    consumes no cash or slot. Every filled P3 screen rank uses reason
    ``filled_screen_value``. Every policy spends settled cash only: a
    screen-valued sizing base never creates cash. A rank spends at most the
    smaller of its desired notional and the remaining cash, fees included, and
    its desired notional bounds the principal. Slots bound the number of ranks
    that may receive a fill.
    """
    policy = _canonical_policy(policy)
    if not isinstance(costs, CostSpec) or not isinstance(execution, ExecutionSpec):
        raise ValueError("Sizing needs an explicit CostSpec and ExecutionSpec")
    available = _amount(cash)
    desired = tuple(_amount(value) for value in desired_in_rank_order)
    prices = _prices(price, len(desired))
    slot_count = integer(slots, minimum=0)
    unresolved = _unresolved_ranks(unresolved_ranks, len(desired))
    screen = _screen_inputs(unresolved, screen_values, screen_prices)
    remaining, filled, reasons, fees, opened = available, [], [], ZERO, 0
    for rank, want in enumerate(desired):
        if policy == "P1_freeze_on_unresolved" and unresolved:
            filled.append(ZERO)
            reasons.append("frozen_unresolved")
            continue
        if policy == "P2_size_excluding_unresolved" and rank in unresolved:
            filled.append(ZERO)
            reasons.append("excluded_unresolved")
            continue
        screen_rank = policy == "P3_size_including_unresolved_at_screen" and rank in unresolved
        if screen_rank:
            evidence = screen[rank]
            if evidence is None:
                filled.append(ZERO)
                reasons.append("screen_value_unavailable")
                continue
            want, rank_price = evidence
        else:
            rank_price = prices[rank]
        if want == 0:
            filled.append(ZERO)
            reasons.append("no_request")
            continue
        if opened >= slot_count:
            filled.append(ZERO)
            reasons.append("no_slot")
            continue
        if remaining <= 0:
            filled.append(ZERO)
            reasons.append("no_cash")
            continue
        quote = opening_quote("buy", 1, rank_price, costs, execution)
        quantity = floor(Fraction(want) / Fraction(quote.fill_price))
        if quantity < 1:
            filled.append(ZERO)
            reasons.append("below_one_share")
            continue
        fill = affordable_buy(quantity, rank_price, min(want, remaining), want, costs,
                              execution)
        if not fill.quantity:
            filled.append(ZERO)
            reasons.append(fill.reason)
            continue
        filled.append(fill.notional)
        reasons.append("filled_screen_value" if screen_rank else "filled")
        fees += fill.fees.total
        remaining -= fill.notional + fill.fees.total
        opened += 1
    return SizingResult(policy, tuple(filled), remaining, tuple(reasons), fees)


def size_portfolio_orders(portfolio, valuation, cash, desired_in_rank_order, *, price,
                          costs, execution, universe=None, momentum=None,
                          unresolved_ranks=(), screen_values=None, screen_prices=None):
    """The only ``PortfolioSpec`` entry point for sizing.

    Cross-spec validation runs first (``validate_run_specs``); the canonical
    policy is then resolved once through ``resolve_sizing_policy`` and
    ``PortfolioSpec.max_positions`` supplies the slot count. A ``PortfolioSpec``
    never reaches ``size_orders`` directly.
    """
    validate_run_specs(portfolio, valuation, execution, universe, momentum, costs)
    policy = resolve_sizing_policy(portfolio, valuation)
    return size_orders(policy, cash, desired_in_rank_order, price=price,
                       slots=portfolio.max_positions, costs=costs, execution=execution,
                       unresolved_ranks=unresolved_ranks, screen_values=screen_values,
                       screen_prices=screen_prices)


def _canonical_policy(policy):
    if isinstance(policy, PortfolioSpec):
        raise ValueError(
            "size_orders does not accept a PortfolioSpec; use size_portfolio_orders")
    identifier(policy)
    if policy in ALIASES:
        policy = ALIASES[policy]
    if policy not in CANONICAL_POLICIES:
        raise ValueError(
            f"unknown_sizing_policy: {policy!r}; expected one of {CANONICAL_POLICIES} "
            f"or aliases {tuple(ALIASES)}")
    return policy


def _amount(value, *, positive=False):
    try:
        return money(value, positive=positive)
    except TypeError as error:
        raise ValueError("Amounts must be exact decimals or integer paise, not floats or bools") from error


def _price(value):
    try:
        return decimal(value, positive=True)
    except TypeError as error:
        raise ValueError("Prices must be exact decimals, not floats or bools") from error


def _prices(price, count):
    values = price if isinstance(price, (list, tuple)) else (price,) * count
    prices = tuple(_price(value) for value in values)
    if len(prices) != count:
        raise ValueError("Every rank needs exactly one price")
    return prices


def _unresolved_ranks(unresolved_ranks, count):
    try:
        ranks = tuple(unresolved_ranks)
    except TypeError as error:
        raise ValueError("Unresolved ranks must be a sequence of rank positions") from error
    seen = set()
    for rank in ranks:
        integer(rank, minimum=0)
        if rank >= count:
            raise ValueError("An unresolved rank must exist in the rank order")
        if rank in seen:
            raise ValueError("An unresolved rank must appear at most once")
        seen.add(rank)
    return ranks


def _screen_sequence(name, value, count):
    if isinstance(value, (str, bytes, dict)):
        raise ValueError(f"{name} must be a sequence aligned one-to-one with unresolved_ranks")
    try:
        values = tuple(value)
    except TypeError as error:
        raise ValueError(
            f"{name} must be a sequence aligned one-to-one with unresolved_ranks") from error
    if len(values) != count:
        raise ValueError(f"{name} must contain exactly one entry per unresolved rank")
    return values


def _screen_inputs(unresolved_ranks, screen_values, screen_prices):
    count = len(unresolved_ranks)
    values = ((None,) * count if screen_values is None
              else _screen_sequence("screen_values", screen_values, count))
    prices = ((None,) * count if screen_prices is None
              else _screen_sequence("screen_prices", screen_prices, count))
    screen = {}
    for rank, value, on_price in zip(unresolved_ranks, values, prices):
        screen[rank] = (None if value is None or on_price is None
                        else (_amount(value), _price(on_price)))
    return screen
