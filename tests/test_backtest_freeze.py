"""Tests for the backtest freeze diagnostic.

The diagnostic explains why the 2000-2026 momentum backtest stops trading.
These tests prove the *measurement* properties of that explanation on a small
synthetic fixture: that the zero-trade streak is detected correctly, that the
mechanism attribution buckets are assigned the way the engine actually behaves,
that the per-year known-equity share is computed correctly, and that the whole
diagnostic is a deterministic function of its inputs.

They never assert that the strategy works.  A freeze is a data/model
limitation, and this module exists to quantify it, not to excuse it.
"""
from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.backtest.freeze import (  # noqa: E402
    BUCKET_FROZEN,
    BUCKET_NO_ELIGIBLE,
    BUCKET_NOT_EXECUTABLE,
    BUCKET_ORDERS,
    BUCKET_QUARANTINED,
    attribute_execution_sessions,
    build_diagnostic,
    execution_sessions_for,
    known_equity_by_year,
    longest_zero_trade_streak,
    render_markdown,
    write_outputs,
)

VARIANT_A = "T-L21-N10-audit_only"
VARIANT_B = "T-L21-N10-quarantine_around_events"

#: The first session of the fixture.
FIRST_SESSION = date(2000, 1, 3)
#: The session on which the only holding stops being priceable.
FREEZE_SESSION = date(2000, 4, 3)
#: The session on which the strategy finally trades.
FIRST_TRADE_SESSION = date(2000, 3, 1)
#: The single security whose disappearance freezes the book.
FROZEN_SYMBOL = "FROZENCO"
#: Sessions on which the book's equity is knowable.
KNOWN_SESSIONS = 65
#: Sessions on which the book holds an unpriceable position.
UNKNOWN_SESSIONS = 10


def weekday_sessions(start: date, count: int) -> list[date]:
    """``count`` consecutive weekday sessions starting at ``start``."""
    sessions: list[date] = []
    on = start
    while len(sessions) < count:
        if on.weekday() < 5:
            sessions.append(on)
        on += timedelta(days=1)
    return sessions


#: 75 weekday sessions: 2000-01-03 .. 2000-04-14.
#: Month ends are 2000-01-31, 2000-02-29 and 2000-03-31, so the engine's
#: execution sessions are 2000-02-01, 2000-03-01 and 2000-04-03.
FIXTURE_SESSIONS = weekday_sessions(FIRST_SESSION, 75)
FIXTURE_EXECUTIONS = [date(2000, 2, 1), date(2000, 3, 1), date(2000, 4, 3)]


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in columns})


def build_fixture(root: Path) -> Path:
    """Write a synthetic sensitivity run that reproduces the freeze.

    The engine rebalances at the last session of a month and executes at the
    open of the next session, so the fixture has three execution sessions:
    2000-02-01, 2000-03-01 and 2000-04-03.

    * 2000-02-01 is a warm-up: the book is empty and the momentum rank cannot
      yet name anything, so no order is produced.
    * 2000-03-01 buys the single security the book will hold.
    * 2000-04-03 that security has no bar in the date-local research
      universe, so the held opening mark is unknown: every buy is skipped, the
      holding can never be sold, and the equity of every later session is
      unknown.

    Variant B differs in one respect: at the warm-up session a target is
    refused by the quarantine policy.  A quarantine refusal is a more specific
    cause than 'no eligible names', so the attribution must prefer it.
    """
    sensitivity = root / "processed" / "backtest" / "sensitivity"
    reports = root / "reports"

    marks: list[dict] = []
    trades: list[dict] = []
    for variant in (VARIANT_A, VARIANT_B):
        for session in FIXTURE_SESSIONS:
            frozen = session >= FREEZE_SESSION
            held = session >= FIRST_TRADE_SESSION
            marks.append({
                "variant_id": variant,
                "date": session.isoformat(),
                "cash": "1000000.0",
                "positions_value_known": "10000.0" if held and not frozen else "0.0",
                "equity_strict": "" if frozen else "1000000.0",
                "equity_last_observed_close_marking_ASSUMPTION": "" if frozen else "1000000.0",
                "equity_status": "unknown_unpriced_holding" if frozen else "known",
                "n_positions": "1" if held else "0",
                "unpriced_positions": "1" if frozen else "0",
                "unpriced_shares": "100" if frozen else "0",
                "unpriced_symbols": FROZEN_SYMBOL if frozen else "",
            })
        trades.append({
            "variant_id": variant,
            "date": FIRST_TRADE_SESSION.isoformat(),
            "signal_date": "2000-02-29",
            "key": f"SYMBOL:{FROZEN_SYMBOL}",
            "symbol": FROZEN_SYMBOL,
            "side": "buy",
            "fill_price": "100.0",
            "shares": "100",
            "notional": "10000.0",
            "cost": "20.0",
            "cash_after": "999980.0",
        })

    impact = [
        {
            "variant_id": VARIANT_B,
            "date": "2000-02-01",
            "scope": "order",
            "kind": "execution",
            "key": "SYMBOL:XYZ",
            "symbol": "XYZ",
            "event_id": "buy:2000-02-01:SYMBOL:XYZ",
            "reason": "buy_quarantined_unresolved_event",
        },
    ]

    unknown_year = []
    for variant in (VARIANT_A, VARIANT_B):
        unknown_year.append({
            "variant_id": variant,
            "lookback_sessions": "21",
            "top_n": "10",
            "policy": "audit_only" if variant == VARIANT_A else "quarantine_around_events",
            "year": "2000",
            "sessions": str(len(FIXTURE_SESSIONS)),
            "sessions_with_unknown_positions": str(UNKNOWN_SESSIONS),
            "sessions_all_positions_marked": str(KNOWN_SESSIONS),
            "unknown_position_instances": str(UNKNOWN_SESSIONS),
            "unknown_reason_no_bar_in_date_local_eq_research_universe":
                str(UNKNOWN_SESSIONS),
            "unknown_reason_no_valid_close_for_session": "0",
            "unknown_reason_position_blocked_unverified_identity_change": "0",
            "unknown_reason_quarantined_unresolved_event": "0",
        })

    variants = []
    for variant, policy, scheduled in ((VARIANT_A, "audit_only", 2),
                                       (VARIANT_B, "quarantine_around_events", 3)):
        skip_counts = {
            "buy_unknown_held_opening_value": 1,
            "sell_not_executable": 1,
        }
        if policy == "quarantine_around_events":
            skip_counts["buy_quarantined_unresolved_event"] = 1
        variants.append({
            "variant_id": variant,
            "lookback_sessions": 21,
            "top_n": 10,
            "policy": policy,
            "sessions": len(FIXTURE_SESSIONS),
            "rebalances": 3,
            "signals_scheduled": scheduled,
            "trades": 1,
            "buys": 1,
            "sells": 0,
            "skips_total": sum(skip_counts.values()),
            "skip_counts": skip_counts,
            "sessions_with_all_positions_marked": KNOWN_SESSIONS,
            "sessions_with_unknown_positions": UNKNOWN_SESSIONS,
            "unpriced_equity_sessions": UNKNOWN_SESSIONS,
            "unknown_position_instances": UNKNOWN_SESSIONS,
            "position_days_marked_known": KNOWN_SESSIONS,
            "position_days_unknown": UNKNOWN_SESSIONS,
            "sessions_with_unknown_opening_mark": 1,
            "sessions_where_no_total_could_be_published": UNKNOWN_SESSIONS,
            "unpriced_reason_counts": {
                "no_bar_in_date_local_eq_research_universe": UNKNOWN_SESSIONS,
                "no_valid_close_for_session": 0,
            },
            "unpriced_shares_total": UNKNOWN_SESSIONS * 100,
            "max_unpriced_positions_in_session": 1,
            "final_cash": 999980.0,
            "total_costs": 20.0,
            "final_equity_strict": None,
            "terminal_positions": 1,
            "first_trade": FIRST_TRADE_SESSION.isoformat(),
            "last_trade": FIRST_TRADE_SESSION.isoformat(),
        })

    summary = {
        "schema_version": 1,
        "mode": "test_fixture",
        "start": FIXTURE_SESSIONS[0].isoformat(),
        "end": FIXTURE_SESSIONS[-1].isoformat(),
        "ready_for_backtesting": False,
        "ready_for_alpha_claim": False,
        "variants": variants,
    }

    comparison_columns = [
        "variant_id", "lookback_sessions", "top_n", "policy", "sessions",
        "rebalances", "trades", "buys", "sells", "skips_total",
        "sessions_with_all_positions_marked", "sessions_with_unknown_positions",
        "unpriced_equity_sessions", "sessions_with_unknown_opening_mark",
        "final_equity_strict", "terminal_positions", "first_trade", "last_trade",
    ]
    comparison = []
    for variant in variants:
        row = {key: variant.get(key, "") for key in comparison_columns}
        row["variant_id"] = variant["variant_id"]
        comparison.append(row)

    _write_csv(sensitivity / "equity_marks.csv",
               ["variant_id", "date", "cash", "positions_value_known", "equity_strict",
                "equity_last_observed_close_marking_ASSUMPTION", "equity_status",
                "n_positions", "unpriced_positions", "unpriced_shares",
                "unpriced_symbols"], marks)
    _write_csv(sensitivity / "trades.csv",
               ["variant_id", "date", "signal_date", "key", "symbol", "side",
                "fill_price", "shares", "notional", "cost", "cash_after"], trades)
    _write_csv(sensitivity / "impact_audit.csv",
               ["variant_id", "date", "scope", "kind", "key", "symbol", "event_id",
                "reason"], impact)
    _write_csv(sensitivity / "unknown_coverage_by_year.csv",
               list(unknown_year[0].keys()), unknown_year)
    _write_csv(sensitivity / "comparison.csv", comparison_columns, comparison)
    (sensitivity / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")

    reports.mkdir(parents=True, exist_ok=True)
    (reports / "identity-coverage.json").write_text(json.dumps({
        "terminal_proceeds": {
            "delisted_rows": 3,
            "proceeds_resolved": 0,
            "proceeds_unresolved": 3,
        },
    }, indent=2), encoding="utf-8")
    return root


class ZeroTradeStreakTests(unittest.TestCase):
    def test_streak_is_the_longest_consecutive_run_without_a_trade(self):
        sessions = [date(2020, 1, 1) + timedelta(days=i) for i in range(7)]
        streak = longest_zero_trade_streak(sessions, {sessions[2], sessions[3]})
        # Two trading sessions split the window into a 2-session and a
        # 3-session run; the later, longer one wins.
        self.assertEqual(streak.sessions, 3)
        self.assertEqual(streak.start, date(2020, 1, 5))
        self.assertEqual(streak.end, date(2020, 1, 7))

    def test_a_gap_between_sessions_does_not_break_a_streak(self):
        """A weekend or a holiday is not a trade: the streak continues."""
        sessions = [date(2020, 1, 3), date(2020, 1, 6), date(2020, 1, 7)]
        streak = longest_zero_trade_streak(sessions, set())
        self.assertEqual(streak.sessions, 3)
        self.assertEqual(streak.start, date(2020, 1, 3))
        self.assertEqual(streak.end, date(2020, 1, 7))

    def test_no_trades_at_all_means_one_streak_over_the_whole_window(self):
        sessions = [date(2020, 1, 1) + timedelta(days=i) for i in range(10)]
        streak = longest_zero_trade_streak(sessions, set())
        self.assertEqual(streak.sessions, 10)
        self.assertEqual(streak.start, sessions[0])
        self.assertEqual(streak.end, sessions[-1])

    def test_trading_every_session_means_no_streak(self):
        sessions = [date(2020, 1, 1) + timedelta(days=i) for i in range(5)]
        streak = longest_zero_trade_streak(sessions, set(sessions))
        self.assertEqual(streak.sessions, 0)
        self.assertIsNone(streak.start)
        self.assertIsNone(streak.end)

    def test_trades_outside_the_calendar_are_ignored(self):
        sessions = [date(2020, 1, 1), date(2020, 1, 2)]
        streak = longest_zero_trade_streak(sessions, {date(2021, 5, 5)})
        self.assertEqual(streak.sessions, 2)


class ExecutionSessionTests(unittest.TestCase):
    def test_execution_follows_the_last_session_of_every_month(self):
        sessions = weekday_sessions(date(2020, 1, 1), 70)
        executions = execution_sessions_for(sessions)
        month_ends = []
        for session in sessions:
            if not month_ends or month_ends[-1].month != session.month:
                month_ends.append(session)
            else:
                month_ends[-1] = session
        expected = []
        index = {session: i for i, session in enumerate(sessions)}
        for month_end in month_ends:
            i = index[month_end]
            if i + 1 < len(sessions):
                expected.append(sessions[i + 1])
        self.assertEqual(executions, expected)
        self.assertEqual(len(executions), len(month_ends) - 1)

    def test_the_final_month_end_never_executes(self):
        sessions = weekday_sessions(date(2020, 1, 1), 70)
        executions = execution_sessions_for(sessions)
        self.assertNotIn(sessions[-1], executions)


class MechanismAttributionTests(unittest.TestCase):
    def test_a_session_with_orders_is_counted_as_orders_filled(self):
        sessions = [date(2020, 1, 1), date(2020, 1, 2)]
        counts = attribute_execution_sessions(
            sessions, {date(2020, 1, 1): 3}, {date(2020, 1, 1): 0})
        self.assertEqual(counts[BUCKET_ORDERS], 1)

    def test_an_unpriceable_holding_freezes_the_whole_book(self):
        sessions = [date(2020, 1, 1), date(2020, 1, 2)]
        counts = attribute_execution_sessions(
            sessions, {}, {date(2020, 1, 1): 1, date(2020, 1, 2): 1})
        self.assertEqual(counts[BUCKET_FROZEN], 2)
        self.assertNotIn(BUCKET_ORDERS, counts)

    def test_not_executable_and_quarantined_are_their_own_buckets(self):
        sessions = [date(2020, 1, 1), date(2020, 1, 2)]
        attested = {
            date(2020, 1, 1): frozenset({"buy_missing_open_or_not_executable"}),
            date(2020, 1, 2): frozenset({"buy_quarantined_unresolved_event"}),
        }
        counts = attribute_execution_sessions(sessions, {}, {}, attested)
        self.assertEqual(counts[BUCKET_NOT_EXECUTABLE], 1)
        self.assertEqual(counts[BUCKET_QUARANTINED], 1)

    def test_a_book_with_nothing_to_buy_has_no_eligible_names(self):
        sessions = [date(2020, 1, 1)]
        counts = attribute_execution_sessions(sessions, {}, {date(2020, 1, 1): 0})
        self.assertEqual(counts[BUCKET_NO_ELIGIBLE], 1)

    def test_an_attested_market_refusal_outranks_the_freeze(self):
        """A not-executable order is a market fact; it is not a data gap."""
        sessions = [date(2020, 1, 1)]
        attested = {date(2020, 1, 1): frozenset({"buy_missing_open_or_not_executable"})}
        counts = attribute_execution_sessions(
            sessions, {}, {date(2020, 1, 1): 1}, attested)
        self.assertEqual(counts[BUCKET_NOT_EXECUTABLE], 1)
        self.assertNotIn(BUCKET_FROZEN, counts)

    def test_buckets_are_disjoint_and_sum_to_the_execution_sessions(self):
        sessions = [date(2020, 1, 1) + timedelta(days=i) for i in range(6)]
        counts = attribute_execution_sessions(
            sessions,
            {sessions[0]: 2},
            {sessions[1]: 1, sessions[2]: 1, sessions[4]: 0},
            {sessions[3]: frozenset({"buy_quarantined_unresolved_event"})},
        )
        self.assertEqual(sum(counts.values()), len(sessions))
        self.assertEqual(counts[BUCKET_ORDERS], 1)
        self.assertEqual(counts[BUCKET_FROZEN], 2)
        self.assertEqual(counts[BUCKET_QUARANTINED], 1)
        self.assertEqual(counts[BUCKET_NO_ELIGIBLE], 2)


class KnownEquityByYearTests(unittest.TestCase):
    def test_known_share_counts_sessions_with_no_unpriceable_position(self):
        rows = [
            {"year": 2020, "unpriced_positions": 0},
            {"year": 2020, "unpriced_positions": 1},
            {"year": 2020, "unpriced_positions": 0},
            {"year": 2021, "unpriced_positions": 0},
            {"year": 2021, "unpriced_positions": 2},
            {"year": 2021, "unpriced_positions": 3},
        ]
        years = known_equity_by_year(rows)
        self.assertEqual([row["year"] for row in years], [2020, 2021])
        self.assertEqual(years[0]["sessions"], 3)
        self.assertEqual(years[0]["known_equity_sessions"], 2)
        self.assertEqual(years[0]["unknown_equity_sessions"], 1)
        self.assertAlmostEqual(years[0]["known_equity_share"], 2 / 3, places=6)
        self.assertEqual(years[1]["known_equity_sessions"], 1)
        self.assertAlmostEqual(years[1]["known_equity_share"], 1 / 3, places=6)
        self.assertEqual(years[1]["unpriced_position_instances"], 5)

    def test_a_year_with_no_known_session_reports_a_zero_share(self):
        rows = [{"year": 2022, "unpriced_positions": 1}]
        years = known_equity_by_year(rows)
        self.assertEqual(years[0]["known_equity_sessions"], 0)
        self.assertEqual(years[0]["known_equity_share"], 0.0)


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_fixture(Path(self._tmp.name))
        self.diagnostic = build_diagnostic(self.root, enrich=False)

    def tearDown(self):
        self._tmp.cleanup()

    def _variant(self, variant_id: str) -> dict:
        for variant in self.diagnostic["variants"]:
            if variant["variant_id"] == variant_id:
                return variant
        self.fail(f"variant {variant_id} missing from the diagnostic")

    def test_the_execution_calendar_is_rebuilt_from_the_session_calendar(self):
        variant = self._variant(VARIANT_A)
        self.assertEqual(variant["execution_sessions"], 3)
        self.assertEqual(variant["rebalances"], 3)
        self.assertEqual(
            variant["reconstruction_cross_checks"]["trades_on_non_execution_sessions"], 0)

    def test_the_trading_activity_is_measured_correctly(self):
        variant = self._variant(VARIANT_A)
        self.assertEqual(variant["sessions"], 75)
        self.assertEqual(variant["trades"], 1)
        self.assertEqual(variant["trade_sessions"], 1)
        self.assertEqual(variant["sessions_with_zero_trades"], 74)
        self.assertEqual(variant["first_trade"], "2000-03-01")
        self.assertEqual(variant["last_trade"], "2000-03-01")
        streak = variant["longest_zero_trade_streak"]
        # 2000-01-03 .. 2000-02-29 is one unbroken run of 42 sessions with no
        # trade: the warm-up and the first rebalance that produced nothing.
        self.assertEqual(streak["sessions"], 42)
        self.assertEqual(streak["start"], "2000-01-03")
        self.assertEqual(streak["end"], "2000-02-29")

    def test_the_mechanism_attribution_separates_the_freeze_from_the_rest(self):
        variant = self._variant(VARIANT_A)
        buckets = variant["mechanism_attribution"]["buckets"]
        self.assertEqual(buckets[BUCKET_ORDERS], 1)
        self.assertEqual(buckets[BUCKET_FROZEN], 1)
        self.assertEqual(buckets[BUCKET_NO_ELIGIBLE], 1)
        self.assertEqual(
            variant["mechanism_attribution"]["data_or_model_limitation_sessions"], 1)
        self.assertEqual(
            variant["mechanism_attribution"]["genuine_market_or_strategy_sessions"], 1)
        self.assertEqual(variant["execution_sessions_without_orders"], 2)
        self.assertAlmostEqual(variant["share_rebalances_without_order"], 2 / 3, places=6)

    def test_an_attested_quarantine_is_attributed_to_the_policy(self):
        variant = self._variant(VARIANT_B)
        buckets = variant["mechanism_attribution"]["buckets"]
        self.assertEqual(buckets[BUCKET_QUARANTINED], 1)
        self.assertEqual(buckets[BUCKET_FROZEN], 1)
        self.assertEqual(buckets.get(BUCKET_NO_ELIGIBLE, 0), 0)
        self.assertEqual(
            variant["mechanism_attribution"]["genuine_market_or_strategy_sessions"], 1)

    def test_rebalances_without_eligible_names_come_from_the_signal_counter(self):
        self.assertEqual(self._variant(VARIANT_A)["rebalances_without_eligible_names"], 1)
        self.assertEqual(self._variant(VARIANT_B)["rebalances_without_eligible_names"], 0)

    def test_the_counterfactual_counts_sessions_with_a_knowable_equity(self):
        variant = self._variant(VARIANT_A)
        self.assertEqual(variant["known_equity_sessions"], KNOWN_SESSIONS)
        self.assertEqual(variant["unknown_equity_sessions"], UNKNOWN_SESSIONS)
        self.assertAlmostEqual(variant["known_equity_share"], KNOWN_SESSIONS / 75, places=6)
        counterfactual = self.diagnostic["counterfactual_known_equity"]
        self.assertEqual(len(counterfactual["per_variant"]), 2)
        year = counterfactual["by_year"][0]
        self.assertEqual(year["year"], 2000)
        self.assertEqual(year["known_equity_sessions_total"], KNOWN_SESSIONS * 2)
        self.assertEqual(year["total_variant_sessions"], 150)

    def test_the_per_year_table_covers_every_session(self):
        rows = self.diagnostic["variant_year_activity"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["sessions"], 75)
            self.assertEqual(row["known_equity_sessions"], KNOWN_SESSIONS)
            self.assertEqual(row["unknown_equity_sessions"], UNKNOWN_SESSIONS)
            self.assertEqual(row["trades"], 1)
            self.assertEqual(row["trade_sessions"], 1)

    def test_no_variant_publishes_a_final_equity(self):
        self.assertFalse(self.diagnostic["verdict"]["final_equity_available"])
        self.assertEqual(self.diagnostic["summary"]["variants_with_final_equity"], 0)
        for variant in self.diagnostic["variants"]:
            self.assertIsNone(variant["final_equity_strict"])

    def test_the_terminal_proceeds_gap_is_reported_plainly(self):
        terminal = self.diagnostic["verdict"]["terminal_proceeds"]
        self.assertEqual(terminal["proceeds_resolved"], 0)
        self.assertEqual(terminal["proceeds_unresolved"], 3)
        self.assertIn("3", terminal["statement"])

    def test_the_frozen_security_is_identified(self):
        self.assertEqual(self.diagnostic["frozen_securities"]["symbols"], [FROZEN_SYMBOL])

    def test_the_skip_reason_is_reported_as_raised(self):
        verdict = self.diagnostic["verdict"]
        self.assertTrue(verdict["hypothesis_supported"])
        self.assertTrue(verdict["skip_reason_is_raised"])
        self.assertEqual(verdict["skip_reason_checked"], "buy_unknown_held_opening_value")
        self.assertEqual(
            self._variant(VARIANT_A)["engine_skip_counts"]["buy_unknown_held_opening_value"], 1)

    def test_the_diagnostic_is_deterministic(self):
        again = build_diagnostic(self.root, enrich=False)
        self.assertEqual(self.diagnostic["determinism"]["analysis_digest"],
                         again["determinism"]["analysis_digest"])
        self.assertEqual(json.dumps(self.diagnostic, sort_keys=True, default=str),
                         json.dumps(again, sort_keys=True, default=str))

    def test_the_warmup_cross_check_matches_the_no_eligible_names_bucket(self):
        """The no-order sessions before the first trade are the warm-up."""
        a = self._variant(VARIANT_A)["reconstruction_cross_checks"]
        self.assertEqual(a["no_order_sessions_before_first_trade"], 1)
        self.assertEqual(a["no_eligible_names_bucket_sessions"], 1)
        # Variant B's warm-up session carries an attested quarantine refusal,
        # which is a more specific cause than 'no eligible names', so the two
        # counts deliberately differ there.
        b = self._variant(VARIANT_B)["reconstruction_cross_checks"]
        self.assertEqual(b["no_order_sessions_before_first_trade"], 1)
        self.assertEqual(b["no_eligible_names_bucket_sessions"], 0)

    def test_the_freeze_detector_agrees_with_the_engine_counter(self):
        """The detector and the engine's own counter must not disagree."""
        for variant in self.diagnostic["variants"]:
            checks = variant["reconstruction_cross_checks"]
            self.assertEqual(checks["trades_on_non_execution_sessions"], 0)
            self.assertEqual(checks["sessions_unpriced_but_opening_mark_known"], 0)
            self.assertEqual(checks["execution_sessions_with_unpriced_holding"],
                             checks["engine_sessions_with_unknown_opening_mark"])

    def test_the_outputs_are_written_and_reproducible(self):
        out = self.root / "processed/backtest/freeze"
        reports = self.root / "reports"
        first = write_outputs(self.diagnostic, out_dir=out, reports_dir=reports)
        self.assertTrue(Path(first["report_json"]).exists())
        self.assertTrue(Path(first["report_md"]).exists())
        self.assertTrue(Path(first["variant_activity_csv"]).exists())
        self.assertTrue(Path(first["variant_year_activity_csv"]).exists())
        self.assertTrue(Path(first["known_equity_by_year_csv"]).exists())
        json_bytes = Path(first["report_json"]).read_bytes()
        md_bytes = Path(first["report_md"]).read_bytes()
        write_outputs(self.diagnostic, out_dir=out, reports_dir=reports)
        self.assertEqual(json_bytes, Path(first["report_json"]).read_bytes())
        self.assertEqual(md_bytes, Path(first["report_md"]).read_bytes())

    def test_the_markdown_report_contains_the_tables(self):
        markdown = render_markdown(self.diagnostic)
        self.assertIn("# Backtest freeze diagnostic", markdown)
        self.assertIn(VARIANT_A, markdown)
        self.assertIn("no bar in date-local EQ/INE universe", markdown)
        self.assertIn("known equity", markdown.lower())
        self.assertIn("## Per-variant trading activity", markdown)
        self.assertIn("## Counterfactual", markdown)
        # A year is a label, not a quantity: it must not carry a thousands
        # separator.
        self.assertIn("| 2000 |", markdown)
        self.assertNotIn("| 2,000 |", markdown)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
