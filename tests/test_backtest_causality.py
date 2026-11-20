"""Regression tests for execution-time information and universe filters."""
from datetime import date, timedelta
from pathlib import Path
import json
import sys
import tempfile
import unittest

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nse_pipeline.backtest.engine import (CostConfig, RotationBacktester, UniverseConfig,
                                          apply_identity_continuity,
                                          apply_key_stabilization,
                                          build_key_stabilization)


def bar(day, key, close=10.0, volume=10000, valid=True):
    return dict(date=day, key=key, symbol=key, series="EQ", open=10.0,
                close=close, previous_close=10.0, volume=volume, valid_ohlcv=valid)


class CausalityTests(unittest.TestCase):
    def test_execution_close_cannot_change_open_orders(self):
        days = [date(2000, 1, 3) + timedelta(days=i) for i in range(3)]
        costs = CostConfig(brokerage_bps=0, stt_bps_buy=0, stt_bps_sell=0,
                           stamp_bps=0, exchange_bps=0, slippage_bps=0,
                           gst_on_brokerage_pct=0)
        def run(final_close):
            rows = [bar(d, k, final_close if d == days[2] and k == "B" else 10)
                    for d in days for k in ("A", "B", "C")]
            return RotationBacktester(costs=costs).run(
                pd.DataFrame(rows), days[:2],
                lambda e, b, d: ["B", "C"] if d == days[0] else ["B", "A"],
                initial_cash=1000)["trades"]
        pd.testing.assert_frame_equal(run(10), run(1))

    def test_rank_respects_liquidity_and_raw_price_not_future_adjusted_level(self):
        d0, d1 = date(2000, 1, 3), date(2000, 1, 4)
        rows = [bar(d, k, c, v) for d, c in ((d0, 10), (d1, 20))
                for k, v in (("LIQUID", 10000), ("ILLIQUID", 1))]
        # A future split changes the absolute adjusted level, not today's
        # economic price or historical eligibility.
        for row in rows:
            row["raw_close"] = row["close"]
            row["close"] /= 100
        from nse_pipeline.backtest.total_return import TotalReturnBacktester
        for kind in (RotationBacktester, TotalReturnBacktester):
            with self.subTest(engine=kind.__name__):
                bt = kind(universe=UniverseConfig(min_days_observed=2))
                ranked = bt.rank_momentum(pd.DataFrame(rows), d1, lookback_sessions=1)
                self.assertEqual(ranked["key"].tolist(), ["LIQUID"])

    def test_duplicate_security_session_cannot_overwrite_fill_silently(self):
        d = date(2000, 1, 3)
        rows = [bar(d, "A"), bar(d, "A"), bar(d + timedelta(days=1), "A")]
        with self.assertRaisesRegex(ValueError, "[Dd]uplicate"):
            RotationBacktester().run(pd.DataFrame(rows), [d], lambda *a: ["A"])

    def test_invalid_numpy_boolean_rejected(self):
        from nse_pipeline.backtest.engine import CircuitConfig, executable_reason
        import numpy as np
        self.assertFalse(executable_reason(bar(date(2000, 1, 3), "A", valid=np.bool_(False)),
                                           CircuitConfig())[0])

    def test_stamp_duty_is_buy_only(self):
        cfg = CostConfig(brokerage_bps=0, stt_bps_buy=0, stt_bps_sell=0,
                         exchange_bps=0, slippage_bps=0, stamp_bps=1.5)
        self.assertAlmostEqual(cfg.cost_for(10000, "buy"), 1.5)
        self.assertEqual(cfg.cost_for(10000, "sell"), 0)


class IdentityRefusalTests(unittest.TestCase):
    """The engine applies only the edges the identity layer admitted."""

    def _bars(self):
        d0, d1 = date(2000, 1, 3), date(2000, 1, 4)
        rows = [bar(d0, "OLD"), bar(d1, "NEW")]
        frame = pd.DataFrame(rows)
        frame["key"] = ["SYMBOL:" + s for s in frame["symbol"]]
        return frame

    def test_refused_api_edge_never_remaps_a_key(self):
        # The identity layer refused this edge (the exchange ratio at the
        # handover is undocumented), so the successor's bars never join the
        # predecessor's key and no holding is carried across.
        edges = [{"old_symbol": "OLD", "new_symbol": "NEW",
                  "effective_date": "2000-01-04",
                  "isin_old": "INE000A", "isin_new": "INE000B",
                  "remap_admissible": False,
                  "refusal_codes": [
                      "successor_continuity_proved_but_exchange_ratio_undocumented"]}]
        mapped, stats = apply_identity_continuity(self._bars(), edges)
        self.assertEqual(set(mapped["key"]), {"SYMBOL:OLD", "SYMBOL:NEW"})
        self.assertEqual(stats["edges_excluded_not_remappable"], 1)
        self.assertEqual(stats["bars_remapped"], 0)

    def test_handover_ratio_the_engine_cannot_verify_is_not_remapped(self):
        # A documented handover ratio is carried by the applied action audit,
        # which this loader does not read: the key is not remapped, so no
        # holding is carried across on an unverified share count.
        edges = [{"old_symbol": "OLD", "new_symbol": "NEW",
                  "effective_date": "2000-01-04",
                  "isin_old": "INE000A", "isin_new": "INE000B",
                  "remap_admissible": True,
                  "share_count_multiplier": 10.0}]
        mapped, stats = apply_identity_continuity(self._bars(), edges)
        self.assertEqual(set(mapped["key"]), {"SYMBOL:OLD", "SYMBOL:NEW"})
        self.assertEqual(stats["edges_excluded_not_remappable"], 1)
        self.assertEqual(stats["bars_remapped"], 0)

    def test_admitted_api_edge_remaps_the_successor_onto_the_predecessor(self):
        # The same ISIN under both symbols proves the same instrument: the
        # successor's rows join the predecessor's key from the dated step.
        edges = [{"old_symbol": "OLD", "new_symbol": "NEW",
                  "effective_date": "2000-01-04",
                  "isin_old": "INE000A", "isin_new": "INE000A",
                  "remap_admissible": True}]
        mapped, stats = apply_identity_continuity(self._bars(), edges)
        self.assertEqual(set(mapped["key"]), {"SYMBOL:OLD"})
        self.assertEqual(stats["edges_used"], 1)
        self.assertEqual(stats["bars_remapped"], 1)


class KeyStabilizationTests(unittest.TestCase):
    """One proven key per symbol, or no change at all.

    The stabilizer may only bind a symbol to the single ISIN that the
    symbol's own bars prove. It must never merge two securities: an
    ambiguous symbol keeps the key it has today and is reported as
    unresolved with a reason.
    """

    @staticmethod
    def _weekdays(count, start=date(2020, 1, 2)):
        days, on = [], start
        while len(days) < count:
            if on.weekday() < 5:
                days.append(on)
            on += timedelta(days=1)
        return days

    def _fixture(self, root, bars, identity_edges=()):
        """A tiny bars database plus the identity-edge file, if any."""
        dbpath = root / "market.duckdb"
        with duckdb.connect(str(dbpath)) as db:
            db.execute("CREATE TABLE bars(date DATE, symbol VARCHAR, series VARCHAR, isin VARCHAR)")
            db.executemany("INSERT INTO bars VALUES (?,?,?,?)", bars)
        if identity_edges:
            path = root / "processed/identity/identity_edges.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(json.dumps(row) + "\n" for row in identity_edges),
                            encoding="utf-8")
        return dbpath

    def _frame(self, bars):
        frame = pd.DataFrame([
            {"date": row[0], "symbol": row[1], "series": row[2], "isin": row[3],
             "security_key": ("ISIN:" + row[3]) if row[3] else ("SYMBOL:" + row[1])}
            for row in bars])
        frame["key"] = frame["security_key"]
        return frame

    def test_null_isin_then_one_isin_keeps_one_key_and_stays_markable(self):
        # The symbol's own bars prove one security: null ISIN first, then
        # exactly one ISIN on the next session. The whole life carries one
        # key, so a holding bought before the ISIN onset is still priced
        # after it instead of being stranded under a dead key.
        days = self._weekdays(4)
        bars = [(days[0], "AAA", "EQ", None), (days[1], "AAA", "EQ", None),
                (days[2], "AAA", "EQ", "INE000A"), (days[3], "AAA", "EQ", "INE000A")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dbpath = self._fixture(root, bars)
            stabilization = build_key_stabilization(dbpath, root)
            self.assertEqual(stabilization.key_for("AAA"), "ISIN:INE000A")
            self.assertEqual(stabilization.stats["stabilized"], 1)
            self.assertEqual(stabilization.unresolved, [])
            mapped, stats = apply_key_stabilization(self._frame(bars), stabilization)
            self.assertEqual(stats["rows_rewritten"], 4)
            # One key for the whole life: the pre-ISIN bar and the post-ISIN
            # bar are the same security, so the position stays markable.
            self.assertEqual(mapped["key"].nunique(), 1)
            self.assertEqual(set(mapped["key"]), {"ISIN:INE000A"})
            day = {mapped.loc[i, "key"]: i for i in range(len(mapped))}
            self.assertEqual(len(day), 1)

    def test_two_isins_are_never_stabilized(self):
        # Two distinct ISINs for one symbol: the bars do not prove one
        # security, so the key must not change and the symbol must be
        # reported as unresolved.
        days = self._weekdays(4)
        bars = [(days[0], "BBB", "EQ", None), (days[1], "BBB", "EQ", "INE000B"),
                (days[2], "BBB", "EQ", "INE000C"), (days[3], "BBB", "EQ", "INE000C")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dbpath = self._fixture(root, bars)
            stabilization = build_key_stabilization(dbpath, root)
            self.assertIsNone(stabilization.key_for("BBB"))
            self.assertEqual(stabilization.unresolved[0]["reason"],
                             "multiple_distinct_isins")
            mapped, stats = apply_key_stabilization(self._frame(bars), stabilization)
            self.assertEqual(stats["rows_rewritten"], 0)
            self.assertEqual(set(mapped["key"]), {"SYMBOL:BBB", "ISIN:INE000B", "ISIN:INE000C"})

    def test_rename_edge_separated_symbol_is_never_stabilized(self):
        # The symbol is the old side of an official rename edge: the ticker
        # does not prove the security across that boundary, so it is refused.
        days = self._weekdays(4)
        bars = [(days[0], "CCC", "EQ", None), (days[1], "CCC", "EQ", None),
                (days[2], "CCC", "EQ", "INE000C"), (days[3], "CCC", "EQ", "INE000C")]
        edges = [{"old_symbol": "CCC", "new_symbol": "DDD",
                  "effective_date": str(days[2]),
                  "verification": "official_symbolchange_row",
                  "isin_old": None, "isin_new": None, "reuse_flags": {}}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dbpath = self._fixture(root, bars, identity_edges=edges)
            stabilization = build_key_stabilization(dbpath, root)
            self.assertIsNone(stabilization.key_for("CCC"))
            self.assertEqual(stabilization.unresolved[0]["reason"],
                             "symbol_on_official_identity_edge")
            mapped, stats = apply_key_stabilization(self._frame(bars), stabilization)
            self.assertEqual(stats["rows_rewritten"], 0)
            self.assertIn("SYMBOL:CCC", set(mapped["key"]))

    def test_handover_gap_beyond_ten_sessions_is_never_stabilized(self):
        # The handover gap is measured in exchange sessions. A gap of ten
        # sessions is allowed; a gap of eleven is refused.
        days = self._weekdays(25)
        bars = []
        for on in days[3:]:                       # keep every session observed
            bars.append((on, "ZZZ", "EQ", "INEZZZZ"))
        for on in days[:3]:
            bars.append((on, "EEE", "EQ", None))
        bars.append((days[12], "EEE", "EQ", "INE000E"))   # gap == 10 -> allowed
        for on in days[:3]:
            bars.append((on, "FFF", "EQ", None))
        bars.append((days[13], "FFF", "EQ", "INE000F"))   # gap == 11 -> refused
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dbpath = self._fixture(root, bars)
            stabilization = build_key_stabilization(dbpath, root)
            self.assertEqual(stabilization.key_for("EEE"), "ISIN:INE000E")
            self.assertIsNone(stabilization.key_for("FFF"))
            reasons = {row["symbol"]: row["reason"] for row in stabilization.unresolved}
            self.assertEqual(reasons.get("FFF"), "handover_gap_exceeds_limit")
            mapped, stats = apply_key_stabilization(self._frame(bars), stabilization)
            self.assertEqual(stats["rows_rewritten"], 4)

    def test_symbols_without_an_isin_onset_keep_their_keys(self):
        # An ISIN-throughout symbol and a symbol that never carries an ISIN
        # are not candidates: their keys are exactly what they were.
        days = self._weekdays(4)
        bars = [(days[0], "GGG", "EQ", "INE000G"), (days[1], "GGG", "EQ", "INE000G"),
                (days[2], "GGG", "EQ", "INE000G"), (days[3], "GGG", "EQ", "INE000G"),
                (days[0], "HHH", "EQ", None), (days[1], "HHH", "EQ", None),
                (days[2], "HHH", "EQ", None), (days[3], "HHH", "EQ", None)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dbpath = self._fixture(root, bars)
            stabilization = build_key_stabilization(dbpath, root)
            self.assertEqual(stabilization.canonical, {})
            self.assertEqual(stabilization.unresolved, [])
            mapped, stats = apply_key_stabilization(self._frame(bars), stabilization)
            self.assertEqual(stats["rows_rewritten"], 0)
            self.assertEqual(set(mapped["key"]), {"ISIN:INE000G", "SYMBOL:HHH"})


    def test_history_identity_uses_the_stabilized_key_for_the_whole_life(self):
        # The full-history runner keys its bars through ``_Identity.key``.
        # A proven symbol must get the same key there before and after the
        # ISIN onset; an unproven symbol must keep the key it has today.
        from nse_pipeline.backtest.history import _Identity
        days = self._weekdays(4)
        bars = [(days[0], "AAA", "EQ", None), (days[1], "AAA", "EQ", None),
                (days[2], "AAA", "EQ", "INE000A"), (days[3], "AAA", "EQ", "INE000A"),
                (days[0], "BBB", "EQ", None), (days[1], "BBB", "EQ", None),
                (days[2], "BBB", "EQ", "INE000B"), (days[3], "BBB", "EQ", "INE000C")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root, bars)
            identity = _Identity(root, set(), [])
            self.assertEqual(identity.key("AAA", None, days[0]), "ISIN:INE000A")
            self.assertEqual(identity.key("AAA", "INE000A", days[2]), "ISIN:INE000A")
            # Two ISINs: not proven, so the fallback key is unchanged.
            self.assertEqual(identity.key("BBB", None, days[0]), "SYMBOL:BBB")


if __name__ == "__main__":
    unittest.main()
