"""Catalog SQL shared by the writer and a read-only, relocatable reader.

Only TEMP views are installed by open_catalog. Never repair persisted SQL on a
read path, and never silently fall back to another machine's source files.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb

from nse_pipeline.paths import resolve_data


def sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def view_definitions(data: Path, *, partitions: dict | None = None,
                     include_actions: bool | None = None) -> dict[str, str]:
    data = resolve_data(data)
    if partitions is None:
        manifest = data / "manifests/prices-build.json"
        partitions = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {
            path.parent.name.split("=", 1)[1]: {} for path in
            sorted((data / "processed/prices").glob("year=*/bars.parquet"))
        }
    files = [data / "processed/prices" / f"year={year}" / "bars.parquet"
             for year in sorted(partitions, key=int)]
    if not files:
        raise FileNotFoundError(f"No local prices partitions under {data}")
    for path in files:
        if not path.is_file():
            raise FileNotFoundError(f"Missing local prices partition: {path}")
    quoted = ", ".join(f"'{sql_path(path)}'" for path in files)
    definitions = {
        "bars": f"SELECT * FROM read_parquet([{quoted}], hive_partitioning=false)",
        "universe_observations": "SELECT date, symbol, series, isin, volume, valid_ohlcv FROM bars",
        "equity_candidates": """SELECT *, CASE WHEN isin IS NOT NULL THEN 'ISIN:' || isin
            ELSE 'SYMBOL:' || symbol END AS security_key
            FROM bars WHERE series IN ('EQ', 'BE', 'BZ', 'SM', 'ST')""",
        "security_history": """SELECT security_key, isin, min(date) AS first_observed,
            max(date) AS last_observed, count(DISTINCT date) AS sessions,
            string_agg(DISTINCT symbol, ', ' ORDER BY symbol) AS symbols
            FROM equity_candidates GROUP BY security_key, isin""",
        "usable_equity_candidates": "SELECT * FROM equity_candidates WHERE valid_ohlcv AND volume > 0",
        "cash_equity_candidates": """SELECT * FROM usable_equity_candidates
            WHERE (isin LIKE 'INE%' OR isin IS NULL) AND symbol NOT IN (
                SELECT symbol FROM usable_equity_candidates
                WHERE isin LIKE 'INF%' OR isin LIKE 'IN9%')""",
    }
    actions = data / "processed/actions/actions.parquet"
    if include_actions is None:
        include_actions = actions.is_file()
    if include_actions:
        if not actions.is_file():
            raise FileNotFoundError(actions)
        definitions["corporate_actions"] = f"SELECT * FROM read_parquet('{sql_path(actions)}')"
        definitions["equity_corporate_actions"] = """SELECT * FROM corporate_actions
            WHERE series IN ('EQ', 'BE', 'BZ', 'SM', 'ST')"""
    older = data / "processed/older_actions/nse-api-actions.jsonl"
    if older.is_file():
        definitions["pre2011_actions"] = f"""SELECT symbol, series, CAST(ex_date AS DATE) AS ex_date,
            subject AS purpose, isin, action_tags, identity_status, adjustment_status,
            source_url AS source FROM read_json_auto('{sql_path(older)}')"""
        if include_actions:
            definitions["corporate_actions_all"] = """SELECT symbol, series, ex_date, purpose,
                NULL::VARCHAR AS isin, action_type, source, 'PR_bhav_2011_plus' AS dataset
                FROM corporate_actions UNION ALL SELECT symbol, series, ex_date, purpose,
                isin, NULL::VARCHAR AS action_type, source, 'NSE_API_pre2011' AS dataset
                FROM pre2011_actions"""
            definitions["equity_corporate_actions_all"] = """SELECT * FROM corporate_actions_all
                WHERE series IN ('EQ', 'BE', 'BZ', 'SM', 'ST')"""
    return definitions


MANAGED_VIEWS = (
    "bars", "universe_observations", "equity_candidates", "security_history",
    "usable_equity_candidates", "cash_equity_candidates", "corporate_actions",
    "equity_corporate_actions", "pre2011_actions", "corporate_actions_all",
    "equity_corporate_actions_all",
)


def open_catalog(data: str | Path | None = None, *, database: str | Path | None = None,
                 read_only: bool = True, config: dict | None = None) -> duckdb.DuckDBPyConnection:
    """Read local sources through TEMP views; also support physical-table fixtures.

    A supplied database filename wins; its parent is the default data root.
    Missing optional sources referenced by persisted views fail closed too.
    """
    if not read_only:
        raise ValueError("open_catalog is read-only; use create_catalog for writes")
    path = Path(database).resolve() if database is not None else resolve_data(data) / "market.duckdb"
    root = resolve_data(data) if data is not None else path.parent
    if not path.is_file():
        raise FileNotFoundError(path)
    db = duckdb.connect(str(path), read_only=True, config=config or {})
    try:
        tables = {row[0] for row in db.execute(
            "SELECT table_name FROM duckdb_tables() WHERE schema_name='main' AND NOT internal"
        ).fetchall()}
        views = dict(db.execute(
            "SELECT view_name, sql FROM duckdb_views() WHERE schema_name='main' AND NOT internal"
        ).fetchall())
        if tables or not views:
            # Do not overwrite explicit table-backed catalogs with disk globbing.
            # Reject external views rather than following an old absolute path.
            if any(re.search(r'\b(?:FROM|JOIN)\s+[\w".]+\s*\(', sql, re.I)
                   for sql in views.values()):
                raise ValueError("Table-backed catalog contains external views; explicit rebinding required")
            return db
        # Persisted SQL supplies selection metadata only, never source content.
        # Preserve a writer's bounded partition set even if extra files exist.
        years = re.findall(r"year=(\d+)/bars\.parquet", views.get("bars", "").replace("\\", "/"))
        if not years:
            raise FileNotFoundError("Catalog prices view has no recognized local partition mapping")
        definitions = view_definitions(root, partitions=dict.fromkeys(years),
                                       include_actions="corporate_actions" in views)
        missing = set(views).intersection(MANAGED_VIEWS) - definitions.keys()
        if missing:
            raise FileNotFoundError(f"Missing local sources for catalog views: {sorted(missing)}")
        unmanaged = set(views) - set(MANAGED_VIEWS)
        if unmanaged:
            raise ValueError(f"Unmanaged catalog views require explicit rebinding: {sorted(unmanaged)}")
        for name, sql in definitions.items():
            if name in views:
                db.execute(f'CREATE OR REPLACE TEMP VIEW "{name}" AS {sql}')
        return db
    except BaseException:
        db.close()
        raise


def catalog_is_relocatable(data: str | Path | None = None) -> dict:
    """Inspect persistent source SQL only; no query executes against old sources."""
    root = resolve_data(data)
    path = root / "market.duckdb"
    if not path.is_file():
        raise FileNotFoundError(path)
    with duckdb.connect(str(path), read_only=True) as db:
        external = {name: sql for name, sql in db.execute(
            "SELECT view_name, sql FROM duckdb_views() WHERE schema_name='main' AND NOT internal"
        ).fetchall() if "read_parquet(" in sql.lower() or "read_json" in sql.lower()}
    local = sql_path(root).lower() + "/"
    return {"data_root": str(root), "external_views": sorted(external),
            "requires_temp_rebinding": any(local not in sql.replace("\\", "/").lower()
                                            for sql in external.values())}
