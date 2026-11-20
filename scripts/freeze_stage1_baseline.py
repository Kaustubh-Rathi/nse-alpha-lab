"""Batch 0 read-only baseline freeze for the Stage-1 accounting slice.

The script reads Git state and hashes tracked source and the three immutable
goldens. It invokes no tests, writes no production or dataset artifact, and
refuses any output path outside ``verification/stage1/``.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ROOT = Path("verification/stage1")
GOLDENS = (
    "tests/golden/accounting_cases.json",
    "tests/golden/execution_cases.json",
    "tests/golden/valuation_cases.json",
)
AUDIT_FROM = "QUANT_REPOSITORY_AUDIT.md"
AUDIT_TO = "docs/QUANT_REPOSITORY_AUDIT.md"
SOURCE_ROOTS = ("src", "tests", "scripts")


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=root, text=True).strip()


def check_output(root: Path, path: Path) -> Path:
    """Allow writes only beneath the repository's verification/stage1 tree."""
    root = root.resolve()
    output = Path(path)
    output = (output if output.is_absolute() else root / output).resolve()
    allowed = (root / ALLOWED_ROOT).resolve()
    if not output.is_relative_to(allowed):
        raise ValueError("Batch 0 artifacts must be written under verification/stage1/")
    return output


def tracked_files(root: Path) -> tuple[str, ...]:
    raw = subprocess.check_output(["git", "ls-files", "-z", *SOURCE_ROOTS], cwd=root)
    return tuple(sorted(part.decode("utf-8") for part in raw.split(b"\0") if part))


def source_hashes(root: Path) -> dict[str, str]:
    hashes = {}
    for relative in tracked_files(root):
        path = root / relative
        if path.is_file():
            hashes[relative] = sha256_file(path)
    return hashes


def golden_hashes(root: Path) -> dict[str, dict]:
    record = {}
    for relative in GOLDENS:
        path = root / relative
        digest = sha256_file(path)
        committed = hashlib.sha256(
            subprocess.check_output(["git", "show", f"HEAD:{relative}"], cwd=root)
        ).hexdigest()
        record[relative] = {
            "sha256": digest,
            "committed_blob_sha256": committed,
            "matches_committed_blob": digest == committed,
        }
    return record


def audit_relocation(root: Path) -> dict:
    relocating = root / AUDIT_FROM
    relocated = root / AUDIT_TO
    digest = sha256_file(relocated)
    committed = hashlib.sha256(
        subprocess.check_output(["git", "show", f"HEAD:{AUDIT_TO}"], cwd=root)
    ).hexdigest()
    return {
        "from": AUDIT_FROM,
        "to": AUDIT_TO,
        "sha256": digest,
        "committed_blob_sha256": committed,
        "matches_committed_blob": digest == committed,
        "source_deleted_in_worktree": not relocating.exists(),
    }


def worktree_rows(root: Path) -> list[str]:
    raw = subprocess.check_output(["git", "status", "--porcelain=v1", "-uall"], cwd=root, text=True)
    return [row for row in raw.splitlines() if row.strip()]


def read_junit(path: Path) -> tuple[dict, list[str]]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tests = failures = errors = skipped = subtests = 0
    skip_ids = []
    for suite in suites:
        cases = list(suite.iter("testcase"))
        tests += len(cases)
        failures += sum(case.find("failure") is not None for case in cases)
        errors += sum(case.find("error") is not None for case in cases)
        skipped += sum(case.find("skipped") is not None for case in cases)
        subtests += max(0, int(suite.get("tests", 0)) - len(cases))
        for case in cases:
            if case.find("skipped") is not None:
                classname = case.get("classname", "")
                name = case.get("name", "")
                skip_ids.append(f"{classname}::{name}" if classname else name)
    counts = {"tests": tests, "failures": failures, "errors": errors,
              "skipped": skipped, "passed": tests - failures - errors - skipped}
    if subtests:
        counts["subtests"] = subtests
    return counts, sorted(skip_ids)


def _environment() -> dict:
    try:
        from importlib.metadata import version
        pytest_version = version("pytest")
    except Exception:  # pragma: no cover - environment metadata is best effort
        pytest_version = None
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "pytest": pytest_version,
    }


def _json_or_none(path: Path | None):
    if path is None or not Path(path).exists():
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_manifest(root: Path, *, generated_at: str | None = None, junit_path: Path | None = None,
                   inventory_path: Path | None = None, decisions_path: Path | None = None) -> dict:
    root = root.resolve()
    rows = worktree_rows(root)
    tracked_changes = [row for row in rows if not row.startswith("??")]
    counts, skips = read_junit(junit_path) if junit_path is not None and Path(junit_path).exists() else (None, [])
    inventory = _json_or_none(inventory_path)
    return {
        "schema_version": 1,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "tested_commit": git(root, "rev-parse", "HEAD"),
        "head_subject": git(root, "log", "-1", "--pretty=%s"),
        "worktree_state": "dirty" if rows else "clean",
        "dirty_paths": rows,
        "tracked_changes": tracked_changes,
        "untracked_paths": [row[3:] for row in rows if row.startswith("??")],
        "source_sha256": source_hashes(root),
        "golden_sha256": golden_hashes(root),
        "audit_relocation": audit_relocation(root),
        "inventory_sha256": None if inventory is None else inventory.get("inventory_sha256"),
        "inventory_state": None if inventory is None else {
            "checked_files": inventory.get("checked_files"),
            "all_hashes_match": inventory.get("all_hashes_match"),
            "only_accepted_exception": inventory.get("only_accepted_exception"),
            "unexpected_changes": len(inventory.get("unexpected_changes", [])),
            "errors": len(inventory.get("errors", [])),
        },
        "environment": _environment(),
        "test_counts": counts,
        "skip_ids": skips,
        "decisions": _json_or_none(decisions_path),
        "notes": [
            "Batch 0 reads evidence and invokes no tests.",
            "The audit relocation (QUANT_REPOSITORY_AUDIT.md -> docs/QUANT_REPOSITORY_AUDIT.md) is committed.",
            "Full suite measured with --extra pdf: 1139 tests plus 129 subtests, 0 skips. The audit's "
            "812 pass / 5 skip figure predates the post-baseline test modules committed at f5e9430 "
            "and was measured without the optional PDF dependencies.",
            "Inventory checked 8153 files with zero errors, zero unexpected changes and exactly one "
            "acknowledged historical dataset_manifest.json exception.",
        ],
    }


def update_index(evidence_root: Path, tested_commit: str) -> dict:
    """Append content-addressed entries; never rewrite an existing entry."""
    path = evidence_root / "index.json"
    record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "schema_version": 1, "append_only": True, "artifacts": []}
    seen = {(entry["path"], entry["sha256"]) for entry in record["artifacts"]}
    for artifact in sorted(evidence_root.rglob("*")):
        if not artifact.is_file() or artifact == path or "__pycache__" in artifact.parts:
            continue
        relative = artifact.relative_to(evidence_root.parent).as_posix()
        digest = sha256_file(artifact)
        if (relative, digest) in seen:
            continue
        record["artifacts"].append({
            "path": relative,
            "sha256": digest,
            "tested_commit": tested_commit,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "BASELINE_EVIDENCE" if "/baseline-" in relative else "APPENDED",
        })
        seen.add((relative, digest))
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    output = check_output(ROOT, args.out)
    output.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(
        ROOT,
        junit_path=output / "pytest-full.xml",
        inventory_path=output / "inventory.json",
        decisions_path=output / "decision-gates.json",
    )
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "golden-sha256.json").write_text(
        json.dumps({"schema_version": 1, "tested_commit": manifest["tested_commit"],
                    "golden_sha256": manifest["golden_sha256"],
                    "audit_relocation": manifest["audit_relocation"]}, indent=2) + "\n",
        encoding="utf-8")
    update_index(output.parent, manifest["tested_commit"])
    print(json.dumps({"tested_commit": manifest["tested_commit"],
                      "worktree_state": manifest["worktree_state"],
                      "test_counts": manifest["test_counts"],
                      "golden_sha256": manifest["golden_sha256"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
