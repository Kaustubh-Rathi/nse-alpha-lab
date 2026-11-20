"""Verification must not overwrite pre-existing production artifacts."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from nse_pipeline import stage1_evidence as evidence

ROOT = Path(__file__).resolve().parents[1]
DATA_README_SHA256 = "230470d34cd6d495406be50879f06b31c7e015f7b16880f96f59cc1828f821d1"


def _module(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def _coherent_closed_report(tmp_path: Path):
    """Build a real-tree report whose only closure reason is the non-PASS matrix."""
    report = _module("report_stage1_accounting")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite><testcase classname="tests.test_accounting_report" '
        'name="test_report_contract"/></testsuite></testsuites>', encoding="utf-8")
    inventory_sha = hashlib.sha256(
        (ROOT / "data/manifests/dataset_hashes.json").read_bytes()).hexdigest()
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({
        "state": "AUTHORIZED_EXCEPTION",
        "integrity_ok": True,
        "inventory_sha256": inventory_sha,
        "checked_files": 8153,
        "unexpected_changes": [],
        "errors": [],
    }), encoding="utf-8")
    return report.build_report(
        junit, junit, inventory,
        skip_policy_path=ROOT / "verification" / "skip_policy.json",
        tested_commit=_head(), root=ROOT)


def _historical_artifact_hashes() -> dict[str, str]:
    files = []
    for batch in ("batch1", "batch2", "batch3"):
        files.extend(path for path in (ROOT / "verification" / "stage1" / batch).rglob("*")
                     if path.is_file())
    files.extend(path for path in (ROOT / "verification").glob("accounting-*") if path.is_file())
    files.extend(path for path in (ROOT / "verification").glob("foundation-*") if path.is_file())
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files}


def test_evidence_checker_accepts_a_correctly_closed_report_but_requires_authorization_on_request(tmp_path):
    checker = _module("check_stage1_evidence")
    report = _coherent_closed_report(tmp_path)
    assert report["authorization"]["status"] == "CLOSED"
    accepted = checker.check_report(report, root=ROOT)
    assert accepted["ok"] is True, accepted["errors"]
    required = checker.check_report(report, root=ROOT, require_authorization=True)
    assert required["ok"] is False
    assert any("authorization" in error.lower() for error in required["errors"])


def test_evidence_checker_rejects_an_overclaim_and_a_stale_binding(tmp_path):
    checker = _module("check_stage1_evidence")
    report = _coherent_closed_report(tmp_path)
    overclaim = json.loads(json.dumps(report))
    overclaim["authorization"] = {"status": "OPEN", "reasons": []}
    overclaim["overall_p0_gate"] = "OPEN"
    result = checker.check_report(overclaim, root=ROOT)
    assert result["ok"] is False
    assert any("authorization" in error.lower() or "overclaim" in error.lower()
               for error in result["errors"])

    stale = json.loads(json.dumps(report))
    stale["source_fingerprint"] = "0" * 64
    result = checker.check_report(stale, root=ROOT)
    assert result["ok"] is False
    assert any("source" in error.lower() for error in result["errors"])


def test_evidence_checker_rejects_status_promotion_or_component_substitution(tmp_path):
    checker = _module("check_stage1_evidence")
    report = _coherent_closed_report(tmp_path)
    promoted = json.loads(json.dumps(report))
    row = next(row for row in promoted["mandatory_contracts"]
               if row["contract_id"] == "P0-DIVIDEND")
    row["contract_status"] = "CONTRACT_PASS"
    promoted["mandatory_families"]["P0-DIVIDEND"] = "CONTRACT_PASS"
    result = checker.check_report(promoted, root=ROOT)
    assert result["ok"] is False
    assert any("declared" in error.lower() or "contract status" in error.lower()
               for error in result["errors"])

    substituted = json.loads(json.dumps(report))
    row = next(row for row in substituted["mandatory_contracts"]
               if row["contract_id"] == "P0-SIZING")
    row["component_modules"] = ["test_accounting_core"]
    row["component_status"] = "COMPONENT_VERIFIED"
    row["component_result"] = "PASS"
    result = checker.check_report(substituted, root=ROOT)
    assert result["ok"] is False
    assert any("declared" in error.lower() or "component" in error.lower()
               for error in result["errors"])


def test_evidence_tools_do_not_modify_historical_artifacts(tmp_path):
    checker = _module("check_stage1_evidence")
    before = _historical_artifact_hashes()
    assert before
    report = _coherent_closed_report(tmp_path)
    checker.check_report(report, root=ROOT)
    for script in ("verify_stage1_inventory", "report_stage1_accounting",
                   "check_stage1_evidence"):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / f"{script}.py"), "--help"],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
    assert _historical_artifact_hashes() == before


def test_data_readme_is_tracked_and_hash_unchanged():
    checker = _module("check_stage1_evidence")
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "data/README.md"],
                             cwd=ROOT, capture_output=True, text=True)
    assert tracked.returncode == 0, tracked.stderr
    assert hashlib.sha256((ROOT / "data/README.md").read_bytes()).hexdigest() == DATA_README_SHA256
    assert checker.check_data_prerequisite(ROOT) == []


def test_data_readme_can_be_declared_in_a_prerequisite_manifest_instead(tmp_path):
    checker = _module("check_stage1_evidence")
    readme = tmp_path / "data" / "README.md"
    readme.parent.mkdir(parents=True)
    readme.write_bytes(b"prerequisite bytes")
    assert checker.check_data_prerequisite(tmp_path)
    manifest = tmp_path / "verification" / "dataset_prerequisites.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({
        "required": [{"path": "data/README.md",
                      "sha256": hashlib.sha256(b"prerequisite bytes").hexdigest()}],
    }), encoding="utf-8")
    assert checker.check_data_prerequisite(tmp_path, manifest_path=manifest) == []


def test_write_guard_preserves_existing_artifact_and_allows_reads(tmp_path):
    from nse_pipeline.verification import protect_artifacts

    production = tmp_path / "production"
    production.mkdir()
    artifact = production / "dataset_manifest.json"
    artifact.write_bytes(b"original authoritative bytes")
    with protect_artifacts([production]):
        assert artifact.read_bytes() == b"original authoritative bytes"
        with pytest.raises(PermissionError):
            artifact.write_text("unexpected verification output")
        with pytest.raises(PermissionError):
            artifact.unlink()
        with pytest.raises(PermissionError):
            (production / "new.json").write_text("new output")
        (tmp_path / "isolated.json").write_text("allowed")
    assert artifact.read_bytes() == b"original authoritative bytes"
    artifact.write_text("guard is scoped, not permanent")


@pytest.mark.skipif(os.name != "nt", reason="Native no-write file sharing is Windows-specific")
def test_native_subprocess_cannot_bypass_existing_file_protection(tmp_path):
    from nse_pipeline.verification import protect_artifacts

    artifact = tmp_path / "dataset_manifest.json"
    artifact.write_text("original")
    with protect_artifacts([tmp_path]):
        # -I deliberately bypasses PYTHONPATH/sitecustomize: the parent-held
        # Windows file handle still denies native or child-process writes.
        result = subprocess.run([sys.executable, "-I", "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('overwrite')", str(artifact)],
            capture_output=True, text=True, timeout=30)
        assert result.returncode != 0
        assert "PermissionError" in result.stderr
    assert artifact.read_text() == "original"


def test_original_dataset_cli_path_is_blocked_before_production_outputs(tmp_path):
    root = Path(__file__).resolve().parents[1]
    manifest = root / "data/processed/dataset/dataset_manifest.json"
    before = hashlib.sha256(manifest.read_bytes()).hexdigest()
    env = dict(os.environ, PYTHONPATH=str(root / "src"), NSE_DATA_DIR=str(root / "data"))
    result = subprocess.run([sys.executable, str(root / "src/get_data.py"), "dataset"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "protected production artifacts" in result.stderr
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == before


def test_dataset_cli_rejects_production_root_before_reading_large_inputs(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(root / "src"), NSE_DATA_DIR=str(root / "data"))
    code = """
import sys, pathlib, os
import get_data
protected = pathlib.Path(sys.argv[1]).resolve()
def no_dataset_reads(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        if pathlib.Path(os.fsdecode(args[0])).resolve().is_relative_to(protected):
            raise AssertionError('Dataset read before verification root rejection')
sys.addaudithook(no_dataset_reads)
sys.argv = ['get_data.py', 'dataset']
get_data.main()
"""
    result = subprocess.run([sys.executable, "-c", code, str(root / "data")], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "protected production artifacts" in result.stderr, result.stderr
    assert "Dataset read before" not in result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Native no-write file sharing is Windows-specific")
def test_native_duckdb_copy_cannot_overwrite_a_protected_file(tmp_path):
    import duckdb
    from nse_pipeline.verification import protect_artifacts

    artifact = tmp_path / "sentinel.csv"
    artifact.write_bytes(b"authoritative")
    with protect_artifacts([tmp_path]), duckdb.connect() as db:
        with pytest.raises(duckdb.IOException):
            db.execute("COPY (SELECT 1) TO ? (FORMAT CSV)", [str(artifact)])
    assert artifact.read_bytes() == b"authoritative"
