"""Cross-specification validation and the portfolio rebalance-schedule adapter.

``validate_run_specs`` is the mandatory integration-time gate (D-15). It is not
wired to any legacy runner: the only production caller is
``backtest.sizing.size_portfolio_orders``, which must validate before it sizes a
single order.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import (
    CostSpec,
    ExecutionSpec,
    MomentumSpec,
    PortfolioSpec,
    UniverseSpec,
    ValuationPolicy,
    validate_policy_combination,
)

REBALANCE_FREQUENCY = "monthly"
REBALANCE_ANCHOR = "known_calendar_else_observed_month_change"

# Fields whose contract admits exactly one legal value (D-16 IMPLEMENTED
# (implicit)). Their constructors already reject a second value; the run gate
# re-asserts them explicitly so each field has a production reader.
SETTLEMENT_FUNDING = "settled_cash_only"
ALLOCATION_ORDER = "rank_then_key"
DIVIDEND_CASH_PHASE = "post_open_settlement"
SCHEDULE_BASIS = "flat_research_assumption"
MONETARY_ROUNDING = "paise_half_up_per_component"


@dataclass(frozen=True)
class RebalanceScheduleDescriptor:
    """The frozen spec-to-schedule result for ``PortfolioSpec``.

    Only monthly rebalancing anchored on a known calendar, else the observed
    month change, is admissible. The descriptor carries no calendar authority of
    its own: a published-exchange calendar is still required before a month-end
    may fire (D-11).
    """

    frequency: str
    anchor: str

    def __post_init__(self):
        if (self.frequency != REBALANCE_FREQUENCY
                or self.anchor != REBALANCE_ANCHOR):
            raise ValueError(
                "unsupported_rebalance_schedule: only monthly + "
                "known_calendar_else_observed_month_change is admissible, not "
                f"{self.frequency!r}/{self.anchor!r}")


def resolve_rebalance_schedule(portfolio: PortfolioSpec) -> RebalanceScheduleDescriptor:
    """Consume ``PortfolioSpec.rebalance_frequency`` and ``rebalance_anchor`` once."""
    if not isinstance(portfolio, PortfolioSpec):
        raise ValueError("resolve_rebalance_schedule requires a PortfolioSpec")
    frequency = portfolio.rebalance_frequency
    anchor = portfolio.rebalance_anchor
    if frequency != REBALANCE_FREQUENCY or anchor != REBALANCE_ANCHOR:
        raise ValueError(
            "unsupported_rebalance_schedule: only monthly + "
            "known_calendar_else_observed_month_change is admissible, not "
            f"{frequency!r}/{anchor!r}")
    return RebalanceScheduleDescriptor(frequency=frequency, anchor=anchor)


def validate_run_specs(portfolio, valuation, execution, universe=None, momentum=None,
                       costs=None) -> None:
    """Validate every cross-spec rule before any order is sized.

    The documented five-argument call is canonical. The optional ``costs``
    argument is additive: when omitted, the pinned ``CostSpec`` contract values
    are asserted against the class defaults, and ``size_portfolio_orders`` passes
    the caller's actual cost model.
    """
    if not isinstance(portfolio, PortfolioSpec):
        raise ValueError("validate_run_specs requires a PortfolioSpec")
    if not isinstance(valuation, ValuationPolicy):
        raise ValueError("validate_run_specs requires a ValuationPolicy")
    if not isinstance(execution, ExecutionSpec):
        raise ValueError("validate_run_specs requires an ExecutionSpec")
    if universe is not None and not isinstance(universe, UniverseSpec):
        raise ValueError("validate_run_specs requires a UniverseSpec or None")
    if momentum is not None and not isinstance(momentum, MomentumSpec):
        raise ValueError("validate_run_specs requires a MomentumSpec or None")
    if costs is not None and not isinstance(costs, CostSpec):
        raise ValueError("validate_run_specs requires a CostSpec or None")
    validate_policy_combination(portfolio, valuation)
    resolve_rebalance_schedule(portfolio)
    if execution.settlement_funding != SETTLEMENT_FUNDING:
        raise ValueError(
            f"settlement_funding must be {SETTLEMENT_FUNDING!r}, "
            f"not {execution.settlement_funding!r}")
    if portfolio.allocation_order != ALLOCATION_ORDER:
        raise ValueError(
            f"allocation_order must be {ALLOCATION_ORDER!r}, "
            f"not {portfolio.allocation_order!r}")
    if valuation.dividend_cash_phase != DIVIDEND_CASH_PHASE:
        raise ValueError(
            f"dividend_cash_phase must be {DIVIDEND_CASH_PHASE!r}, "
            f"not {valuation.dividend_cash_phase!r}")
    cost_source = costs if costs is not None else CostSpec()
    if cost_source.schedule_basis != SCHEDULE_BASIS:
        raise ValueError(
            f"schedule_basis must be {SCHEDULE_BASIS!r}, not {cost_source.schedule_basis!r}")
    if cost_source.monetary_rounding != MONETARY_ROUNDING:
        raise ValueError(
            "monetary_rounding must be "
            f"{MONETARY_ROUNDING!r}, not {cost_source.monetary_rounding!r}")
