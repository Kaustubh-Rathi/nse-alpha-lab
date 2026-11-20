"""Batch 5 scoped adjustments: immutable raw prices, exact factors, point-in-time availability."""
from datetime import date
from decimal import Decimal
from fractions import Fraction
import json
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden/scoped_adjustment_cases.json"
RECORDS = json.loads(GOLDEN.read_text(encoding="utf-8"))
CASES = {case["id"]: case for case in RECORDS["cases"]}


def event(payload):
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    return EventTime(date.fromisoformat(payload["session"]), Phase(payload["phase"]),
                     payload.get("sequence", 0))


def raw_series():
    from nse_pipeline.backtest.scoped_adjustment import RawPriceSeries
    return RawPriceSeries(
        RECORDS["raw_series"]["security_key"],
        tuple((date.fromisoformat(row["on"]), Decimal(row["price"]))
              for row in RECORDS["raw_series"]["prices"]))


def factor(payload):
    from nse_pipeline.backtest.scoped_adjustment import AdjustmentFactor
    return AdjustmentFactor(
        security_key=payload["security_key"], action_id=payload["action_id"],
        effective_date=date.fromisoformat(payload["effective_date"]),
        available_at=event(payload["available_at"]), factor=payload["factor"],
        scope_from=date.fromisoformat(payload["scope_from"]),
        scope_to=(None if payload["scope_to"] is None else date.fromisoformat(payload["scope_to"])),
        version=payload["version"], citation=payload["citation"],
        raw_series_sha256=payload["raw_series_sha256"])


@pytest.mark.parametrize("case_id", tuple(CASES))
def test_golden_scoped_adjustment_cases_match_hand_derived_values(case_id):
    from nse_pipeline.backtest.scoped_adjustment import adjusted_series
    case = CASES[case_id]
    series = raw_series()
    factors = tuple(factor(row) for row in case["factors"])
    if "expected_error" in case:
        with pytest.raises(ValueError, match=case["expected_error"]):
            adjusted_series(series, factors, mode="point_in_time",
                            decision_at=event({"session": "2024-06-05", "phase": 60}))
        return
    for query in case["queries"]:
        result = adjusted_series(series, factors, mode=query["mode"],
                                 decision_at=event(query["decision_at"]))
        assert [(point.on.isoformat(), point.adjusted)
                for point in result.points] == [
                    (on, Fraction(value)) for on, value in query["expected"]["points"]]
        assert [(action_id, version) for action_id, version in result.applied] == [
            (action_id, version) for action_id, version in query["expected"]["applied"]]


def test_raw_series_is_immutable_and_never_rewritten():
    from nse_pipeline.backtest.scoped_adjustment import adjusted_series
    case = CASES["point_in_time_excludes_future_split"]
    series = raw_series()
    digest = series.raw_series_sha256
    original = tuple((point_on, price) for point_on, price in series.prices)
    result = adjusted_series(series, tuple(factor(row) for row in case["factors"]),
                             mode="point_in_time", decision_at=event(case["queries"][1]["decision_at"]))
    assert series.prices == original
    assert series.raw_series_sha256 == digest
    assert result.raw_series_sha256 == digest
    assert result is not series
    assert result.value_on(date(2024, 6, 3)) == Fraction(50)
    with pytest.raises((AttributeError, TypeError)):
        series.prices = ()


def test_adjustment_view_does_not_rewrite_recorded_cash_or_units():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase, Position
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.scoped_adjustment import adjusted_series
    case = CASES["point_in_time_excludes_future_split"]
    book = Portfolio("500", on=date(2024, 6, 3),
                     positions=(Position("p", "AAA", 10, date(2024, 5, 31)),))
    before = book.snapshot
    adjusted_series(raw_series(), tuple(factor(row) for row in case["factors"]),
                    mode="point_in_time",
                    decision_at=EventTime(date(2024, 6, 5), Phase.PRE_OPEN_EVIDENCE))
    assert book.snapshot == before
    assert book.balances.cash == 500 and book.positions["p"].shares == 10


def test_factor_validation_rejects_floats_zero_scope_conflicts_and_bad_version():
    from nse_pipeline.backtest.scoped_adjustment import AdjustmentFactor
    base = dict(
        security_key="AAA", action_id="split", effective_date=date(2024, 6, 5),
        available_at=event({"session": "2024-06-05", "phase": 10}), factor="1/2",
        scope_from=date(2024, 6, 1), scope_to=date(2024, 6, 4), version=1,
        citation="synthetic", raw_series_sha256=None)
    with pytest.raises(TypeError):
        AdjustmentFactor(**{**base, "factor": 0.5})
    with pytest.raises(ValueError):
        AdjustmentFactor(**{**base, "factor": "0"})
    with pytest.raises(ValueError):
        AdjustmentFactor(**{**base, "factor": "-1/2"})
    with pytest.raises(ValueError):
        AdjustmentFactor(**{**base, "effective_date": date(2024, 5, 31)})
    with pytest.raises(ValueError):
        AdjustmentFactor(**{**base, "version": 0})
    with pytest.raises(ValueError):
        AdjustmentFactor(**{**base, "citation": ""})
    with pytest.raises(ValueError):
        AdjustmentFactor(**{**base, "raw_series_sha256": "xyz"})
    assert AdjustmentFactor(**{**base, "factor": Decimal("0.5")}).factor == Fraction(1, 2)
    assert AdjustmentFactor(**{**base, "factor": Fraction(1, 2)}).factor == Fraction(1, 2)


def test_duplicate_versions_and_security_mismatch_fail_closed():
    from nse_pipeline.backtest.scoped_adjustment import AdjustmentFactor, adjusted_series
    case = CASES["point_in_time_excludes_future_split"]
    proof = factor(case["factors"][0])
    duplicate = AdjustmentFactor(
        proof.security_key, proof.action_id, proof.effective_date, proof.available_at,
        "1/4", proof.scope_from, proof.scope_to, proof.version, "conflicting duplicate")
    with pytest.raises(ValueError, match="conflicting_adjustment_versions"):
        adjusted_series(raw_series(), (proof, duplicate), mode="point_in_time",
                        decision_at=event({"session": "2024-06-05", "phase": 10}))
    foreign = AdjustmentFactor(
        "OTHER", "split", proof.effective_date, proof.available_at, "1/2",
        proof.scope_from, proof.scope_to, 1, "foreign")
    with pytest.raises(ValueError, match="factor_security_scope_mismatch"):
        adjusted_series(raw_series(), (foreign,), mode="point_in_time",
                        decision_at=event({"session": "2024-06-05", "phase": 10}))


def test_prefix_invariance_of_point_in_time_queries():
    from nse_pipeline.backtest.scoped_adjustment import AdjustmentFactor, adjusted_series
    series = raw_series()
    early = (factor(CASES["later_version_supersedes_only_when_available"]["factors"][0]),)
    later = factor(CASES["later_version_supersedes_only_when_available"]["factors"][1])
    decision = event({"session": "2024-06-04", "phase": 60})
    before = adjusted_series(series, early, mode="point_in_time", decision_at=decision)
    after = adjusted_series(series, early + (later,), mode="point_in_time", decision_at=decision)
    assert before.points == after.points
    assert before.applied == after.applied
    ex_post = adjusted_series(series, early + (later,), mode="ex_post", decision_at=decision)
    assert ex_post.value_on(date(2024, 6, 3)) == Fraction(25)
    assert before.value_on(date(2024, 6, 3)) == Fraction(50)


def test_point_in_time_mode_requires_an_explicit_decision_event():
    from nse_pipeline.backtest.scoped_adjustment import adjusted_series
    with pytest.raises(ValueError, match="decision_at"):
        adjusted_series(raw_series(), (), mode="point_in_time", decision_at=None)
    with pytest.raises(ValueError):
        adjusted_series(raw_series(), (), mode="not_a_mode",
                        decision_at=event({"session": "2024-06-05", "phase": 10}))
