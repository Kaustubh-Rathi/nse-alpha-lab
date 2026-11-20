"""P0-UNIVERSE: spec-driven research eligibility at a session, with a reason for every exclusion."""
from datetime import date
from decimal import Decimal as D

import pytest

from nse_pipeline.backtest.evidence import ObservationLedger
from nse_pipeline.backtest.observations import Bar, SessionStream
from nse_pipeline.backtest.universe import Membership, build_membership, build_universe
from nse_pipeline.specs import UniverseSpec

DAYS = (date(2024, 1, 1), date(2024, 1, 2), date(2024, 2, 15), date(2024, 2, 16),
        date(2024, 6, 3), date(2024, 6, 4))
SESSIONS = DAYS[:5]
UNOBSERVED = date(2025, 1, 1)

OVERRIDES = {
    "ADV_499": {"traded_value": "499"},
    "ADV_AT": {"traded_value": "500"},
    "ADV_LOW": {"traded_value": "100"},
    "ADV_MISSING": {"traded_value": None},
    "AT_PRICE": {"close": "5"},
    "AT_VOL": {"volume": 5000},
    "BE_SER": {"series": "BE"},
    "CHEAP": {"close": "1"},
    "CHEAP_THIN": {"close": "1", "volume": 10},
    "FOREIGN": {"isin": "US0378331005"},
    "NO_ISIN": {"isin": None},
    "NO_PRICE": {"close": None, "valid": False},
    "THIN_VOL": {"volume": 10},
    "TRD_9": {"trades": 9},
    "TRD_AT": {"trades": 10},
    "TRD_LOW": {"trades": 1},
    "TRD_MISSING": {"trades": None},
    "XX_SER": {"series": "XX"},
}

KEYS = ("ADV_499", "ADV_AT", "ADV_LOW", "ADV_MISSING", "ADV_OK", "AT_PRICE", "AT_VOL", "BE_SER",
        "CHEAP", "CHEAP_THIN", "EQ_OK", "FOREIGN", "NO_ISIN", "NO_PRICE", "THIN_VOL", "TRD_9",
        "TRD_AT", "TRD_LOW", "TRD_MISSING", "TRD_OK", "XX_SER")


def bar(key, i, close="100", valid=True, series="EQ", isin="INE000000001", volume=10000,
        traded_value="1000", trades=100, sessions=DAYS):
    c = None if close is None else D(close)
    return Bar(key, key, series, sessions[i], c, c, c, c, volume, valid, isin,
               None if traded_value is None or not valid else D(traded_value),
               None if trades is None or not valid else trades)


def build(rows, sessions=DAYS):
    stream = SessionStream()
    ledger = ObservationLedger(stream)
    for i, bars in enumerate(rows):
        stream.observe(sessions[i])
        ledger.ingest(sessions[i], bars)
    return ledger


def frame(keys=KEYS, sessions=2):
    return [[bar(key, i, **OVERRIDES.get(key, {})) for key in keys] for i in range(sessions)]


def spec(**kw):
    base = dict(series=("EQ",), isin_policy="any", min_price=0.0, min_volume=0,
                min_adv_traded_value=0.0, min_avg_trades=0.0, min_adv_window_sessions=2,
                min_listed_sessions=1, label="test research universe")
    base.update(kw)
    return UniverseSpec(**base)


def test_the_isin_policy_decides_which_keys_may_trade():
    ledger = build(frame(("EQ_OK", "NO_ISIN", "FOREIGN")))
    anything = build_universe(ledger, DAYS[1], spec(isin_policy="any"))
    assert anything.eligible == ("EQ_OK", "FOREIGN", "NO_ISIN")
    assert anything.reasons("FOREIGN") == () and anything.reasons("NO_ISIN") == ()
    ine_or_null = build_universe(ledger, DAYS[1], spec(isin_policy="ine_or_null"))
    assert ine_or_null.eligible == ("EQ_OK", "NO_ISIN")
    assert ine_or_null.reasons("FOREIGN") == ("isin_policy",)
    assert ine_or_null.reasons("NO_ISIN") == ()
    ine_only = build_universe(ledger, DAYS[1], spec(isin_policy="ine_only"))
    assert ine_only.eligible == ("EQ_OK",)
    assert ine_only.reasons("NO_ISIN") == ("isin_policy",)
    assert ine_only.reasons("FOREIGN") == ("isin_policy",)


def test_excluded_and_inactive_series_are_reported_separately():
    ledger = build(frame(("EQ_OK", "BE_SER", "XX_SER")))
    membership = build_universe(ledger, DAYS[1], spec())
    assert membership.eligible == ("EQ_OK",)
    assert membership.reasons("BE_SER") == ("series_excluded",)
    assert membership.reasons("XX_SER") == ("series_not_active",)
    relaxed = build_universe(ledger, DAYS[1], spec(series=("EQ", "XX")))
    assert relaxed.eligible == ("EQ_OK", "XX_SER")
    assert relaxed.reasons("BE_SER") == ("series_excluded",)


def test_the_price_and_volume_floors_are_inclusive():
    ledger = build(frame(("EQ_OK", "AT_PRICE", "AT_VOL", "CHEAP", "CHEAP_THIN", "NO_PRICE", "THIN_VOL")))
    membership = build_universe(ledger, DAYS[1], spec(min_price=5.0, min_volume=5000))
    assert membership.eligible == ("AT_PRICE", "AT_VOL", "EQ_OK")
    assert membership.reasons("CHEAP") == ("below_min_price",)
    assert membership.reasons("CHEAP_THIN") == ("below_min_price", "below_min_volume")
    assert membership.reasons("NO_PRICE") == ("invalid_bar",)
    assert membership.reasons("THIN_VOL") == ("below_min_volume",)
    assert membership.reasons("EQ_OK") == ()


def test_liquidity_thresholds_report_below_and_unknown():
    ledger = build(frame())
    membership = build_universe(ledger, DAYS[1], spec(min_adv_traded_value=500.0, min_avg_trades=10.0))
    assert membership.reasons("ADV_LOW") == ("below_min_adv_traded_value",)
    assert membership.reasons("ADV_499") == ("below_min_adv_traded_value",)
    assert membership.reasons("ADV_MISSING") == ("adv_traded_value_unknown",)
    assert membership.reasons("TRD_LOW") == ("below_min_avg_trades",)
    assert membership.reasons("TRD_9") == ("below_min_avg_trades",)
    assert membership.reasons("TRD_MISSING") == ("avg_trades_unknown",)
    assert membership.reasons("ADV_OK") == ()
    assert membership.reasons("ADV_AT") == ()
    assert membership.reasons("TRD_OK") == ()
    assert membership.reasons("TRD_AT") == ()


def test_a_zero_threshold_disables_the_liquidity_filters():
    ledger = build(frame())
    membership = build_universe(ledger, DAYS[1], spec())
    assert membership.reasons("ADV_MISSING") == ()
    assert membership.reasons("TRD_MISSING") == ()
    assert "ADV_MISSING" in membership.eligible and "TRD_MISSING" in membership.eligible


def test_unknown_liquidity_is_reported_and_never_estimated():
    ledger = build(frame())
    short = build_universe(ledger, DAYS[0], spec(min_adv_traded_value=500.0))
    assert short.reasons("EQ_OK") == ("adv_traded_value_unknown",)
    broken = build([[bar("EQ_OK", 0, valid=False)], [bar("EQ_OK", 1)]])
    invalid = build_universe(broken, DAYS[1], spec(min_adv_traded_value=500.0))
    assert invalid.reasons("EQ_OK") == ("adv_traded_value_unknown",)


def test_listed_age_counts_observed_sessions_and_censoring_is_disclosed():
    ledger = build([[bar("CENSORED", 0)], [bar("CENSORED", 1), bar("LATE", 1)]])
    too_young = build_universe(ledger, DAYS[1], spec(min_listed_sessions=3))
    assert too_young.eligible == ()
    assert too_young.reasons("CENSORED") == ("listed_sessions_censored",)
    assert too_young.reasons("LATE") == ("below_min_listed_sessions",)
    old = build_universe(ledger, DAYS[1], spec(min_listed_sessions=2))
    assert old.eligible == ("CENSORED",)
    assert old.censored_lower_bound == ("CENSORED",)
    assert old.reasons("LATE") == ("below_min_listed_sessions",)
    young = build_universe(ledger, DAYS[1], spec(min_listed_sessions=1))
    assert young.eligible == ("CENSORED", "LATE")
    assert young.censored_lower_bound == ("CENSORED",)


def test_a_session_without_a_usable_bar_excludes_the_key():
    ledger = build([[bar("GONE", 0), bar("INVALID", 0), bar("LATE", 0)],
                    [bar("INVALID", 1, valid=False), bar("LATE", 1)]])
    membership = build_universe(ledger, DAYS[1], spec())
    assert membership.eligible == ("LATE",)
    assert membership.reasons("GONE") == ("no_bar_on_session",)
    assert membership.reasons("INVALID") == ("invalid_bar",)


def test_membership_reports_its_as_of_label_and_reasons():
    ledger = build(frame(("EQ_OK", "CHEAP")))
    membership = build_universe(ledger, DAYS[1], spec(min_price=5.0, label="research test universe"))
    assert isinstance(membership, Membership)
    assert membership.as_of == DAYS[1]
    assert membership.kind == "research_eligibility"
    assert membership.label == "RESEARCH_ELIGIBILITY_ONLY"
    assert membership.spec_label == "research test universe"
    assert membership.official is False
    assert membership.reasons("CHEAP") == ("below_min_price",)
    assert membership.reasons("EQ_OK") == ()
    assert membership.reasons("NEVER_SEEN") == ()


def test_appending_later_sessions_never_changes_membership_at_as_of():
    early = build(frame())
    later = build(frame() + [[bar("NEW", 2)], [bar("NEW", 3)]])
    later_at_as_of = build_universe(later, DAYS[1], spec())
    assert later_at_as_of == build_universe(early, DAYS[1], spec())
    assert "NEW" not in later_at_as_of.eligible
    assert "NEW" in build_universe(later, DAYS[2], spec()).eligible
    before = build_universe(early, DAYS[1], spec(min_adv_traded_value=500.0))
    for i in (2, 3):
        early.stream.observe(DAYS[i])
        early.ingest(DAYS[i], [bar("NEW", i, "1")])
    assert build_universe(early, DAYS[1], spec(min_adv_traded_value=500.0)) == before


def test_universe_is_as_of_and_future_blind():
    kwargs = dict(min_price=5.0, min_volume=5000, min_adv_traded_value=500.0,
                  min_avg_trades=10.0, min_adv_window_sessions=2,
                  min_listed_sessions=3, label="causality research universe")
    base_rows = [
        [bar("ALPHA", 0), bar("THIN_EARLY", 0, volume=10),
         bar("CHEAP_EARLY", 0, close="1", traded_value="1000"),
         bar("ADV_EARLY", 0, traded_value="100"), bar("GONE_AT_S3", 0),
         bar("INVALID_AT_S3", 0)],
        [bar("ALPHA", 1), bar("THIN_EARLY", 1, volume=10),
         bar("CHEAP_EARLY", 1, close="1", traded_value="1000"),
         bar("ADV_EARLY", 1, traded_value="100"), bar("GONE_AT_S3", 1),
         bar("INVALID_AT_S3", 1), bar("YOUNG", 1)],
        [bar("ALPHA", 2), bar("THIN_EARLY", 2, volume=10),
         bar("CHEAP_EARLY", 2, close="1", traded_value="1000"),
         bar("ADV_EARLY", 2, traded_value="100"), bar("INVALID_AT_S3", 2, valid=False),
         bar("YOUNG", 2)],
    ]
    expected = {
        "ALPHA": (),
        "THIN_EARLY": ("below_min_volume",),
        "CHEAP_EARLY": ("below_min_price",),
        "ADV_EARLY": ("below_min_adv_traded_value",),
        "GONE_AT_S3": ("no_bar_on_session",),
        "INVALID_AT_S3": ("invalid_bar",),
        "YOUNG": ("below_min_listed_sessions",),
    }
    early = build(base_rows, SESSIONS)
    before = build_universe(early, SESSIONS[2], spec(**kwargs))
    assert before.kind == "research_eligibility"
    assert before.label == "RESEARCH_ELIGIBILITY_ONLY"
    assert before.as_of == SESSIONS[2]
    assert before.eligible == ("ALPHA",)
    for key, reasons in expected.items():
        assert before.reasons(key) == reasons

    future_keys = tuple(expected) + ("FUTURE_CONSTITUENT",)
    future_rows = [
        [bar(key, 3, close="100", volume=100_000, traded_value="1000000")
         for key in future_keys],
        [bar(key, 4, close="100", volume=100_000, traded_value="1000000")
         for key in future_keys],
    ]
    for i in (3, 4):
        early.stream.observe(SESSIONS[i])
        early.ingest(SESSIONS[i], future_rows[i - 3])

    after = build_universe(early, SESSIONS[2], spec(**kwargs))
    assert after == before
    assert after.as_of == SESSIONS[2]
    assert after.eligible == before.eligible == ("ALPHA",)
    assert after.exclusions == before.exclusions
    assert tuple(key for key, _ in after.exclusions) == tuple(
        key for key, _ in before.exclusions)
    for key, reasons in expected.items():
        assert after.reasons(key) == reasons
    assert "FUTURE_CONSTITUENT" not in after.eligible
    assert all(key != "FUTURE_CONSTITUENT" for key, _ in after.exclusions)


def test_screen_is_not_labelled_official_membership():
    rows = [
        [bar("GAPPED", 0), bar("SUSPENDED", 0), bar("DELISTED", 0)],
        [bar("SUSPENDED", 1), bar("DELISTED", 1)],
        [bar("SUSPENDED", 2), bar("DELISTED", 2)],
        [bar("GAPPED", 3)],
    ]
    ledger = build(rows, DAYS[:4])
    research = build_universe(ledger, DAYS[3], spec(label="screen test universe"))

    assert research.kind == "research_eligibility"
    assert research.label == "RESEARCH_ELIGIBILITY_ONLY"
    assert research.spec_label == "screen test universe"
    assert research.official is False
    assert research.as_of == DAYS[3]
    assert "GAPPED" in research.eligible
    assert research.reasons("GAPPED") == ()
    assert "GAPPED" in research.gapped
    assert research.reasons("SUSPENDED") == ("no_bar_on_session",)
    assert research.reasons("DELISTED") == ("no_bar_on_session",)

    with pytest.raises(ValueError, match="unsupported_membership_kind"):
        build_universe(ledger, DAYS[3], spec(), kind="official_membership")


def test_suspended_gapped_and_absent_keys_have_distinct_reasons():
    rows = [
        [bar("SUSPENDED", 0), bar("GAPPED", 0), bar("ABSENT", 0)],
        [bar("SUSPENDED", 1)],
        [bar("SUSPENDED", 2), bar("GAPPED", 2)],
        [bar("SUSPENDED", 3)],
        [],
    ]
    membership = build_universe(build(rows, SESSIONS), SESSIONS[4], spec())

    assert membership.eligible == ()
    assert membership.reasons("SUSPENDED") == ("no_bar_on_session",)
    assert membership.reasons("GAPPED") == ("session_gap",)
    assert membership.reasons("ABSENT") == ("absent_for_multiple_sessions",)
    assert len({membership.reasons(key) for key in ("SUSPENDED", "GAPPED", "ABSENT")}) == 3
    assert membership.gapped == ("GAPPED",)


def test_official_membership_uses_only_explicit_evidence():
    class Evidence:
        def is_member(self, key, as_of):
            return key == "EQ_OK"

    ledger = build(frame(("EQ_OK", "CHEAP")))
    membership = build_membership(ledger, DAYS[1], spec(), membership_evidence=Evidence())

    assert membership.kind == "official_membership"
    assert membership.official is True
    assert membership.eligible == ("EQ_OK",)
    assert membership.reasons("CHEAP") == ("not_a_member_as_of",)


@pytest.mark.parametrize("call", (
    lambda l: build_universe(None, DAYS[1], spec()),
    lambda l: build_universe(l, DAYS[1], object()),
    lambda l: build_universe(l, UNOBSERVED, spec()),
))
def test_malformed_inputs_raise(call):
    with pytest.raises(ValueError):
        call(build(frame()))
