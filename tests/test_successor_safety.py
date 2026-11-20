"""Production execution gates; legacy synthetic examples are not evidence."""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.backtest import successor
from nse_pipeline.backtest.total_return import TotalReturnBacktester


DAY = date(2024, 1, 10)


def execution_entry():
    return {
        "scheme_id": "VERIFIED", "scheme_type": "demerger", "status": "effective",
        "parties": [
            {"symbol": "B", "role": "resulting_company"},
            {"symbol": "A", "role": "demerged_company"},
        ],
        "dates": {"ex_date": DAY.isoformat()},
        "ratio_num": 1, "ratio_den": 2, "cash_per_share": None,
        "successor_symbol": "B", "terms_verified": True,
        "pdf_url": "https://nsearchives.nseindia.com/terms.pdf", "page_ref": "p.2",
        "verified_by": "reader", "extraction_source": "manual filing reading",
        "execution": {
            "ex_date": DAY.isoformat(), "ex_date_verified": True, "exchange": "NSE",
            "source_url": "https://nsearchives.nseindia.com/ex-date.pdf",
            "page_ref": "p.1", "verified_by": "reader",
            "party_binding_verified": True, "source_symbol": "A", "source_key": "OLD-A",
            "successor_symbol": "B", "successor_key": "NEW-B",
            "successor_tradable_date": DAY.isoformat(),
        },
    }


class ExecutionValidationTests(unittest.TestCase):
    def test_coverage_does_not_label_legacy_terms_production_actionable(self):
        entry = execution_entry()
        entry.pop("execution")
        coverage = successor.build_coverage([entry])
        self.assertEqual(coverage["actionable"], 0)
        self.assertEqual(coverage["unresolved"], 1)

    def test_record_board_and_effective_dates_are_not_execution_dates(self):
        entry = execution_entry()
        entry["dates"] = {"board_date": "2023-10-01", "record_date": DAY.isoformat()}
        entry.pop("execution")
        with self.assertRaisesRegex(ValueError, "ex.date"):
            successor.validate_execution_entry(entry)

    def test_party_binding_is_verified_and_not_first_party_order(self):
        entry = execution_entry()
        self.assertEqual(successor.validate_execution_entry(entry)["source_key"], "OLD-A")
        entry["execution"]["source_symbol"] = "B"
        with self.assertRaisesRegex(ValueError, "party|source"):
            successor.validate_execution_entry(entry)

    def test_production_terms_are_finite_numbers_not_nan_infinity_or_booleans(self):
        for value in (float("nan"), float("inf"), True):
            with self.subTest(value=value):
                entry = execution_entry()
                entry["ratio_num"] = value
                with self.assertRaisesRegex(ValueError, "finite|number"):
                    successor.validate_execution_entry(entry)


class ExposureTests(unittest.TestCase):
    def test_held_unresolved_risk_is_not_cured_by_five_session_exclusion(self):
        entry = execution_entry()
        entry["terms_verified"] = False
        # Source is SECOND in parties. Holdings must still be found.
        assessment = successor.assess_exposure(
            [entry], [{"key": "OLD-A", "symbol": "A", "shares": 10}],
            DAY + timedelta(days=30), lookback_start=DAY - timedelta(days=63))
        self.assertFalse(assessment["safe"])
        self.assertTrue(assessment["issues"])
        self.assertIn("OLD-A", assessment["signal_exclusions"])

    def test_quarantine_covers_momentum_anchor_without_erasing_preevent_history(self):
        entry = execution_entry()
        before = successor.assess_exposure([entry], [], DAY - timedelta(days=1))
        contaminated = successor.assess_exposure(
            [entry], [], DAY + timedelta(days=30), lookback_start=DAY - timedelta(days=1))
        clean_anchor = successor.assess_exposure(
            [entry], [], DAY + timedelta(days=100), lookback_start=DAY)
        self.assertEqual(before["signal_exclusions"], [])
        self.assertIn("OLD-A", contaminated["signal_exclusions"])
        self.assertEqual(clean_anchor["signal_exclusions"], [])

    def test_split_adjusted_share_counts_and_total_return_are_not_supported(self):
        entry = execution_entry()
        holdings = [{"key": "OLD-A", "symbol": "A", "shares": 10}]
        for options, reason in (({"price_basis": "split_adjusted"}, "split-adjusted"),
                                ({"return_mode": "total_return"}, "total_return")):
            with self.subTest(options=options):
                assessment = successor.assess_exposure([entry], holdings, DAY, **options)
                self.assertFalse(assessment["safe"])
                self.assertIn(reason, assessment["issues"][0]["reason"])


def bars_for(dates, symbols=("A", "B")):
    return pd.DataFrame([
        {"date": d, "key": s, "symbol": s, "series": "EQ", "open": 100.,
         "high": 101., "low": 99., "close": 100., "total_close": 100.,
         "previous_close": 100., "volume": 100000, "valid_ohlcv": True}
        for d in dates for s in symbols
    ])


class OpeningCausalityTests(unittest.TestCase):
    def test_execution_close_cannot_change_opening_orders_in_either_engine(self):
        dates = [DAY + timedelta(days=i) for i in range(3)]
        bars = bars_for(dates)
        changed = bars.copy()
        changed.loc[(changed["date"] == dates[2]) & (changed["key"] == "A"),
                    ["close", "low", "total_close"]] = [10., 9., 10.]
        signal = lambda e, b, d: ["A", "MISSING"] if d == dates[0] else ["A", "B"]
        for engine in (successor.SuccessorBacktester, TotalReturnBacktester):
            with self.subTest(engine=engine.__name__):
                before = engine().run(bars, dates[:2], signal, initial_cash=100000)
                after = engine().run(changed, dates[:2], signal, initial_cash=100000)
                pd.testing.assert_frame_equal(before["trades"], after["trades"])

    def test_legacy_swap_fraction_uses_known_open_not_future_close(self):
        dates = [DAY - timedelta(days=2), DAY - timedelta(days=1), DAY]
        entry = execution_entry()
        entry.pop("execution")
        entry["parties"] = [{"symbol": "A", "role": "demerged_company"}]
        for close in (1., 10000.):
            bars = bars_for(dates)
            bars.loc[(bars["date"] == DAY) & (bars["symbol"] == "B"), "close"] = close
            result = successor.SuccessorBacktester().run(
                bars, [dates[0]], lambda e, b, d: ["A"], initial_cash=600,
                successor_entries=[entry])
            self.assertEqual(result["corporate_actions"].iloc[0]["cash_credited"], 50.)
            self.assertEqual(result["quality"]["execution_mode"], "synthetic_legacy")
            self.assertFalse(result["quality"]["production_safe"])


class ProductionRunnerTests(unittest.TestCase):
    def test_raw_claim_is_rejected_when_preserved_raw_open_disagrees(self):
        bars = bars_for([DAY])
        bars["raw_open"] = 200.
        with self.assertRaisesRegex(ValueError, "raw_open|price basis"):
            successor.SuccessorBacktester().run(
                bars, [], lambda e, b, d: [], production=True, price_basis="raw")

    def test_verified_cash_consideration_is_not_immediately_spendable_on_ex_date(self):
        entry = execution_entry()
        entry["scheme_type"] = "cash_out"
        entry["parties"] = [{"symbol": "A", "role": "target"}]
        entry["ratio_num"] = entry["ratio_den"] = entry["successor_symbol"] = None
        entry["cash_per_share"] = 150.
        result = successor.assess_exposure(
            [entry], [{"key": "OLD-A", "symbol": "A", "shares": 10}], DAY)
        self.assertFalse(result["safe"])
        self.assertIn("no immediate cash credit", result["issues"][0]["reason"])

    def test_duplicate_scheme_ids_cannot_skip_a_second_held_event(self):
        entry = execution_entry()
        with self.assertRaisesRegex(ValueError, "[Dd]uplicate scheme"):
            successor.SuccessorBacktester().run(
                bars_for([DAY]), [], lambda e, b, d: [],
                successor_entries=[entry, entry.copy()], production=True, price_basis="raw")

    def test_same_session_chain_cannot_depend_on_ledger_row_order(self):
        first = execution_entry()
        second = execution_entry()
        second["scheme_id"] = "SECOND"
        second["parties"] = [{"symbol": "B", "role": "demerged_company"},
                              {"symbol": "C", "role": "resulting_company"}]
        second["successor_symbol"] = "C"
        second["execution"].update(source_symbol="B", source_key="NEW-B",
                                    successor_symbol="C", successor_key="NEW-C")
        with self.assertRaisesRegex(ValueError, "[Ss]ame.session"):
            successor.SuccessorBacktester().run(
                bars_for([DAY]), [], lambda e, b, d: [],
                successor_entries=[first, second], production=True, price_basis="raw")

    def test_excluded_unresolved_target_does_not_make_coverage_production_safe(self):
        dates = [DAY, DAY + timedelta(days=1)]
        entry = execution_entry()
        entry["terms_verified"] = False
        result = successor.SuccessorBacktester().run(
            bars_for(dates), [dates[0]], lambda e, b, d: ["A", "B"],
            successor_entries=[entry], production=True, price_basis="raw")
        self.assertFalse(result["quality"]["production_safe"])
        self.assertEqual(result["quality"]["unresolved_entries"], 1)
        self.assertTrue(result["quality"]["signal_quarantines"])
        self.assertNotIn("A", {p["key"] for p in result["positions"]})

    def test_undated_scheme_stops_a_new_holding_instead_of_dropping_all_history(self):
        dates = [DAY, DAY + timedelta(days=1)]
        entry = execution_entry()
        entry["dates"] = {}
        entry.pop("execution")
        with self.assertRaises(successor.SchemeSafetyError) as caught:
            successor.SuccessorBacktester().run(
                bars_for(dates), [dates[0]], lambda e, b, d: ["A"],
                successor_entries=[entry], production=True, price_basis="raw")
        self.assertEqual(caught.exception.assessment["on"], dates[1])
        self.assertEqual(caught.exception.assessment["holdings"][0]["key"], "A")

    def test_unresolved_existing_holding_stops_before_event_mark_or_sale(self):
        dates = [DAY - timedelta(days=2), DAY - timedelta(days=1), DAY]
        bars = bars_for(dates)
        entry = execution_entry()
        entry["terms_verified"] = False
        with self.assertRaises(successor.SchemeSafetyError) as caught:
            successor.SuccessorBacktester().run(
                bars, dates[:2], lambda e, b, d: ["A"] if d == dates[0] else ["B"],
                successor_entries=[entry], production=True, price_basis="raw")
        self.assertEqual(caught.exception.assessment["on"], DAY)
        self.assertEqual(caught.exception.assessment["holdings"][0]["key"], "A")
        self.assertTrue(caught.exception.assessment["holdings"][0]["shares"] > 0)

    def test_total_return_cannot_silently_ignore_held_scheme(self):
        dates = [DAY - timedelta(days=2), DAY - timedelta(days=1), DAY]
        entry = execution_entry()
        entry["terms_verified"] = False
        with self.assertRaisesRegex(successor.SchemeSafetyError, "exposure"):
            TotalReturnBacktester().run(
                bars_for(dates), [dates[0]], lambda e, b, d: ["A"],
                successor_entries=[entry])

    def test_total_return_verified_binding_mismatch_does_not_hide_held_party(self):
        dates = [DAY - timedelta(days=2), DAY - timedelta(days=1), DAY]
        with self.assertRaises(successor.SchemeSafetyError):
            TotalReturnBacktester().run(
                bars_for(dates), [dates[0]], lambda e, b, d: ["A"],
                successor_entries=[execution_entry()])

    def test_verified_successor_key_must_identify_the_bound_party_on_event_date(self):
        dates = [DAY - timedelta(days=2), DAY - timedelta(days=1), DAY]
        bars = bars_for(dates)
        bars["key"] = bars["key"].replace({"A": "OLD-A", "B": "NEW-B"})
        bars.loc[(bars["date"] == DAY) & (bars["key"] == "NEW-B"), "symbol"] = "OTHER"
        entry = execution_entry()
        entry["ratio_den"] = 1
        with self.assertRaisesRegex(successor.SchemeSafetyError, "binding"):
            successor.SuccessorBacktester().run(
                bars, [dates[0]], lambda e, b, d: ["OLD-A"], initial_cash=1000,
                successor_entries=[entry], production=True, price_basis="raw")

    def test_no_position_event_does_not_waive_binding_check_for_new_opening_buy(self):
        dates = [DAY - timedelta(days=2), DAY - timedelta(days=1), DAY]
        entry = execution_entry()
        entry["ratio_den"] = 1
        with self.assertRaisesRegex(successor.SchemeSafetyError, "binding"):
            successor.SuccessorBacktester().run(
                bars_for(dates), dates[:2], lambda e, b, d: ["B"] if d == dates[0] else ["A"],
                initial_cash=1000, successor_entries=[entry], production=True, price_basis="raw")

    def test_verified_whole_share_demerger_uses_event_key_not_first_symbol_history(self):
        dates = [DAY - timedelta(days=2), DAY - timedelta(days=1), DAY]
        bars = bars_for(dates)
        bars["key"] = bars["key"].replace({"A": "OLD-A", "B": "NEW-B"})
        ancient = bars_for([DAY - timedelta(days=100)], ("A",))
        ancient["key"] = "ANCIENT-A"
        bars = pd.concat([ancient, bars], ignore_index=True)
        entry = execution_entry()
        entry["ratio_den"] = 1
        result = successor.SuccessorBacktester().run(
            bars, [dates[0]], lambda e, b, d: ["OLD-A"], initial_cash=1000,
            successor_entries=[entry], production=True, price_basis="raw")
        held = {p["key"]: p["shares"] for p in result["positions"]}
        self.assertEqual(held, {"OLD-A": 9, "NEW-B": 9})
        self.assertTrue(result["quality"]["production_safe"])

    def test_unlisted_child_is_pending_not_zero_valued_or_tradable(self):
        dates = [DAY - timedelta(days=2), DAY - timedelta(days=1), DAY, DAY + timedelta(days=1)]
        bars = bars_for(dates)
        bars = bars[(bars["symbol"] == "A") | (bars["date"] > DAY)].copy()
        bars["key"] = bars["key"].replace({"A": "OLD-A", "B": "NEW-B"})
        entry = execution_entry()
        entry["ratio_den"] = 1
        entry["execution"]["successor_tradable_date"] = dates[-1].isoformat()
        with self.assertRaises(successor.SchemeSafetyError) as caught:
            successor.SuccessorBacktester().run(
                bars, [dates[0]], lambda e, b, d: ["OLD-A"], initial_cash=1000,
                successor_entries=[entry], production=True, price_basis="raw")
        pending = caught.exception.pending_entitlements[0]
        self.assertEqual(pending["shares"], 9)
        self.assertFalse(pending["tradable"])
        self.assertIsNone(pending["market_value"])
        self.assertEqual(pending["cash_credited"], 0)
        self.assertEqual({p["key"] for p in caught.exception.assessment["holdings"]}, {"OLD-A"})

    def test_fraction_is_not_paid_at_execution_day_future_close(self):
        dates = [DAY - timedelta(days=2), DAY - timedelta(days=1), DAY]
        bars = bars_for(dates)
        bars["key"] = bars["key"].replace({"A": "OLD-A", "B": "NEW-B"})
        entry = execution_entry()
        for close in (1., 10000.):
            bars.loc[(bars["date"] == DAY) & (bars["symbol"] == "B"), "close"] = close
            with self.assertRaises(successor.SchemeSafetyError) as caught:
                successor.SuccessorBacktester().run(
                    bars, [dates[0]], lambda e, b, d: ["OLD-A"], initial_cash=600,
                    successor_entries=[entry], production=True, price_basis="raw")
            pending = caught.exception.pending_entitlements[0]
            self.assertEqual(pending["fractional_shares"], .5)
            self.assertEqual(pending["cash_credited"], 0)

if __name__ == "__main__":
    unittest.main()
