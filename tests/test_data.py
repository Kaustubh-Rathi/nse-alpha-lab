from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import threading
import urllib.error
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb

from nse_pipeline.build.catalog import (
    action_rows, build_actions, build_database, normalize_prices, parse_action, read_pr_zip,
)
from nse_pipeline.download.client import download, fetch_archives, fetch_recent, report_date, request
# ``get_data`` is the legacy acquisition CLI and is not part of the public
# package: this repo ships the backtest engine, research drivers and strategy
# lab, not the NSE scraper. The single helper it contributed is inlined here so
# this module keeps testing the price/download parsing behaviours it covers.
def default_cutoff(today: date | None = None) -> date:
    """Exclude the current, potentially unfinished calendar day by default."""
    return (today or date.today()) - timedelta(days=1)

TEMP_ROOT = Path.home() / "AppData" / "Local" / "Temp" / "opencode"
if not TEMP_ROOT.exists():
    TEMP_ROOT = None


def csv_report(symbol="OLDFAILED", day="03-JAN-2000", close="11", isin="") -> bytes:
    return (
        "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,ISIN\n"
        f"{symbol},EQ,10,12,9,{close},10,1000,11000,{day},{isin}\n"
    ).encode()


def zipped(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


class PriceTests(unittest.TestCase):
    def test_default_cutoff_excludes_current_calendar_day(self):
        self.assertEqual(default_cutoff(date(2026, 11, 8)), date(2026, 11, 7))
        self.assertEqual(default_cutoff(date(2026, 1, 1)), date(2025, 12, 31))

    def test_locale_independent_filenames(self):
        self.assertEqual(report_date("nseBhav/2000/cm03JAN2000bhav.csv"), date(2000, 1, 3))
        self.assertEqual(report_date("BhavCopy_NSE_CM_0_0_0_20261106_F_0000.csv.zip"), date(2026, 11, 6))
        self.assertEqual(report_date("PR220111.zip"), date(2011, 1, 22))
        self.assertEqual(report_date("cm03JAN2000bhav.csv.zip"), date(2000, 1, 3))
        self.assertIsNone(report_date("README.txt"))

    def test_legacy_prices_not_adjusted_or_forward_filled(self):
        frame = normalize_prices(csv_report(), date(2000, 1, 3), "fixture")
        self.assertEqual(frame.iloc[0]["close"], 11)
        self.assertTrue(frame.iloc[0]["valid_ohlcv"])
        self.assertTrue(frame["isin"].isna().all())

    def test_modern_udiff_schema(self):
        content = (
            "TradDt,TckrSymb,SctySrs,ISIN,OpnPric,HghPric,LwPric,ClsPric,PrvsClsgPric,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd\n"
            "2026-11-06,TEST,EQ,INE123456789,10,12,9,11,10,120,1320,5\n"
        ).encode()
        row = normalize_prices(content, date(2026, 11, 6), "fixture").iloc[0]
        self.assertEqual(row["trades"], 5)
        self.assertEqual(row["traded_value"], 1320)
        self.assertEqual(row["volume"], 120)

    def test_bad_prices_are_retained_and_flagged(self):
        frame = normalize_prices(csv_report(close="0"), date(2000, 1, 3), "fixture")
        self.assertEqual(len(frame), 1)
        self.assertFalse(frame.iloc[0]["valid_ohlcv"])

    def test_na_is_a_real_nse_series_not_missing_data(self):
        content = csv_report().replace(b",EQ,", b",NA,")
        frame = normalize_prices(content, date(2000, 1, 3), "fixture")
        self.assertEqual(frame.iloc[0]["series"], "NA")

    def test_unclassified_exchange_rows_are_retained_without_guessing_series(self):
        content = csv_report(isin="INE148I07GL3").replace(b",EQ,", b",,")
        frame = normalize_prices(content, date(2000, 1, 3), "fixture")
        self.assertEqual(len(frame), 1)
        self.assertTrue(frame["series"].isna().all())
        self.assertEqual(frame.iloc[0]["isin"], "INE148I07GL3")

    def test_unknown_schema_is_not_silently_skipped(self):
        with self.assertRaisesRegex(ValueError, "missing required column"):
            normalize_prices(b"SYMBOL,SERIES\nTEST,EQ\n", date(2000, 1, 3), "fixture")

    def test_report_date_must_match_filename(self):
        with self.assertRaisesRegex(ValueError, "report date differs"):
            normalize_prices(csv_report(), date(2000, 1, 4), "fixture")

    def test_duplicates_fail(self):
        content = csv_report()
        content += content.splitlines(keepends=True)[1]
        with self.assertRaisesRegex(ValueError, "duplicate security rows"):
            normalize_prices(content, date(2000, 1, 3), "fixture")


class ActionTests(unittest.TestCase):
    def test_parallel_year_merge_preserves_earliest_source_observation(self):
        report = b"SYMBOL,SERIES,PURPOSE,EX_DT,RECORD_DT\nTEST,EQ,Bonus 1:1,03/01/2001,04/01/2001\n"
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            root = Path(temp)
            (root / "raw").mkdir()
            (root / "raw" / "PR_bhav-master.zip").write_bytes(zipped({
                "PR311200.zip": zipped({"bc31122000.csv": report}),
                "PR020101.zip": zipped({"bc02012001.csv": report}),
            }))
            build_actions(root, date(2001, 1, 3), workers=2)
            path = root / "processed" / "actions" / "actions.parquet"
            with duckdb.connect() as db:
                row = db.execute("SELECT first_observed,last_observed,source FROM read_parquet(?)", [str(path)]).fetchall()
            self.assertEqual(len(row), 1)
            self.assertEqual(row[0][0], date(2000, 12, 31))
            self.assertEqual(row[0][1], date(2001, 1, 2))
            self.assertIn("PR311200.zip", row[0][2])

    def test_bonus_multiplier(self):
        result = parse_action("Bonus 1:2")
        self.assertEqual(result["share_multiplier"], 1.5)
        self.assertEqual(result["parse_status"], "parsed_not_applied")

    def test_split_multiplier(self):
        result = parse_action("Face Value Split From Rs 10/- To Rs 2/-")
        self.assertEqual(result["share_multiplier"], 5)

    def test_exchange_abbreviations_do_not_change_split_meaning(self):
        for purpose in ("FVSPLT FRM RS 10 TO RS 2", "FV SPLT FRM RS 5 TO RE 1",
                        "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"):
            self.assertEqual(parse_action(purpose)["share_multiplier"], 5)

    def test_dividend_is_not_used_to_adjust_volume(self):
        result = parse_action("Interim Dividend Rs 5 Per Share")
        self.assertEqual(result["dividend_per_share"], 5)
        self.assertIsNone(result["share_multiplier"])

    def test_compound_actions_and_percentage_dividends_require_review(self):
        for purpose in ("Bonus 1:1 / Dividend Rs 5", "Dividend 200%", "Rights 1:4", "Split"):
            self.assertEqual(parse_action(purpose)["parse_status"], "needs_review")

    def test_nested_pr_zip(self):
        content = zipped({"bc030100.csv":
            b"SYMBOL,SERIES,PURPOSE,EX_DT,RECORD_DT\nOLDFAILED,EQ,Bonus 1:1,03/01/2000,04/01/2000\n"})
        rows = read_pr_zip(content, date(2000, 1, 3), "fixture")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ex_date"], date(2000, 1, 3))

    def test_unquoted_comma_in_purpose_is_preserved_and_flagged(self):
        report = (
            b"SERIES,SYMBOL,SECURITY,RECORD_DT,BC_STRT_DT,BC_END_DT,EX_DT,ND_STRT_DT,ND_END_DT,PURPOSE\n"
            b"BE,TCS,TATA CONSULTANCY SERV LTD,18/10/2019, , ,17/10/2019, , ,INT DIV-RS 5, SPL DIV-RS 5\n"
        )
        rows = action_rows(report, date(2019, 10, 11), "fixture")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["purpose"], "INT DIV-RS 5, SPL DIV-RS 5")
        self.assertTrue(rows[0]["csv_overflow_repaired"])
        self.assertEqual(rows[0]["parse_status"], "needs_review")


class DownloadTests(unittest.TestCase):
    def test_parallel_archives_keep_all_manifest_records(self):
        barrier = threading.Barrier(2, timeout=10)
        def release(tag):
            filename = "nseBhav.zip" if tag == "2025-master" else "PR_bhav-master.zip"
            return {"html_url": f"https://example.invalid/{tag}", "published_at": "2026-01-01",
                    "assets": [{"name": filename, "url": f"https://example.invalid/{filename}",
                                "size": 100, "browser_download_url": f"https://example.invalid/{filename}"}]}
        def get_release(url):
            return io.BytesIO(json.dumps(release(url.rsplit("/", 1)[1])).encode())
        def fake_download(url, target, size, digest):
            barrier.wait()  # This fails if the implementation accidentally serializes downloads.
            return {"file": target.name, "bytes": size, "sha256": "fixture"}
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            with patch("nse_pipeline.download.client.request", side_effect=get_release), patch("nse_pipeline.download.client.download", side_effect=fake_download):
                fetch_archives(Path(temp), ["prices", "actions"], workers=2)
            records = json.loads((Path(temp) / "manifests" / "downloads.json").read_text())
            self.assertEqual(set(records), {"prices", "actions"})

    def test_asset_api_requests_binary_not_metadata(self):
        with patch("nse_pipeline.download.client.urllib.request.urlopen") as open_url:
            request("https://api.github.com/repos/owner/repo/releases/assets/123")
        self.assertEqual(open_url.call_args.args[0].get_header("Accept"), "application/octet-stream")

    def test_update_records_and_stops_on_access_denied(self):
        error = urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", {}, None)
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            with patch("nse_pipeline.download.client.download", side_effect=error) as fetch, patch("nse_pipeline.download.client.time.sleep"):
                with self.assertRaisesRegex(RuntimeError, "update incomplete"):
                    fetch_recent(Path(temp), date(2020, 1, 1), date(2020, 1, 5))
                self.assertEqual(fetch.call_count, 2)
            records = json.loads((Path(temp) / "manifests" / "recent-downloads.json").read_text())
            self.assertTrue(all(row["http_status"] == 403 for row in records.values()))

    def test_actions_only_skips_price_requests(self):
        error = urllib.error.HTTPError("https://example.invalid", 404, "Not Found", {}, None)
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            with patch("nse_pipeline.download.client.download", side_effect=error) as fetch, patch("nse_pipeline.download.client.time.sleep"):
                fetch_recent(Path(temp), date(2020, 1, 1), date(2020, 1, 2),
                             include_actions=True, include_prices=False)
                self.assertEqual(fetch.call_count, 2)
                urls = [call.args[0] for call in fetch.call_args_list]
                self.assertTrue(all("/pr/PR" in url for url in urls))

    def test_cached_zip_checks_publisher_hash(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            path = Path(temp) / "archive.zip"
            content = zipped({"report.csv": csv_report()})
            path.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            result = download("https://example.invalid/unused", path, len(content), "sha256:" + digest)
            self.assertTrue(result["publisher_digest_verified"])
            with self.assertRaisesRegex(ValueError, "SHA256"):
                download("https://example.invalid/unused", path, len(content), "sha256:" + "0" * 64)

    def test_resume_appends_only_the_requested_bytes(self):
        content = zipped({"report.csv": csv_report()})
        class Response(io.BytesIO):
            status = 206
            headers = {"Content-Range": f"bytes 10-{len(content)-1}/{len(content)}",
                       "Content-Length": str(len(content) - 10)}
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            path = Path(temp) / "archive.zip"
            path.with_suffix(".zip.part").write_bytes(content[:10])
            with patch("nse_pipeline.download.client.request", return_value=Response(content[10:])) as request:
                download("https://example.invalid/test", path, len(content), hashlib.sha256(content).hexdigest())
                self.assertEqual(request.call_args.args[1], {"Range": "bytes=10-"})
            self.assertEqual(path.read_bytes(), content)

    def test_ignored_range_restarts_instead_of_corrupting_file(self):
        content = zipped({"report.csv": csv_report()})
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Length": str(len(content))}
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            path = Path(temp) / "archive.zip"
            path.with_suffix(".zip.part").write_bytes(b"old-partial")
            with patch("nse_pipeline.download.client.request", return_value=Response(content)):
                download("https://example.invalid/test", path, len(content))
            self.assertEqual(path.read_bytes(), content)


class PipelineTests(unittest.TestCase):
    def test_build_preserves_old_names_and_catalog_obeys_requested_range(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            root = Path(temp)
            (root / "raw").mkdir()
            (root / "raw" / "nseBhav.zip").write_bytes(zipped({
                "nseBhav/2000/cm03JAN2000bhav.csv": csv_report(),
                "nseBhav/2001/cm02JAN2001bhav.csv": csv_report("NEWNAME", "02-JAN-2001", isin="INE123456789"),
            }))
            (root / "raw" / "PR_bhav-master.zip").write_bytes(zipped({
                "PR030100.zip": zipped({"bc030100.csv":
                    b"SYMBOL,SERIES,PURPOSE,EX_DT,RECORD_DT\nOLDFAILED,EQ,Bonus 1:1,03/01/2000,04/01/2000\n"}),
            }))
            (root / "processed" / "older_actions").mkdir(parents=True)
            (root / "processed" / "older_actions" / "nse-api-actions.jsonl").write_text(json.dumps({
                "symbol": "OLDFAILED", "series": "EQ", "ex_date": "2000-01-03",
                "subject": "Bonus 1:1", "isin": "INE123456789", "action_tags": ["bonus"],
                "identity_status": "test", "adjustment_status": "not_applied",
                "source_url": "https://example.invalid/actions",
            }) + "\n")
            build_database(root, date(2000, 1, 1), date(2001, 12, 31), workers=2)
            with duckdb.connect(str(root / "market.duckdb"), read_only=True) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM bars").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT count(*) FROM bars WHERE symbol='OLDFAILED'").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM corporate_actions").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM pre2011_actions").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM corporate_actions_all").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT count(*) FROM cash_equity_candidates").fetchone()[0], 2)
            report = json.loads((root / "reports" / "validation.json").read_text())
            self.assertFalse(report["ready_for_unbiased_backtesting"])
            self.assertFalse(json.loads((root / "reports" / "readiness.json").read_text())["ready_for_backtesting"])
            build_database(root, date(2001, 1, 1), date(2001, 12, 31), include_actions=False)
            with duckdb.connect(str(root / "market.duckdb"), read_only=True) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM bars").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT min(date) FROM bars").fetchone()[0], date(2001, 1, 2))
                self.assertEqual(db.execute("SELECT count(*) FROM pre2011_actions").fetchone()[0], 1)
                with self.assertRaises(duckdb.CatalogException):
                    db.execute("SELECT count(*) FROM corporate_actions_all").fetchone()


if __name__ == "__main__":
    unittest.main()
