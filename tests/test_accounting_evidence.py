"""P0-EVIDENCE: session-based counts, left-censoring and ticker-reuse isolation by stable key."""
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from nse_pipeline.backtest.evidence import KeyHistory, ObservationLedger, SymbolSpan
from nse_pipeline.backtest.observations import Bar, SessionStream

# Irregular calendar gaps prove that counts are in observed sessions, not days.
DAYS = (date(2024, 1, 1), date(2024, 1, 2), date(2024, 2, 15), date(2024, 2, 16),
        date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5), date(2024, 12, 31))


def bar(key, i, valid=True, symbol=None, close="10"):
    c = D(close) if valid else None
    return Bar(key, symbol or key, "EQ", DAYS[i], c, c, c, c, 100 if valid else 0, valid,
               "INE000000001", D("1000") if valid else None, 5)


def build(frame):
    stream = SessionStream()
    ledger = ObservationLedger(stream)
    for i, bars in enumerate(frame):
        stream.observe(DAYS[i])
        ledger.ingest(DAYS[i], bars)
    return ledger


FRAME = [
    [bar("A", 0), bar("C", 0)],
    [bar("A", 1)],
    [bar("A", 2, valid=False), bar("B", 2)],
    [bar("A", 3), bar("B", 3)],
    [bar("A", 4)],
]


def test_counts_absence_and_censoring_are_session_based():
    ledger = build(FRAME)
    a = ledger.history("A", DAYS[4])
    assert a == KeyHistory("A", DAYS[4], DAYS[0], DAYS[4], bar("A", 4), 5, 4, 5, 0, True)
    assert a.counts_are_lower_bounds
    b = ledger.history("B", DAYS[4])
    assert (b.first_session, b.valid_observations, b.sessions_since_first, b.absence_sessions) == (DAYS[2], 2, 3, 1)
    assert not b.left_censored and not b.counts_are_lower_bounds
    c = ledger.history("C", DAYS[4])
    assert (c.valid_observations, c.sessions_since_first, c.absence_sessions, c.left_censored) == (1, 5, 4, True)
    assert ledger.history("A", DAYS[2]).last_valid_bar == bar("A", 1)
    assert ledger.valid_count("A", DAYS[2]) == 2
    unseen = ledger.history("B", DAYS[1])
    assert (unseen.first_session, unseen.bar_observations, unseen.sessions_since_first,
            unseen.absence_sessions, unseen.left_censored) == (None, 0, 0, None, False)
    assert ledger.keys_as_of(DAYS[1]) == ("A", "C") and ledger.keys_as_of(DAYS[4]) == ("A", "B", "C")
    assert ledger.window(DAYS[4], 3) == DAYS[2:5] and ledger.window(DAYS[1], 3) is None


def test_reused_ticker_does_not_inherit_history_but_rename_keeps_it():
    ledger = build([
        [bar("K1", 0, symbol="XYZ"), bar("K3", 0, symbol="OLD")],
        [bar("K1", 1, symbol="XYZ"), bar("K3", 1, symbol="OLD")],
        [bar("K1", 2, symbol="XYZ"), bar("K3", 2, symbol="NEW")],
        [bar("K3", 3, symbol="NEW")],
        [bar("K2", 4, symbol="XYZ"), bar("K3", 4, symbol="NEW")],
        [bar("K2", 5, symbol="XYZ")],
    ])
    k2 = ledger.history("K2", DAYS[5])
    assert (k2.valid_observations, k2.sessions_since_first, k2.left_censored) == (2, 2, False)
    assert ledger.history("K1", DAYS[5]).valid_observations == 3
    assert ledger.history("K3", DAYS[4]).valid_observations == 5
    assert ledger.symbol_history("XYZ", DAYS[5]) == (
        SymbolSpan("XYZ", "K1", DAYS[0], DAYS[2]), SymbolSpan("XYZ", "K2", DAYS[4], DAYS[5]))
    assert ledger.symbol_history("XYZ", DAYS[1]) == (SymbolSpan("XYZ", "K1", DAYS[0], DAYS[1]),)
    assert ledger.symbol_history("NEW", DAYS[1]) == ()


def test_malformed_ingestion_is_rejected_atomically():
    stream = SessionStream(DAYS[:3])
    ledger = ObservationLedger(stream)
    ledger.ingest(DAYS[0], [bar("A", 0)])
    before = (ledger.sessions, ledger.history("A", DAYS[0]), ledger.keys_as_of(DAYS[0]))
    for session, bars in (
            (DAYS[1], [bar("A", 1), bar("A", 1)]),
            (DAYS[1], [bar("B", 1), bar("A", 2)]),
            (DAYS[2], [bar("A", 2)]),
            (DAYS[0], [bar("A", 0)]),
            (DAYS[3], [bar("A", 3)]),
            (DAYS[1], [bar("A", 1), "not a bar"]),
            (DAYS[1], bar("A", 1)),
            ("2024-01-02", [])):
        with pytest.raises(ValueError):
            ledger.ingest(session, bars)
        assert (ledger.sessions, ledger.history("A", DAYS[0]), ledger.keys_as_of(DAYS[0])) == before
    with pytest.raises(ValueError):
        ledger.history("A", DAYS[1])
    with pytest.raises(ValueError):
        ObservationLedger([DAYS[0]])
    assert ledger.ingest(DAYS[1], []) == 1 and ledger.history("A", DAYS[1]).absence_sessions == 1


def test_window_valid_count_counts_only_the_closed_window():
    ledger = build(FRAME)
    assert ledger.valid_count("A", DAYS[4]) == 4
    assert ledger.window_valid_count("A", DAYS[4], 3) == 2  # indices 2(valid=False),3,4
    assert ledger.window_valid_count("A", DAYS[4], 5) == 4
    assert ledger.window_valid_count("A", DAYS[4], 1) == 1
    assert ledger.window_valid_count("B", DAYS[4], 3) == 2  # first seen at index 2, absent at 4
    assert ledger.window_valid_count("Z", DAYS[4], 3) == 0  # never seen is a zero count, not a bar
    for sessions in (0, -1, True, 6):
        with pytest.raises(ValueError):
            ledger.window_valid_count("A", DAYS[4], sessions)
    with pytest.raises(ValueError):
        ledger.window_valid_count("A", DAYS[4], 3.0)


def test_window_counts_are_append_invariant():
    ledger = build(FRAME[:5])
    keys = ("A", "B", "C", "Z")
    before = {(key, i): (ledger.valid_count(key, DAYS[i]), ledger.window_valid_count(key, DAYS[i], 3))
              for key in keys for i in range(2, 5)}
    for i in range(5, 8):
        ledger.stream.observe(DAYS[i])
        ledger.ingest(DAYS[i], [bar("A", i, valid=False), bar("Z", i), bar("B", i, symbol="XYZ")])
    assert {(key, i): (ledger.valid_count(key, DAYS[i]), ledger.window_valid_count(key, DAYS[i], 3))
            for key in keys for i in range(2, 5)} == before


def test_appending_future_sessions_never_changes_earlier_answers():
    ledger = build(FRAME[:3])
    keys = ("A", "B", "C", "Z")
    snapshot = [ledger.history(k, DAYS[i]) for k in keys for i in range(3)]
    windows = [ledger.window(DAYS[i], 2) for i in range(3)]
    for i in range(3, 8):
        ledger.stream.observe(DAYS[i])
        ledger.ingest(DAYS[i], [bar("A", i, valid=False), bar("Z", i), bar("B", i, symbol="XYZ")])
    assert [ledger.history(k, DAYS[i]) for k in keys for i in range(3)] == snapshot
    assert [ledger.window(DAYS[i], 2) for i in range(3)] == windows
    assert ledger.symbol_history("XYZ", DAYS[2]) == ()
    assert ledger.keys_as_of(DAYS[2]) == ("A", "B", "C")
