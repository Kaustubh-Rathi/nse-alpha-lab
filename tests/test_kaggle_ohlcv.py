import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

SPEC = importlib.util.spec_from_file_location(
    "kaggle_ohlcv", Path(__file__).resolve().parents[1] / "src/nse_pipeline/thirdparty/kaggle_ohlcv.py")
kag = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kag)

COLS = kag.EXPECTED_COLUMNS


def make_df(rows=100, tickers=("RELIANCE.NS", "TCS.NS")):
    dates = pd.date_range("2015-01-01", periods=rows, freq="B").strftime("%Y-%m-%d %H:%M:%S+05:30")
    data = []
    for t in tickers:
        for i, d in enumerate(dates):
            close = 100.0 + i
            data.append({"Date": d, "Ticker": t, "Company_Name": "X Ltd.", "Sector": "IT",
                         "Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": 1000 + i,
                         "Dividend": 0.0, "Stock_Split": None, "Daily_Return": 0.001, "Volatility_20D": 0.02,
                         "MA_50": close, "MA_200": close, "Market_Cap": 1e12, "PE_Ratio": 20.0, "Forward_PE": 18.0,
                         "PEG_Ratio": None, "Price_to_Book": 2.0, "Dividend_Yield": 1.0, "EPS": 5.0, "Beta": 0.5,
                         "52Week_High": 120.0, "52Week_Low": 90.0})
    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["Date"].str.slice(0, 10)).dt.date
    df["symbol"] = df["Ticker"].map(kag.normalize_symbol)
    return df


class KaggleTests(unittest.TestCase):
    def test_schema_and_coverage(self):
        df = make_df()
        s = kag.validate_schema(df)
        self.assertTrue(s["schema_ok"])
        self.assertEqual(s["missing_columns"], [])
        self.assertEqual(s["n_rows"], 200)
        self.assertEqual(s["n_symbols"], 2)
        self.assertEqual(s["n_duplicate_date_symbol"], 0)

    def test_parse_and_normalize(self):
        self.assertEqual(str(kag.parse_kaggle_date("2026-01-30 00:00:00+05:30")), "2026-01-30")
        self.assertEqual(str(kag.parse_kaggle_date("1999-01-01")), "1999-01-01")
        self.assertEqual(kag.normalize_symbol("RELIANCE.NS"), "RELIANCE")
        self.assertEqual(kag.normalize_symbol("M&M.NS"), "M&M")
        self.assertEqual(kag.normalize_symbol("BAJAJ-AUTO.NS"), "BAJAJ-AUTO")

    def test_fundamentals_not_pit_safe(self):
        df = make_df()
        pit = kag.fundamentals_pit_audit(df)
        self.assertFalse(pit["has_publication_dates"])
        self.assertFalse(pit["pit_safe_for_backtesting"])
        self.assertFalse(pit["fundamentals"]["Market_Cap"]["pit_safe"])
        self.assertFalse(pit["fundamentals"]["PE_Ratio"]["has_publication_date"])

    def test_thirdparty_label_and_no_db_touch(self):
        df = make_df()
        with tempfile.TemporaryDirectory() as tmp:
            imp = kag.import_thirdparty(df, Path(tmp) / "out")
            self.assertEqual(imp["source"], "THIRDPARTY-KAGGLE")
            bars = pd.read_parquet(imp["bars_parquet"])
            self.assertEqual(set(bars["source"].unique()), {"THIRDPARTY-KAGGLE"})
            self.assertTrue(bars["is_adjusted"].all())
            snap = pd.read_parquet(imp["snapshot_parquet"])
            self.assertEqual(len(snap), 2)
            self.assertFalse(snap["pit_safe"].any())

    def test_invalid_rows_detected(self):
        df = make_df(rows=10)
        df.loc[0, "High"] = 50.0  # High < Low
        s = kag.validate_schema(df)
        self.assertGreaterEqual(s["n_invalid_ohlcv"], 1)

    def test_compare_counts_and_ratio_constancy(self):
        import duckdb
        df = make_df(rows=60)
        with tempfile.TemporaryDirectory() as tmp:
            dbp = Path(tmp) / "m.duckdb"
            with duckdb.connect(str(dbp)) as db:
                rows = []
                for r in df.itertuples():
                    rows.append((str(r.date), r.symbol, "EQ", r.Open, r.High, r.Low, r.Close, r.Close - 1, r.Volume, 0.0, 1))
                db.execute("CREATE TABLE bars (date DATE, symbol VARCHAR, series VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, previous_close DOUBLE, volume BIGINT, traded_value DOUBLE, trades BIGINT)")
                db.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            m, summary = kag.compare_to_official(df, dbp)
            self.assertEqual(summary["symbols"], ["RELIANCE", "TCS"])
            for sym in ("RELIANCE", "TCS"):
                self.assertGreater(summary["per_symbol"][sym]["n_compared"], 0)
                # identical fixture => raw matches, ratio ~1
                self.assertEqual(summary["per_symbol"][sym]["close_match_raw"],
                                 summary["per_symbol"][sym]["n_compared"])

    def test_build_writes_reports_and_sample(self):
        df = make_df(rows=30)
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            raw = data / "raw" / "kaggle_ohlcv"
            raw.mkdir(parents=True)
            (raw / kag.HIST_FILE).write_text("placeholder")
            import duckdb
            with duckdb.connect(str(data / "market.duckdb")) as db:
                db.execute("CREATE TABLE bars (date DATE, symbol VARCHAR, series VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, previous_close DOUBLE, volume BIGINT, traded_value DOUBLE, trades BIGINT)")
            # Write a real CSV then build
            df_out = df[COLS].copy()
            df_out.to_csv(raw / kag.HIST_FILE, index=False)
            cov = kag.build_all(data, raw, data / "processed" / "kaggle_ohlcv", do_download=False)
            self.assertTrue((data / "reports" / "kaggle-ohlcv-coverage.json").exists())
            self.assertTrue((data / "reports" / "kaggle-ohlcv-research.md").exists())
            j = json.loads((data / "reports" / "kaggle-ohlcv-coverage.json").read_text())
            self.assertEqual(j["license"], "CC0-1.0")
            self.assertEqual(j["import"]["source"], "THIRDPARTY-KAGGLE")

    def test_ticker_map(self):
        df = make_df()
        m = kag.ticker_map(df)
        self.assertEqual(m["RELIANCE.NS"], "RELIANCE")


if __name__ == "__main__":
    unittest.main()
