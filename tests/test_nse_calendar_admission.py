"""Offline integrity/admission checks for the calendar source audit.

These tests validate the specific acquired PDF bytes and quoted source fields. They
intentionally do not certify exhaustive notice capture or complete session calendars.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

import pytest

from nse_pipeline.backtest.accounting_types import EventTime, Phase
from nse_pipeline.backtest.boundary import (
    INFERRED_WEEKDAY,
    KnownCalendar,
    RebalanceSchedule,
)

ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "verification/stage1/readiness-reopen/calendar-source-audit.json"
BATCH7 = ROOT / "verification/stage1/batch7"
_RECON_PATH = BATCH7 / "calendar-reconciliation.json"
_PROVENANCE_PATH = BATCH7 / "provenance.json"

# These tests verify the bytes of acquired NSE PDFs, which are not distributed
# with this repository. Skip cleanly rather than fail collection on a clone.
pytestmark = pytest.mark.skipif(
    not (AUDIT_PATH.is_file() and _RECON_PATH.is_file() and _PROVENANCE_PATH.is_file()),
    reason="Stage-1 evidence payload not present (verification/stage1)",
)

AUDIT = json.loads(AUDIT_PATH.read_text(encoding="utf-8")) if AUDIT_PATH.is_file() else {"artifacts": []}
PROVENANCE = json.loads(_PROVENANCE_PATH.read_text(encoding="utf-8")) if _PROVENANCE_PATH.is_file() else {}
RECONCILIATION = json.loads(_RECON_PATH.read_text(encoding="utf-8")) if _RECON_PATH.is_file() else {}


def test_audited_pdf_bytes_have_exact_hash_size_and_identity():
    """Byte checks are mandatory and need no optional PDF parser or network."""
    for artifact in AUDIT["artifacts"]:
        path = ROOT / artifact["path"]
        raw = path.read_bytes()
        assert len(raw) == artifact["bytes"], artifact["path"]
        assert hashlib.sha256(raw).hexdigest() == artifact["sha256"], artifact["path"]
        assert raw.startswith(b"%PDF-"), artifact["path"]


def test_audited_publication_dates_and_references_match_pdf_text():
    """Use the optional project PDF extra to independently read exact local PDFs."""
    pypdf = pytest.importorskip("pypdf", reason="install the optional 'pdf' extra for PDF text verification")
    for artifact in AUDIT["artifacts"]:
        reader = pypdf.PdfReader(str(ROOT / artifact["path"]))
        pages = [page.extract_text() or "" for page in reader.pages]
        all_text = "\n".join(pages)
        date_page = " ".join((pages[artifact["date_page"] - 1]).split())
        normalized_text = " ".join(all_text.split())
        assert " ".join(artifact["publication_date_quote"].split()) in date_page
        assert " ".join(artifact["reference_quote"].split()) in date_page
        assert artifact["publisher"] in normalized_text
        assert artifact["download_ref"] in normalized_text
        normalize = lambda value: re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
        assert normalize(artifact["session_date_quote"]) in normalize(normalized_text)
        parsed = date.fromisoformat(artifact["publication_date"])
        month_names = (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        )
        printed = f"{month_names[parsed.month - 1]} {parsed.day:02d}, {parsed.year}"
        assert printed in artifact["publication_date_quote"]


def test_conflicting_historical_publication_date_claims_are_preserved_and_resolved():
    claims = {
        ("CMTR50560_2022.pdf", "provenance_json", "2021-12-14", "2021-12-10"),
        ("CMTR54757_2023.pdf", "provenance_json", "2022-12-16", "2022-12-08"),
        ("CMTR59722_2024.pdf", "provenance_json", "2023-12-15", "2023-12-12"),
        ("CMTR64628_2024_muhurat.pdf", "provenance_json", "2024-10-31", "2024-10-19"),
        ("CMTR70319_2025_muhurat.pdf", "provenance_json", "2025-10-30", "2025-09-22"),
    }
    actual = set()
    provenance_by_name = {
        Path(item["path"]).name: item["published_date"]
        for item in PROVENANCE["artifacts"] if item["path"].startswith("calendar/")
    }
    for artifact in AUDIT["artifacts"]:
        name = Path(artifact["path"]).name
        provenance_record = next(item for item in PROVENANCE["artifacts"]
                                 if item["path"].endswith(name))
        assert provenance_record["publisher"] in artifact["publisher"]
        assert artifact["url"] == provenance_record["url"]
        assert artifact["sha256"] == provenance_record["sha256"]
        if artifact["kind"] == "annual_calendar":
            year_record = RECONCILIATION["years"][str(artifact["year"])]
            assert artifact["download_ref"] == year_record["annual_circular"]["download_ref"]
            assert artifact["circular_ref"] == year_record["annual_circular"]["circular_ref"]
        else:
            year_record = RECONCILIATION["years"][str(artifact["year"])]
            assert artifact["download_ref"] == year_record["later_change_notice"]["download_ref"]
            assert artifact["circular_ref"] == year_record["later_change_notice"]["circular_ref"]
        for conflict in artifact["conflicts"]:
            actual.add((name, "provenance_json",
                        conflict["provenance_json"], conflict["verified_pdf"]))
            assert conflict["calendar_reconciliation_json"] == conflict["verified_pdf"]
            assert conflict["provenance_json"] != conflict["verified_pdf"]
            assert provenance_by_name[name] == conflict["provenance_json"]
            assert artifact["publication_date"] == conflict["verified_pdf"]
    assert actual == claims


def test_audit_does_not_claim_exhaustive_notices_or_complete_months():
    assert AUDIT["confidence"] == "PARTIAL_NOT_A_COMPLETE_CALENDAR"
    assert AUDIT["complete_calendar_claim"] is False
    assert AUDIT["all_amendments_captured"] is False
    assert len(AUDIT["artifacts"]) == 10
    assert any("exhaustive notice-series search" in gap["gap"] for gap in AUDIT["known_gaps"])


def test_weekday_minus_annual_holiday_candidate_is_diagnostic_and_cannot_register():
    """A digest plus caller-declared months cannot promote an inferred candidate."""
    candidate = KnownCalendar(
        "diagnostic transcription, not complete source-authenticated calendar",
        EventTime(date(2024, 12, 13), Phase.CLOSE_AUDIT),
        tuple(date(2025, 1, day) for day in (2, 3, 6)),
        authority=INFERRED_WEEKDAY,
        source_sha256="1e0f85615fbbcde9243dd6cafbbad4d9b581b8d2467fd9e27c0a4fe42a720c7d",
    )
    assert candidate.authoritative is False
    with pytest.raises(ValueError, match="calendar_not_authoritative"):
        RebalanceSchedule([candidate])
    with pytest.raises(ValueError, match="calendar_not_authoritative"):
        RebalanceSchedule().add_calendar(candidate)


def test_special_notice_publication_is_not_backdated_to_annual_circular_or_session():
    for artifact in AUDIT["artifacts"]:
        if artifact["kind"] != "muhurat_timing_notice":
            continue
        published = date.fromisoformat(artifact["publication_date"])
        session = date.fromisoformat(artifact["session_date"])
        assert published < session
        assert published.strftime("%B %d, %Y") in artifact["publication_date_quote"]
        assert artifact["publication_date"] != artifact["session_date"]
        assert "session date" in artifact["resolution"]
