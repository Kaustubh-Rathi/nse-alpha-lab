"""Read every inventoried byte; write a separate verification report, never data.

Run with --out outside the production data tree. One shared state machine
(``nse_pipeline.stage1_evidence.inventory_state``) classifies clean trees,
accepted exceptions, absent exceptions and every failure mode. The CLI exit code
and the report authorization use the same integrity predicate.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nse_pipeline.stage1_evidence import (  # noqa: E402
    inventory_integrity_ok,
    inventory_state,
)

MANIFEST = "data/processed/dataset/dataset_manifest.json"
ORIGINAL = "5547ac458195acc1c4443dd042d9377ac4a79330533a764b038f4c89ff1d4ab9"
ACKNOWLEDGED = "c304d08ef89e3e91b738ba28c201bbcdb045f627ec6615bef87400aaf6d44293"
INVENTORY = "data/manifests/dataset_hashes.json"


def default_acknowledged(root: Path):
    """The production exception list applies only to the production checkout."""
    if Path(root).resolve() != ROOT.resolve():
        return ()
    return [{
        "path": MANIFEST,
        "original_sha256": ORIGINAL,
        "accepted_sha256": ACKNOWLEDGED,
    }]


def verify(root: Path, acknowledged=None) -> dict:
    root = Path(root).resolve()
    inventory = root / INVENTORY
    entries = json.loads(inventory.read_text(encoding="utf-8"))["data"]
    if acknowledged is None:
        acknowledged = default_acknowledged(root)
    result = inventory_state(root, entries, acknowledged)
    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    result["baseline_inventory"] = INVENTORY
    result["overall_p0_gate"] = "CLOSED"
    return result


def exit_code(result: dict) -> int:
    return 0 if inventory_integrity_ok(result) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    output = args.out.resolve()
    if output.is_relative_to((ROOT / "data").resolve()):
        parser.error("Verification reports must not overwrite production data artifacts")
    result = verify(ROOT)
    result["verified_commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(f"inventory state: {result['state']}")
    raise SystemExit(exit_code(result))


if __name__ == "__main__":
    main()
