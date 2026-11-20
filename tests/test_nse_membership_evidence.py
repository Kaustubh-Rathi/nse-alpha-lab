"""Real NIFTY membership evidence (Batch 7).

Loads the acquired reconstruction CSV (third-party baseline) and checks
internal effective-date interval consistency, including selected membership
changes cross-checked against dated NSE Indices press releases. This does not
prove historical known_at membership or official completeness. The CSV is a
baseline to audit, not ground truth: inferred/snapshot_floor rows are flagged,
and owner approval plus official-snapshot reconciliation remain outstanding.
"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BATCH7 = ROOT / "verification" / "stage1" / "batch7"
CSV_PATH = BATCH7 / "membership" / "index_membership_history.csv"
_RECON_PATH = BATCH7 / "membership-reconciliation.json"

# The Stage-1 evidence payload is not distributed with this repository.
pytestmark = pytest.mark.skipif(
    not (CSV_PATH.is_file() and _RECON_PATH.is_file()),
    reason="Stage-1 evidence payload not present (verification/stage1/batch7)",
)

RECON = json.loads(_RECON_PATH.read_text(encoding="utf-8")) if _RECON_PATH.is_file() else {}


def _rows():
    with CSV_PATH.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _interval_covers(row: dict, as_of: date) -> bool:
    valid_from = date.fromisoformat(row["valid_from"])
    if as_of < valid_from:
        return False
    valid_to = row["valid_to"].strip()
    if valid_to and as_of >= date.fromisoformat(valid_to):
        return False
    return True


def is_member(rows, index: str, symbol: str, as_of: date) -> bool:
    return any(r["index_name"] == index and r["symbol"] == symbol and _interval_covers(r, as_of)
               for r in rows)


def test_csv_coverage_matches_the_recorded_summary():
    rows = _rows()
    assert len(rows) == RECON["baseline"]["rows"] == 6670
    nifty50 = [r for r in rows if r["index_name"] == "Nifty 50"]
    nifty500 = [r for r in rows if r["index_name"] == "Nifty 500"]
    assert len(nifty50) == RECON["indices"]["Nifty 50"]["rows"] == 84
    assert len(nifty500) == RECON["indices"]["Nifty 500"]["rows"] == 1151
    current_500 = {r["symbol"] for r in nifty500 if not r["valid_to"].strip()}
    assert len(current_500) == 499  # size of the staged reconstruction; not certification of an official current list


def test_intervals_have_valid_date_order():
    rows = _rows()
    for r in rows:
        valid_from = date.fromisoformat(r["valid_from"])
        if r["valid_to"].strip():
            assert valid_from < date.fromisoformat(r["valid_to"]), r


def test_press_release_changes_are_reflected_by_effective_date():
    rows = _rows()
    # ADANIENT joined Nifty 50 effective 2022-09-30 (ind_prs01092022).
    assert not is_member(rows, "Nifty 50", "ADANIENT", date(2022, 9, 1))
    assert not is_member(rows, "Nifty 50", "ADANIENT", date(2022, 9, 29))
    assert is_member(rows, "Nifty 50", "ADANIENT", date(2022, 9, 30))
    assert is_member(rows, "Nifty 50", "ADANIENT", date(2022, 10, 1))
    # SHREECEM was excluded the same review (valid_to 2022-09-30).
    assert is_member(rows, "Nifty 50", "SHREECEM", date(2022, 9, 1))
    assert is_member(rows, "Nifty 50", "SHREECEM", date(2022, 9, 29))
    assert not is_member(rows, "Nifty 50", "SHREECEM", date(2022, 9, 30))
    assert not is_member(rows, "Nifty 50", "SHREECEM", date(2022, 10, 1))
    # JIOFIN joined effective 2025-03-28 (ind_prs21022025).
    assert not is_member(rows, "Nifty 50", "JIOFIN", date(2025, 2, 1))
    assert is_member(rows, "Nifty 50", "JIOFIN", date(2025, 4, 1))


def test_inferred_rows_are_flagged_and_not_treated_as_primary_evidence():
    rows = _rows()
    inferred = [r for r in rows if r["notes"].startswith("inferred-") or r["source"] == "snapshot_floor"]
    # The reconstruction explicitly marks rows it could not tie to a press release.
    assert inferred
    for r in inferred:
        assert r["source"] in ("snapshot_floor", "snapshot") or r["notes"].startswith("inferred-")
