"""P0-OBSERVATIONS: bars become known only at CLOSE_VALUATION of their own session; sessions are the only clock."""
from dataclasses import FrozenInstanceError
from datetime import date, datetime
from decimal import Decimal as D

import pytest

from nse_pipeline.backtest.accounting_types import Evidence, EventTime, Phase
from nse_pipeline.backtest.evidence import ObservationLedger
from nse_pipeline.backtest.observations import Bar, SessionStream

# Irregular calendar gaps prove that session indices count observed sessions, not days.
DAYS = (date(2024, 1, 5), date(2024, 1, 8), date(2024, 1, 10))


def bar(key="K", symbol="AAA", series="EQ", session=DAYS[0], open="10", high="11", low="9",
        close="10.5", volume=100, valid=True, isin="INE000000001", traded_value="1000", trades=5,
        bar_quality=None):
    return Bar(key, symbol, series, session, open, high, low, close, volume, valid, isin,
               traded_value, trades, bar_quality=bar_quality)


def test_valid_bar_is_built_from_explicit_session_and_values():
    b = bar()
    assert (b.key, b.symbol, b.series) == ("K", "AAA", "EQ")
    assert b.session == DAYS[0] and b.valid_ohlcv is True
    assert b.volume == 100 and b.trades == 5
    assert b.close == D("10.5") and isinstance(b.close, D)
    assert b.traded_value == D("1000")
    assert b.available_at == EventTime(DAYS[0], Phase.CLOSE_VALUATION)
    optional = bar(open=None, traded_value=None, trades=None, isin=None)
    assert (optional.open, optional.traded_value, optional.trades, optional.isin) == (None, None, None, None)


@pytest.mark.parametrize("build", (
    lambda: bar(key=""),
    lambda: bar(key="   "),
    lambda: bar(key=None),
    lambda: bar(symbol=""),
    lambda: bar(series="  "),
    lambda: bar(session="2024-01-05"),
    lambda: bar(session=datetime(2024, 1, 5)),
    lambda: bar(valid="yes"),
    lambda: bar(valid=1),
    lambda: bar(volume=-1),
    lambda: bar(volume=1.5),
    lambda: bar(volume=True),
    lambda: bar(trades=-1),
    lambda: bar(trades="5"),
    lambda: bar(isin=""),
    lambda: bar(isin=5),
))
def test_bar_rejects_empty_identifiers_and_mistyped_fields(build):
    with pytest.raises(ValueError):
        build()


def test_bar_amounts_are_exact_decimals_not_floats():
    assert bar(open="10.25").open == D("10.25")
    assert bar(close=10).close == D("10")
    with pytest.raises(TypeError):
        bar(close=10.5)
    with pytest.raises(TypeError):
        bar(close=True)
    with pytest.raises(ValueError):
        bar(close=D("-1"))
    with pytest.raises(ValueError):
        bar(close="abc")


def test_valid_bar_requires_close_and_quality_agrees():
    with pytest.raises(ValueError, match=r"^valid_ohlcv requires close$"):
        bar(open=None, high=None, low=None, close=None, valid=True)

    close_only = bar(open=None, high=None, low=None, close="100")
    assert close_only.bar_quality == "close_only"
    assert bar(open=None, high=None, low=None, close="100",
               bar_quality="close_only").bar_quality == "close_only"

    complete = bar(open="100", high="105", low="95", close="102")
    assert complete.bar_quality == "complete"
    assert bar(bar_quality="complete").bar_quality == "complete"

    degraded = bar(open=None, high=None, low=None, close=None, volume=0,
                   traded_value=None, trades=None, valid=False)
    assert degraded.bar_quality == "degraded"
    assert bar(open=None, high=None, low=None, close=None, volume=0, traded_value=None,
               trades=None, valid=False, bar_quality="degraded").bar_quality == "degraded"

    for mismatched in (
        dict(valid=True, bar_quality="degraded"),
        dict(open=None, high=None, low=None, valid=True, bar_quality="complete"),
        dict(valid=False, bar_quality="complete"),
        dict(valid=False, bar_quality="close_only"),
        dict(bar_quality="partial"),
    ):
        with pytest.raises(ValueError, match=r"^bar_quality_mismatch$"):
            bar(**mismatched)


def test_valid_bar_rejects_incoherent_ohlc():
    control = bar(open="100", high="105", low="95", close="102")
    assert control.bar_quality == "complete"

    for overrides, offenders in (
        (dict(high="90", low="100"), ("low", "high")),
        (dict(close="110", high="105"), ("close", "high")),
        (dict(open="90", low="95"), ("open", "low")),
    ):
        values = dict(open="100", high="105", low="95", close="102")
        values.update(overrides)
        with pytest.raises(ValueError) as excinfo:
            bar(**values)
        message = str(excinfo.value)
        assert message.startswith("incoherent_ohlc"), message
        assert all(field in message for field in offenders), message

    with pytest.raises(ValueError, match=r"^negative_price$"):
        bar(open="-1")

    close_only = bar(open=None, high=None, low=None, close="100")
    assert close_only.bar_quality == "close_only"


def test_degraded_bar_is_not_usable_downstream():
    stream = SessionStream((DAYS[0],))
    ledger = ObservationLedger(stream)
    degraded = bar(open=None, high=None, low=None, close=None, volume=0,
                   traded_value=None, trades=None, valid=False)
    assert degraded.bar_quality == "degraded"
    ledger.ingest(DAYS[0], [degraded])
    history = ledger.history("K", DAYS[0])
    assert history.bar_observations == 1
    assert history.valid_observations == 0
    assert history.last_valid_bar is None
    assert ledger.valid_count("K", DAYS[0]) == 0


def test_bar_becomes_known_only_at_close_valuation_of_its_own_session():
    b = bar(session=DAYS[1])
    assert b.available_at == EventTime(DAYS[1], Phase.CLOSE_VALUATION)
    assert bar(session=DAYS[2]).available_at > b.available_at
    assert bar(session=DAYS[0]).available_at < b.available_at
    evidence = Evidence("bhav.csv", "K", DAYS[1], b.available_at, True)
    evidence.require_applicable("K", b.available_at)
    with pytest.raises(ValueError):
        evidence.require_applicable("K", EventTime(DAYS[1], Phase.OPEN_FILLS))
    with pytest.raises(ValueError):
        evidence.require_applicable("K", EventTime(DAYS[0], Phase.CLOSE_VALUATION))
    with pytest.raises(ValueError):
        evidence.require_applicable("OTHER", b.available_at)


def test_session_stream_is_append_only_and_indexes_observed_sessions():
    stream = SessionStream()
    assert stream.observe(DAYS[0]) == 0
    assert stream.observe(DAYS[2]) == 1
    assert stream.sessions == (DAYS[0], DAYS[2])
    assert stream.index(DAYS[0]) == 0 and stream.index(DAYS[2]) == 1
    assert stream.as_of(DAYS[0]) == (DAYS[0],)
    assert stream.as_of(DAYS[2]) == (DAYS[0], DAYS[2])
    for bad in (DAYS[2], DAYS[1], "2024-01-11", datetime(2024, 1, 11)):
        with pytest.raises(ValueError):
            stream.observe(bad)
    assert stream.sessions == (DAYS[0], DAYS[2])
    with pytest.raises(ValueError):
        stream.index(DAYS[1])
    with pytest.raises(ValueError):
        stream.as_of(DAYS[1])
    with pytest.raises(AttributeError):
        stream.sessions = ()


def test_session_stream_constructor_requires_strictly_increasing_sessions():
    assert SessionStream(DAYS).sessions == DAYS
    with pytest.raises(ValueError):
        SessionStream((DAYS[0], DAYS[0]))
    with pytest.raises(ValueError):
        SessionStream((DAYS[2], DAYS[0]))


def test_bar_records_are_frozen():
    b = bar()
    with pytest.raises(FrozenInstanceError):
        b.key = "OTHER"
    with pytest.raises(FrozenInstanceError):
        b.close = D("1")


def test_bar_rejects_a_whitespace_only_isin():
    with pytest.raises(ValueError):
        bar(isin="   ")
