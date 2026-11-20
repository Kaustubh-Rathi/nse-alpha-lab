"""P0-BOUNDARY evidence binding: real Batch 8 calendar data, never weekday inference.

The Batch 8 lane is an evidence artifact, not an owner decision. The acceptance
records created here are synthetic fixtures used only to prove the gate's
mechanism; they are written to a temporary directory and never to the lane.
"""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest

from nse_pipeline.backtest.accounting_types import EventTime, Phase
from nse_pipeline.backtest.boundary import KNOWN_CALENDAR, RebalanceSchedule
from nse_pipeline.nifty.calendar_evidence import (
    CONTEMPORANEOUS_SESSION_SOURCE, CalendarEvidenceError, OwnerAcceptanceInvalid,
    OwnerAcceptanceRequired, build_published_calendar, load_owner_acceptance,
    load_published_calendar_evidence, monthly_calendars, retrospective_calendar,
)

ROOT = Path(__file__).resolve().parents[1]
LANE = ROOT / "verification/stage1/batch8/calendar"
EVIDENCE = LANE / "session-evidence.json"
PROVENANCE = LANE / "provenance.json"
OWNER_ACCEPTANCE = LANE / "owner-acceptance.json"


def load(**kwargs):
    return load_published_calendar_evidence(EVIDENCE, provenance_path=PROVENANCE, **kwargs)


def acceptance_payload(evidence, **overrides):
    payload = {
        "kind": "nse_calendar_owner_acceptance",
        "accepted_by": "synthetic test fixture; not an owner decision",
        "accepted_at": "2026-11-16",
        "evidence_sha256": evidence.file_sha256,
        "session_source": CONTEMPORANEOUS_SESSION_SOURCE,
        "accepts_msd_notices": True,
        "accepts_revised_muhurat_citation": True,
        "coverage_end": "2025-12-31",
    }
    payload.update(overrides)
    return payload


def load_acceptance(tmp_path, evidence, **overrides):
    path = tmp_path / "acceptance.json"
    path.write_text(json.dumps(acceptance_payload(evidence, **overrides)), encoding="utf-8")
    return load_owner_acceptance(path, evidence)


def test_real_batch8_evidence_reconciles_exactly_and_hashes_every_artifact():
    evidence = load()
    assert evidence.file_sha256 == load().file_sha256
    assert len(evidence.file_sha256) == 64
    assert evidence.retrospective_basis == "provenance_retrieval"
    assert evidence.retrospective_at == EventTime(date(2026, 11, 15), Phase.CLOSE_AUDIT)
    assert len(evidence.artifacts) == 49
    assert len(evidence.verify_artifacts()) == 49
    assert evidence.contradictions and all(item.resolved for item in evidence.contradictions)

    calendar = build_published_calendar(evidence)
    assert len(calendar.sessions) == 1_983
    assert len(calendar.closures) == 147
    assert len(calendar.special_sessions) == 13
    assert len(calendar.complete_months) == 96
    assert calendar.coverage_start == date(2018, 1, 1)
    assert calendar.coverage_end == date(2025, 12, 31)
    assert len(calendar.manifest_sha256) == 64
    assert calendar.manifest_sha256 == build_published_calendar(load()).manifest_sha256
    assert all((year, month) in {(on.year, on.month) for on in calendar.sessions}
               for year, month in calendar.complete_months)


def test_retrospective_index_copies_cannot_fire_before_their_retrieval():
    evidence = load()
    calendar = build_published_calendar(evidence)

    with pytest.raises(ValueError, match="was not available at the month-end decision"):
        calendar.to_monthly_calendars()
    with pytest.raises(OwnerAcceptanceRequired, match="owner acceptance"):
        monthly_calendars(evidence, None)

    retrospective = retrospective_calendar(evidence)
    assert retrospective.available_at == EventTime(date(2026, 11, 15), Phase.CLOSE_AUDIT)
    assert retrospective.coverage_end == date(2025, 12, 31)

    schedule = RebalanceSchedule([retrospective])
    for on in calendar.sessions:
        schedule.observe(on)
    assert schedule.decisions
    assert not any(decision.basis == KNOWN_CALENDAR for decision in schedule.decisions)


def test_owner_accepted_real_evidence_fires_once_per_month_at_the_last_session(tmp_path):
    evidence = load()
    acceptance = load_acceptance(tmp_path, evidence)
    months = monthly_calendars(evidence, acceptance)
    calendar = build_published_calendar(evidence, acceptance=acceptance)

    assert len(months) == 96
    expected = []
    for month in calendar.complete_months:
        expected.append((month, max(on for on in calendar.sessions
                                    if (on.year, on.month) == month)))
    assert [(item.complete_months[0], item.coverage_end) for item in months] == expected
    assert all(item.authority == "published_exchange" for item in months)
    assert all(item.available_at == EventTime(item.coverage_end, Phase.CLOSE_ORDERS)
               for item in months)

    schedule = RebalanceSchedule(months)
    for on in calendar.sessions:
        schedule.observe(on)
    signals = [decision for decision in schedule.decisions if decision.signal]
    assert len(signals) == 96
    assert all(decision.basis == KNOWN_CALENDAR and not decision.late for decision in signals)
    assert [(decision.rebalance_month, decision.session) for decision in signals] == expected
    assert schedule.pending_month is None


def test_an_accepted_calendar_never_fires_a_future_month(tmp_path):
    evidence = load()
    acceptance = load_acceptance(tmp_path, evidence)
    calendar = build_published_calendar(evidence, acceptance=acceptance)
    months = [item for item in calendar.to_monthly_calendars()
              if item.coverage_end <= date(2024, 6, 30)]

    schedule = RebalanceSchedule(months)
    for on in calendar.sessions:
        if on > date(2024, 6, 30):
            break
        schedule.observe(on)
    signals = [decision for decision in schedule.decisions if decision.signal]
    assert signals[-1].rebalance_month == (2024, 6)
    assert all(decision.rebalance_month <= (2024, 6) for decision in signals)


def test_the_extension_is_explicit_and_stops_at_the_last_complete_month(tmp_path):
    evidence = load()
    acceptance = load_acceptance(tmp_path, evidence, coverage_end="2026-10-31")
    months = monthly_calendars(evidence, acceptance)
    assert len(months) == 106
    assert months[-1].complete_months == ((2026, 10),)
    assert months[-1].coverage_end == date(2026, 10, 30)
    assert all(item.coverage_end <= date(2026, 10, 31) for item in months)


def test_owner_acceptance_is_bound_to_the_digest_and_every_required_decision(tmp_path):
    evidence = load()
    with pytest.raises(OwnerAcceptanceInvalid, match="digest"):
        load_acceptance(tmp_path, evidence, evidence_sha256="0" * 64)
    with pytest.raises(OwnerAcceptanceInvalid, match="session_source"):
        load_acceptance(tmp_path, evidence, session_source="weekday_minus_holiday")
    with pytest.raises(OwnerAcceptanceInvalid, match="MSD"):
        load_acceptance(tmp_path, evidence, accepts_msd_notices=False)
    with pytest.raises(OwnerAcceptanceInvalid, match="Muhurat"):
        load_acceptance(tmp_path, evidence, accepts_revised_muhurat_citation=False)
    with pytest.raises(OwnerAcceptanceInvalid, match="coverage end"):
        load_acceptance(tmp_path, evidence, coverage_end="2025-06-30")
    with pytest.raises(OwnerAcceptanceInvalid, match="predates"):
        load_acceptance(tmp_path, evidence, accepted_at="2026-11-14")


def test_a_missing_or_tampered_artifact_is_refused(tmp_path):
    evidence = load()
    with pytest.raises(CalendarEvidenceError, match="missing calendar artifact"):
        evidence.verify_artifacts(tmp_path)
    artifact = tmp_path / "raw/index/nifty50/2018.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"not the published index series")
    with pytest.raises(CalendarEvidenceError, match="hash mismatch"):
        evidence.verify_artifacts(tmp_path)


def test_evidence_without_provenance_uses_the_manifest_generation_time():
    evidence = load_published_calendar_evidence(EVIDENCE)
    assert evidence.retrospective_basis == "self_declared_generated_at"
    assert evidence.retrospective_at == EventTime(date(2026, 11, 15), Phase.CLOSE_AUDIT)


def test_header_and_complete_month_invariants_are_enforced(tmp_path):
    payload = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    def write(mutated):
        path = tmp_path / "session-evidence.json"
        path.write_text(json.dumps(mutated), encoding="utf-8")
        return path

    wrong_status = dict(payload, status="CONTRACT_PASS")
    with pytest.raises(CalendarEvidenceError, match="status"):
        load_published_calendar_evidence(write(wrong_status))

    wrong_kind = dict(payload, kind="weekday_calendar")
    with pytest.raises(CalendarEvidenceError, match="kind"):
        load_published_calendar_evidence(write(wrong_kind))

    conflicting = json.loads(json.dumps(payload))
    conflicting["years"]["2024"]["index_date_sets_identical"] = False
    with pytest.raises(CalendarEvidenceError, match="date sets disagree"):
        load_published_calendar_evidence(write(conflicting))

    wrong_count = json.loads(json.dumps(payload))
    wrong_count["years"]["2024"]["session_count"] += 1
    with pytest.raises(CalendarEvidenceError, match="session_count"):
        load_published_calendar_evidence(write(wrong_count))

    unresolved = json.loads(json.dumps(payload))
    unresolved["years"]["2024"]["months"]["2024-03"]["contradictions"] = [
        {"date": "2024-03-02", "type": "closure_and_index_session", "resolved": False}]
    with pytest.raises(CalendarEvidenceError, match="unresolved contradictions"):
        load_published_calendar_evidence(write(unresolved))


def test_the_signed_owner_acceptance_record_loads_against_the_real_evidence():
    evidence = load()
    acceptance = load_owner_acceptance(OWNER_ACCEPTANCE, evidence)
    assert acceptance.coverage_end == date(2026, 10, 31)
    assert acceptance.accepted_at == date(2026, 11, 16)
    assert acceptance.session_source == CONTEMPORANEOUS_SESSION_SOURCE
    assert acceptance.accepts_msd_notices is True
    assert acceptance.accepts_revised_muhurat_citation is True
    assert acceptance.evidence_sha256 == evidence.file_sha256


def test_the_signed_acceptance_burns_in_106_month_end_calendars():
    evidence = load()
    acceptance = load_owner_acceptance(OWNER_ACCEPTANCE, evidence)
    months = monthly_calendars(evidence, acceptance)

    assert len(months) == 106
    assert months[0].complete_months == ((2018, 1),)
    assert months[0].coverage_end == date(2018, 1, 31)
    assert months[-1].complete_months == ((2026, 10),)
    assert months[-1].coverage_end == date(2026, 10, 30)
    assert all(a.coverage_end < b.coverage_end for a, b in zip(months, months[1:]))
    assert all(m.complete_months == ((m.coverage_end.year, m.coverage_end.month),)
               for m in months)


def test_every_signed_calendar_admits_only_sessions_published_by_its_month_end():
    evidence = load()
    acceptance = load_owner_acceptance(OWNER_ACCEPTANCE, evidence)
    months = monthly_calendars(evidence, acceptance)
    special_days = {row.on for year in evidence.years for row in year.special_sessions}

    for calendar in months:
        assert calendar.available_at.session <= calendar.coverage_end
        for on in calendar.sessions:
            assert on.weekday() < 5 or on in special_days


def test_the_signed_record_refuses_forged_coverage_and_digest(tmp_path):
    evidence = load()
    payload = json.loads(OWNER_ACCEPTANCE.read_text(encoding="utf-8"))

    def write(mutated):
        path = tmp_path / "forged-acceptance.json"
        path.write_text(json.dumps(mutated), encoding="utf-8")
        return load_owner_acceptance(path, evidence)

    with pytest.raises(OwnerAcceptanceInvalid, match="coverage end"):
        write(dict(payload, coverage_end="2026-11-30"))
    with pytest.raises(OwnerAcceptanceInvalid, match="digest"):
        write(dict(payload, evidence_sha256="0" * 64))
