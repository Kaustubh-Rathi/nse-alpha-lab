"""Synthetic tests for src/universe.py (no database required)."""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.universe.builder import (
    EligibilityConfig,
    coverage_from_observations,
    evaluate_symbol,
    is_valid_ohlcv,
    load_snapshot_symbols,
    members_on_exact,
    monthly_universe_from_bars,
    overlap_report,
    validate_config,
    write_jsonl,
)

TEMP_ROOT = Path.home() / "AppData" / "Local" / "Temp" / "opencode"
if not TEMP_ROOT.exists():
    TEMP_ROOT = None


def eq_bar(d: date, symbol="AAA", close=100.0, volume=20000, series="EQ", valid=True):
    return {"date": d, "symbol": symbol, "series": series,
            "open": close, "high": close + 1, "low": close - 1,
            "close": close, "volume": volume, "valid_ohlcv": valid}


def trap_history(symbol="AAA", start=date(2020, 1, 1), n=70, **kw):
    """n consecutive EQ bars ending strictly before a later month-end."""
    return [eq_bar(start + timedelta(days=i), symbol=symbol, **kw) for i in range(n)]


class EligibilityTests(unittest.TestCase):
    def test_eligible_when_all_filters_pass(self):
        cfg = EligibilityConfig(min_price=5.0, min_volume=5000, min_days_observed=3)
        result = evaluate_symbol(eq_bar(date(2020, 1, 31)), 10, cfg)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reasons"], [])

    def test_fails_below_min_price(self):
        result = evaluate_symbol(eq_bar(date(2020, 1, 31), close=3.0), 10,
                                 EligibilityConfig(min_price=5.0, min_days_observed=3))
        self.assertFalse(result["eligible"])
        self.assertTrue(any("min_price" in r for r in result["reasons"]))

    def test_fails_below_min_volume(self):
        result = evaluate_symbol(eq_bar(date(2020, 1, 31), volume=4999), 10,
                                 EligibilityConfig(min_volume=5000, min_days_observed=3))
        self.assertFalse(result["eligible"])
        self.assertTrue(any("min_volume" in r for r in result["reasons"]))

    def test_fails_on_short_history(self):
        result = evaluate_symbol(eq_bar(date(2020, 1, 31)), 5,
                                 EligibilityConfig(min_days_observed=63))
        self.assertFalse(result["eligible"])
        self.assertTrue(any("days_observed" in r for r in result["reasons"]))

    def test_invalid_ohlcv_flag_and_numbers(self):
        self.assertFalse(evaluate_symbol(eq_bar(date(2020, 1, 31), valid=False),
                                         100, EligibilityConfig(min_days_observed=3))["eligible"])
        bad = eq_bar(date(2020, 1, 31))
        bad["high"], bad["low"] = 50.0, 90.0  # high below low
        result = evaluate_symbol(bad, 100, EligibilityConfig(min_days_observed=3))
        self.assertFalse(result["eligible"])
        self.assertTrue(any("invalid_ohlcv" in r for r in result["reasons"]))
        self.assertFalse(is_valid_ohlcv(10, 9, 12, 10, 100))
        self.assertTrue(is_valid_ohlcv(10, 12, 9, 11, 0))

    def test_non_eq_series_and_missing_bar(self):
        cfg = EligibilityConfig(min_days_observed=3)
        self.assertFalse(evaluate_symbol(eq_bar(date(2020, 1, 31), series="BE"), 100, cfg)["eligible"])
        self.assertFalse(evaluate_symbol(None, 100, cfg)["eligible"])

    def test_config_fail_closed(self):
        for cfg in (EligibilityConfig(min_price=0), EligibilityConfig(min_volume=0),
                    EligibilityConfig(min_days_observed=0), EligibilityConfig(series="BE")):
            with self.assertRaises(ValueError):
                validate_config(cfg)

    def test_infy_symbol_not_excluded_by_prefix(self):
        # Series filter is EQ; symbol prefixes such as INFY must not be treated as INF series.
        cfg = EligibilityConfig(min_days_observed=3)
        bar = eq_bar(date(2020, 1, 31), symbol="INFY")
        self.assertTrue(evaluate_symbol(bar, 100, cfg)["eligible"])


class PointInTimeTests(unittest.TestCase):
    def test_future_symbols_excluded(self):
        # AAA traded all year, BBB only starts after the January month-end.
        t1, t2 = date(2020, 1, 31), date(2020, 2, 29)
        bars = trap_history("AAA", date(2020, 1, 1), 65) + trap_history("BBB", date(2020, 2, 1), 65)
        # ensure both have a bar on their month-ends
        bars.append(eq_bar(t1, "AAA"))
        bars.append(eq_bar(t2, "AAA"))
        bars.append(eq_bar(t2, "BBB"))
        obs = monthly_universe_from_bars(bars, [t1, t2], EligibilityConfig(min_days_observed=10))
        jan = {o["symbol"] for o in obs if o["month_end"] == "2020-01-31"}
        feb = {o["symbol"] for o in obs if o["month_end"] == "2020-02-29"}
        self.assertIn("AAA", jan)
        self.assertNotIn("BBB", jan)  # survivorship: BBB not yet observed
        self.assertIn("BBB", feb)

    def test_delisted_symbol_drops_out(self):
        t1, t2 = date(2020, 1, 31), date(2020, 2, 29)
        bars = trap_history("AAA", date(2020, 1, 1), 65) + [eq_bar(t1, "AAA"), eq_bar(t2, "AAA")]
        # BBB present in Jan, absent in Feb (stopped trading).
        bars += trap_history("BBB", date(2020, 1, 1), 31) + [eq_bar(t1, "BBB")]
        obs = monthly_universe_from_bars(bars, [t1, t2], EligibilityConfig(min_days_observed=10))
        self.assertIn("BBB", {o["symbol"] for o in obs if o["month_end"] == "2020-01-31"})
        self.assertNotIn("BBB", {o["symbol"] for o in obs if o["month_end"] == "2020-02-29"})

    def test_turnover_entries_exits_retention(self):
        obs = [{"month_end": "2020-01-31", "symbol": s} for s in ("A", "B", "C")]
        obs += [{"month_end": "2020-02-29", "symbol": s} for s in ("B", "C", "D")]
        coverage = coverage_from_observations(obs)
        feb = next(r for r in coverage["monthly"] if r["month"] == "2020-02")
        self.assertEqual((feb["symbols"], feb["entries"], feb["exits"], feb["retained"]), (3, 1, 1, 2))
        self.assertAlmostEqual(feb["retention_rate_vs_prior"], 2 / 3, places=4)

    def test_jsonl_sample_roundtrip(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            path = Path(tmp) / "sample.jsonl"
            write_jsonl(path, [{"symbol": "AAA"}, {"symbol": "BBB"}])
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["symbol"] for r in rows], ["AAA", "BBB"])


class NiftyExactDateTests(unittest.TestCase):
    def test_exact_date_only_no_fill(self):
        snapshots = {("NIFTY50", "2020-10-30"): {"AAA", "BBB"}}
        self.assertEqual(members_on_exact(snapshots, "NIFTY50", "2020-10-30"), {"AAA", "BBB"})
        for other in ("2020-10-29", "2020-10-31", "2020-01-31"):
            with self.assertRaises(LookupError):
                members_on_exact(snapshots, "NIFTY50", other)
        with self.assertRaises(LookupError):
            members_on_exact(snapshots, "NIFTY500", "2020-10-30")

    def test_overlap_counts(self):
        report = overlap_report({"AAA", "BBB", "CCC"}, {"BBB", "CCC", "DDD"})
        self.assertEqual(report["overlap"], 2)
        self.assertAlmostEqual(report["overlap_pct_of_snapshot"], 2 / 3, places=4)
        self.assertEqual(report["snapshot_not_eligible"], 1)
        self.assertEqual(report["eligible_not_in_snapshot"], 1)
        unknown = overlap_report(None, {"AAA"})
        self.assertIsNone(unknown["overlap"])
        self.assertEqual(unknown["reason"], "snapshot_date_not_an_observed_session")

    def test_snapshot_loader_and_duplicate_guard(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            root = Path(tmp)
            for row in ({"index_code": "NIFTY50", "as_of": "2020-01-31",
                         "snapshot_csv": "data/processed/index_membership/NIFTY50_2020-01-31.csv"},
                        {"index_code": "NIFTY50", "as_of": "2020-02-29",
                         "snapshot_csv": "data/processed/index_membership/NIFTY50_2020-02-29.csv"}):
                path = root / Path(row["snapshot_csv"]).name
                with path.open("w", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=["symbol", "company_name", "isin"])
                    writer.writeheader()
                    writer.writerow({"symbol": "AAA", "company_name": "A", "isin": ""})
            (root / "catalog.json").write_text(json.dumps(
                [{"index_code": "NIFTY50", "as_of": "2020-01-31", "snapshot_csv": "NIFTY50_2020-01-31.csv"},
                 {"index_code": "NIFTY50", "as_of": "2020-02-29", "snapshot_csv": "NIFTY50_2020-02-29.csv"}]))
            mapping, inventory = load_snapshot_symbols(root)
            self.assertEqual(members_on_exact(mapping, "NIFTY50", "2020-01-31"), {"AAA"})
            self.assertEqual(len(inventory), 2)
            # duplicate same-date inventory must fail closed
            (root / "catalog.json").write_text(json.dumps(
                [{"index_code": "NIFTY50", "as_of": "2020-01-31", "snapshot_csv": "NIFTY50_2020-01-31.csv"},
                 {"index_code": "NIFTY50", "as_of": "2020-01-31", "snapshot_csv": "NIFTY50_2020-02-29.csv"}]))
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                load_snapshot_symbols(root)


    def test_zero_eligible_months_fill(self):
        # January had no eligible symbols (history too short): it must
        # still appear with 0 symbols so 2000-2026 coverage is complete.
        obs = [{"month_end": "2020-02-29", "symbol": "AAA"}]
        coverage = coverage_from_observations(
            obs, all_month_ends=["2020-01-31", "2020-02-29"])
        jan = next(r for r in coverage["monthly"] if r["month"] == "2020-01")
        feb = next(r for r in coverage["monthly"] if r["month"] == "2020-02")
        self.assertEqual(jan["symbols"], 0)
        self.assertEqual(jan["month_end"], "2020-01-31")
        self.assertEqual(feb["symbols"], 1)
        self.assertEqual(feb["entries"], 1)
        self.assertEqual(coverage["totals"]["months"], 2)

    def test_zero_month_turnover(self):
        # Entering and leaving a zero month reports exits/retention honestly.
        obs = [{"month_end": "2020-01-31", "symbol": "AAA"},
               {"month_end": "2020-03-31", "symbol": "BBB"}]
        coverage = coverage_from_observations(
            obs, all_month_ends=["2020-01-31", "2020-02-29", "2020-03-31"])
        feb = next(r for r in coverage["monthly"] if r["month"] == "2020-02")
        mar = next(r for r in coverage["monthly"] if r["month"] == "2020-03")
        self.assertEqual((feb["symbols"], feb["exits"], feb["retained"]), (0, 1, 0))
        self.assertEqual((mar["symbols"], mar["entries"], mar["exits"]), (1, 1, 0))
        self.assertIsNone(mar["retention_rate_vs_prior"])  # prior month empty


if __name__ == "__main__":
    unittest.main()
