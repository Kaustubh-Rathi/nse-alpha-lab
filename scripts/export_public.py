"""Allowlist export: build the public repo from D:\fix_Investment_backtesting.

Copies ONLY explicitly allowed files. Never copies data/, verification/ bulk,
PDFs, ZIPs, parquet, logs, caches or scratch files.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

SRC = Path(r"D:\fix_Investment_backtesting")
DST = Path(__file__).resolve().parents[1]

# Files/dirs dropped during export (noise + duplicates proven unused).
EXCLUDE_NAMES = {
    "verify2.py", "verify3.py",
    "pre2011_queue.py", "pre2011_verify.py",
    "test_verify2.py", "test_verify3.py",
    "test_pre2011_queue.py", "test_pre2011_verify.py",
}
BANNED_SUFFIXES = {".pdf", ".zip", ".parquet", ".log", ".pyc", ".ipynb", ".db", ".duckdb", ".zst"}
BANNED_DIRS = {"__pycache__", ".pytest_cache", ".venv", ".perch", ".git", "data", "verification"}

ALPHA = "verification/research/alpha-backtest"
ALPHA_FILES = [
    "run_sweep.py", "merge_sweep.py", "validate_alpha_output.py", "compute_alpha.py",
    "build_report.py", "build_final_csv.py", "build_benchmark.py", "run_alpha_backtest.py",
    "REPORT.md", "SWEEP_REPORT.md", "BENCHMARK.md",
    "FINAL_RESULTS.csv", "FLAGSHIP_RESULTS.csv", "FLAGSHIP_EVIDENCE.csv", "validation.json",
]

# (source relative path, target relative path)
TREES = [
    ("src/nse_pipeline", "src/nse_pipeline"),
    ("tests", "tests"),
    ("docs", "docs"),
    ("scripts", "scripts"),
]
ROOT_FILES = ["pyproject.toml", "uv.lock", ".python-version"]


def allowed(rel: Path) -> bool:
    """Judge a SOURCE path by name/suffix only.

    Directory bans are applied by the tree walker (``TREES``), never here:
    the alpha-backtest drivers legitimately live under ``verification/``,
    so testing parts of the source path would drop them silently.
    """
    if rel.name in EXCLUDE_NAMES:
        return False
    if rel.suffix.lower() in BANNED_SUFFIXES:
        return False
    return True


def main() -> int:
    manifest = {"copied": [], "skipped": [], "missing": []}

    def copy(rel_src: str, rel_dst: str) -> None:
        s = SRC / rel_src
        if not s.is_file():
            manifest["missing"].append(rel_src)
            return
        rel = Path(rel_src)
        if not allowed(rel):
            manifest["skipped"].append(rel_src)
            return
        d = DST / rel_dst
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
        manifest["copied"].append(rel_dst)

    for tree_src, tree_dst in TREES:
        base = SRC / tree_src
        if not base.exists():
            manifest["missing"].append(tree_src)
            continue
        for f in base.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(SRC)
            # Directory bans apply to the walked tree, not to explicit allowlist entries.
            if any(part in BANNED_DIRS for part in rel.parts):
                manifest["skipped"].append(str(rel).replace("\\", "/"))
                continue
            copy(str(rel).replace("\\", "/"), str(rel).replace("\\", "/"))

    for name in ROOT_FILES:
        copy(name, name)

    # research/ drivers were explicitly allowlisted from verification/.
    for name in ALPHA_FILES:
        copy(f"{ALPHA}/{name}", f"research/{name}")

    (DST / ".export-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"copied={len(manifest['copied'])} skipped={len(manifest['skipped'])} missing={len(manifest['missing'])}")
    for m in manifest["missing"]:
        print("  MISSING:", m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
