"""Synthetic DuckDB fixtures for the series-fallback cause analysis.

These tests prove the *classification contract*, never that a fallback is
legitimate: every unpriceable position-day receives exactly one cause, the
five causes are mutually exclusive and exhaustive, bucket 4 is never treated
as a mark, no equity figure is ever published, and the analysis is
deterministic.

The fixture is a tiny market with one security per cause plus one security
whose price history simply stops.

The market is built so that every cause is reachable *as the runner would see
it*:

* ``ENDSOK``    - bars stop before the unpriced sessions, not delisted.
* ``ENDSDEAD``  - bars stop before the unpriced sessions, on the official
  delisting list.
* ``RENAMED``   - the bars carry the new ticker, the position is booked under
  the old one, and the rename edge is dated after the session, so the row's
  key does not resolve to the held key.
* ``OTHERONLY`` - the security migrates from EQ to BE: the EQ line stops and
  the BE line carries on.
* ``BADCLOSE``  - the row exists under the held key but is not a valid
  observation.
* ``GHOST``     - no row exists in any series (used by its own test).
* ``FINE``      - a healthy security that is never unpriceable.
"""
import csv
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb

from nse_pipeline.backtest.series_fallback import (
    CAUSE_CROSS_SERIES_OTHER_ISIN_ONLY,
    CAUSE_IDENTITY_KEY_MISMATCH,
    CAUSE_INVALID_OR_MISSING_CLOSE,
    CAUSE_PRICE_HISTORY_ENDS_DELISTED,
    CAUSE_PRICE_HISTORY_ENDS_NOT_DELISTED,
    CAUSES,
    RunnerIdentity,
    analyse,
    company_key,
    render_markdown,
)

#: The session on which every fixture position is bought.
D0 = "2019-12-31"
#: The two sessions on which every fixture position is unpriceable.
D1 = "2020-01-02"
D2 = "2020-01-03"
#: A rename edge dated AFTER the unpriced sessions, so the row's key does not
#: walk back to the key the position is booked under.
RENAME_EFFECTIVE = "2020-06-03"


def bar(on, symbol, series="EQ", isin=None, close=100.0, valid=True):
    return (on, symbol, series, isin, close, valid)


def make_market(root, rows):
    """Create a minimal market.duckdb with a bars table."""
    with duckdb.connect(str(root / "market.duckdb")) as db:
        db.execute(
            "CREATE TABLE bars(date DATE,symbol VARCHAR,series VARCHAR,isin VARCHAR,"
            "open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,previous_close DOUBLE,"
            "volume BIGINT,traded_value DOUBLE,trades BIGINT,valid_ohlcv BOOLEAN,source VARCHAR)"
        )
        db.executemany(
            "INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (d, s, ser, isin, c, c, c, c, c, 1000, 1000.0, 10, valid, "fixture")
                for d, s, ser, isin, c, valid in rows
            ],
        )
        db.execute(
            "CREATE TABLE corporate_actions(symbol VARCHAR,series VARCHAR,ex_date DATE,"
            "purpose VARCHAR,isin VARCHAR,action_type VARCHAR,source VARCHAR,dataset VARCHAR)"
        )


def write_marks(root, marks):
    """Write the sensitivity ledger of unpriceable position-days."""
    path = root / "processed/backtest/sensitivity/equity_marks.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["variant_id", "date", "unpriced_positions", "unpriced_symbols"]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in marks:
            writer.writerow(row)


def write_trades(root, trades):
    path = root / "processed/backtest/sensitivity/trades.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["variant_id", "date", "signal_date", "key", "symbol", "side",
               "fill_price", "shares", "notional", "cost", "cash_after"]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in trades:
            writer.writerow(row)


def write_delisted(root, rows):
    path = root / "raw/security_master/delisted.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["Symbol", "Company", "Delisted Date", "Type of Delisting"]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_api_actions(root, rows):
    path = root / "processed/older_actions/nse-api-actions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def write_identity_edges(root, rows):
    path = root / "processed/identity/identity_edges.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


class SeriesFallbackTests(unittest.TestCase):
    """The five causes, one security each, plus the no-bar case."""

    def build_fixture(self, root):
        rows = [
            # (1) PRICE HISTORY ENDS, not on the delisting list.
            bar(D0, "ENDSOK"),
            # (2) PRICE HISTORY ENDS, and on the official delisting list.
            bar(D0, "ENDSDEAD"),
            # (3) IDENTITY KEY MISMATCH: the row exists but under another key.
            bar(D0, "RENAMED"), bar(D1, "RENAMED"), bar(D2, "RENAMED"),
            # (4) CROSS-SERIES ONLY: the EQ line stops, the BE line carries on.
            bar(D0, "OTHERONLY", series="EQ", isin="INE000OTHER"),
            bar(D1, "OTHERONLY", series="BE", isin=None),
            bar(D2, "OTHERONLY", series="BE", isin=None),
            # (5) INVALID OR MISSING CLOSE: the row exists but is not usable.
            bar(D0, "BADCLOSE"), bar(D1, "BADCLOSE", close=0.0, valid=False),
            bar(D2, "BADCLOSE", close=0.0, valid=False),
            # A security with a valid row that is simply not held unpriced.
            bar(D0, "FINE", isin="INE000FINE"),
            bar(D1, "FINE", isin="INE000FINE"), bar(D2, "FINE", isin="INE000FINE"),
        ]
        make_market(root, rows)
        marks = [
            {"variant_id": "V1", "date": D1, "unpriced_positions": 5,
             "unpriced_symbols": "ENDSOK;ENDSDEAD;RENAMED;OTHERONLY;BADCLOSE"},
            {"variant_id": "V1", "date": D2, "unpriced_positions": 5,
             "unpriced_symbols": "ENDSOK;ENDSDEAD;RENAMED;OTHERONLY;BADCLOSE"},
        ]
        write_marks(root, marks)
        # The holdings are bought on D0 and are still held on D1 and D2.
        # RENAMED is booked under the pre-rename key.
        trades = []
        for symbol, key in (("ENDSOK", "SYMBOL:ENDSOK"), ("ENDSDEAD", "SYMBOL:ENDSDEAD"),
                            ("RENAMED", "SYMBOL:OLDNAME"), ("OTHERONLY", "SYMBOL:OTHERONLY"),
                            ("BADCLOSE", "SYMBOL:BADCLOSE")):
            trades.append({"variant_id": "V1", "date": D0, "signal_date": D0, "key": key,
                           "symbol": symbol, "side": "buy", "fill_price": 100.0,
                           "shares": 10, "notional": 1000.0, "cost": 0.0, "cash_after": 0.0})
        write_trades(root, trades)
        write_delisted(root, [
            {"Symbol": "ENDSDEAD", "Company": "Dead Co Ltd", "Delisted Date": "01-Jan-20",
             "Type of Delisting": "Voluntary Delisting"},
        ])
        write_api_actions(root, [
            {"symbol": "ENDSOK", "comp": "End Co Limited", "exDate": "02-Jan-2020", "isin": None},
            {"symbol": "ENDRENEW", "comp": "End Co Limited", "exDate": "02-Jan-2020", "isin": None},
        ])
        write_identity_edges(root, [
            {"old_symbol": "OLDNAME", "new_symbol": "RENAMED",
             "effective_date": RENAME_EFFECTIVE,
             "verification": "official_symbolchange_row", "isin_old": None, "isin_new": None,
             "bars_order_violation": False, "isin_old_ambiguous": False,
             "isin_new_ambiguous": False, "reuse_flags": {}},
        ])
        return root

    def run_fixture(self, root):
        return analyse(root)

    def test_every_cause_is_reachable_and_the_counts_sum(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_fixture(Path(tmp))
            report = self.run_fixture(root)
            counts = report["summary"]["cause_counts"]
            self.assertEqual(set(counts), set(CAUSES))
            self.assertEqual(sum(counts.values()), 10)
            self.assertEqual(counts[CAUSE_PRICE_HISTORY_ENDS_NOT_DELISTED], 2)
            self.assertEqual(counts[CAUSE_PRICE_HISTORY_ENDS_DELISTED], 2)
            self.assertEqual(counts[CAUSE_IDENTITY_KEY_MISMATCH], 2)
            self.assertEqual(counts[CAUSE_CROSS_SERIES_OTHER_ISIN_ONLY], 2)
            self.assertEqual(counts[CAUSE_INVALID_OR_MISSING_CLOSE], 2)

    def test_each_cause_is_assigned_to_the_right_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_fixture(Path(tmp))
            report = self.run_fixture(root)
            by_symbol = {}
            for row in report["classifications"]:
                by_symbol.setdefault(row["symbol"], set()).add(row["cause"])
            self.assertEqual(by_symbol["ENDSOK"], {CAUSE_PRICE_HISTORY_ENDS_NOT_DELISTED})
            self.assertEqual(by_symbol["ENDSDEAD"], {CAUSE_PRICE_HISTORY_ENDS_DELISTED})
            self.assertEqual(by_symbol["RENAMED"], {CAUSE_IDENTITY_KEY_MISMATCH})
            self.assertEqual(by_symbol["OTHERONLY"], {CAUSE_CROSS_SERIES_OTHER_ISIN_ONLY})
            self.assertEqual(by_symbol["BADCLOSE"], {CAUSE_INVALID_OR_MISSING_CLOSE})

    def test_exactly_one_cause_per_position_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_fixture(Path(tmp))
            report = self.run_fixture(root)
            seen = set()
            for row in report["classifications"]:
                self.assertIn(row["cause"], CAUSES)
                key = (row["variant_id"], row["date"], row["symbol"])
                self.assertNotIn(key, seen)
                seen.add(key)
            self.assertEqual(len(seen), 10)

    def test_no_bar_case_has_no_row_at_all(self):
        """The no-bar case: a security with no row on the session at all."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_market(root, [bar(D1, "OTHER", isin="INE000OTHER")])
            write_marks(root, [{"variant_id": "V1", "date": D1, "unpriced_positions": 1,
                                "unpriced_symbols": "GHOST"}])
            write_trades(root, [{"variant_id": "V1", "date": D1, "signal_date": D1,
                                 "key": "SYMBOL:GHOST", "symbol": "GHOST", "side": "buy",
                                 "fill_price": 10.0, "shares": 1, "notional": 10.0,
                                 "cost": 0.0, "cash_after": 0.0}])
            report = analyse(root)
            causes = [r["cause"] for r in report["classifications"]]
            self.assertEqual(causes, [CAUSE_PRICE_HISTORY_ENDS_NOT_DELISTED])
            detail = report["classifications"][0]["detail"]
            self.assertIn("no bar exists in any series", detail["note"].lower())

    def test_classification_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_fixture(Path(tmp))
            first = self.run_fixture(root)
            second = self.run_fixture(root)
            self.assertEqual(first["summary"], second["summary"])
            self.assertEqual(first["classifications"], second["classifications"])
            self.assertEqual(first["symbols"], second["symbols"])

    def test_no_equity_or_mark_is_published(self):
        """Nothing in the report may be a price, a mark or an equity figure."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_fixture(Path(tmp))
            report = self.run_fixture(root)
            text = json.dumps(report).lower()
            for banned in ("equity", "mark_value", "portfolio_value", "nav"):
                self.assertNotIn(banned, text)
            # No price is published at all: the rows that are mentioned carry
            # the facts about the observation, never its value.
            for row in report["classifications"]:
                for evidence in row["detail"].get("rows", ()):
                    self.assertNotIn("close_value", evidence)
                    self.assertNotIn("price", evidence)
                    self.assertIn("close_present", evidence)
                    self.assertIn("close_positive", evidence)

    def test_cross_series_rows_are_never_a_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_fixture(Path(tmp))
            report = self.run_fixture(root)
            rows = [r for r in report["classifications"]
                    if r["cause"] == CAUSE_CROSS_SERIES_OTHER_ISIN_ONLY]
            self.assertTrue(rows)
            for row in rows:
                note = row["detail"]["note"]
                self.assertIn("PROXY", note)
                self.assertIn("never marks", note)
                self.assertFalse(row["detail"]["same_isin_in_other_series"])

    def test_per_symbol_evidence_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_fixture(Path(tmp))
            report = self.run_fixture(root)
            by_symbol = {row["symbol"]: row for row in report["symbols"]}
            self.assertEqual(by_symbol["ENDSOK"]["first_bar_by_series"]["EQ"], D0)
            self.assertEqual(by_symbol["ENDSOK"]["last_bar_by_series"]["EQ"], D0)
            self.assertEqual(by_symbol["OTHERONLY"]["first_bar_by_series"]["BE"], D1)
            self.assertEqual(by_symbol["OTHERONLY"]["last_bar_by_series"]["BE"], D2)
            self.assertEqual(by_symbol["ENDSDEAD"]["unpriceable_position_days"], 2)
            self.assertTrue(by_symbol["ENDSDEAD"]["in_official_delisting_list"])
            self.assertEqual(by_symbol["ENDSDEAD"]["official_delisting_date"], "01-Jan-20")
            self.assertFalse(by_symbol["ENDSOK"]["in_official_delisting_list"])
            # The same official company name is a LEAD, never a link.
            self.assertEqual(by_symbol["ENDSOK"]["api_same_name_successors"], ["ENDRENEW"])
            self.assertTrue(by_symbol["ENDSOK"]["api_successor_is_lead_only"])

    def test_company_key_ignores_legal_form_words(self):
        self.assertEqual(company_key("Acme Industries Limited"), company_key("ACME INDUSTRIES LTD"))
        self.assertNotEqual(company_key("Acme Industries Limited"), company_key("Acme Traders Ltd"))

    def test_runner_identity_uses_the_runner_key(self):
        """The identity key of a bar is the runner's own key."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_fixture(Path(tmp))
            import duckdb as _duckdb

            connection = _duckdb.connect(str(root / "market.duckdb"), read_only=True)
            try:
                identity = RunnerIdentity.from_data(root, connection)
                # A rename edge dated after the session does not apply yet.
                self.assertEqual(
                    identity.key("RENAMED", None, date(2020, 1, 3)), "SYMBOL:RENAMED"
                )
                # A row with an ISIN is keyed by that ISIN.
                self.assertEqual(
                    identity.key("FINE", "INE000FINE", date(2020, 1, 3)),
                    "ISIN:INE000FINE",
                )
            finally:
                connection.close()

    def test_markdown_report_names_every_cause_and_publishes_no_figure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_fixture(Path(tmp))
            report = self.run_fixture(root)
            markdown = render_markdown(report)
            for cause in CAUSES:
                self.assertIn(cause, markdown)
            self.assertIn("PROXY", markdown)
            self.assertNotIn("equity", markdown.lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
