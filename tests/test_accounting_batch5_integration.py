"""Batch 5 cross-domain integration: one hand-derived contract fixture.

Identity, continuity, scoped adjustment, lifecycle, hypothetical scenario and
liquidation are joined by the same frozen successor key and the same information
horizon. Every expected value below comes from the labelled synthetic fixture,
not from production output. A later document is appended only to prove that it
cannot change an already-frozen answer.
"""
from datetime import date
from decimal import Decimal
from fractions import Fraction
import json
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden/batch5_integration_cases.json"
RECORDS = json.loads(GOLDEN.read_text(encoding="utf-8"))
NEW = RECORDS["expected_keys"]["new"]
OLD = RECORDS["expected_keys"]["old"]


def event(payload):
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    return EventTime(date.fromisoformat(payload["session"]), Phase(payload["phase"]),
                     payload.get("sequence", 0))


def decimal_or_none(value):
    return None if value is None else Decimal(value)


def identity_inputs():
    from nse_pipeline.backtest.identity import IdentityEdge, Observation

    payload = RECORDS["edge"]
    edge = IdentityEdge(
        edge_id=payload["edge_id"],
        old_symbol=payload["old_symbol"],
        new_symbol=payload["new_symbol"],
        effective_on=date.fromisoformat(payload["effective_on"]),
        available_at=event(payload["available_at"]),
        citation=payload["citation"],
        old_isin=payload["old_isin"],
        new_isin=payload["new_isin"])
    observations = tuple(
        Observation(row["symbol"], row["series"], row["isin"],
                    date.fromisoformat(row["session"]))
        for row in RECORDS["observations"])
    return edge, observations


def identity_ledger():
    from nse_pipeline.backtest.identity import IdentityResolver

    edge, observations = identity_inputs()
    resolver = IdentityResolver((edge,), series=RECORDS["series"],
                                bind_first_isin=RECORDS["bind_first_isin"])
    resolutions = tuple(resolver.observe(observation) for observation in observations)
    return resolver.ledger, resolutions


def documentary(payload):
    from nse_pipeline.backtest.continuity import DocumentaryEvidence

    return DocumentaryEvidence(
        publisher=payload["publisher"],
        document_id=payload["document_id"],
        published_at=date.fromisoformat(payload["published_at"]),
        available_at=event(payload["available_at"]),
        effective_from=date.fromisoformat(payload["effective_from"]),
        effective_to=(None if payload["effective_to"] is None
                      else date.fromisoformat(payload["effective_to"])),
        citation=payload["citation"],
        content_sha256=payload["content_sha256"])


def continuity_boundary(payload):
    from nse_pipeline.backtest.continuity import ContinuityBoundary

    return ContinuityBoundary(
        payload["boundary_id"], OLD, NEW, payload["event_kind"],
        date.fromisoformat(payload["effective_on"]), documentary(payload["evidence"]))


def raw_series():
    from nse_pipeline.backtest.scoped_adjustment import RawPriceSeries

    return RawPriceSeries(NEW, tuple(
        (date.fromisoformat(row["on"]), Decimal(row["price"]))
        for row in RECORDS["adjustment"]["prices"]))


def adjustment_factor():
    from nse_pipeline.backtest.scoped_adjustment import AdjustmentFactor

    payload = RECORDS["adjustment"]["factor"]
    return AdjustmentFactor(
        security_key=NEW,
        action_id=payload["action_id"],
        effective_date=date.fromisoformat(payload["effective_date"]),
        available_at=event(payload["available_at"]),
        factor=payload["factor"],
        scope_from=date.fromisoformat(payload["scope_from"]),
        scope_to=(None if payload["scope_to"] is None
                  else date.fromisoformat(payload["scope_to"])),
        version=payload["version"],
        citation=payload["citation"],
        raw_series_sha256=None)


def lifecycle_record(payload):
    from nse_pipeline.backtest.lifecycle import LifecycleRecord, LifecycleState

    return LifecycleRecord(
        security_key=NEW,
        state=LifecycleState(payload["state"]),
        effective_from=date.fromisoformat(payload["effective_from"]),
        available_at=event(payload["available_at"]),
        citation=payload["citation"],
        terminal_proceeds=decimal_or_none(payload["terminal_proceeds"]),
        effective_to=(None if payload["effective_to"] is None
                      else date.fromisoformat(payload["effective_to"])),
        proceeds_verified=payload["proceeds_verified"])


def lifecycle_records():
    return tuple(lifecycle_record(row) for row in RECORDS["lifecycle"]["records"])


def reference():
    from nse_pipeline.backtest.scenarios import ReferenceSnapshot

    payload = RECORDS["scenario"]["reference"]
    return ReferenceSnapshot(
        cash=Decimal(payload["cash"]),
        holdings=tuple((key, int(shares)) for key, shares in payload["holdings"]),
        claims=tuple(tuple(claim) for claim in payload["claims"]),
        as_of=date.fromisoformat(payload["as_of"]))


def scenario_portfolio():
    from nse_pipeline.backtest.scenarios import ScenarioPortfolio

    scenario = ScenarioPortfolio.from_reference(reference())
    for operation in RECORDS["scenario"]["operations"]:
        if operation["kind"] == "buy":
            scenario = scenario.buy(operation["security_key"], operation["quantity"],
                                    Decimal(operation["price"]),
                                    fee=Decimal(operation["fee"]))
        elif operation["kind"] == "sell":
            scenario = scenario.sell(operation["security_key"], operation["quantity"],
                                     Decimal(operation["price"]),
                                     fee=Decimal(operation["fee"]))
        else:
            raise AssertionError(operation["kind"])
    return scenario


def scenario_mark(payload):
    from nse_pipeline.backtest.scenarios import ScenarioMark

    return ScenarioMark(payload["security_key"], Decimal(payload["price"]),
                        date.fromisoformat(payload["on"]), event(payload["available_at"]))


def liquidation_schedule():
    from nse_pipeline.backtest.liquidation import LiquidationCostSchedule
    from nse_pipeline.specs import CostSpec

    payload = RECORDS["liquidation"]["schedule"]
    return LiquidationCostSchedule(
        schedule_id=payload["schedule_id"],
        effective_from=date.fromisoformat(payload["effective_from"]),
        effective_to=(None if payload["effective_to"] is None
                      else date.fromisoformat(payload["effective_to"])),
        available_at=event(payload["available_at"]),
        citation=payload["citation"],
        costs=CostSpec(**payload["costs"]),
        adverse_slippage_bps=Decimal(payload["adverse_slippage_bps"]))


def liquidation_inputs():
    from nse_pipeline.backtest.liquidation import (
        LiquidationCapacity, LiquidationPrice, SaleOrder)

    order = RECORDS["liquidation"]["order"]
    price = RECORDS["liquidation"]["price"]
    capacity = RECORDS["liquidation"]["capacity"]
    return (
        (SaleOrder(order["order_id"], order["security_key"], order["quantity"]),),
        (LiquidationPrice(price["security_key"], Decimal(price["price"]),
                          date.fromisoformat(price["on"]), event(price["available_at"]),
                          price["citation"]),),
        (LiquidationCapacity(capacity["security_key"],
                             date.fromisoformat(capacity["as_of"]),
                             event(capacity["available_at"]),
                             capacity["capacity_shares"], capacity["citation"]),),
    )


def test_integration_fixture_is_labelled_synthetic_and_hand_derived():
    assert RECORDS["schema_version"] == 1
    assert RECORDS["fixture"] == "synthetic_fixture"
    assert "hand_derived" in RECORDS["status"]
    assert "not_production_P0_pass" in RECORDS["status"]
    assert RECORDS["derivation"].strip()
    assert RECORDS["expected_keys"] == {"old": OLD, "new": NEW}
    assert RECORDS["edge"]["effective_on"] == "2024-05-03"
    assert OLD != NEW


def test_identity_migration_produces_the_keys_used_by_every_domain():
    ledger, resolutions = identity_ledger()

    assert [resolution.status for resolution in resolutions] == [
        "new", "resolved", "resolved"]
    assert tuple(key.key for key in ledger.keys) == (OLD, NEW)
    assert ledger.issues == ()
    assert len(ledger.migrations) == 1
    migration = ledger.migrations[0]
    assert migration.old_key == OLD
    assert migration.new_key == NEW
    assert migration.effective_on == date(2024, 5, 3)
    assert migration.available_at == event(RECORDS["edge"]["available_at"])
    assert ledger.keys[0].retired_on == date(2024, 5, 3)
    assert ledger.keys[1].first_seen == date(2024, 5, 3)


def test_continuity_uses_the_identity_keys_and_future_documents_are_invisible():
    from nse_pipeline.backtest.continuity import admit_boundaries

    decision = event(RECORDS["continuity"]["decision_at"])
    early = (continuity_boundary(RECORDS["continuity"]["boundary"]),)
    future = continuity_boundary(RECORDS["future_continuity_boundary"])

    before = admit_boundaries(early, decision_at=decision)
    after = admit_boundaries(early + (future,), decision_at=decision)
    assert before == after
    assert not hasattr(before, "unpublished_pairs")
    assert not hasattr(after, "unpublished_pairs")
    assert len(before.records) == 1
    assert before.issues == ()
    record = before.resolve(OLD, NEW, date.fromisoformat(
        RECORDS["continuity"]["query_on"]))
    assert record.admitted is True
    assert record.reason == "admitted"
    assert record.record.source_key == OLD
    assert record.record.successor_key == NEW
    assert record.record.evidence.content_sha256 == RECORDS[
        "continuity"]["boundary"]["evidence"]["content_sha256"]

    invisible = after.resolve(OLD, NEW, date.fromisoformat(
        RECORDS["continuity"]["query_on"]))
    assert invisible == record

    later = admit_boundaries(early + (future,), decision_at=event(
        RECORDS["future_continuity_boundary"]["evidence"]["available_at"]))
    assert later.records == ()
    assert any(issue.reason == "duplicate_boundary" for issue in later.issues)
    assert later.resolve(OLD, NEW, date(2024, 5, 3)).reason == "conflicting_evidence"


def test_adjustment_is_point_in_time_exact_and_never_rewrites_raw_prices():
    from nse_pipeline.backtest.scoped_adjustment import adjusted_series

    raw = raw_series()
    raw_prices_before = raw.prices
    raw_digest_before = raw.raw_series_sha256
    factor = adjustment_factor()

    for query in RECORDS["adjustment"]["queries"]:
        adjusted = adjusted_series(raw, (factor,), mode="point_in_time",
                                   decision_at=event(query["decision_at"]))
        assert adjusted.security_key == NEW
        assert adjusted.raw_series_sha256 == raw_digest_before
        assert [(point.on.isoformat(), point.adjusted)
                for point in adjusted.points] == [
            (on, Fraction(value)) for on, value in query["expected_points"]]
        assert adjusted.applied == tuple(
            (action_id, version) for action_id, version in query["expected_applied"])

    assert raw.prices == raw_prices_before
    assert raw.raw_series_sha256 == raw_digest_before
    assert [price for _, price in raw.prices] == [Decimal("100"), Decimal("102"),
                                                  Decimal("104")]


def test_lifecycle_states_are_dated_and_terminal_proceeds_are_never_guessed():
    from nse_pipeline.backtest.lifecycle import (
        blocking_reason, require_negotiable, require_terminal_proceeds,
        resolve_lifecycle)

    records = lifecycle_records()
    future = lifecycle_record(RECORDS["lifecycle"]["future_record"])

    for query in RECORDS["lifecycle"]["queries"]:
        status = resolve_lifecycle(records + (future,), NEW,
                                   date.fromisoformat(query["on"]),
                                   decision_at=event(query["decision_at"]))
        expected = query["expected"]
        assert status.security_key == NEW
        assert status.state.value == expected["state"]
        assert status.reason == expected["reason"]
        assert status.terminal_proceeds == decimal_or_none(expected["terminal_proceeds"])
        assert blocking_reason(status) == expected["blocking_reason"]
        if expected["negotiable"]:
            require_negotiable(status)
        else:
            with pytest.raises(ValueError):
                require_negotiable(status)
        if expected["state"] == "delisted":
            with pytest.raises(ValueError, match="terminal_proceeds_unknown"):
                require_terminal_proceeds(status)

    before = resolve_lifecycle(records, NEW, date(2024, 5, 14),
                               decision_at=event({"session": "2024-05-14",
                                                  "phase": 60, "sequence": 0}))
    after = resolve_lifecycle(records + (future,), NEW, date(2024, 5, 14),
                              decision_at=event({"session": "2024-05-14",
                                                 "phase": 60, "sequence": 0}))
    assert before == after
    assert before.state.value == "listed"

    later = resolve_lifecycle(records + (future,), NEW, date(2024, 5, 14),
                              decision_at=event({"session": "2024-06-05",
                                                 "phase": 10, "sequence": 0}))
    assert later.state.value == "delisted"
    assert later.terminal_proceeds is None


def test_scenario_is_hypothetical_and_cannot_touch_the_reference_or_publish():
    from nse_pipeline.backtest.scenarios import (
        ScenarioStatisticError, ScenarioStatistics)

    snapshot = reference()
    fingerprint = snapshot.fingerprint()
    scenario = scenario_portfolio()
    expected = RECORDS["scenario"]["expected"]

    assert snapshot.cash == Decimal(expected["reference_cash"])
    assert snapshot.holdings == ((NEW, int(expected["reference_shares"])),)
    assert snapshot.fingerprint() == fingerprint

    assert scenario.reference == snapshot
    assert scenario.cash == Decimal(expected["cash"])
    assert scenario.holdings_map == {NEW: int(expected["shares"])}

    mark = scenario_mark(RECORDS["scenario"]["mark"])
    nav = scenario.nav((mark,), at=event(RECORDS["scenario"]["at"]))
    assert nav.nav == Decimal(expected["nav"])
    assert nav.cash == Decimal(expected["cash"])
    assert nav.marked_value == Decimal(expected["marked_value"])
    assert nav.reason == expected["reason"]
    assert nav.hypothetical is True
    assert nav.authoritative is False
    assert nav.authoritative_nav is None
    with pytest.raises(ValueError, match="hypothetical_scenario_value_is_not_authoritative"):
        nav.require_authoritative()

    future_nav = scenario.nav(
        (scenario_mark(RECORDS["scenario"]["future_mark"]),),
        at=event(RECORDS["scenario"]["at"]))
    assert future_nav.nav is None
    assert future_nav.reason == expected["future_mark_reason"]
    assert future_nav.unpriced_keys == (NEW,)

    statistics = ScenarioStatistics.from_navs((nav,))
    assert statistics.latest_nav == Decimal(expected["nav"])
    with pytest.raises(ScenarioStatisticError):
        statistics.authoritative_statistics()
    with pytest.raises(ScenarioStatisticError):
        ScenarioStatistics.from_valuation(nav)


def test_liquidation_uses_the_successor_key_capacity_and_verified_costs():
    from nse_pipeline.backtest.liquidation import estimate_liquidation

    orders, prices, capacities = liquidation_inputs()
    expected = RECORDS["liquidation"]["expected"]
    estimate = estimate_liquidation(
        orders, prices=prices, capacities=capacities,
        schedules=(liquidation_schedule(),),
        on=date.fromisoformat(RECORDS["liquidation"]["on"]),
        decision_at=event(RECORDS["liquidation"]["decision_at"]))

    assert estimate.status == expected["status"]
    assert estimate.gross_proceeds == Decimal(expected["gross_proceeds"])
    assert estimate.fees == Decimal(expected["fees"])
    assert estimate.net_proceeds == Decimal(expected["net_proceeds"])
    assert estimate.zero_friction_subtotal == Decimal(
        expected["zero_friction_subtotal"])
    assert estimate.unknown_reasons == ()
    assert estimate.hypothetical is True
    assert estimate.authoritative is False
    assert estimate.authoritative_nav is None
    assert estimate.claims_created == 0
    with pytest.raises(ValueError, match="liquidation_estimate_is_not_authoritative"):
        estimate.require_authoritative()

    line = estimate.lines[0]
    assert line.order_id == RECORDS["liquidation"]["order"]["order_id"]
    assert line.filled_quantity == expected["filled_quantity"]
    assert line.requested_quantity == expected["requested_quantity"]
    assert line.reference_price == Decimal(expected["reference_price"])
    assert line.slipped_price == Decimal(expected["slipped_price"])
    assert line.gross == Decimal(expected["gross_proceeds"])
    assert line.fees == Decimal(expected["fees"])
    assert line.net == Decimal(expected["net_proceeds"])
    assert line.status == expected["line_status"]


def test_liquidation_fails_closed_when_a_sale_order_has_no_price():
    from nse_pipeline.backtest.liquidation import SaleOrder, estimate_liquidation

    _, prices, capacities = liquidation_inputs()
    estimate = estimate_liquidation(
        (SaleOrder("liq-unknown", OLD, 10),), prices=prices, capacities=capacities,
        schedules=(liquidation_schedule(),),
        on=date.fromisoformat(RECORDS["liquidation"]["on"]),
        decision_at=event(RECORDS["liquidation"]["decision_at"]))

    assert estimate.status == "unknown"
    assert estimate.net_proceeds is None
    assert estimate.unknown_reasons == (f"{OLD}:unpriced",)
    assert estimate.lines[0].status == "unpriced"
    assert estimate.lines[0].filled_quantity == 0


def test_every_domain_uses_the_same_identity_and_information_horizon():
    ledger, _ = identity_ledger()
    migration = ledger.migrations[0]
    boundary = continuity_boundary(RECORDS["continuity"]["boundary"])

    assert boundary.source_key == migration.old_key
    assert boundary.successor_key == migration.new_key
    assert all(record.security_key == NEW for record in lifecycle_records())
    assert raw_series().security_key == NEW
    assert adjustment_factor().security_key == NEW
    assert reference().holdings == ((NEW, 100),)
    assert scenario_portfolio().holdings_map == {NEW: 125}
    assert RECORDS["liquidation"]["order"]["security_key"] == NEW
