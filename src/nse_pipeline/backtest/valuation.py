"""Defensible NAV and explicitly non-valuation reference diagnostics.

This pure view consumes a frozen accounting snapshot, never signals or orders.
Reference marks must already be expressed in the snapshot's raw-share units.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction

from .accounting_types import BookState, EventTime, Phase, ZERO, decimal, identifier, money


REFERENCE_COVERAGE_REASONS = (
    "no_positions",
    "missing_reference",
    "zero_gross_notional",
    "complete_current_date",
    "dated_reference_only",
    "mixed_current_and_dated_reference",
)
NAV_REASONS = (
    "nav_available",
    "unpriced_positions",
    "unknown_assets",
    "unpriced_positions_and_unknown_assets",
)


@dataclass(frozen=True)
class Mark:
    position_id: str
    price: Decimal
    on: date
    available_at: EventTime
    source: str = "close"

    def __post_init__(self):
        identifier(self.position_id)
        object.__setattr__(self, "price", decimal(self.price, positive=True))
        if type(self.on) is not date or not isinstance(self.available_at, EventTime):
            raise ValueError("A mark needs its observation and availability date/phase")
        if self.source not in ("close", "reference") or self.available_at.session < self.on:
            raise ValueError("Unknown mark source or impossible availability date")
        if self.source == "close" and self.available_at.session == self.on and self.available_at.phase < Phase.CLOSE_VALUATION:
            raise ValueError("The close is unavailable before the close phase")


@dataclass(frozen=True)
class ScenarioValue:
    name: str
    amount: Decimal | None
    assumption: str


@dataclass(frozen=True)
class Valuation:
    nav: Decimal | None
    cash: Decimal
    cash_available: Decimal | None
    receivables: Decimal | None
    liabilities: Decimal | None
    marked_value_priced: Decimal
    unresolved_positions: tuple[str, ...]
    unresolved_assets: tuple[str, ...]
    position_completeness: Fraction
    unresolved_reference_notional: Decimal | None
    known_reference_subtotal: Decimal | None
    missing_reference_count: int
    gross_reference_notional: Decimal | None
    reference_notional_coverage: Fraction | None
    reference_coverage_reason: str
    liquidation_known_zero_friction: Decimal | None
    nav_to_cash_liquidation_difference: Decimal | None
    scenarios: tuple[ScenarioValue, ...]
    liquidation_assumption: str = "Hypothetical zero-friction disposal of priced holdings; unsettled receivables excluded, unknown recovery not valued."
    nav_reason: str = "unknown_assets"
    nav_reported: Decimal | None = None
    unresolved_settlement_claims: tuple[str, ...] = ()


def _index(records):
    records = tuple(records)
    indexed = {m.position_id: m for m in records}
    if len(indexed) != len(records):
        raise ValueError("Ambiguous multiple marks for a position")
    return indexed


def value(state: BookState, at: EventTime, *, marks=(), references=()) -> Valuation:
    if not isinstance(at, EventTime):
        raise ValueError("Valuation requires an explicit as-of phase")
    if state.as_of is None or at.session != state.as_of.session or at < state.as_of:
        raise ValueError("Valuation requires a current-session snapshot no later than the requested phase")
    marks, references = _index(marks), _index(references)
    holdings = [p for p in state.positions if p.shares > 0]
    marked, known_reference = ZERO, ZERO
    unresolved, missing_reference = [], 0
    for position in holdings:
        mark = marks.get(position.position_id)
        priced = (not position.blocked_reason and mark is not None and mark.source == "close"
                  and mark.on == at.session and mark.available_at <= at)
        if priced:
            marked += position.shares * mark.price
        else:
            unresolved.append(position.position_id)
            ref = references.get(position.position_id)
            if ref is None or ref.source != "reference" or ref.on > at.session or ref.available_at > at:
                missing_reference += 1
            else:
                known_reference += position.shares * ref.price
    unknown_assets = tuple(sorted([c.claim_id for c in state.claims if c.amount is None] +
                                 [f"unaccrued:{e.dividend_id}" for e in state.entitlements if not e.accrued]))
    balances = state.balances
    reference = None if missing_reference else known_reference
    gross = None if reference is None else marked + reference
    coverage = None if gross is None or gross == 0 else Fraction(marked) / Fraction(gross)
    if not holdings:
        reason = "no_positions"
    elif gross is None:
        reason = "missing_reference"
    elif gross == 0:
        reason = "zero_gross_notional"
    elif reference == 0:
        reason = "complete_current_date"
    elif marked == 0:
        reason = "dated_reference_only"
    else:
        reason = "mixed_current_and_dated_reference"
    known_wealth = (None if balances.receivables is None or balances.liabilities is None else
                    balances.cash + balances.receivables + marked - balances.liabilities)
    nav = known_wealth if not unresolved and not unknown_assets else None
    if nav is not None:
        nav_reason = "nav_available"
    elif unresolved and unknown_assets:
        nav_reason = "unpriced_positions_and_unknown_assets"
    elif unresolved:
        nav_reason = "unpriced_positions"
    else:
        nav_reason = "unknown_assets"
    nav_reported = None if nav is None else money(nav)
    unresolved_settlement_claims = tuple(sorted(
        claim.claim_id for claim in state.claims if claim.amount is not None and claim.due is None))
    liquidation = None if balances.liabilities is None else balances.cash + marked - balances.liabilities
    scenario_base = None if unknown_assets else known_wealth
    scenarios = (ScenarioValue("zero_recovery", scenario_base, "Unpriced holdings assumed to recover zero; unknown financial amounts remain unknown."),
                 ScenarioValue("last_reference", None if scenario_base is None or reference is None else scenario_base + reference,
                               "Unpriced holdings assumed to realize their dated, current-share-unit reference notionals."))
    known_reference_subtotal = (None if missing_reference and known_reference == ZERO
                                else known_reference)
    # A missing reference makes the aggregate unknown. A nonzero partial sum is
    # preserved; when no unresolved holding contributed one the subtotal is None.
    return Valuation(nav, balances.cash, balances.cash_available, balances.receivables, balances.liabilities,
        marked, tuple(sorted(unresolved)), unknown_assets,
        Fraction(len(holdings) - len(unresolved), len(holdings)) if holdings else Fraction(1),
        reference, known_reference_subtotal, missing_reference, gross, coverage, reason, liquidation,
        None if nav is None or liquidation is None else nav - liquidation, scenarios,
        nav_reason=nav_reason, nav_reported=nav_reported,
        unresolved_settlement_claims=unresolved_settlement_claims)
