from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.actions.verified import normalize_actions
from nse_pipeline.adjust.factors import apply_adjustment, build_intervals, classify_actions, run_adjustment


def api(subject="Bonus 1:1", **kwargs):
    return {
        "symbol": "AAA", "series": "EQ", "isin": "CURRENT_ISIN",
        "subject": subject, "exDate": "03-Jan-2006", "ex_date": "2006-01-03",
        "recDate": "05-Jan-2006", "source_file": "nse-api-2006.json", "source_row": 0,
        "source_url": "https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date=01-01-2006&to_date=31-12-2006",
        "action_tags": ["bonus"], **kwargs,
    }


def observations(symbol="AAA", series="EQ"):
    return [
        {"symbol": symbol, "series": series, "date": "2006-01-02", "isin": None},
        {"symbol": symbol, "series": series, "date": "2006-01-03", "isin": None},
    ]


class VerifiedActionTests(unittest.TestCase):
    def test_explicit_official_pre2011_source_reaches_existing_classifier(self):
        normalized = normalize_actions([api()], [], observations(), [])
        self.assertEqual(len(normalized), 1)
        row = normalized[0]
        self.assertTrue(row.execution_ready)
        self.assertEqual(row.share_multiplier, 2.0)
        frame = pd.DataFrame([row.adjustment_row()])
        classified = classify_actions(frame, {("AAA", "EQ", date(2006, 1, 3)): 1})
        self.assertEqual(classified.iloc[0]["decision"], "applied")

    def test_record_effective_and_break_dates_are_not_exchange_ex_dates(self):
        circular = {
            "symbol": "AAA", "series": "EQ", "date": "2006-01-03",
            "ex_date": "2006-01-03", "record_date": "2006-01-05",
            "verification_status": "verified_split", "verified_event_type": "split",
            "stated_terms_verbatim": "Shares were split from Rs.10/- per share to Re.1/- per share effective January 5, 2006.",
            "citation": {"url": "https://www.bseindia.com/annual.pdf", "snippet": "Shares were split from Rs.10/- per share to Re.1/- per share effective January 5, 2006.", "stated_dates": ["2006-01-05"]},
        }
        row = normalize_actions([], [circular], observations(), [])[0]
        self.assertTrue(row.terms_verified)
        self.assertEqual(row.share_multiplier, 10.0)
        self.assertFalse(row.execution_ready)
        self.assertIsNone(row.ex_date)
        self.assertEqual(row.readiness_reason, "missing_exact_exchange_ex_date")

    def test_wrong_derived_api_date_and_non_exchange_citations_fail_closed(self):
        for changed, reason in (
            ({"ex_date": "2006-01-05"}, "conflicting_exchange_ex_date"),
            ({"exDate": None}, "missing_exact_exchange_ex_date"),
            ({"source_url": "https://example.com/api"}, "unverified_exchange_source"),
        ):
            with self.subTest(changed=changed):
                row = normalize_actions([api(**changed)], [], observations(), [])[0]
                self.assertFalse(row.execution_ready)
                self.assertEqual(row.readiness_reason, reason)

    def test_split_bonus_semantics_exact_duplicates_and_compound_conflicts(self):
        for subject, multiplier in (("Fv Split Rs.10/- To Rs.2/", 5.0), ("Bonus 1:2", 1.5)):
            with self.subTest(subject=subject):
                rows = normalize_actions([api(subject), api(subject, source_row=1)], [], observations(), [])
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].share_multiplier, multiplier)
                self.assertEqual(rows[0].duplicate_source_rows, 2)
                self.assertEqual(len(rows[0].citations), 2)
        rows = normalize_actions([api(), api("Fv Split Rs.10/- To Rs.2/", source_row=1)], [], observations(), [])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(not row.execution_ready for row in rows))
        self.assertTrue(all(row.readiness_reason == "conflicting_same_day_actions" for row in rows))
        for subject in ("Bonus 1:1 And Face Value Split From Rs.2/- To Re.1/-", "Bonus 1 Dvr : 10 Eq Share", "Sch Of Agmt- Bonus Deb1:1", "Fv Spl-Rs10tors2/Bon-12:1", "Bon 1:1/Fv Spl Rs.5tors.2"):
            self.assertFalse(normalize_actions([api(subject)], [], observations(), [])[0].execution_ready)

    def test_factor_plus_nonfactor_compounds_decompose(self):
        # Bonus+dividend / split+dividend / bonus+rights: the factor
        # component executes on its own explicit terms; the dividend /
        # rights component is a non-factor audit record.
        rows = normalize_actions([api("Div-30%/Bonus 1:1")], [], observations(), [])
        self.assertEqual({r.action_type for r in rows}, {"bonus", "dividend"})
        factor = next(r for r in rows if r.action_type == "bonus")
        other = next(r for r in rows if r.action_type == "dividend")
        self.assertTrue(factor.execution_ready)
        self.assertEqual(factor.share_multiplier, 2.0)
        self.assertFalse(other.execution_ready)
        self.assertTrue(other.non_factor)
        self.assertIsNone(other.share_multiplier)
        self.assertEqual(other.readiness_reason, "non_factor_record_no_price_adjustment")
        self.assertEqual(other.numeric_terms, {"dividend": {"percent_of_face_value": 30.0}})
        rows = normalize_actions([api("Bonus 1:2/Rights 1:1")], [], observations(), [])
        self.assertEqual({r.action_type for r in rows}, {"bonus", "rights"})
        rights = next(r for r in rows if r.action_type == "rights")
        self.assertTrue(rights.non_factor)
        self.assertEqual(rights.numeric_terms["rights"]["rights_shares"], 1.0)
        # Split+bonus stays one fail-closed compound record.
        rows = normalize_actions([api("Bonus 1:1 And Face Value Split From Rs.2/- To Re.1/-")], [], observations(), [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].readiness_reason, "compound_action_ambiguous")
        self.assertTrue(rows[0].evidence_required)

    def test_bare_face_value_and_bare_new_value_splits(self):
        row = normalize_actions([api("Fv Spl 100 To 10/Div150%")], [], observations(), [])[0]
        self.assertEqual(row.action_type, "split")
        self.assertEqual(row.share_multiplier, 10.0)
        row = normalize_actions([api("Fv Spl-Rs10to2/Div-Rs2.50")], [], observations(), [])[0]
        self.assertEqual(row.share_multiplier, 5.0)

    def test_rights_and_scheme_require_explicit_structured_terms(self):
        # Rights with an explicit ratio are structured audit records even
        # without an issue price; price-only or label-only subjects are
        # retained for audit but never count as terms-verified.
        row = normalize_actions([api("Rights 3:10")], [], observations(), [])[0]
        self.assertTrue(row.non_factor)
        self.assertTrue(row.terms_verified)
        self.assertEqual(row.numeric_terms["rights"]["rights_shares"], 3.0)
        for subject in ("Rights-1 Pcd:2 Eq @Rs.400", "Rights Equity/Warrant",
                        "To Distribute Net Proceeds From The Sale Of Rights Entitlement"):
            with self.subTest(subject=subject):
                rows = normalize_actions([api(subject)], [], observations(), [])
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0].non_factor)
                self.assertFalse(rows[0].terms_verified)
                self.assertIsNone(rows[0].share_multiplier)
                self.assertFalse(rows[0].execution_ready)
                self.assertEqual(rows[0].readiness_reason,
                                 "missing_explicit_supported_numeric_terms")
        # A scheme label states a kind, not numeric terms.
        rows = normalize_actions([api("Scheme Of Arrangement")], [], observations(), [])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].non_factor)
        self.assertFalse(rows[0].terms_verified)
        self.assertEqual(rows[0].readiness_reason,
                         "missing_explicit_supported_numeric_terms")

    def test_standalone_rights_and_scheme_are_non_factor_records(self):
        rows = normalize_actions([api("Rights 1:4 @Prem Rs.480/-")], [], observations(), [])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].non_factor)
        self.assertFalse(rows[0].execution_ready)
        self.assertIsNone(rows[0].share_multiplier)
        self.assertEqual(rows[0].numeric_terms["rights"]["issue_price_stated"], 480.0)
        rows = normalize_actions([api("Scheme Of Arrangement")], [], observations(), [])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].non_factor)
        self.assertEqual(rows[0].action_type, "scheme")

    def test_identity_uses_dated_edges_exact_series_and_never_backfills_isin(self):
        edge = {"old_symbol": "OLD", "new_symbol": "AAA", "effective_date": "2011-07-01",
                "edge_id": "OLD->AAA@2011-07-01", "verification": "official_symbolchange_row"}
        row = normalize_actions([api()], [], observations("OLD"), [edge])[0]
        self.assertTrue(row.execution_ready)
        self.assertEqual(row.symbol, "OLD")
        self.assertEqual(row.identity_edges, ["OLD->AAA@2011-07-01"])
        self.assertIsNone(row.historical_isin)
        self.assertFalse(normalize_actions([api()], [], observations("OLD", "BE"), [edge])[0].execution_ready)
        self.assertFalse(normalize_actions([api()], [], observations("OLD"), [])[0].execution_ready)
        self.assertFalse(normalize_actions([api()], [], [{"symbol": "AAA", "series": "EQ", "date": "2011-01-03", "isin": "CURRENT_ISIN"}], [])[0].execution_ready)

    def test_identity_epoch_blocks_symbol_reuse_and_branch_ambiguity(self):
        incoming = {"old_symbol": "OLD", "new_symbol": "AAA", "effective_date": "2005-01-01",
                    "edge_id": "OLD->AAA@2005-01-01", "verification": "official_symbolchange_row"}
        obs = observations() + [{"symbol": "AAA", "series": "EQ", "date": "2000-01-03", "isin": None}]
        row = normalize_actions([api()], [], obs, [incoming])[0]
        self.assertTrue(row.execution_ready)
        self.assertEqual(row.pre_ex_bars, 1)
        self.assertEqual(row.identity_start, date(2006, 1, 2))
        branch = {**incoming, "old_symbol": "OTHER", "edge_id": "OTHER->AAA@2005-01-01"}
        self.assertFalse(normalize_actions([api()], [], obs, [incoming, branch])[0].execution_ready)

    def test_normalized_factor_cannot_leak_before_observed_identity_epoch(self):
        row = normalize_actions([api()], [], observations(), [])[0]
        actions = pd.DataFrame([row.adjustment_row()])
        bars = pd.DataFrame([
            {"symbol": "AAA", "series": "EQ", "date": when, "open": 100.0, "high": 100.0,
             "low": 100.0, "close": 100.0, "previous_close": 100.0}
            for when in ("2000-01-03", "2006-01-02", "2006-01-03")
        ])
        result = apply_adjustment(bars, actions)
        self.assertEqual(result["adjustment_factor"].tolist(), [1.0, 2.0, 1.0])
        intervals = build_intervals(actions)
        self.assertEqual(intervals["factor"].tolist(), [1.0, 2.0, 1.0])

    def test_separate_same_day_dividend_is_non_factor_alongside_factor(self):
        # A dividend on the same day is a separate non-factor audit
        # record; it is not a competing factor and must not hide or
        # block the bonus factor, nor be converted into a factor.
        rows = normalize_actions([api(), api("Dividend Rs.2/- Per Share", source_row=1)], [], observations(), [])
        self.assertEqual(len(rows), 2)
        bonus = next(r for r in rows if r.action_type == "bonus")
        dividend = next(r for r in rows if r.action_type == "dividend")
        self.assertTrue(bonus.execution_ready)
        self.assertEqual(bonus.share_multiplier, 2.0)
        self.assertFalse(bonus.non_factor)
        self.assertFalse(dividend.execution_ready)
        self.assertTrue(dividend.non_factor)
        self.assertIsNone(dividend.share_multiplier)
        self.assertEqual(dividend.readiness_reason,
                         "non_factor_record_no_price_adjustment")
        self.assertEqual(dividend.numeric_terms,
                         {"dividend": {"amount_per_share": 2.0}})
        self.assertTrue(dividend.evidence_required)
        self.assertEqual(len(bonus.citations), 1)
        self.assertEqual(len(dividend.citations), 1)

    def test_separate_same_day_rights_is_non_factor_alongside_factor(self):
        # Rights is carried as a non-factor audit record in this pipeline
        # (stated ratio/price, share_multiplier is never derived), so it
        # is not a competing split/bonus factor and must not block, share
        # citations with, or be converted alongside a bonus factor.
        rows = normalize_actions(
            [api(), api("Rights 1:4 @Prem Rs.480/-", source_row=1)], [], observations(), [])
        self.assertEqual(len(rows), 2)
        bonus = next(r for r in rows if r.action_type == "bonus")
        rights = next(r for r in rows if r.action_type == "rights")
        self.assertTrue(bonus.execution_ready)
        self.assertEqual(bonus.share_multiplier, 2.0)
        self.assertFalse(bonus.non_factor)
        self.assertFalse(rights.execution_ready)
        self.assertTrue(rights.non_factor)
        self.assertIsNone(rights.share_multiplier)
        self.assertEqual(rights.readiness_reason,
                         "non_factor_record_no_price_adjustment")
        self.assertEqual(rights.numeric_terms["rights"]["rights_shares"], 1.0)
        self.assertEqual(rights.numeric_terms["rights"]["existing_shares"], 4.0)
        self.assertEqual(len(bonus.citations), 1)
        self.assertEqual(len(rights.citations), 1)

    def test_separate_same_day_non_factor_does_not_mask_factor_conflict(self):
        # A dividend sitting alongside two distinct factor rows does not
        # turn the non-factor into a factor, and must not mask the
        # split-vs-bonus conflict: both factors still fail closed, the
        # dividend remains a plain non-factor audit record. Exact
        # duplicates of the bonus, by contrast, keep collapsing to one
        # ready factor when only a dividend sits alongside them.
        rows = normalize_actions(
            [api(), api("Dividend Rs.2/- Per Share", source_row=1),
             api("Fv Split Rs.10/- To Rs.2/", source_row=2)], [], observations(), [])
        self.assertEqual(len(rows), 3)
        factors = [r for r in rows if not r.non_factor]
        dividend = next(r for r in rows if r.action_type == "dividend")
        self.assertEqual({r.action_type for r in factors}, {"bonus", "split"})
        self.assertTrue(all(not r.execution_ready for r in factors))
        self.assertTrue(all(r.readiness_reason == "conflicting_same_day_actions"
                            for r in factors))
        self.assertTrue(dividend.non_factor)
        self.assertEqual(dividend.readiness_reason,
                         "non_factor_record_no_price_adjustment")
        # A mixed factor+dividend row still supplies a competing factor.
        rows = normalize_actions(
            [api("Div-30%/Bonus 1:1"), api("Fv Split Rs.10/- To Rs.2/", source_row=1)],
            [], observations(), [])
        factors = [r for r in rows if not r.non_factor]
        self.assertEqual(len(factors), 2)
        self.assertTrue(all(not r.execution_ready for r in factors))
        # Exact duplicates alongside a dividend collapse and stay ready.
        rows = normalize_actions(
            [api(), api(source_row=1), api("Dividend Rs.2/- Per Share", source_row=2)],
            [], observations(), [])
        self.assertEqual(len(rows), 2)
        bonus = next(r for r in rows if r.action_type == "bonus")
        self.assertTrue(bonus.execution_ready)
        self.assertEqual(bonus.duplicate_source_rows, 2)
        self.assertEqual(len(bonus.citations), 2)

    def test_raw_candidate_cannot_override_a_normalized_compound_conflict(self):
        compound = normalize_actions([api("Bonus 1:1/Fv Split Rs.10/- To Rs.2/")], [], observations(), [])[0]
        raw = {**compound.adjustment_row(), "action_type": "bonus", "purpose": "Bonus 1:1",
               "share_multiplier": 2.0, "parse_status": "parsed_not_applied",
               "verified_action_id": None, "verified_readiness_reason": None, "source": "raw"}
        result = classify_actions(pd.DataFrame([raw, compound.adjustment_row()]),
                                  {("AAA", "EQ", date(2006, 1, 3)): 1})
        self.assertTrue((result["decision"] == "excluded").all())

    def test_run_adjustment_imports_pre2011_source_without_changing_raw_database(self):
        import duckdb

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = root / "market.duckdb"
            con = duckdb.connect(str(db))
            con.execute("CREATE TABLE corporate_actions(symbol VARCHAR, series VARCHAR, ex_date DATE, record_date DATE, purpose VARCHAR, action_type VARCHAR, share_multiplier DOUBLE, dividend_per_share DOUBLE, parse_status VARCHAR, first_observed DATE, last_observed DATE, source VARCHAR)")
            con.execute("CREATE TABLE pre2011_actions(symbol VARCHAR, series VARCHAR, ex_date DATE, purpose VARCHAR, action_tags VARCHAR[])")
            con.execute("INSERT INTO pre2011_actions VALUES ('AAA','EQ','2006-01-03','Bonus 1:1',['bonus'])")
            con.execute("CREATE TABLE bars(date DATE, symbol VARCHAR, series VARCHAR, isin VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, previous_close DOUBLE, volume BIGINT, traded_value DOUBLE, trades BIGINT, valid_ohlcv BOOLEAN, source VARCHAR)")
            con.execute("INSERT INTO bars VALUES ('2006-01-02','AAA','EQ',NULL,100,100,100,100,99,10,1000,1,true,'raw'),('2006-01-03','AAA','EQ',NULL,50,50,50,50,100,20,1000,1,true,'raw')")
            con.close()
            record = api()
            raw = {k: v for k, v in record.items() if k not in {"source_file", "source_row", "source_url", "ex_date", "action_tags"}}
            for path, content in (
                (root / "processed/older_actions/nse-api-actions.jsonl", json.dumps(record) + "\n"),
                (root / "raw/older_actions/nse-api-2006.json", json.dumps([raw])),
                (root / "processed/identity/identity_edges.jsonl", ""),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            digest = hashlib.sha256(db.read_bytes()).hexdigest()
            report = run_adjustment(root)
            self.assertEqual(report["pre2011"]["applied"], 1)
            out = pd.read_parquet(root / "processed/adjusted/adjusted_bars.parquet")
            self.assertEqual(out["adj_close"].tolist(), [50.0, 50.0])
            audit = pd.read_parquet(root / "processed/adjusted/action_audit.parquet")
            self.assertTrue(audit.iloc[0]["source"].startswith("normalized_verified:"))
            normalized_report = json.loads((root / "reports/verified-actions.json").read_text())
            self.assertEqual(normalized_report["actually_applied_actions"], 1)
            manifest = json.loads((root / "processed/adjusted/audit_manifest.json").read_text())
            self.assertEqual(manifest["pre2011_applied"], 1)
            self.assertEqual(manifest["applied_actions"], 1)
            self.assertEqual(manifest["bars_rows_adjusted_factor_ne1"], 1)
            self.assertEqual(len(manifest["files"]["adjusted_bars"]["sha256"]), 64)
            self.assertEqual(len(manifest["files"]["action_audit"]["sha256"]), 64)
            self.assertTrue(manifest["files"]["adjusted_bars"]["bytes"] > 0)
            self.assertIn("bounded", manifest["bounded_path"].lower())
            self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), digest)
            (root / "raw/older_actions/nse-api-2006.json").unlink()
            no_raw = run_adjustment(root)
            self.assertEqual(no_raw["pre2011"]["applied"], 0)
            self.assertEqual(no_raw["verified_actions"]["execution_ready_actions"], 0)
            unchanged = pd.read_parquet(root / "processed/adjusted/adjusted_bars.parquet")
            self.assertEqual(unchanged["adjustment_factor"].tolist(), [1.0, 1.0])
            self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
