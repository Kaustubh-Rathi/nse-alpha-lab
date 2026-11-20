"""Batch 5 liquidation: per-order sell costs, adverse slippage, capacity and explicit unknowns."""
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden/liquidation_cases.json"
RECORDS = json.loads(GOLDEN.read_text(encoding="utf-8"))
CASES = {case["id"]: case for case in RECORDS["cases"]}


def event(payload):
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    return EventTime(date.fromisoformat(payload["session"]), Phase(payload["phase"]),
                     payload.get("sequence", 0))


def schedule():
    from nse_pipeline.backtest.liquidation import LiquidationCostSchedule
    from nse_pipeline.specs import CostSpec
    payload = RECORDS["schedule"]
    return LiquidationCostSchedule(
        schedule_id=payload["schedule_id"],
        effective_from=date.fromisoformat(payload["effective_from"]),
        effective_to=(None if payload["effective_to"] is None
                      else date.fromisoformat(payload["effective_to"])),
        available_at=event(payload["available_at"]),
        citation=payload["citation"],
        costs=CostSpec(**payload["costs"]),
        adverse_slippage_bps=Decimal(payload["adverse_slippage_bps"]))


def inputs(case):
    from nse_pipeline.backtest.liquidation import (
        LiquidationCapacity, LiquidationPrice, SaleOrder)
    orders = tuple(SaleOrder(row["order_id"], row["security_key"], row["quantity"])
                   for row in case["orders"])
    prices = tuple(LiquidationPrice(
        row["security_key"],
        None if row.get("price") is None else Decimal(row["price"]),
        date.fromisoformat(row["on"]), event(row["available_at"]), row["citation"])
        for row in case["prices"])
    capacities = tuple(LiquidationCapacity(
        row["security_key"], date.fromisoformat(row["as_of"]), event(row["available_at"]),
        row["capacity_shares"], row["citation"]) for row in case["capacities"])
    return orders, prices, capacities


def run(case):
    from nse_pipeline.backtest.liquidation import estimate_liquidation
    orders, prices, capacities = inputs(case)
    return estimate_liquidation(orders, prices=prices, capacities=capacities,
                                schedules=(schedule(),), on=date(2024, 6, 4),
                                decision_at=event(case["decision_at"]))


def decimal_or_none(value):
    return None if value is None else Decimal(value)


@pytest.mark.parametrize("case_id", tuple(CASES))
def test_golden_liquidation_cases_match_hand_derived_values(case_id):
    case = CASES[case_id]
    estimate = run(case)
    expected = case["expected"]
    assert estimate.status == expected["status"]
    assert estimate.gross_proceeds == decimal_or_none(expected["gross_proceeds"])
    assert estimate.fees == decimal_or_none(expected["fees"])
    assert estimate.net_proceeds == decimal_or_none(expected["net_proceeds"])
    assert estimate.zero_friction_subtotal == decimal_or_none(expected["zero_friction_subtotal"])
    assert list(estimate.unknown_reasons) == expected["unknown_reasons"]
    assert estimate.hypothetical is True
    assert estimate.authoritative_nav is None
    assert estimate.claims_created == 0
    assert len(estimate.lines) == len(expected["lines"])
    for line, target in zip(estimate.lines, expected["lines"]):
        assert line.order_id == target["order_id"]
        assert line.requested_quantity == target["requested_quantity"]
        assert line.filled_quantity == target["filled_quantity"]
        assert line.reference_price == decimal_or_none(target["reference_price"])
        assert line.slipped_price == decimal_or_none(target["slipped_price"])
        assert line.gross == decimal_or_none(target["gross"])
        assert line.fees == decimal_or_none(target["fees"])
        assert line.net == decimal_or_none(target["net"])
        assert line.status == target["status"]


def test_dated_cost_schedule_must_be_available_and_cover_the_session():
    from nse_pipeline.backtest.liquidation import select_cost_schedule
    from nse_pipeline.backtest.accounting_types import EventTime, Phase

    at = EventTime(date(2024, 6, 4), Phase.CLOSE_VALUATION)
    with pytest.raises(ValueError, match="cost_schedule_unavailable"):
        select_cost_schedule((schedule(),), on=date(2023, 12, 31), decision_at=at)
    with pytest.raises(ValueError, match="cost_schedule_unavailable"):
        select_cost_schedule((schedule(),), on=date(2024, 6, 4),
                             decision_at=EventTime(date(2023, 12, 31), Phase.CLOSE_VALUATION))
    assert select_cost_schedule((schedule(),), on=date(2024, 6, 4),
                                decision_at=at).schedule_id == "synthetic-sell-costs-2024"


def test_fees_are_per_order_and_never_deducted_twice():
    case = CASES["two_orders_pay_fees_once_each"]
    estimate = run(case)
    assert estimate.claims_created == 0
    assert estimate.fees == sum((line.fees for line in estimate.lines), Decimal("0.00"))
    for line in estimate.lines:
        assert line.net == line.gross - line.fees
    assert estimate.net_proceeds == estimate.gross_proceeds - estimate.fees == Decimal("1978.02")


def test_unknown_price_or_capacity_never_becomes_zero_or_fills():
    from nse_pipeline.backtest.liquidation import (
        LiquidationCapacity, LiquidationPrice, SaleOrder, estimate_liquidation)
    at = event({"session": "2024-06-04", "phase": 60})
    order = (SaleOrder("o", "AAA", 10),)
    priced = (LiquidationPrice("AAA", Decimal("100"), date(2024, 6, 4), at, "synthetic close"),)
    unknown_capacity = (LiquidationCapacity("AAA", date(2024, 6, 4), at, None, "synthetic"),)
    no_capacity = ()
    for capacities in (unknown_capacity, no_capacity):
        estimate = estimate_liquidation(order, prices=priced, capacities=capacities,
                                        schedules=(schedule(),), on=date(2024, 6, 4), decision_at=at)
        assert estimate.net_proceeds is None and estimate.status == "unknown"
        assert estimate.lines[0].filled_quantity == 0
        assert estimate.lines[0].status == "capacity_unknown"
    future_capacity = (LiquidationCapacity(
        "AAA", date(2024, 6, 4), event({"session": "2024-06-05", "phase": 10}), 10, "future"),)
    estimate = estimate_liquidation(order, prices=priced, capacities=future_capacity,
                                    schedules=(schedule(),), on=date(2024, 6, 4), decision_at=at)
    assert estimate.net_proceeds is None
    unpriced = (LiquidationPrice("AAA", None, date(2024, 6, 4), at, "no price"),)
    estimate = estimate_liquidation(order, prices=unpriced, capacities=unknown_capacity,
                                    schedules=(schedule(),), on=date(2024, 6, 4), decision_at=at)
    assert estimate.net_proceeds is None and estimate.lines[0].status == "unpriced"


def test_liquidation_is_adverse_and_not_authoritative_nav():
    case = CASES["capacity_limits_quantity"]
    estimate = run(case)
    line = estimate.lines[0]
    assert line.slipped_price < line.reference_price
    assert line.gross < estimate.zero_friction_subtotal
    with pytest.raises(ValueError, match="not_authoritative"):
        estimate.require_authoritative()
    with pytest.raises(ValueError, match="not_authoritative"):
        estimate.authoritative_statistics()


def test_liquidation_does_not_mutate_an_accounting_reference():
    from nse_pipeline.backtest.accounting_types import Position
    from nse_pipeline.backtest.core import Portfolio

    book = Portfolio("500", on=date(2024, 6, 4),
                     positions=(Position("p", "AAA", 10, date(2024, 5, 31)),))
    before = book.snapshot
    run(CASES["capacity_limits_quantity"])
    assert book.snapshot == before
    assert book.positions["p"].shares == 10 and book.balances.cash == 500


def test_cost_schedule_requires_explicit_nonnegative_adverse_slippage():
    from nse_pipeline.backtest.liquidation import LiquidationCostSchedule
    from nse_pipeline.specs import CostSpec
    payload = RECORDS["schedule"]
    base = dict(
        schedule_id="s", effective_from=date(2024, 1, 1), effective_to=None,
        available_at=event({"session": "2024-01-01", "phase": 10}),
        citation="synthetic", costs=CostSpec(**payload["costs"]),
        adverse_slippage_bps=Decimal("100"))
    with pytest.raises(ValueError):
        LiquidationCostSchedule(**{**base, "adverse_slippage_bps": Decimal("-1")})
    with pytest.raises(ValueError):
        LiquidationCostSchedule(**{**base, "adverse_slippage_bps": Decimal("10000")})
    with pytest.raises(ValueError):
        LiquidationCostSchedule(**{**base, "citation": ""})
    with pytest.raises(ValueError):
        LiquidationCostSchedule(**{**base, "effective_to": date(2023, 12, 31)})
