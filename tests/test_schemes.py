import importlib.util
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "schemes", Path(__file__).resolve().parents[1] / "src/nse_pipeline/schemes/indexer.py")
schemes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(schemes)


def make_row(symbol="RAYMOND", an_dt="19-Dec-2024 18:20:10",
             desc="Scheme of Arrangement", attchmntFile=None, **extra):
    row = {"symbol": symbol, "sm_isin": "INE301F01017",
           "sm_name": "Raymond Limited", "an_dt": an_dt,
           "exchdisstime": an_dt, "desc": desc,
           "attchmntFile": attchmntFile or
           "https://nsearchives.nseindia.com/corporate/"
           "RAYMOND_19122024182010_SEintimationsigned.pdf",
           "attchmntText": "Intimation", "seq_id": "106000001"}
    row.update(extra)
    return row


class SchemesTests(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(str(schemes.parse_date("29-Jun-2024 20:51:51")), "2024-06-29")
        self.assertEqual(str(schemes.parse_date("08-Nov-2026")), "2026-11-08")
        self.assertEqual(str(schemes.parse_date("2024-06-29 20:51:51")), "2024-06-29")
        self.assertIsNone(schemes.parse_date("-"))
        with self.assertRaises(ValueError):
            schemes.parse_date("not-a-date")

    def test_api_urls(self):
        self.assertIn("from_date=01-01-2024", schemes.api_url(2024))
        self.assertIn("to_date=31-12-2024", schemes.api_url(2024))
        with self.assertRaises(ValueError):
            schemes.api_url(2010)
        with self.assertRaises(ValueError):
            schemes.api_url(2027)
        url = schemes.api_url_symbol("raymond", 2024)
        self.assertIn("symbol=RAYMOND", url)
        with self.assertRaises(ValueError):
            schemes.api_url_symbol("BAD SYMBOL!", 2024)
        with self.assertRaises(ValueError):
            schemes.api_url_symbol("RAYMOND", 2010)

    def test_validate_rejects_payloads(self):
        with self.assertRaises(ValueError):
            schemes.validate_api({"error": "blocked"}, 2024)
        with self.assertRaises(ValueError):
            schemes.validate_api([{"symbol": "X"}], 2024)
        with self.assertRaises(ValueError):
            schemes.validate_api([{"symbol": "X", "an_dt": "not-a-date", "desc": "d"}], 2024)
        with self.assertRaises(ValueError):
            schemes.validate_api(["not-a-dict"], 2024)
        self.assertEqual(len(schemes.validate_api([make_row()], 2024)), 1)

    def test_scheme_filter(self):
        for desc in ("Scheme of Arrangement", "Amalgamation/Merger",
                     "Demerger", "Restructuring"):
            self.assertTrue(schemes.is_scheme_row(make_row(desc=desc)), desc)
        for desc in ("Trading Window", "Credit Rating", "General Updates",
                     "Loss of Share Certificates"):
            self.assertFalse(schemes.is_scheme_row(make_row(desc=desc)), desc)

    def test_withdrawn_detection(self):
        self.assertTrue(schemes.is_withdrawn_row(make_row(
            desc="Scheme of Arrangement", attchmntText="Withdrawal of Scheme")))
        self.assertTrue(schemes.is_withdrawn_row(make_row(
            desc="Cancellation of amalgamation")))
        self.assertTrue(schemes.is_withdrawn_row(make_row(
            desc="Scheme of Arrangement", attchmntText="Company abandons the scheme")))
        self.assertTrue(schemes.is_withdrawn_row(make_row(
            desc="Scheme of Arrangement", attchmntFile=
            "https://nsearchives.nseindia.com/corporate/DIL_08112024121416_Intimation_-_Withdrawal_of_Scheme.pdf")))
        self.assertFalse(schemes.is_withdrawn_row(make_row(desc="Scheme of Arrangement")))
        self.assertFalse(schemes.is_withdrawn_row(make_row(desc="Demerger")))

    def test_dedup_key(self):
        row = make_row()
        self.assertEqual(schemes.dedup_key(row), schemes.dedup_key(make_row()))
        other = make_row(attchmntFile="https://nsearchives.nseindia.com/other.pdf")
        self.assertNotEqual(schemes.dedup_key(row), schemes.dedup_key(other))
        other2 = make_row(an_dt="20-Dec-2024 10:00:00")
        self.assertNotEqual(schemes.dedup_key(row), schemes.dedup_key(other2))

    def test_build_index_no_terms_no_price_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            dup = make_row()
            rows = [make_row(),
                    dup,  # exact duplicate: must be deduped
                    make_row(symbol="TCPLPACK", desc="Amalgamation/Merger",
                             an_dt="29-Jun-2024 20:51:51"),
                    make_row(symbol="UDS", desc="Scheme of Arrangement",
                             an_dt="27-Jun-2024 20:53:59",
                             attchmntText="Withdrawal of the scheme"),
                    make_row(symbol="OTHER", desc="Trading Window")]
            (raw / "nse-corporate-announcements-2024.json").write_text(json.dumps(rows))
            result = schemes.build(raw, root / "out", root / "out/coverage.json")
            stored = [json.loads(line) for line in
                      (root / "out/scheme-index.jsonl").read_text().splitlines()]
            self.assertEqual(len(stored), 3)  # 4 scheme-like minus 1 duplicate
            self.assertEqual(result["duplicate_rows_removed"], 1)
            self.assertEqual(result["scheme_rows"], 3)
            self.assertEqual(result["symbols"], 3)
            self.assertEqual(result["withdrawn_rows"], 1)
            for row in stored:
                for banned in ("swap_ratio_num", "swap_ratio_den", "exchange_ratio",
                               "cash_per_share", "successor_symbol", "price_factor",
                               "share_multiplier", "revenue", "profit"):
                    self.assertNotIn(banned, row, f"fabricated field {banned}")
                self.assertFalse(row["scheme_terms_extracted"])
                self.assertFalse(row["prices_adjusted"])
                self.assertTrue(row["scheme_like"])
                self.assertEqual(row["source_file"],
                                 "nse-corporate-announcements-2024.json")
            self.assertFalse(result["scheme_terms_extracted"])
            self.assertFalse(result["ratios_inferred"])
            self.assertFalse(result["prices_adjusted"])
            self.assertTrue(any("index only" in x.lower() for x in result["limitations"]))
            self.assertTrue(result["missing_files"])  # other years explicit
            self.assertFalse(result["complete"])

    def test_out_of_window_counted_not_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            rows = [make_row(an_dt="05-Jan-2025 10:00:00", desc="Scheme of Arrangement"),
                    make_row(an_dt="19-Dec-2024 18:20:10", desc="Scheme of Arrangement")]
            (raw / "nse-corporate-announcements-2024.json").write_text(json.dumps(rows))
            result = schemes.build(raw, root / "out", root / "out/coverage.json")
            self.assertEqual(result["scheme_rows"], 2)
            self.assertEqual(result["rows_outside_requested_year"], 1)

    def test_access_limit_stops_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            error = urllib.error.HTTPError(
                "https://www.nseindia.com/", 429, "Limited", {}, None)
            with mock.patch.object(schemes.urllib.request, "urlopen",
                                   side_effect=error) as request:
                with self.assertRaises(urllib.error.HTTPError):
                    schemes.acquire(Path(tmp), start_year=2024, end_year=2024)
                self.assertEqual(request.call_count, 1)
            manifest = json.loads((Path(tmp) / "acquisition.json").read_text())
            self.assertIn("429", manifest["nse-corporate-announcements-2024.json"]["error"])


if __name__ == "__main__":
    unittest.main()
