"""Pins for the Batch 9 production document evidence lane.

Covers: catalog/disk digest agreement, URL and date completeness, refusal
records, README/inventory count consistency, and the documented behaviour of
build_catalog.py. No contract promotion is asserted anywhere: the lane's
catalog keeps contract_pass_claimed == False.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LANE = ROOT / "verification" / "stage1" / "batch9" / "documents"
SOURCES = LANE / "sources"
CATALOG = LANE / "documents.json"
REFUSALS = LANE / "refusals.json"
INVENTORY = LANE / "inventory.json"
README = LANE / "README.md"
BUILD = LANE / "build_catalog.py"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def disk_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@pytest.fixture(scope="module")
def catalog() -> dict:
    assert CATALOG.is_file(), "catalog missing: documents.json"
    return load(CATALOG)


@pytest.fixture(scope="module")
def refusals() -> dict:
    assert REFUSALS.is_file(), "refusals file missing: refusals.json"
    return load(REFUSALS)


@pytest.fixture(scope="module")
def build_catalog_module():
    spec = importlib.util.spec_from_file_location("batch9_build_catalog", BUILD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# (a) catalog validity and digest custody ------------------------------------

def test_documents_json_is_valid_json_with_expected_shape(catalog: dict):
    assert catalog["schema_version"] == 2
    assert catalog["kind"] == "nse_stage1_document_evidence_catalog"
    assert catalog["status"] == "PARTIAL_EVIDENCE"
    assert catalog["contract_pass_claimed"] is False
    assert set(catalog["contracts"]) == {"P0-DIVIDEND", "P0-SCOPED-ADJ", "P0-LIFECYCLE"}
    assert isinstance(catalog["documents"], list) and catalog["documents"]


def test_every_retained_lane_file_is_recorded_once(catalog: dict):
    on_disk = sorted(
        p.relative_to(SOURCES).as_posix()
        for p in SOURCES.rglob("*")
        if p.is_file() and not p.name.endswith(".meta.json")
    )
    recorded = sorted(doc["local_path"] for doc in catalog["documents"] if doc["local_path"])
    assert recorded == on_disk
    assert len(recorded) == len(set(recorded))


def test_every_lane_document_digest_matches_disk(catalog: dict):
    for doc in catalog["documents"]:
        if doc["custody"] == "lane":
            path = SOURCES / doc["local_path"]
            assert re.fullmatch(r"[0-9a-f]{64}", doc["sha256"]), doc["document_id"]
            assert disk_sha256(path) == doc["sha256"], doc["document_id"]
            assert doc["bytes"] == path.stat().st_size, doc["document_id"]


def test_every_external_document_digest_matches_cache(catalog: dict):
    external = [d for d in catalog["documents"]
                if d["custody"] == "external_acquisition_cache"]
    assert external, "catalog must bind the oversized filings it references"
    for doc in external:
        path = Path(doc["external_cache_path"])
        assert path.is_file(), f"acquisition cache byte missing for {doc['document_id']}"
        assert disk_sha256(path) == doc["sha256"], doc["document_id"]


def test_meta_shas_that_exist_match_disk_bytes(catalog: dict):
    for doc in catalog["documents"]:
        if doc["custody"] != "lane":
            continue
        path = SOURCES / doc["local_path"]
        for candidate in (path.with_name(path.name + ".meta.json"),
                          path.with_name(path.stem + ".meta.json")):
            if candidate.is_file():
                meta = load(candidate)
                if meta.get("sha256"):
                    assert meta["sha256"] == doc["sha256"], doc["document_id"]
                break


# (b) URL and date completeness -------------------------------------------------

def test_every_document_carries_url_and_dates(catalog: dict):
    for doc in catalog["documents"]:
        assert isinstance(doc.get("url"), str) and doc["url"].startswith("http"), doc["document_id"]
        assert doc.get("published_at") or doc.get("retrieved_at"), doc["document_id"]
        assert doc.get("available_at"), doc["document_id"]


# (c) refusal records -----------------------------------------------------------

def test_retained_refusal_entries_are_bound(refusals: dict, catalog: dict):
    by_id = {d["document_id"]: d for d in catalog["documents"]}
    for entry in refusals["retained_refusal_bytes"]:
        assert entry["url"], entry["id"]
        assert entry["reason"], entry["id"]
        doc = by_id[entry["id"]]
        assert doc["sha256"] == entry["sha256"]
        path = SOURCES / entry["local_path"]
        assert path.is_file() and disk_sha256(path) == entry["sha256"], entry["id"]


def test_fetch_refusals_carry_url_and_reason(refusals: dict):
    assert refusals["fetch_refusals"], "this session's failed re-acquisitions must be recorded"
    for entry in refusals["fetch_refusals"]:
        assert entry["url"].startswith("http"), entry["id"]
        assert entry["reason"], entry["id"]
        assert entry["attempted_at"], entry["id"]


def test_unresolved_items_carry_named_attempts(refusals: dict):
    ids = [item["id"] for item in refusals["unresolved_items"]]
    assert len(ids) == len(set(ids))
    for item in refusals["unresolved_items"]:
        assert item["status"].startswith("UNRESOLVED"), item["id"]
        assert item["attempts"], item["id"]
        for attempt in item["attempts"]:
            assert attempt["url"], (item["id"], "attempt url")
            assert attempt["reason"], (item["id"], "attempt reason")


def test_no_item_is_promoted_by_the_lane(refusals: dict, catalog: dict):
    assert catalog["contract_pass_claimed"] is False
    readme_text = README.read_text(encoding="utf-8")
    assert "No contract row is promoted" in readme_text
    for item in refusals["unresolved_items"]:
        assert not item["status"].startswith("RESOLVED"), item["id"]
    for item in refusals["resolved_items"]:
        assert item["status"] == "RESOLVED"
        for doc_id in item["evidence_document_ids"]:
            doc = next(d for d in catalog["documents"] if d["document_id"] == doc_id)
            assert doc["url"] and doc["sha256"]


# (d) README and inventory counts -------------------------------------------------

README_COUNT_LABELS = {
    "Retained source files under sources/": "retained_source_files",
    "Catalog documents (total)": "catalog_documents_total",
    "Catalog documents (lane custody)": "catalog_lane_documents",
    "Catalog documents (external acquisition cache)": "catalog_external_documents",
    "Retained refusal bytes": "retained_refusal_bytes",
    "Fetch refusals recorded": "fetch_refusals_recorded",
    "Resolved items": "resolved_items",
    "Unresolved items": "unresolved_items",
}


def actual_counts(catalog: dict, refusals: dict) -> dict:
    files = sum(1 for p in SOURCES.rglob("*")
                if p.is_file() and not p.name.endswith(".meta.json"))
    return {
        "retained_source_files": files,
        "catalog_documents_total": catalog["counts"]["documents_total"],
        "catalog_lane_documents": catalog["counts"]["lane_documents"],
        "catalog_external_documents": catalog["counts"]["external_acquisition_cache"],
        "retained_refusal_bytes": len(refusals["retained_refusal_bytes"]),
        "fetch_refusals_recorded": len(refusals["fetch_refusals"]),
        "resolved_items": len(refusals["resolved_items"]),
        "unresolved_items": len(refusals["unresolved_items"]),
    }


def test_readme_counts_match_the_files(catalog: dict, refusals: dict):
    text = README.read_text(encoding="utf-8")
    actual = actual_counts(catalog, refusals)
    for label, key in README_COUNT_LABELS.items():
        match = re.search(rf"^- {re.escape(label)}: (\d+)$", text, re.MULTILINE)
        assert match, f"README count line missing for {label!r}"
        assert int(match.group(1)) == actual[key], f"README count wrong for {label!r}"


def test_inventory_json_matches_the_files(catalog: dict, refusals: dict):
    inventory = load(INVENTORY)
    actual = actual_counts(catalog, refusals)
    for key, value in actual.items():
        assert inventory["counts"][key] == value, key
    assert sorted(inventory["unresolved_item_ids"]) == sorted(
        i["id"] for i in refusals["unresolved_items"])
    assert sorted(inventory["resolved_item_ids"]) == sorted(
        i["id"] for i in refusals["resolved_items"])


def test_catalog_counts_fields_match_documents(catalog: dict):
    docs = catalog["documents"]
    assert catalog["counts"]["documents_total"] == len(docs)
    assert catalog["counts"]["lane_documents"] == sum(
        1 for d in docs if d["custody"] == "lane")
    assert catalog["counts"]["external_acquisition_cache"] == sum(
        1 for d in docs if d["custody"] == "external_acquisition_cache")


# (e) build_catalog.py behaviour ----------------------------------------------------

def test_build_document_binds_circular_urls(build_catalog_module):
    for stem, record in build_catalog_module.CIRCULARS.items():
        doc = build_catalog_module.build_document(SOURCES / "nse_circulars" / f"{stem}.pdf")
        assert doc["url"] == record["url"], stem
        assert doc["published_at"] == record["published_at"], stem
        assert doc["retrieved_at"] == record["retrieved_at"], stem
        assert "exchange_circular" in doc["roles"] and "lifecycle" in doc["roles"], stem


def test_meta_for_handles_both_naming_conventions(build_catalog_module):
    via_stem = build_catalog_module.meta_for(SOURCES / "company_filings" / "TRENT_BONUS_BM_20260422.pdf")
    via_name = build_catalog_module.meta_for(SOURCES / "company_filings" / "5PAISA_RIGHTS_20180717.zip")
    assert via_stem.get("url") and via_name.get("url")
    assert build_catalog_module.meta_for(SOURCES / "nse_circulars" / "CML77365.pdf") == {}


def test_publication_for_falls_back_to_filename_date(build_catalog_module):
    published, precision, basis = build_catalog_module.publication_for(
        "SOME_DOC_20240601", "SOME_DOC_20240601.pdf", {})
    assert (published, precision, basis) == ("2024-06-01", "date", "filename_date")
    published, precision, basis = build_catalog_module.publication_for(
        "MCX_NOT_A_DATE_X", "MCX_NOT_A_DATE_X.pdf", {})
    assert (published, precision, basis) == (None, None, None)


def test_build_external_matches_catalog_entries(build_catalog_module, catalog: dict):
    external = {d["document_id"]: d for d in catalog["documents"]
                if d["custody"] == "external_acquisition_cache"}
    assert set(external) == set(build_catalog_module.EXTERNAL)
    for doc_id, path in build_catalog_module.EXTERNAL.items():
        rebuilt = build_catalog_module.build_external(doc_id, path)
        assert rebuilt == external[doc_id], doc_id


def test_catalog_rebuild_is_stable(catalog: dict):
    before = sorted((d["document_id"], d["sha256"]) for d in catalog["documents"])
    proc = subprocess.run(
        [sys.executable, str(BUILD)],
        cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr
    after_doc = load(CATALOG)
    after = sorted((d["document_id"], d["sha256"]) for d in after_doc["documents"])
    assert after == before
    assert after_doc["counts"] == catalog["counts"]
