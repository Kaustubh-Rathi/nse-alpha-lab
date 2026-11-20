import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import nse_pipeline.thirdparty.yahoo as yahoo

EPOCH_DAY = 1704067200  # 2024-01-01 00:00:00+05:30 local (IST)
EPOCH_DAY2 = 1704153600


def chart_payload(symbol="RELIANCE.NS", granularity="1d", closes=[100.0, 101.0]):
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {
                        "symbol": symbol,
                        "dataGranularity": granularity,
                        "gmtoffset": 19800,
                        "firstTradeDate": EPOCH_DAY,
                    },
                    "timestamp": [EPOCH_DAY, EPOCH_DAY2][: len(closes)],
                    "indicators": {
                        "quote": [
                            {
                                "open": [100.0, 101.0][: len(closes)],
                                "high": [101.0, 102.0][: len(closes)],
                                "low": [99.0, 100.0][: len(closes)],
                                "close": list(closes),
                                "volume": [1000, 1100][: len(closes)],
                            }
                        ],
                        "adjclose": [{"adjclose": [50.0, 50.5][: len(closes)]}],
                    },
                    "events": {},
                }
            ],
        }
    }


class YahooTests(unittest.TestCase):
    def test_chart_url_encodes_index(self):
        url = yahoo.chart_url("^NSEI", period2=1700000000)
        self.assertIn("%5ENSEI", url)
        self.assertIn("interval=1d", url)
        self.assertIn("period1=0", url)

    def test_raw_name(self):
        self.assertEqual(yahoo.raw_name("^NSEI"), "yahoo-idx_NSEI-1d.json")
        self.assertEqual(yahoo.raw_name("RELIANCE.NS"), "yahoo-RELIANCE_NS-1d.json")

    def test_validate_rejects_error_and_wrong_granularity(self):
        with self.assertRaises(ValueError):
            yahoo.validate_chart({"chart": {"error": {"code": "Bad"}}}, "X")
        with self.assertRaises(ValueError):
            yahoo.validate_chart(chart_payload(granularity="1mo"), "RELIANCE.NS")

    def test_normalize_keeps_close_and_adjclose_distinct(self):
        bars = yahoo.normalize_bars(yahoo.validate_chart(chart_payload(), "RELIANCE.NS"))
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["close"], 100.0)
        self.assertEqual(bars[0]["adjclose"], 50.0)
        # Yahoo Close is split/bonus-adjusted; Adj adds dividends, so close/adj > 1 here.
        self.assertAlmostEqual(bars[0]["split_bonus_factor_close_over_adj"], 2.0)

    def test_normalize_drops_null_close_rows(self):
        payload = chart_payload(closes=[100.0, None])
        # second timestamp has None close: must be dropped, first kept.
        bars = yahoo.normalize_bars(yahoo.validate_chart(payload, "RELIANCE.NS"))
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["close"], 100.0)

    def test_compare_adjustment_identical_is_one(self):
        ybars = [{"date": "2026-01-02", "close": 100.0, "adjclose": 100.0}]
        hbars = [{"date": "2026-01-02", "close": 100.0, "adj_close": 100.0}]
        cmp = yahoo.compare_adjustment(ybars, hbars)
        self.assertEqual(cmp["common_dates"], 1)
        self.assertEqual(cmp["by_year"]["2026"]["median_yahoo_close_over_host_raw"], 1.0)
        self.assertEqual(cmp["by_year"]["2026"]["median_yahoo_adj_over_host_adj"], 1.0)

    def test_compare_adjustment_detects_dividend_gap(self):
        # Yahoo Adj 50 vs host split/bonus-only adj 100 -> ratio 0.5 (dividend gap).
        ybars = [{"date": "2020-01-02", "close": 100.0, "adjclose": 50.0}]
        hbars = [{"date": "2020-01-02", "close": 50.0, "adj_close": 100.0}]
        cmp = yahoo.compare_adjustment(ybars, hbars)
        self.assertEqual(cmp["by_year"]["2020"]["median_yahoo_adj_over_host_adj"], 0.5)

    def test_fundamentals_not_point_in_time_safe(self):
        summary = yahoo.summarize_fundamentals_tables(
            {"financials_annual": ["2022-03-31", "2026-03-31"]}
        )
        self.assertFalse(summary["publication_dates_present"])
        self.assertFalse(summary["point_in_time_safe"])
        self.assertEqual(summary["earliest_year"], "2022")
        self.assertEqual(summary["tables"]["financials_annual"]["n_period_ends"], 2)

    def test_build_reports_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, processed, report = Path(tmp) / "raw", Path(tmp) / "proc", Path(tmp) / "cov.json"
            raw.mkdir()
            coverage = yahoo.build(raw, processed, report)
            self.assertFalse(coverage["complete"])
            self.assertEqual(len(coverage["missing_files"]), 4)


if __name__ == "__main__":
    unittest.main()
