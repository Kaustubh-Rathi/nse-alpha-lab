"""Whole-share remainders stay in cash; rounding never creates outside capital."""
from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from nse_pipeline.backtest.accounting_types import EventKind, Position
from nse_pipeline.backtest.observed_session import SessionInput, SessionRunner
from tests.test_stage1_production_runner_contracts import (
    D0, D1, D2, ZERO_COSTS, bar, buy, execution, opening, sell, source)


@pytest.mark.parametrize("brokerage_bps,expected_reserve,expected_cash", [
    (0, D("990.00"), D("10.00")),
    (100, D("999.90"), D("0.10")),
])
def test_1000_budget_at_30_preserves_remainder_through_reserve_fill_and_settlement(
        brokerage_bps, expected_reserve, expected_cash):
    costs = replace(ZERO_COSTS, brokerage_bps=brokerage_bps, brokerage_cap_per_order=2000)
    runner = SessionRunner(
        "1000", on=D0, initial_provenance=source(D0),
        costs=costs, execution=execution())
    decision = runner.step(SessionInput(
        D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
        buy_targets=(buy("rounding-buy", "AAA", 0, quantity=33, budget="1000"),)))
    assert decision.next_orders[0].quantity == 33
    assert decision.next_orders[0].reserved == expected_reserve
    assert runner.snapshot.cash == D("1000")
    assert runner.snapshot.balances.cash_available == expected_cash
    filled = runner.step(SessionInput(
        D1, source(D1), opening_prints=(opening("AAA", D1, "30"),),
        close_bars=(bar("AAA", D1, "30"),)))
    assert runner.snapshot.cash == D("1000")  # Earmarked until purchase settlement.
    assert runner.snapshot.balances.liabilities == expected_reserve
    assert runner.snapshot.balances.cash_available == expected_cash
    settled = runner.step(SessionInput(
        D2, source(D2), close_bars=(bar("AAA", D2, "30"),)))
    assert runner.snapshot.cash == expected_cash
    assert runner.snapshot.balances.cash_available == expected_cash
    assert runner.snapshot.claims == ()
    assert runner.snapshot.positions[0].shares == 33
    assert all(result.reconciliation.passed for result in (decision, filled, settled))
    assert not any(event.kind == EventKind.EXTERNAL_FLOW for event in runner.events)


def test_1000_holding_target_at_30_credits_extra_rounding_only_when_sale_settles():
    runner = SessionRunner(
        "0", on=D0, initial_provenance=source(D0),
        opening_positions=(Position("held", "AAA", 100, date(2024, 5, 31), 100),),
        costs=ZERO_COSTS, execution=execution())
    # Target floor(1000/30)=33; the explicit reduction is 100-33=67 shares.
    runner.step(SessionInput(
        D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
        sell_targets=(sell("rounding-sell", "held", 67, 0),)))
    filled = runner.step(SessionInput(
        D1, source(D1), opening_prints=(opening("AAA", D1, "30"),),
        close_bars=(bar("AAA", D1, "30"),)))
    assert runner.snapshot.positions[0].shares == 33
    assert filled.valuation.marked_value_priced == D("990")
    assert runner.snapshot.balances.sale_receivable == D("2010")
    assert runner.snapshot.cash == D("0")
    assert runner.snapshot.balances.cash_available == D("0")
    settled = runner.step(SessionInput(
        D2, source(D2), close_bars=(bar("AAA", D2, "30"),)))
    assert runner.snapshot.cash == D("2010")
    assert runner.snapshot.balances.cash_available == D("2010")
    assert runner.snapshot.claims == ()
    assert settled.valuation.nav == D("3000")
    assert filled.reconciliation.passed and settled.reconciliation.passed
    assert not any(event.kind == EventKind.EXTERNAL_FLOW for event in runner.events)
