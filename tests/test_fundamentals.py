import importlib.util
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "fundamentals", Path(__file__).resolve().parents[1] / "src/nse_pipeline/fundamentals/index.py")
fund = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fund)


def make_row(symbol="RELIANCE", toDate="31-Dec-2024", filingDate="30-Jul-2025 17:17",
             xbrl="https://nsearchives.nseindia.com/corporate/xbrl/TEST.xml", **extra):
    row = {"symbol": symbol, "companyName": "Reliance Industries Limited",
           "period": "Quarterly", "relatingTo": "Third Quarter",
           "fromDate": "01-Oct-2024", "toDate": toDate,
           "filingDate": filingDate, "broadCastDate": "30-Jul-2025 17:17:53",
           "audited": "Audited", "consolidated": "Consolidated",
           "isin": "INE002A01019", "seqNumber": "1", "xbrl": xbrl,
           "resultDetailedDataLink": None}
    row.update(extra)
    return row


class FundamentalsTests(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(str(fund.parse_date("30-Jul-2025 17:17")), "2025-07-30")
        self.assertEqual(str(fund.parse_date("31-Dec-2024")), "2024-12-31")
        self.assertIsNone(fund.parse_date("-"))
        with self.assertRaises(ValueError):
            fund.parse_date("not-a-date")

    def test_api_url_rejects_unknown_period_and_year(self):
        self.assertIn("period=Quarterly", fund.api_url("Quarterly", 2023))
        with self.assertRaises(ValueError):
            fund.api_url("Monthly", 2023)
        with self.assertRaises(ValueError):
            fund.api_url("Quarterly", 2000)

    def test_validate_rejects_payloads(self):
        with self.assertRaises(ValueError):
            fund.validate_api({"error": "blocked"}, "Quarterly")
        with self.assertRaises(ValueError):
            fund.validate_api([{"symbol": "X"}], "Quarterly")
        with self.assertRaises(ValueError):
            fund.validate_api([make_row(period="Annual")], "Quarterly")

    def test_placeholder_xbrl_detected(self):
        row = fund.normalize_row(make_row(xbrl="https://nsearchives.nseindia.com/corporate/xbrl/-"),
                                 "f.json", 0, "https://example.com")
        self.assertTrue(row["xbrl_is_placeholder"])
        real = fund.normalize_row(make_row(), "f.json", 0, "https://example.com")
        self.assertFalse(real["xbrl_is_placeholder"])
        self.assertFalse(real["values_extracted"])
        self.assertEqual(real["point_in_time_date"], "2025-07-30")
        self.assertEqual(real["period_end"], "2024-12-31")

    def test_build_preserves_publication_dates_no_fabricated_values(self):
        import uuid
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / uuid.uuid4().hex[:8]
            raw = root / "raw"
            raw.mkdir(parents=True)
            rows = [make_row(),
                    make_row(symbol="RESTATED", toDate="31-Dec-2024",
                             filingDate="15-Jan-2025 10:00")]
            rows[1]["period"] = "Quarterly"
            (raw / "nse-financial-results-quarterly-2023.json").write_text(json.dumps(rows))
            result = fund.build(raw, root / "out", root / "out/coverage.json")
            stored = [json.loads(line) for line in (root / "out/filing-index.jsonl").read_text().splitlines()]
            self.assertEqual(len(stored), 2)
            self.assertEqual(stored[0]["point_in_time_date"], "2025-07-30")
            for row in stored:
                for banned in ("revenue", "profit", "eps", "debt", "shares_outstanding"):
                    self.assertNotIn(banned, row, f"fabricated field {banned}")
                self.assertFalse(row["values_extracted"])
            self.assertFalse(result["values_extracted"])
            self.assertTrue(any("index only" in x.lower() for x in result["limitations"]))
            # missing files are explicit, not silently complete
            self.assertTrue(result["missing_files"])
            self.assertFalse(result["complete"])  # full range not present in this tmp fixture

    def test_restatements_are_separate_rows_not_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            first = make_row(filingDate="01-Jul-2025 10:00")
            restated = make_row(filingDate="01-Aug-2025 10:00")
            (raw / "nse-financial-results-quarterly-2023.json").write_text(json.dumps([first, restated]))
            fund.build(raw, root / "out", root / "out/coverage.json")
            stored = [json.loads(line) for line in (root / "out/filing-index.jsonl").read_text().splitlines()]
            self.assertEqual(len(stored), 2)
            self.assertEqual({r["point_in_time_date"] for r in stored}, {"2025-07-01", "2025-08-01"})

    def test_access_limit_stops_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            error = urllib.error.HTTPError("https://www.nseindia.com/", 429, "Limited", {}, None)
            with mock.patch.object(fund.urllib.request, "urlopen", side_effect=error) as request:
                with self.assertRaises(urllib.error.HTTPError):
                    fund.acquire(Path(tmp), periods=("Quarterly",), start_year=2023, end_year=2023)
                self.assertEqual(request.call_count, 1)
            manifest = json.loads((Path(tmp) / "acquisition.json").read_text())
            self.assertIn("429", manifest["nse-financial-results-quarterly-2023.json"]["error"])


if __name__ == "__main__":
    unittest.main()
