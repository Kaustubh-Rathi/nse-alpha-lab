"""Build one integrated research dataset from existing processed outputs.

Read-only on data/market.duckdb and all agent outputs.
Writes only under data/processed/dataset/.

Usage:
  uv run python src/dataset.py
  uv run python src/dataset.py --out data/processed/dataset
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from nse_pipeline.paths import data_root
from nse_pipeline.build.portable import open_catalog

import duckdb
import pandas as pd

DEFAULT_DATA = data_root()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_dataset(data: Path, out: Path | None = None) -> dict:
    out = out or data / "processed" / "dataset"
    out.mkdir(parents=True, exist_ok=True)
    db_path = data / "market.duckdb"
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    canonical = data / "processed" / "ledger" / "canonical-scheme-ledger.jsonl"
    legacy = data / "processed" / "ledger" / "scheme-ledger.jsonl"
    ledger = canonical if canonical.exists() else legacy
    ledger_rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()
                   if line.strip()] if ledger.exists() else []
    terms_verified = sum(bool(row.get("terms_verified")) for row in ledger_rows)
    execution_ready = sum(row.get("execution_ready") is True for row in ledger_rows)
    readiness_path = data / "reports" / "full-history-readiness.json"
    readiness = json.loads(readiness_path.read_text(encoding="utf-8")) if readiness_path.exists() else None
    manifest: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cutoff": None,
        "scheme_coverage": {"ledger_rows": len(ledger_rows), "terms_verified": terms_verified,
                            "execution_ready": execution_ready, "source": str(ledger)},
        "fundamentals": "deferred_not_required_for_price_volume_strategy",
        "readiness": readiness,
        "sources": {},
        "outputs": {},
        "limitations": [
            "Prices in market.duckdb are raw; adjusted series is separate parquet.",
            "Research universe is NOT official Nifty membership.",
            "Verified document terms are not automatically executable entitlements; see scheme_coverage.",
            "Split-only bars exclude cash distributions and schemes; dividend outputs are separate.",
            "Pre-2011 coverage is partial; see adjustment-coverage.json and verified-actions.json.",
        ],
    }

    # 1. Adjusted bars: copy reference + counts (avoid 400MB copy by symlink-like manifest if same fs; fall back to copy metadata)
    adj = data / "processed" / "adjusted" / "adjusted_bars.parquet"
    uni = data / "processed" / "universe" / "universe_observations.parquet"
    tri = data / "processed" / "actions_triage" / "triage.parquet"
    for name, p in [("adjusted_bars", adj), ("universe", uni), ("triage", tri)]:
        if not p.exists():
            raise FileNotFoundError(p)
        manifest["sources"][name] = {"path": str(p), "bytes": p.stat().st_size, "sha256": sha256_file(p)}

    # Row counts via duckdb (read-only, no full pandas load for 10M rows)
    with open_catalog(database=db_path) as db:
        last = db.execute("SELECT max(date) FROM bars").fetchone()[0]
        manifest["cutoff"] = str(last) if last else None
        manifest["sources"]["market_duckdb"] = {
            "bars_rows": db.execute("SELECT count(*) FROM bars").fetchone()[0],
            "sessions": db.execute("SELECT count(DISTINCT date) FROM bars").fetchone()[0],
            "cash_rows": db.execute("SELECT count(*) FROM cash_equity_candidates").fetchone()[0],
        }
        # Proper dataset view: bars + security_key + adjusted factor join (left join to adjusted parquet via python parquet metadata)
        # Write a DuckDB view spec instead of materializing 400MB duplicate: store SQL
        def view(reader, path):
            quoted = str(path.resolve()).replace("\\", "/").replace("'", "''")
            return f"SELECT * FROM {reader}('{quoted}')"
        manifest["dataset_sql"] = {
            "bars_adjusted": view("read_parquet", adj),
            "universe_monthly": view("read_parquet", uni),
            "actions_triage": view("read_parquet", tri),
            "identity_edges": view("read_json_auto", data / "processed" / "identity" / "identity_edges.jsonl"),
            "scheme_ledger": view("read_json_auto", ledger),
            "scheme_index": view("read_json_auto", data / "processed" / "schemes" / "scheme-index.jsonl"),
        }
        fundamentals = data / "processed" / "fundamentals" / "filing-index.jsonl"
        if fundamentals.exists():
            manifest["dataset_sql"]["fundamentals_index"] = view("read_json_auto", fundamentals)

    # Copy small jsonl/parquet manifest tables into dataset dir for single-folder use (hardlink if possible)
    import os
    copies = [
        ("identity_edges.jsonl", data / "processed" / "identity" / "identity_edges.jsonl"),
        ("universe_observations.parquet", uni),
        ("triage.parquet", tri),
        ("scheme-ledger.jsonl", ledger),
        ("full-history-readiness.json", readiness_path),
        ("pdf-reading-queue.json", data / "processed" / "ledger" / "pdf-reading-queue.json"),
        ("scheme-index.jsonl", data / "processed" / "schemes" / "scheme-index.jsonl"),
        ("fundamentals-coverage.json", data / "processed" / "fundamentals" / "coverage.json"),
        ("action_audit.parquet", data / "processed" / "adjusted" / "action_audit.parquet"),
    ]
    for dest_name, src in copies:
        if src.exists():
            dest = out / dest_name
            try:
                if dest.exists():
                    dest.unlink()
                os.link(src, dest)  # hardlink, cheap
            except OSError:
                import shutil
                shutil.copy2(src, dest)
            manifest["outputs"][dest_name] = {"bytes": dest.stat().st_size, "sha256": sha256_file(dest)}

    # Bars: write manifest + sample, not a second 400MB copy; document that adjusted_bars.parquet IS the dataset bars
    manifest["outputs"]["adjusted_bars.parquet"] = {
        "note": "Canonical adjusted bars; referenced, not duplicated. See sources.adjusted_bars.",
        "bytes": adj.stat().st_size,
    }
    # Sample for quick inspection (duckdb limit, no full load)
    with duckdb.connect() as db:
        sample = db.execute("SELECT * FROM read_parquet(?) LIMIT 1000", [str(adj)]).fetchdf()
    sample.to_json(out / "adjusted_bars_sample_1000.json", orient="records", indent=1)
    manifest["outputs"]["adjusted_bars_sample_1000.json"] = {"rows": len(sample)}

    (out / "dataset_manifest.json").write_text(json.dumps(manifest, indent=2))
    (out / "README.md").write_text(
        "# Integrated research dataset\n\n"
        "Canonical bars: `data/processed/adjusted/adjusted_bars.parquet` (adj OHLC, raw preserved).\n"
        "Views: see `dataset_manifest.json::dataset_sql`.\n"
        f"Universe: research-defined, NOT Nifty. Scheme rows: {len(ledger_rows)}; document terms verified: {terms_verified}; execution ready: {execution_ready}.\n"
        "Fundamentals deferred. Split-only bars are not a complete total-return dataset.\n"
        "Readiness and unresolved coverage: see `dataset_manifest.json` and `full-history-readiness.json`.\n"
    )
    return manifest


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data", type=Path, default=data_root())
    p.add_argument("--out", type=Path, default=None)
    a = p.parse_args()
    m = build_dataset(a.data, a.out)
    print(json.dumps({"outputs": m["outputs"], "sources": {k: v.get("bars_rows", v.get("bytes")) for k, v in m["sources"].items()}}, indent=2))


if __name__ == "__main__":
    main()
