from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.actions.specials import (
    SPECIAL_THRESHOLD,
    attach_prices_and_classify,
    build_bridge,
    build_specials_events,
    classify_special,
    parse_components,
)


class ParseComponentsTests(unittest.TestCase):
    def test_fin_plus_spl(self):
        comp = parse_components("DIV- FIN RS.19+SPL RS.25")
        self.assertEqual(comp["parse_status"], "parsed")
        self.assertAlmostEqual(comp["fin_per_share"], 19.0)
        self.assertAlmostEqual(comp["spl_per_share"], 25.0)
        self.assertIsNone(comp["int_per_share"])
        self.assertAlmostEqual(comp["total_cash_per_share"], 44.0)

    def test_int_plus_spl_synonyms_tcs_2014_and_2023(self):
        comp = parse_components("DIV: INT RS 5+SPCL RS 40")
        self.assertEqual(comp["parse_status"], "parsed")
        self.assertAlmostEqual(comp["int_per_share"], 5.0)
        self.assertAlmostEqual(comp["spl_per_share"], 40.0)
        self.assertAlmostEqual(comp["total_cash_per_share"], 45.0)

        comp = parse_components("INTDIV- RS 8 SPLDV RS 67")
        self.assertAlmostEqual(comp["int_per_share"], 8.0)
        self.assertAlmostEqual(comp["spl_per_share"], 67.0)
        self.assertAlmostEqual(comp["total_cash_per_share"], 75.0)

        comp = parse_components("AGM/DIV FIN-RS.8 SPL-RS.8")
        self.assertAlmostEqual(comp["fin_per_share"], 8.0)
        self.assertAlmostEqual(comp["spl_per_share"], 8.0)

    def test_trailing_spl_label_and_slash_spldiv(self):
        comp = parse_components("DIV-RE 0.80 + RE 0.50 SPL")
        self.assertEqual(comp["parse_status"], "parsed")
        self.assertAlmostEqual(comp["ordinary_per_share"], 0.80)
        self.assertAlmostEqual(comp["spl_per_share"], 0.50)

        comp = parse_components("AGM/DIV-RS 40/SPLDIV-RS 90")
        self.assertAlmostEqual(comp["ordinary_per_share"], 40.0)
        self.assertAlmostEqual(comp["spl_per_share"], 90.0)

    def test_redemption_leg_is_not_dividend_cash(self):
        comp = parse_components("PART REDM RS2/DIV-RE0.11")
        self.assertEqual(comp["parse_status"], "parsed")
        self.assertAlmostEqual(comp["ordinary_per_share"], 0.11)
        self.assertAlmostEqual(comp["total_cash_per_share"], 0.11)
        self.assertEqual(comp["non_dividend_legs"], [2.0])

    def test_face_value_and_split_ratio_legs_not_cash(self):
        comp = parse_components("DIV-RS6/SPLIT RS 10TORE 1")
        self.assertEqual(comp["parse_status"], "parsed")
        self.assertAlmostEqual(comp["ordinary_per_share"], 6.0)
        self.assertAlmostEqual(comp["total_cash_per_share"], 6.0)
        self.assertIn(10.0, comp["non_dividend_legs"])

    def test_combined_div_spdv_labels_are_ambiguous(self):
        comp = parse_components("DIV/SPDV RS 80 & RS 50")
        self.assertEqual(comp["parse_status"], "ambiguous_excluded")
        self.assertEqual(comp["ambiguous_reason"], "combined_label_amounts_not_separable")
        self.assertIsNone(comp["total_cash_per_share"])

    def test_unlabelled_amount_is_ambiguous(self):
        comp = parse_components("DIV-RS 5 + RS 3")
        self.assertEqual(comp["parse_status"], "ambiguous_excluded")
        self.assertIsNone(comp["total_cash_per_share"])

    def test_no_amount_is_no_components(self):
        comp = parse_components("INTERIM DIVIDEND")
        self.assertEqual(comp["parse_status"], "no_components")
        self.assertIsNone(comp["total_cash_per_share"])

    def test_duplicate_component_differing_amounts_ambiguous(self):
        comp = parse_components("DIV-FIN RS.5+SPL RS.5+SPL RS.6")
        self.assertEqual(comp["parse_status"], "ambiguous_excluded")
        self.assertIn("duplicate_spl", comp["ambiguous_reason"])

    def test_currency_dash_and_leading_dot_forms(self):
        comp = parse_components("DIV RS - 8.50 PER SHARE")
        self.assertEqual(comp["parse_status"], "parsed")
        self.assertAlmostEqual(comp["ordinary_per_share"], 8.5)
        comp = parse_components("DIVIDEND-RE .75 PER SHAre")
        self.assertAlmostEqual(comp["ordinary_per_share"], 0.75)

    def test_currencyless_labelled_multi_legs(self):
        comp = parse_components("DIV-FIN 0.20+SPL 0.15")
        self.assertEqual(comp["parse_status"], "parsed")
        self.assertAlmostEqual(comp["fin_per_share"], 0.20)
        self.assertAlmostEqual(comp["spl_per_share"], 0.15)
        self.assertAlmostEqual(comp["total_cash_per_share"], 0.35)

        comp = parse_components("INT DIV 3+SPL DIV 1.25")
        self.assertAlmostEqual(comp["int_per_share"], 3.0)
        self.assertAlmostEqual(comp["spl_per_share"], 1.25)

    def test_bare_duplicate_component_differing_ambiguous(self):
        comp = parse_components("II INT DIV 12+I INT DIV10")
        self.assertEqual(comp["parse_status"], "ambiguous_excluded")

    def test_percentage_rate_is_not_cash(self):
        comp = parse_components("DIVIDEND- 6% PER ANNUM")
        self.assertEqual(comp["parse_status"], "no_components")
        self.assertIsNone(comp["total_cash_per_share"])


class ClassifySpecialTests(unittest.TestCase):
    def test_below_2pct_is_ordinary(self):
        # TCS 2023: Rs 67 on Rs 3374.55 -> 1.99% (below threshold).
        cls = classify_special(67.0, 3374.55, 1.0)
        self.assertFalse(cls["is_special_2pct"])
        self.assertEqual(cls["nse_treatment"], "special_labelled_below_2pct")
        self.assertLess(cls["spl_yield"], SPECIAL_THRESHOLD)
        self.assertIsNone(cls["pr_divisor_factor"])

    def test_at_or_above_2pct_excluded_from_tr(self):
        cls = classify_special(10.0, 100.0, 1.0)  # exactly 10%
        self.assertTrue(cls["is_special_2pct"])
        self.assertEqual(cls["nse_treatment"], "special_ge_2pct_excluded_from_TR")
        self.assertAlmostEqual(cls["pr_divisor_factor"], 0.90)

    def test_boundary_2pct_is_special(self):
        cls = classify_special(2.0, 100.0, 1.0)
        self.assertTrue(cls["is_special_2pct"])
        self.assertAlmostEqual(cls["spl_yield"], 0.02)

    def test_split_restatement_keeps_yield_invariant(self):
        # Pre-ex price already split-adjusted; Rs 40 nominal / 2x split
        # on Rs 500 adjusted (= Rs 1000 raw) is still 4%.
        cls = classify_special(40.0, 500.0, 2.0)
        self.assertAlmostEqual(cls["spl_adj"], 20.0)
        self.assertAlmostEqual(cls["spl_yield"], 0.04)

    def test_no_bars_is_untestable(self):
        cls = classify_special(50.0, None, 1.0)
        self.assertEqual(cls["nse_treatment"], "untestable_no_bars")
        self.assertIsNone(cls["is_special_2pct"])

    def test_no_spl_leg(self):
        cls = classify_special(None, 100.0, 1.0)
        self.assertEqual(cls["nse_treatment"], "no_special_leg")


class BuildAndClassifyTests(unittest.TestCase):
    def _bars(self, price=100.0):
        return pd.DataFrame([
            {"date": date(2020, 1, 6), "symbol": "A", "series": "EQ", "close": price,
             "adj_close": price, "adjustment_factor": 1.0},
            {"date": date(2020, 1, 7), "symbol": "A", "series": "EQ", "close": price,
             "adj_close": price, "adjustment_factor": 1.0},
            {"date": date(2020, 1, 8), "symbol": "A", "series": "EQ", "close": price * 0.95,
             "adj_close": price * 0.95, "adjustment_factor": 1.0},
        ])

    def test_int_leg_becomes_nse_tr_dividend(self):
        multi = pd.DataFrame([
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 8),
             "purpose": "INTDIV- RS 8 SPLDV RS 5", "dividend_amount_count": 2},
        ])
        events = build_specials_events(multi)
        self.assertEqual(len(events), 1)
        out = attach_prices_and_classify(events, self._bars())
        row = out.iloc[0]
        # SPL Rs 5 / 100 = 5% >= 2% -> excluded; INT Rs 8 is the TR leg.
        self.assertTrue(row["is_special_2pct"])
        self.assertAlmostEqual(row["nse_tr_per_share"], 8.0)
        self.assertAlmostEqual(row["yahoo_tr_per_share"], 13.0)
        self.assertAlmostEqual(row["special_excluded_from_TR_per_share"], 5.0)
        self.assertAlmostEqual(row["pr_divisor_factor"], 0.95)
        self.assertTrue(row["included_in_nse_TR"])

    def test_small_special_included_in_nse_tr(self):
        multi = pd.DataFrame([
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 8),
             "purpose": "DIV: INT RS 5+SPCL RS 1", "dividend_amount_count": 2},
        ])
        out = attach_prices_and_classify(build_specials_events(multi), self._bars())
        row = out.iloc[0]
        # SPL Rs 1 / 100 = 1% < 2% -> ordinary treatment: 5 + 1 = 6.
        self.assertFalse(row["is_special_2pct"])
        self.assertEqual(row["nse_treatment"], "special_labelled_below_2pct")
        self.assertAlmostEqual(row["nse_tr_per_share"], 6.0)
        self.assertAlmostEqual(row["special_excluded_from_TR_per_share"], 0.0)

    def test_ambiguous_row_excluded_from_both_bridges(self):
        multi = pd.DataFrame([
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 8),
             "purpose": "DIV/SPDV RS 80 & RS 50", "dividend_amount_count": 2},
        ])
        out = attach_prices_and_classify(build_specials_events(multi), self._bars())
        row = out.iloc[0]
        self.assertFalse(row["included_in_nse_TR"])
        self.assertFalse(row["included_in_yahoo_bridge"])
        self.assertEqual(row["reason"], "combined_label_amounts_not_separable")

    def test_no_bars_excluded_with_disclosure(self):
        multi = pd.DataFrame([
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 8),
             "purpose": "DIV-FIN RS.5+SPL RS.5", "dividend_amount_count": 2},
        ])
        out = attach_prices_and_classify(build_specials_events(multi), self._bars())
        # Same bars present -> included; drop bars -> excluded.
        self.assertTrue(bool(out.iloc[0]["included_in_nse_TR"]))
        empty_bars = self._bars().iloc[0:0].copy()
        out2 = attach_prices_and_classify(build_specials_events(multi), empty_bars)
        self.assertFalse(bool(out2.iloc[0]["included_in_nse_TR"]))
        self.assertEqual(out2.iloc[0]["reason"], "no_bars_for_line")

    def test_bridge_overlap_classification(self):
        multi = pd.DataFrame([
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 8),
             "purpose": "DIV-FIN RS.5+SPL RS.5", "dividend_amount_count": 2},
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 9),
             "purpose": "DIV-FIN RS.5+SPL RS.5", "dividend_amount_count": 2},
        ])
        events = attach_prices_and_classify(build_specials_events(multi), self._bars())
        existing = pd.DataFrame([
            {"symbol": "A", "series": "EQ", "ex_date": "2020-01-08", "dividend_per_share": 5.0},
            {"symbol": "A", "series": "EQ", "ex_date": "2020-01-09", "dividend_per_share": 9.0},
        ])
        bridge = build_bridge(events, existing)
        by_date = bridge.set_index("ex_date")["overlap_type"].to_dict()
        # NSE TR legs here: FIN 5 + SPL 5 (5% >= 2% -> excluded) = 5.
        self.assertEqual(by_date["2020-01-08"], "identical_amount")
        self.assertEqual(by_date["2020-01-09"], "differing_amount")

    def test_identical_duplicate_rows_collapse(self):
        multi = pd.DataFrame([
            {"symbol": "A", "series": "BE", "ex_date": date(2020, 1, 8),
             "purpose": "DIV-FIN RS.5+SPL RS.5", "dividend_amount_count": 2},
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 8),
             "purpose": "DIV-FIN RS.5+SPL RS.5", "dividend_amount_count": 2},
        ])
        # BE and EQ are separate lines -> two events; no cross-series merge.
        self.assertEqual(len(build_specials_events(multi)), 2)


if __name__ == "__main__":
    unittest.main()
