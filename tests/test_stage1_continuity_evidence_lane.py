"""Pins for the Batch 8 continuity evidence lane (P0-CONTINUITY).

Covers: headline counts (59 boundaries / 50 refusals / 1,113 press-release
digests), honest coverage disclosure (coverage.complete is False, the proven
effective interval, and the two unproven intervals disclosed instead of
fabricated), refusal-ledger validity, mutual consistency of the six lane
deliverables, and the digest-inventory precondition the lane generator relies
on: it refuses to write when a stored document digest no longer matches disk,
so every pinned digest must recompute from the retained corpus.

No contract promotion is asserted anywhere; the lane keeps
status == INCOMPLETE and contract_pass_claimed == False.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LANE = ROOT / "verification" / "stage1" / "batch8" / "continuity"

PROVEN_FROM = "2018-02-05"
PROVEN_TO = "2026-09-07"
EXPECTED_UNPROVEN = [
    ("2018-01-01", "2018-02-04"),
    ("2026-09-08", "2026-11-16"),
]
REFUSAL_REASON_CODES = {
    "already_listed_independent_identity",
    "no_bound_document",
    "positional_alias_only",
    "rename_effective_date_unproven",
    "share_class_cancellation",
    "successor_not_listed_identity",
    "successor_symbol_unbound",
    "successor_unnamed",
    "voluntary_delisting",
}


def load(name: str):
    return json.loads((LANE / name).read_text(encoding="utf-8"))


def disk_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@pytest.fixture(scope="module")
def boundaries() -> dict:
    return load("boundaries.json")


@pytest.fixture(scope="module")
def refusals() -> dict:
    return load("refusals.json")


@pytest.fixture(scope="module")
def documents() -> dict:
    return load("documents.json")


@pytest.fixture(scope="module")
def provenance() -> dict:
    return load("provenance.json")


@pytest.fixture(scope="module")
def catalog(documents: dict) -> dict:
    return {doc["document_id"]: doc for doc in documents["documents"]}


# (1) status honesty -------------------------------------------------------------

def test_all_deliverables_declare_incomplete_without_contract_pass(
    boundaries, refusals, documents, provenance
):
    for payload in (boundaries, refusals, documents, provenance):
        assert payload["status"] == "INCOMPLETE"
        assert payload["contract_pass_claimed"] is False
        assert payload["publisher"] == "NSE Indices Limited"
    readme = (LANE / "README.md").read_text(encoding="utf-8")
    assert "Status: INCOMPLETE" in readme
    assert "No contract pass is claimed" in readme


# (2) headline counts ------------------------------------------------------------

def test_headline_counts_are_pinned(boundaries, refusals, documents):
    assert len(boundaries["boundaries"]) == 59
    assert len(refusals["refused_candidates"]) == 50
    assert len(documents["documents"]) == 1113
    assert len(documents["reference_documents"]) == 5
    assert boundaries["counts"]["proven_boundaries"] == 59
    assert boundaries["counts"]["by_event_kind"] == {"merger": 30, "demerger": 29}
    assert boundaries["counts"]["refused_candidates"] == 50
    assert refusals["counts"]["refused_candidates"] == 50
    assert documents["counts"]["press_release_documents"] == 1113


# (3) coverage disclosure --------------------------------------------------------

def test_coverage_is_incomplete_with_the_proven_interval(boundaries):
    cov = boundaries["coverage"]
    assert cov["complete"] is False
    assert "screened subset" in cov["completeness_reason"]
    assert cov["proven_effective_from"] == PROVEN_FROM
    assert cov["proven_effective_to"] == PROVEN_TO
    for row in boundaries["boundaries"]:
        assert PROVEN_FROM <= row["effective_on"] <= PROVEN_TO, row["boundary_id"]


def test_unproven_intervals_are_disclosed_not_fabricated(boundaries):
    unproven = boundaries["coverage"]["unproven_intervals"]
    assert [(iv["from"], iv["to"]) for iv in unproven] == EXPECTED_UNPROVEN
    for iv in unproven:
        assert iv["reason"], (iv["from"], iv["to"])
    # the disclosed gaps must not overlap the proven interval itself
    assert unproven[0]["to"] < PROVEN_FROM
    assert unproven[1]["from"] > PROVEN_TO


def test_symbol_and_isin_change_gaps_stay_recorded(boundaries):
    gaps = boundaries["coverage"]["event_kind_gaps"]
    assert "symbol_change" in gaps and "isin_change" in gaps
    assert {row["event_kind"] for row in boundaries["boundaries"]} == {
        "merger", "demerger"
    }
    readme = (LANE / "README.md").read_text(encoding="utf-8")
    assert "symbol_change" in readme and "isin_change" in readme


# (4) boundary evidence consistency ----------------------------------------------

def test_boundary_evidence_binds_catalog_documents(boundaries, catalog):
    seen = set()
    for row in boundaries["boundaries"]:
        ev = row["evidence"]
        seen.add(row["boundary_id"])
        assert ev["document_id"] in catalog, row["boundary_id"]
        assert ev["content_sha256"] == catalog[ev["document_id"]]["sha256"]
        assert ev["effective_to"] is None
        assert ev["effective_from"] <= row["effective_on"]
        assert ev["published_at"] <= ev["available_at"]["session"]
        assert ev["available_at"]["phase"] == 10
    assert len(seen) == 59


def test_supporting_documents_bind_catalog_documents(boundaries, catalog):
    cited_support = set()
    for row in boundaries["boundaries"]:
        for sup in row["supporting_documents"]:
            assert sup["document_id"] in catalog, (row["boundary_id"], sup["document_id"])
            assert sup["content_sha256"] == catalog[sup["document_id"]]["sha256"]
            cited_support.add(sup["document_id"])
    assert boundaries["counts"]["supporting_documents"] == 15


# (5) refusal ledger validity ------------------------------------------------------

def test_refusals_carry_reason_codes_and_document_references(refusals, catalog):
    without_docs = []
    for row in refusals["refused_candidates"]:
        assert row["refusal_id"]
        assert row["reason"]
        assert row["reason_code"] in REFUSAL_REASON_CODES, row["refusal_id"]
        if not row["documents"]:
            without_docs.append(row["refusal_id"])
        for doc in row["documents"]:
            assert doc["document_id"] in catalog, (row["refusal_id"], doc["document_id"])
            assert doc["sha256"] == catalog[doc["document_id"]]["sha256"]
    # exactly one candidate (the no_bound_document case) has no bound document
    assert len(without_docs) == 1
    assert refusals["counts"]["without_bound_document"] == 1
    assert refusals["counts"]["with_bound_document_reference"] == 49
    assert sum(refusals["counts"]["by_reason"].values()) == 50
    assert set(refusals["counts"]["by_reason"]) == REFUSAL_REASON_CODES


# (6) digest integrity over the stored corpus --------------------------------------

def test_every_stored_document_digest_matches_disk(documents, catalog):
    # This is the precondition the generator and verify_artifacts rely on:
    # a changed stored-PDF digest makes the lane refuse to rebuild.
    assert len(catalog) == 1113
    for doc in documents["documents"]:
        path = LANE / doc["local_path"]
        assert path.is_file(), doc["document_id"]
        assert disk_sha256(path) == doc["sha256"], doc["document_id"]
        assert path.stat().st_size == doc["bytes"], doc["document_id"]


def test_reference_documents_match_disk(documents):
    for ref in documents["reference_documents"]:
        base = LANE if ref["custody"] == "lane" else ROOT
        path = base / ref["local_path"]
        assert path.is_file(), ref["document_id"]
        assert disk_sha256(path) == ref["sha256"], ref["document_id"]


def test_provenance_binds_the_same_corpus_and_error_log(provenance, documents, catalog):
    assert len(provenance["documents"]) == 1113
    for row in provenance["documents"]:
        doc = catalog[row["document_id"]]
        assert row["sha256"] == doc["sha256"], row["document_id"]
        assert row["local_path"] == doc["local_path"], row["document_id"]
    corpus = provenance["corpus"]["publisher_release_index"]
    assert corpus["release_count"] == 1534
    assert corpus["releases_in_requested_interval"] == 1119
    assert provenance["corpus"]["downloaded_pdf_count"] == 1113
    errors = provenance["corpus"]["acquisition_error_log"]
    assert errors["rows"] == 6  # 1119 - 1113 publisher-side gaps
    assert disk_sha256(LANE / errors["path"]) == errors["sha256"]
    for snap_field, digest_field in (
        ("snapshot_path", "snapshot_sha256"),
        ("parsed_index_path", "parsed_index_sha256"),
    ):
        assert disk_sha256(LANE / corpus[snap_field]) == corpus[digest_field], snap_field


# (7) README / reconciliation cross-consistency ------------------------------------

def test_readme_count_table_matches_the_deliverables(boundaries, refusals, documents):
    text = (LANE / "README.md").read_text(encoding="utf-8")
    table = {}
    for m in re.finditer(r"^\| (.+?) \| (.+?) \|$", text, re.M):
        table[m.group(1).strip()] = m.group(2).strip().replace(",", "")
    expected = {
        "Proven boundaries": str(len(boundaries["boundaries"])),
        "Press releases catalogued": str(len(documents["documents"])),
        "Reference documents": str(len(documents["reference_documents"])),
        "Refused candidates": str(len(refusals["refused_candidates"])),
        "Primary boundary documents": str(boundaries["counts"]["primary_boundary_documents"]),
        "Supporting documents (not primary)": str(boundaries["counts"]["supporting_documents"]),
        "Documents cited overall (boundaries + refusals)":
            str(documents["counts"]["cited_documents_total"]),
    }
    for label, value in expected.items():
        assert table.get(label) == value, (label, table.get(label), value)
    assert "2018-02-05 .. 2026-09-07" in text


def test_reconciliation_states_corpus_size_and_the_residual_gaps():
    text = (LANE / "reconciliation.md").read_text(encoding="utf-8")
    assert "**1,534** releases" in text
    assert "1,113" in text
    assert "2018-01-01 .. 2018-02-04" in text
    assert "2026-09-08 .. 2026-11-16" in text
    assert "coverage.complete=false" in text


# (8) hash inventory over the final on-disk state ------------------------------------

def test_inventory_hashes_the_final_lane_state():
    inv = load("inventory.json")
    assert inv["schema_version"] == 1
    assert inv["root"] == "continuity"
    assert inv["file_count"] == len(inv["files"])
    assert inv["file_count"] > 1100  # must cover the extended corpus, not 340
    paths = [row["path"] for row in inv["files"]]
    assert paths == sorted(paths)
    assert "inventory.json" not in paths
    pdfs = [p for p in paths if p.startswith("documents/press_releases/") and p.endswith(".pdf")]
    assert len(pdfs) == 1113
    for row in inv["files"]:
        path = LANE / row["path"]
        assert path.is_file(), row["path"]
        assert disk_sha256(path) == row["sha256"], row["path"]
        assert path.stat().st_size == row["bytes"], row["path"]
