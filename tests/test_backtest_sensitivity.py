"""Synthetic DuckDB/parquet fixtures for the sensitivity diagnostic.

These tests prove the *safety properties* of the diagnostic, never that the
strategy works: strict refusal while readiness blockers remain, no alpha flag
anywhere, unknown positions that are never silently valued as cash, the two
unresolved-event policies that actually differ in the audited impact counts,
and a grid whose output is deterministic.
"""
import csv
import contextlib
from datetime import date, timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb

from nse_pipeline.backtest.sensitivity import (
    DEFAULT_POLICIES,
    SensitivityReadinessError,
    build_grid,
    main,
    run_sensitivity,
)

SCHEME_EX_DATE = date(2020, 7, 10)
# Dated between the 2020-07-31 signal and its 2020-08-03 execution, so the
# buy quarantine bites without the signal contamination rule removing it.
SCHEME_PRE_EXECUTION_EX_DATE = date(2020, 8, 1)


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


def make_data(root, rows, splits=(), risks=(), dividends=(), identities=()):
    adjusted = root / "processed/adjusted"
    adjusted.mkdir(parents=True)
    with duckdb.connect(str(root / "market.duckdb")) as db:
        db.execute("CREATE TABLE bars(date DATE,symbol VARCHAR,series VARCHAR,isin VARCHAR,"
                   "open DOUBLE,close DOUBLE,previous_close DOUBLE,volume BIGINT,valid_ohlcv BOOLEAN)")
        db.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?)", [r[:9] for r in rows])
        db.execute("CREATE TABLE adjusted(date DATE,symbol VARCHAR,series VARCHAR,adj_close DOUBLE)")
        db.executemany("INSERT INTO adjusted VALUES (?,?,?,?)", [(r[0], r[1], r[2], r[9]) for r in rows])
        db.execute("COPY adjusted TO ? (FORMAT PARQUET)", [str(adjusted / "adjusted_bars.parquet")])
        db.execute("CREATE TABLE audit(symbol VARCHAR,series VARCHAR,ex_date DATE,"
                   "share_multiplier DOUBLE,decision VARCHAR)")
        if splits:
            db.executemany("INSERT INTO audit VALUES (?,?,?,?,?)", list(splits))
        db.execute("COPY audit TO ? (FORMAT PARQUET)", [str(adjusted / "action_audit.parquet")])
    for path, records in [
        (root / "processed/ledger/canonical-events.jsonl", risks),
        (root / "processed/actions_dividends/dividend_events.jsonl", dividends),
        (root / "processed/identity/identity_edges.jsonl", identities),
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def read_csv(path):
    with Path(path).open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def alpha_keys(node, path="summary"):
    """Every mapping key that mentions alpha, with its value."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if "alpha" in str(key).lower():
                found.append((f"{path}.{key}", value))
            found.extend(alpha_keys(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(alpha_keys(value, f"{path}[{index}]"))
    return found


class SensitivityTests(unittest.TestCase):
    def build_fixture(self, root, dates):
        """Two listed securities with unresolved scheme events and two gaps.

        ``A`` has an unresolved scheme inside its lookback window (signal
        contamination) and ``B`` has one between a signal and its execution
        (buy quarantine).  The gaps keep the exchange sessions alive (another
        listing) so the holding stays open and becomes unpriced rather than
        being sold into cash.
        """
        missing = {date(2020, 7, 15), dates[-1]}
        rows = [row(d) for d in dates if d not in missing]
        rows += [row(d, "B", isin="INE000B") for d in dates]
        for d in sorted(missing):
            rows.append(row(d, "OTHER", isin="INEOTHER"))
        rows.sort(key=lambda r: (r[0], r[1]))
        make_data(root, rows, risks=[
            {"symbol": "A", "event_id": "UNKNOWN-SCHEME-A",
             "ex_date": str(SCHEME_EX_DATE), "terms_verified": False},
            {"symbol": "B", "event_id": "UNKNOWN-SCHEME-B",
             "ex_date": str(SCHEME_PRE_EXECUTION_EX_DATE), "terms_verified": False},
        ])
        return dates

    def run_fixture(self, root, dates, **kwargs):
        options = dict(allow_incomplete=True, initial_cash=100000,
                       lookbacks=(21, 63), top_ns=(2,), policies=DEFAULT_POLICIES)
        options.update(kwargs)
        return run_sensitivity(root, str(dates[0]), str(dates[-1]), **options)

    def test_strict_run_refuses_while_readiness_blockers_remain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, dates = Path(tmp), sessions(165)
            self.build_fixture(root, dates)
            with self.assertRaises(SensitivityReadinessError) as caught:
                run_sensitivity(root, str(dates[0]), str(dates[-1]))
            self.assertIn("corporate_actions", caught.exception.blocker_codes)
            self.assertFalse(caught.exception.readiness["ready_for_alpha_claim"])
            # The gate ran first and no trading output was written.
            out = root / "processed/backtest/sensitivity"
            self.assertFalse((out / "summary.json").exists())
            self.assertFalse((out / "comparison.csv").exists())
            self.assertFalse((out / "trades.csv").exists())

    def test_cli_exits_nonzero_without_allow_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, dates = Path(tmp), sessions(165)
            self.build_fixture(root, dates)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = main(["--data", str(root), "--start", str(dates[0]),
                             "--end", str(dates[-1]), "--cash", "100000",
                             "--lookback", "21", "--top-n", "1"])
            self.assertEqual(code, 2)
            self.assertIn("refused", buffer.getvalue())
            self.assertFalse((root / "processed/backtest/sensitivity/summary.json").exists())

    def test_no_alpha_flag_anywhere_in_the_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, dates = Path(tmp), sessions(165)
            self.build_fixture(root, dates)
            summary = self.run_fixture(root, dates)
            self.assertFalse(summary["ready_for_alpha_claim"])
            self.assertFalse(summary["ready_for_backtesting"])
            self.assertFalse(summary["total_return"])
            self.assertFalse(summary["alpha_claim"])
            for path, value in alpha_keys(summary):
                self.assertFalse(value, f"alpha flag set at {path}")
            text = json.dumps(summary)
            self.assertNotIn('"ready_for_alpha_claim": true', text)
            report = (root / "reports/backtest-sensitivity.json").read_text(encoding="utf-8")
            self.assertNotIn('"ready_for_alpha_claim": true', report)
            markdown = (root / "reports/backtest-sensitivity.md").read_text(encoding="utf-8")
            self.assertIn("not alpha", markdown)

    def test_unknown_position_is_never_silently_valued_as_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, dates = Path(tmp), sessions(165)
            self.build_fixture(root, dates)
            summary = self.run_fixture(root, dates)
            marks = read_csv(summary["outputs"]["equity_marks_csv"])
            audit_only = "L21-N2-audit_only"
            gap = [m for m in marks if m["variant_id"] == audit_only
                   and m["date"] == "2020-07-15"]
            self.assertEqual(len(gap), 1)
            row_ = gap[0]
            # Equity is unknown: not zero, not the cash balance.
            self.assertEqual(row_["equity_strict"], "")
            self.assertEqual(row_["unpriced_positions"], "1")
            self.assertEqual(row_["equity_status"], "unknown_unpriced_holding")
            # The unpriceable holding contributes NOTHING to the strict equity:
            # the strict figure stays unknown, and the cash balance is not a
            # substitute for it.
            self.assertNotEqual(row_["equity_strict"], str(float(row_["cash"])))
            # No total of any kind is published while the holding is
            # unpriceable: publishing one would mean putting a stale price on
            # a security that cannot be priced.
            self.assertEqual(row_["equity_last_observed_close_marking_ASSUMPTION"], "")
            # The holding is still held and no cash was released for it.
            self.assertEqual(row_["n_positions"], "2")
            previous = [m for m in marks if m["variant_id"] == audit_only
                        and m["date"] == "2020-07-14"]
            self.assertEqual(previous[0]["cash"], row_["cash"])
            self.assertTrue(float(previous[0]["equity_strict"]) > float(row_["cash"]))
            # The unknown session must not carry the previous session's equity
            # forward as if it were a value.
            self.assertNotEqual(row_["equity_strict"], previous[0]["equity_strict"])
            # The exposure is disclosed instead of valued.
            self.assertGreater(int(row_["unpriced_shares"]), 0)
            self.assertEqual(row_["unpriced_symbols"], "A")
            # The marked total is not published at all while a holding is
            # unpriced; it is never a return.
            report = next(v for v in summary["variants"] if v["variant_id"] == audit_only)
            self.assertGreaterEqual(report["unpriced_equity_sessions"], 1)
            self.assertEqual(report["sessions_with_unknown_positions"],
                             report["unpriced_equity_sessions"])
            marking = report["equity_last_observed_close_marking"]
            self.assertIn("not a return", marking["label"])
            self.assertIn("ASSUMPTION", marking["assumption"])
            self.assertFalse(marking["published"])
            self.assertIsNone(marking["final_equity"])
            self.assertEqual(report["sessions_where_no_total_could_be_published"],
                             report["sessions_with_unknown_positions"])
            # A position that cannot be priced at the end leaves the strict
            # equity unknown instead of being cashed out.
            self.assertIsNone(report["final_equity_strict"])
            self.assertGreaterEqual(report["terminal_positions"], 1)

    def test_no_equity_figure_ever_contains_an_unpriceable_holding(self):
        """The invariant, checked over every session of every variant.

        An unpriceable holding contributes nothing to any equity figure: not
        to the strict equity and not to the labelled assumption total either.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root, dates = Path(tmp), sessions(165)
            self.build_fixture(root, dates)
            summary = self.run_fixture(root, dates)
            marks = read_csv(summary["outputs"]["equity_marks_csv"])
            self.assertTrue(marks)
            known = 0
            unknown = 0
            for row in marks:
                unpriced = int(row["unpriced_positions"])
                if unpriced:
                    # No total of any kind is published on such a session: it
                    # is not cash, not zero and not a stale price.
                    self.assertEqual(row["equity_strict"], "", row)
                    self.assertEqual(row["equity_last_observed_close_marking_ASSUMPTION"], "",
                                     row)
                    self.assertEqual(row["equity_status"], "unknown_unpriced_holding", row)
                    self.assertGreater(int(row["unpriced_shares"]), 0, row)
                    unknown += 1
                else:
                    known += 1
                    self.assertNotEqual(row["equity_strict"], "", row)
                    # Where everything was priced the marked total is simply
                    # the strict equity; it never invents a value.
                    self.assertEqual(row["equity_last_observed_close_marking_ASSUMPTION"],
                                     row["equity_strict"], row)
                    self.assertEqual(row["equity_status"], "known", row)
            self.assertGreater(known, 0)
            self.assertGreater(unknown, 0)
            # The per-variant report keeps the two figures apart as well.
            for report in summary["variants"]:
                marking = report["equity_last_observed_close_marking"]
                if report["final_equity_strict"] is None:
                    # The equity is unknown and no marked total is published.
                    self.assertFalse(marking["published"], report["variant_id"])
                    self.assertIsNone(marking["final_equity"], report["variant_id"])
                    self.assertIn("not a return", marking["label"])
                else:
                    self.assertTrue(marking["published"], report["variant_id"])
                    self.assertIsInstance(marking["final_equity"], float, report["variant_id"])
                    self.assertEqual(marking["final_equity"], report["final_equity_strict"],
                                     report["variant_id"])

    def test_unpriced_exposure_is_disclosed_per_session_and_never_valued(self):
        """An unpriceable holding is disclosed as exposure, never as a value.

        The strict equity stays unknown and the marked total stays
        unpublished; what is published instead is the *size* of the hole:
        the notional exposure at the position's own last observed close and
        the staleness of that mark.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root, dates = Path(tmp), sessions(165)
            self.build_fixture(root, dates)
            summary = self.run_fixture(root, dates)
            marks = read_csv(summary["outputs"]["equity_marks_csv"])
            audit_only = "L21-N2-audit_only"
            gap = [m for m in marks if m["variant_id"] == audit_only
                   and m["date"] == "2020-07-15"]
            self.assertEqual(len(gap), 1)
            row_ = gap[0]
            # The equity is still unknown: not zero, not cash, not a stale
            # price, and no marked total at all.
            self.assertEqual(row_["equity_strict"], "")
            self.assertEqual(row_["equity_last_observed_close_marking_ASSUMPTION"], "")
            self.assertEqual(row_["equity_status"], "unknown_unpriced_holding")
            # The size of the hole is disclosed instead.
            shares = int(row_["unpriced_shares"])
            self.assertGreater(shares, 0)
            exposure = float(row_["unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION"])
            # A was last observed at a close of 100.0 on the previous session.
            self.assertAlmostEqual(exposure, shares * 100.0, places=6)
            # A had a last observed close, so nothing is missing from the
            # exposure and the mark is one calendar day old.
            self.assertEqual(row_["unpriced_positions_without_last_close"], "0")
            self.assertEqual(row_["unpriced_mark_staleness_days_max"], "1")
            # Where EVERY holding is unpriceable the published known value is
            # exactly zero: the unpriceable book contributes nothing to any
            # published value, it is only disclosed.
            fully_unpriced = [m for m in marks if int(m["n_positions"]) > 0
                              and int(m["unpriced_positions"]) == int(m["n_positions"])]
            self.assertTrue(fully_unpriced)
            for row_ in fully_unpriced:
                self.assertEqual(float(row_["positions_value_known"]), 0.0, row_)
                self.assertGreater(
                    float(row_["unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION"]),
                    0.0, row_)

    def test_unpriced_exposure_aggregates_match_the_per_session_figures(self):
        """Per-variant and per-year aggregates are session figures, never sums."""
        with tempfile.TemporaryDirectory() as tmp:
            root, dates = Path(tmp), sessions(165)
            self.build_fixture(root, dates)
            summary = self.run_fixture(root, dates)
            marks = read_csv(summary["outputs"]["equity_marks_csv"])
            years = read_csv(summary["outputs"]["unknown_coverage_by_year_csv"])
            reports = {v["variant_id"]: v for v in summary["variants"]}
            self.assertTrue(reports)
            for variant_id, report in reports.items():
                rows = [m for m in marks if m["variant_id"] == variant_id]
                self.assertTrue(rows, variant_id)
                session_values = [
                    float(m["unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION"])
                    for m in rows]
                self.assertGreater(max(session_values), 0.0, variant_id)
                # The aggregate is the FINAL session's magnitude and the
                # worst-session magnitude. It is never a session sum: the
                # same stuck holding is unpriceable on every session it stays
                # unpriceable, so a sum would re-count one position
                # thousands of times and describe nothing.
                self.assertAlmostEqual(
                    session_values[-1],
                    report["final_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION"],
                    places=4, msg=variant_id)
                self.assertAlmostEqual(
                    max(session_values),
                    report["max_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION"],
                    places=4, msg=variant_id)
                staleness = max(int(m["unpriced_mark_staleness_days_max"]) for m in rows)
                self.assertEqual(staleness, report["max_unpriced_mark_staleness_days"],
                                 variant_id)
                without = sum(int(m["unpriced_positions_without_last_close"]) for m in rows)
                self.assertEqual(without, report["unpriced_positions_without_last_close"],
                                 variant_id)
                # The per-year table carries maxima and final-session values
                # and its worst year equals the variant maximum.
                year_rows = [r for r in years if r["variant_id"] == variant_id]
                self.assertTrue(year_rows, variant_id)
                self.assertAlmostEqual(
                    max(float(r["max_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION"])
                        for r in year_rows),
                    report["max_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION"],
                    places=4, msg=variant_id)
                self.assertEqual(
                    max(int(r["max_unpriced_mark_staleness_days"]) for r in year_rows),
                    report["max_unpriced_mark_staleness_days"], variant_id)
                # The exposure never becomes an equity figure, and the
                # published aggregate key does not read like a total.
                self.assertNotIn("equity", "final_unpriced_exposure_value_"
                                 "AT_LAST_OBSERVED_CLOSE_ASSUMPTION")
                self.assertNotIn("total", "final_unpriced_exposure_value_"
                                 "AT_LAST_OBSERVED_CLOSE_ASSUMPTION")

    def test_no_equity_named_column_carries_the_exposure_value(self):
        """The exposure is published under a name that cannot be read as a total."""
        with tempfile.TemporaryDirectory() as tmp:
            root, dates = Path(tmp), sessions(165)
            self.build_fixture(root, dates)
            summary = self.run_fixture(root, dates)
            expected_equity_columns = {
                "equity_marks_csv": {"equity_strict",
                                     "equity_last_observed_close_marking_ASSUMPTION",
                                     "equity_status"},
                "comparison_csv": {"unpriced_equity_sessions", "final_equity_strict",
                                   "final_equity_last_observed_close_marking_ASSUMPTION"},
                "unknown_coverage_by_year_csv": set(),
            }
            for name, expected in expected_equity_columns.items():
                rows = read_csv(summary["outputs"][name])
                self.assertTrue(rows, name)
                header = list(rows[0].keys())
                self.assertEqual({c for c in header if "equity" in c.lower()}, expected, name)
                # No new column carries the exposure value under an equity name.
                for column in header:
                    if "exposure" in column.lower():
                        self.assertNotIn("equity", column.lower(), f"{name}:{column}")
            # The exposure value never appears inside an equity figure.
            marks = read_csv(summary["outputs"]["equity_marks_csv"])
            for row in marks:
                exposure = float(row["unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION"])
                if int(row["unpriced_positions"]):
                    self.assertEqual(row["equity_strict"], "", row)
                    self.assertEqual(row["equity_last_observed_close_marking_ASSUMPTION"], "",
                                     row)
                for column in ("equity_strict",
                               "equity_last_observed_close_marking_ASSUMPTION"):
                    if row[column] != "":
                        self.assertNotEqual(float(row[column]), exposure, row)
            # The summary keys that carry the exposure are not equity keys.
            for report in summary["variants"]:
                for key in ("final_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION",
                            "max_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION",
                            "max_unpriced_mark_staleness_days",
                            "unpriced_positions_without_last_close"):
                    self.assertIn(key, report)
                    self.assertNotIn("equity", key.lower())
                disclosure = report["unpriced_exposure_disclosure"]
                self.assertIn("not equity", disclosure["label"])
                self.assertIn("ASSUMPTION", disclosure["assumption"])

    def test_exposure_disclosure_is_deterministic(self):
        """Two runs over the same inputs publish the same exposure figures."""
        with tempfile.TemporaryDirectory() as tmp:
            root, dates = Path(tmp), sessions(165)
            self.build_fixture(root, dates)
            first = self.run_fixture(root, dates)
            second = self.run_fixture(root, dates)
            self.assertEqual(first["determinism"]["analysis_digest"],
                             second["determinism"]["analysis_digest"])
            for name in ("equity_marks_csv", "comparison_csv",
                         "unknown_coverage_by_year_csv", "report_md"):
                self.assertEqual(Path(first["outputs"][name]).read_bytes(),
                                 Path(second["outputs"][name]).read_bytes(), name)
            left = read_csv(first["outputs"]["equity_marks_csv"])
            right = read_csv(second["outputs"]["equity_marks_csv"])
            columns = ("unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION",
                       "unpriced_positions_without_last_close",
                       "unpriced_mark_staleness_days_max")
            self.assertEqual([[row[c] for c in columns] for row in left],
                             [[row[c] for c in columns] for row in right])

    def test_quarantine_policy_changes_the_outcome_versus_audit_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, dates = Path(tmp), sessions(165)
            self.build_fixture(root, dates)
            summary = self.run_fixture(root, dates)
            reports = {v["variant_id"]: v for v in summary["variants"]}
            audit = reports["L21-N2-audit_only"]
            quarantine = reports["L21-N2-quarantine_around_events"]
            # The unresolved scheme events quarantine the holding in the
            # fail-closed policy, so more sessions have unknown positions.
            self.assertGreater(quarantine["sessions_with_unknown_positions"],
                               audit["sessions_with_unknown_positions"])
            self.assertGreater(quarantine["skip_counts"].get(
                "buy_quarantined_unresolved_event", 0), 0)
            # A quarantined holding is not liquidated into cash either.
            self.assertGreater(quarantine["skip_counts"].get(
                "sell_quarantined_unresolved_event", 0), 0)

    def test_quarantine_policy_differs_in_the_audited_impact_counts(self):
        """The two policies must differ in the audit trail, not only in wording."""
        with tempfile.TemporaryDirectory() as tmp:
            root, dates = Path(tmp), sessions(165)
            self.build_fixture(root, dates)
            summary = self.run_fixture(root, dates)
            reports = {v["variant_id"]: v for v in summary["variants"]}
            audit = reports["L21-N2-audit_only"]
            quarantine = reports["L21-N2-quarantine_around_events"]
            self.assertNotEqual(audit["impact_counts"], quarantine["impact_counts"])
            self.assertGreater(quarantine["impact_counts"].get("order:execution", 0),
                               audit["impact_counts"].get("order:execution", 0))
            # The grid-level audit trail differs by policy as well.
            by_policy = summary["impact_counts_by_policy"]
            self.assertNotEqual(by_policy["audit_only"], by_policy["quarantine_around_events"])
            # And the comparison block states the difference explicitly.
            comparison = summary["policy_comparison"]
            self.assertTrue(comparison)
            for item in comparison:
                self.assertTrue(item["policies_differ"], item)
                self.assertIn("trades", item["outcomes"])
                self.assertIn("impact_counts", item)

    def test_comparison_csv_columns_carry_the_reported_values(self):
        """Every comparison column must carry the value the report states."""
        with tempfile.TemporaryDirectory() as tmp:
            root, dates = Path(tmp), sessions(165)
            self.build_fixture(root, dates)
            summary = self.run_fixture(root, dates)
            reports = {v["variant_id"]: v for v in summary["variants"]}
            rows = read_csv(summary["outputs"]["comparison_csv"])
            self.assertEqual(len(rows), len(reports))
            for row in rows:
                report = reports[row["variant_id"]]
                self.assertEqual(int(row["unpriced_equity_sessions"]),
                                 report["unpriced_equity_sessions"], row)
                self.assertEqual(int(row["sessions_with_unknown_positions"]),
                                 report["sessions_with_unknown_positions"], row)
                self.assertEqual(int(row["unknown_position_instances"]),
                                 report["unknown_position_instances"], row)
                self.assertEqual(int(row["trades"]), report["trades"], row)
                self.assertEqual(int(row["skips_total"]), report["skips_total"], row)
                self.assertEqual(int(row["sessions_where_no_total_could_be_published"]),
                                 report["sessions_where_no_total_could_be_published"], row)
                # The comparison CSV never publishes a marked total that
                # contains an unpriceable holding.
                if report["final_equity_strict"] is None:
                    self.assertEqual(row["final_equity_strict"], "", row)
                    self.assertEqual(
                        row["final_equity_last_observed_close_marking_ASSUMPTION"], "", row)
                else:
                    self.assertEqual(row["final_equity_last_observed_close_marking_ASSUMPTION"],
                                     row["final_equity_strict"], row)
                for reason in ("no_bar_in_date_local_eq_research_universe",
                               "no_valid_close_for_session",
                               "position_blocked_unverified_identity_change",
                               "quarantined_unresolved_event"):
                    self.assertEqual(int(row[f"unpriced_{reason}"]),
                                     report["unpriced_reason_counts"].get(reason, 0), row)

    def test_grid_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, dates = Path(tmp), sessions(165)
            self.build_fixture(root, dates)
            first = self.run_fixture(root, dates)
            second = self.run_fixture(root, dates)
            self.assertEqual([v["variant_id"] for v in first["variants"]],
                             [v["variant_id"] for v in second["variants"]])
            self.assertEqual(first["grid"]["variant_ids"], second["grid"]["variant_ids"])
            for left, right in zip(first["variants"], second["variants"]):
                left = dict(left)
                right = dict(right)
                left.pop("unknown_coverage_by_year", None)
                right.pop("unknown_coverage_by_year", None)
                self.assertEqual(left, right)
            self.assertEqual(first["variants"][0]["unknown_coverage_by_year"],
                             second["variants"][0]["unknown_coverage_by_year"])
            self.assertEqual(read_csv(first["outputs"]["comparison_csv"]),
                             read_csv(second["outputs"]["comparison_csv"]))

    def test_outputs_are_byte_deterministic(self):
        """Two runs over the same inputs produce the same bytes and the same digest."""
        with tempfile.TemporaryDirectory() as tmp:
            root, dates = Path(tmp), sessions(165)
            self.build_fixture(root, dates)
            first = self.run_fixture(root, dates)
            first_bytes = {name: Path(first["outputs"][name]).read_bytes()
                           for name in ("comparison_csv", "equity_marks_csv",
                                        "unknown_coverage_by_year_csv", "trades_csv",
                                        "impact_audit_csv", "report_md")}
            second = self.run_fixture(root, dates)
            for name, payload in first_bytes.items():
                self.assertEqual(payload, Path(second["outputs"][name]).read_bytes(), name)
            self.assertEqual(first["determinism"]["analysis_digest"],
                             second["determinism"]["analysis_digest"])
            self.assertTrue(first["determinism"]["analysis_digest"])
            self.assertIn("generated_at", first["determinism"]["volatile_fields"])

    def test_grid_cap_is_deterministic_and_recorded(self):
        grid, dropped = build_grid((21, 63, 126), (10, 20, 40), DEFAULT_POLICIES, 18)
        self.assertEqual(len(grid), 18)
        self.assertEqual(dropped, [])
        grid, dropped = build_grid((21, 63, 126), (10, 20, 40), DEFAULT_POLICIES, 5)
        self.assertEqual(len(grid), 5)
        self.assertEqual(len(dropped), 13)
        self.assertEqual([v.id for v in grid],
                         ["L21-N10-audit_only", "L21-N10-quarantine_around_events",
                          "L21-N20-audit_only", "L21-N20-quarantine_around_events",
                          "L21-N40-audit_only"])
        with tempfile.TemporaryDirectory() as tmp:
            root, dates = Path(tmp), sessions(165)
            self.build_fixture(root, dates)
            summary = self.run_fixture(root, dates, max_variants=2)
            self.assertEqual(summary["grid"]["variants_run"], 2)
            self.assertEqual(len(summary["grid"]["dropped_variants"]), 2)
            self.assertTrue(summary["grid"]["cap_reasons"])


if __name__ == "__main__":
    unittest.main()
