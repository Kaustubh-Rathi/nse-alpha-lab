"""Read-only relocated catalogs must never fall through to their old host."""
import hashlib
import json
import shutil
from datetime import date

import duckdb
import pandas as pd
import pytest

from nse_pipeline.build.catalog import PRICE_SCHEMA, create_catalog


def seed(data, count):
    folder = data / "processed/prices/year=2000"
    folder.mkdir(parents=True)
    rows = [dict(date=date(2000, 1, 3), symbol=f"X{i}", series="EQ", isin=None,
                 open=10., high=11., low=9., close=10., previous_close=10.,
                 volume=100, traded_value=1000., trades=1, valid_ohlcv=True,
                 source="hand-authored") for i in range(count)]
    pd.DataFrame(rows, columns=PRICE_SCHEMA.names).to_parquet(folder / "bars.parquet", index=False)


def test_readonly_catalog_uses_relocated_tree_and_preserves_database(tmp_path):
    from nse_pipeline.build.portable import open_catalog

    original, moved = tmp_path / "old", tmp_path / "moved ' data"
    seed(original, 2)
    create_catalog(original, {"2000": {}}, include_actions=False)
    seed(moved, 7)
    shutil.copy2(original / "market.duckdb", moved / "market.duckdb")
    before = hashlib.sha256((moved / "market.duckdb").read_bytes()).hexdigest()
    with open_catalog(moved) as db:
        assert db.execute("select count(*) from bars").fetchone() == (7,)
        assert db.execute("select count(*) from cash_equity_candidates").fetchone() == (7,)
        assert db.execute("select count(*) from security_history").fetchone() == (7,)
        with pytest.raises(duckdb.InvalidInputException):
            db.execute("create table forbidden(i integer)")
    assert hashlib.sha256((moved / "market.duckdb").read_bytes()).hexdigest() == before
    with duckdb.connect(str(moved / "market.duckdb"), read_only=True) as db:
        assert db.execute("select count(*) from bars").fetchone() == (2,)
    original.rename(tmp_path / "original source unavailable")
    with open_catalog(moved) as db:
        assert db.execute("select count(*) from bars").fetchone() == (7,)


def test_missing_relocated_prices_fails_instead_of_reading_original(tmp_path):
    from nse_pipeline.build.portable import open_catalog

    original, moved = tmp_path / "old", tmp_path / "empty"
    seed(original, 2)
    create_catalog(original, {"2000": {}}, include_actions=False)
    moved.mkdir()
    shutil.copy2(original / "market.duckdb", moved / "market.duckdb")
    with pytest.raises(FileNotFoundError, match="prices"):
        open_catalog(moved)


def test_physical_table_catalog_remains_supported(tmp_path):
    from nse_pipeline.build.portable import open_catalog

    path = tmp_path / "test.duckdb"
    with duckdb.connect(str(path)) as db:
        db.execute("create table bars as select 17 as sentinel")
    with open_catalog(database=path) as db:
        assert db.execute("select sentinel from bars").fetchone() == (17,)


def test_table_backed_catalog_rejects_external_reader_views(tmp_path):
    from nse_pipeline.build.portable import open_catalog

    source = tmp_path / "old.csv"
    source.write_text("value\n17\n", encoding="utf-8")
    path = tmp_path / "mixed.duckdb"
    with duckdb.connect(str(path)) as db:
        db.execute("create table bars as select 1 as sentinel")
        db.execute(f"create view old_source as select * from read_csv_auto('{source.as_posix()}')")
    with pytest.raises(ValueError, match="external views"):
        open_catalog(database=path)


def test_catalog_partition_selection_survives_extra_local_files(tmp_path):
    from nse_pipeline.build.portable import open_catalog

    seed(tmp_path, 2)
    extra = tmp_path / "processed/prices/year=2001"
    extra.mkdir()
    shutil.copy2(tmp_path / "processed/prices/year=2000/bars.parquet", extra / "bars.parquet")
    create_catalog(tmp_path, {"2000": {}}, include_actions=False)
    with open_catalog(tmp_path) as db:
        assert db.execute("select count(*) from bars").fetchone() == (2,)


def test_optional_action_views_are_local_and_missing_sources_fail_closed(tmp_path):
    from nse_pipeline.build.portable import open_catalog

    original, moved = tmp_path / "old", tmp_path / "moved"
    seed(original, 2)
    actions = original / "processed/actions/actions.parquet"
    actions.parent.mkdir()
    pd.DataFrame([dict(symbol="OLD", series="EQ", ex_date=date(2000, 1, 3),
                       purpose="SPLIT", action_type="split", source="fixture")]).to_parquet(actions)
    older = original / "processed/older_actions/nse-api-actions.jsonl"
    older.parent.mkdir()
    older.write_text(json.dumps(dict(symbol="OLD", series="EQ", ex_date="2000-01-03",
                                    subject="split", isin="INE0001", action_tags=["split"],
                                    identity_status="fixture", adjustment_status="fixture",
                                    source_url="fixture")) + "\n", encoding="utf-8")
    create_catalog(original, {"2000": {}}, include_actions=True)
    shutil.copytree(original, moved)
    pd.DataFrame([dict(symbol="LOCAL", series="EQ", ex_date=date(2000, 1, 3),
                       purpose="SPLIT", action_type="split", source="fixture")]).to_parquet(
                           moved / "processed/actions/actions.parquet")
    with open_catalog(moved) as db:
        assert db.execute("select symbol from corporate_actions").fetchone() == ("LOCAL",)
        assert db.execute("select count(*) from equity_corporate_actions_all").fetchone() == (2,)
    (moved / "processed/actions/actions.parquet").unlink()
    with pytest.raises(FileNotFoundError):
        open_catalog(moved)
