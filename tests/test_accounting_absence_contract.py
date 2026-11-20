"""D-09 absence truth table: every row is checked against hand-derived golden arithmetic.

The golden file is an independent oracle. Nothing in this module derives an expected
value from production output; it only builds the input ledger described by the case
and compares the production record with the hand-written expectation.
"""
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
import json
from pathlib import Path

import pytest

from nse_pipeline.backtest.adv import capacity_notional, trailing_adv
from nse_pipeline.backtest.evidence import ObservationLedger
from nse_pipeline.backtest.observations import Bar, SessionStream
from nse_pipeline.specs import ExecutionSpec

GOLDEN = Path(__file__).parent / "golden/absence_cases.json"
RECORDS = json.loads(GOLDEN.read_text(encoding="utf-8"))
START = date.fromisoformat(RECORDS["start_session"])
DEFAULT_WINDOW = RECORDS["window_sessions"]
RATE = Decimal(RECORDS["capacity_rate"])
CASES = {case["id"]: case for case in RECORDS["cases"]}


def decimal_or_none(value):
    return None if value is None else Decimal(str(value))


def fraction_or_none(value):
    return None if value is None else Fraction(str(value))


def oracle_floor_cents(value: Fraction) -> Decimal:
    """Independent floor-to-paise oracle, without importing production's floor_cents."""
    cents = (value.numerator * 100) // value.denominator
    return Decimal(cents) / Decimal(100)


def spec_for(window_sessions: int) -> ExecutionSpec:
    rate = float(RATE)
    return ExecutionSpec(trade_settlement_lag_sessions=1, sizing_participation_rate=rate,
                         max_participation_rate=max(rate, 0.05),
                         adv_window_sessions=window_sessions)


def make_bar(key: str, session: date, row: dict) -> Bar:
    if not row.get("valid", True):
        return Bar(key=key, symbol=key, series="EQ", session=session, open=None, high=None,
                   low=None, close=None, volume=0, valid_ohlcv=False,
                   isin="INE000000001", traded_value=None, trades=None)
    close = Decimal(str(row.get("close", "50")))
    traded_value = row.get("traded_value")
    return Bar(key=key, symbol=key, series="EQ", session=session, open=close, high=close,
               low=close, close=close, volume=int(row.get("volume", 100)), valid_ohlcv=True,
               isin="INE000000001",
               traded_value=None if traded_value is None else Decimal(str(traded_value)),
               trades=row.get("trades", 10))


def bar_spec(case: dict, index: int):
    overrides = {row["index"]: row for row in case.get("bars", ())}
    if index in overrides:
        row = overrides[index]
        if row.get("missing"):
            return None
        merged = dict(case.get("all_bars") or {})
        merged.update({name: value for name, value in row.items() if name != "index"})
        return merged
    return dict(case["all_bars"]) if case.get("all_bars") is not None else None


def build_case(case: dict):
    key = case.get("key", "K")
    window_sessions = case.get("window_sessions", DEFAULT_WINDOW)
    preload_sessions = case.get("preload_sessions", 0)
    sessions = [START + timedelta(days=i) for i in range(preload_sessions + window_sessions)]
    stream = SessionStream()
    ledger = ObservationLedger(stream)
    for index, session in enumerate(sessions):
        stream.observe(session)
        bars = []
        if index < preload_sessions:
            if index == 0 and case.get("preload_bar"):
                bars = [make_bar(key, session, case["preload_bar"])]
        else:
            row = bar_spec(case, index - preload_sessions)
            if row is not None:
                bars = [make_bar(key, session, row)]
        ledger.ingest(session, bars)
    return ledger, key, sessions[-1], window_sessions


def run_case(case: dict):
    ledger, key, as_of, window_sessions = build_case(case)
    call = case.get("call", {})
    status = call.get("security_status")
    result = trailing_adv(ledger, key, as_of, window_sessions,
                          allow_partial_adv=call.get("allow_partial_adv", False),
                          security_status=status)
    return ledger, key, as_of, window_sessions, status, result


@pytest.mark.parametrize("case_id", tuple(CASES))
def test_absence_truth_table_matches_hand_derived_row(case_id):
    case = CASES[case_id]
    expected = case["expected"]
    ledger, key, as_of, window_sessions, status, result = run_case(case)
    execution = spec_for(window_sessions)

    assert result.total == decimal_or_none(expected["total"]), case_id
    assert result.exact == fraction_or_none(expected["exact"]), case_id
    assert result.value == decimal_or_none(expected["value"]), case_id
    assert result.traded_sessions == expected["traded_sessions"], case_id
    assert result.observed_sessions == expected["observed_sessions"], case_id
    assert result.missing_sessions == expected["missing_sessions"], case_id
    assert result.zero_volume_sessions == expected["zero_volume_sessions"], case_id
    assert result.invalid_sessions == expected["invalid_sessions"], case_id
    assert result.unknown_sessions == expected["unknown_sessions"], case_id
    assert result.coverage == Fraction(expected["coverage"]), case_id
    if "reason" in expected:
        assert result.reason == expected["reason"], case_id
    else:
        assert result.reason.startswith(expected["reason_prefix"]), case_id
    assert (result.exact is None) == (result.total is None), case_id

    assert capacity_notional(ledger, key, as_of, execution,
                             security_status=status) == decimal_or_none(
                                 expected["capacity_default"]), case_id
    assert capacity_notional(ledger, key, as_of, execution, allow_partial_adv=True,
                             security_status=status) == decimal_or_none(
                                 expected["capacity_optin"]), case_id

    # The opt-in is a caller policy for capacity only; it cannot rewrite the record.
    again = trailing_adv(ledger, key, as_of, window_sessions,
                         allow_partial_adv=case.get("call", {}).get("allow_partial_adv", False),
                         security_status=status)
    assert again == result, case_id


def test_golden_declares_every_d09_row_with_a_case():
    rows = RECORDS["d09_rows"]
    assert RECORDS["schema_version"] == 1
    assert "hand_derived" in RECORDS["status"]
    assert "D-09" in RECORDS["derivation"]
    assert "PENDING" in RECORDS["decision"]
    assert [row["row"] for row in rows] == list(range(1, 10))
    referenced = set()
    for row in rows:
        assert row["case_ids"], row["row"]
        for case_id in row["case_ids"]:
            assert case_id in CASES, case_id
            referenced.add(case_id)
    assert referenced == set(CASES)


def test_golden_values_are_hand_derived_arithmetic_not_production_output():
    for case_id, case in CASES.items():
        expected = case["expected"]
        window = case.get("window_sessions", DEFAULT_WINDOW)
        total = decimal_or_none(expected["total"])
        exact = fraction_or_none(expected["exact"])
        value = decimal_or_none(expected["value"])
        coverage = Fraction(expected["coverage"])
        if total is None:
            assert exact is None and value is None, case_id
            assert expected["capacity_default"] is None, case_id
            seen_before_window = (case.get("preload_sessions", 0) > 0
                                  or bool(case.get("bars"))
                                  or case.get("all_bars") is not None)
            if case.get("call", {}).get("security_status") == "listed" and seen_before_window:
                # Verified-listed absence is a zero under the explicit opt-in;
                # a never-seen key remains unknown even with that assertion.
                assert expected["capacity_optin"] == "0.00", case_id
            else:
                assert expected["capacity_optin"] is None, case_id
            continue
        assert exact == Fraction(total) / window, case_id
        assert value == oracle_floor_cents(exact), case_id
        assert expected["capacity_default"] == (
            None if coverage < 1
            else format(oracle_floor_cents(exact * Fraction(RATE)), ".2f")
        ), case_id
        assert expected["capacity_optin"] == format(
            oracle_floor_cents(exact * Fraction(RATE)), ".2f"), case_id


def test_never_seen_key_capacity_is_none_even_with_verified_listing_opt_in():
    stream = SessionStream()
    ledger = ObservationLedger(stream)
    sessions = [START + timedelta(days=i) for i in range(DEFAULT_WINDOW)]
    for session in sessions:
        stream.observe(session)
        ledger.ingest(session, [])
    execution = spec_for(DEFAULT_WINDOW)
    assert capacity_notional(ledger, "NEVER", sessions[-1], execution) is None
    assert capacity_notional(ledger, "NEVER", sessions[-1], execution,
                             allow_partial_adv=True, security_status="listed") is None
    result = trailing_adv(ledger, "NEVER", sessions[-1], DEFAULT_WINDOW,
                          allow_partial_adv=True, security_status="listed")
    assert (result.exact, result.value, result.reason) == (None, None, "no_observations")


def test_missing_and_zero_bars_are_distinct_in_every_combination():
    def run(rows):
        stream = SessionStream()
        ledger = ObservationLedger(stream)
        sessions = [START + timedelta(days=i) for i in range(DEFAULT_WINDOW)]
        for session, row in zip(sessions, rows):
            stream.observe(session)
            ledger.ingest(session, [] if row is None else [make_bar("K", session, row)])
        return trailing_adv(ledger, "K", sessions[-1], DEFAULT_WINDOW)

    full_zero = run([{"traded_value": "0", "trades": 0}] * DEFAULT_WINDOW)
    partial_zero = run([None] * (DEFAULT_WINDOW - 1) + [{"traded_value": "0", "trades": 0}])
    full_missing = run([None] * DEFAULT_WINDOW)

    assert (full_zero.exact, full_zero.traded_sessions, full_zero.coverage) == (
        Fraction(0), DEFAULT_WINDOW, Fraction(1))
    assert (partial_zero.exact, partial_zero.traded_sessions, partial_zero.coverage) == (
        Fraction(0), 1, Fraction(1, DEFAULT_WINDOW))
    assert (partial_zero.reason, partial_zero.zero_volume_sessions) == (
        "partial_coverage", 1)
    assert full_missing.exact is None and full_missing.value is None
    assert full_missing.reason == "no_observations"
