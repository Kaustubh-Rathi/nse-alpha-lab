"""Batch 9 universe lane: post-2022 NIFTY 500 membership point evidence.

Pins contract P0-UNIVERSE evidence properties for
verification/stage1/batch9/universe: digest-bound official sources for every
anchor, strictly ordered walk-forward anchors with no membership inherited
across gaps (CoverageUnknown between anchors), refusal log integrity and
README count honesty. This lane claims no contract PASS; these tests never
weaken assertions when data is absent - absent data itself must fail.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LANE = ROOT / "verification" / "stage1" / "batch9" / "universe"
BATCH8 = ROOT / "verification" / "stage1" / "batch8" / "membership"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(name: str):
    return json.loads((LANE / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def anchors():
    return load("anchors.json")


@pytest.fixture(scope="module")
def provenance():
    return load("provenance.json")


@pytest.fixture(scope="module")
def reconstruction():
    return load("reconstruction.json")


def _symbols(csv_path: Path) -> list[str]:
    rows = list(csv.reader(csv_path.read_text(encoding="utf-8").splitlines()))
    assert rows[0] == ["symbol", "company_name", "isin"]
    return [r[0] for r in rows[1:]]


def test_base_snapshot_is_byte_identical_to_batch8(anchors):
    base = anchors["snapshot_anchors"][0]
    assert base["anchor_kind"] == "official_snapshot"
    assert base["as_of"] == "2022-03-31"
    ours = LANE / base["retained_csv_path"]
    theirs = BATCH8 / "snapshots" / "NIFTY500_2022-03-31.csv"
    assert sha256(ours) == sha256(theirs) == base["csv_sha256"]


def test_every_post_2022_anchor_snapshot_parses_with_verified_provenance(anchors, provenance):
    docs = {d["file"]: d for d in provenance["documents"]}
    post = [a for a in anchors["snapshot_anchors"] if a["as_of"] > "2022-03-31"]
    assert post, "lane must carry post-2022 anchors"
    for a in post:
        csv_path = LANE / a["retained_csv_path"]
        sidecar_path = LANE / a["retained_sidecar_path"]
        assert csv_path.exists() and sidecar_path.exists()
        syms = _symbols(csv_path)
        assert len(syms) == a["row_count"] == a["unique_symbol_count"]
        digest = sha256(csv_path)
        assert digest == a["csv_sha256"] == a["snapshot_sha256"]
        assert csv_path.stat().st_size == a["csv_bytes"]
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        assert sidecar["as_of"] == a["as_of"]
        assert sha256(sidecar_path) == a["manifest_sha256"]
        # every cited evidence document is an official dated source on disk
        assert a["evidence_documents"], a["as_of"]
        for ev in a["evidence_documents"]:
            assert re.match(r"https://www\.niftyindices\.com/Press_Release/ind_prs\d{8}(_\d+)?\.pdf$",
                            ev["url"]), (a["as_of"], ev)
            assert ev["publication_date"] and ev["retrieved_at_utc"]
            disk = LANE / "sources" / "press_releases" / ev["file"]
            assert disk.exists() and sha256(disk) == ev["sha256"]
            assert docs[ev["file"]]["sha256"] == ev["sha256"]
            assert docs[ev["file"]]["publication_date"] == ev["publication_date"]
            # availability may only follow the later of the effective date and
            # the disclosure of the citing document (corporate-action
            # transitions are documented retrospectively)
            assert ev["publication_date"] <= a["available_at"]["date"]


def test_anchor_dates_strictly_ordered_and_complete(anchors):
    dates = [a["as_of"] for a in anchors["snapshot_anchors"]]
    assert dates == sorted(dates) and len(set(dates)) == len(dates)
    assert anchors["coverage"]["dates"] == dates
    assert anchors["anchor_counts"]["total_constituent_anchor_count"] == len(dates)


def test_no_snapshot_inherits_membership_across_a_gap(anchors):
    """Each anchor equals its predecessor with only that anchor's recorded rows applied."""
    cur: set[str] | None = None
    for a in anchors["snapshot_anchors"]:
        syms = set(_symbols(LANE / a["retained_csv_path"]))
        if cur is None:
            assert a["anchor_kind"] == "official_snapshot"
        else:
            nxt = set(cur)
            for ev in a["changes_applied"]:
                for r in ev["rows_applied"]:
                    if r["result"] == "removed":
                        assert r["symbol"] in nxt
                        nxt.discard(r["symbol"])
                    elif r["result"] == "added":
                        assert r["symbol"] not in nxt
                        nxt.add(r["symbol"])
                    elif r["result"] == "unresolved_symbol_absent":
                        assert r["direction"] in ("excluded", "inclusion_revoked")
                        assert r["symbol"] not in nxt
            assert nxt == syms, a["as_of"]
        cur = syms


def test_intervals_and_windows_exist_only_on_anchor_dates(reconstruction, anchors):
    anchor_dates = {a["as_of"] for a in anchors["snapshot_anchors"]}
    for iv in reconstruction["intervals"]:
        assert iv["effective_from"] == iv["effective_to"] in anchor_dates
    for w in reconstruction["coverage_windows"]:
        assert w["effective_from"] == w["effective_to"] in anchor_dates
    covered = {w["effective_from"] for w in reconstruction["coverage_windows"]}
    assert covered == anchor_dates


def test_between_anchor_dates_are_coverage_unknown(reconstruction):
    cov = reconstruction["coverage"]
    assert cov["complete"] is False
    assert cov["between_anchor_policy"] == "CoverageUnknown"
    assert cov["last_date"] == "2026-09-30"
    dates = set(cov["dates"])
    # dates between proven anchors and beyond the chain end are not covered
    for probe in ["2022-06-15", "2024-01-15", "2026-10-15", "2026-10-31", "2026-11-13"]:
        assert probe not in dates
    # no forward-fill: an interval's window is exactly its anchor day
    for w in reconstruction["coverage_windows"]:
        assert w["effective_from"] == w["effective_to"]


def test_current_csv_is_reconciliation_target_only(anchors, reconstruction):
    assert anchors["current_csv_reconciliation_target"]["role"] == "reconciliation_only_not_authority"
    assert anchors["current_csv_reconciliation_target"]["emits_intervals"] is False
    anchor_dates = {a["as_of"] for a in anchors["snapshot_anchors"]}
    for iv in reconstruction["intervals"]:
        assert iv["effective_from"] in anchor_dates


def test_unresolved_rows_are_disclosed_not_repaired(reconstruction, anchors):
    unresolved = {
        (a["as_of"], r["symbol"])
        for a in anchors["snapshot_anchors"] for r in a["unresolved_rows"]
    }
    assert unresolved == {("2024-03-28", "EPIGRAL"), ("2025-03-28", "CIEINDIA"),
                          ("2026-09-30", "JSWDULUX")}
    for date, _ in unresolved:
        comp = reconstruction["anchor_completeness"][date]
        assert comp["completeness"] == "incomplete_unresolved_rows"
        assert date in reconstruction["coverage"]["unresolved_dates"]


def test_available_at_uses_cbm_calendar_rule(reconstruction, anchors):
    basis = reconstruction["available_at_basis"]
    assert basis["phase_value"] == 10
    for src in basis["calendar_sources"]:
        path = ROOT / src["path"]
        assert path.exists() and sha256(path) == src["sha256"]
    holidays: dict[str, str] = {}
    for year in range(2022, 2027):
        cal = json.loads((ROOT / "data" / "raw" / "calendar" / f"trading-holidays-{year}.json")
                         .read_text(encoding="utf-8"))
        import datetime as dt
        for e in cal["CBM"]:
            d = dt.datetime.strptime(e["tradingDate"], "%d-%b-%Y").date()
            holidays[d.isoformat()] = e.get("description") or "trading_holiday"
    for a in anchors["snapshot_anchors"]:
        avail = a["available_at"]["date"]
        assert avail > a["as_of"]
        if a["anchor_kind"] == "official_snapshot":
            disclosure = a["as_of"]
        else:
            disclosure = max([a["as_of"]] + [ev["publication_date"] for ev in a["evidence_documents"]])
        d = _next_day(disclosure)
        while d.isoformat() != avail:
            assert d.weekday() >= 5 or d.isoformat() in holidays
            d = d + __import__("datetime").timedelta(days=1)
        assert d.weekday() < 5 and avail not in holidays
        for sk in a["available_at_skipped_dates"]:
            assert sk["date"] > a["as_of"] and sk["date"] < avail


def _next_day(iso: str):
    import datetime as dt
    y, m, d = (int(x) for x in iso.split("-"))
    return dt.date.fromordinal(dt.date(y, m, d).toordinal() + 1)


def test_refusals_are_valid_and_actionable():
    ref = load("refusals.json")
    assert isinstance(ref["refusals"], list) and ref["refusals"]
    assert ref["contract_pass_claimed"] is False
    for e in ref["refusals"]:
        assert e["url"].startswith("https://")
        assert e["reason"]
        assert e["http_status"] is not None or e["transport_error"]
        assert e["artifact_admitted"] is False


def test_readme_counts_match_lane_state(anchors, provenance, reconstruction):
    text = (LANE / "README.md").read_text(encoding="utf-8")
    table = {}
    for m in re.finditer(r"^\| (.+?) \| (.+?) \|$", text, re.M):
        table[m.group(1).strip()] = m.group(2).strip().replace(",", "")
    refusals = load("refusals.json")
    import glob as _glob
    pdfs_on_disk = len(_glob.glob(str(LANE / "sources" / "press_releases" / "*.pdf")))
    texts_on_disk = len(_glob.glob(str(LANE / "text" / "press_releases" / "*.txt")))
    final_csv = _symbols(LANE / "snapshots" / f"NIFTY500_{anchors['coverage']['last_date']}.csv")
    expected = {
        "Retained official PDFs (all in provenance)": str(pdfs_on_disk),
        "Retained extracted texts": str(texts_on_disk),
        "Notices with explicit NIFTY 500 sections": str(
            load("changes.json")["counts"]["notices_with_nifty500_sections"]),
        "Exact-date anchors": str(anchors["anchor_counts"]["total_constituent_anchor_count"]),
        "One-day membership intervals": str(reconstruction["counts"]["interval_count"]),
        "One-day coverage windows": str(reconstruction["counts"]["coverage_window_count"]),
        "Chain-end symbols (2026-09-30)": str(len(final_csv)),
        "Unresolved exclusion rows": "3",
        "Refusal entries": str(len(refusals["refusals"])),
        "Coverage complete": "**no**",
    }
    assert pdfs_on_disk == len(provenance["documents"]) == texts_on_disk
    for label, value in expected.items():
        assert table.get(label) == value, (label, table.get(label), value)
    assert "contract pass claimed:** no" in text.lower() or "Contract pass claimed:** no" in text
    # the README status must not claim WORK_IN_PROGRESS nor contract PASS
    assert "WORK_IN_PROGRESS" not in text
    for status in (anchors, reconstruction, refusals, provenance):
        assert status["status"] == "FINAL_EVIDENCE"
        assert status.get("contract_pass_claimed", False) is False


def test_inventory_hashes_every_lane_file():
    inv = load("inventory.json")
    assert inv["file_count"] == len(inv["files"])
    for entry in inv["files"]:
        path = LANE / entry["path"]
        assert path.exists(), entry["path"]
        assert sha256(path) == entry["sha256"], entry["path"]
        assert path.stat().st_size == entry["bytes"]
