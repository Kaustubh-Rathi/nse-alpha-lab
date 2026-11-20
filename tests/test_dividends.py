from __future__ import annotations

import math
import sys
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.actions.dividends import (
    apply_total_return,
    build_events,
    compare_to_yahoo,
    compute_factors,
    cumulative_dividend_factors,
    dividend_factor,
    net_dividend_factor,
    normalize_ex_date,
    parse_bare_dividend,
    parse_explicit_amounts,
    record_date_status,
    restate_to_split_adj,
)


class DividendFactorTests(unittest.TestCase):
    def test_yahoo_worked_example(self):
        # Yahoo Help: $0.08 dividend, prior close $24.96 -> 0.9968
        factor = dividend_factor(24.96, 0.08)
        self.assertAlmostEqual(factor, 0.9968, places=4)
        self.assertAlmostEqual(factor, 1 - 0.08 / 24.96, places=12)

    def test_dividend_factor_rejects_invalid(self):
        self.assertIsNone(dividend_factor(0, 1.0))
        self.assertIsNone(dividend_factor(-5, 1.0))
        self.assertIsNone(dividend_factor(10, 0))
        self.assertIsNone(dividend_factor(10, 10))  # div >= price -> factor <= 0
        self.assertIsNone(dividend_factor(10, 11))
        self.assertIsNone(dividend_factor(float("nan"), 1.0))
        self.assertIsNone(dividend_factor(10, float("inf")))

    def test_net_factor_applies_tax_rate(self):
        gross = dividend_factor(100.0, 5.0)
        net = net_dividend_factor(100.0, 5.0, 0.10)
        self.assertAlmostEqual(gross, 0.95)
        self.assertAlmostEqual(net, (100 - 4.5) / 100)
        self.assertEqual(net_dividend_factor(100.0, 5.0, 0.0), gross)
        self.assertIsNone(net_dividend_factor(100.0, 5.0, 1.0))
        self.assertIsNone(net_dividend_factor(100.0, 5.0, -0.1))

    def test_restate_to_split_adj(self):
        # Rs 8 dividend (2011) after two 1:1 bonuses (2x each) -> Rs 2
        self.assertAlmostEqual(restate_to_split_adj(8.0, 4.0), 2.0)
        self.assertAlmostEqual(restate_to_split_adj(8.0, 1.0), 8.0)


class BuildEventsTests(unittest.TestCase):
    def _parsed(self, rows):
        return pd.DataFrame(rows)

    def test_identical_same_day_announcements_collapse(self):
        parsed = self._parsed([
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 10), "dividend_per_share": 3.0, "purpose": "AGM/DIV RS 3", "action_type": "dividend"},
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 10), "dividend_per_share": 3.0, "purpose": "DIV RS 3", "action_type": "dividend"},
        ])
        events = build_events(parsed)
        self.assertEqual(len(events), 1)
        self.assertEqual(events.iloc[0]["dividend_per_share"], 3.0)
        self.assertEqual(events.iloc[0]["duplicate_count"], 2)
        self.assertFalse(events.iloc[0]["ambiguous_same_day"])

    def test_differing_same_day_amounts_are_ambiguous_not_summed(self):
        parsed = self._parsed([
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 10), "dividend_per_share": 12.0, "purpose": "DIV RS 12", "action_type": "dividend"},
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 10), "dividend_per_share": 6.0, "purpose": "INTDIV RS 6", "action_type": "dividend"},
        ])
        events = build_events(parsed)
        self.assertEqual(len(events), 2)
        self.assertTrue(events["ambiguous_same_day"].all())
        self.assertEqual(sorted(events["dividend_per_share"].tolist()), [6.0, 12.0])

    def test_triaged_single_amount_merges_with_parsed(self):
        parsed = self._parsed([
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 10), "dividend_per_share": 3.0, "purpose": "DIV RS 3", "action_type": "dividend"},
        ])
        triaged = pd.DataFrame([
            {"symbol": "B", "series": "EQ", "ex_date": date(2021, 2, 1), "dividend_per_share_triaged": 2.45, "purpose": "DIV RS 2.45", "action_type": "dividend"},
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 10), "dividend_per_share_triaged": 3.0, "purpose": "DIV RS 3", "action_type": "dividend"},
        ])
        events = build_events(parsed, triaged)
        self.assertEqual(len(events), 2)
        merged = events[events["symbol"] == "A"].iloc[0]
        self.assertIn("parsed_not_applied", merged["sources"])
        self.assertIn("triaged_single_amount", merged["sources"])
        self.assertEqual(merged["duplicate_count"], 2)
        single = events[events["symbol"] == "B"].iloc[0]
        self.assertAlmostEqual(single["dividend_per_share"], 2.45)

    def test_invalid_amounts_dropped(self):
        parsed = self._parsed([
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 10), "dividend_per_share": 0.0, "purpose": "DIV RS 0", "action_type": "dividend"},
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 11), "dividend_per_share": float("nan"), "purpose": "DIV", "action_type": "dividend"},
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 12), "dividend_per_share": -1.0, "purpose": "DIV", "action_type": "dividend"},
        ])
        self.assertEqual(len(build_events(parsed)), 0)


class FactorAndSeriesTests(unittest.TestCase):
    def _bars(self):
        return pd.DataFrame([
            {"date": date(2020, 1, 6), "symbol": "A", "series": "EQ", "close": 100.0, "adj_close": 100.0, "adjustment_factor": 1.0},
            {"date": date(2020, 1, 7), "symbol": "A", "series": "EQ", "close": 100.0, "adj_close": 100.0, "adjustment_factor": 1.0},
            {"date": date(2020, 1, 8), "symbol": "A", "series": "EQ", "close": 95.0, "adj_close": 95.0, "adjustment_factor": 1.0},
            {"date": date(2020, 1, 9), "symbol": "A", "series": "EQ", "close": 96.0, "adj_close": 96.0, "adjustment_factor": 1.0},
        ])

    def _events(self):
        return pd.DataFrame([
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 8), "dividend_per_share": 5.0,
             "sources": "parsed_not_applied", "duplicate_count": 1, "purpose": "DIV RS 5",
             "action_type": "dividend", "ambiguous_same_day": False},
        ])

    def test_compute_factors_included_and_exclusions(self):
        bars = self._bars()
        events = self._events()
        # Ambiguous and no-pre-ex rows are excluded with reasons.
        extra = pd.DataFrame([
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 8), "dividend_per_share": 1.0,
             "sources": "parsed_not_applied", "duplicate_count": 1, "purpose": "DIV RS 1",
             "action_type": "dividend", "ambiguous_same_day": True},
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 1), "dividend_per_share": 2.0,
             "sources": "parsed_not_applied", "duplicate_count": 1, "purpose": "DIV RS 2",
             "action_type": "dividend", "ambiguous_same_day": False},
            {"symbol": "ZZ", "series": "EQ", "ex_date": date(2020, 1, 8), "dividend_per_share": 2.0,
             "sources": "parsed_not_applied", "duplicate_count": 1, "purpose": "DIV RS 2",
             "action_type": "dividend", "ambiguous_same_day": False},
        ])
        all_events = pd.concat([events, extra], ignore_index=True)
        with_factors = compute_factors(bars, all_events)
        included = with_factors[with_factors["included_in_factor"]]
        self.assertEqual(len(included), 1)
        row = included.iloc[0]
        self.assertAlmostEqual(row["prev_split_adj_close"], 100.0)
        self.assertAlmostEqual(row["dividend_factor"], 0.95)
        reasons = set(with_factors[~with_factors["included_in_factor"]]["reason"])
        self.assertIn("ambiguous_same_day_amounts", reasons)
        self.assertIn("no_pre_ex_bar", reasons)
        self.assertIn("no_bars_for_line", reasons)

    def test_total_return_removes_ex_date_drop(self):
        bars = self._bars()
        events = compute_factors(bars, self._events())
        total = apply_total_return(bars, events)
        # Pre-ex bars carry the 0.95 factor; ex-date and later do not.
        self.assertAlmostEqual(total.iloc[0]["adj_close_total"], 95.0)
        self.assertAlmostEqual(total.iloc[1]["adj_close_total"], 95.0)
        self.assertAlmostEqual(total.iloc[2]["adj_close_total"], 95.0)
        self.assertAlmostEqual(total.iloc[3]["adj_close_total"], 96.0)
        # Split-only return on ex-date is -5%; total return is 0%.
        self.assertAlmostEqual(total.iloc[2]["adj_close"] / total.iloc[1]["adj_close"] - 1, -0.05)
        self.assertAlmostEqual(total.iloc[2]["adj_close_total"] / total.iloc[1]["adj_close_total"] - 1, 0.0)

    def test_apply_total_return_no_events_equals_split_only(self):
        bars = self._bars()
        total = apply_total_return(bars, pd.DataFrame())
        pd.testing.assert_series_equal(total["adj_close_total"], total["adj_close"].astype(float), check_names=False)

    def test_cumulative_factors_strictly_before_ex_date(self):
        dates = np.array(["2020-01-06", "2020-01-07", "2020-01-08", "2020-01-09"], dtype="datetime64[ns]")
        ex = np.array(["2020-01-08"], dtype="datetime64[ns]")
        fac = np.array([0.95])
        cum = cumulative_dividend_factors(dates, ex, fac)
        np.testing.assert_allclose(cum, [0.95, 0.95, 1.0, 1.0])
        # Two dividends compound multiplicatively.
        ex2 = np.array(["2020-01-07", "2020-01-09"], dtype="datetime64[ns]")
        fac2 = np.array([0.95, 0.90])
        cum2 = cumulative_dividend_factors(dates, ex2, fac2)
        np.testing.assert_allclose(cum2, [0.95 * 0.90, 0.90, 0.90, 1.0])

    def test_future_split_restatement_in_factor(self):
        # Pre-ex split-adj close already reflects a later 2x split; the
        # Rs 10 nominal dividend is Rs 5 on that basis.
        bars = pd.DataFrame([
            {"date": date(2020, 1, 6), "symbol": "A", "series": "EQ", "close": 200.0, "adj_close": 100.0, "adjustment_factor": 2.0},
            {"date": date(2020, 1, 8), "symbol": "A", "series": "EQ", "close": 100.0, "adj_close": 50.0, "adjustment_factor": 2.0},
        ])
        events = self._events()
        events["dividend_per_share"] = 10.0
        with_factors = compute_factors(bars, events)
        row = with_factors.iloc[0]
        self.assertTrue(row["included_in_factor"])
        self.assertAlmostEqual(row["dividend_adj"], 5.0)
        self.assertAlmostEqual(row["dividend_factor"], 0.95)


class YahooCompareTests(unittest.TestCase):
    def test_identical_series_gives_perfect_correlation(self):
        dates = pd.date_range("2020-01-01", periods=10, freq="D")
        level = np.linspace(100.0, 120.0, 10) + np.array([0, 1, -1, 0, 2, -2, 1, 0, -1, 0], dtype=float)
        our = pd.DataFrame({"date": dates, "adj_close": level, "adj_close_total": level})
        yahoo = pd.DataFrame({"date": dates, "Adj Close": level})
        result = compare_to_yahoo(our, yahoo)
        self.assertEqual(result["n_common"], 10)
        self.assertAlmostEqual(result["total_return"]["daily_return_correlation"], 1.0, places=9)
        self.assertAlmostEqual(result["total_return"]["final_ratio_ours_over_yahoo"], 1.0, places=9)
        self.assertAlmostEqual(result["total_return"]["median_ratio_ours_over_yahoo"], 1.0, places=9)

    def test_compare_normalises_level_gap(self):
        # Same returns, 10x level gap (different split coverage) -> ratio 1 after normalisation.
        dates = pd.date_range("2020-01-01", periods=5, freq="D")
        ours_level = np.array([100.0, 110.0, 105.0, 115.0, 120.0])
        yahoo_level = ours_level * 10.0
        our = pd.DataFrame({"date": dates, "adj_close": ours_level, "adj_close_total": ours_level})
        yahoo = pd.DataFrame({"date": dates, "Adj Close": yahoo_level})
        result = compare_to_yahoo(our, yahoo)
        self.assertAlmostEqual(result["total_return"]["final_ratio_ours_over_yahoo"], 1.0, places=9)
        self.assertAlmostEqual(result["total_return"]["daily_return_correlation"], 1.0, places=9)

    def test_dividend_factor_isolation_tracks_yahoo(self):
        # Yahoo Close is split-adjusted; Adj/Close is the dividend factor.
        # Our total/split must track it even if levels differ.
        dates = pd.date_range("2020-01-01", periods=4, freq="D")
        split_only = np.array([100.0, 100.0, 95.0, 96.0])
        total_ours = np.array([95.0, 95.0, 95.0, 96.0])
        our = pd.DataFrame({"date": dates, "adj_close": split_only, "adj_close_total": total_ours})
        yahoo = pd.DataFrame({"date": dates, "Close": split_only, "Adj Close": total_ours})
        result = compare_to_yahoo(our, yahoo)
        iso = result["dividend_factor_isolation"]
        self.assertAlmostEqual(iso["full_window"]["ratio_ours_over_yahoo"], 1.0, places=9)
        self.assertAlmostEqual(iso["correlation_div_factor_norm"], 1.0, places=9)

    def test_total_return_closer_than_split_only_when_dividend_present(self):
        # Yahoo adj = total-return level; split-only drops on ex-date.
        dates = pd.date_range("2020-01-01", periods=4, freq="D")
        split_only = np.array([100.0, 100.0, 95.0, 96.0])
        total_ours = np.array([95.0, 95.0, 95.0, 96.0])
        our = pd.DataFrame({"date": dates, "adj_close": split_only, "adj_close_total": total_ours})
        yahoo = pd.DataFrame({"date": dates, "Adj Close": total_ours})
        result = compare_to_yahoo(our, yahoo)
        self.assertAlmostEqual(result["total_return"]["final_ratio_ours_over_yahoo"], 1.0, places=9)
        self.assertNotAlmostEqual(result["split_only"]["final_ratio_ours_over_yahoo"], 1.0, places=3)


class ExplicitAmountParserTests(unittest.TestCase):
    def test_currency_dash_separator_and_leading_dot(self):
        self.assertEqual([a["amount"] for a in parse_explicit_amounts("DIV RS - 8.50 PER SHARE")], [8.5])
        self.assertEqual([a["amount"] for a in parse_explicit_amounts("DIVIDEND-RE .75 PER SHAre")], [0.75])
        self.assertEqual([a["amount"] for a in parse_explicit_amounts("INT DIV RS -18 PER SHARE")], [18.0])
        self.assertEqual([a["amount"] for a in parse_explicit_amounts("DIV - RSE 1 PER SH")], [1.0])

    def test_currencyless_per_share_amounts(self):
        self.assertEqual([a["amount"] for a in parse_explicit_amounts("INT DIV 3 PER SHARE")], [3.0])
        self.assertEqual([a["amount"] for a in parse_explicit_amounts("INT DIV - 2.50 PER SH")], [2.5])
        self.assertEqual([a["amount"] for a in parse_explicit_amounts("DIV- 2.20 PER SHARE")], [2.2])

    def test_percentage_fractional_and_ordinal_never_amounts(self):
        self.assertEqual(parse_explicit_amounts("DIVIDEND- 6% PER ANNUM"), [])
        self.assertEqual(parse_explicit_amounts("DIVIDEND @ 7.5% PER SHARE"), [])
        self.assertEqual(parse_explicit_amounts("DIV - RE PER 0.125 SH"), [])
        self.assertEqual(parse_explicit_amounts("2ND INTERIM DIVIDEND"), [])
        self.assertEqual(parse_explicit_amounts("INTERIM DIVIDEND"), [])

    def test_bare_multi_leg_summed_only_when_separable(self):
        dec = parse_bare_dividend("DIV-FIN 0.20+SPL 0.15")
        self.assertEqual(dec["status"], "recovered_multi_sum")
        self.assertAlmostEqual(dec["amount"], 0.35)
        dec2 = parse_bare_dividend("II INT DIV 12+I INT DIV10")
        self.assertEqual(dec2["status"], "excluded_ambiguous_multi")
        self.assertIsNone(dec2["amount"])

    def test_bare_single_and_strict_exclusions(self):
        self.assertEqual(parse_bare_dividend("INT DIV 3 PER SHARE")["status"], "recovered_single")
        self.assertEqual(parse_bare_dividend("INTERIM DIVIDEND")["status"], "excluded_no_amount")
        self.assertEqual(parse_bare_dividend("DIVIDEND - 10%")["status"], "excluded_percentage_rate")
        self.assertEqual(parse_bare_dividend("DIV - RE PER 0.125 SH")["status"], "excluded_fractional_share")


class DateSemanticsTests(unittest.TestCase):
    def test_timestamp_floored_not_rounded(self):
        self.assertEqual(normalize_ex_date("2020-01-10 23:59:00"), date(2020, 1, 10))
        self.assertEqual(normalize_ex_date("2020-01-10T05:00:00"), date(2020, 1, 10))
        self.assertIsNone(normalize_ex_date(None))
        self.assertIsNone(normalize_ex_date(""))

    def test_record_date_status(self):
        self.assertEqual(record_date_status(date(2020, 1, 11), date(2020, 1, 10)), "after_ex normal")
        self.assertEqual(record_date_status(date(2020, 1, 10), date(2020, 1, 10)), "same_day")
        self.assertEqual(record_date_status(date(2020, 1, 9), date(2020, 1, 10)), "record_before_ex anomaly")
        self.assertEqual(record_date_status(None, date(2020, 1, 10)), "missing")

    def test_build_events_carries_record_date_fields(self):
        parsed = pd.DataFrame([
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 10),
             "record_date": date(2020, 1, 11), "dividend_per_share": 3.0,
             "purpose": "DIV RS 3", "action_type": "dividend"},
        ])
        events = build_events(parsed)
        self.assertEqual(events.iloc[0]["record_date"], "2020-01-11")
        self.assertEqual(events.iloc[0]["ex_record_delta_days"], 1)
        self.assertEqual(events.iloc[0]["record_date_status"], "after_ex normal")


class BareRegistryTests(unittest.TestCase):
    def test_overlapping_bare_aggregates_not_new_event(self):
        parsed = pd.DataFrame([
            {"symbol": "SAIL", "series": "EQ", "ex_date": date(2011, 1, 18),
             "record_date": date(2011, 1, 19), "dividend_per_share": 1.2,
             "purpose": "INT DIV-RS.1.20 PER SHARE", "action_type": "dividend"},
        ])
        bare = pd.DataFrame([
            {"symbol": "SAIL", "series": "EQ", "ex_date": date(2011, 1, 18),
             "record_date": date(2011, 1, 19), "purpose": "INTERIM DIVIDEND",
             "action_type": "dividend"},
        ])
        events = build_events(parsed, None, bare)
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events.iloc[0]["dividend_per_share"], 1.2)
        self.assertIn("parsed_not_applied", events.iloc[0]["sources"])
        self.assertIn("INTERIM DIVIDEND", events.iloc[0]["purpose"])

    def test_non_overlapping_bare_recovered(self):
        parsed = pd.DataFrame([
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 10),
             "dividend_per_share": 3.0, "purpose": "DIV RS 3", "action_type": "dividend"},
        ])
        bare = pd.DataFrame([
            {"symbol": "B", "series": "EQ", "ex_date": date(2013, 1, 23),
             "record_date": date(2013, 1, 24), "purpose": "INT DIV 3 PER SHARE",
             "action_type": "dividend"},
            {"symbol": "C", "series": "EQ", "ex_date": date(2011, 1, 24),
             "record_date": date(2011, 1, 25), "purpose": "2ND INTERIM DIVIDEND",
             "action_type": "dividend"},
            {"symbol": "D", "series": "EQ", "ex_date": date(2011, 2, 11),
             "record_date": date(2011, 2, 12), "purpose": "DIV-FIN 0.20+SPL 0.15",
             "action_type": "dividend"},
        ])
        events = build_events(parsed, None, bare)
        symbols = set(events["symbol"].tolist())
        self.assertEqual(symbols, {"A", "B", "D"})  # C has no amount -> excluded
        self.assertAlmostEqual(float(events[events["symbol"] == "B"].iloc[0]["dividend_per_share"]), 3.0)
        self.assertIn("bare_recovered_single", events[events["symbol"] == "B"].iloc[0]["sources"])
        self.assertAlmostEqual(float(events[events["symbol"] == "D"].iloc[0]["dividend_per_share"]), 0.35)
        self.assertIn("bare_recovered_multi_sum", events[events["symbol"] == "D"].iloc[0]["sources"])


if __name__ == "__main__":
    unittest.main()
