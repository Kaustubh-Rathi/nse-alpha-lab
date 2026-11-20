import importlib.util
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path
from unittest import mock

import duckdb


SPEC = importlib.util.spec_from_file_location("older_actions", Path(__file__).resolve().parents[1] / "src/nse_pipeline/actions/older.py")
older = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(older)


class OlderActionsTests(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(str(older.parse_date("04-Jan-2000")), "2000-01-04")
        self.assertEqual(str(older.parse_date("15-Apr-02")), "2002-04-15")
        self.assertIsNone(older.parse_date("-"))
        with self.assertRaises(ValueError):
            older.parse_date("not-a-date")

    def test_wrong_year_or_error_payload_rejected(self):
        with self.assertRaises(ValueError):
            older.validate_api({"error": "blocked"}, 2010)
        with self.assertRaises(ValueError):
            older.validate_api([{"symbol": "X", "subject": "Bonus 1:1", "exDate": "01-Jan-2011"}], 2010)
        with self.assertRaises(ValueError):
            older.api_url(2011)

    def test_labels_are_not_adjustment_factors(self):
        self.assertEqual(older.action_tags("Fv Split Rs.10 To Re.1"), ["split"])
        self.assertEqual(older.action_tags("Agm/Div-Rs.3.50 Per Share"), ["dividend"])
        self.assertEqual(older.action_tags("Nil"), ["other"])

    def test_legacy_nse_csv_nonbreaking_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.csv"
            path.write_bytes(b"Symbol,Company\nTEST,Legacy\xa0Limited\n")
            self.assertEqual(older.csv_rows(path)[0]["Company"], "Legacy\xa0Limited")

    def test_build_preserves_original_identity_and_missing_years(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            original = {"symbol": "NEWNAME", "subject": "Bonus 1:1", "exDate": "04-Jan-2000", "isin": "SOURCE_ISIN"}
            (raw / "nse-api-2000.json").write_text(json.dumps([original]))
            result = older.build(raw, root / "out", root / "out/coverage.json")
            row = json.loads((root / "out/nse-api-actions.jsonl").read_text())
            self.assertEqual(row["symbol"], original["symbol"])
            self.assertEqual(row["isin"], original["isin"])
            self.assertEqual(row["adjustment_status"], "not_applied")
            self.assertNotIn("factor", row)
            self.assertEqual(result["missing_years"], list(range(2001, 2011)))
            self.assertFalse(result["complete"])

    def test_upstream_local_midnight_not_utc_previous_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "source.db"
            with closing(sqlite3.connect(db)) as con:
                con.executescript("CREATE TABLE Stocks(id INTEGER, name TEXT); CREATE TABLE Actions(stock_id INTEGER, hash TEXT, subject TEXT, exDate INTEGER, recDate INTEGER); INSERT INTO Stocks VALUES(1,'TEST'); INSERT INTO Actions VALUES(1,'h','Bonus',946665000,NULL);")
                con.commit()
            before = db.read_bytes()
            rows, stats = older.sqlite_rows(db)
            self.assertEqual(rows[0]["ex_date"], "2000-01-01")
            self.assertEqual(stats["timestamp_day_remainders"], {"66600": 1})
            self.assertEqual(before, db.read_bytes())

    def test_access_limit_stops_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            error = urllib.error.HTTPError("https://www.nseindia.com/", 429, "Limited", {}, None)
            with mock.patch.object(older.urllib.request, "urlopen", side_effect=error) as request:
                with self.assertRaises(urllib.error.HTTPError):
                    older.acquire(Path(tmp))
                self.assertEqual(request.call_count, 1)
            manifest = json.loads((Path(tmp) / "acquisition.json").read_text())
            self.assertIn("429", manifest["nse-api-2000.json"]["error"])

    def test_company_key_keeps_only_exact_name_lead(self):
        self.assertEqual(older.company_key("Godrej Foods Limited"), "GODREJ FOODS")
        self.assertEqual(older.company_key("Anik Ship Breaking Industries Ltd."),
                         "ANIK SHIP BREAKING INDUSTRIES")
        self.assertNotEqual(older.company_key("Anik Industries Limited"),
                            older.company_key("Anik Ship Breaking Industries Ltd."))

    def test_probe_checks_compare_multisets_not_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            annual = [{"symbol": "A", "subject": "Bonus 1:1", "exDate": "04-Jan-2000"},
                      {"symbol": "B", "subject": "Agm", "exDate": "05-Feb-2000"},
                      {"symbol": "C", "subject": "Agm", "exDate": "05-May-2000"}]
            (raw / "nse-api-2000.json").write_text(json.dumps(annual))
            # Same Q1 multiset, different order.
            (raw / "probe-2000-q1.json").write_text(json.dumps([annual[1], annual[0]]))
            checks = older.probe_checks(raw)
            self.assertTrue(checks["all_tested_slices_equal"])
            self.assertEqual(checks["quarter_slices"]["2000"]["probe_rows"], 2)

    def test_delisted_same_name_lead_is_not_verified_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            (raw / "nse-delisted.csv").write_bytes(
                b"Symbol,Company,Delisted Date,Type of Delisting\n"
                b"GFL,Godrej Foods Limited,19-Apr-05,Voluntary Delisting\n"
                b"NOPE,Nowhere Limited,26-Jul-04,Compulsory Delisting\n")
            (raw / "nse-symbolchange.csv").write_bytes(
                b"NAME OF THE COMPANY,OLD SYMBOL,NEW SYMBOL,EFFECTIVE DATE\n"
                b"Some Other Ltd,OLD,NEW,01-Jan-2005\n")
            official = [{"symbol": "GODREJFOOD", "subject": "Agm", "exDate": "30-Aug-2000",
                         "comp": "Godrej Foods Limited", "ex_date": "2000-08-30"}]
            rows = older.build_delisted(raw / "nse-delisted.csv", official,
                                        older.csv_rows(raw / "nse-symbolchange.csv"))
            gfl = next(row for row in rows if row["symbol"] == "GFL")
            self.assertEqual(gfl["exact_symbol_api_rows"], 0)
            self.assertEqual(gfl["same_company_name_api_symbols"], ["GODREJFOOD"])
            self.assertEqual(gfl["resolution_status"],
                             "unresolved_same_company_name_lead_not_verified")
            nope = next(row for row in rows if row["symbol"] == "NOPE")
            self.assertEqual(nope["resolution_status"], "unresolved_no_official_identity_evidence")

    def test_bars_diagnostics_never_emits_factor(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bars.db"
            con = duckdb.connect(str(db_path))
            con.execute("CREATE TABLE t(date DATE, symbol VARCHAR, series VARCHAR, "
                        "close DOUBLE, previous_close DOUBLE)")
            con.execute("INSERT INTO t VALUES "
                        "(DATE '2010-01-02','S','EQ',100,100),"
                        "(DATE '2010-01-03','S','EQ',50,100),"
                        "(DATE '2010-01-04','S','EQ',50,50),"
                        "(DATE '2010-01-02','U','EQ',10,10),"
                        "(DATE '2010-01-03','U','EQ',60,10)")
            con.execute("CREATE VIEW bars AS SELECT * FROM t")
            con.close()
            official = [{"symbol": "S", "subject": "Fv Split Rs.10 To Rs.5",
                         "ex_date": "2010-01-03", "action_tags": ["split"]}]
            summary, queue, presence = older.bars_diagnostics(db_path, official, ["U"])
            self.assertTrue(summary["read_only"])
            self.assertFalse(summary["is_official_action_source"])
            semantics = summary["previous_close_semantics"]
            self.assertEqual(semantics["split_or_bonus_api_rows"], 1)
            self.assertEqual(semantics["split_or_bonus_rows_matched_to_large_move"], 1)
            self.assertEqual(len(queue), 1)
            self.assertEqual(queue[0]["symbol"], "U")
            self.assertEqual(queue[0]["status"], "unresolved_price_discontinuity_not_an_event")
            for row in queue:
                self.assertNotIn("factor", row)
            self.assertEqual(presence["U"][0]["rows"], 2)
            self.assertEqual(summary["years"]["2010"]["large_close_moves_20pct"], 2)

    def test_orders_page_checks_summarize_dates_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.html"
            path.write_text(
                '<a href="https://nsearchives.nseindia.com/x.pdf"></a>'
                '<span>25-Aug-2016</span><span>09-Oct-2026</span>')
            result = older.orders_page_checks(path, ["GFL", "ANIKSHIP"])
            self.assertEqual(result["pre2011_listed_orders"], 0)
            self.assertEqual(result["earliest_listed_order_date"], "2016-08-25")
            self.assertEqual(result["unresolved_symbols_named_on_page"], [])

    def test_build_reports_validation_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            rows = [{"symbol": "A", "subject": "Bonus 1:1", "exDate": "04-Jan-2000", "isin": "INE1"},
                    {"symbol": "A", "subject": "Bonus 1:1", "exDate": "04-Jan-2000", "isin": "INE1"}]
            (raw / "nse-api-2000.json").write_text(json.dumps(rows))
            result = older.build(raw, root / "out", root / "out/coverage.json")
            self.assertEqual(result["validation"]["official_raw_rows"] if "official_raw_rows" in result["validation"] else result["official_raw_rows"], 2)
            self.assertEqual(result["validation"]["exact_key_duplicates"], 1)
            self.assertEqual(result["validation"]["distinct_returned_symbols"], 1)


if __name__ == "__main__":
    unittest.main()
