"""Synthetic tests for src/nifty_proxy.py (no database required)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.nifty.proxy import (
    METHODOLOGY_ERAS,
    PROXY_LABEL,
    ProxyConfig,
    anchor_for,
    apply_changelog,
    build_observations,
    certainty_for,
    free_float_proxy,
    impact_cost_proxy,
    next_snapshot_for,
    precision_recall,
    rank_proxy,
    select_proxy,
    validate_replay,
)

SNAPSHOTS = {
    ("NIFTY50", "2008-10-31"): {"AAA", "BBB", "CCC"},
    ("NIFTY50", "2009-10-30"): {"BBB", "CCC", "DDD"},
    ("NIFTY500", "2015-10-30"): {"AAA", "BBB"},
}


def candidate(symbol, turnover, freq=1.0, **flags):
    base = {"symbol": symbol, "close_ok": True, "volume_ok": True,
            "history_ok": True, "frequency_ok": True, "turnover_ok": True,
            "avg_turnover": turnover, "trading_frequency": freq,
            "impact_cost_proxy": impact_cost_proxy(turnover),
            "free_float_proxy": free_float_proxy(turnover, freq)}
    base.update(flags)
    return base


class IntervalCarryTests(unittest.TestCase):
    def test_anchor_is_latest_on_or_before(self):
        dates = ["2008-10-31", "2009-10-30"]
        self.assertEqual(anchor_for("2009-02-15", dates), "2008-10-31")
        self.assertEqual(anchor_for("2008-10-31", dates), "2008-10-31")
        self.assertIsNone(anchor_for("2007-01-31", dates))

    def test_next_snapshot_strictly_after(self):
        dates = ["2008-10-31", "2009-10-30"]
        self.assertEqual(next_snapshot_for("2008-10-31", dates), "2009-10-30")
        self.assertIsNone(next_snapshot_for("2009-10-30", dates))

    def test_certainty_levels(self):
        self.assertEqual(certainty_for("2008-10-31", "2008-10-31", "2009-10-30"), "exact_anchor")
        self.assertEqual(certainty_for("2009-01-31", "2008-10-31", "2009-10-30"),
                         "carried_forward_inside_interval")
        self.assertEqual(certainty_for("2010-01-31", "2009-10-30", None),
                         "carried_forward_beyond_last_snapshot")
        self.assertEqual(certainty_for("2000-01-31", None, "2008-10-31"),
                         "unknown_before_first_snapshot")

    def test_before_first_snapshot_emits_nothing(self):
        obs = build_observations(["2000-01-31", "2008-10-31"], SNAPSHOTS,
                                 index_codes=("NIFTY50",))
        months = {o["month_end"] for o in obs}
        self.assertNotIn("2000-01-31", months)  # unknown, never back-filled
        exact = [o for o in obs if o["month_end"] == "2008-10-31"]
        self.assertEqual({o["symbol"] for o in exact}, {"AAA", "BBB", "CCC"})
        self.assertTrue(all(o["certainty"] == "exact_anchor" for o in exact))

    def test_carried_rows_are_labelled_proxy(self):
        obs = build_observations(["2009-01-30"], SNAPSHOTS, index_codes=("NIFTY50",))
        self.assertTrue(obs)
        for row in obs:
            self.assertTrue(row["is_proxy"])
            self.assertFalse(row["is_official"])
            self.assertEqual(row["label"], PROXY_LABEL)
            self.assertEqual(row["certainty"], "carried_forward_inside_interval")
            self.assertEqual(row["anchor_as_of"], "2008-10-31")
            self.assertEqual(row["next_snapshot_as_of"], "2009-10-30")
            self.assertIsNotNone(row["days_from_anchor"])
            self.assertIsNotNone(row["days_to_next_snapshot"])


class ReplayTests(unittest.TestCase):
    def test_impact_proxy_decreasing_in_turnover(self):
        self.assertLess(impact_cost_proxy(100_000_000), impact_cost_proxy(1_000_000))
        self.assertEqual(impact_cost_proxy(0), float("inf"))

    def test_free_float_scales_with_frequency(self):
        self.assertLess(free_float_proxy(100, 0.5), free_float_proxy(100, 1.0))
        self.assertEqual(free_float_proxy(100, 1.0), 100.0)

    def test_rank_proxy_stable_and_turnover_ordered(self):
        ranked = rank_proxy([candidate("AAA", 10), candidate("BBB", 50), candidate("CCC", 50)])
        # BBB/CCC tie at 50: symbol asc breaks the tie deterministically.
        self.assertEqual([c["symbol"] for c in ranked], ["BBB", "CCC", "AAA"])

    def test_select_respects_screens_and_top_n(self):
        cfg = ProxyConfig(target_counts={"NIFTY50": 2, "NIFTY500": 500})
        cands = [candidate("AAA", 30), candidate("BBB", 20), candidate("CCC", 10),
                 candidate("DDD", 99, frequency_ok=False)]
        self.assertEqual(select_proxy(cands, cfg, "NIFTY50"), {"AAA", "BBB"})

    def test_precision_recall_math(self):
        scores = precision_recall({"A", "B", "C"}, {"B", "C", "D"})
        self.assertEqual((scores["tp"], scores["fp"], scores["fn"]), (2, 1, 1))
        self.assertAlmostEqual(scores["precision"], 2 / 3, places=4)
        self.assertAlmostEqual(scores["recall"], 2 / 3, places=4)
        self.assertAlmostEqual(scores["f1"], 2 / 3, places=4)

    def test_validate_replay_per_snapshot(self):
        predicted = {("NIFTY50", "2008-10-31"): {"AAA", "BBB", "ZZZ"}}
        rows = validate_replay(predicted, SNAPSHOTS)
        exact = next(r for r in rows if r["index_code"] == "NIFTY50" and r["as_of"] == "2008-10-31")
        self.assertEqual(exact["ground_truth"], 3)
        self.assertAlmostEqual(exact["recall"], 2 / 3, places=4)
        missing = next(r for r in rows if r["index_code"] == "NIFTY500")
        self.assertIsNone(missing["predicted"])  # never silently scored

    def test_config_fail_closed(self):
        with self.assertRaises(ValueError):
            ProxyConfig(min_price=0).validate()
        with self.assertRaises(ValueError):
            ProxyConfig(min_trading_frequency=1.5).validate()
        with self.assertRaises(ValueError):
            ProxyConfig(series="BE").validate()


class ChangelogTests(unittest.TestCase):
    def test_effective_date_point_in_time(self):
        events = [{"symbol": "XXX", "action": "add", "effective": "2010-01-15"},
                  {"symbol": "BBB", "action": "remove", "effective": "2010-06-15"}]
        before = apply_changelog({"AAA", "BBB"}, events, "2010-03-31")
        self.assertEqual(before, {"AAA", "BBB", "XXX"})
        after = apply_changelog({"AAA", "BBB"}, events, "2010-07-31")
        self.assertEqual(after, {"AAA", "XXX"})

    def test_malformed_event_fails_closed(self):
        with self.assertRaises(ValueError):
            apply_changelog({"AAA"}, [{"symbol": "BBB", "action": "swap",
                                       "effective": "2010-01-01"}], "2010-02-01")
        with self.assertRaises(ValueError):
            apply_changelog({"AAA"}, [{"symbol": "BBB", "action": "add",
                                       "effective": "not-a-date"}], "2010-02-01")

    def test_methodology_eras_cover_scope(self):
        joined = [e["era"] for e in METHODOLOGY_ERAS]
        self.assertTrue(any("2009" in e for e in joined))
        self.assertTrue(any("2016" in e for e in joined))
        self.assertEqual(METHODOLOGY_ERAS[0]["from"], "2000-01-01")


if __name__ == "__main__":
    unittest.main()
