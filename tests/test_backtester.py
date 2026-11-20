"""Synthetic tests for src/backtester.py (no DB required)."""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from nse_pipeline.backtest.engine import (
    CircuitConfig, CostConfig, RotationBacktester, apply_identity_continuity,
    executable_reason, map_adjusted_columns, month_end_dates,
)


def make_bars(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def base_row(d: date, key: str, o=100.0, h=101.0, l=99.0, c=100.0,
             prev=100.0, vol=100000, valid=True, series="EQ"):
    return {"date": d, "key": key, "symbol": key, "series": series,
            "open": o, "high": h, "low": l, "close": c,
            "previous_close": prev, "volume": vol, "valid_ohlcv": valid}


class BacktesterTests(unittest.TestCase):
    def setUp(self):
        self.d0 = date(2020, 1, 2)
        self.d1 = date(2020, 1, 3)
        self.d2 = date(2020, 1, 6)

    def test_next_session_execution(self):
        # Signal at close d0, must fill at open d1, not close d0.
        rows = [
            base_row(self.d0, "AAA", o=100, c=100, prev=100),
            base_row(self.d1, "AAA", o=110, c=105, prev=100),
            base_row(self.d2, "AAA", o=105, c=105, prev=105),
        ]
        bars = make_bars(rows)
        bt = RotationBacktester()
        res = bt.run(bars, [self.d0], lambda e, b, d: ["AAA"], initial_cash=100000)
        self.assertEqual(len(res["trades"]), 1)
        self.assertEqual(res["trades"].iloc[0]["date"], self.d1)
        self.assertAlmostEqual(float(res["trades"].iloc[0]["fill_price"]), 110.0)

    def test_costs_reduce_cash(self):
        costs = CostConfig(brokerage_bps=100.0, stt_bps_buy=100.0, stt_bps_sell=100.0,
                           stamp_bps=0.0, exchange_bps=0.0, slippage_bps=0.0,
                           gst_on_brokerage_pct=0.0, brokerage_cap_per_order=1e9)
        rows = [base_row(self.d0, "AAA"), base_row(self.d1, "AAA", o=100, c=100, prev=100),
                base_row(self.d2, "AAA")]
        bars = make_bars(rows)
        free = RotationBacktester(costs=CostConfig(brokerage_bps=0, stt_bps_buy=0,
                               stt_bps_sell=0, stamp_bps=0, exchange_bps=0, slippage_bps=0,
                               gst_on_brokerage_pct=0))
        costly = RotationBacktester(costs=costs)
        r_free = free.run(bars, [self.d0], lambda e, b, d: ["AAA"], initial_cash=100000)
        r_cost = costly.run(bars, [self.d0], lambda e, b, d: ["AAA"], initial_cash=100000)
        self.assertLess(r_cost["stats"]["final_equity"], r_free["stats"]["final_equity"])
        self.assertGreater(float(r_cost["trades"]["cost"].sum()), 0)

    def test_circuit_skip_upper_limit(self):
        rows = [
            base_row(self.d0, "AAA"),
            # open at +20% circuit -> skip
            base_row(self.d1, "AAA", o=120, h=120, l=120, c=120, prev=100),
            base_row(self.d2, "AAA"),
        ]
        bars = make_bars(rows)
        bt = RotationBacktester(circuit=CircuitConfig(limit_pct=0.20))
        res = bt.run(bars, [self.d0], lambda e, b, d: ["AAA"], initial_cash=100000)
        self.assertEqual(len(res["trades"]), 0)
        self.assertTrue((res["skips"]["reason"] == "open_upper_limit").any())
        ok, reason = executable_reason(rows[1], CircuitConfig(limit_pct=0.20))
        self.assertFalse(ok)

    def test_zero_volume_and_invalid_skip(self):
        rows = [
            base_row(self.d0, "AAA"),
            base_row(self.d1, "AAA", vol=0),
            base_row(self.d2, "AAA", valid=False),
        ]
        bars = make_bars(rows)
        bt = RotationBacktester()
        res = bt.run(bars, [self.d0], lambda e, b, d: ["AAA"], initial_cash=100000)
        self.assertEqual(len(res["trades"]), 0)
        self.assertTrue((res["skips"]["reason"] == "zero_volume").any())

    def test_invalid_ohlcv_skip(self):
        ok, reason = executable_reason(base_row(self.d0, "AAA", valid=False),
                                       CircuitConfig())
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_ohlcv")

    def test_cash_conservation_no_negative_cash(self):
        rows = []
        for i, d in enumerate([self.d0, self.d1, self.d2, self.d0 + timedelta(days=5),
                               self.d0 + timedelta(days=6)]):
            rows.append(base_row(d, "AAA", o=100, c=100 + i, prev=100 + i))
            rows.append(base_row(d, "BBB", o=50, c=50 + i, prev=50 + i))
        bars = make_bars(rows)
        bt = RotationBacktester()
        res = bt.run(bars, [self.d0, self.d0 + timedelta(days=5)],
                     lambda e, b, d: ["AAA", "BBB"], initial_cash=50000)
        self.assertTrue((res["equity"]["cash"] >= -1e-6).all())
        # equity == cash + positions_value by construction
        diff = (res["equity"]["equity"]
                - (res["equity"]["cash"] + res["equity"]["positions_value"])).abs()
        self.assertTrue((diff < 1e-6).all())

    def test_survivorship_no_future_symbols(self):
        # BBB first appears on d1; signal on d0 must not see it.
        rows = [
            base_row(self.d0, "AAA"),
            base_row(self.d1, "AAA", o=100, c=100, prev=100),
            base_row(self.d1, "BBB", o=10, c=10, prev=10),
            base_row(self.d2, "AAA"), base_row(self.d2, "BBB"),
        ]
        bars = make_bars(rows)
        bt = RotationBacktester()
        eligible = bt.build_eligible(bars, self.d0)
        self.assertIn("AAA", set(eligible["key"]))
        self.assertNotIn("BBB", set(eligible["key"]))

    def test_delisting_flag_and_last_price(self):
        # Hold AAA on d1, then disappears on d2 -> flagged, no crash.
        rows = [
            base_row(self.d0, "AAA"),
            base_row(self.d1, "AAA", o=100, c=100, prev=100, vol=100000),
            # d2: AAA absent (delisted/suspended); only BBB present
            base_row(self.d2, "BBB", o=10, c=10, prev=10),
        ]
        bars = make_bars(rows)
        bt = RotationBacktester()
        res = bt.run(bars, [self.d0], lambda e, b, d: ["AAA"], initial_cash=100000)
        self.assertGreaterEqual(len(res["trades"]), 1)
        flags = {p["key"]: p["flag"] for p in res["positions"]}
        # Either sold (no position) or flagged delisted — never silently dropped.
        traded = set(res["trades"]["key"])
        self.assertIn("AAA", traded)
        self.assertTrue(flags.get("AAA", "delisted_or_suspended") is not None)

    def test_month_end_calendar(self):
        dates = [date(2020, 1, 30), date(2020, 1, 31), date(2020, 2, 27),
                 date(2020, 2, 28)]
        self.assertEqual(month_end_dates(dates), [date(2020, 1, 31), date(2020, 2, 28)])

    def test_adjusted_exdate_fill_uses_adj_open(self):
        # 2:1 split on d1: raw open halves to 50, adjusted open stays 100.
        rows = [
            {"date": self.d0, "key": "AAA", "symbol": "AAA", "series": "EQ",
             "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
             "previous_close": 100.0, "volume": 100000, "valid_ohlcv": True,
             "adj_open": 100.0, "adj_high": 101.0, "adj_low": 99.0,
             "adj_close": 100.0, "adj_previous_close": 100.0,
             "adjustment_factor": 1.0, "is_adjusted": False},
            {"date": self.d1, "key": "AAA", "symbol": "AAA", "series": "EQ",
             "open": 50.0, "high": 51.0, "low": 49.0, "close": 50.0,
             "previous_close": 100.0, "volume": 100000, "valid_ohlcv": True,
             "adj_open": 100.0, "adj_high": 102.0, "adj_low": 98.0,
             "adj_close": 100.0, "adj_previous_close": 100.0,
             "adjustment_factor": 1.0, "is_adjusted": False},
            {"date": self.d2, "key": "AAA", "symbol": "AAA", "series": "EQ",
             "open": 50.0, "high": 51.0, "low": 49.0, "close": 50.0,
             "previous_close": 50.0, "volume": 100000, "valid_ohlcv": True,
             "adj_open": 100.0, "adj_high": 101.0, "adj_low": 99.0,
             "adj_close": 100.0, "adj_previous_close": 100.0,
             "adjustment_factor": 1.0, "is_adjusted": False},
        ]
        bars = map_adjusted_columns(make_bars(rows))
        self.assertAlmostEqual(float(bars.loc[bars["date"] == self.d1, "open"].iloc[0]), 100.0)
        bt = RotationBacktester()
        res = bt.run(bars, [self.d0], lambda e, b, d: ["AAA"], initial_cash=100000)
        self.assertEqual(len(res["trades"]), 1)
        self.assertEqual(res["trades"].iloc[0]["date"], self.d1)
        self.assertAlmostEqual(float(res["trades"].iloc[0]["fill_price"]), 100.0)

    def test_adjusted_raw_parity_no_actions(self):
        # factor == 1 everywhere means adj == raw, so both modes agree.
        rows = []
        for i, d in enumerate([self.d0, self.d1, self.d2]):
            row = base_row(d, "AAA", o=100.0 + i, c=100.0 + i, prev=100.0 + i)
            row.update({f"adj_{c}": row[c] for c in ("open", "high", "low", "close", "previous_close")})
            row.update({"adjustment_factor": 1.0, "is_adjusted": False})
            rows.append(row)
        raw = make_bars(rows)
        adj = map_adjusted_columns(make_bars(rows))
        bt = RotationBacktester()
        sig = lambda e, b, d: ["AAA"]
        r_raw = bt.run(raw, [self.d0], sig, initial_cash=100000)
        r_adj = bt.run(adj, [self.d0], sig, initial_cash=100000)
        self.assertAlmostEqual(r_raw["stats"]["final_equity"], r_adj["stats"]["final_equity"])
        self.assertEqual(r_raw["stats"]["n_trades"], r_adj["stats"]["n_trades"])
        self.assertAlmostEqual(r_raw["stats"]["total_return"], r_adj["stats"]["total_return"])

    def test_identity_continuity_renamed_symbol(self):
        # OLD -> NEW rename effective d1; no-ISIN keys must merge, and
        # bars strictly before the effective date must not be remapped.
        rows = [
            base_row(self.d0, "OLD", o=100, c=100, prev=100),
            base_row(self.d1, "NEW", o=100, c=110, prev=100),
            base_row(self.d2, "NEW", o=110, c=110, prev=110),
        ]
        bars = make_bars(rows)
        bars["key"] = ["SYMBOL:" + s for s in bars["symbol"]]
        edges = [{"old_symbol": "OLD", "new_symbol": "NEW",
                  "effective_date": self.d1.isoformat(),
                  "isin_old": None, "isin_new": None}]
        mapped, stats = apply_identity_continuity(bars, edges)
        keys_d1 = set(mapped.loc[mapped["date"] == self.d1, "key"])
        keys_d2 = set(mapped.loc[mapped["date"] == self.d2, "key"])
        keys_d0 = set(mapped.loc[mapped["date"] == self.d0, "key"])
        self.assertEqual(keys_d1, {"SYMBOL:OLD"})
        self.assertEqual(keys_d2, {"SYMBOL:OLD"})
        self.assertEqual(keys_d0, {"SYMBOL:OLD"})
        self.assertGreaterEqual(stats["bars_remapped"], 1)

    def test_identity_ambiguous_branch_not_chained(self):
        # OLD has two different successors: neither may be chained blindly.
        rows = [
            base_row(self.d0, "OLD", o=100, c=100, prev=100),
            base_row(self.d1, "NEW1", o=100, c=100, prev=100),
            base_row(self.d1, "NEW2", o=100, c=100, prev=100),
        ]
        bars = make_bars(rows)
        bars["key"] = ["SYMBOL:" + s for s in bars["symbol"]]
        edges = [
            {"old_symbol": "OLD", "new_symbol": "NEW1",
             "effective_date": self.d1.isoformat(), "isin_old": None, "isin_new": None},
            {"old_symbol": "OLD", "new_symbol": "NEW2",
             "effective_date": self.d1.isoformat(), "isin_old": None, "isin_new": None},
        ]
        mapped, stats = apply_identity_continuity(bars, edges)
        self.assertEqual(stats["bars_remapped"], 0)
        self.assertIn("SYMBOL:NEW1", set(mapped["key"]))
        self.assertIn("SYMBOL:NEW2", set(mapped["key"]))

    def test_identity_isin_key_not_overridden(self):
        # ISIN-keyed rows keep their ISIN key even when an edge exists.
        rows = [
            base_row(self.d0, "OLD", o=100, c=100, prev=100),
            base_row(self.d1, "NEW", o=100, c=100, prev=100),
        ]
        bars = make_bars(rows)
        bars["key"] = ["ISIN:INE001"] * len(bars)
        edges = [{"old_symbol": "OLD", "new_symbol": "NEW",
                  "effective_date": self.d1.isoformat(),
                  "isin_old": None, "isin_new": None}]
        mapped, stats = apply_identity_continuity(bars, edges)
        self.assertEqual(set(mapped["key"]), {"ISIN:INE001"})
        self.assertEqual(stats["bars_remapped"], 0)


if __name__ == "__main__":
    unittest.main()
