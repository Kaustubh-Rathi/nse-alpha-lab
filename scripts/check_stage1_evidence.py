"""Read-only validator for a Stage-1 evidence report.

Exit 0 when the report is internally consistent and does not overclaim, even if
the aggregate gate is correctly CLOSED. Exit 1 on inconsistency, overclaim, a
stale/mismatched revision binding, or a missing prerequisite. With
``--require-authorization``, a CLOSED gate is also a failure.

This script never writes a file; redirect stdout if an artifact is needed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nse_pipeline.stage1_evidence import (  # noqa: E402
    CONTRACTS,
    authorization_reasons,
    dirty_fingerprint,
    golden_sha256,
    inventory_integrity_ok,
    revision_binding,
    source_fingerprint,
    validate_contract_rows,
)

DATA_README = "data/README.md"
DATA_README_SHA256 = "230470d34cd6d495406be50879f06b31c7e015f7b16880f96f59cc1828f821d1"
PREREQUISITE_MANIFEST = "verification/dataset_prerequisites.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _head(root: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _tracked(root: Path, relative: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative],
        capture_output=True, text=True)
    return result.returncode == 0


def _changed_paths(root: Path, tested_commit: str, head: str | None) -> list[str]:
    if not tested_commit or not head:
        return []
    try:
        output = subprocess.check_output(
            ["git", "-C", str(root), "diff", "--name-only", f"{tested_commit}..{head}"],
            text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def check_data_prerequisite(root, expected_sha256: str = DATA_README_SHA256,
                            manifest_path=None) -> list[str]:
    """data/README.md must be tracked, or declared with a matching hash."""
    root = Path(root).resolve()
    errors: list[str] = []
    readme = root / DATA_README
    if not readme.is_file():
        return [f"{DATA_README} is missing"]
    actual = _sha256(readme)
    if _tracked(root, DATA_README):
        if actual != expected_sha256:
            errors.append(f"{DATA_README} hash changed: {actual} != {expected_sha256}")
        return errors

    manifest = Path(manifest_path) if manifest_path else root / PREREQUISITE_MANIFEST
    declared = False
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError as error:
            errors.append(f"prerequisite manifest is not valid JSON: {error}")
            payload = {}
        rows = payload.get("required", payload.get("required_paths",
                                                    payload.get("prerequisites", [])))
        for row in rows:
            if isinstance(row, str):
                if row == DATA_README:
                    declared = actual == expected_sha256
            elif isinstance(row, dict) and row.get("path") == DATA_README:
                declared = row.get("sha256", actual) == actual
            if declared:
                break
    if not declared:
        errors.append(f"{DATA_README} is untracked and not declared in a prerequisite manifest")
    return errors


def _arithmetic_errors(name: str, record: dict) -> list[str]:
    errors = []
    tests = record.get("tests")
    passed = record.get("pass")
    failed = record.get("fail")
    skipped = record.get("skip")
    if not all(isinstance(value, int) for value in (tests, passed, failed, skipped)):
        return [f"{name} counts are missing or non-integer"]
    if tests != passed + failed + skipped:
        errors.append(f"{name} counts do not sum: {tests} != {passed}+{failed}+{skipped}")
    mandatory = record.get("mandatory_skips")
    optional = record.get("optional_skips")
    if not isinstance(mandatory, int) or not isinstance(optional, int):
        errors.append(f"{name} mandatory/optional skip counters are missing")
    elif mandatory + optional != skipped:
        errors.append(
            f"{name} skip split does not sum: {skipped} != {mandatory}+{optional}")
    return errors


def check_report(report: dict, *, root=ROOT, require_authorization: bool = False,
                 allow_evidence_only_descendant: bool = False,
                 prerequisite_manifest=None) -> dict:
    root = Path(root).resolve()
    errors: list[str] = []
    checks: list[dict] = []

    def check(name: str, condition: bool, message: str) -> bool:
        checks.append({"check": name, "ok": bool(condition)})
        if not condition:
            errors.append(message)
        return bool(condition)

    check("schema_version", report.get("schema_version") == 2,
          f"schema_version must be 2, got {report.get('schema_version')!r}")

    rows = report.get("mandatory_contracts")
    if not isinstance(rows, list):
        rows = []
        errors.append("mandatory_contracts must be a list")
    row_errors = validate_contract_rows(rows, require_pass=False)
    check("contract_rows", not row_errors, "; ".join(row_errors))

    declared = {contract.contract_id: contract for contract in CONTRACTS}
    declaration_errors = []
    for row in rows:
        contract_id = row.get("contract_id")
        spec = declared.get(contract_id)
        if spec is None:
            continue
        if row.get("contract_status") != spec.contract_status:
            declaration_errors.append(
                f"declared contract status for {contract_id} is {spec.contract_status!r}, "
                f"report says {row.get('contract_status')!r}")
        if row.get("component_status") != spec.component_status:
            declaration_errors.append(
                f"declared component status for {contract_id} is {spec.component_status!r}, "
                f"report says {row.get('component_status')!r}")
        if tuple(row.get("component_modules") or ()) != spec.component_modules:
            declaration_errors.append(
                f"declared component modules for {contract_id} are "
                f"{spec.component_modules!r}, report says {row.get('component_modules')!r}")
    check("declared_contract_matrix", not declaration_errors,
          "; ".join(declaration_errors))

    families = report.get("mandatory_families") or {}
    expected_families = {row.get("contract_id"): row.get("contract_status") for row in rows}
    check("mandatory_families", families == expected_families,
          "mandatory_families does not match the contract rows")

    head = _head(root)
    tested = report.get("tested_commit")
    changed = (_changed_paths(root, tested, head)
               if allow_evidence_only_descendant and tested != head else ())
    source = source_fingerprint(root)
    goldens = golden_sha256(root)
    inventory_file = root / "data" / "manifests" / "dataset_hashes.json"
    actual_inventory = (_sha256(inventory_file) if inventory_file.is_file()
                        else report.get("inventory_sha256"))
    binding = revision_binding(
        tested,
        head,
        worktree_state=report.get("worktree_state"),
        dirty_fingerprint_value=report.get("dirty_fingerprint"),
        source_fingerprint_value=report.get("source_fingerprint"),
        expected_source_fingerprint=source,
        golden_hashes=report.get("golden_sha256") or {},
        expected_golden_hashes=goldens,
        inventory_sha256=report.get("inventory_sha256"),
        expected_inventory_sha256=actual_inventory,
        allow_evidence_only_descendant=allow_evidence_only_descendant,
        changed_paths=changed,
    )
    check("revision_binding", binding["ok"], "; ".join(binding["reasons"]))

    actual_state, actual_dirty = dirty_fingerprint(root)
    if report.get("worktree_state") == "dirty" and actual_state == "dirty":
        check("dirty_fingerprint", actual_dirty == report.get("dirty_fingerprint"),
              "dirty_fingerprint is not reproducible from the current tree")
    elif report.get("worktree_state") == "clean" and actual_state == "dirty":
        check("worktree_state", False,
              "report claims a clean tree but source-relevant paths are dirty")

    test_results = report.get("test_results") or {}
    regression = report.get("regression") or {}
    for name, record in (("test_results", test_results), ("regression", regression)):
        problems = _arithmetic_errors(name, record)
        check(f"{name}_arithmetic", not problems, "; ".join(problems))

    integrity = report.get("integrity")
    if not isinstance(integrity, dict) or "state" not in integrity:
        check("inventory_disclosed", False,
              "report does not disclose the inventory state machine result")
        inventory_ok = False
    else:
        inventory_ok = inventory_integrity_ok(integrity)
        check("inventory_integrity", inventory_ok,
              f"inventory state {integrity.get('state')!r} is not integrity-ok")
        check("inventory_state_bound",
              integrity.get("inventory_sha256") == report.get("inventory_sha256"),
              "inventory state is not bound to the reported inventory_sha256")

    skip_report = {
        "skip": regression.get("skip", 0),
        "mandatory_skips": regression.get("mandatory_skips", 0),
        "optional_skips": regression.get("optional_skips", 0),
    }
    reasons = authorization_reasons(
        rows, regression, binding=binding,
        inventory=integrity if isinstance(integrity, dict) else None,
        skip_report=skip_report)
    expected_status = "OPEN" if not reasons else "CLOSED"
    declared = report.get("authorization") or {}
    check("authorization_follows_rows", declared.get("status") == expected_status,
          f"authorization.status is {declared.get('status')!r}, expected {expected_status!r}")
    if declared.get("status") == "OPEN" and reasons:
        errors.append("authorization overclaims OPEN despite: " + "; ".join(reasons))
    if declared.get("status") == "CLOSED" and not reasons:
        errors.append("authorization is CLOSED but no row/binding reason closes it")
    check("authorization_reasons_disclosed",
          bool(declared.get("reasons")) == (expected_status == "CLOSED"),
          "authorization.reasons is inconsistent with the closing reasons")

    check("overall_p0_gate_matches_authorization",
          report.get("overall_p0_gate") == expected_status,
          "overall_p0_gate must equal the authorization status")
    check("integration_authorized",
          report.get("integration_authorized") is (expected_status == "OPEN"),
          "integration_authorized overclaims or underclaims the authorization status")
    if skip_report["mandatory_skips"]:
        check("no_mandatory_skip_when_open",
              declared.get("status") != "OPEN",
              "a mandatory skip cannot coexist with OPEN authorization")

    for message in check_data_prerequisite(
            root, manifest_path=prerequisite_manifest):
        check("data_prerequisite", False, message)

    if require_authorization and declared.get("status") != "OPEN":
        check("require_authorization", False,
              "authorization is not OPEN (--require-authorization)")

    return {
        "ok": not errors,
        "errors": errors,
        "checks": checks,
        "tested_commit": tested,
        "head_at_verification": head,
        "authorization_status": declared.get("status"),
        "overall_p0_gate": report.get("overall_p0_gate"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path,
                        default=ROOT / "verification/stage1/batch4/component-results.json")
    parser.add_argument("--require-authorization", action="store_true")
    parser.add_argument("--allow-evidence-only-descendant", action="store_true")
    parser.add_argument("--prerequisite-manifest", type=Path, default=None)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    result = check_report(
        report,
        require_authorization=args.require_authorization,
        allow_evidence_only_descendant=args.allow_evidence_only_descendant,
        prerequisite_manifest=args.prerequisite_manifest,
    )
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
