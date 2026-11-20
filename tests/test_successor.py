"""Synthetic tests for src/nse_pipeline/backtest/successor.py (no DB)."""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from nse_pipeline.backtest.engine import RotationBacktester
from nse_pipeline.backtest.successor import (
    SuccessorBacktester,
    apply_swap,
    classify_entry,
    classify_entries,
    exclusion_windows,
    run_sanofi_raymond_1to1,
    run_synthetic,
)

D0 = date(2024, 1, 2)
DATES = [D0 + timedelta(days=i) for i in range(6)]


def row(d, key, o=100.0, c=100.0, vol=100000, valid=True):
    return {"date": d, "key": key, "symbol": key, "series": "EQ",
            "open": o, "high": max(o, c) + 1, "low": min(o, c) - 1, "close": c,
            "previous_close": c, "volume": vol, "valid_ohlcv": valid}


def verified(scheme_id, scheme_type, symbol, successor, ratio_num, ratio_den,
             cash, event_date, status="effective"):
    return {
        "scheme_id": scheme_id, "scheme_type": scheme_type,
        "parties": [{"symbol": symbol, "company_name": symbol, "role": "target"}],
        "dates": {"effective_date": event_date.isoformat()},
        "ratio_num": ratio_num, "ratio_den": ratio_den, "cash_per_share": cash,
        "successor_symbol": successor,
        "pdf_url": "https://nsearchives.nseindia.com/synthetic.pdf",
        "page_ref": "p.42", "status": status, "verified_by": "manual-reader",
        "extraction_source": "manual PDF reading (synthetic)",
        "terms_verified": True,
    }


def unverified_stub(symbol, event_date):
    e = verified("UNV-" + symbol, "merger", symbol, "B", 1, 1, None, event_date)
    e["terms_verified"] = False
    e["verified_by"] = None
    return e


class SwapMathTests(unittest.TestCase):
    def test_merger_swap_with_fractional_cash(self):
        whole, cash = apply_swap(10, 1, 2, 4.0, 20.0)
        # 10 * 1/2 = 5 successor shares, no fraction.
        self.assertAlmostEqual(whole, 5.0)
        self.assertAlmostEqual(cash, 0.0)
        whole, cash = apply_swap(5, 1, 2, 4.0, 20.0)
        # 5 * 1/2 = 2.5 -> 2 shares + 0.5 * Rs4 fractional cash.
        self.assertAlmostEqual(whole, 2.0)
        self.assertAlmostEqual(cash, 2.0)

    def test_fractional_cash_falls_back_to_successor_close(self):
        whole, cash = apply_swap(5, 1, 2, None, 20.0)
        self.assertAlmostEqual(whole, 2.0)
        self.assertAlmostEqual(cash, 0.5 * 20.0)

    def test_fractional_cash_never_invented_without_term_or_close(self):
        with self.assertRaises(ValueError):
            apply_swap(5, 1, 2, None, None)
        with self.assertRaises(ValueError):
            apply_swap(-1, 1, 1, None, 10.0)
        with self.assertRaises(ValueError):
            apply_swap(5, 0, 1, None, 10.0)


class ClassifyTests(unittest.TestCase):
    def test_unverified_with_ratio_is_unresolved_never_applied(self):
        e = unverified_stub("A", DATES[3])
        disp, reason = classify_entry(e)
        self.assertEqual(disp, "unresolved")
        self.assertIn("terms_verified", reason)

    def test_missing_citation_rejected(self):
        e = verified("M", "merger", "A", "B", 1, 1, None, DATES[3])
        e["page_ref"] = None
        self.assertEqual(classify_entry(e)[0], "unresolved")

    def test_price_inference_fields_rejected(self):
        e = verified("P", "merger", "A", "B", 1, 1, None, DATES[3])
        e["extraction_source"] = "from raw price discontinuity"
        self.assertEqual(classify_entry(e)[0], "unresolved")
        e2 = verified("P2", "merger", "A", "B", 1, 1, None, DATES[3])
        e2["inferred_from_price"] = True
        self.assertEqual(classify_entry(e2)[0], "unresolved")

    def test_withdrawn_ignored_and_cash_out_requires_cash(self):
        e = verified("W", "merger", "A", "B", 1, 1, None, DATES[3], status="withdrawn")
        self.assertEqual(classify_entry(e)[0], "ignored_withdrawn")
        e2 = verified("C", "cash_out", "A", None, None, None, None, DATES[3])
        self.assertEqual(classify_entry(e2)[0], "unresolved")
        e3 = verified("C2", "cash_out", "A", None, None, None, 25.0, DATES[3])
        self.assertEqual(classify_entry(e3)[0], "actionable")

    def test_partition_counts(self):
        entries = [verified("V", "merger", "A", "B", 1, 1, None, DATES[3]),
                   unverified_stub("C", DATES[3]),
                   verified("W", "merger", "A", "B", 1, 1, None, DATES[3], status="withdrawn")]
        part = classify_entries(entries)
        self.assertEqual(len(part["actionable"]), 1)
        self.assertEqual(len(part["unresolved"]), 1)
        self.assertEqual(len(part["withdrawn"]), 1)


class EngineIntegrationTests(unittest.TestCase):
    def test_no_entries_matches_base_engine(self):
        rows = []
        for i, d in enumerate(DATES):
            rows.append(row(d, "A", o=100 + i, c=100 + i))
            rows.append(row(d, "B", o=50 + i, c=50 + i))
        bars = pd.DataFrame(rows)
        sig = lambda e, b, d: ["A", "B"]
        base = RotationBacktester().run(bars, [D0], sig, initial_cash=100000)
        succ = SuccessorBacktester().run(bars, [D0], sig, initial_cash=100000,
                                         successor_entries=[])
        self.assertAlmostEqual(base["stats"]["final_equity"], succ["stats"]["final_equity"])
        self.assertEqual(len(base["trades"]), len(succ["trades"]))
        self.assertEqual(len(succ["corporate_actions"]), 0)

    def test_merger_transition_replaces_target_with_survivor(self):
        rows = []
        for i, d in enumerate(DATES):
            rows.append(row(d, "A", o=10.0, c=10.0))
            rows.append(row(d, "B", o=20.0, c=20.0))
            rows.append(row(d, "C", o=10.0, c=10.0))
        bars = pd.DataFrame(rows)
        bars = bars[~((bars["symbol"] == "A") & (bars["date"] >= DATES[3]))]
        entries = [verified("M1", "merger", "A", "B", 1, 2, 4.0, DATES[3])]
        sig = lambda e, b, d: ["A", "C"] if d == D0 else ["B"]
        res = SuccessorBacktester().run(bars, [D0], sig, initial_cash=1_000_000.0,
                                        successor_entries=entries,
                                        exclusion_buffer_sessions=1)
        ca = res["corporate_actions"]
        self.assertEqual(len(ca), 1)
        self.assertEqual(ca.iloc[0]["action"], "merged_into_survivor")
        self.assertGreater(float(ca.iloc[0]["successor_shares"]), 0)
        held = {p["key"] for p in res["positions"]}
        self.assertNotIn("A", held)
        self.assertTrue((res["equity"]["cash"] >= -1e-6).all())

    def test_demerger_keeps_parent_and_adds_resulting_company(self):
        rows = []
        for d in DATES:
            rows.append(row(d, "P", o=100.0, c=100.0))
            rows.append(row(d, "R", o=50.0, c=50.0))
        bars = pd.DataFrame(rows)
        entries = [verified("D1", "demerger", "P", "R", 1, 1, None, DATES[3])]
        sig = lambda e, b, d: ["P"] if d == D0 else ["P", "R"]
        res = SuccessorBacktester().run(bars, [D0], sig, initial_cash=100000.0,
                                        successor_entries=entries,
                                        exclusion_buffer_sessions=1)
        ca = res["corporate_actions"]
        self.assertEqual(ca.iloc[0]["action"], "demerged_child")
        self.assertGreater(float(ca.iloc[0]["successor_shares"]), 0)
        held = {p["key"] for p in res["positions"]}
        self.assertIn("P", held)  # parent retained

    def test_cash_out_gives_terminal_proceeds(self):
        rows = []
        for d in DATES:
            rows.append(row(d, "A", o=10.0, c=10.0))
            rows.append(row(d, "B", o=10.0, c=10.0))
        bars = pd.DataFrame(rows)
        entries = [verified("CO1", "cash_out", "A", None, None, None, 25.0, DATES[3])]
        sig = lambda e, b, d: ["A"] if d == D0 else ["B"]
        res = SuccessorBacktester().run(bars, [D0], sig, initial_cash=100000.0,
                                        successor_entries=entries,
                                        exclusion_buffer_sessions=1)
        ca = res["corporate_actions"]
        self.assertEqual(ca.iloc[0]["action"], "cashed_out")
        self.assertAlmostEqual(float(ca.iloc[0]["cash_credited"]),
                               float(ca.iloc[0]["shares_before"]) * 25.0)

    def test_unresolved_symbol_excluded_from_signal_window(self):
        sess = list(DATES)
        windows = exclusion_windows([unverified_stub("A", DATES[3])], sess, buffer_sessions=1)
        self.assertIn("A", windows.get(DATES[3], set()))
        self.assertIn("A", windows.get(DATES[2], set()))
        self.assertNotIn("A", windows.get(DATES[0], set()))
        rows = []
        for d in DATES:
            rows.append(row(d, "A", o=100.0, c=100.0))
            rows.append(row(d, "B", o=100.0, c=100.0))
        bars = pd.DataFrame(rows)
        # Signal wants A on the excluded date; B survives.
        sig = lambda e, b, d: ["A", "B"] if d == DATES[3] else ["B"]
        res = SuccessorBacktester().run(bars, [DATES[3]], sig, initial_cash=100000.0,
                                        successor_entries=[unverified_stub("A", DATES[3])],
                                        exclusion_buffer_sessions=1)
        buys = res["trades"][res["trades"]["side"] == "buy"]
        self.assertNotIn("A", set(buys["key"].tolist()))

    def test_sample_runners_produce_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            syn = run_synthetic(tmp)
            self.assertGreater(syn["corporate_actions"][0].get("successor_shares", 0), 0)
            samp = run_sanofi_raymond_1to1(tmp)
            actions = [c["action"] for c in samp["corporate_actions"]]
            self.assertIn("demerged_child", actions)
            self.assertIn("merged_into_survivor", actions)


if __name__ == "__main__":
    unittest.main()
