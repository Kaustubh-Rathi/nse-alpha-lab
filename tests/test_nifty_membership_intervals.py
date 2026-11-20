"""Point-in-time membership intervals: absence is known only inside complete windows."""
from datetime import date

import pytest

from nse_pipeline.backtest.accounting_types import EventTime, Phase
from nse_pipeline.backtest.evidence import ObservationLedger
from nse_pipeline.backtest.observations import Bar, SessionStream
from nse_pipeline.backtest.universe import NOT_A_MEMBER_AS_OF_REASON, build_membership
from nse_pipeline.nifty.membership_intervals import (
    CHANGE_NOTICE_EVIDENCE, SNAPSHOT_EVIDENCE, IntervalMembershipEvidence,
    MembershipCoverageWindow, MembershipInterval,
)
from nse_pipeline.nifty.membership_port import CoverageUnknown
from nse_pipeline.specs import UniverseSpec

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
DECISION = EventTime(date(2024, 12, 31), Phase.CLOSE_VALUATION)


def interval(symbol, first, last, available=date(2024, 1, 2),
             kind=SNAPSHOT_EVIDENCE, digest=DIGEST_A, citation="NSE/snapshot/2024-01"):
    return MembershipInterval(symbol, first, last,
                              EventTime(available, Phase.PRE_OPEN_EVIDENCE),
                              citation, digest, kind)


def window(first, last, available, citation="NSE/reconstruction/2024", digest=DIGEST_C):
    return MembershipCoverageWindow(first, last,
                                    EventTime(available, Phase.CLOSE_VALUATION),
                                    citation, digest)


W1 = window(date(2024, 1, 1), date(2024, 6, 30), date(2024, 6, 28))
W2 = window(date(2024, 7, 1), date(2024, 12, 31), date(2024, 12, 31))
AAA = interval("AAA", date(2024, 1, 1), date(2024, 6, 30))
BBB = interval("BBB", date(2024, 7, 1), date(2024, 12, 31), available=date(2024, 7, 1),
               kind=CHANGE_NOTICE_EVIDENCE, digest=DIGEST_B, citation="NSE/change/2024-07")


def evidence(intervals=(AAA, BBB), windows=(W1, W2), mapping=None, decision=DECISION):
    return IntervalMembershipEvidence(
        intervals, windows, mapping or {"AAA": "AAA", "BBB": "BBB", "CCC": "CCC"},
        decision_at=decision)


def test_membership_is_known_inside_complete_windows_and_absence_is_false():
    evidence_port = evidence()
    assert evidence_port.is_member("AAA", date(2024, 3, 1)) is True
    assert evidence_port.is_member("BBB", date(2024, 3, 1)) is False
    assert evidence_port.is_member("BBB", date(2024, 8, 1)) is True
    assert evidence_port.is_member("AAA", date(2024, 8, 1)) is False
    assert evidence_port.is_member("CCC", date(2024, 8, 1)) is False


def test_outside_every_window_is_unknown_never_false():
    evidence_port = evidence()
    for day in (date(2023, 12, 31), date(2025, 1, 1)):
        with pytest.raises(CoverageUnknown):
            evidence_port.is_member("AAA", day)
    with pytest.raises(CoverageUnknown):
        evidence_port.is_member("AAA", date(2025, 1, 1))


def test_windows_and_intervals_become_visible_only_when_available():
    before_july = EventTime(date(2024, 6, 28), Phase.CLOSE_VALUATION)
    evidence_port = evidence(decision=before_july)
    assert evidence_port.is_member("AAA", date(2024, 3, 1)) is True
    with pytest.raises(CoverageUnknown):
        evidence_port.is_member("BBB", date(2024, 8, 1))
    with pytest.raises(CoverageUnknown):
        evidence_port.is_member("AAA", date(2024, 8, 1))


def test_a_spanning_interval_is_covered_by_contiguous_windows():
    spanning = interval("AAA", date(2024, 1, 1), date(2024, 12, 31))
    evidence_port = evidence(intervals=(spanning,), mapping={"AAA": "AAA"})
    assert evidence_port.is_member("AAA", date(2024, 3, 1)) is True
    assert evidence_port.is_member("AAA", date(2024, 8, 1)) is True


def test_an_interval_must_be_covered_by_an_admissible_complete_window():
    with pytest.raises(ValueError, match="not covered by a complete window"):
        evidence(intervals=(interval("AAA", date(2024, 1, 1), date(2024, 7, 15)),),
                 windows=(W1,), mapping={"AAA": "AAA"})
    with pytest.raises(ValueError, match="not covered by a complete window"):
        evidence(intervals=(interval("AAA", date(2023, 12, 1), date(2024, 1, 15)),),
                 mapping={"AAA": "AAA"})


def test_an_interval_cannot_become_available_after_the_window_certifying_it():
    late = interval("AAA", date(2024, 1, 1), date(2024, 6, 30), available=date(2024, 7, 1))
    with pytest.raises(ValueError, match="cannot become available after"):
        evidence(intervals=(late,), mapping={"AAA": "AAA"})


def test_overlapping_intervals_for_one_symbol_are_refused():
    early = interval("AAA", date(2024, 1, 1), date(2024, 4, 30))
    late = interval("AAA", date(2024, 4, 1), date(2024, 6, 30), digest=DIGEST_B,
                    citation="NSE/change/2024-04")
    with pytest.raises(ValueError, match="overlapping membership intervals"):
        evidence(intervals=(early, late), mapping={"AAA": "AAA"})


def test_overlapping_coverage_windows_are_refused():
    with pytest.raises(ValueError, match="coverage windows overlap"):
        evidence(windows=(W1, window(date(2024, 6, 30), date(2024, 12, 31), date(2024, 12, 31))))


def test_key_mapping_is_exact_and_bijective():
    with pytest.raises(ValueError, match="collision"):
        evidence(mapping={"K1": "AAA", "K2": "AAA"})
    evidence_port = evidence()
    with pytest.raises(ValueError, match="exact key"):
        evidence_port.is_member("NOT_MAPPED", date(2024, 3, 1))


def test_require_coverage_is_fail_closed_on_the_requested_range():
    evidence_port = evidence()
    assert evidence_port.require_coverage(date(2024, 2, 1), date(2024, 3, 1)) == W1
    with pytest.raises(CoverageUnknown):
        evidence_port.require_coverage(date(2024, 6, 1), date(2024, 7, 2))


def test_future_intervals_are_invisible_and_do_not_change_an_earlier_answer():
    future = interval("AAA", date(2024, 7, 1), date(2024, 12, 31), available=date(2025, 1, 2),
                      digest=DIGEST_B, citation="NSE/change/2025-01")
    without = evidence(intervals=(AAA,), mapping={"AAA": "AAA"})
    with_future = evidence(intervals=(AAA, future), mapping={"AAA": "AAA"})
    for day in (date(2024, 3, 1), date(2024, 8, 1)):
        assert without.is_member("AAA", day) == with_future.is_member("AAA", day)


def ledger(on, *keys):
    stream = SessionStream()
    observations = ObservationLedger(stream)
    stream.observe(on)
    observations.ingest(on, [
        Bar(key, key, "EQ", on, 100, 100, 100, 100, 10000, True,
            "INE000000001", 1000, 10) for key in keys])
    return observations


def test_interval_port_composes_with_the_public_official_membership_builder():
    day = date(2024, 3, 1)
    result = build_membership(ledger(day, "AAA", "BBB"), day,
                              UniverseSpec(min_listed_sessions=1),
                              membership_evidence=evidence())
    assert result.kind == "official_membership"
    assert result.official is True
    assert result.eligible == ("AAA",)
    assert result.reasons("BBB") == (NOT_A_MEMBER_AS_OF_REASON,)


def test_uncovered_universe_date_fails_closed_instead_of_excluding_members():
    day = date(2023, 12, 29)
    with pytest.raises(CoverageUnknown):
        build_membership(ledger(day, "AAA"), day, UniverseSpec(min_listed_sessions=1),
                         membership_evidence=evidence())
