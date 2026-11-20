"""T12a/G9/G22: trailing as-of ADV over observed sessions and conservative paise-down capacity."""
from datetime import date, timedelta
from decimal import Decimal as D
from fractions import Fraction

import pytest

from nse_pipeline.backtest.adv import TrailingMean, capacity_notional, trailing_adv, trailing_mean
from nse_pipeline.backtest.evidence import ObservationLedger
from nse_pipeline.backtest.observations import Bar, SessionStream
from nse_pipeline.specs import ExecutionSpec

DAYS = tuple(date(2024, 3, 1) + timedelta(days=3 * i) for i in range(24))


def bar(key, i, tv="100", valid=True, trades=10, volume=2):
    c = D("50") if valid else None
    return Bar(key, key, "EQ", DAYS[i], c, c, c, c, volume, valid, "INE000000001",
               None if tv is None else D(tv), trades)


def build(frame):
    stream = SessionStream()
    ledger = ObservationLedger(stream)
    for i, bars in enumerate(frame):
        stream.observe(DAYS[i])
        ledger.ingest(DAYS[i], bars)
    return ledger


def spec(rate=0.05, window=3):
    return ExecutionSpec(trade_settlement_lag_sessions=1, sizing_participation_rate=rate,
                         max_participation_rate=max(rate, 0.05), adv_window_sessions=window)


FRAME = [
    [bar("A", 0, "999"), bar("B", 0, "5"), bar("Z", 0, valid=False)],
    [bar("A", 1, "100"), bar("B", 1, None)],
    [bar("A", 2, "200"), bar("C", 2, "10")],
    [bar("A", 3, "300"), bar("C", 3, "10"), bar("B", 3, "1")],
    [bar("A", 4, "13.37"), bar("C", 4, "13.37"), bar("B", 4, "1")],
]


def test_window_ends_at_t_inclusive_and_missing_bars_make_partial_coverage():
    ledger = build(FRAME)
    a = trailing_adv(ledger, "A", DAYS[3], 3)
    assert a == TrailingMean("A", DAYS[3], "traded_value", 3, D("600"), 3)
    assert (a.exact, a.value) == (Fraction(200), D("200.00"))
    assert (a.reason, a.coverage, a.partial) == ("", Fraction(1), False)
    c = trailing_adv(ledger, "C", DAYS[3], 3)
    assert (c.total, c.traded_sessions, c.exact) == (D("20"), 2, Fraction(20, 3)) and c.value == D("6.66")
    assert (c.reason, c.coverage, c.partial) == ("partial_coverage", Fraction(2, 3), True)
    never = trailing_adv(ledger, "NEVER", DAYS[4], 2)
    assert (never.total, never.exact, never.value, never.reason) == (
        None, None, None, "no_observations")
    assert trailing_mean(ledger, "A", DAYS[4], 2, "trades").exact == 10


def test_insufficient_sessions_invalid_or_unknown_value_fail_closed():
    ledger = build(FRAME)
    short = trailing_adv(ledger, "A", DAYS[1], 3)
    assert (short.total, short.exact, short.value, short.reason) == (None, None, None, "insufficient_sessions")
    assert trailing_adv(ledger, "B", DAYS[3], 3).reason == f"traded_value_unknown:{DAYS[1]}"
    assert trailing_adv(ledger, "B", DAYS[4], 3).exact == Fraction(2, 3)
    assert trailing_adv(ledger, "Z", DAYS[2], 3).reason == f"invalid_bar:{DAYS[0]}"
    assert trailing_adv(ledger, "Z", DAYS[3], 3).exact is None
    assert capacity_notional(ledger, "B", DAYS[3], spec()) is None
    assert capacity_notional(ledger, "A", DAYS[1], spec()) is None


def test_capacity_is_exact_and_rounded_down_to_paise():
    ledger = build(FRAME)
    # mean (10 + 10 + 13.37) / 3 = 11.1233.. ; x 0.05 = 0.55616.. -> 0.55
    assert capacity_notional(ledger, "C", DAYS[4], spec()) == D("0.55")
    # binary floats give 200 * 0.29 = 57.99999999999999; the exact answer is 58.00
    assert capacity_notional(ledger, "A", DAYS[3], spec(rate=0.29)) == D("58.00")
    assert str(capacity_notional(ledger, "A", DAYS[3], spec(rate=0.0))) == "0.00"


def test_never_seen_key_has_unknown_adv():
    ledger = build([[bar("A", i)] for i in range(20)])
    result = trailing_adv(ledger, "NEVER", DAYS[19], 20)
    assert (result.total, result.exact, result.value) == (None, None, None)
    assert (result.traded_sessions, result.observed_sessions, result.missing_sessions) == (0, 0, 20)
    assert result.coverage == 0 and result.reason == "no_observations"
    assert capacity_notional(ledger, "NEVER", DAYS[19], spec(window=20)) is None
    assert capacity_notional(ledger, "NEVER", DAYS[19], spec(window=20),
                             allow_partial_adv=True, security_status="listed") is None


def test_explicit_zero_volume_is_known_zero():
    ledger = build([[bar("ZERO", i, "0", trades=0, volume=0)] for i in range(20)])
    result = trailing_adv(ledger, "ZERO", DAYS[19], 20)
    assert result.exact == 0 and result.exact is not None
    assert result.value == D("0.00")
    assert (result.traded_sessions, result.zero_volume_sessions) == (20, 20)
    assert (result.reason, result.coverage) == ("", Fraction(1))
    assert capacity_notional(ledger, "ZERO", DAYS[19], spec(window=20)) == D("0.00")


def test_partial_adv_coverage_is_not_a_full_window():
    ledger = build([[] for _ in range(19)] + [[bar("PART", 19, "1000")]])
    result = trailing_adv(ledger, "PART", DAYS[19], 20)
    assert result.total == D("1000")
    assert result.value == D("50.00")
    assert (result.traded_sessions, result.observed_sessions, result.missing_sessions) == (1, 1, 19)
    assert result.coverage == Fraction(1, 20)
    assert (result.reason, result.partial) == ("partial_coverage", True)
    assert capacity_notional(ledger, "PART", DAYS[19], spec(window=20)) is None
    assert capacity_notional(ledger, "PART", DAYS[19], spec(window=20),
                             allow_partial_adv=True) == D("2.50")
    after = trailing_adv(ledger, "PART", DAYS[19], 20)
    assert (after.coverage, after.reason, after.partial) == (
        Fraction(1, 20), "partial_coverage", True)
    assert after == result


def test_capacity_is_none_below_minimum_observed_coverage():
    ledger = build([[bar("NINETEEN", i)] for i in range(19)] + [[]])
    result = trailing_adv(ledger, "NINETEEN", DAYS[19], 20)
    assert (result.coverage, result.reason, result.value) == (
        Fraction(19, 20), "partial_coverage", D("95.00"))
    assert capacity_notional(ledger, "NINETEEN", DAYS[19], spec(window=20)) is None
    assert capacity_notional(ledger, "NINETEEN", DAYS[19], spec(window=20),
                             min_coverage=Fraction(19, 20)) == D("4.75")
    assert capacity_notional(ledger, "NINETEEN", DAYS[19], spec(window=20),
                             allow_partial_adv=True) == D("4.75")


def test_missing_bar_and_explicit_zero_bar_are_distinguished():
    full_zero = trailing_adv(build([[bar("K", i, "0", trades=0)] for i in range(20)]),
                             "K", DAYS[19], 20)
    partial_zero = trailing_adv(
        build([[] for _ in range(19)] + [[bar("K", 19, "0", trades=0)]]), "K", DAYS[19], 20)
    never_seen = trailing_adv(build([[bar("OTHER", i)] for i in range(20)]),
                              "K", DAYS[19], 20)
    assert (full_zero.exact, full_zero.traded_sessions, full_zero.coverage) == (
        Fraction(0), 20, Fraction(1))
    assert (partial_zero.exact, partial_zero.traded_sessions, partial_zero.coverage) == (
        Fraction(0), 1, Fraction(1, 20))
    assert never_seen.exact is None
    assert full_zero.exact is not None and partial_zero.exact is not None
    assert len({(full_zero.exact, full_zero.traded_sessions, full_zero.coverage),
                (partial_zero.exact, partial_zero.traded_sessions, partial_zero.coverage),
                (never_seen.exact, never_seen.traded_sessions, never_seen.coverage)}) == 3


def test_appending_later_sessions_never_changes_an_earlier_result():
    ledger = build(FRAME[:4])
    keys, ends = ("A", "B", "C", "Z"), DAYS[:4]
    before = [(trailing_adv(ledger, k, t, 2), capacity_notional(ledger, k, t, spec(window=2)))
              for k in keys for t in ends]
    for i in range(4, 12):
        ledger.stream.observe(DAYS[i])
        ledger.ingest(DAYS[i], [bar("A", i, "1000000"), bar("B", i, None), bar("C", i, valid=False)])
    assert [(trailing_adv(ledger, k, t, 2), capacity_notional(ledger, k, t, spec(window=2)))
            for k in keys for t in ends] == before


def test_append_invariance_includes_absence_diagnostics_and_coverage():
    ledger = build(FRAME[:4])
    keys, ends = ("A", "B", "C", "Z", "NEVER"), DAYS[:4]

    def snapshot():
        rows = []
        for key in keys:
            for as_of in ends:
                result = trailing_adv(ledger, key, as_of, 2)
                rows.append((result.total, result.exact, result.value, result.traded_sessions,
                             result.reason, result.observed_sessions, result.missing_sessions,
                             result.zero_volume_sessions, result.invalid_sessions,
                             result.unknown_sessions, result.coverage,
                             capacity_notional(ledger, key, as_of, spec(window=2))))
        return rows

    before = snapshot()
    for i in range(4, 20):
        ledger.stream.observe(DAYS[i])
        ledger.ingest(DAYS[i], [bar("A", i, "1000000"), bar("B", i, None),
                                bar("C", i, valid=False), bar("NEVER", i, "50")])
    assert snapshot() == before


@pytest.mark.parametrize("call", (
    lambda l: trailing_adv(l, "A", DAYS[3], 0),
    lambda l: trailing_adv(l, "A", DAYS[3], True),
    lambda l: trailing_adv(l, "", DAYS[3], 2),
    lambda l: trailing_adv(l, "A", DAYS[9], 2),
    lambda l: trailing_mean(l, "A", DAYS[3], 2, "close"),
    lambda l: trailing_adv(None, "A", DAYS[3], 2),
    lambda l: capacity_notional(l, "A", DAYS[3], object()),
    lambda l: trailing_adv(l, "A", DAYS[3], 2, allow_partial_adv=1),
    lambda l: trailing_adv(l, "A", DAYS[3], 2, security_status="bogus"),
    lambda l: capacity_notional(l, "A", DAYS[3], spec(), min_coverage=Fraction(3, 2)),
))
def test_malformed_inputs_raise(call):
    with pytest.raises(ValueError):
        call(build(FRAME))
