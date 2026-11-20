"""Diagnostic transcriptions of acquired NSE CMTR annual holiday circulars.

These checks preserve the transcribed holiday intent, but do not turn weekdays minus
annual holidays into a complete published-exchange session calendar. The acquired
circular set is not an authenticated inventory of every session/amendment.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from nse_pipeline.backtest.accounting_types import EventTime, Phase
from nse_pipeline.backtest.boundary import INFERRED_WEEKDAY, KnownCalendar, RebalanceSchedule

ROOT = Path(__file__).resolve().parents[1]
_RECON_PATH = ROOT / "verification" / "stage1" / "batch7" / "calendar-reconciliation.json"

# The Stage-1 evidence payload is not distributed with this repository (it holds
# acquired NSE circulars). Skip cleanly rather than fail collection on a clone.
pytestmark = pytest.mark.skipif(
    not _RECON_PATH.is_file(),
    reason="Stage-1 evidence payload not present (verification/stage1/batch7)",
)

RECON = json.loads(_RECON_PATH.read_text(encoding="utf-8")) if _RECON_PATH.is_file() else {"years": {}}


def _weekdays(year: int) -> list[date]:
    on, out = date(year, 1, 1), []
    while on.year == year:
        if on.weekday() < 5:
            out.append(on)
        on += timedelta(days=1)
    return out


def build_candidate(year: str) -> KnownCalendar:
    """Build an explicitly diagnostic transcription; not an admitted full calendar."""
    record = RECON["years"][year]
    circular = record["annual_circular"]
    y = int(year)
    holidays = {date.fromisoformat(d) for d in record["weekday_holidays"]}
    muhurat = date.fromisoformat(record["muhurat_session"]["date"])
    sessions = tuple(sorted(set([d for d in _weekdays(y) if d not in holidays] + [muhurat])))
    available_at = EventTime(date.fromisoformat(circular["publication_date"]), Phase.CLOSE_AUDIT)
    return KnownCalendar(
        f"diagnostic transcription only: {circular['download_ref']} {y} annual holidays",
        available_at,
        sessions,
        authority=INFERRED_WEEKDAY,
        special_sessions=(muhurat,),
        source_sha256=circular["sha256"],
        # Deliberately no complete_months or coverage_end: holiday list alone
        # does not authenticate the full session calendar.
    )


def test_acquired_year_transcriptions_are_diagnostic_not_authoritative():
    for year in RECON["years"]:
        candidate = build_candidate(year)
        assert candidate.authoritative is False
        with pytest.raises(ValueError, match="calendar_not_authoritative"):
            candidate.require_authoritative()
        with pytest.raises(ValueError, match="calendar_not_authoritative"):
            RebalanceSchedule([candidate])
        with pytest.raises(ValueError, match="calendar_not_authoritative"):
            RebalanceSchedule().add_calendar(candidate)
        assert candidate.complete_months == ()


def test_2025_transcription_preserves_holiday_intent_without_claiming_completeness():
    candidate = build_candidate("2025")
    sessions = set(candidate.sessions)
    # Christmas 2025 is a listed weekday holiday, so not a candidate session.
    assert date(2025, 12, 25) not in sessions
    # Republic Day 2025 falls on Sunday (already a non-trading weekend day).
    assert date(2025, 1, 26) not in sessions
    # A normal Monday appears in the weekday-derived candidate.
    assert date(2025, 1, 6) in sessions
    # Diwali Laxmi Pujan is the announced Muhurat special session.
    assert candidate.special_sessions == (date(2025, 10, 21),)
    assert date(2025, 10, 21) in sessions
    # This count is only the arithmetic candidate, not independently verified coverage.
    assert len(candidate.sessions) == 248


def test_publication_availability_does_not_make_holiday_transcription_admissible():
    for year in RECON["years"]:
        record = RECON["years"][year]
        candidate = build_candidate(year)
        assert candidate.available_at.session == date.fromisoformat(record["annual_circular"]["publication_date"])
        schedule = RebalanceSchedule()
        assert schedule.observe(candidate.available_at.session).basis != "known_calendar"
        with pytest.raises(ValueError, match="calendar_not_authoritative"):
            schedule.add_calendar(candidate)


def test_2025_transcription_cannot_fire_a_signal_after_its_publication_date():
    candidate = build_candidate("2025")
    schedule = RebalanceSchedule()
    for on in (date(2024, 12, 2), date(2024, 12, 3)):
        decision = schedule.observe(on)
        assert decision.basis != "known_calendar"
    # Timing of publication is not sufficient authority: annual holiday-derived
    # candidates remain diagnostic even at an observation after publication.
    with pytest.raises(ValueError, match="calendar_not_authoritative"):
        schedule.add_calendar(candidate)
    decision = schedule.observe(date(2025, 1, 1))
    assert decision.basis != "known_calendar"
