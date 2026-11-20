"""Tests for the read-only NIFTY index level parser over synthetic trees.

Policy under test: same-date same-level duplicates across sources collapse;
same-date conflicting levels raise; malformed rows/columns raise; the
filename date is authoritative over any content date. Synthetic fixtures
are regression tools, not historical evidence.
"""
import io
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nse_pipeline.analytics.index_levels import IndexLevelRecord, load_nifty_levels

D = Decimal
HEADER = ("Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
          "Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield")


def report(closing_value: str, index_date: str = "01-01-2020", nifty_name: str = "Nifty 50") -> str:
    return "\n".join([
        HEADER,
        f"{nifty_name},{index_date},100,101,99,{closing_value},0.1,0.10,1000000,500.0,20,3,1",
        f"Nifty Next 50,{index_date},200,201,199,200.5,0.5,0.25,999,100.0,24,4,1",
        f"India VIX,{index_date},13,14,12,13.5,0.5,3.85,0,0.0,,,",
        "",
    ])


def build_tree(root: Path, zip_members: dict[str, str], loose: dict[str, str]) -> Path:
    calendar = root / "raw" / "calendar"
    calendar.mkdir(parents=True)
    if zip_members:
        archive_path = root / "raw" / "nseIndices.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            for name, content in zip_members.items():
                archive.writestr(name, content)
    for name, content in loose.items():
        (calendar / name).write_text(content, encoding="utf-8")
    return root


class TestHappyPath:
    def test_zip_members_are_parsed_and_sorted(self, tmp_path):
        root = build_tree(tmp_path, {
            "nseIndices/2013/ind_close_all_08022013.csv": report("6495.55"),
            "nseIndices/2013/ind_close_all_07022013.csv": report("6500.00"),
        }, {})
        assert load_nifty_levels(root) == (
            IndexLevelRecord(date(2013, 2, 7), D("6500.00")),
            IndexLevelRecord(date(2013, 2, 8), D("6495.55")),
        )

    def test_filename_date_is_authoritative(self, tmp_path):
        # Audit 6.7.1: three archive members carry day/month-swapped content
        # dates; the filename 06042023 must win over content 04-06-2023.
        root = build_tree(tmp_path, {
            "nseIndices/2023/ind_close_all_06042023.csv": report("17800.00", index_date="04-06-2023"),
        }, {})
        assert load_nifty_levels(root) == (IndexLevelRecord(date(2023, 4, 6), D("17800.00")),)

    def test_historical_index_names_accepted(self, tmp_path):
        root = build_tree(tmp_path, {
            "nseIndices/2012/ind_close_all_03012012.csv": report("5100.00", nifty_name="S&P CNX Nifty"),
            "nseIndices/2015/ind_close_all_05012015.csv": report("8200.00", nifty_name="CNX Nifty"),
        }, {})
        assert [r.level for r in load_nifty_levels(root)] == [D("5100.00"), D("8200.00")]

    def test_loose_calendar_files_and_non_matching_names_ignored(self, tmp_path):
        root = build_tree(tmp_path, {}, {
            "ind_close_all_01022026.csv": report("24825.45"),
            "exchange-holidays.html": "<html></html>",
        })
        assert load_nifty_levels(root) == (IndexLevelRecord(date(2026, 2, 1), D("24825.45")),)


class TestDuplicates:
    def test_same_level_across_sources_collapses(self, tmp_path):
        root = build_tree(tmp_path, {
            "nseIndices/2023/ind_close_all_11042023.csv": report("18100.00"),
        }, {"ind_close_all_11042023.csv": report("18100")})
        records = load_nifty_levels(root)
        assert len(records) == 1
        assert records[0] == IndexLevelRecord(date(2023, 4, 11), D("18100.00"))

    def test_conflicting_levels_raise(self, tmp_path):
        root = build_tree(tmp_path, {
            "nseIndices/2023/ind_close_all_11042023.csv": report("18100.00"),
        }, {"ind_close_all_11042023.csv": report("18111.11")})
        with pytest.raises(ValueError, match="Conflicting NIFTY levels"):
            load_nifty_levels(root)

    def test_conflicting_rows_within_one_report_raise(self, tmp_path):
        content = HEADER + "\n" + "Nifty 50,01-01-2020,100,101,99,7000.00,0.1,0.10,1,1,1,1,1\n" \
                  + "CNX Nifty,01-01-2020,100,101,99,7000.10,0.1,0.10,1,1,1,1,1\n"
        root = build_tree(tmp_path, {"nseIndices/2020/ind_close_all_01012020.csv": content}, {})
        with pytest.raises(ValueError, match="within one report"):
            load_nifty_levels(root)


class TestMalformed:
    def test_unparseable_closing_value(self, tmp_path):
        root = build_tree(tmp_path, {
            "nseIndices/2020/ind_close_all_01012020.csv": report("N/A"),
        }, {})
        with pytest.raises(ValueError, match="Malformed closing index value"):
            load_nifty_levels(root)

    def test_non_positive_level(self, tmp_path):
        root = build_tree(tmp_path, {
            "nseIndices/2020/ind_close_all_01012020.csv": report("-100"),
        }, {})
        with pytest.raises(ValueError, match="Non-positive"):
            load_nifty_levels(root)

    def test_missing_required_column(self, tmp_path):
        root = build_tree(tmp_path, {
            "nseIndices/2020/ind_close_all_01012020.csv":
                "Index Name,Index Date,Closing Price\nNifty 50,01-01-2020,100\n",
        }, {})
        with pytest.raises(ValueError, match="missing required columns"):
            load_nifty_levels(root)

    def test_empty_tree(self, tmp_path):
        root = build_tree(tmp_path, {}, {})
        with pytest.raises(ValueError, match="No NIFTY index levels"):
            load_nifty_levels(root)


class TestReadOnly:
    def test_nothing_is_written(self, tmp_path):
        root = build_tree(tmp_path, {
            "nseIndices/2013/ind_close_all_08022013.csv": report("6495.55"),
        }, {"ind_close_all_01022026.csv": report("24825.45")})
        before = {p.relative_to(root): p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
        load_nifty_levels(root)
        after = {p.relative_to(root): p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
        assert before == after

    def test_records_are_frozen(self):
        record = IndexLevelRecord(date(2020, 1, 1), D("100"))
        with pytest.raises(Exception):
            record.level = D("101")


def test_zip_roundtrip_sanity():
    # Guard for the fixture builder itself: members really are readable back.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("nseIndices/2013/ind_close_all_08022013.csv", report("6495.55"))
    with zipfile.ZipFile(buffer) as archive:
        assert archive.read("nseIndices/2013/ind_close_all_08022013.csv").startswith(b"Index Name")
