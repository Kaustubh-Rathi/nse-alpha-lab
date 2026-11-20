from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.adjust.factors import (
    apply_adjustment,
    bar_factors,
    build_intervals,
    classify_actions,
)

ACTION_COLUMNS = ["symbol", "series", "ex_date", "action_type", "share_multiplier", "parse_status"]


def action(symbol="AAA", series="EQ", ex_date=date(2020, 6, 1), action_type="split",
           multiplier=2.0, status="parsed_not_applied", purpose="SPLIT"):
    row = {column: None for column in ACTION_COLUMNS}
    row.update({
        "symbol": symbol, "series": series, "ex_date": ex_date,
        "action_type": action_type, "share_multiplier": multiplier,
        "parse_status": status, "purpose": purpose,
    })
    return row


def actions_frame(rows):
    return pd.DataFrame(rows)


def bars_frame(rows):
    base = {column: None for column in
            ("symbol", "series", "date", "open", "high", "low", "close", "previous_close", "volume")}
    merged_rows = []
    for override in rows:
        merged = {**base, **override}
        merged_rows.append(merged)
    frame = pd.DataFrame(merged_rows)
    for column in ("open", "high", "low", "close", "previous_close"):
        frame[column] = frame[column].astype("float64")
    return frame


class ClassifyTests(unittest.TestCase):
    def test_needs_review_is_excluded_and_never_infers_a_factor(self):
        frame = classify_actions(actions_frame([action(status="needs_review", multiplier=None)]))
        self.assertEqual(frame.iloc[0]["decision"], "excluded")
        self.assertIn("needs_review", frame.iloc[0]["reason"])

    def test_dividend_is_excluded_without_price_adjustment(self):
        frame = classify_actions(actions_frame([action(action_type="dividend", multiplier=None)]))
        self.assertEqual(frame.iloc[0]["decision"], "excluded")
        self.assertEqual(frame.iloc[0]["reason"], "scope_not_split_bonus")

    def test_compound_type_is_excluded(self):
        frame = classify_actions(actions_frame([action(action_type="split+bonus")]))
        self.assertEqual(frame.iloc[0]["decision"], "excluded")

    def test_slash_compound_purpose_is_excluded(self):
        row = action(purpose="BONUS 1:1/FV SPL 10 TO 2")
        frame = classify_actions(actions_frame([row]),
                                 {("AAA", "EQ", date(2020, 6, 1)): 10})
        self.assertEqual(frame.iloc[0]["decision"], "excluded")
        self.assertEqual(frame.iloc[0]["reason"], "compound_purpose_ambiguous")

    def test_single_component_slash_purpose_stays_applied(self):
        row = action(purpose="AGM/FV SPLT FRM 10 TO 1")
        frame = classify_actions(actions_frame([row]),
                                 {("AAA", "EQ", date(2020, 6, 1)): 10})
        self.assertEqual(frame.iloc[0]["decision"], "applied")

    def test_invalid_multiplier_is_excluded(self):
        frame = classify_actions(actions_frame([action(multiplier=1.0)]))
        self.assertEqual(frame.iloc[0]["decision"], "excluded")
        self.assertEqual(frame.iloc[0]["reason"], "invalid_share_multiplier")

    def test_missing_multiplier_is_excluded(self):
        frame = classify_actions(actions_frame([action(multiplier=np.nan)]))
        self.assertEqual(frame.iloc[0]["decision"], "excluded")
        self.assertEqual(frame.iloc[0]["reason"], "missing_share_multiplier")

    def test_same_day_duplicate_group_is_excluded_as_compound(self):
        first, second = action(multiplier=5.0), action(multiplier=2.0)
        frame = classify_actions(actions_frame([first, second]),
                                 {("AAA", "EQ", date(2020, 6, 1)): 10})
        self.assertTrue((frame["decision"] == "excluded").all())
        self.assertTrue((frame["reason"] == "compound_same_day_ambiguous").all())

    def test_no_pre_ex_bars_is_excluded(self):
        frame = classify_actions(actions_frame([action()]),
                                 {("AAA", "EQ", date(2020, 6, 1)): 0})
        self.assertEqual(frame.iloc[0]["decision"], "excluded")
        self.assertEqual(frame.iloc[0]["reason"], "no_pre_ex_bars")

    def test_same_date_different_series_is_not_a_compound_group(self):
        first = action(series="EQ")
        second = action(series="BE")
        counts = {("AAA", "EQ", date(2020, 6, 1)): 5, ("AAA", "BE", date(2020, 6, 1)): 5}
        frame = classify_actions(actions_frame([first, second]), counts)
        self.assertTrue((frame["decision"] == "applied").all())


class ApplyTests(unittest.TestCase):
    def test_single_split_divides_pre_ex_only_and_ex_date_is_unchanged(self):
        bars = bars_frame([
            {"symbol": "AAA", "series": "EQ", "date": date(2020, 5, 29), "open": 100.0, "high": 100.0,
             "low": 100.0, "close": 100.0, "previous_close": 100.0},
            {"symbol": "AAA", "series": "EQ", "date": date(2020, 6, 1), "open": 50.0, "high": 50.0,
             "low": 50.0, "close": 50.0, "previous_close": 100.0},
            {"symbol": "AAA", "series": "EQ", "date": date(2020, 6, 2), "open": 55.0, "high": 55.0,
             "low": 55.0, "close": 55.0, "previous_close": 50.0},
        ])
        applied = actions_frame([action(multiplier=2.0)])
        out = apply_adjustment(bars, applied)
        out = out.sort_values("date").reset_index(drop=True)
        self.assertAlmostEqual(out.loc[0, "adj_close"], 50.0)
        self.assertAlmostEqual(out.loc[1, "adj_close"], 50.0)
        self.assertAlmostEqual(out.loc[2, "adj_close"], 55.0)
        self.assertEqual(out.loc[0, "adjustment_factor"], 2.0)
        self.assertEqual(out.loc[1, "adjustment_factor"], 1.0)
        # Raw columns must stay untouched.
        self.assertAlmostEqual(out.loc[0, "close"], 100.0)

    def test_multiple_splits_compound_cumulatively(self):
        bars = bars_frame([
            {"symbol": "AAA", "series": "EQ", "date": date(2019, 1, 2), "open": 120.0, "high": 120.0,
             "low": 120.0, "close": 120.0, "previous_close": 120.0},
            {"symbol": "AAA", "series": "EQ", "date": date(2020, 6, 1), "open": 60.0, "high": 60.0,
             "low": 60.0, "close": 60.0, "previous_close": 120.0},
            {"symbol": "AAA", "series": "EQ", "date": date(2021, 1, 4), "open": 20.0, "high": 20.0,
             "low": 20.0, "close": 20.0, "previous_close": 60.0},
        ])
        applied = actions_frame([
            action(ex_date=date(2020, 6, 1), multiplier=2.0),
            action(ex_date=date(2021, 1, 4), multiplier=3.0),
        ])
        out = apply_adjustment(bars, applied).sort_values("date").reset_index(drop=True)
        self.assertAlmostEqual(out.loc[0, "adj_close"], 20.0)  # 120 / (2*3)
        self.assertAlmostEqual(out.loc[1, "adj_close"], 20.0)  # 60 / 3
        self.assertAlmostEqual(out.loc[2, "adj_close"], 20.0)  # 20 / 1
        self.assertEqual(out.loc[0, "n_future_actions"], 2)

    def test_precision_is_preserved_without_rounding(self):
        bars = bars_frame([
            {"symbol": "AAA", "series": "EQ", "date": date(2019, 1, 2), "close": 100.0,
             "open": 100.0, "high": 100.0, "low": 100.0, "previous_close": 100.0},
        ])
        applied = actions_frame([action(multiplier=3.0)])
        out = apply_adjustment(bars, applied)
        self.assertAlmostEqual(out.iloc[0]["adj_close"], 100.0 / 3.0, places=12)
        # Must NOT be rounded to 2 decimals.
        self.assertNotEqual(out.iloc[0]["adj_close"], 33.33)

    def test_no_actions_means_adjusted_equals_raw(self):
        bars = bars_frame([
            {"symbol": "AAA", "series": "EQ", "date": date(2020, 1, 2), "close": 42.5,
             "open": 42.5, "high": 42.5, "low": 42.5, "previous_close": 42.5},
        ])
        out = apply_adjustment(bars, actions_frame([]))
        self.assertEqual(out.iloc[0]["adj_close"], 42.5)
        self.assertEqual(out.iloc[0]["adjustment_factor"], 1.0)

    def test_dividend_only_history_is_not_price_adjusted(self):
        bars = bars_frame([
            {"symbol": "AAA", "series": "EQ", "date": date(2019, 1, 2), "close": 100.0,
             "open": 100.0, "high": 100.0, "low": 100.0, "previous_close": 100.0},
        ])
        # Dividend rows are excluded upstream; empty applied set leaves prices raw.
        out = apply_adjustment(bars, actions_frame([]))
        self.assertEqual(out.iloc[0]["adj_close"], 100.0)

    def test_previous_close_uses_lagged_factor(self):
        bars = bars_frame([
            {"symbol": "AAA", "series": "EQ", "date": date(2020, 5, 29), "close": 100.0,
             "open": 100.0, "high": 100.0, "low": 100.0, "previous_close": 100.0},
            {"symbol": "AAA", "series": "EQ", "date": date(2020, 6, 1), "close": 50.0,
             "open": 50.0, "high": 50.0, "low": 50.0, "previous_close": 100.0},
        ])
        applied = actions_frame([action(multiplier=2.0)])
        out = apply_adjustment(bars, applied).sort_values("date").reset_index(drop=True)
        self.assertTrue(pd.isna(out.loc[0, "adj_previous_close"]))
        self.assertAlmostEqual(out.loc[1, "adj_previous_close"], 50.0)

    def test_bar_factors_strict_before_semantics(self):
        dates = np.array(["2020-05-31", "2020-06-01", "2020-06-02"], dtype="datetime64[ns]")
        ex = np.array(["2020-06-01"], dtype="datetime64[ns]")
        factor, count = bar_factors(dates, ex, np.array([4.0]))
        self.assertEqual(list(factor), [4.0, 1.0, 1.0])
        self.assertEqual(list(count), [1, 0, 0])

    def test_intervals_tile_history_with_suffix_factors(self):
        intervals = build_intervals(actions_frame([
            action(ex_date=date(2020, 1, 1), multiplier=2.0),
            action(ex_date=date(2021, 1, 1), multiplier=3.0),
        ]))
        self.assertEqual(len(intervals), 3)
        self.assertEqual(list(intervals["factor"]), [6.0, 3.0, 1.0])
        self.assertEqual(list(intervals["n_future"]), [2, 1, 0])

    def test_other_symbols_are_unaffected(self):
        bars = bars_frame([
            {"symbol": "AAA", "series": "EQ", "date": date(2019, 1, 2), "close": 100.0,
             "open": 100.0, "high": 100.0, "low": 100.0, "previous_close": 100.0},
            {"symbol": "BBB", "series": "EQ", "date": date(2019, 1, 2), "close": 100.0,
             "open": 100.0, "high": 100.0, "low": 100.0, "previous_close": 100.0},
        ])
        applied = actions_frame([action(multiplier=2.0)])
        out = apply_adjustment(bars, applied)
        by_symbol = out.set_index("symbol")["adj_close"]
        self.assertAlmostEqual(by_symbol["AAA"], 50.0)
        self.assertAlmostEqual(by_symbol["BBB"], 100.0)


class ManifestTests(unittest.TestCase):
    def test_run_adjustment_writes_audit_manifest_with_bounded_hashes(self):
        import json
        import tempfile

        import duckdb

        from nse_pipeline.adjust.factors import run_adjustment

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = root / "market.duckdb"
            con = duckdb.connect(str(db))
            con.execute("CREATE TABLE corporate_actions(symbol VARCHAR, series VARCHAR, ex_date DATE, record_date DATE, purpose VARCHAR, action_type VARCHAR, share_multiplier DOUBLE, dividend_per_share DOUBLE, parse_status VARCHAR, first_observed DATE, last_observed DATE, source VARCHAR)")
            con.execute("INSERT INTO corporate_actions VALUES ('AAA','EQ','2020-06-01',NULL,'SPLIT','split',2.0,NULL,'parsed_not_applied',NULL,NULL,'raw')")
            con.execute("CREATE TABLE pre2011_actions(symbol VARCHAR, series VARCHAR, ex_date DATE, purpose VARCHAR, action_tags VARCHAR[])")
            con.execute("CREATE TABLE bars(date DATE, symbol VARCHAR, series VARCHAR, isin VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, previous_close DOUBLE, volume BIGINT, traded_value DOUBLE, trades BIGINT, valid_ohlcv BOOLEAN, source VARCHAR)")
            con.execute("INSERT INTO bars VALUES ('2020-05-29','AAA','EQ',NULL,100,100,100,100,100,10,1000,1,true,'raw'),('2020-06-01','AAA','EQ',NULL,50,50,50,50,100,20,1000,1,true,'raw')")
            con.close()
            report = run_adjustment(root)
            self.assertEqual(report["applied_actions"], 1)
            manifest = json.loads((root / "processed/adjusted/audit_manifest.json").read_text())
            self.assertEqual(manifest["applied_actions"], 1)
            self.assertEqual(manifest["bars_rows_adjusted_factor_ne1"], 1)
            self.assertEqual(manifest["corporate_actions_rows"], 1)
            self.assertEqual(manifest["normalized_verified_actions_rows"], 0)
            self.assertEqual(len(manifest["files"]["adjusted_bars"]["sha256"]), 64)
            out = pd.read_parquet(root / "processed/adjusted/adjusted_bars.parquet")
            self.assertEqual(out["adjustment_factor"].tolist(), [2.0, 1.0])
            audit = pd.read_parquet(root / "processed/adjusted/action_audit.parquet")
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit.iloc[0]["decision"], "applied")

    def test_run_adjustment_mixed_db_and_verified_dates_do_not_crash(self):
        import json
        import tempfile

        import duckdb

        from nse_pipeline.adjust.factors import run_adjustment

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = root / "market.duckdb"
            con = duckdb.connect(str(db))
            con.execute("CREATE TABLE corporate_actions(symbol VARCHAR, series VARCHAR, ex_date DATE, record_date DATE, purpose VARCHAR, action_type VARCHAR, share_multiplier DOUBLE, dividend_per_share DOUBLE, parse_status VARCHAR, first_observed DATE, last_observed DATE, source VARCHAR)")
            con.execute("INSERT INTO corporate_actions VALUES ('AAA','EQ','2020-06-01',NULL,'SPLIT','split',2.0,NULL,'parsed_not_applied',NULL,NULL,'raw')")
            con.execute("CREATE TABLE pre2011_actions(symbol VARCHAR, series VARCHAR, ex_date DATE, purpose VARCHAR, action_tags VARCHAR[])")
            con.execute("CREATE TABLE bars(date DATE, symbol VARCHAR, series VARCHAR, isin VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, previous_close DOUBLE, volume BIGINT, traded_value DOUBLE, trades BIGINT, valid_ohlcv BOOLEAN, source VARCHAR)")
            con.execute("INSERT INTO bars VALUES ('2020-05-29','AAA','EQ',NULL,100,100,100,100,100,10,1000,1,true,'raw'),('2020-06-01','AAA','EQ',NULL,50,50,50,50,100,20,1000,1,true,'raw')")
            con.close()
            record = {
                "symbol": "BBB", "series": "EQ", "isin": "CUR",
                "subject": "Bonus 1:1", "exDate": "03-Jan-2006", "ex_date": "2006-01-03",
                "recDate": "05-Jan-2006", "source_file": "nse-api-2006.json", "source_row": 0,
                "source_url": "https://www.nseindia.com/api/corporates-corporateActions?index=equities",
                "action_tags": ["bonus"],
            }
            raw = {k: v for k, v in record.items()
                   if k not in {"source_file", "source_row", "source_url", "ex_date", "action_tags"}}
            for path, content in (
                (root / "processed/older_actions/nse-api-actions.jsonl", json.dumps(record) + "\n"),
                (root / "raw/older_actions/nse-api-2006.json", json.dumps([raw])),
                (root / "processed/identity/identity_edges.jsonl", ""),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            con = duckdb.connect(str(db))
            con.execute("INSERT INTO bars VALUES ('2006-01-02','BBB','EQ',NULL,100,100,100,100,99,10,1000,1,true,'raw'),('2006-01-03','BBB','EQ',NULL,50,50,50,50,100,20,1000,1,true,'raw')")
            con.close()
            report = run_adjustment(root)
            self.assertEqual(report["applied_actions"], 2)
            self.assertEqual(report["pre2011"]["applied"], 1)
            self.assertEqual(report["normalized_verified_actions_rows"], 1)
            manifest = json.loads((root / "processed/adjusted/audit_manifest.json").read_text())
            self.assertEqual(manifest["applied_actions"], 2)
            audit = pd.read_parquet(root / "processed/adjusted/action_audit.parquet")
            self.assertEqual(len(audit), 2)


if __name__ == "__main__":
    unittest.main()
