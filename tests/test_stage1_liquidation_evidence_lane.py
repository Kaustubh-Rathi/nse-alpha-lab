"""Batch 9 lane G: frozen production reference book and isolated liquidation evidence.

The test reads the frozen lane artifacts, re-runs the production liquidation and
scenario helpers over those inputs, and checks digests, arithmetic, capacity,
fee-once behavior, reference/scenario isolation and future-mark invisibility.
It does not touch production data, goldens or another lane's files.
"""
from datetime import date
from decimal import Decimal, ROUND_FLOOR
import hashlib
import json
from pathlib import Path

import pytest

from nse_pipeline.backtest.accounting_types import EventTime, Phase
from nse_pipeline.backtest.fees import fee_breakdown
from nse_pipeline.backtest.liquidation import (
    LiquidationCapacity,
    LiquidationCostSchedule,
    LiquidationPrice,
    SaleOrder,
    estimate_liquidation,
    select_cost_schedule,
)
from nse_pipeline.backtest.scenarios import (
    ReferenceSnapshot,
    ScenarioHolding,
    ScenarioMark,
    ScenarioPortfolio,
    ScenarioStatisticError,
    ScenarioStatistics,
)
from nse_pipeline.specs import CostSpec

ROOT = Path(__file__).resolve().parents[1]
LANE = ROOT / "verification" / "stage1" / "batch9" / "liquidation"
SOURCE_FIELDS = (
    "security_key", "symbol", "isin", "quantity", "raw_open_inr", "raw_high_inr",
    "raw_low_inr", "raw_close_inr", "previous_close_inr", "volume",
    "traded_value_inr", "trades",
)


def load(name):
    return json.loads((LANE / name).read_text(encoding="utf-8"))


def event(payload):
    return EventTime(date.fromisoformat(payload["session"]), Phase(payload["phase"]),
                     payload.get("sequence", 0))


def decimal_or_none(value):
    return None if value is None else Decimal(value)


def canonical_sha256(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_schedule():
    payload = load("cost_schedule.json")
    return LiquidationCostSchedule(
        schedule_id=payload["schedule_id"],
        effective_from=date.fromisoformat(payload["effective_from"]),
        effective_to=(None if payload["effective_to"] is None
                      else date.fromisoformat(payload["effective_to"])),
        available_at=event(payload["available_at"]),
        citation=payload["citation"],
        costs=CostSpec(**payload["costs"]),
        adverse_slippage_bps=Decimal(payload["adverse_slippage_bps"]))


def build_inputs(run):
    orders = tuple(SaleOrder(row["order_id"], row["security_key"], row["quantity"],
                             row.get("position_id", ""))
                   for row in run["inputs"]["orders"])
    prices = tuple(LiquidationPrice(
        row["security_key"], Decimal(row["price_inr"]), date.fromisoformat(row["on"]),
        event(row["available_at"]), row["citation"])
        for row in run["inputs"]["prices"])
    capacities = tuple(LiquidationCapacity(
        row["security_key"], date.fromisoformat(row["as_of"]),
        event(row["available_at"]), row["capacity_shares"], row["citation"])
        for row in run["inputs"]["capacities"])
    return orders, prices, capacities


def build_estimate():
    run = load("liquidation_run.json")
    orders, prices, capacities = build_inputs(run)
    estimate = estimate_liquidation(
        orders, prices=prices, capacities=capacities, schedules=(build_schedule(),),
        on=date.fromisoformat(run["as_of"]), decision_at=event(run["decision_at"]))
    return estimate, run, orders, prices, capacities


def build_snapshot(book):
    return ReferenceSnapshot(
        cash=Decimal(book["cash_inr"]),
        holdings=tuple((row["security_key"], row["quantity"]) for row in book["positions"]),
        as_of=date.fromisoformat(book["as_of"]))


def build_scenario(snapshot, isolation):
    scenario = ScenarioPortfolio(
        snapshot, snapshot.cash,
        tuple(ScenarioHolding(key, shares) for key, shares in snapshot.holdings),
        label=isolation["inputs"]["scenario_label"])
    for operation in isolation["inputs"]["scenario_operations"]:
        assert operation["kind"] == "sell"
        scenario = scenario.sell(
            operation["security_key"], operation["quantity"],
            Decimal(operation["price_inr"]), fee=Decimal(operation["fee_inr"]))
    return scenario


def build_marks(isolation, rows=None):
    rows = isolation["inputs"]["current_marks"] if rows is None else rows
    return tuple(ScenarioMark(
        row["security_key"], Decimal(row["price_inr"]), date.fromisoformat(row["on"]),
        event(row["available_at"])) for row in rows)


def test_lane_files_are_digest_bound_and_match_provenance():
    provenance = load("provenance.json")
    assert provenance["lane"] == "G"
    assert provenance["repository"]["branch"] == "stage1-completion"
    assert provenance["repository"]["head"] == "c8b65710749fea0eb4da5f346392bc58b6e8378d"
    assert "No strategy run" in provenance["scope"]
    assert "no returns" in provenance["scope"]

    expected_files = {
        "README.md", "capacity_schedule.json", "cost_schedule.json",
        "isolation.json", "liquidation_run.json", "reference_book.json",
    }
    files = provenance["files"]
    assert {row["path"] for row in files} == expected_files
    for row in files:
        payload = (LANE / row["path"]).read_bytes()
        assert len(payload) == row["bytes"]
        assert hashlib.sha256(payload).hexdigest() == row["sha256"]
    manifest = hashlib.sha256("\n".join(
        f"{row['path']}:{row['sha256']}"
        for row in sorted(files, key=lambda item: item["path"])
    ).encode("utf-8")).hexdigest()
    assert manifest == provenance["manifest_sha256"]

    for source in provenance["sources"]:
        path = ROOT / source["path"]
        assert source["access"] == "read_only"
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == source["sha256"]
    for code in provenance["code"]:
        path = ROOT / code["path"]
        assert path.is_file()
        assert len(code["sha256"]) == 64
        assert code["bytes"] > 0
    assert "working tree" in provenance["code_provenance_note"]

    book = load("reference_book.json")
    capacity = load("capacity_schedule.json")
    extract = {
        "reference_session": book["source_session"],
        "window_dates": capacity["inputs"]["window_dates"],
        "positions": [{field: row[field] for field in SOURCE_FIELDS}
                      for row in book["positions"]],
        "capacity_traded_values_inr": {
            key: row["traded_value_inr"]
            for key, row in capacity["inputs"]["per_name"].items()
        },
        "capacity_raw_closes_inr": {
            key: row["raw_close_inr"]
            for key, row in capacity["inputs"]["per_name"].items()
        },
        "participation_rate": "0.05",
        "capacity_rule": ("floor_paise(mean_traded_value_inr * 0.05) then "
                          "floor(capacity_notional_inr / raw_close_inr)"),
    }
    assert canonical_sha256(extract) == provenance["source_extract"]["extract_sha256"]
    assert provenance["source_extract"]["rows_extracted"] == 100
    assert provenance["source_extract"]["all_100_rows_valid_ohlcv"] is True
    assert (provenance["source_extract"]["source_session_eq_usable_rows"]
            == book["selection"]["usable_eq_rows_on_reference_session"])


def test_reference_book_and_capacity_schedule_are_reproducible():
    book = load("reference_book.json")
    capacity = load("capacity_schedule.json")
    assert book["as_of"] == capacity["as_of"] == "2022-03-31"
    assert book["source_session"] == book["as_of"]
    assert book["totals"]["position_count"] == len(book["positions"]) == 5
    assert book["totals"]["total_shares"] == sum(row["quantity"] for row in book["positions"])
    assert len({row["security_key"] for row in book["positions"]}) == 5

    positions = {row["security_key"]: row for row in book["positions"]}
    for row in book["positions"]:
        assert row["security_key"] == f"ISIN:{row['isin']}"
        assert row["source_session"] == book["as_of"]
        assert row["valid_ohlcv"] is True
        assert row["quantity"] > 0
        assert row["volume"] > 0
        assert Decimal(row["traded_value_inr"]) > 0
        opening = Decimal(row["raw_open_inr"])
        high = Decimal(row["raw_high_inr"])
        low = Decimal(row["raw_low_inr"])
        close = Decimal(row["raw_close_inr"])
        assert low <= min(opening, close) <= max(opening, close) <= high

    snapshot = build_snapshot(book)
    assert snapshot.fingerprint() == book["snapshot_fingerprint_sha256"]
    holdings_value = sum(
        (Decimal(row["quantity"]) * Decimal(row["raw_close_inr"])
         for row in book["positions"]), Decimal("0"))
    assert holdings_value == Decimal(book["totals"]["reference_mark_to_market_holdings_inr"])
    assert (Decimal(book["cash_inr"]) + holdings_value
            == Decimal(book["totals"]["reference_mark_to_market_nav_inr"]))
    assert book["totals"]["authoritative"] is False

    window = capacity["inputs"]["window_dates"]
    assert len(window) == capacity["rule"]["window_sessions"] == 20
    assert len(set(window)) == 20
    assert tuple(sorted(window)) == tuple(window)
    assert window[-1] == capacity["as_of"]
    assert Decimal(capacity["rule"]["participation_rate"]) == Decimal("0.05")
    assert capacity["rule"]["window_order"] == "oldest_to_newest"
    assert "window_dates[i]" in capacity["rule"]["traded_value_alignment"]

    per_name = capacity["inputs"]["per_name"]
    assert set(per_name) == set(positions)
    capacities = {row["security_key"]: row for row in capacity["capacities"]}
    assert set(capacities) == set(positions)
    for key, row in per_name.items():
        assert row["observed_sessions"] == 20
        assert row["first_session"] == window[0]
        assert row["last_session"] == window[-1]
        values = [Decimal(value) for value in row["traded_value_inr"]]
        assert len(values) == 20
        assert all(value > 0 for value in values)
        total = sum(values, Decimal("0"))
        mean = total / Decimal("20")
        assert Decimal(row["traded_value_sum_inr"]) == total
        assert Decimal(row["mean_traded_value_inr"]) == mean
        cap_notional = (mean * Decimal("0.05")).quantize(
            Decimal("0.01"), rounding=ROUND_FLOOR)
        assert Decimal(row["capacity_notional_inr"]) == cap_notional
        close = Decimal(row["raw_close_inr"])
        cap_shares = int((cap_notional / close).to_integral_value(rounding=ROUND_FLOOR))
        assert row["capacity_shares"] == cap_shares > 0
        assert capacities[key]["capacity_shares"] == cap_shares
        assert capacities[key]["as_of"] == capacity["as_of"]
        assert event(capacities[key]["available_at"]) == event(capacity["available_at"])
        assert values[-1] == Decimal(positions[key]["traded_value_inr"])
        assert close == Decimal(positions[key]["raw_close_inr"])
    assert capacity["unresolved"] == []


def test_liquidation_run_matches_frozen_fills_and_reconciles():
    estimate, run, orders, prices, capacities = build_estimate()
    book = load("reference_book.json")
    expected = run["estimate"]

    assert estimate.status == expected["status"] == "estimated"
    assert estimate.unknown_reasons == ()
    assert estimate.schedule_id == expected["schedule_id"]
    assert estimate.gross_proceeds == Decimal(expected["gross_proceeds_inr"])
    assert estimate.fees == Decimal(expected["fees_inr"])
    assert estimate.net_proceeds == Decimal(expected["net_proceeds_inr"])
    assert estimate.zero_friction_subtotal == Decimal(expected["zero_friction_subtotal_inr"])
    assert estimate.hypothetical is True
    assert estimate.authoritative is False
    assert estimate.authoritative_nav is None
    assert estimate.claims_created == 0
    with pytest.raises(ValueError, match="liquidation_estimate_is_not_authoritative"):
        estimate.require_authoritative()
    with pytest.raises(ValueError, match="liquidation_estimate_is_not_authoritative"):
        estimate.authoritative_statistics()

    assert len(estimate.lines) == len(expected["lines"]) == 5
    order_key = {row["order_id"]: row["security_key"] for row in run["inputs"]["orders"]}
    capacity_by_key = {row["security_key"]: row["capacity_shares"]
                       for row in run["inputs"]["capacities"]}
    for line, target in zip(estimate.lines, expected["lines"]):
        assert line.order_id == target["order_id"]
        assert line.requested_quantity == target["requested_quantity"]
        assert line.filled_quantity == target["filled_quantity"]
        assert line.reference_price == decimal_or_none(target["reference_price_inr"])
        assert line.slipped_price == decimal_or_none(target["slipped_price_inr"])
        assert line.gross == decimal_or_none(target["gross_inr"])
        assert line.fees == decimal_or_none(target["fees_inr"])
        assert line.net == decimal_or_none(target["net_inr"])
        assert line.status == target["status"]
        assert line.filled_quantity == min(line.requested_quantity,
                                           capacity_by_key[order_key[line.order_id]])
        assert line.filled_quantity <= capacity_by_key[order_key[line.order_id]]
        if line.requested_quantity > capacity_by_key[order_key[line.order_id]]:
            assert line.status == "capacity_limited"
        else:
            assert line.status == "filled"

    assert estimate.fees == sum((line.fees for line in estimate.lines), Decimal("0.00"))
    assert estimate.net_proceeds == estimate.gross_proceeds - estimate.fees
    assert all(line.net == line.gross - line.fees for line in estimate.lines)
    assert estimate.zero_friction_subtotal == sum(
        (line.reference_price * line.filled_quantity for line in estimate.lines
         if line.filled_quantity), Decimal("0.00"))
    for line in estimate.lines:
        fees = fee_breakdown(line.gross, "sell", build_schedule().costs)
        assert line.fees == fees.total
        assert fees.brokerage == Decimal("20.00")
        assert fees.gst == Decimal("3.60")

    reconciliation = run["reconciliation"]
    assert reconciliation["order_count"] == len(estimate.lines) == 5
    assert reconciliation["filled_order_count"] == 1
    assert reconciliation["capacity_limited_order_count"] == 4
    assert reconciliation["zero_capacity_order_count"] == 0
    assert reconciliation["unknown_order_count"] == 0
    assert reconciliation["fees_charged_once"] is True
    assert reconciliation["capacity_respected"] is True
    assert reconciliation["cash_reconciled"] is True
    assert reconciliation["positions_reconciled"] is True
    assert reconciliation["requested_shares_total"] == sum(
        line.requested_quantity for line in estimate.lines)
    assert reconciliation["filled_shares_total"] == sum(
        line.filled_quantity for line in estimate.lines)
    assert reconciliation["residual_shares_total"] == (
        reconciliation["requested_shares_total"] - reconciliation["filled_shares_total"])
    assert (Decimal(reconciliation["ending_cash_inr"])
            == Decimal(reconciliation["reference_cash_inr"]) + estimate.net_proceeds)
    assert (Decimal(reconciliation["filled_notional_at_reference_inr"])
            == estimate.zero_friction_subtotal)
    assert (Decimal(reconciliation["requested_notional_at_reference_inr"])
            == Decimal(reconciliation["residual_notional_at_reference_inr"])
            + Decimal(reconciliation["filled_notional_at_reference_inr"]))
    assert Decimal(reconciliation["requested_notional_at_reference_inr"]) == sum(
        (Decimal(row["quantity"]) * Decimal(row["raw_close_inr"])
         for row in book["positions"]), Decimal("0.00"))

    reference_positions = {row["security_key"]: row["quantity"]
                           for row in reconciliation["reference_positions"]}
    residual_positions = {row["security_key"]: row["quantity"]
                          for row in reconciliation["residual_positions"]}
    for line in estimate.lines:
        key = order_key[line.order_id]
        assert residual_positions.get(key, 0) == (
            reference_positions[key] - line.filled_quantity)
    assert sum(residual_positions.values()) == reconciliation["residual_shares_total"]

    schedule = build_schedule()
    assert select_cost_schedule((schedule,), on=date(2022, 3, 31),
                                decision_at=event(run["decision_at"])) is schedule
    with pytest.raises(ValueError, match="cost_schedule_unavailable"):
        select_cost_schedule((schedule,), on=date(2022, 3, 30),
                             decision_at=event(run["decision_at"]))
    with pytest.raises(ValueError, match="cost_schedule_unavailable"):
        select_cost_schedule((schedule,), on=date(2022, 3, 31),
                             decision_at=event({"session": "2022-03-30", "phase": 60}))


def test_scenario_nav_is_isolated_from_the_reference_nav():
    book = load("reference_book.json")
    isolation = load("isolation.json")
    estimate, run, _, _, _ = build_estimate()
    snapshot = build_snapshot(book)
    before = snapshot.fingerprint()
    scenario = build_scenario(snapshot, isolation)
    after = snapshot.fingerprint()
    assert before == after == book["snapshot_fingerprint_sha256"]
    assert scenario.reference is snapshot
    assert isolation["reference"]["fingerprint_unchanged"] is True

    marks = build_marks(isolation)
    nav = scenario.nav(marks, at=event(isolation["valuation_at"]))
    assert nav.nav is not None
    assert nav.reason == isolation["scenario"]["nav_reason"] == "complete_marks"
    assert nav.nav == Decimal(isolation["scenario"]["post_liquidation_nav_inr"])
    assert nav.cash == Decimal(isolation["scenario"]["post_liquidation_cash_inr"])
    assert nav.marked_value == Decimal(
        isolation["scenario"]["post_liquidation_marked_value_inr"])
    assert nav.hypothetical is True
    assert nav.authoritative is False
    assert nav.authoritative_nav is None
    with pytest.raises(ValueError, match="hypothetical_scenario_value_is_not_authoritative"):
        nav.require_authoritative()
    assert scenario.holdings_map == {
        row["security_key"]: row["quantity"]
        for row in isolation["scenario"]["post_liquidation_holdings"]
    }

    reference_nav = Decimal(book["cash_inr"]) + sum(
        (Decimal(row["quantity"]) * Decimal(row["raw_close_inr"])
         for row in book["positions"]), Decimal("0.00"))
    assert reference_nav == Decimal(isolation["reference"]["mark_to_market_nav_inr"])
    assert reference_nav == Decimal(isolation["scenario"]["pre_liquidation_nav_inr"])
    assert Decimal(isolation["scenario"]["post_liquidation_nav_inr"]) != reference_nav

    pre_scenario = ScenarioPortfolio(
        snapshot, snapshot.cash,
        tuple(ScenarioHolding(key, shares) for key, shares in snapshot.holdings),
        label=isolation["inputs"]["scenario_label"])
    pre_marks = tuple(ScenarioMark(
        row["security_key"], Decimal(row["raw_close_inr"]),
        date.fromisoformat(book["as_of"]), event(isolation["valuation_at"]))
        for row in book["positions"])
    assert pre_scenario.nav(pre_marks, at=event(isolation["valuation_at"])).nav == reference_nav

    difference = reference_nav - nav.nav
    assert difference == Decimal(
        isolation["scenario"]["difference_reference_minus_scenario_inr"])
    assert difference == estimate.zero_friction_subtotal - estimate.net_proceeds
    assert isolation["scenario"]["difference_reconciled"] is True
    assert nav.cash - snapshot.cash == estimate.net_proceeds
    assert nav.cash - snapshot.cash == Decimal(
        isolation["liquidation"]["scenario_cash_minus_reference_cash_inr"])
    assert isolation["liquidation"]["cash_reconciled"] is True
    assert isolation["liquidation"]["estimate_is_scenario_nav"] is False
    assert isolation["liquidation"]["estimate_authoritative"] is False
    assert isolation["liquidation"]["estimate_claims_created"] == 0

    statistics = ScenarioStatistics.from_navs((nav,))
    assert statistics.scope == "hypothetical_scenario_only"
    assert statistics.latest_nav == nav.nav
    with pytest.raises(ScenarioStatisticError):
        statistics.authoritative_statistics()
    with pytest.raises(ScenarioStatisticError):
        ScenarioStatistics.from_valuation(nav)
    assert isolation["scenario"]["authoritative_statistics_published"] is False

    assert isolation["separation"] == {
        "same_reference_snapshot": True,
        "reference_mutated": False,
        "scenario_nav_equals_reference_nav": False,
        "scenario_nav_is_authoritative": False,
        "reference_nav_is_authoritative": False,
        "liquidation_estimate_is_separate": True,
        "authoritative_valuation_published": False,
    }

    held_probe = next(row for row in isolation["future_mark_probes"]
                      if row["probe_id"] == "future_mark_for_held_key")
    held_marks = tuple(mark for mark in marks
                       if mark.security_key != held_probe["security_key"])
    future_held = ScenarioMark(
        held_probe["security_key"], Decimal(held_probe["mark_price_inr"]),
        date.fromisoformat(held_probe["mark_on"]), event(held_probe["mark_available_at"]))
    held_nav = scenario.nav(held_marks + (future_held,), at=event(isolation["valuation_at"]))
    assert held_nav.nav is None
    assert held_nav.reason == held_probe["observed_reason"] == "unpriced_holdings"
    assert held_nav.unpriced_keys == (held_probe["security_key"],)
    assert held_probe["future_price_used"] is False

    unheld_probe = next(row for row in isolation["future_mark_probes"]
                        if row["probe_id"] == "future_mark_for_unheld_key")
    future_unheld = ScenarioMark(
        unheld_probe["security_key"], Decimal(unheld_probe["mark_price_inr"]),
        date.fromisoformat(unheld_probe["mark_on"]), event(unheld_probe["mark_available_at"]))
    unheld_nav = scenario.nav(marks + (future_unheld,), at=event(isolation["valuation_at"]))
    assert unheld_nav.nav == nav.nav
    assert unheld_nav.reason == nav.reason
    assert unheld_probe["future_price_used"] is False


def test_missing_and_future_facts_fail_closed_without_substitution():
    estimate, run, orders, prices, capacities = build_estimate()
    book = load("reference_book.json")
    isolation = load("isolation.json")
    schedule = build_schedule()
    probe_key = "ISIN:INE040A01034"
    probe_order = (SaleOrder("probe-hdfcbank", probe_key, 1000),)
    current_price = next(row for row in prices if row.security_key == probe_key)
    current_capacity = next(row for row in capacities if row.security_key == probe_key)
    future_probe = next(row for row in isolation["future_mark_probes"]
                        if row["probe_id"] == "future_mark_for_held_key")
    future_date = date.fromisoformat(future_probe["mark_on"])
    future_event = event(future_probe["mark_available_at"])
    future_price = LiquidationPrice(
        probe_key, Decimal(future_probe["mark_price_inr"]), future_date, future_event,
        "production observation after the decision; must not be used")
    late_event = EventTime(date(2022, 4, 1), Phase.PRE_OPEN_EVIDENCE)

    def probe(*, probe_prices, probe_capacities):
        return estimate_liquidation(
            probe_order, prices=probe_prices, capacities=probe_capacities,
            schedules=(schedule,), on=date(2022, 3, 31),
            decision_at=event(run["decision_at"]))

    results = {
        "missing_price_does_not_use_last_close": probe(
            probe_prices=(), probe_capacities=(current_capacity,)),
        "future_price_is_invisible": probe(
            probe_prices=(future_price,), probe_capacities=(current_capacity,)),
        "same_session_price_available_after_decision_is_invisible": probe(
            probe_prices=(LiquidationPrice(
                probe_key, current_price.price, date(2022, 3, 31), late_event,
                "information-horizon probe"),),
            probe_capacities=(current_capacity,)),
        "missing_capacity_is_unknown_not_zero": probe(
            probe_prices=(current_price,), probe_capacities=()),
        "future_capacity_is_invisible": probe(
            probe_prices=(current_price,),
            probe_capacities=(LiquidationCapacity(
                probe_key, date(2022, 3, 31), late_event,
                current_capacity.capacity_shares, "information-horizon probe"),)),
    }
    assert set(results) == {row["probe_id"] for row in run["fail_closed_probes"]}
    for target in run["fail_closed_probes"]:
        result = results[target["probe_id"]]
        line = result.lines[0]
        assert target["failed_closed"] is True
        assert result.status == target["observed_status"] == "unknown"
        assert result.net_proceeds is None
        assert list(result.unknown_reasons) == target["observed_unknown_reasons"]
        assert line.status == target["observed_line_status"]
        assert line.filled_quantity == target["observed_filled_quantity"] == 0
        assert line.slipped_price is None and line.gross is None and line.fees is None

    assert results["missing_price_does_not_use_last_close"].lines[0].status == "unpriced"
    assert results["missing_price_does_not_use_last_close"].lines[0].reference_price is None
    assert results["future_price_is_invisible"].lines[0].reference_price is None
    assert results["same_session_price_available_after_decision_is_invisible"].lines[0].reference_price is None
    missing_capacity_line = results["missing_capacity_is_unknown_not_zero"].lines[0]
    assert missing_capacity_line.status == "capacity_unknown"
    assert missing_capacity_line.reference_price == current_price.price
    future_capacity_line = results["future_capacity_is_invisible"].lines[0]
    assert future_capacity_line.status == "capacity_unknown"
    assert future_capacity_line.reference_price == current_price.price
    assert book["as_of"] == "2022-03-31"
    assert estimate.claims_created == 0


def test_lane_publishes_no_strategy_or_performance_statistic():
    forbidden_keys = {
        "annual_return", "annualized_return", "cagr", "drawdown", "hit_rate",
        "performance", "returns", "sharpe", "total_return",
    }

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key.lower() not in forbidden_keys, key
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for name in ("reference_book.json", "cost_schedule.json", "capacity_schedule.json",
                 "liquidation_run.json", "isolation.json"):
        walk(load(name))
    run = load("liquidation_run.json")
    assert run["invariants"]["no_strategy_run"] is True
    assert run["invariants"]["no_performance_statistics_published"] is True
    assert run["invariants"]["no_fill_beyond_capacity"] is True
    assert run["invariants"]["fees_charged_once_per_order"] is True
    assert run["invariants"]["unknown_never_becomes_zero_or_a_fill"] is True
    assert run["estimate"]["claims_created"] == 0
    assert run["estimate"]["hypothetical"] is True
    assert run["estimate"]["authoritative"] is False
