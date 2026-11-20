from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.actions.triage import classify_actions, parse_dividend, parse_rights, summarize, triage_row


class TriageBucketTests(unittest.TestCase):
    def test_interest_payment_is_debt_interest(self):
        row = triage_row("INTEREST PAYMENT", "other")
        self.assertEqual(row["bucket"], "debt_interest")
        self.assertFalse(row["price_relevant"])

    def test_interest_redemption_is_debt_interest(self):
        row = triage_row("INT PAYMENT/REDEMPTION", "other")
        self.assertEqual(row["bucket"], "debt_interest")

    def test_annual_general_meeting_is_agm_other(self):
        row = triage_row("Annual General Meeting", "other")
        self.assertEqual(row["bucket"], "agm_other")
        self.assertFalse(row["price_relevant"])

    def test_single_amount_dividend_is_dividend_cash(self):
        row = triage_row("DIV - RS 5 PER SH", "dividend")
        self.assertEqual(row["bucket"], "dividend_cash")
        self.assertTrue(row["price_relevant"])
        self.assertEqual(row["dividend_per_share_triaged"], 5.0)
        # Two FIN+SPL components: parseable but flagged as multi-amount.
        multi = triage_row("AGM/DIV-FIN RS.19+SPL RS.25", "dividend")
        self.assertEqual(multi["bucket"], "dividend_cash")
        self.assertEqual(multi["dividend_amount_count"], 2)
        self.assertAlmostEqual(multi["dividend_total_if_summed"], 44.0)

    def test_single_amount_dividend_value(self):
        parsed = parse_dividend("DIV - RS 2 PER SH")
        self.assertTrue(parsed["parseable"])
        self.assertEqual(parsed["dividend_per_share"], 2.0)

    def test_bare_dividend_without_amount_is_unparseable(self):
        row = triage_row("INTERIM DIVIDEND", "dividend")
        self.assertEqual(row["bucket"], "unparseable")
        self.assertFalse(row["price_relevant"])
        self.assertEqual(row["reason"], "dividend_without_per_share_amount")

    def test_pct_only_dividend_is_not_cash_parseable(self):
        parsed = parse_dividend("DIVIDEND - 10% PER ANNUM")
        self.assertFalse(parsed["parseable"])
        self.assertTrue(parsed["pct_only"])

    def test_rights_bucket_and_ratio_parse(self):
        row = triage_row("RIGHTS 2:5 @ PREM RS 75", "rights")
        self.assertEqual(row["bucket"], "rights")
        self.assertTrue(row["price_relevant"])
        self.assertEqual(row["rights_ratio"], "2:5")
        rights = parse_rights("RGHTS 1:1 @PRM RS 25/-")
        self.assertTrue(rights["parseable"])

    def test_buyback_bucket(self):
        row = triage_row("BUY BACK", "other")
        self.assertEqual(row["bucket"], "buyback")
        self.assertTrue(row["price_relevant"])

    def test_scheme_reorg_bucket(self):
        row = triage_row("SCHEME OF ARRANGEMENT", "reorganization")
        self.assertEqual(row["bucket"], "scheme_reorg")
        self.assertTrue(row["price_relevant"])

    def test_capital_reduction_is_scheme_reorg(self):
        row = triage_row("CAPITAL REDUCTION", "reorganization")
        self.assertEqual(row["bucket"], "scheme_reorg")

    def test_split_needs_review_is_unparseable_but_price_relevant(self):
        row = triage_row("FV SPLIT RS.10 TO RS.2", "split")
        self.assertEqual(row["bucket"], "unparseable")
        self.assertTrue(row["price_relevant"])

    def test_scheme_outranks_debt(self):
        row = triage_row("SCH AGMT-BONUS NCRPS4:1", "bonus")
        self.assertEqual(row["bucket"], "scheme_reorg")

    def test_classify_counts_and_summarize_reconcile(self):
        frame = pd.DataFrame([
            {"symbol": "A", "series": "EQ", "ex_date": date(2020, 1, 5), "purpose": "INTEREST PAYMENT", "action_type": "other"},
            {"symbol": "B", "series": "EQ", "ex_date": date(2021, 1, 5), "purpose": "Annual General Meeting", "action_type": "other"},
            {"symbol": "C", "series": "EQ", "ex_date": date(2022, 1, 5), "purpose": "DIV - RS 5 PER SH", "action_type": "dividend"},
            {"symbol": "D", "series": "EQ", "ex_date": date(2023, 1, 5), "purpose": "RIGHTS 1:1 @ PRM RS 25", "action_type": "rights"},
            {"symbol": "E", "series": "MF", "ex_date": date(2024, 1, 5), "purpose": "BUYBACK", "action_type": "other"},
            {"symbol": "F", "series": "EQ", "ex_date": date(2025, 1, 5), "purpose": "MERGER", "action_type": "reorganization"},
            {"symbol": "G", "series": "EQ", "ex_date": date(2026, 1, 5), "purpose": "INTERIM DIVIDEND", "action_type": "dividend"},
        ])
        triaged = classify_actions(frame)
        self.assertEqual(len(triaged), 7)
        summary = summarize(triaged, pd.DataFrame({"dividend_per_share": [2.0, 3.0]}))
        self.assertEqual(sum(summary["bucket_counts"].values()), 7)
        self.assertEqual(summary["equity_subset"]["rows"], 6)
        # Equity price-relevant: C (dividend_cash) + D (rights) + F (scheme) = 3.
        self.assertEqual(summary["equity_subset"]["price_relevant_rows"], 3)
        self.assertEqual(summary["dividend_cash"]["rows"], 1)
        self.assertEqual(summary["dividend_cash"]["single_amount_sum_rs_per_share"], 5.0)
        # Bare G stays out of dividend_cash and out of price-relevant.
        self.assertEqual(summary["bare_dividend_without_amount_rows"], 1)
        # Every bucket in the summary is one of the seven canonical buckets.
        self.assertEqual(set(summary["bucket_counts"]), {"dividend_cash", "rights", "buyback", "agm_other", "debt_interest", "scheme_reorg", "unparseable"})


if __name__ == "__main__":
    unittest.main()
