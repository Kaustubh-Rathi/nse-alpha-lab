"""The version-2 report must derive declared rows and never authorize non-PASS contracts."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from nse_pipeline.stage1_evidence import CONTRACTS, MANDATORY_CONTRACTS

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "accounting_report", ROOT / "scripts" / "report_stage1_accounting.py")
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)

REGRESSION = {"tests": 1, "pass": 1, "fail": 0, "skip": 0,
              "mandatory_skips": 0, "optional_skips": 0}


def component_cases(status: str = "PASS") -> list[dict]:
    cases = []
    for contract in CONTRACTS:
        for module in contract.component_modules:
            cases.append({"id": f"tests.{module}::test_component",
                          "classname": f"tests.{module}",
                          "name": "test_component", "status": status})
    return cases


def write_junit(path: Path, cases: list[dict]) -> Path:
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    for case in cases:
        classname, name = case["id"].split("::", 1)
        element = ET.SubElement(suite, "testcase", classname=classname, name=name)
        if case["status"] == "FAIL":
            ET.SubElement(element, "failure")
        elif case["status"] == "SKIP":
            ET.SubElement(element, "skipped")
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    return path


def write_inventory(path: Path, *, state: str = "AUTHORIZED_EXCEPTION",
                    integrity_ok: bool = True, sha: str = "i" * 64) -> Path:
    path.write_text(json.dumps({
        "state": state,
        "integrity_ok": integrity_ok,
        "inventory_sha256": sha,
        "checked_files": 1,
        "unexpected_changes": [],
        "errors": [],
    }), encoding="utf-8")
    return path


def build(tmp_path: Path, monkeypatch, *, head: str = "a" * 40,
          tested_commit: str = "a" * 40, cases: list[dict] | None = None, **kwargs) -> dict:
    cases = cases if cases is not None else component_cases()
    focused = write_junit(tmp_path / "focused.xml", cases)
    regression = write_junit(tmp_path / "regression.xml", cases)
    inventory = write_inventory(tmp_path / "inventory.json", sha=kwargs.pop("inventory_sha", "i" * 64))
    monkeypatch.setattr(report, "head_commit", lambda root: head)
    return report.build_report(
        focused, regression, inventory,
        skip_policy_path=ROOT / "verification" / "skip_policy.json",
        tested_commit=tested_commit,
        root=tmp_path,
        **kwargs,
    )


def test_report_emits_schema_v2_with_29_rows_and_a_closed_gate(tmp_path, monkeypatch):
    result = build(tmp_path, monkeypatch)
    assert result["schema_version"] == 2
    rows = result["mandatory_contracts"]
    assert len(rows) == 29
    assert [row["contract_id"] for row in rows] == [c.contract_id for c in CONTRACTS]
    assert "P0-E2E" in {row["contract_id"] for row in rows}
    assert result["authorization"]["status"] == "CLOSED"
    assert result["authorization"]["reasons"]
    assert result["overall_p0_gate"] == "CLOSED"
    assert result["integration_authorized"] is False
    assert result["strategy_returns_generated"] is False
    assert result["mandatory_families"]["P0-E2E"] == "CONTRACT_PARTIAL"
    e2e = next(row for row in rows if row["contract_id"] == "P0-E2E")
    assert e2e["component_status"] == "COMPONENT_IMPLEMENTED"
    assert e2e["component_modules"] == (
        "test_stage1_production_runner_contracts", "test_stage1_production_e2e",
        "test_accounting_observed_session_phase30", "test_accounting_target_composition")
    assert e2e["component_result"] == "PASS"
    assert set(result["mandatory_families"]) == MANDATORY_CONTRACTS
    for key in ("tested_commit", "head_at_verification", "worktree_state",
                "dirty_fingerprint", "source_fingerprint", "test_selection",
                "test_results", "regression", "golden_sha256", "inventory_sha256",
                "environment", "verification_scope", "legacy_compatibility"):
        assert key in result, key
    for key in ("tests", "pass", "fail", "skip", "mandatory_skips",
                "optional_skips", "xml_sha256"):
        assert key in result["test_results"], key
    assert result["test_selection"]["paths"]
    assert len(result["test_selection"]["hash"]) == 64


def test_report_rejects_junit_from_another_commit(tmp_path, monkeypatch):
    result = build(tmp_path, monkeypatch, head="a" * 40, tested_commit="b" * 40)
    assert result["authorization"]["status"] == "CLOSED"
    assert any("tested_commit" in reason or "head" in reason.lower()
               for reason in result["authorization"]["reasons"])


def test_report_requires_dirty_fingerprint_for_dirty_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "dirty_fingerprint", lambda root: ("dirty", None))
    result = build(tmp_path, monkeypatch)
    assert result["authorization"]["status"] == "CLOSED"
    assert any("dirty" in reason.lower() for reason in result["authorization"]["reasons"])


def test_report_requires_current_source_and_inventory_hashes(tmp_path, monkeypatch):
    stale_source = build(tmp_path, monkeypatch, expected_source="x" * 64)
    assert any("source" in reason.lower() for reason in stale_source["authorization"]["reasons"])
    stale_inventory = build(tmp_path, monkeypatch, expected_inventory="x" * 64)
    assert any("inventory" in reason.lower()
               for reason in stale_inventory["authorization"]["reasons"])


def test_report_accepts_a_matching_expected_binding(tmp_path, monkeypatch):
    source = None
    result = build(tmp_path, monkeypatch)
    source = result["source_fingerprint"]
    matching = build(tmp_path, monkeypatch, expected_source=source,
                     expected_inventory=result["inventory_sha256"])
    assert not any("source fingerprint" in reason.lower()
                   for reason in matching["authorization"]["reasons"])


def test_report_maps_sizing_budget_to_sizing_tests_only(tmp_path, monkeypatch):
    sizing_modules = {
        "tests.test_accounting_sizing",
        "tests.test_accounting_signal_sizing_clause_contract",
    }
    all_cases = component_cases()
    without_sizing = build(
        tmp_path, monkeypatch,
        cases=[case for case in all_cases if case["classname"] not in sizing_modules])
    row = next(r for r in without_sizing["mandatory_contracts"]
               if r["contract_id"] == "P0-SIZING")
    assert row["component_modules"] == (
        "test_accounting_sizing", "test_accounting_signal_sizing_clause_contract")
    assert row["component_result"] == "NOT IMPLEMENTED"

    without_clause = build(
        tmp_path, monkeypatch,
        cases=[case for case in all_cases
               if case["classname"] != "tests.test_accounting_signal_sizing_clause_contract"])
    row = next(r for r in without_clause["mandatory_contracts"]
               if r["contract_id"] == "P0-SIZING")
    assert row["component_result"] == "NOT IMPLEMENTED"

    without_base_sizing = build(
        tmp_path, monkeypatch,
        cases=[case for case in all_cases
               if case["classname"] != "tests.test_accounting_sizing"])
    row = next(r for r in without_base_sizing["mandatory_contracts"]
               if r["contract_id"] == "P0-SIZING")
    assert row["component_result"] == "NOT IMPLEMENTED"

    complete = build(tmp_path, monkeypatch)
    row = next(r for r in complete["mandatory_contracts"]
               if r["contract_id"] == "P0-SIZING")
    assert row["component_result"] == "PASS"
    assert row["contract_status"] == "CONTRACT_PASS"


def test_report_component_verified_never_authorizes_a_nonpass_contract(tmp_path, monkeypatch):
    result = build(tmp_path, monkeypatch)
    dividend = next(r for r in result["mandatory_contracts"] if r["contract_id"] == "P0-DIVIDEND")
    assert dividend["component_status"] == "COMPONENT_VERIFIED"
    assert dividend["component_result"] == "PASS"
    assert dividend["contract_status"] == "CONTRACT_PARTIAL"
    assert result["authorization"]["status"] == "CLOSED"
    assert result["overall_p0_gate"] == "CLOSED"


def test_report_names_only_real_test_modules(tmp_path, monkeypatch):
    result = build(tmp_path, monkeypatch)
    for row in result["mandatory_contracts"]:
        for module in row["component_modules"]:
            assert (ROOT / "tests" / f"{module}.py").is_file(), (row["contract_id"], module)


def test_junit_failure_and_skip_remain_nonpassing(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text("""<testsuites><testsuite>
<testcase classname="tests.test_accounting_core" name="ok"/>
<testcase classname="tests.test_accounting_core" name="bad"><failure/></testcase>
<testcase classname="tests.test_accounting_core" name="skip"><skipped/></testcase>
</testsuite></testsuites>""")
    cases = report.junit(path)
    assert report.summary(cases) == {"tests": 3, "pass": 1, "fail": 1, "skip": 1}
    rows = report.component_rows(cases)
    assert next(r for r in rows if r["contract_id"] == "P0-CASH")["component_result"] == "FAIL"
    core_only = [c for c in cases if c["status"] == "PASS"]
    rows = report.component_rows(core_only)
    assert next(r for r in rows if r["contract_id"] == "P0-CASH")["component_result"] == "NOT IMPLEMENTED"
    assert not report.integration_authorized(rows, report.summary(cases))


def test_report_cli_preserves_legacy_flags_and_adds_batch4_flags():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "report_stage1_accounting.py"), "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    for flag in ("--focused", "--regression", "--inventory", "--out",
                 "--skip-policy", "--tested-commit", "--expected-source",
                 "--expected-inventory", "--allow-evidence-only-descendant"):
        assert flag in result.stdout, flag


def test_report_refuses_output_under_data(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "report_stage1_accounting.py"),
         "--out", str(ROOT / "data" / "component-results.json")],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert "production data" in result.stderr.lower()
