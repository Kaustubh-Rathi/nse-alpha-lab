"""Synthetic tests for src/nse_pipeline/backtest/total_return.py (no DB)."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import pandas as pd

from nse_pipeline.backtest.engine import RotationBacktester, UniverseConfig, annual_stats
from nse_pipeline.backtest.total_return import (
    TotalReturnBacktester,
    attach_total_close,
    compare_series,
    events_by_key,
    load_dividend_events,
    reconstruct_equities,
    total_momentum_signal,
)

D0, D1, D2, D3 = date(2020, 1, 2), date(2020, 1, 3), date(2020, 1, 6), date(2020, 1, 7)


def split_row(d, key, o=100.0, c=100.0, prev=100.0, vol=100000, valid=True):
    return {"date": d, "key": key, "symbol": key, "series": "EQ",
            "open": o, "high": max(o, c) + 1, "low": min(o, c) - 1, "close": c,
            "previous_close": prev, "volume": vol, "valid_ohlcv": valid,
            "adj_open": o, "adj_high": max(o, c) + 1, "adj_low": min(o, c) - 1,
            "adj_close": c, "adj_previous_close": prev,
            "adjustment_factor": 1.0, "is_adjusted": False}


def events_frame(rows):
    return pd.DataFrame(rows, columns=["symbol", "series", "ex_date",
                                       "dividend_factor", "dividend_per_share"])


class LoadEventsTests(unittest.TestCase):
    def test_filters_included_series_window_and_factor(self):
        import json, tempfile
        recs = [
            {"symbol": "A", "series": "EQ", "ex_date": "2020-01-06", "dividend_per_share": 5.0,
             "included_in_factor": True, "dividend_factor": 0.95},
            {"symbol": "A", "series": "EQ", "ex_date": "2020-01-07", "dividend_per_share": 5.0,
             "included_in_factor": False, "dividend_factor": 0.95},
            {"symbol": "B", "series": "BE", "ex_date": "2020-01-06", "dividend_per_share": 5.0,
             "included_in_factor": True, "dividend_factor": 0.95},
            {"symbol": "C", "series": "EQ", "ex_date": "2019-12-31", "dividend_per_share": 5.0,
             "included_in_factor": True, "dividend_factor": 0.95},
            {"symbol": "D", "series": "EQ", "ex_date": "2020-01-06", "dividend_per_share": 5.0,
             "included_in_factor": True, "dividend_factor": 1.5},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")
            path = fh.name
        got = load_dividend_events(path, start="2020-01-01", end="2020-12-31", series="EQ")
        self.assertEqual(len(got), 1)
        self.assertEqual(got.iloc[0]["symbol"], "A")
        self.assertAlmostEqual(got.iloc[0]["dividend_factor"], 0.95)
        # Inclusive end, exclusive start.
        got2 = load_dividend_events(path, start="2020-01-06", end="2020-01-06", series="EQ")
        self.assertEqual(len(got2), 0)


class AttachTotalCloseTests(unittest.TestCase):
    def test_total_close_removes_ex_date_drop_and_keeps_split(self):
        bars = pd.DataFrame([
            split_row(D0, "A", o=100, c=100, prev=100),
            split_row(D1, "A", o=100, c=100, prev=100),
            split_row(D2, "A", o=95, c=95, prev=100),   # ex-date drop
            split_row(D3, "A", o=95, c=96, prev=95),
        ])
        events = events_frame([{"symbol": "A", "series": "EQ", "ex_date": D2,
                                "dividend_factor": 0.95, "dividend_per_share": 5.0}])
        # Factor 0.95 is what dividends.compute_factors would emit for
        # P_prev=100, D=5; attach must carry it through apply_total_return
        # (pre-ex bars discounted, ex-date bar not).
        out = attach_total_close(bars, events)
        self.assertAlmostEqual(out.iloc[0]["total_close"], 95.0)
        self.assertAlmostEqual(out.iloc[2]["total_close"], 95.0)
        self.assertAlmostEqual(out.iloc[2]["close"], 95.0)  # split untouched
        self.assertAlmostEqual(out.iloc[2]["open"], 95.0)
        self.assertAlmostEqual(out.iloc[3]["total_close"] / out.iloc[2]["total_close"] - 1,
                               96 / 95 - 1)

    def test_no_events_total_equals_split(self):
        bars = pd.DataFrame([split_row(D0, "A"), split_row(D1, "A")])
        out = attach_total_close(bars, events_frame([]))
        pd.testing.assert_series_equal(out["total_close"], out["close"], check_names=False)


class AccrualEngineTests(unittest.TestCase):
    def test_parity_with_split_engine_when_no_dividends(self):
        rows = []
        for i, d in enumerate([D0, D1, D2, D3]):
            rows.append(split_row(d, "A", o=100 + i, c=100 + i, prev=100 + i))
            rows.append(split_row(d, "B", o=50 + i, c=50 + i, prev=50 + i))
        bars = pd.DataFrame(rows)
        sig = lambda e, b, d: ["A", "B"]
        r_split = RotationBacktester().run(bars, [D0], sig, initial_cash=100000)
        r_total = TotalReturnBacktester().run(bars, [D0], sig, initial_cash=100000,
                                              dividend_events=events_frame([]))
        self.assertAlmostEqual(r_split["stats"]["final_equity"], r_total["stats"]["final_equity"])
        self.assertEqual(len(r_split["trades"]), len(r_total["trades"]))
        self.assertAlmostEqual(r_split["stats"]["total_return"], r_total["stats"]["total_return"])

    def test_accrual_makes_ex_date_equity_continuous(self):
        # Buy at 100 on D1, ex-drop to 95 on D2 (Rs5 dividend, f=0.95).
        bars = pd.DataFrame([
            split_row(D0, "A", o=100, c=100, prev=100),
            split_row(D1, "A", o=100, c=100, prev=100),
            split_row(D2, "A", o=95, c=95, prev=100),
            split_row(D3, "A", o=95, c=96, prev=95),
        ])
        events = events_frame([{"symbol": "A", "series": "EQ", "ex_date": D2,
                                "dividend_factor": 0.95, "dividend_per_share": 5.0}])
        sig = lambda e, b, d: ["A"]
        total = TotalReturnBacktester().run(bars, [D0], sig, initial_cash=100000,
                                            dividend_events=events)
        eq = total["equity"].set_index("date")["equity"]
        pos_split = total["equity"].set_index("date")["positions_value_split_basis"]
        pos_total = total["equity"].set_index("date")["positions_value"]
        # Total (accrued) equity is flat across the ex-date; the split
        # position value falls 5% (equity itself includes leftover cash).
        self.assertAlmostEqual(eq.loc[D2] / eq.loc[D1], 1.0, places=9)
        self.assertAlmostEqual(pos_total.loc[D2] / pos_total.loc[D1], 1.0, places=9)
        self.assertAlmostEqual(pos_split.loc[D2] / pos_split.loc[D1], 0.95, places=9)
        self.assertEqual(len(total["dividends"]), 1)
        self.assertGreater(total["dividends"].iloc[0]["shares_after"],
                           total["dividends"].iloc[0]["shares_before"])

    def test_execution_fill_uses_split_open_not_total(self):
        # Total close on D1 is discounted (95) but the fill must be the
        # split open (100): cash spent must not use future dividend info.
        bars = pd.DataFrame([
            split_row(D0, "A", o=100, c=100, prev=100),
            split_row(D1, "A", o=100, c=100, prev=100),
            split_row(D2, "A", o=95, c=95, prev=100),
        ])
        events = events_frame([{"symbol": "A", "series": "EQ", "ex_date": D2,
                                "dividend_factor": 0.95, "dividend_per_share": 5.0}])
        bars = attach_total_close(bars, events)
        self.assertAlmostEqual(bars.loc[bars["date"] == D1, "total_close"].iloc[0], 95.0)
        res = TotalReturnBacktester().run(bars, [D0], lambda e, b, d: ["A"],
                                          initial_cash=100000, dividend_events=events)
        buy = res["trades"][res["trades"]["side"] == "buy"].iloc[0]
        self.assertAlmostEqual(float(buy["fill_price"]), 100.0)
        self.assertAlmostEqual(float(buy["notional"] / buy["shares"]), 100.0)

    def test_sell_after_ex_realises_accrued_shares(self):
        # Hold through ex, sell on D3: proceeds use accrued shares, so
        # sale notional exceeds split-basis shares x fill.
        bars = pd.DataFrame([
            split_row(D0, "A", o=100, c=100, prev=100),
            split_row(D1, "A", o=100, c=100, prev=100),
            split_row(D2, "A", o=95, c=95, prev=100),
            split_row(D3, "A", o=96, c=96, prev=95),
        ])
        events = events_frame([{"symbol": "A", "series": "EQ", "ex_date": D2,
                                "dividend_factor": 0.95, "dividend_per_share": 5.0}])
        sig = lambda e, b, d, c=None: ["A"] if d == D0 else []
        res = TotalReturnBacktester().run(bars, [D0, D2], sig, initial_cash=100000,
                                          dividend_events=events)
        sells = res["trades"][res["trades"]["side"] == "sell"]
        buys = res["trades"][res["trades"]["side"] == "buy"]
        self.assertEqual(len(sells), 1)
        split_shares = float(buys["shares"].sum())
        self.assertGreater(float(sells.iloc[0]["shares"]), split_shares)
        self.assertAlmostEqual(float(sells.iloc[0]["shares"]), split_shares / 0.95, places=6)

    def test_total_momentum_not_fooled_by_ex_date_drop(self):
        # A drops 5% on ex-date (split), flat on total; B falls 2% genuinely.
        # Total momentum must rank A above B at D2.
        bars = pd.DataFrame([
            split_row(D0, "A", o=100, c=100, prev=100),
            split_row(D0, "B", o=100, c=100, prev=100),
            split_row(D1, "A", o=100, c=100, prev=100),
            split_row(D1, "B", o=100, c=100, prev=100),
            split_row(D2, "A", o=95, c=95, prev=100),
            split_row(D2, "B", o=98, c=98, prev=100),
        ])
        events = events_frame([{"symbol": "A", "series": "EQ", "ex_date": D2,
                                "dividend_factor": 0.95, "dividend_per_share": 5.0}])
        bars = attach_total_close(bars, events)
        eng = TotalReturnBacktester(universe=UniverseConfig(min_days_observed=3))
        ranked_total = eng.rank_momentum(bars, D2, lookback_sessions=2, price_col="total_close")
        ranked_split = eng.rank_momentum(bars, D2, lookback_sessions=2, price_col="close")
        self.assertEqual(ranked_total.iloc[0]["key"], "A")    # total: 0% vs -2%
        self.assertEqual(ranked_split.iloc[0]["key"], "B")    # split: -5% vs -2%
        # Signal helper agrees with total ranking.
        self.assertEqual(total_momentum_signal(top_n=1, lookback_sessions=2)(eng, bars, D2, {"day_map": {d: g for d, g in bars.groupby("date")},
                                                                          "sessions": [D0, D1, D2]}), ["A"])


class OverlayTests(unittest.TestCase):
    def _split_run_with_dividend(self):
        bars = pd.DataFrame([
            split_row(D0, "A", o=100, c=100, prev=100),
            split_row(D1, "A", o=100, c=100, prev=100),
            split_row(D2, "A", o=95, c=95, prev=100),
            split_row(D3, "A", o=95, c=96, prev=95),
        ])
        events = events_frame([{"symbol": "A", "series": "EQ", "ex_date": D2,
                                "dividend_factor": 0.95, "dividend_per_share": 5.0}])
        bars = attach_total_close(bars, events)
        split = RotationBacktester().run(bars, [D0], lambda e, b, d: ["A"],
                                         initial_cash=100000)
        return bars, events, split

    def test_overlay_no_events_equals_split(self):
        bars = pd.DataFrame([split_row(D0, "A"), split_row(D1, "A", o=101, c=101, prev=100),
                             split_row(D2, "A", o=101, c=102, prev=101)])
        split = RotationBacktester().run(bars, [D0], lambda e, b, d: ["A"], initial_cash=100000)
        out = reconstruct_equities(split["trades"], split["equity"], bars, events_frame([]))
        np.testing.assert_allclose(out["equity_total"], split["equity"]["equity"].to_numpy(), atol=1e-6)
        np.testing.assert_allclose(out["equity_split"], split["equity"]["equity"].to_numpy(), atol=1e-6)

    def test_overlay_dividend_contribution_and_reconstruction(self):
        bars, events, split = self._split_run_with_dividend()
        out = reconstruct_equities(split["trades"], split["equity"], bars, events)
        # Split reconstruction matches the engine equity at every session.
        np.testing.assert_allclose(out["equity_split"], split["equity"]["equity"].to_numpy(), atol=1e-6)
        # Total overlay is flat across the ex-date and ends above split.
        self.assertAlmostEqual(out.set_index("date").loc[D2, "equity_total"]
                               / out.set_index("date").loc[D1, "equity_total"], 1.0, places=9)
        self.assertGreater(out["equity_total"].iloc[-1], out["equity_split"].iloc[-1])
        # Naive marking understates accrual equity while the position is
        # open pre-ex (its level carries the future-factor constant).
        self.assertLess(out.set_index("date").loc[D1, "equity_naive"],
                        out.set_index("date").loc[D1, "equity_total"])

    def test_compare_series_arithmetic(self):
        split_eq = pd.Series([100.0, 110.0, 121.0], index=[D0, D1, D2])
        total_eq = pd.Series([100.0, 115.0, 132.25], index=[D0, D1, D2])
        cmp = compare_series(split_eq, total_eq)
        dc = cmp["dividend_contribution"]
        self.assertAlmostEqual(dc["final_equity_diff"], 11.25)
        self.assertAlmostEqual(dc["total_return_pp_diff"], 0.1125)
        self.assertAlmostEqual(annual_stats(split_eq)["total_return"], 0.21)
        self.assertAlmostEqual(annual_stats(total_eq)["total_return"], 0.3225)


class DividendCoverageNoteTests(unittest.TestCase):
    def test_limitation_references_live_coverage_not_stale_counts(self):
        from nse_pipeline.backtest import total_return
        import inspect
        src = inspect.getsource(total_return.run_all)
        self.assertIn("dividends-coverage.json", src)
        self.assertNotIn("multi-amount (546) and bare (7070)", src)


class CashConservationTests(unittest.TestCase):
    def test_cash_never_negative_and_accrual_adds_no_cash(self):
        bars = pd.DataFrame([
            split_row(D0, "A", o=100, c=100, prev=100),
            split_row(D1, "A", o=100, c=100, prev=100),
            split_row(D2, "A", o=95, c=95, prev=100),
            split_row(D3, "A", o=95, c=96, prev=95),
        ])
        events = events_frame([{"symbol": "A", "series": "EQ", "ex_date": D2,
                                "dividend_factor": 0.95, "dividend_per_share": 5.0}])
        res = TotalReturnBacktester().run(bars, [D0], lambda e, b, d: ["A"],
                                          initial_cash=100000, dividend_events=events)
        eq = res["equity"]
        self.assertTrue((eq["cash"] >= -1e-6).all())
        # Cash path identical to a split run with the same trades sizing:
        # accrual changes share counts, it never credits cash at ex-date.
        cash_before_ex = float(eq.loc[eq["date"] == D1, "cash"].iloc[0])
        cash_on_ex = float(eq.loc[eq["date"] == D2, "cash"].iloc[0])
        self.assertAlmostEqual(cash_before_ex, cash_on_ex)


if __name__ == "__main__":
    unittest.main()
