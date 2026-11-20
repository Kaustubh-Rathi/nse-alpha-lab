"""Batch 8 NIFTY 500 membership evidence: retained bytes and exact-date loading."""
from datetime import date
import json
from pathlib import Path
import shutil

import pytest

from nse_pipeline.backtest.accounting_types import EventTime, Phase
from nse_pipeline.nifty import membership_evidence
from nse_pipeline.nifty.membership_evidence import (
    MembershipEvidenceError, load_batch9_membership_evidence,
    load_merged_membership_evidence, load_membership_evidence,
)
from nse_pipeline.nifty.membership_port import CoverageUnknown

ROOT = Path(__file__).resolve().parents[1]
LANE = ROOT / "verification" / "stage1" / "batch8" / "membership"
BATCH9_LANE = ROOT / "verification" / "stage1" / "batch9" / "universe"
FIRST = "2013-04-18"
SECOND = "2013-05-31"
DECISION = EventTime(date(2022, 4, 4), Phase.PRE_OPEN_EVIDENCE)
EARLY_DECISION = EventTime(date(2013, 4, 22), Phase.PRE_OPEN_EVIDENCE)
BATCH9_DECISION = EventTime(date(2026, 10, 1), Phase.PRE_OPEN_EVIDENCE)
BATCH9_LAST = "2026-09-30"
BOUNDARY_DIGEST = "6b5ab98ea251b9808f5985caa9f1eacf8c19451cf7b58e26dde8ab2486ea8bcd"


def _read(name: str) -> dict:
    return json.loads((LANE / name).read_text(encoding="utf-8"))


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def _mutate(lane: Path, name: str, change) -> None:
    payload = json.loads((lane / name).read_text(encoding="utf-8"))
    change(payload)
    _write(lane / name, payload)


@pytest.fixture
def trimmed_lane(tmp_path, monkeypatch) -> Path:
    """Two real anchors, reduced so every mutation test stays fast."""
    anchors = _read("anchors.json")
    reconstruction = _read("reconstruction.json")
    selected = [row for row in anchors["snapshot_anchors"]
                if row["as_of"] in {FIRST, SECOND}]
    selected.sort(key=lambda row: row["as_of"])
    assert [row["as_of"] for row in selected] == [FIRST, SECOND]

    lane = tmp_path / "lane"
    for anchor in selected:
        for field in ("retained_csv_path", "retained_sidecar_path"):
            target = lane / anchor[field]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(LANE / anchor[field], target)

    dates = {row["as_of"] for row in selected}
    intervals = [row for row in reconstruction["intervals"]
                 if row["effective_from"] in dates]
    windows = [row for row in reconstruction["coverage_windows"]
               if row["effective_from"] in dates]
    symbols = {row["symbol"] for row in intervals}
    mapping = {key: symbol for key, symbol in reconstruction["key_to_symbol"].items()
               if symbol in symbols}

    anchors["snapshot_anchors"] = selected
    anchors["anchor_counts"] = {
        "verified_snapshot_anchor_count": len(selected),
        "pdf_cross_check_anchor_count": 0,
        "total_constituent_anchor_count": len(selected),
        "current_reconciliation_target_count": 0,
    }
    reconstruction["intervals"] = intervals
    reconstruction["coverage_windows"] = windows
    reconstruction["key_to_symbol"] = mapping
    reconstruction["counts"] = {
        "snapshot_count": len(selected),
        "interval_count": len(intervals),
        "coverage_window_count": len(windows),
        "distinct_symbol_count": len(symbols),
        "key_to_symbol_count": len(mapping),
        "total_snapshot_rows": len(intervals),
    }
    reconstruction["coverage"]["dates"] = [FIRST, SECOND]
    reconstruction["coverage"]["first_date"] = FIRST
    reconstruction["coverage"]["last_date"] = SECOND
    reconstruction["coverage"]["interval_count_by_date"] = {
        day: sum(1 for row in intervals if row["effective_from"] == day)
        for day in (FIRST, SECOND)
    }
    reconstruction["coverage"]["window_count_by_date"] = {day: 1 for day in (FIRST, SECOND)}

    _write(lane / "anchors.json", anchors)
    _write(lane / "reconstruction.json", reconstruction)
    monkeypatch.setattr(membership_evidence, "EXPECTED_ANCHOR_COUNT", len(selected))
    monkeypatch.setattr(membership_evidence, "EXPECTED_WINDOW_COUNT", len(windows))
    monkeypatch.setattr(membership_evidence, "EXPECTED_INTERVAL_COUNT", len(intervals))
    return lane


def test_real_lane_loads_exact_dates_and_refuses_between_snapshot_dates():
    evidence = load_membership_evidence(decision_at=DECISION)
    assert len(evidence.intervals) == 54_072
    assert len(evidence.coverage_windows) == 108
    assert len(evidence.key_to_symbol) == 917

    day = date.fromisoformat(FIRST)
    assert evidence.is_member("ORCHIDCHEM", day) is True
    assert evidence.is_member("TATAMTRDVR", day) is False
    with pytest.raises(CoverageUnknown):
        evidence.is_member("ORCHIDCHEM", date(2013, 4, 19))

    assert evidence.require_coverage(day, day).effective_from == day
    with pytest.raises(CoverageUnknown):
        evidence.require_coverage(day, date.fromisoformat(SECOND))


def test_future_snapshots_are_invisible_at_an_earlier_decision(trimmed_lane):
    evidence = load_membership_evidence(trimmed_lane, decision_at=EARLY_DECISION)
    assert evidence.is_member("ORCHIDCHEM", date.fromisoformat(FIRST)) is True
    with pytest.raises(CoverageUnknown):
        evidence.is_member("ORCHIDCHEM", date.fromisoformat(SECOND))


def test_retained_csv_byte_tamper_is_refused(trimmed_lane):
    anchor = json.loads((trimmed_lane / "anchors.json").read_text(encoding="utf-8"))[
        "snapshot_anchors"][0]
    path = trimmed_lane / anchor["retained_csv_path"]
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 0xFF
    path.write_bytes(bytes(raw))
    with pytest.raises(MembershipEvidenceError):
        load_membership_evidence(trimmed_lane, decision_at=DECISION)


def test_anchor_digest_tamper_is_refused(trimmed_lane):
    def change(payload):
        payload["snapshot_anchors"][0]["snapshot_sha256"] = "0" * 64

    _mutate(trimmed_lane, "anchors.json", change)
    with pytest.raises(MembershipEvidenceError):
        load_membership_evidence(trimmed_lane, decision_at=DECISION)


def test_missing_retained_csv_is_refused(trimmed_lane):
    anchor = json.loads((trimmed_lane / "anchors.json").read_text(encoding="utf-8"))[
        "snapshot_anchors"][0]
    (trimmed_lane / anchor["retained_csv_path"]).unlink()
    with pytest.raises(MembershipEvidenceError):
        load_membership_evidence(trimmed_lane, decision_at=DECISION)


@pytest.mark.parametrize("name", ["anchors.json", "reconstruction.json"])
def test_promoted_status_is_refused(trimmed_lane, name):
    _mutate(trimmed_lane, name, lambda payload: payload.__setitem__("status", "CONTRACT_PASS"))
    with pytest.raises(MembershipEvidenceError):
        load_membership_evidence(trimmed_lane, decision_at=DECISION)


@pytest.mark.parametrize("name", ["anchors.json", "reconstruction.json"])
def test_contract_pass_claim_is_refused(trimmed_lane, name):
    _mutate(trimmed_lane, name,
            lambda payload: payload.__setitem__("contract_pass_claimed", True))
    with pytest.raises(MembershipEvidenceError):
        load_membership_evidence(trimmed_lane, decision_at=DECISION)


def test_interval_date_outside_the_anchors_is_refused(trimmed_lane):
    def change(payload):
        payload["intervals"][0]["effective_from"] = "2013-04-19"
        payload["intervals"][0]["effective_to"] = "2013-04-19"

    _mutate(trimmed_lane, "reconstruction.json", change)
    with pytest.raises(MembershipEvidenceError):
        load_membership_evidence(trimmed_lane, decision_at=DECISION)


def test_interval_and_window_availability_must_agree(trimmed_lane):
    def change(payload):
        payload["intervals"][0]["available_at"]["date"] = "2013-04-23"
        payload["intervals"][0]["available_at"]["session"] = "2013-04-23"

    _mutate(trimmed_lane, "reconstruction.json", change)
    with pytest.raises(MembershipEvidenceError):
        load_membership_evidence(trimmed_lane, decision_at=DECISION)


def test_decision_before_the_first_availability_is_refused(trimmed_lane):
    before = EventTime(date(2013, 4, 21), Phase.PRE_OPEN_EVIDENCE)
    with pytest.raises(MembershipEvidenceError):
        load_membership_evidence(trimmed_lane, decision_at=before)


def test_future_evidence_is_not_exposed_by_the_bound_port(trimmed_lane):
    evidence = load_membership_evidence(trimmed_lane, decision_at=EARLY_DECISION)
    assert {row.effective_from for row in evidence.intervals} == {date(2013, 4, 18)}
    assert len(evidence.coverage_windows) == 1


def test_batch9_lane_loads_point_evidence_and_refuses_between_anchors():
    evidence = load_batch9_membership_evidence(decision_at=BATCH9_DECISION)
    assert len(evidence.intervals) == 32_670
    assert len(evidence.coverage_windows) == 65
    assert len(evidence.key_to_symbol) == 727

    last = date.fromisoformat(BATCH9_LAST)
    assert evidence.is_member("DUMMYHEG", last) is True
    assert evidence.is_member("DUMMYHEG", date(2026, 9, 7)) is True
    assert evidence.require_coverage(last, last).source_sha256
    with pytest.raises(CoverageUnknown):
        evidence.is_member("DUMMYHEG", date(2024, 1, 15))
    with pytest.raises(CoverageUnknown):
        evidence.is_member("DUMMYHEG", date(2026, 10, 15))
    with pytest.raises(CoverageUnknown):
        evidence.require_coverage(date(2026, 9, 29), last)


def test_merged_lane_joins_only_the_shared_boundary_and_keeps_gaps_unknown():
    evidence = load_merged_membership_evidence(decision_at=BATCH9_DECISION)
    assert len(evidence.intervals) == 54_072 + 32_670 - 501
    assert len(evidence.coverage_windows) == 108 + 65 - 1
    assert len(evidence.key_to_symbol) == 1_097

    assert evidence.is_member("ORCHIDCHEM", date(2013, 4, 18)) is True
    assert evidence.is_member("DUMMYHEG", date.fromisoformat(BATCH9_LAST)) is True
    assert evidence.is_member("DUMMYHEG", date(2022, 3, 31)) is False
    boundary = evidence.require_coverage(date(2022, 3, 31), date(2022, 3, 31))
    assert boundary.source_sha256 == BOUNDARY_DIGEST

    with pytest.raises(CoverageUnknown):
        evidence.is_member("ORCHIDCHEM", date(2013, 4, 19))
    with pytest.raises(CoverageUnknown):
        evidence.is_member("DUMMYHEG", date(2024, 1, 15))
    with pytest.raises(CoverageUnknown):
        evidence.is_member("DUMMYHEG", date(2026, 10, 15))


def test_merged_lane_hides_future_batch9_evidence_at_an_early_decision():
    evidence = load_merged_membership_evidence(decision_at=EARLY_DECISION)
    assert {row.effective_from for row in evidence.intervals} == {date(2013, 4, 18)}
    assert len(evidence.coverage_windows) == 1
    with pytest.raises(CoverageUnknown):
        evidence.is_member("ORCHIDCHEM", date.fromisoformat(SECOND))
