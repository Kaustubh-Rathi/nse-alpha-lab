"""Batch 0 evidence freeze: revision binding, output guards, golden hashes, repro.

These tests exercise the Batch 0 tooling only. They never write under data/,
never modify a golden, and never invoke the pytest suite from inside a script.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLOCK = "2026-11-14T00:00:00+00:00"


def _module(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    assert path.exists(), f"Batch 0 tooling is missing: {path}"
    spec = importlib.util.spec_from_file_location(f"stage1_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_blob_sha256(relative: str) -> str:
    data = subprocess.check_output(["git", "show", f"HEAD:{relative}"], cwd=ROOT)
    return hashlib.sha256(data).hexdigest()


def test_freeze_manifest_binds_commit_and_worktree_state():
    freeze = _module("freeze_stage1_baseline")
    manifest = freeze.build_manifest(ROOT, generated_at=CLOCK)

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    subject = subprocess.check_output(["git", "log", "-1", "--pretty=%s"], cwd=ROOT, text=True).strip()
    assert manifest["schema_version"] == 1
    assert manifest["tested_commit"] == head
    assert manifest["head_subject"] == subject
    assert manifest["worktree_state"] in ("clean", "dirty")
    assert isinstance(manifest["dirty_paths"], list)
    relocation = manifest["audit_relocation"]
    assert relocation["from"] == "QUANT_REPOSITORY_AUDIT.md"
    assert relocation["to"] == "docs/QUANT_REPOSITORY_AUDIT.md"
    assert relocation["source_deleted_in_worktree"] is True
    assert relocation["matches_committed_blob"] is True
    assert relocation["sha256"] == git_blob_sha256("docs/QUANT_REPOSITORY_AUDIT.md")


def test_freeze_refuses_output_under_data(tmp_path):
    freeze = _module("freeze_stage1_baseline")
    reproduce = _module("reproduce_stage1_findings")

    for module in (freeze, reproduce):
        with pytest.raises(ValueError):
            module.check_output(ROOT, ROOT / "data" / "reports" / "manifest.json")
        with pytest.raises(ValueError):
            module.check_output(ROOT, tmp_path)
    allowed = ROOT / "verification" / "stage1" / "baseline-f5e9430"
    assert freeze.check_output(ROOT, allowed / "manifest.json") == allowed / "manifest.json"
    assert reproduce.check_output(ROOT, allowed / "reproduction.json") == allowed / "reproduction.json"


def test_freeze_hashes_existing_goldens_without_changing_them():
    freeze = _module("freeze_stage1_baseline")
    goldens = ("tests/golden/accounting_cases.json", "tests/golden/execution_cases.json",
               "tests/golden/valuation_cases.json")
    before = {name: sha256(ROOT / name) for name in goldens}

    record = freeze.golden_hashes(ROOT)

    for name in goldens:
        assert record[name]["sha256"] == before[name]
        assert record[name]["committed_blob_sha256"] == git_blob_sha256(name)
        assert record[name]["matches_committed_blob"] is True
    assert sha256(ROOT / goldens[0]) == before[goldens[0]]


def test_freeze_records_full_suite_counts_and_skip_names(tmp_path):
    freeze = _module("freeze_stage1_baseline")
    xml = tmp_path / "pytest-full.xml"
    xml.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3" failures="1" errors="0" skipped="1">
  <testcase classname="tests.test_a" name="test_pass"/>
  <testcase classname="tests.test_a" name="test_fail"><failure message="x"/></testcase>
  <testcase classname="tests.test_pdf" name="test_pdf_skip"><skipped message="missing optional dependency"/></testcase>
</testsuite></testsuites>
""", encoding="utf-8")
    counts, skips = freeze.read_junit(xml)
    assert counts == {"tests": 3, "failures": 1, "errors": 0, "skipped": 1, "passed": 1}
    assert skips == ["tests.test_pdf::test_pdf_skip"]
    manifest = freeze.build_manifest(ROOT, generated_at=CLOCK, junit_path=xml)
    assert manifest["test_counts"] == counts
    assert manifest["skip_ids"] == skips


def test_reproduction_records_f1_f2_f3_f5():
    baseline = ROOT / "verification" / "stage1" / "baseline-f5e9430"
    artifact_path = baseline / "reproduction.json"
    assert artifact_path.exists(), f"Batch 0 artifact is missing: {artifact_path}"
    record = json.loads(artifact_path.read_text(encoding="utf-8"))

    assert record["schema_version"] == 1
    assert record["tested_commit"] == "f5e943000764fade242b0482d67db2808991c076"
    assert record["overall_p0_gate"] == "CLOSED"

    index = json.loads((ROOT / "verification" / "stage1" / "index.json").read_text(encoding="utf-8"))
    entries = {entry["path"]: entry for entry in index["artifacts"]}
    frozen = entries["stage1/baseline-f5e9430/reproduction.json"]
    assert frozen["tested_commit"] == record["tested_commit"]
    assert frozen["sha256"] == sha256(artifact_path)

    by_id = {finding["id"]: finding for finding in record["findings"]}
    assert len(record["findings"]) == len(by_id)
    assert set(by_id) == {"F1", "F2", "F3", "F5"}
    for finding in by_id.values():
        assert finding["status"] == "DEFECT_REPRODUCED"
        assert finding["command"]
        assert finding["expected_contract"]
        assert isinstance(finding["observed"], dict) and finding["observed"]
        assert len(finding["artifact_sha256"]) == 64
        int(finding["artifact_sha256"], 16)

    f1 = by_id["F1"]["observed"]
    assert f1["applied_principal_cap_exposed"] is False
    assert f1["buy"]["99.996"]["quantity"] == 10
    assert f1["buy"]["99.996"]["notional"] == "100.00"
    assert f1["sell"]["99.996"]["quantity"] == 10
    assert f1["expected"]["99.996"] == {"applied_cap": "99.99", "quantity": 9, "notional": "90.00"}

    f2 = by_id["F2"]["observed"]
    assert f2["basis_provenance_exposed"] is False
    assert f2["first_price_basis"] == f2["second_price_basis"] == "close"
    assert f2["first_momentum"] != f2["second_momentum"]

    f3 = by_id["F3"]["observed"]
    assert f3["reference_coverage_reason"] == "mixed_date_reference_only"
    assert f3["expected_reference_coverage_reason"] == "complete_current_date"
    assert f3["nav"] == "2000.00"

    f5 = by_id["F5"]["observed"]
    assert f5["claim_due"] is None
    assert f5["confirm_operation"] is False
    assert f5["settle_error"]
    assert f5["expected_lifecycle"] == "accrued_held -> confirmed -> settled (or voided)"
