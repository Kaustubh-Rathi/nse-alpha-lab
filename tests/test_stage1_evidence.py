"""Batch 6: revision-bound evidence, the 29-row status model and the skip policy.

These tests are authored from the hand-written oracle
``tests/golden/report_matrix_cases_v6.json``. They never derive an expected
contract status by running the implementation.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
import subprocess

import pytest

from nse_pipeline import stage1_evidence as evidence

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT / "tests" / "golden" / "report_matrix_cases_v6.json"


def _golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _cases_for_components(status: str = "PASS") -> list[dict]:
    cases = []
    for contract in evidence.CONTRACTS:
        for module in contract.component_modules:
            cases.append({
                "id": f"tests.{module}::test_component",
                "classname": f"tests.{module}",
                "name": "test_component",
                "status": status,
            })
    return cases


def _passing_rows() -> list[dict]:
    return [{
        "id": contract.contract_id.lower(),
        "contract_id": contract.contract_id,
        "title": contract.title,
        "contract_status": "CONTRACT_PASS",
        "component_status": contract.component_status,
        "component_result": "PASS" if contract.component_modules else "NO COMPONENT",
        "component_modules": list(contract.component_modules),
        "test_results": [],
        "blocking_reason": "",
        "required_proof": "",
    } for contract in evidence.CONTRACTS]


REGRESSION = {"tests": 10, "pass": 10, "fail": 0, "skip": 0,
              "mandatory_skips": 0, "optional_skips": 0}
CLEAN_INVENTORY = {"state": "CLEAN", "integrity_ok": True, "checked_files": 1}


def _binding(**overrides):
    values = dict(
        tested_commit="a" * 40,
        head_commit="a" * 40,
        worktree_state="clean",
        dirty_fingerprint_value=None,
        source_fingerprint_value="s" * 64,
        expected_source_fingerprint="s" * 64,
        golden_hashes={"tests/golden/x.json": "g" * 64},
        expected_golden_hashes={"tests/golden/x.json": "g" * 64},
        inventory_sha256="i" * 64,
        expected_inventory_sha256="i" * 64,
    )
    values.update(overrides)
    return evidence.revision_binding(**values)


# ---------------------------------------------------------------------------
# The 29-row contract matrix
# ---------------------------------------------------------------------------

def test_contract_matrix_has_exactly_29_unique_mandatory_rows():
    assert evidence.SCHEMA_VERSION == 2
    assert len(evidence.CONTRACTS) == 29
    ids = [contract.contract_id for contract in evidence.CONTRACTS]
    assert len(set(ids)) == 29
    assert evidence.MANDATORY_CONTRACTS == frozenset(ids)
    assert "P0-E2E" in evidence.MANDATORY_CONTRACTS


def test_contract_counts_are_the_documented_19_9_0_0_1_vector():
    golden = _golden()
    counts = evidence.contract_counts(evidence.CONTRACTS)
    for status, expected in golden["expected_counts"].items():
        assert counts[status] == expected, status
    assert counts["CONTRACT_PASS"] == 19
    assert counts["CONTRACT_PARTIAL"] == 9
    assert counts["CONTRACT_FAIL"] == 0
    assert counts["CONTRACT_NOT_IMPLEMENTED"] == 0
    assert counts["CONTRACT_BLOCKED"] == 1
    assert counts["COMPONENT_IMPLEMENTED"] == 0
    assert counts["COMPONENT_VERIFIED"] == 0
    assert sum(golden["expected_counts"].values()) == 29


def test_contract_matrix_matches_the_hand_authored_golden():
    golden = _golden()
    actual = [{
        "contract_id": contract.contract_id,
        "title": contract.title,
        "contract_status": contract.contract_status,
        "component_status": contract.component_status,
        "component_modules": list(contract.component_modules),
    } for contract in evidence.CONTRACTS]
    expected = [{
        "contract_id": row["contract_id"],
        "title": row["title"],
        "contract_status": row["contract_status"],
        "component_status": row["component_status"],
        "component_modules": row["component_modules"],
    } for row in golden["contracts"]]
    assert actual == expected


def test_policies_pass_is_bound_to_the_owner_approved_trim_evidence():
    policies = next(c for c in evidence.CONTRACTS if c.contract_id == "P0-POLICIES")
    assert policies.contract_status == "CONTRACT_PASS"
    assert policies.blocking_reason == ""
    assert "test_accounting_trims" in policies.component_modules
    assert "test_accounting_observed_session_trims" in policies.component_modules
    assert "explicit-target" in policies.required_proof


def test_every_component_module_is_a_real_test_module_and_sizing_is_corrected():
    for contract in evidence.CONTRACTS:
        for module in contract.component_modules:
            assert (ROOT / "tests" / f"{module}.py").is_file(), (contract.contract_id, module)
    sizing = next(c for c in evidence.CONTRACTS if c.contract_id == "P0-SIZING")
    assert "test_accounting_sizing" in sizing.component_modules
    assert "test_accounting_signal_sizing_clause_contract" in sizing.component_modules
    assert "test_accounting_core" not in sizing.component_modules
    assert "test_accounting_fees" not in sizing.component_modules


def test_component_status_counts_match_the_matrix():
    golden = _golden()
    counts = {"COMPONENT_VERIFIED": 0, "COMPONENT_IMPLEMENTED": 0, "NO_COMPONENT": 0}
    for contract in evidence.CONTRACTS:
        if contract.component_status is None:
            counts["NO_COMPONENT"] += 1
        else:
            counts[contract.component_status] += 1
    assert counts == golden["expected_component_status_counts"]
    assert counts == {
        "COMPONENT_VERIFIED": 28, "COMPONENT_IMPLEMENTED": 1, "NO_COMPONENT": 0}
    assert sum(counts.values()) == 29


def test_validate_contract_rows_rejects_duplicate_missing_extra_and_nonpass_rows():
    rows = [{"contract_id": c.contract_id, "contract_status": "CONTRACT_PASS"}
            for c in evidence.CONTRACTS]
    assert evidence.validate_contract_rows(rows) == []
    assert evidence.validate_contract_rows(rows + rows[:1])
    assert evidence.validate_contract_rows(rows[:-1])
    assert evidence.validate_contract_rows(rows + [{"contract_id": "P0-EXTRA",
                                                    "contract_status": "CONTRACT_PASS"}])
    failing = [dict(row) for row in rows]
    failing[0]["contract_status"] = "CONTRACT_FAIL"
    assert any("P0-CASH" in reason for reason in evidence.validate_contract_rows(failing))
    assert evidence.validate_contract_rows(failing, require_pass=False) == []
    unknown = [dict(row) for row in rows]
    unknown[0]["contract_status"] = "PASS"
    assert evidence.validate_contract_rows(unknown, require_pass=False)


# ---------------------------------------------------------------------------
# Component rows never authorize a contract
# ---------------------------------------------------------------------------

def test_component_rows_cover_all_29_contracts_including_e2e():
    rows = evidence.component_rows(_cases_for_components())
    assert len(rows) == 29
    assert [row["contract_id"] for row in rows] == [c.contract_id for c in evidence.CONTRACTS]
    e2e = next(row for row in rows if row["contract_id"] == "P0-E2E")
    assert e2e["contract_status"] == "CONTRACT_PARTIAL"
    assert e2e["component_status"] == "COMPONENT_IMPLEMENTED"
    assert e2e["component_result"] == "PASS"
    assert e2e["component_modules"] == (
        "test_stage1_production_runner_contracts", "test_stage1_production_e2e",
        "test_accounting_observed_session_phase30", "test_accounting_target_composition")


def test_component_rows_report_no_component_for_a_synthetic_unmapped_contract():
    no_component = replace(
        next(c for c in evidence.CONTRACTS if c.contract_id == "P0-E2E"),
        component_modules=(), component_status=None)
    row = evidence.component_rows([], contracts=(no_component,))[0]
    assert row["component_status"] is None
    assert row["component_result"] == "NO COMPONENT"
    assert row["component_modules"] == ()


def test_component_verified_pass_never_changes_a_nonpass_contract():
    rows = evidence.component_rows(_cases_for_components())
    lifecycle = next(row for row in rows if row["contract_id"] == "P0-LIFECYCLE")
    assert lifecycle["component_status"] == "COMPONENT_VERIFIED"
    assert lifecycle["component_result"] == "PASS"
    assert lifecycle["contract_status"] == "CONTRACT_PARTIAL"
    assert not evidence.integration_authorized(rows, REGRESSION)
    reasons = evidence.authorization_reasons(
        rows, REGRESSION, binding=_binding(), inventory=CLEAN_INVENTORY)
    assert any("P0-LIFECYCLE" in reason for reason in reasons)


def test_component_result_distinguishes_fail_skip_missing_and_no_component():
    failed = evidence.component_rows(_cases_for_components("FAIL"))
    skipped = evidence.component_rows(_cases_for_components("SKIP"))
    missing = evidence.component_rows([])
    assert next(r for r in failed if r["contract_id"] == "P0-CASH")["component_result"] == "FAIL"
    assert next(r for r in skipped if r["contract_id"] == "P0-CASH")["component_result"] == "SKIP"
    assert next(r for r in missing if r["contract_id"] == "P0-CASH")["component_result"] == "NOT IMPLEMENTED"
    assert next(r for r in missing if r["contract_id"] == "P0-E2E")["component_result"] == "NOT IMPLEMENTED"


def test_component_rows_expose_the_declared_test_modules():
    rows = {row["contract_id"]: row for row in evidence.component_rows([])}
    assert rows["P0-SIZING"]["component_modules"] == (
        "test_accounting_sizing", "test_accounting_signal_sizing_clause_contract")
    assert rows["P0-CONSERVATION"]["component_modules"] == (
        "test_accounting_reconciliation", "test_accounting_replay",
        "test_accounting_external_flows", "test_accounting_observed_session_external_flows",
        "test_accounting_replay_independence")
    assert rows["P0-GATE"]["component_modules"] == ("test_accounting_report",)
    assert rows["P0-VERIFY"]["component_modules"] == (
        "test_verification_isolation", "test_inventory_verification")


def test_e2e_is_mandatory_so_28_passing_rows_still_cannot_authorize():
    rows = _passing_rows()
    assert evidence.integration_authorized(rows, REGRESSION)
    rows[-1]["contract_status"] = "CONTRACT_NOT_IMPLEMENTED"
    assert not evidence.integration_authorized(rows, REGRESSION)
    reasons = evidence.authorization_reasons(
        rows, REGRESSION, binding=_binding(), inventory=CLEAN_INVENTORY)
    assert any("P0-E2E" in reason for reason in reasons)


# ---------------------------------------------------------------------------
# Authorization reasons
# ---------------------------------------------------------------------------

def test_authorization_reasons_are_empty_only_for_a_complete_pass_binding():
    assert evidence.authorization_reasons(
        _passing_rows(), REGRESSION, binding=_binding(), inventory=CLEAN_INVENTORY) == []


def test_authorization_reasons_close_for_missing_or_nonpass_rows():
    rows = _passing_rows()
    rows[0]["contract_status"] = "CONTRACT_PARTIAL"
    assert any("P0-CASH" in reason for reason in evidence.authorization_reasons(
        rows, REGRESSION, binding=_binding(), inventory=CLEAN_INVENTORY))
    assert any("missing" in reason.lower() for reason in evidence.authorization_reasons(
        rows[:-1], REGRESSION, binding=_binding(), inventory=CLEAN_INVENTORY))


def test_authorization_reasons_close_for_inventory_integrity_and_zero_test_regression():
    bad_inventory = {"state": "UNEXPECTED_CHANGE", "integrity_ok": False}
    reasons = evidence.authorization_reasons(
        _passing_rows(), REGRESSION, binding=_binding(), inventory=bad_inventory)
    assert any("inventory" in reason.lower() for reason in reasons)
    zero = dict(REGRESSION, tests=0, **{"pass": 0})
    assert any("zero tests" in reason.lower() for reason in evidence.authorization_reasons(
        _passing_rows(), zero, binding=_binding(), inventory=CLEAN_INVENTORY))
    failed = dict(REGRESSION, fail=1, **{"pass": 9})
    assert any("failure" in reason.lower() for reason in evidence.authorization_reasons(
        _passing_rows(), failed, binding=_binding(), inventory=CLEAN_INVENTORY))


def test_mandatory_skip_blocks_authorization_but_optional_pdf_skip_is_disclosed():
    mandatory = evidence.skip_policy([
        {"id": "tests.test_accounting_core::test_ok", "status": "PASS"},
        {"id": "tests.test_accounting_core::test_required", "status": "SKIP"},
    ], ROOT / "verification" / "skip_policy.json")
    assert mandatory["mandatory_skips"] == 1
    assert mandatory["pass"] == 1
    reasons = evidence.authorization_reasons(
        _passing_rows(), dict(REGRESSION, skip=1, **{"pass": 9}),
        binding=_binding(), inventory=CLEAN_INVENTORY, skip_report=mandatory)
    assert any("mandatory skip" in reason.lower() for reason in reasons)

    optional = evidence.skip_policy([
        {"id": "tests.test_ratios::RatioParseTests::test_pdf_roundtrip_if_pypdf_available",
         "status": "SKIP"},
        {"id": "tests.test_accounting_core::test_ok", "status": "PASS"},
    ], ROOT / "verification" / "skip_policy.json")
    assert optional["optional_skips"] == 1
    assert optional["mandatory_skips"] == 0
    assert optional["pass"] == 1
    assert optional["optional_skip_details"][0]["dependency"] == "pdf"
    assert evidence.authorization_reasons(
        _passing_rows(), dict(REGRESSION, skip=1, **{"pass": 9}),
        binding=_binding(), inventory=CLEAN_INVENTORY, skip_report=optional) == []


# ---------------------------------------------------------------------------
# Revision binding
# ---------------------------------------------------------------------------

def test_revision_binding_accepts_a_clean_matching_evidence_set():
    binding = _binding()
    assert binding["ok"] is True
    assert binding["reasons"] == []
    assert binding["commit_matches_head"] is True


def test_revision_binding_rejects_junit_from_another_commit():
    binding = _binding(head_commit="b" * 40)
    assert binding["ok"] is False
    assert any("tested_commit" in reason or "head" in reason.lower() for reason in binding["reasons"])


def test_revision_binding_allows_only_a_proven_evidence_only_descendant():
    allowed = _binding(
        head_commit="b" * 40,
        allow_evidence_only_descendant=True,
        changed_paths=["verification/stage1/batch4/component-results.json"],
    )
    assert allowed["ok"] is True
    assert allowed["evidence_only_descendant"] is True
    not_allowed = _binding(
        head_commit="b" * 40,
        allow_evidence_only_descendant=True,
        changed_paths=["src/nse_pipeline/stage1_evidence.py"],
    )
    assert not_allowed["ok"] is False


def test_revision_binding_requires_a_fingerprint_for_a_dirty_tree():
    dirty = _binding(worktree_state="dirty", dirty_fingerprint_value="d" * 64)
    assert dirty["ok"] is True
    missing = _binding(worktree_state="dirty", dirty_fingerprint_value=None)
    assert missing["ok"] is False
    assert any("dirty" in reason.lower() for reason in missing["reasons"])


def test_revision_binding_rejects_source_golden_and_inventory_hash_mismatches():
    assert _binding(expected_source_fingerprint="x" * 64)["ok"] is False
    assert _binding(expected_golden_hashes={"tests/golden/x.json": "x" * 64})["ok"] is False
    assert _binding(expected_inventory_sha256="x" * 64)["ok"] is False


def test_source_fingerprint_is_stable_and_ignores_verification_and_pycache(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("alpha", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("beta", encoding="utf-8")
    (tmp_path / "verification").mkdir()
    (tmp_path / "verification" / "evidence.json").write_text("{}", encoding="utf-8")
    (tmp_path / "src" / "__pycache__").mkdir()
    (tmp_path / "src" / "__pycache__" / "a.pyc").write_bytes(b"cache")

    first = evidence.source_fingerprint(tmp_path)
    assert len(first) == 64
    (tmp_path / "verification" / "evidence.json").write_text('{"new": true}', encoding="utf-8")
    assert evidence.source_fingerprint(tmp_path) == first
    (tmp_path / "tests" / "test_a.py").write_text("changed", encoding="utf-8")
    assert evidence.source_fingerprint(tmp_path) != first


def test_golden_sha256_hashes_every_golden_json(tmp_path):
    golden = tmp_path / "tests" / "golden"
    golden.mkdir(parents=True)
    (golden / "a.json").write_bytes(b"{}\n")
    (golden / "b.json").write_bytes(b"[]\n")
    (golden / "ignore.txt").write_bytes(b"x")
    hashes = evidence.golden_sha256(tmp_path)
    assert sorted(hashes) == ["tests/golden/a.json", "tests/golden/b.json"]
    assert hashes["tests/golden/a.json"] == hashlib.sha256(b"{}\n").hexdigest()


def test_dirty_fingerprint_ignores_verification_and_is_reproducible(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("alpha", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "batch4@example.invalid")
    _git(tmp_path, "config", "user.name", "Batch 4")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")

    assert evidence.dirty_fingerprint(tmp_path) == ("clean", None)
    (tmp_path / "src" / "a.py").write_text("changed", encoding="utf-8")
    state, fingerprint = evidence.dirty_fingerprint(tmp_path)
    assert state == "dirty"
    assert len(fingerprint) == 64
    (tmp_path / "verification").mkdir()
    (tmp_path / "verification" / "evidence.json").write_text("{}", encoding="utf-8")
    assert evidence.dirty_fingerprint(tmp_path) == ("dirty", fingerprint)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_new.py").write_text("new", encoding="utf-8")
    second_state, second_fingerprint = evidence.dirty_fingerprint(tmp_path)
    assert second_state == "dirty"
    assert second_fingerprint != fingerprint


# ---------------------------------------------------------------------------
# Skip policy
# ---------------------------------------------------------------------------

def test_skip_policy_file_contains_the_five_optional_pdf_tests():
    policy = json.loads((ROOT / "verification" / "skip_policy.json").read_text(encoding="utf-8"))
    assert policy["schema_version"] == 1
    assert policy["default"] == "MANDATORY"
    matches = [rule["match"] for rule in policy["rules"]]
    assert matches == [
        "tests.test_pdf_extract::ExtractionTests::test_text_extracted_and_page_citation",
        "tests.test_pdf_extract::ExtractionTests::test_scanned_no_text_flagged_never_inferred",
        "tests.test_pdf_extract::EndToEndTests::test_run_produces_text_ratios_and_manifest",
        "tests.test_ratios::RatioParseTests::test_pdf_roundtrip_if_pypdf_available",
        "tests.test_scheme_attachments::ExtractionAndOcrTests::test_real_pdf_in_zip_per_page_citations_when_dependency_available",
    ]
    assert all(rule["classification"] == "OPTIONAL_DEPENDENCY" for rule in policy["rules"])
    assert all(rule["dependency"] == "pdf" for rule in policy["rules"])


def test_skip_policy_scenarios_match_the_hand_authored_golden():
    policy = json.loads((ROOT / "verification" / "skip_policy.json").read_text(encoding="utf-8"))
    for scenario in _golden()["skip_scenarios"]:
        result = evidence.skip_policy(scenario["cases"], policy)
        for key, expected in scenario["expected"].items():
            assert result[key] == expected, (scenario["name"], key)


def test_unmatched_skip_defaults_to_mandatory():
    result = evidence.skip_policy([
        {"id": "tests.test_accounting_core::test_unlisted", "status": "SKIP"},
    ], {"schema_version": 1, "default": "MANDATORY", "rules": []})
    assert result["mandatory_skips"] == 1
    assert result["optional_skips"] == 0
    assert result["pass"] == 0


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
