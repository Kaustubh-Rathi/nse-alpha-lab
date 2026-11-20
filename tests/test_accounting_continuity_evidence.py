"""Batch 8 continuity evidence: verified documents, no promotion, no future leak."""
from datetime import date
import json
from pathlib import Path
import shutil

import pytest

from nse_pipeline.backtest.accounting_types import EventTime, Phase
from nse_pipeline.backtest.continuity_evidence import (
    ContinuityCoverageUnknown,
    ContinuityEvidenceError,
    load_continuity_evidence,
    load_continuity_evidence_many,
)

ROOT = Path(__file__).resolve().parents[1]
LANE = ROOT / "verification" / "stage1" / "batch8" / "continuity"
HISTORY = LANE / "history"
PROBE = EventTime(date(2026, 11, 13), Phase.CLOSE_VALUATION)
FIRST = "b-2022-05-31-jslhisar-jsl"
LATE = "b-2024-12-06-star-onesource"


def _read(name):
    return json.loads((LANE / name).read_text(encoding="utf-8"))


def _read_history(name):
    return json.loads((HISTORY / name).read_text(encoding="utf-8"))


def minimal_lane(tmp_path) -> Path:
    """One real boundary and its real PDF, reduced so mutation tests stay fast."""
    ledger = _read("boundaries.json")
    catalog = _read("documents.json")
    boundary = next(row for row in ledger["boundaries"] if row["boundary_id"] == FIRST)
    document = next(row for row in catalog["documents"]
                    if row["document_id"] == boundary["evidence"]["document_id"])
    lane = tmp_path / "lane"
    target = lane / document["local_path"]
    target.parent.mkdir(parents=True)
    shutil.copyfile(LANE / document["local_path"], target)
    ledger["boundaries"] = [boundary]
    catalog["documents"] = [document]
    catalog["reference_documents"] = []
    (lane / "boundaries.json").write_text(json.dumps(ledger, indent=1), encoding="utf-8")
    (lane / "documents.json").write_text(json.dumps(catalog, indent=1), encoding="utf-8")
    return lane


def minimal_history_lane(tmp_path) -> Path:
    """One retained company filing plus one metadata-only external document."""
    ledger = _read_history("boundaries.json")
    catalog = _read_history("documents.json")
    boundary = ledger["boundaries"][0]
    document = next(row for row in catalog["documents"]
                    if row["document_id"] == boundary["evidence"]["document_id"])
    external = next(row for row in catalog["documents"]
                    if row["document_id"] != document["document_id"]
                    and row.get("retention") == "external_acquisition_cache")
    lane = tmp_path / "history"
    target = lane / document["local_path"]
    target.parent.mkdir(parents=True)
    shutil.copyfile(HISTORY / document["local_path"], target)
    ledger["boundaries"] = [boundary]
    catalog["documents"] = [document, external]
    catalog["reference_documents"] = []
    (lane / "boundaries.json").write_text(json.dumps(ledger, indent=1), encoding="utf-8")
    (lane / "documents.json").write_text(json.dumps(catalog, indent=1), encoding="utf-8")
    return lane


def _mutate(lane: Path, name: str, change) -> None:
    payload = json.loads((lane / name).read_text(encoding="utf-8"))
    change(payload)
    (lane / name).write_text(json.dumps(payload, indent=1), encoding="utf-8")


def test_real_parent_lane_verifies_every_document_and_builds_the_probe_ledger():
    evidence = load_continuity_evidence()
    assert evidence.status == "INCOMPLETE"
    assert evidence.profile_name == "nse_indices"
    # Batch 10 corpus extension: 143 -> 1113 stored releases, 44 -> 59 boundaries.
    assert len(evidence.boundaries) == 59
    assert len(evidence.document_digests) == 1113
    # The proven interval now starts at the first two-sided 2018 release.
    assert evidence.coverage.proven_interval == (date(2018, 2, 5), date(2026, 9, 7))
    assert evidence.coverage.complete is False

    ledger = evidence.ledger_at(PROBE)
    assert len(ledger.records) == 59
    assert ledger.issues == ()
    assert ledger.excluded_count == 0
    assert {record.event_kind for record in ledger.records} == {"merger", "demerger"}


def test_documents_published_after_the_decision_leave_no_trace():
    evidence = load_continuity_evidence()
    # The lane now also proves 2018-2019 edges, so the probe asserts the specific
    # boundary is invisible before its availability time rather than that the
    # whole ledger is empty at that instant.
    before = evidence.ledger_at(EventTime(date(2022, 5, 24), Phase.CLOSE_VALUATION))
    assert FIRST not in {record.boundary_id for record in before.records}

    at_open = evidence.ledger_at(EventTime(date(2022, 5, 25), Phase.PRE_OPEN_EVIDENCE))
    assert FIRST in {record.boundary_id for record in at_open.records}


def test_late_identity_evidence_is_never_backfilled():
    evidence = load_continuity_evidence()
    boundary = next(row for row in evidence.boundaries if row.boundary_id == LATE)
    at_effective = evidence.ledger_at(EventTime(date(2024, 12, 6), Phase.PRE_OPEN_EVIDENCE))
    resolution = at_effective.resolve(boundary.source_key, boundary.successor_key,
                                      date(2024, 12, 6))
    assert resolution.record is None
    assert resolution.reason == "no_admitted_continuity"

    later = evidence.ledger_at(EventTime(date(2025, 2, 6), Phase.PRE_OPEN_EVIDENCE))
    assert later.resolve(boundary.source_key, boundary.successor_key,
                         date(2024, 12, 6)).admitted


def test_screened_corpus_never_claims_continuous_coverage():
    evidence = load_continuity_evidence()
    assert evidence.coverage.complete is False
    assert "screened subset" in evidence.coverage.completeness_reason
    with pytest.raises(ContinuityCoverageUnknown, match="not complete"):
        evidence.require_coverage(date(2022, 5, 31), date(2026, 9, 7))
    with pytest.raises(ContinuityCoverageUnknown):
        evidence.require_coverage(date(2018, 1, 1), date(2022, 5, 30))
    with pytest.raises(ContinuityCoverageUnknown):
        evidence.require_coverage(date(2022, 5, 31), date(2026, 9, 8))
    with pytest.raises(ContinuityCoverageUnknown):
        evidence.require_coverage(date(2026, 9, 8), date(2026, 11, 16))


def test_real_history_lane_binds_company_filings_without_promotion():
    evidence = load_continuity_evidence(HISTORY)
    assert evidence.status == "INCOMPLETE"
    assert evidence.profile_name == "company_filings"
    assert len(evidence.boundaries) == 14
    assert len(evidence.document_digests) == 302
    assert evidence.coverage.proven_interval == (date(2018, 3, 31), date(2022, 5, 3))
    assert evidence.coverage.complete is False
    assert {row.event_kind for row in evidence.boundaries} == {
        "merger", "demerger", "scheme_of_arrangement"}
    for row in evidence.boundaries:
        assert row.evidence.publisher != "NSE Indices Limited"
        assert row.evidence.published_at <= row.evidence.available_at.session
        assert row.evidence.available_at.phase is Phase.PRE_OPEN_EVIDENCE
    with pytest.raises(ContinuityCoverageUnknown, match="not complete"):
        evidence.require_coverage(date(2018, 3, 31), date(2018, 3, 31))


def test_history_late_identity_evidence_is_never_backfilled():
    evidence = load_continuity_evidence(HISTORY)
    boundary = next(row for row in evidence.boundaries
                    if row.source_key.startswith("NSE:HTMEDIA|"))
    early = evidence.ledger_at(EventTime(date(2018, 4, 2), Phase.PRE_OPEN_EVIDENCE))
    assert early.resolve(boundary.source_key, boundary.successor_key,
                         date(2018, 3, 31)).record is None

    late = evidence.ledger_at(EventTime(date(2019, 5, 16), Phase.PRE_OPEN_EVIDENCE))
    assert late.resolve(boundary.source_key, boundary.successor_key,
                        date(2018, 3, 31)).admitted


def test_combined_ledger_merges_both_screened_corpora():
    combined = load_continuity_evidence_many()
    assert len(combined.lanes) == 2
    assert combined.statuses == ("INCOMPLETE", "INCOMPLETE")
    # 59 parent (NSE Indices) + 14 history (company filings).
    assert len(combined.boundaries) == 73
    assert len(combined.document_digests) == 1415
    ledger = combined.ledger_at(PROBE)
    # The Batch 10 extension added early parent-lane edges that collide with five
    # history-lane edges on the same source key. The combined loader refuses them
    # fail-closed: 10 boundaries are excluded and 5 issues are reported, so the
    # merged ledger carries 63 admitted records, not 73.
    assert len(ledger.records) == 63
    assert ledger.excluded_count == 10
    assert {issue.reason for issue in ledger.issues} == {
        "ambiguous_successor", "duplicate_boundary"}
    assert len(ledger.issues) == 5
    with pytest.raises(ContinuityCoverageUnknown):
        combined.require_coverage(date(2022, 5, 31), date(2026, 9, 7))
    with pytest.raises(ContinuityCoverageUnknown):
        combined.require_coverage(date(2018, 3, 31), date(2018, 3, 31))


def test_minimal_lane_loads_then_a_changed_byte_is_refused(tmp_path):
    lane = minimal_lane(tmp_path)
    evidence = load_continuity_evidence(lane)
    assert len(evidence.boundaries) == 1

    document = json.loads((lane / "documents.json").read_text(encoding="utf-8"))["documents"][0]
    path = lane / document["local_path"]
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 0xFF
    path.write_bytes(bytes(raw))
    with pytest.raises(ContinuityEvidenceError, match="SHA-256 changed"):
        load_continuity_evidence(lane)


def test_missing_document_is_refused(tmp_path):
    lane = minimal_lane(tmp_path)
    document = json.loads((lane / "documents.json").read_text(encoding="utf-8"))["documents"][0]
    (lane / document["local_path"]).unlink()
    with pytest.raises(ContinuityEvidenceError, match="missing"):
        load_continuity_evidence(lane)


def test_catalog_digest_mismatch_is_refused(tmp_path):
    lane = minimal_lane(tmp_path)

    def change(payload):
        payload["documents"][0]["sha256"] = "0" * 64

    _mutate(lane, "documents.json", change)
    with pytest.raises(ContinuityEvidenceError, match="SHA-256 changed"):
        load_continuity_evidence(lane)


def test_boundary_content_digest_must_match_the_catalog(tmp_path):
    lane = minimal_lane(tmp_path)

    def change(payload):
        payload["boundaries"][0]["evidence"]["content_sha256"] = "1" * 64

    _mutate(lane, "boundaries.json", change)
    with pytest.raises(ContinuityEvidenceError, match="disagrees with the document catalog"):
        load_continuity_evidence(lane)


def test_catalog_must_bind_the_boundary_as_primary(tmp_path):
    lane = minimal_lane(tmp_path)

    def change(payload):
        payload["documents"][0]["primary_boundary_ids"] = []

    _mutate(lane, "documents.json", change)
    with pytest.raises(ContinuityEvidenceError, match="does not bind"):
        load_continuity_evidence(lane)


def test_effective_date_outside_the_documentary_interval_is_refused(tmp_path):
    lane = minimal_lane(tmp_path)

    def change(payload):
        payload["boundaries"][0]["effective_on"] = "2022-05-30"

    _mutate(lane, "boundaries.json", change)
    with pytest.raises(ContinuityEvidenceError, match="not admissible documentary evidence"):
        load_continuity_evidence(lane)


def test_a_contract_pass_claim_is_refused(tmp_path):
    lane = minimal_lane(tmp_path)

    def change(payload):
        payload["contract_pass_claimed"] = True

    _mutate(lane, "boundaries.json", change)
    with pytest.raises(ContinuityEvidenceError, match="must not claim a contract pass"):
        load_continuity_evidence(lane)


def test_a_promoted_status_is_refused(tmp_path):
    lane = minimal_lane(tmp_path)

    def change(payload):
        payload["status"] = "CONTRACT_PASS"

    _mutate(lane, "boundaries.json", change)
    with pytest.raises(ContinuityEvidenceError, match="non-promoting review state"):
        load_continuity_evidence(lane)


def test_a_document_path_cannot_escape_the_lane(tmp_path):
    lane = minimal_lane(tmp_path)
    (tmp_path / "outside.pdf").write_bytes(b"not evidence")

    def change(payload):
        payload["documents"][0]["local_path"] = "../outside.pdf"

    _mutate(lane, "documents.json", change)
    with pytest.raises(ContinuityEvidenceError, match="escapes the evidence lane"):
        load_continuity_evidence(lane)


def test_effective_rows_are_filtered_by_their_own_dates():
    evidence = load_continuity_evidence()
    rows = evidence.boundaries_effective_between(date(2022, 5, 31), date(2022, 8, 8))
    assert tuple(row.boundary_id for row in rows) == (
        "b-2022-05-31-jslhisar-jsl",
        "b-2022-08-08-nmdc-nslnisp",
        "b-2022-08-08-pel-pplpharma",
        "b-2022-08-08-shriramcit-srtransfin",
    )


def test_company_role_outside_authority_is_refused(tmp_path):
    lane = minimal_history_lane(tmp_path)

    def change(payload):
        payload["documents"][1]["publisher_role"] = "Unaffiliated research blog"

    _mutate(lane, "documents.json", change)
    with pytest.raises(ContinuityEvidenceError, match="outside the declared authority"):
        load_continuity_evidence(lane)


def test_company_venue_outside_nse_is_refused(tmp_path):
    lane = minimal_history_lane(tmp_path)

    def change(payload):
        payload["documents"][1]["venue"] = "Unaffiliated market blog"

    _mutate(lane, "documents.json", change)
    with pytest.raises(ContinuityEvidenceError, match="outside the declared authority"):
        load_continuity_evidence(lane)


def test_history_publication_timestamp_must_be_timezone_aware(tmp_path):
    lane = minimal_history_lane(tmp_path)

    def change(payload):
        payload["boundaries"][0]["evidence"]["published_at"] = "2019-05-15T07:19:00"

    _mutate(lane, "boundaries.json", change)
    with pytest.raises(ContinuityEvidenceError, match="must declare a timezone"):
        load_continuity_evidence(lane)


def test_external_cache_documents_are_metadata_only(tmp_path):
    lane = minimal_history_lane(tmp_path)
    evidence = load_continuity_evidence(lane)
    assert len(evidence.document_digests) == 2

    def change(payload):
        payload["documents"][1]["local_path"] = "documents/pdf/missing.pdf"

    _mutate(lane, "documents.json", change)
    with pytest.raises(ContinuityEvidenceError, match="still declares a lane-local path"):
        load_continuity_evidence(lane)


def test_history_boundary_cannot_cite_an_external_document(tmp_path):
    lane = minimal_history_lane(tmp_path)

    def change(payload):
        document = payload["documents"][0]
        document["retention"] = "external_acquisition_cache"
        document["cache_path"] = "documents/pdf/external.pdf"
        document["local_path"] = None
        document["hash_verified_on_disk"] = False

    _mutate(lane, "documents.json", change)
    with pytest.raises(ContinuityEvidenceError, match="not retained in the lane"):
        load_continuity_evidence(lane)


def test_duplicate_cross_lane_boundary_is_refused(tmp_path):
    lane = minimal_lane(tmp_path)
    with pytest.raises(ContinuityEvidenceError, match="duplicate boundary id across lanes"):
        load_continuity_evidence_many((lane, lane))
