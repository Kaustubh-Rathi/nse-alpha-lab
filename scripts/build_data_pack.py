#!/usr/bin/env python3
"""Build the distributable NSE Alpha Lab data pack (offline, reproducible).

The shipped CLI defines the contract this pack must satisfy
(``src/nse_pipeline/cli.py``):

* ``verify_pack`` reads a manifest that is one of ``manifest.json``,
  ``pack.json`` or ``checksums.json`` at the pack root, takes the ``files``
  list, and for every entry ``{"path": str, "sha256": str, "size": int}``
  checks the bytes at ``<pack>/<path>``.  ``path`` is relative to the pack
  root, uses ``/`` separators and may never escape the pack directory.
* ``cmd_backtest`` / ``_bars_path`` read
  ``<data root>/processed/adjusted/adjusted_bars.parquet`` and require at
  least the columns ``date, key, open, close, volume`` (``_load_bars``).
* The data root is ``$NSE_DATA_DIR`` when set, else ``<repo>/data``
  (``nse_pipeline.paths.data_root``).  The pack is therefore assembled in the
  data-root layout: unpacking it to ``<repo>/data`` or pointing
  ``NSE_DATA_DIR`` at it is enough to backtest.

The upstream canonical ``adjusted_bars.parquet`` has ``symbol``/``isin`` and
raw + ``adj_*`` columns, but no ``key`` column, so the shipped CLI cannot read
it directly.  This builder derives the backtest-ready bars with the
repository's own identity rules: the same EQ/valid/volume/price filter as
``nse_pipeline.backtest.momentum_grid.load_adjusted_bars`` and
``nse_pipeline.backtest.security_identity.resolve_identity`` for the stable
``key``.  The identity mapping is computed from aggregate symbol/ISIN spans to
keep memory bounded; the builder self-checks that applying that mapping to a
real window reproduces per-row ``resolve_identity`` exactly before writing the
full file.

Only stdlib plus pandas/pyarrow/duckdb is used.  No network access.

Usage (run from the repository root, with the source data read-only):

    python scripts/build_data_pack.py --source D:/path/to/data

Outputs under ``dist/`` (ignored by git):

    dist/nse-alpha-data-pack-<version>/      the pack tree
    dist/nse-alpha-data-pack-<version>.tar.gz
    dist/nse-alpha-data-pack-<version>.tar.gz.sha256
    dist/kaggle-upload/                      staged Kaggle dataset + metadata
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

PACK_NAME = "nse-alpha-data-pack"
DEFAULT_VERSION = "1.0.0"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT.parents[1] / "data"
DEFAULT_DIST = REPO_ROOT / "dist"

#: README.md "Useful audit outputs" -- the public coverage/readiness reports.
REPORT_FILES = (
    "validation.json",
    "readiness.json",
    "full-history-readiness.json",
    "canonical-schemes.json",
    "verified-actions.json",
    "adjustment-coverage.json",
    "dividends-coverage.json",
    "identity-coverage.json",
    "calendar-remediation.json",
    "coverage-audit.json",
    "coverage-audit.md",
    "official-gap-backfill.json",
    "pre2011-actions-coverage.json",
    "index-membership-research.md",
    "actions-needing-review.csv",
    "data-status.md",
    "repair-validation.md",
)

#: Download/build provenance for the upstream series, kept small and public.
MANIFEST_FILES = (
    "release-2025-master.json",
    "release-pr-bhav-master.json",
    "prices-build.json",
    "actions-build.json",
    "downloads.json",
)

#: Copied from the repository, not from the data root: redistribution terms.
DOC_FILES = ("DATA_LICENSES.md", "DISCLAIMER.md")

#: Same filters as momentum_grid.load_adjusted_bars.
BARS_FILTER = (
    "series = 'EQ' AND valid_ohlcv AND volume > 0 "
    "AND adj_close IS NOT NULL AND adj_close > 0 "
    "AND adj_open IS NOT NULL AND adj_open > 0"
)

BARS_REQUIRED_COLUMNS = ("date", "key", "open", "close", "volume")

TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".csv", ".txt", ".cff", ".yml", ".yaml"}

#: A Windows drive path such as ``D:\\fix...`` or ``D:/fix...``.  URLs
#: (``https://``) do not match because the character after the separator is
#: itself a separator.
ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\\/]")

#: Patterns that leak the private working directory into a public file.
_LEAK_PATTERNS = (
    re.compile(r"[A-Za-z]:[\\/]+fix_Investment_backtesting[\\/]+data[\\/]+", re.IGNORECASE),
    re.compile(r"[A-Za-z]:[\\/]+fix_Investment_backtesting[\\/]*", re.IGNORECASE),
)

USERNAME_RE = re.compile(r"Kaustuvh|fix_Investment_backtesting", re.IGNORECASE)


class BuildError(Exception):
    """The pack cannot be built honestly; the message says exactly why."""


# ---------------------------------------------------------------------------
# hashing / io helpers
# ---------------------------------------------------------------------------

def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 text with LF newlines so output is OS-independent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write_json(path: Path, payload: object) -> None:
    write_text(path, json.dumps(payload, indent=2, sort_keys=False) + "\n")


def sanitise_text(text: str) -> tuple[str, bool]:
    """Remove private absolute source paths from a public text file."""
    changed = False
    for pattern in _LEAK_PATTERNS:
        text, count = pattern.subn("", text)
        changed = changed or bool(count)
    return text, changed


def assert_no_leaks(path: Path) -> None:
    """Refuse to ship a text file that still names a private absolute path."""
    suffix = path.suffix.lower()
    if suffix not in TEXT_SUFFIXES:
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    hits = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ABSOLUTE_PATH_RE.search(line) or USERNAME_RE.search(line):
            hits.append(f"  {path.name}:{number}: {line.strip()[:160]}")
    if hits:
        raise BuildError(
            "refusing to ship a file that leaks a private absolute path or "
            "username:\n" + "\n".join(hits))


# ---------------------------------------------------------------------------
# pack assembly
# ---------------------------------------------------------------------------

def copy_source_tree_file(source: Path, relative: str, dest_root: Path) -> dict:
    """Copy one upstream file into the pack, sanitising any private paths.

    Returns the provenance record shared by ``build-provenance.json``.
    """
    origin = source / relative
    if not origin.is_file():
        raise BuildError(f"missing upstream file: {origin}")
    target = dest_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = origin.read_bytes()
    source_sha = hashlib.sha256(raw).hexdigest()
    shipped = raw
    sanitised = False
    if target.suffix.lower() in TEXT_SUFFIXES:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BuildError(f"{origin} is not UTF-8 text: {exc}") from exc
        text, changed = sanitise_text(text)
        if changed:
            shipped = text.encode("utf-8")
            sanitised = True
    target.write_bytes(shipped)
    assert_no_leaks(target)
    return {
        "path": relative,
        "source": relative,
        "source_sha256": source_sha,
        "sha256": hashlib.sha256(shipped).hexdigest(),
        "size": len(shipped),
        "sanitised": sanitised,
    }


def _require_modules() -> None:
    missing = []
    for module in ("duckdb", "pandas", "pyarrow"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        raise BuildError(
            "the builder needs " + ", ".join(missing)
            + "; use the project venv (pip install duckdb pandas pyarrow)")


def _repo_identity_resolver():
    """Import the repository's own identity resolver (code is authoritative)."""
    src = REPO_ROOT / "src"
    if not (src / "nse_pipeline").is_dir():
        raise BuildError(f"repository source not found: {src}")
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from nse_pipeline.backtest.security_identity import resolve_identity  # noqa: PLC0415

    return resolve_identity


def _span_mapping(connection, parquet: str, start, end, edges_path: Path):
    """Compute symbol/ISIN identity maps from aggregate spans only.

    ``resolve_identity`` only needs, per symbol, the observed bar span, and, per
    (symbol, ISIN) pair, its observed bar span.  Feeding those two-row-per-pair
    aggregates through the real function reproduces the per-row result exactly
    while keeping memory to a few thousand rows instead of ten million.
    """
    resolve_identity = _repo_identity_resolver()
    filter_sql = BARS_FILTER
    spans = connection.execute(
        f"SELECT symbol, min(date) AS d0, max(date) AS d1 "
        f"FROM read_parquet(?) WHERE {filter_sql} AND date BETWEEN ? AND ? "
        f"GROUP BY symbol",
        [parquet, start, end],
    ).fetchdf()
    isin_spans = connection.execute(
        f"SELECT symbol, isin, min(date) AS d0, max(date) AS d1 "
        f"FROM read_parquet(?) WHERE {filter_sql} AND date BETWEEN ? AND ? "
        f"GROUP BY symbol, isin",
        [parquet, start, end],
    ).fetchdf()
    if spans.empty:
        raise BuildError(f"no bars pass the filter between {start} and {end}")
    import pandas as pd  # noqa: PLC0415

    synthetic = pd.concat(
        [
            isin_spans[["symbol", "isin"]].assign(date=isin_spans["d0"]),
            isin_spans[["symbol", "isin"]].assign(date=isin_spans["d1"]),
        ],
        ignore_index=True,
    )[["date", "symbol", "isin"]]
    resolution = resolve_identity(synthetic, edges_path)
    frame = resolution.frame
    resolved = (frame[["symbol", "resolved_symbol"]]
                .drop_duplicates()
                .reset_index(drop=True))
    isin_map = (frame[["isin", "canonical_isin"]]
                .dropna(subset=["isin"])
                .drop_duplicates()
                .reset_index(drop=True))
    return resolved, isin_map, resolution.stats


def _apply_mapping_pandas(frame, resolved, isin_map):
    """Attach the key in pandas the same way the SQL COPY does."""
    import numpy as np  # noqa: PLC0415

    merged = frame.merge(resolved, on="symbol", how="left")
    merged = merged.merge(
        isin_map.rename(columns={"canonical_isin": "_canonical_isin"}),
        on="isin", how="left")
    isin = merged["isin"]
    canonical = merged["_canonical_isin"].where(
        merged["_canonical_isin"].notna(), isin)
    symbol_key = "SYMBOL:" + merged["resolved_symbol"].astype(str)
    isin_key = "ISIN:" + canonical.astype(str)
    merged["key"] = np.where(isin.notna(), isin_key, symbol_key)
    return merged


def _self_check(connection, parquet: str, edges_path: Path, start, end) -> dict:
    """Prove span-mapping == per-row resolve_identity on a real window."""
    import pandas as pd  # noqa: PLC0415

    resolve_identity = _repo_identity_resolver()
    resolved, isin_map, _stats = _span_mapping(
        connection, parquet, start, end, edges_path)
    window = connection.execute(
        f"SELECT date, isin, symbol, adj_open AS open, adj_close AS close, "
        f"volume FROM read_parquet(?) WHERE {BARS_FILTER} AND date BETWEEN ? AND ?",
        [parquet, start, end],
    ).fetchdf()
    if window.empty:
        raise BuildError("identity self-check window has no bars")
    direct = resolve_identity(window, edges_path).frame
    mapped = _apply_mapping_pandas(window, resolved, isin_map)
    direct_keys = direct["key"].astype(str).to_numpy()
    mapped_keys = mapped["key"].astype(str).to_numpy()
    identical = bool((direct_keys == mapped_keys).all())
    if not identical:
        mismatch = int((direct_keys != mapped_keys).sum())
        raise BuildError(
            f"identity derivation self-check failed on {mismatch} of "
            f"{len(direct_keys)} rows; refusing to write an unreproducible pack")
    return {
        "window": [str(start), str(end)],
        "rows": int(len(direct_keys)),
        "identical_keys": identical,
        "distinct_keys": int(pd.Series(direct_keys).nunique()),
    }


def derive_bars(source: Path, dest_root: Path, temp_dir: Path) -> dict:
    """Write the CLI-ready ``processed/adjusted/adjusted_bars.parquet``.

    This is a derivation, not a copy: the upstream canonical file keeps raw and
    ``adj_*`` columns and has no stable ``key``, while the shipped CLI requires
    ``date, key, open, close, volume``.  The build is documented in
    ``provenance/build-provenance.json``.
    """
    import duckdb  # noqa: PLC0415
    import pandas as pd  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415

    canonical = source / "processed/adjusted/adjusted_bars.parquet"
    edges = source / "processed/identity/identity_edges.jsonl"
    for required in (canonical, edges):
        if not required.is_file():
            raise BuildError(f"missing upstream file: {required}")
    canonical_sha = sha256_file(canonical)
    canonical_bytes = canonical.stat().st_size

    temp_dir.mkdir(parents=True, exist_ok=True)
    duckdb_temp = temp_dir / "duckdb-tmp"
    duckdb_temp.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    try:
        connection.execute("SET threads = 1")
        connection.execute("SET memory_limit = '1536MB'")
        connection.execute(f"SET temp_directory = '{_sql_path(duckdb_temp)}'")
        parquet = _sql_path(canonical)
        bounds = connection.execute(
            f"SELECT min(date), max(date), count(*) FROM read_parquet(?) "
            f"WHERE {BARS_FILTER}", [str(canonical)]).fetchone()
        start, end, filtered_rows = bounds
        if not filtered_rows:
            raise BuildError("no bars pass the CLI filter in the canonical file")

        print(f"  bars: {filtered_rows:,} rows between {start} and {end}")
        check_start = max(start, (pd.Timestamp(end) - pd.Timedelta(days=183)).date())
        print(f"  identity self-check window: {check_start} .. {end}")
        self_check = _self_check(connection, parquet, edges, check_start, end)
        print(f"  identity self-check: {self_check['rows']:,} rows, "
              f"identical keys = {self_check['identical_keys']}")

        resolved, isin_map, mapping_stats = _span_mapping(
            connection, parquet, start, end, edges)
        connection.register("resolved_map", resolved)
        connection.register("isin_map", isin_map)

        target = dest_root / "processed/adjusted/adjusted_bars.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        select_sql = f"""
            SELECT b.date,
                   b.isin,
                   b.symbol,
                   b.series,
                   b.adj_open  AS open,
                   b.adj_high  AS high,
                   b.adj_low   AS low,
                   b.adj_close AS close,
                   b.volume,
                   b.valid_ohlcv,
                   CASE WHEN b.isin IS NOT NULL
                        THEN 'ISIN:' || COALESCE(m.canonical_isin, b.isin)
                        ELSE 'SYMBOL:' || COALESCE(r.resolved_symbol, b.symbol)
                   END AS key
            FROM read_parquet('{parquet}') b
            LEFT JOIN isin_map m ON b.isin = m.isin
            LEFT JOIN resolved_map r ON b.symbol = r.symbol
            WHERE {BARS_FILTER}
              AND b.date BETWEEN DATE '{start}' AND DATE '{end}'
            ORDER BY b.date, b.symbol, b.isin
        """
        print(f"  writing {target.relative_to(dest_root)} ...")
        started = time.time()
        connection.execute(
            f"COPY ({select_sql}) TO '{_sql_path(target)}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)")
        connection.close()
        connection = None
        print(f"  wrote {target.stat().st_size / 1e6:.1f} MB in "
              f"{time.time() - started:.1f}s")

        # Re-open read-only to validate exactly what will ship.
        names = [field.name for field in pq.ParquetFile(target).schema_arrow]
        missing = [name for name in BARS_REQUIRED_COLUMNS if name not in names]
        if missing:
            raise BuildError(f"derived bars lack required columns: {missing}")
        check = duckdb.connect()
        try:
            stats = check.execute(
                "SELECT count(*), count(DISTINCT date), count(DISTINCT key), "
                "min(date), max(date) FROM read_parquet(?)",
                [str(target)]).fetchone()
            duplicates = check.execute(
                "SELECT count(*) FROM (SELECT date, key FROM read_parquet(?) "
                "GROUP BY date, key HAVING count(*) > 1)",
                [str(target)]).fetchone()[0]
        finally:
            check.close()
        if duplicates:
            raise BuildError(
                f"derived bars contain {duplicates} duplicated (date, key) rows; "
                "the engine refuses those, so the pack would not be backtestable")
        rows, sessions, keys, first, last = stats
        return {
            "path": "processed/adjusted/adjusted_bars.parquet",
            "derived": True,
            "source": "processed/adjusted/adjusted_bars.parquet",
            "source_sha256": canonical_sha,
            "source_size": canonical_bytes,
            "sha256": sha256_file(target),
            "size": target.stat().st_size,
            "rows": int(rows),
            "sessions": int(sessions),
            "distinct_keys": int(keys),
            "first_date": str(first),
            "last_date": str(last),
            "duplicate_date_key_rows": int(duplicates),
            "filter": BARS_FILTER,
            "identity_edges": {
                "path": "processed/identity/identity_edges.jsonl",
                "sha256": sha256_file(edges),
            },
            "identity_mapping": mapping_stats,
            "self_check": self_check,
            "note": (
                "Derived with the repository's own identity rules: the EQ/valid/"
                "volume/positive-adjusted-price filter of "
                "nse_pipeline.backtest.momentum_grid.load_adjusted_bars and the "
                "stable key from nse_pipeline.backtest.security_identity."
                "resolve_identity, applied over aggregate symbol/ISIN spans. "
                "Upstream canonical columns are replaced by their adjusted "
                "counterparts under the names the CLI expects."),
        }
    finally:
        if connection is not None:
            connection.close()


def _sql_path(path: Path) -> str:
    """A single-quoted-safe POSIX path for DuckDB SQL literals."""
    return str(path.resolve()).replace("\\", "/").replace("'", "''")


# ---------------------------------------------------------------------------
# generated documents
# ---------------------------------------------------------------------------

def pack_readme(version: str) -> str:
    return f"""# NSE Alpha Lab data pack {version}

This directory is the checksummed data pack promised by the `nse-alpha-lab`
public repository README.  It is the data root the shipped CLI expects: unpack
it so the repository contains `data/`, or point `NSE_DATA_DIR` at this
directory.

    nse-alpha-lab/
    └── data/                       <- this directory
        ├── manifest.json           SHA-256 + byte size for every shipped file
        ├── processed/              backtest bars, identity edges, universe, index snapshots
        ├── manifests/              upstream download/build provenance
        ├── reports/                public coverage, readiness and audit reports
        └── provenance/             how this pack was built from the upstream data

## Verify it

    # from the repository checkout
    python -m nse_pipeline verify --pack data

Every file listed in `manifest.json` is checked by byte size and SHA-256;
`manifest.json` itself is not self-listed.  The verifier never reads `.git`, so
a directory that arrived as a tar archive verifies exactly like a clone.

## Run a backtest

    # PowerShell
    $env:NSE_DATA_DIR = "<absolute path to this directory>"
    python -m nse_pipeline backtest examples/specs/primary.json --out backtest.json

    # bash
    NSE_DATA_DIR="<absolute path to this directory>" \\
        python -m nse_pipeline backtest examples/specs/primary.json --out backtest.json

The CLI reads `processed/adjusted/adjusted_bars.parquet` (columns `date`, `key`,
`open`, `close`, `volume` and more).  `--pack DIR` is a different option: it
expects `DIR/adjusted_bars.parquet` directly, so it is not used for this
data-root layout; use `NSE_DATA_DIR` or unpack to `<repo>/data`.

## What is in the pack

| Group | Contents | Why |
|---|---|---|
| `processed/adjusted/adjusted_bars.parquet` | split/bonus-adjusted EQ bars, identity-resolved, 2000-2026 | the only file the shipped backtest reads |
| `processed/adjusted/action_audit.parquet`, `audit_manifest.json` | every corporate action considered and applied or excluded | audit trail for the adjusted prices |
| `processed/identity/identity_edges.jsonl` | dated symbol-rename edges | makes the `key` column reconstructible and rename handling auditable |
| `processed/universe/universe_observations.parquet` | point-in-time research universe observations | documents the research universe (not official Nifty membership) |
| `processed/index_membership/` | dated official NIFTY constituent snapshots | official anchors used by the research scripts |
| `manifests/` | upstream release, download and build provenance | identifies the exact source publications and hashes |
| `reports/` | validation, readiness, coverage and audit reports | the public evidence behind the published results |
| `provenance/build-provenance.json` | upstream hashes, derivation recipe, build self-check | reproducibility record |

Deliberately **not** shipped: `raw/` acquisition archives (2.8 GB, reproducible
from the URLs and hashes in `manifests/`), `market.duckdb` (small, but its views
name raw and processed files that are not in a minimal pack),
`processed/schemes/`, `processed/nifty_doc/` and the other PDF/announcement
working trees, internal backtest run logs, and any `*.internal.csv`.

## Licence, provenance and limitations

Read `DATA_LICENSES.md` before redistributing.  The data is published from
public NSE publications and index announcements; NIFTY index names remain the
trademarks of their owners; nothing here is an official NSE or NSE Indices
product.  All results are research previews, not investment advice.

Known limitations, repeated from the project status:

* Prices are split/bonus-adjusted only, not total-return; dividends are
  excluded on both sides.
* The research universe is a reconstructed point-in-time approximation, not
  official Nifty membership.
* The public sweep scripts in `research/` additionally expect a non-public
  evidence payload under `verification/` and a benchmark series file; this pack
  alone runs the CLI `backtest` command, not the published 968-row sweep.
"""


def build_provenance_document(version: str, release_epoch: int,
                              file_records: list[dict], bars_record: dict,
                              sanitised: list[str]) -> dict:
    return {
        "pack": PACK_NAME,
        "version": version,
        "generated_at": datetime.fromtimestamp(
            release_epoch, tz=timezone.utc).isoformat(),
        "builder": "scripts/build_data_pack.py",
        "source_data_root": "upstream data root (absolute path removed)",
        "derivation": {
            "processed/adjusted/adjusted_bars.parquet": bars_record,
            "identity_rule": (
                "Resolve symbol renames and within-symbol ISIN changes with "
                "nse_pipeline.backtest.security_identity.resolve_identity, "
                "using the same aggregate spans the function derives from full "
                "bars; then key = 'ISIN:<canonical isin>' when an ISIN is known, "
                "else 'SYMBOL:<resolved symbol>'."),
        },
        "sanitised_files": sorted(sanitised),
        "files": sorted(file_records, key=lambda record: record["path"]),
        "tools": {
            "python": platform.python_version(),
            "pandas": _version("pandas"),
            "pyarrow": _version("pyarrow"),
            "duckdb": _version("duckdb"),
        },
    }


def _version(module_name: str) -> str | None:
    try:
        module = __import__(module_name)
    except ImportError:
        return None
    return getattr(module, "__version__", None)


# ---------------------------------------------------------------------------
# manifest / archive / kaggle staging
# ---------------------------------------------------------------------------

def resolved_files(pack_root: Path) -> list[Path]:
    return sorted(
        (path for path in pack_root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(pack_root).as_posix())


def write_manifest(pack_root: Path, version: str, release_epoch: int,
                   manifest_entries: list[dict]) -> dict:
    records = []
    for name in manifest_entries:
        path = pack_root / name
        records.append({
            "path": name,
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        })
    payload = {
        "pack": PACK_NAME,
        "version": version,
        "generated_at": datetime.fromtimestamp(
            release_epoch, tz=timezone.utc).isoformat(),
        "hash_algorithm": "sha256",
        "note": (
            "Manifest for the NSE Alpha Lab data pack. Every listed file is "
            "checked by byte size and SHA-256; this manifest is not self-listed. "
            "Layout: <data root>/{processed,manifests,reports,provenance}. "
            "The CLI verifies it with: python -m nse_pipeline verify --pack <dir>."),
        "files": records,
    }
    write_json(pack_root / "manifest.json", payload)
    return payload


def build_archive(pack_root: Path, archive_path: Path, release_epoch: int) -> dict:
    """Deterministic tar.gz: fixed mtimes, uid/gid, order and gzip header."""
    files = resolved_files(pack_root)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        archive_path.unlink()
    root_name = pack_root.name
    with gzip.GzipFile(filename=str(archive_path), mode="wb", mtime=0) as gzipped:
        with tarfile.open(fileobj=gzipped, mode="w",
                          format=tarfile.USTAR_FORMAT) as archive:
            directories = sorted(
                (path for path in pack_root.rglob("*") if path.is_dir()),
                key=lambda path: path.relative_to(pack_root).as_posix())
            for directory in directories:
                info = tarfile.TarInfo(
                    f"{root_name}/{directory.relative_to(pack_root).as_posix()}")
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                info.mtime = release_epoch
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                archive.addfile(info)
            for path in files:
                relative = path.relative_to(pack_root).as_posix()
                info = tarfile.TarInfo(f"{root_name}/{relative}")
                info.size = path.stat().st_size
                info.mode = 0o644
                info.mtime = release_epoch
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with path.open("rb") as handle:
                    archive.addfile(info, handle)
    digest = sha256_file(archive_path)
    checksum_path = archive_path.with_name(archive_path.name + ".sha256")
    write_text(checksum_path, f"{digest}  {archive_path.name}\n")
    return {
        "path": str(archive_path),
        "bytes": archive_path.stat().st_size,
        "sha256": digest,
        "checksum_file": str(checksum_path),
        "entries": len(files),
    }


def kaggle_metadata(version: str, archive_sha256: str | None = None) -> dict:
    checksum_line = (
        f"\n\nArchive: `nse-alpha-data-pack-{version}.tar.gz`, "
        f"SHA-256 `{archive_sha256}`. Verify the download with "
        f"`sha256sum -c nse-alpha-data-pack-{version}.tar.gz.sha256` before "
        "unpacking."
        if archive_sha256 else "")
    return {
        "title": "NSE Alpha Lab Data Pack",
        "id": "Kaustubh-Rathi/nse-alpha-lab-data-pack",
        "subtitle": "NSE price and corporate-action data for reproducible research",
        "licenses": [{"name": "CC-BY-4.0"}],
        "description": (
            "Checksummed data pack for the nse-alpha-lab open research "
            "repository (pack version " + version + ")." + checksum_line + "\n\n"
            "The pack is the data root expected by the shipped CLI: unpack "
            "`nse-alpha-data-pack-" + version + ".tar.gz` so the repository "
            "contains `data/`, or point `NSE_DATA_DIR` at the unpacked "
            "directory. It contains split/bonus-adjusted NSE EQ bars "
            "(2000-2026) with a stable security key, the identity edges and "
            "corporate-action audit that make those bars reconstructible, the "
            "point-in-time research universe observations, official dated "
            "NIFTY snapshots, upstream download/build provenance and the "
            "public coverage/readiness/audit reports.\n\n"
            "Verify: `python -m nse_pipeline verify --pack data`. "
            "Backtest: `python -m nse_pipeline backtest "
            "examples/specs/primary.json`.\n\n"
            "The pack contains no raw acquisition archives, no order-book or "
            "tick data and no paid vendor feed. Data is derived from public "
            "NSE publications and index announcements; NIFTY index names are "
            "trademarks of their owners and this is not an official NSE or NSE "
            "Indices product. Authoritative redistribution terms and per-source "
            "provenance are in `DATA_LICENSES.md` inside the archive; the code "
            "is Apache-2.0. Results are research previews, not investment "
            "advice. Prices are split/bonus adjusted only (not total return) "
            "and the research universe is not official Nifty membership."),
        "keywords": ["finance", "stock market", "india", "nse", "quantitative finance"],
        "expectedUpdateFrequency": "quarterly",
    }


def stage_kaggle(archive: dict, pack_root: Path, dist: Path,
                 version: str) -> Path:
    staging = dist / "kaggle-upload"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    archive_path = Path(archive["path"])
    target = staging / archive_path.name
    try:
        os.link(archive_path, target)
    except OSError:
        shutil.copy2(archive_path, target)
    shutil.copy2(pack_root / "manifest.json", staging / "manifest.json")
    shutil.copy2(pack_root / "README.md", staging / "README.md")
    shutil.copy2(pack_root / "DATA_LICENSES.md", staging / "DATA_LICENSES.md")
    write_json(staging / "dataset-metadata.json",
               kaggle_metadata(version, archive.get("sha256")))
    return staging


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def _release_epoch(source: Path, relatives: list[str]) -> int:
    stamps = []
    for relative in relatives:
        path = source / relative
        if path.is_file():
            stamps.append(int(path.stat().st_mtime))
    if not stamps:
        raise BuildError("no upstream files to timestamp the release from")
    return max(stamps)


def plan_source_files(source: Path) -> list[str]:
    """Every upstream file the pack ships, in stable order."""
    relatives: list[str] = []
    for name in REPORT_FILES:
        relatives.append(f"reports/{name}")
    for name in MANIFEST_FILES:
        relatives.append(f"manifests/{name}")
    relatives.append("processed/adjusted/action_audit.parquet")
    relatives.append("processed/adjusted/audit_manifest.json")
    relatives.append("processed/identity/identity_edges.jsonl")
    relatives.append("processed/universe/universe_observations.parquet")
    membership = source / "processed/index_membership"
    if not membership.is_dir():
        raise BuildError(f"missing upstream directory: {membership}")
    members = sorted(
        (path.relative_to(source).as_posix()
         for path in membership.iterdir() if path.is_file()),
        key=str)
    relatives.extend(members)
    missing = [relative for relative in relatives
               if not (source / relative).is_file()]
    if missing:
        raise BuildError("missing upstream files:\n" + "\n".join(missing))
    return relatives


def build(source: Path, dist: Path, version: str, *, make_archive: bool,
          reuse_bars: bool) -> dict:
    _require_modules()
    if not source.is_dir():
        raise BuildError(f"source data root not found: {source}")
    pack_root = dist / f"{PACK_NAME}-{version}"
    if pack_root.exists():
        shutil.rmtree(pack_root)
    pack_root.mkdir(parents=True)
    temp_dir = dist / "_build-tmp"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)

    source_relatives = plan_source_files(source)
    for name in DOC_FILES:
        if not (REPO_ROOT / name).is_file():
            raise BuildError(f"missing repository document: {REPO_ROOT / name}")

    started = time.time()
    print(f"pack:   {pack_root}")
    print(f"source: {source}")
    print(f"version: {version}")

    print("1/5 deriving CLI-ready adjusted bars ...")
    bars_target = pack_root / "processed/adjusted/adjusted_bars.parquet"
    if reuse_bars and bars_target.is_file():
        print("  reusing existing derived bars (--reuse-bars)")
        canonical = source / "processed/adjusted/adjusted_bars.parquet"
        import duckdb  # noqa: PLC0415

        check = duckdb.connect()
        try:
            row = check.execute(
                "SELECT count(*), count(DISTINCT date), count(DISTINCT key), "
                "min(date), max(date) FROM read_parquet(?)",
                [str(bars_target)]).fetchone()
        finally:
            check.close()
        bars_record = {
            "path": "processed/adjusted/adjusted_bars.parquet",
            "derived": True,
            "source": "processed/adjusted/adjusted_bars.parquet",
            "source_sha256": sha256_file(canonical),
            "source_size": canonical.stat().st_size,
            "sha256": sha256_file(bars_target),
            "size": bars_target.stat().st_size,
            "rows": int(row[0]),
            "sessions": int(row[1]),
            "distinct_keys": int(row[2]),
            "first_date": str(row[3]),
            "last_date": str(row[4]),
            "note": "reused from a previous build (--reuse-bars)",
        }
    else:
        bars_record = derive_bars(source, pack_root, temp_dir)

    print("2/5 copying provenance, universe, identity, index snapshots, reports ...")
    file_records: list[dict] = [bars_record]
    sanitised: list[str] = []
    for relative in source_relatives:
        record = copy_source_tree_file(source, relative, pack_root)
        if record["sanitised"]:
            sanitised.append(relative)
            print(f"  sanitised private paths in {relative}")
        file_records.append(record)

    print("3/5 copying repository licence documents and writing pack README ...")
    for name in DOC_FILES:
        raw = (REPO_ROOT / name).read_bytes()
        text, changed = sanitise_text(raw.decode("utf-8"))
        target = pack_root / name
        target.write_bytes(text.encode("utf-8"))
        assert_no_leaks(target)
        if changed:
            sanitised.append(name)
        file_records.append({
            "path": name,
            "source": f"repository:{name}",
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "size": target.stat().st_size,
            "sanitised": changed,
        })
    write_text(pack_root / "README.md", pack_readme(version))

    print("4/5 writing provenance and the manifest ...")
    release_epoch = _release_epoch(source, source_relatives)
    (pack_root / "provenance").mkdir(parents=True, exist_ok=True)
    provenance = build_provenance_document(
        version, release_epoch, file_records, bars_record, sanitised)
    write_json(pack_root / "provenance/build-provenance.json", provenance)

    manifest_entries = [
        path.relative_to(pack_root).as_posix()
        for path in resolved_files(pack_root)
        if path.relative_to(pack_root).as_posix() != "manifest.json"
    ]
    manifest = write_manifest(pack_root, version, release_epoch, manifest_entries)
    if not manifest["files"]:
        raise BuildError("the generated manifest lists no files")
    assert_no_leaks(pack_root / "manifest.json")

    for path in resolved_files(pack_root):
        assert_no_leaks(path)

    archive = None
    if make_archive:
        print("5/5 building the versioned archive ...")
        archive = build_archive(
            pack_root, dist / f"{PACK_NAME}-{version}.tar.gz", release_epoch)
        print(f"  archive: {archive['bytes'] / 1e6:.1f} MB  "
              f"sha256={archive['sha256']}")
    else:
        print("5/5 skipping archive (--no-archive)")

    if temp_dir.exists():
        shutil.rmtree(temp_dir)

    pack_bytes = sum(path.stat().st_size for path in resolved_files(pack_root))
    summary = {
        "pack": PACK_NAME,
        "version": version,
        "pack_dir": str(pack_root),
        "generated_at": manifest["generated_at"],
        "build_seconds": round(time.time() - started, 1),
        "file_count": len(resolved_files(pack_root)),
        "manifest_entries": len(manifest["files"]),
        "pack_bytes": pack_bytes,
        "pack_megabytes": round(pack_bytes / 1e6, 1),
        "bars": {key: bars_record.get(key) for key in
                 ("path", "rows", "sessions", "distinct_keys",
                  "first_date", "last_date", "size", "sha256")},
        "bars_self_check": bars_record.get("self_check"),
        "archive": archive,
        "kaggle_upload": None,
    }
    if archive is not None:
        staging = stage_kaggle(archive, pack_root, dist, version)
        summary["kaggle_upload"] = {
            "dir": str(staging),
            "command": f"kaggle datasets create -p {staging} -u",
            "metadata": str(staging / "dataset-metadata.json"),
        }
    write_json(dist / f"{PACK_NAME}-{version}.build.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the distributable, checksummed NSE Alpha Lab data pack.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The pack is assembled in the data-root layout the shipped CLI "
            "reads ($NSE_DATA_DIR or <repo>/data) and gets a root manifest.json "
            "in the schema verify_pack accepts. No network access is used.\n\n"
            "Outputs under dist/: the pack tree, a deterministic "
            "nse-alpha-data-pack-<version>.tar.gz with its .sha256, a build "
            "summary and a staged Kaggle upload directory."))
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                        help=f"upstream read-only data root (default: {DEFAULT_SOURCE})")
    parser.add_argument("--dist", type=Path, default=DEFAULT_DIST,
                        help=f"output directory (default: {DEFAULT_DIST})")
    parser.add_argument("--version", default=DEFAULT_VERSION,
                        help=f"pack version (default: {DEFAULT_VERSION})")
    parser.add_argument("--no-archive", action="store_true",
                        help="assemble the tree and manifest without the tar.gz")
    parser.add_argument("--reuse-bars", action="store_true",
                        help="reuse an already-derived adjusted_bars.parquet "
                             "from a previous build (development only)")
    parser.add_argument("--summary-json", type=Path, default=None,
                        help="also write the build summary JSON to this path")
    args = parser.parse_args(argv)

    try:
        summary = build(args.source, args.dist, args.version,
                        make_archive=not args.no_archive,
                        reuse_bars=args.reuse_bars)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.summary_json is not None:
        write_json(args.summary_json, summary)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
