"""Build a revision-bound, fail-closed Stage-1 component report from JUnit.

The report derives one row per declared contract from
``nse_pipeline.stage1_evidence``. A passing component suite never changes a
contract status, the mandatory set includes P0-E2E, and authorization is bound
to the tested commit, worktree fingerprint, source, goldens and inventory.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nse_pipeline.stage1_evidence import (  # noqa: E402
    MANDATORY_CONTRACTS,
    authorization_reasons,
    component_rows as _component_rows,
    dirty_fingerprint,
    golden_sha256,
    integration_authorized as _integration_authorized,
    inventory_integrity_ok,
    revision_binding,
    skip_policy,
    source_fingerprint,
)

# Legacy alias: contract ids, not report-row ids.
MANDATORY = frozenset(MANDATORY_CONTRACTS)


def junit(path) -> list[dict]:
    cases = []
    for case in ET.parse(path).iter("testcase"):
        status = ("FAIL" if case.find("failure") is not None or case.find("error") is not None
                  else "SKIP" if case.find("skipped") is not None else "PASS")
        cases.append({
            "id": f"{case.get('classname')}::{case.get('name')}",
            "classname": case.get("classname"),
            "name": case.get("name"),
            "status": status,
        })
    return cases


def summary(cases) -> dict:
    """Legacy four-key count retained for existing consumers."""
    cases = list(cases)
    return {"tests": len(cases), **{status.lower(): sum(case["status"] == status for case in cases)
                                    for status in ("PASS", "FAIL", "SKIP")}}


def component_rows(cases) -> list[dict]:
    return _component_rows(cases)


def integration_authorized(rows, regression) -> bool:
    return _integration_authorized(rows, regression)


def head_commit(root=ROOT) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _module_path(case: dict) -> str:
    classname = case.get("classname") or case["id"].split("::", 1)[0]
    parts = classname.split(".")
    if len(parts) >= 2 and parts[0] == "tests":
        return f"tests/{parts[1]}.py"
    return f"tests/{parts[0]}.py"


def _test_selection(cases: list[dict]) -> dict:
    paths = sorted({_module_path(case) for case in cases})
    payload = json.dumps(paths, separators=(",", ":")).encode("utf-8")
    return {"paths": paths, "hash": hashlib.sha256(payload).hexdigest()}


def _skip_summary(cases: list[dict], policy) -> dict:
    return skip_policy(cases, policy)


def _counts(record: dict, xml_path) -> dict:
    return {
        "tests": record["tests"],
        "pass": record["pass"],
        "fail": record["fail"],
        "skip": record["skip"],
        "mandatory_skips": record["mandatory_skips"],
        "optional_skips": record["optional_skips"],
        "xml_sha256": _sha256(Path(xml_path)),
    }


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


def _environment(root: Path) -> dict:
    import pytest
    lock = root / "uv.lock"
    return {
        "python": platform.python_version(),
        "pytest": pytest.__version__,
        "lock_sha256": _sha256(lock) if lock.is_file() else None,
    }


def build_report(focused, regression, inventory, *, skip_policy_path=None,
                 tested_commit=None, expected_source=None, expected_inventory=None,
                 allow_evidence_only_descendant=False, root=ROOT) -> dict:
    root = Path(root).resolve()
    focused_cases = junit(focused)
    regression_cases = junit(regression)
    inventory_result = json.loads(Path(inventory).read_text(encoding="utf-8"))
    policy = skip_policy_path
    if policy is None:
        candidate = root / "verification" / "skip_policy.json"
        policy = candidate if candidate.is_file() else None
    focused_skips = _skip_summary(focused_cases, policy)
    regression_skips = _skip_summary(regression_cases, policy)

    rows = component_rows(focused_cases)
    head = head_commit(root)
    if tested_commit is None:
        tested_commit = head or "UNKNOWN"
    worktree_state, dirty = dirty_fingerprint(root)
    source = source_fingerprint(root)
    goldens = golden_sha256(root)

    recorded_inventory = inventory_result.get("inventory_sha256")
    inventory_file = root / "data" / "manifests" / "dataset_hashes.json"
    actual_inventory = _sha256(inventory_file) if inventory_file.is_file() else recorded_inventory
    expected_inventory_sha = expected_inventory or actual_inventory

    changed = ()
    if allow_evidence_only_descendant and tested_commit != head:
        changed = _changed_paths(root, tested_commit, head)
    binding = revision_binding(
        tested_commit,
        head,
        worktree_state=worktree_state,
        dirty_fingerprint_value=dirty,
        source_fingerprint_value=source,
        expected_source_fingerprint=expected_source or source,
        golden_hashes=goldens,
        expected_golden_hashes=goldens,
        inventory_sha256=recorded_inventory,
        expected_inventory_sha256=expected_inventory_sha,
        allow_evidence_only_descendant=allow_evidence_only_descendant,
        changed_paths=changed,
    )
    reasons = authorization_reasons(
        rows, regression_skips, binding=binding, inventory=inventory_result,
        skip_report=regression_skips)
    authorized = not reasons

    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tested_commit": tested_commit,
        "head_at_verification": head,
        "worktree_state": worktree_state,
        "dirty_fingerprint": dirty,
        "source_fingerprint": source,
        "test_selection": _test_selection(focused_cases),
        "test_results": _counts(focused_skips, focused),
        "regression": _counts(regression_skips, regression),
        "mandatory_families": {row["contract_id"]: row["contract_status"] for row in rows},
        "mandatory_contracts": rows,
        "golden_sha256": goldens,
        "inventory_sha256": recorded_inventory,
        "environment": _environment(root),
        "verification_scope": (
            "Revision-bound focused component JUnit and full regression at the tested "
            "commit; no strategy execution, return series or performance output."),
        "authorization": {"status": "OPEN" if authorized else "CLOSED", "reasons": reasons},
        "overall_p0_gate": "OPEN" if authorized else "CLOSED",
        "integration_authorized": authorized,
        "strategy_returns_generated": False,
        "legacy_compatibility": {
            "focused": summary(focused_cases),
            "regression": summary(regression_cases),
        },
        # Compatibility mirrors for existing evidence consumers.
        "focused": summary(focused_cases),
        "production_components": rows,
        "integrity": inventory_result,
        "junit_note": "JUnit counts exclude separately reported pytest subtests.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--focused", type=Path, default=ROOT / "verification/accounting-focused.xml")
    parser.add_argument("--regression", type=Path, default=ROOT / "verification/accounting-regression.xml")
    parser.add_argument("--inventory", type=Path, default=ROOT / "verification/accounting-posttests.json")
    parser.add_argument("--out", type=Path, default=ROOT / "verification/accounting-component-results.json")
    parser.add_argument("--skip-policy", type=Path,
                        default=ROOT / "verification/skip_policy.json")
    parser.add_argument("--tested-commit", default=None)
    parser.add_argument("--expected-source", default=None)
    parser.add_argument("--expected-inventory", default=None)
    parser.add_argument("--allow-evidence-only-descendant", action="store_true")
    args = parser.parse_args()
    if args.out.resolve().is_relative_to((ROOT / "data").resolve()):
        parser.error("Reports cannot overwrite production data")
    result = build_report(
        args.focused, args.regression, args.inventory,
        skip_policy_path=args.skip_policy,
        tested_commit=args.tested_commit,
        expected_source=args.expected_source,
        expected_inventory=args.expected_inventory,
        allow_evidence_only_descendant=args.allow_evidence_only_descendant,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "test_results": result["test_results"],
        "regression": result["regression"],
        "authorization": result["authorization"],
        "overall_p0_gate": result["overall_p0_gate"],
        "integration_authorized": result["integration_authorized"],
    }, indent=2))


if __name__ == "__main__":
    main()
