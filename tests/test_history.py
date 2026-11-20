"""Small real DuckDB/parquet fixtures for the streaming diagnostic API."""
import csv
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb

from nse_pipeline.backtest.engine import CostConfig
from nse_pipeline.backtest.history import HistoryReadinessError, run_history


def sessions(n=200):
    result, on = [], date(2020, 1, 1)
    while len(result) < n:
        if on.weekday() < 5:
            result.append(on)
        on += timedelta(days=1)
    return result


def row(on, symbol="A", price=100.0, *, opn=None, adj=None, volume=10000,
        isin="INE000A", valid=True, previous=None):
    return (on, symbol, "EQ", isin, price if opn is None else opn, price,
            price if previous is None else previous, volume, valid,
            price if adj is None else adj)


def make_data(root, rows, splits=(), risks=(), dividends=(), identities=(),
              api_identities=()):
    adjusted = root / "processed/adjusted"
    adjusted.mkdir(parents=True)
    with duckdb.connect(str(root / "market.duckdb")) as db:
        db.execute("CREATE TABLE bars(date DATE,symbol VARCHAR,series VARCHAR,isin VARCHAR,"
                   "open DOUBLE,close DOUBLE,previous_close DOUBLE,volume BIGINT,valid_ohlcv BOOLEAN)")
        db.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?)", [r[:9] for r in rows])
        db.execute("CREATE TABLE adjusted(date DATE,symbol VARCHAR,series VARCHAR,adj_close DOUBLE)")
        db.executemany("INSERT INTO adjusted VALUES (?,?,?,?)", [(r[0], r[1], r[2], r[9]) for r in rows])
        db.execute("COPY adjusted TO ? (FORMAT PARQUET)", [str(adjusted / "adjusted_bars.parquet")])
        db.execute("CREATE TABLE audit(symbol VARCHAR,series VARCHAR,ex_date DATE,share_multiplier DOUBLE,decision VARCHAR)")
        if splits:
            db.executemany("INSERT INTO audit VALUES (?,?,?,?,?)", list(splits))
        db.execute("COPY audit TO ? (FORMAT PARQUET)", [str(adjusted / "action_audit.parquet")])
    for path, records in [
        (root / "processed/ledger/canonical-events.jsonl", risks),
        (root / "processed/actions_dividends/dividend_events.jsonl", dividends),
        (root / "processed/identity/identity_edges.jsonl", identities),
        (root / "processed/identity/api_identity_edges.jsonl", api_identities),
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def read_csv(path):
    with Path(path).open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


class HistoryTests(unittest.TestCase):
    def run_fixture(self, root, dates, **kwargs):
        return run_history(root, str(dates[0]), str(dates[-1]),
                           allow_incomplete=True, initial_cash=100000, top_n=1, **kwargs)

    def test_strict_gate_is_called_before_any_trading_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("nse_pipeline.backtest.history.build_readiness", return_value={
                "ready_for_backtesting": False, "blocker_codes": ["schemes", "dividends"],
            }) as gate:
                with self.assertRaises(HistoryReadinessError) as caught:
                    run_history(root)
                gate.assert_called_once()
                self.assertEqual(gate.call_args.kwargs["universe"], "research")
                self.assertIn("schemes", str(caught.exception))
                self.assertFalse((root / "processed/backtest/full_history_research/trades.csv").exists())

    def test_real_gate_blocks_fixture_and_does_not_overwrite_shared_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, dates = Path(tmp), sessions(2)
            make_data(root, [row(d) for d in dates])
            with self.assertRaises(HistoryReadinessError) as caught:
                run_history(root, str(dates[0]), str(dates[-1]))
            self.assertIn("corporate_actions", caught.exception.blocker_codes)
            self.assertFalse((root / "reports/full-history-readiness.json").exists())

    def test_open_sizing_causal_and_database_unchanged(self):
        dates = sessions(165)
        buys = []
        for future_close in (100.0, 900.0):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                # June 30 is the first month-end with 130 observations.
                rows = [row(d, price=future_close if d == date(2020, 7, 1) else 100,
                            opn=110 if d == date(2020, 7, 1) else 100,
                            previous=100) for d in dates]
                make_data(root, rows)
                before = (root / "market.duckdb").read_bytes()
                result = self.run_fixture(root, dates)
                trades = read_csv(result["outputs"]["trades"])
                first = next(t for t in trades if t["side"] == "buy")
                self.assertEqual(first["date"], "2020-07-01")
                self.assertEqual(first["signal_date"], "2020-06-30")
                buys.append((first["shares"], first["fill_price"], first["notional"]))
                n = float(first["notional"])
                self.assertLessEqual(n + CostConfig().cost_for(n, "buy"), 100000)
                self.assertTrue(all(float(t["cash_after"]) >= 0 for t in trades))
                self.assertEqual((root / "market.duckdb").read_bytes(), before)
                self.assertFalse(result["ready_for_backtesting"])
                self.assertFalse(result["total_return"])
        self.assertEqual(buys[0], buys[1])

    def test_raw_share_split_preserves_economic_value_and_applies_once(self):
        dates = sessions(155)
        ex = date(2020, 7, 10)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data(root, [row(d, price=100 if d < ex else 50, adj=50) for d in dates],
                      splits=[("A", "EQ", ex, 2.0, "applied")])
            result = self.run_fixture(root, dates)
            actions = read_csv(result["outputs"]["ledger"])
            self.assertEqual(len(actions), 1)
            action = actions[0]
            self.assertEqual(action["date"], str(ex))
            self.assertEqual(float(action["shares_after"]), float(action["shares_before"]) * 2)
            equity = {r["date"]: r for r in read_csv(result["outputs"]["equity_curve"])}
            self.assertAlmostEqual(float(equity[str(ex)]["equity"]), float(equity["2020-07-09"]["equity"]))
            self.assertEqual(float(next(t for t in read_csv(result["outputs"]["trades"]) if t["side"] == "buy")["fill_price"]), 100)

    def test_rebalance_with_existing_holding_uses_open_not_current_close(self):
        dates = sessions(165)
        execution_trades = []
        for future_close in (100, 900):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                rows = [row(d, price=future_close if d == date(2020, 8, 3) else 100,
                            opn=100, previous=100) for d in dates]
                rows += [row(d, "B", isin="INE000B", volume=1 if d == date(2020, 6, 30) else 10000)
                         for d in dates]
                make_data(root, rows)
                result = run_history(root, str(dates[0]), str(dates[-1]), allow_incomplete=True,
                                     initial_cash=100000, top_n=2)
                trades = [r for r in read_csv(result["outputs"]["trades"]) if r["date"] == "2020-08-03"]
                self.assertEqual([(r["symbol"], r["side"]) for r in trades], [("A", "sell"), ("B", "buy")])
                execution_trades.append([(r["symbol"], r["shares"], r["fill_price"]) for r in trades])
        self.assertEqual(*execution_trades)

    def test_no_future_listing_or_all_time_asset_class_exclusion(self):
        dates = sessions(165)
        rows = [row(d) for d in dates]
        rows.extend(row(d, "FUTURE", price=1000, isin="INEFUTURE") for d in dates if d >= date(2020, 7, 1))
        # A later fund-like ISIN for the same ticker must not exclude the
        # ticker from the earlier research universe, unlike the old SQL view.
        rows.append(row(dates[-1], "A", price=100, isin="INFFUND"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data(root, rows)
            result = self.run_fixture(root, dates)
            signals = read_csv(result["outputs"]["signals"])
            june = [r for r in signals if r["date"] == "2020-06-30"]
            self.assertEqual([r["symbol"] for r in june], ["A"])
            self.assertFalse(any(r["symbol"] == "FUTURE" for r in signals))

    def test_empty_month_end_target_sells_all_after_session_gap(self):
        dates = [d for d in sessions(180) if not date(2020, 8, 1) <= d <= date(2020, 8, 4)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data(root, [row(d, volume=1 if d == date(2020, 7, 31) else 10000) for d in dates])
            result = self.run_fixture(root, dates)
            trades = read_csv(result["outputs"]["trades"])
            sell = next(t for t in trades if t["side"] == "sell")
            self.assertEqual(sell["date"], "2020-08-05")
            self.assertEqual(sell["signal_date"], "2020-07-31")
            eq = {r["date"]: r for r in read_csv(result["outputs"]["equity_curve"])}
            self.assertEqual(eq["2020-08-05"]["n_positions"], "0")

    def test_missing_held_bar_never_fills_or_marks_at_stale_price(self):
        dates = sessions(155)
        absent = date(2020, 7, 10)
        rows = [row(d) for d in dates if d != absent]
        # Preserve the observed exchange session using another listing.
        rows.append(row(absent, "OTHER", isin="INEOTHER"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data(root, rows)
            result = self.run_fixture(root, dates)
            equity = {r["date"]: r for r in read_csv(result["outputs"]["equity_curve"])}
            self.assertEqual(equity[str(absent)]["equity"], "")
            self.assertEqual(equity[str(absent)]["positions_value"], "")
            self.assertEqual(equity[str(absent)]["unpriced_positions"], "1")
            self.assertEqual(result["stats"]["unpriced_equity_sessions"], 1)
            self.assertIsNone(result["stats"]["diagnostic_total_return"])

    def test_missing_exact_anchor_does_not_forward_fill_signal(self):
        dates = sessions(165)
        signal = date(2020, 7, 31)
        anchor = dates[dates.index(signal) - 63]
        rows = [row(d) for d in dates if d != anchor]
        rows.append(row(anchor, "OTHER", isin="INEOTHER"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data(root, rows)
            result = self.run_fixture(root, dates)
            june = [r for r in read_csv(result["outputs"]["signals"]) if r["date"] == str(signal)]
            self.assertEqual(june[0]["n_targets"], "0")

    def test_pre2011_full_queue_not_top100_is_exposure_audit_input(self):
        dates = sessions(165)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data(root, [row(d) for d in dates])
            queue = root / "processed/actions/pre2011_queue/queue-ranked-all.jsonl"
            queue.parent.mkdir(parents=True)
            queue.write_text(json.dumps({"symbol": "A", "date": "2020-07-10", "rank": 1001,
                                         "status": "unresolved_price_discontinuity_not_an_event"}) + "\n", encoding="utf-8")
            result = self.run_fixture(root, dates)
            self.assertEqual(result["input_risk_counts"]["pre2011_discontinuity"], 1)
            self.assertGreater(result["impact_counts"].get("holding:pre2011_discontinuity", 0), 0)
            self.assertGreater(result["impact_counts"].get("signal:pre2011_discontinuity", 0), 0)
            self.assertEqual(read_csv(result["outputs"]["ledger"]), [])

    def test_unknown_scheme_held_and_signal_impact_no_guessed_factor(self):
        dates = sessions(165)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data(root, [row(d) for d in dates], risks=[{
                "symbol": "A", "event_id": "UNKNOWN-SCHEME", "ex_date": "2020-07-10",
                "terms_verified": False,
            }])
            result = self.run_fixture(root, dates)
            impacts = read_csv(result["outputs"]["impact_audit"])
            self.assertTrue(any(r["scope"] == "holding" and r["event_id"] == "UNKNOWN-SCHEME" for r in impacts))
            self.assertTrue(any(r["scope"] == "signal" and r["event_id"] == "UNKNOWN-SCHEME" for r in impacts))
            self.assertEqual(read_csv(result["outputs"]["ledger"]), [])
            self.assertFalse(result["ready_for_backtesting"])

    def test_verified_raw_dividend_cash_only_previous_holders_receive_it(self):
        dates = sessions(155)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event = {"symbol": "A", "series": "EQ", "isin": "INE000A", "ex_date": "2020-07-10",
                     "included_in_factor": True, "dividend_per_share": 2.0,
                     "amount_verified": True, "ex_date_verified": True, "party_binding_verified": True,
                     "verified_by": "fixture", "source_url": "https://www.nseindia.com/fixture.pdf"}
            make_data(root, [row(d) for d in dates], dividends=[event])
            result = self.run_fixture(root, dates)
            credits = read_csv(result["outputs"]["ledger"])
            self.assertEqual(len(credits), 1)
            self.assertEqual(float(credits[0]["cash_amount"]), 2 * float(credits[0]["shares_before"]))
            self.assertEqual(float(credits[0]["shares_before"]), float(credits[0]["shares_after"]))
            self.assertFalse(result["total_return"])

    def test_unverified_included_dividend_factor_is_never_cash(self):
        dates = sessions(155)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data(root, [row(d) for d in dates], dividends=[{
                "symbol": "A", "series": "EQ", "ex_date": "2020-07-10", "included_in_factor": True,
                "dividend_per_share": 2, "dividend_factor": 0.98,
            }])
            result = self.run_fixture(root, dates)
            self.assertEqual(result["gross_dividend_cash"], 0)
            self.assertGreater(result["impact_counts"].get("holding:dividend", 0), 0)

    def test_dated_official_fallback_rename_keeps_raw_holding(self):
        dates = sessions(165)
        ex = date(2020, 7, 10)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data(root, [row(d, "A" if d < ex else "RENAMED", isin=None) for d in dates],
                      identities=[{"old_symbol": "A", "new_symbol": "RENAMED", "effective_date": str(ex),
                                   "verification": "official_symbolchange_row", "isin_old": None,
                                   "isin_new": None, "reuse_flags": {}}])
            result = self.run_fixture(root, dates)
            renamed = [r for r in read_csv(result["outputs"]["signals"]) if r["symbol"] == "RENAMED"]
            self.assertTrue(renamed)
            self.assertEqual(renamed[0]["key"], "SYMBOL:A")
            self.assertEqual(result["stats"]["unpriced_equity_sessions"], 0)
            self.assertFalse(result["ready_for_backtesting"])

    def test_isin_change_never_silently_transfers_old_raw_shares(self):
        dates = sessions(155)
        ex = date(2020, 7, 10)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data(root, [row(d, isin="INE000A" if d < ex else "INE000B") for d in dates])
            result = self.run_fixture(root, dates)
            position = read_csv(result["outputs"]["positions"])[0]
            self.assertEqual(position["key"], "ISIN:INE000A")
            self.assertEqual(position["blocked"], "True")
            self.assertIsNone(result["stats"]["final_equity"])
            self.assertGreater(result["impact_counts"].get("holding:identity", 0), 0)

    def test_api_same_isin_rename_carries_the_holding_over_one_to_one(self):
        # The same ISIN under both symbols proves the same instrument: the
        # successor's bars join the predecessor's key from the observed
        # trading handover, the holding continues and no key change blocks it.
        dates = sessions(165)
        ex = date(2020, 7, 10)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [row(d, "A" if d < ex else "RENAMED", isin=None) for d in dates]
            make_data(root, rows, api_identities=[{
                "edge_id": "API|A->RENAMED|2020-07-10",
                "old_symbol": "A", "new_symbol": "RENAMED",
                "company_name": "Acme Industries Limited",
                "company_name_normalised": "acme industries limited",
                "effective_date": str(ex),
                "evidence_class": "same_isin",
                "isin_old": "INE000A", "isin_new": "INE000A",
                "verification": "official_corporate_actions_api_rename",
                "bars_consistency": {"bars_old_last_date": "2020-07-09",
                                     "bars_new_first_date": str(ex),
                                     "bars_handover_ordering_ok": True},
            }])
            result = self.run_fixture(root, dates)
            renamed = [r for r in read_csv(result["outputs"]["signals"])
                       if r["symbol"] == "RENAMED"]
            self.assertTrue(renamed)
            self.assertEqual(renamed[0]["key"], "SYMBOL:A")
            # The holding was never blocked and the equity stayed known.
            self.assertEqual(result["stats"]["unpriced_equity_sessions"], 0)
            self.assertIsNotNone(result["stats"]["final_equity"])
            position = read_csv(result["outputs"]["positions"])[0]
            self.assertEqual(position["key"], "SYMBOL:A")
            self.assertEqual(position["blocked"], "False")

    def test_rei_agro_style_handover_never_silently_changes_the_share_count(self):
        # The successor continuity is proved (same company, split-bridged
        # ISIN) but the share exchange ratio at the handover is not
        # documented: the holding must NOT be carried over as if 1:1. Its
        # share count is unchanged, its equity stays unknown and the audit
        # names the reason.
        dates = sessions(165)
        ex = date(2020, 7, 10)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [row(d, "A" if d < ex else "RENAMED", isin=None) for d in dates]
            make_data(root, rows, api_identities=[{
                "edge_id": "API|A->RENAMED|2020-07-10",
                "old_symbol": "A", "new_symbol": "RENAMED",
                "company_name": "Acme Industries Limited",
                "company_name_normalised": "acme industries limited",
                "effective_date": str(ex),
                "evidence_class": "split_bridged",
                "isin_old": "INE000A", "isin_new": "INE000B",
                "verification": "official_corporate_actions_api_rename",
                # The documented 10:1 split is dated on the successor's side
                # of the handover, not at the handover itself.
                "split_bridge": {"ex_date": str(ex), "face_value_from": 10.0,
                                 "face_value_to": 1.0},
                "bars_consistency": {"bars_old_last_date": "2020-07-09",
                                     "bars_new_first_date": str(ex),
                                     "bars_handover_ordering_ok": True},
            }])
            result = self.run_fixture(root, dates)
            trades = read_csv(result["outputs"]["trades"])
            bought = next(t for t in trades if t["side"] == "buy")
            position = read_csv(result["outputs"]["positions"])[0]
            # The holding is still the predecessor's and its share count is
            # exactly what was bought: no 1:1 carry-over, no 10x multiplier.
            self.assertEqual(position["key"], "SYMBOL:A")
            self.assertEqual(position["symbol"], "A")
            self.assertEqual(float(position["shares"]), float(bought["shares"]))
            self.assertEqual(read_csv(result["outputs"]["ledger"]), [])
            # The equity of every session after the handover stays unknown.
            self.assertGreater(result["stats"]["unpriced_equity_sessions"], 0)
            self.assertIsNone(result["stats"]["final_equity"])
            # The audit names the reason instead of hiding it.
            impacts = read_csv(result["outputs"]["impact_audit"])
            self.assertTrue(any(
                r["reason"] == "successor_continuity_proved_but_exchange_ratio_undocumented"
                for r in impacts), sorted({r["reason"] for r in impacts}))
            self.assertTrue(any(
                r["reason"] == "successor_continuity_proved_but_exchange_ratio_undocumented"
                for r in impacts if r["scope"] == "holding"))

    def test_duplicate_day_is_quarantined_not_arbitrarily_priced(self):
        dates = sessions(155)
        duplicate = date(2020, 7, 10)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data(root, [row(d) for d in dates] + [row(duplicate, price=110)])
            result = self.run_fixture(root, dates)
            eq = {r["date"]: r for r in read_csv(result["outputs"]["equity_curve"])}
            self.assertEqual(eq[str(duplicate)]["equity"], "")
            self.assertTrue(any(r["reason"] == "duplicate_security_session_quarantined"
                                for r in read_csv(result["outputs"]["impact_audit"])))

    def test_terminal_missing_price_not_cash_or_zero_and_provenance_hashed(self):
        dates = sessions(155)
        rows = [row(d) for d in dates[:-1]] + [row(dates[-1], "OTHER", isin="INEOTHER")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data(root, rows)
            result = self.run_fixture(root, dates)
            self.assertIsNone(result["stats"]["final_equity"])
            self.assertEqual(result["stats"]["n_positions"], 1)
            positions = read_csv(result["outputs"]["positions"])
            self.assertEqual(positions[0]["terminal_price_known"], "False")
            self.assertTrue(all(len(p["sha256"]) == 64 for p in result["provenance"]["inputs"]))
            self.assertTrue(result["provenance"]["database_read_only"])


if __name__ == "__main__":
    unittest.main()
