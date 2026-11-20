"""Batch 5 scenarios: an immutable reference, hypothetical values, no authoritative statistics."""
from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden/scenario_cases.json"
RECORDS = json.loads(GOLDEN.read_text(encoding="utf-8"))
CASES = {case["id"]: case for case in RECORDS["cases"]}


def event(payload):
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    return EventTime(date.fromisoformat(payload["session"]), Phase(payload["phase"]),
                     payload.get("sequence", 0))


def reference():
    from nse_pipeline.backtest.scenarios import ReferenceSnapshot
    payload = RECORDS["reference"]
    return ReferenceSnapshot(
        cash=Decimal(payload["cash"]),
        holdings=tuple((key, int(shares)) for key, shares in payload["holdings"]),
        claims=tuple(tuple(claim) for claim in payload["claims"]))


def marks(case):
    from nse_pipeline.backtest.scenarios import ScenarioMark
    return tuple(ScenarioMark(row["security_key"], Decimal(row["price"]),
                              date.fromisoformat(row["on"]), event(row["available_at"]))
                 for row in case["marks"])


def holdings_map(portfolio):
    return {holding.security_key: holding.shares for holding in portfolio.holdings}


def build(case):
    from nse_pipeline.backtest.scenarios import ScenarioPortfolio
    portfolio = ScenarioPortfolio.from_reference(reference())
    for operation in case["operations"]:
        fee = Decimal(operation["fee"])
        price = Decimal(operation["price"])
        if operation["kind"] == "buy":
            portfolio = portfolio.buy(operation["key"], operation["quantity"], price, fee=fee)
        elif operation["kind"] == "sell":
            portfolio = portfolio.sell(operation["key"], operation["quantity"], price, fee=fee)
        else:
            raise AssertionError(operation["kind"])
    return portfolio


@pytest.mark.parametrize("case_id", tuple(CASES))
def test_golden_scenario_cases_match_hand_derived_values_and_leave_reference_intact(case_id):
    case = CASES[case_id]
    snapshot = reference()
    before = snapshot.fingerprint()
    portfolio = build(case)
    expected = case["expected"]
    assert portfolio.cash == Decimal(expected["cash"])
    assert holdings_map(portfolio) == expected["holdings"]
    liquidation = (None if case["liquidation_value"] is None
                   else Decimal(case["liquidation_value"]))
    result = portfolio.nav(marks(case), at=event(case["at"]), liquidation_value=liquidation)
    assert result.marked_value == Decimal(expected["marked_value"])
    assert result.nav == (None if expected["nav"] is None else Decimal(expected["nav"]))
    assert result.reason == expected["reason"]
    assert result.hypothetical is True
    assert result.authoritative is False
    assert result.liquidation_value == liquidation
    if expected["nav"] is None or liquidation is None:
        assert result.nav_to_liquidation is None
    elif "nav_to_liquidation" in expected:
        assert result.nav_to_liquidation == Decimal(expected["nav_to_liquidation"])
    else:
        assert result.nav_to_liquidation == result.nav - liquidation
    assert snapshot.fingerprint() == before
    assert snapshot.cash == Decimal(RECORDS["reference"]["cash"])
    assert dict(snapshot.holdings) == {key: int(shares) for key, shares in RECORDS["reference"]["holdings"]}


def test_reference_snapshot_is_frozen_and_scenario_methods_do_not_mutate_it():
    from nse_pipeline.backtest.scenarios import ScenarioPortfolio

    snapshot = reference()
    before = snapshot.fingerprint()
    portfolio = ScenarioPortfolio.from_reference(snapshot)
    assert portfolio.reference is snapshot
    changed = portfolio.buy("AAA", 5, Decimal("80"), fee=Decimal("5"))
    assert changed is not portfolio
    assert portfolio.cash == Decimal("1000")
    assert holdings_map(portfolio) == {"AAA": 10}
    assert snapshot.fingerprint() == before
    with pytest.raises(FrozenInstanceError):
        snapshot.cash = Decimal("1")
    with pytest.raises(ValueError):
        portfolio.sell("AAA", 11, Decimal("90"), fee=Decimal("0"))
    with pytest.raises(ValueError):
        portfolio.buy("AAA", 0, Decimal("90"), fee=Decimal("0"))


def test_reference_snapshot_can_be_built_from_an_authoritative_book_state():
    from nse_pipeline.backtest.accounting_types import Position
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.scenarios import ReferenceSnapshot, ScenarioPortfolio

    book = Portfolio("1000", on=date(2024, 6, 3),
                     positions=(Position("p", "AAA", 10, date(2024, 5, 31)),))
    before = book.snapshot
    snapshot = ReferenceSnapshot.from_book_state(before)
    assert snapshot.cash == 1000
    assert snapshot.holdings == (("AAA", 10),)
    scenario = ScenarioPortfolio.from_reference(snapshot)
    scenario.buy("AAA", 1, Decimal("100"), fee=Decimal("1"))
    scenario.sell("AAA", 2, Decimal("110"), fee=Decimal("1"))
    assert book.snapshot is before
    assert book.snapshot == before
    assert book.positions["p"].shares == 10 and book.balances.cash == 1000


def test_scenario_marks_must_be_available_at_the_scenario_valuation_time():
    from nse_pipeline.backtest.scenarios import ScenarioMark, ScenarioPortfolio

    at = event({"session": "2024-06-04", "phase": 60})
    future = ScenarioMark("AAA", Decimal("90"), date(2024, 6, 5),
                          event({"session": "2024-06-05", "phase": 60}))
    result = ScenarioPortfolio.from_reference(reference()).nav((future,), at=at)
    assert result.nav is None and result.reason == "unpriced_holdings"
    assert result.unpriced_keys == ("AAA",)


def test_scenario_nav_rejects_authoritative_publication():
    from nse_pipeline.backtest.scenarios import ScenarioPortfolio

    at = event({"session": "2024-06-04", "phase": 60})
    result = ScenarioPortfolio.from_reference(reference()).nav(
        marks(CASES["reference_snapshot_unchanged_by_sell"]), at=at)
    assert result.nav == 1950
    with pytest.raises(ValueError, match="hypothetical"):
        result.require_authoritative()
    assert result.authoritative_nav is None


def test_scenario_statistics_reject_authoritative_use():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.scenarios import (
        ScenarioPortfolio, ScenarioStatistics, ScenarioStatisticError)
    from nse_pipeline.backtest.valuation import value

    at = EventTime(date(2024, 6, 4), Phase.CLOSE_VALUATION)
    nav = ScenarioPortfolio.from_reference(reference()).nav(
        marks(CASES["reference_snapshot_unchanged_by_sell"]), at=at)
    statistics = ScenarioStatistics.from_navs((nav,))
    assert statistics.scope == "hypothetical_scenario_only"
    assert statistics.latest_nav == Decimal("1950")
    with pytest.raises(ScenarioStatisticError):
        statistics.authoritative_statistics()
    authoritative = value(Portfolio("1000", on=at.session).snapshot, at)
    with pytest.raises(ScenarioStatisticError):
        ScenarioStatistics.from_navs((authoritative,))
    with pytest.raises(ScenarioStatisticError):
        ScenarioStatistics.from_valuation(authoritative)
    with pytest.raises(ScenarioStatisticError):
        ScenarioStatistics(scope="authoritative")
