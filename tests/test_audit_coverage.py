from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest.mock import patch

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.audit.calendar import (
    audit_coverage, audit_security_history, calendar_coverage_breakdown,
    calendar_remediation_report, classify_dates, fetch_sources,
    index_session, load_calendar, load_external_calendar, load_gap_backfill,
    load_index_evidence, read_snapshot, source_reference,
    validate_external_calendar_payload,
)

TEMP_ROOT = Path.home() / "AppData" / "Local" / "Temp" / "opencode"
if not TEMP_ROOT.exists():
    TEMP_ROOT = None


def observed(*days):
    return {date.fromisoformat(day): {"rows": 1, "symbols": 1, "positive_volume_valid_rows": 1} for day in days}


def index_csv(day="11-05-2022", volume="123"):
    return ("Index Name,Index Date,Closing Index Value,Volume\n"
            f"Nifty 50,{day},16000,{volume}\n").encode()


class DateClassificationTests(unittest.TestCase):
    def test_missing_weekday_is_not_a_holiday(self):
        result = classify_dates({}, date(2022, 5, 11), date(2022, 5, 11), {}, {}, {})
        self.assertEqual(result["missing_date_counts"], {"unresolved": 1})

    def test_only_positive_session_evidence_confirms_gap(self):
        day = date(2022, 5, 11)
        result = classify_dates({}, day, day, {day: [{"kind": "independent_index_session"}]}, {}, {})
        self.assertEqual(result["missing_dates"][0]["classification"], "confirmed_source_gap")

    def test_official_holiday_confirms_closure(self):
        day = date(2025, 12, 25)
        result = classify_dates({}, day, day, {}, {day: [{"kind": "official_cm_holiday"}]}, {})
        self.assertEqual(result["missing_date_counts"], {"confirmed_exchange_closure": 1})

    def test_index_session_overrides_holiday_calendar(self):
        day = date(2025, 10, 21)
        result = classify_dates({}, day, day, {day: [{"kind": "independent_index_session"}]}, {day: []}, {})
        self.assertEqual(result["missing_dates"][0]["classification"], "confirmed_source_gap")

    def test_weekend_special_missing_is_not_excluded(self):
        day = date(2024, 3, 2)
        result = classify_dates({}, day, day, {day: [{"kind": "independent_index_session"}]}, {}, {})
        self.assertTrue(result["missing_dates"][0]["weekend"])
        self.assertEqual(result["missing_date_counts"], {"confirmed_source_gap": 1})

    def test_announced_or_ambiguous_special_session_is_not_execution_proof(self):
        day = date(2026, 11, 8)
        for kind, expected in (("official_special_session_announcement", "unresolved"),
                               ("possible_special_session", "unresolved")):
            result = classify_dates({}, day, day, {}, {}, {day: [{"kind": kind}]})
            self.assertEqual(result["missing_dates"][0]["classification"], expected)

    def test_cutoff_weekend_without_evidence_is_unresolved(self):
        result = classify_dates(observed("2026-11-06"), date(2026, 11, 6), date(2026, 11, 7), {}, {}, {})
        self.assertEqual(result["missing_date_counts"], {"unresolved": 1})

    def test_absent_weekends_are_not_asserted_closed_and_observed_are_retained(self):
        result = classify_dates(observed("2024-03-02"), date(2024, 3, 2), date(2024, 3, 4), {}, {}, {})
        self.assertEqual(len(result["observed_weekend_sessions"]), 1)
        self.assertEqual(result["unobserved_weekends_without_independent_or_calendar_evidence"], 1)

    def test_evidence_after_cutoff_does_not_create_missing_date(self):
        result = classify_dates(observed("2026-11-06"), date(2026, 11, 6), date(2026, 11, 6),
                                {date(2026, 11, 8): []}, {}, {})
        self.assertEqual(result["missing_dates"], [])


class EvidenceTests(unittest.TestCase):
    def test_index_report_requires_positive_volume_and_broad_equity_index(self):
        self.assertTrue(index_session(index_csv(), date(2022, 5, 11)))
        self.assertFalse(index_session(index_csv(volume="0"), date(2022, 5, 11)))
        self.assertFalse(index_session(index_csv().replace(b"Nifty 50", b"India VIX"), date(2022, 5, 11)))

    def test_filename_alone_does_not_establish_session(self):
        with self.assertRaisesRegex(ValueError, "disagree"):
            index_session(index_csv(), date(2022, 5, 12))

    def test_legacy_slash_date_is_day_first(self):
        self.assertTrue(index_session(index_csv(day="01/09/2014"), date(2014, 9, 1)))

    def test_invalid_index_report_is_rejected_and_audit_can_continue(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            root = Path(temp)
            folder = root / "raw" / "calendar"
            folder.mkdir(parents=True)
            (folder / "ind_close_all_11052022.csv").write_bytes(index_csv())
            (folder / "ind_close_all_12052022.csv").write_bytes(index_csv())
            sessions, inventory = load_index_evidence(root)
            self.assertIn(date(2022, 5, 11), sessions)
            self.assertNotIn(date(2022, 5, 12), sessions)
            self.assertEqual(len(inventory["rejected_reports"]), 1)

    def test_muhurat_not_classified_as_full_closure_and_empty_calendar_not_complete(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            root = Path(temp)
            calendar = root / "raw" / "calendar"
            calendar.mkdir(parents=True)
            (calendar / "trading-holidays-2000.json").write_text("{}")
            (calendar / "trading-holidays-2025.json").write_text(json.dumps({"CM": [
                {"tradingDate": "21-Oct-2025", "description": "Diwali Laxmi Pujan"},
                {"tradingDate": "25-Dec-2025", "description": "Christmas"},
                {"tradingDate": "01-Feb-2025", "description": "Special Live Trading"},
            ]}))
            closures, special, inventory = load_calendar(root)
            self.assertNotIn(date(2025, 10, 21), closures)
            self.assertIn(date(2025, 10, 21), special)
            self.assertIn(date(2025, 12, 25), closures)
            self.assertNotIn(date(2025, 2, 1), closures)
            self.assertIn(date(2025, 2, 1), special)
            self.assertEqual(inventory["empty_cm_years"], [2000])

    def test_cache_provenance_checksum_is_checked(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            path = Path(temp) / "source.csv"
            path.write_bytes(b"original")
            path.with_suffix(".csv.metadata.json").write_text(json.dumps({
                "sha256": hashlib.sha256(b"original").hexdigest(), "url": "https://www.nseindia.com/example",
            }))
            self.assertIn("sha256", source_reference(path))
            path.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "checksum"):
                source_reference(path)

    def test_official_calendar_announcement_parsed_without_weekend_inference(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            root = Path(temp)
            calendar = root / "raw" / "calendar"
            calendar.mkdir(parents=True)
            (calendar / "exchange-holidays.html").write_text(
                "Muhurat Trading will be conducted on Sunday, <b>November 08, 2026</b> on account of Diwali")
            _, special, _ = load_calendar(root)
            self.assertEqual(special[date(2026, 11, 8)][0]["kind"], "official_special_session_announcement")
            self.assertNotIn(date(2026, 11, 7), special)

    def test_fetch_respects_denial_and_records_failure(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            root = Path(temp)
            error = urllib.error.HTTPError("https://www.nseindia.com", 403, "Forbidden", {}, None)
            with patch("nse_pipeline.audit.calendar.urllib.request.urlopen", side_effect=error) as fetch:
                fetch_sources(root, date(2025, 1, 1), date(2026, 11, 7))
                self.assertEqual(fetch.call_count, 1)
            metadata = json.loads((root / "raw/calendar/exchange-holidays.html.metadata.json").read_text())
            self.assertEqual(metadata["http_status"], 403)


class SnapshotTests(unittest.TestCase):
    def make_database(self, root):
        with duckdb.connect(str(root / "market.duckdb")) as db:
            db.execute("""CREATE TABLE bars(date DATE, symbol VARCHAR, series VARCHAR, isin VARCHAR,
                       valid_ohlcv BOOLEAN, volume BIGINT)""")
            db.execute("""INSERT INTO bars VALUES
                ('2024-03-01','OLDFAILED','EQ',NULL,true,100),
                ('2024-03-02','OLDFAILED','BE',NULL,true,100),
                ('2024-03-04','TODAY','EQ','INE000000001',true,100),
                ('2024-03-05','FUTURE','EQ','INE000000002',true,100)""")

    def test_arbitrary_cutoff_excludes_future_and_closes_database(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            root = Path(temp)
            self.make_database(root)
            before = (root / "market.duckdb").read_bytes()
            snapshot, _, history = read_snapshot(root / "market.duckdb", None, date(2024, 3, 4))
            self.assertEqual(snapshot["rows"], 3)
            self.assertEqual({item["symbol"] for item in history}, {"OLDFAILED", "TODAY"})
            self.assertEqual((root / "market.duckdb").read_bytes(), before)
            with duckdb.connect(str(root / "market.duckdb")) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM bars").fetchone()[0], 4)

    def test_official_delisted_names_retained_without_current_master_filter(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            root = Path(temp)
            folder = root / "raw" / "security_master"
            folder.mkdir(parents=True)
            (folder / "delisted.csv").write_text(
                "Symbol,Company,Delisted Date,Type of Delisting\n"
                "OLDFAILED,Old Failed Ltd,01-Mar-24,Compulsory Delisting\n"
                "UNOBSERVED,Unobserved Ltd,01-Mar-24,Voluntary Delisting\n"
                "FUTURE,Future Ltd,05-Mar-24,Voluntary Delisting\n")
            (folder / "EQUITY_L.csv").write_text("SYMBOL,NAME OF COMPANY\nTODAY,Today Ltd\n")
            history = [{"symbol": "OLDFAILED", "first_observed": date(2024, 3, 1)}]
            result = audit_security_history(root, history, date(2024, 3, 4))
            self.assertEqual(result["matched_delisted_symbol_count"], 1)
            self.assertEqual(result["unmatched_delisted_symbol_count"], 1)
            self.assertEqual(result["official_delisted_matches"][0]["symbol"], "OLDFAILED")
            self.assertEqual(history[0]["symbol"], "OLDFAILED")

    def test_delisted_csv_preserves_legacy_exchange_encoding(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            root = Path(temp)
            folder = root / "raw" / "security_master"
            folder.mkdir(parents=True)
            (folder / "delisted.csv").write_bytes(
                b"Symbol,Company,Delisted Date,Type of Delisting\nOLDFAILED,Old\xa0Failed,01-Mar-24,Compulsory\n")
            result = audit_security_history(root, [{"symbol": "OLDFAILED"}], date(2024, 3, 4))
            self.assertEqual(result["official_delisted_matches"][0]["company"], "Old\xa0Failed")

    def test_reports_written_without_mutating_database_or_inventing_evidence(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            root = Path(temp)
            self.make_database(root)
            before = (root / "market.duckdb").read_bytes()
            report = audit_coverage(root, date(2024, 3, 4))
            self.assertFalse(report["complete_survivorship_free_coverage_established"])
            self.assertEqual(report["snapshot"]["sessions"], 3)
            self.assertEqual(len(report["sessions"]["observed_weekend_sessions"]), 1)
            self.assertEqual((root / "market.duckdb").read_bytes(), before)
            self.assertTrue((root / "reports" / "coverage-audit.json").exists())
            self.assertIn("Limits", (root / "reports" / "coverage-audit.md").read_text())


class OfficialEvidenceTests(unittest.TestCase):
    def test_archived_official_page_confirms_closure_but_keeps_laxmi_puja_special(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            root = Path(temp)
            calendar = root / "raw" / "calendar"
            calendar.mkdir(parents=True)
            (calendar / "nse-archived-eq-holidays.json").write_text(json.dumps({
                "years": {"2002": {
                    "capture_timestamp": "20020806003303",
                    "url": "https://web.archive.org/web/20020806003303id_/http://www.nseindia.com/content/equities/eq_holidays.htm",
                    "rows": [
                        {"date": "2002-03-25", "source_date": "25-Mar-2002", "description": "Moharrum"},
                        {"date": "2002-11-04", "source_date": "04-Nov-2002", "description": "Diwali (Laxmi Puja) *", "special": True},
                    ],
                }}}))
            closures, special, inventory = load_calendar(root)
            self.assertIn(date(2002, 3, 25), closures)
            self.assertIn(date(2002, 11, 4), special)
            self.assertNotIn(date(2002, 11, 4), closures)
            self.assertEqual(inventory["archived_years_with_cm_calendar"], [2002])
            closure = classify_dates({}, date(2002, 3, 25), date(2002, 3, 25), {}, closures, special)
            self.assertEqual(closure["missing_date_counts"], {"confirmed_exchange_closure": 1})
            special_only = classify_dates({}, date(2002, 11, 4), date(2002, 11, 4), {}, closures, special)
            self.assertEqual(special_only["missing_date_counts"], {"unresolved": 1})

    def test_archived_calendar_year_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            root = Path(temp)
            calendar = root / "raw" / "calendar"
            calendar.mkdir(parents=True)
            (calendar / "nse-archived-eq-holidays.json").write_text(json.dumps({
                "years": {"2002": {"capture_timestamp": "x", "url": "https://example.test",
                                   "rows": [{"date": "2003-01-26", "description": "Republic Day"}]}}}))
            with self.assertRaisesRegex(ValueError, "year mismatch"):
                load_calendar(root)

    def test_backfill_download_confirms_gap_but_404_alone_does_not_confirm_closure(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            root = Path(temp)
            reports = root / "reports"
            reports.mkdir(parents=True)
            (reports / "official-gap-backfill.json").write_text(json.dumps({
                "candidate_count": 2,
                "summary": {"absent": 1, "downloaded": 1},
                "blocked": False,
                "downloaded_sessions": ["2024-03-02"],
                "absent_dates": ["2024-03-04"],
                "results": [
                    {"date": "2024-03-02", "status": "downloaded", "url": "https://example.test/a.zip",
                     "bytes": 10, "sha256": "abc"},
                    {"date": "2024-03-04", "status": "absent", "http_status": 404,
                     "url": "https://example.test/b.zip"},
                ],
            }))
            sessions, absent, inventory = load_gap_backfill(root)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(len(absent), 1)
            self.assertFalse(inventory["notes"])
            result = classify_dates({}, date(2024, 3, 2), date(2024, 3, 4), {}, {}, {},
                                    official_sessions=sessions, archive_absence=absent)
            by_date = {item["date"]: item for item in result["missing_dates"]}
            self.assertEqual(by_date[date(2024, 3, 2)]["classification"], "confirmed_source_gap")
            self.assertEqual(by_date[date(2024, 3, 4)]["classification"], "unresolved")
            self.assertIn("official_archive_absent", by_date[date(2024, 3, 4)]["evidence_kinds"])
            self.assertEqual(result["archive_absence_evidence_count"], 1)

    def test_archive_absence_does_not_override_official_closure(self):
        day = date(2024, 3, 4)
        result = classify_dates({}, day, day, {}, {day: [{"kind": "official_cm_holiday"}]}, {},
                                archive_absence={day: [{"kind": "official_archive_absent", "http_status": 404}]})
        self.assertEqual(result["missing_date_counts"], {"confirmed_exchange_closure": 1})
        self.assertIn("official_archive_absent", result["missing_dates"][0]["evidence_kinds"])

    def test_weekend_scope_flags_thin_equity_reports(self):
        day = date(2024, 3, 2)
        observed = {day: {"rows": 7, "symbols": 7, "positive_volume_valid_rows": 7,
                          "equity_candidate_symbols": 7, "equity_candidate_rows": 7, "series": "EQ"}}
        result = classify_dates(observed, day, day, {}, {}, {})
        self.assertEqual(result["observed_weekend_sessions"][0]["scope_assessment"],
                         "thin_equity_session_review")
        self.assertEqual(result["observed_weekend_scope_counts"], {"thin_equity_session_review": 1})

    def test_delisting_coverage_exposes_stale_list_and_unmatched_ending_symbols(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            root = Path(temp)
            folder = root / "raw" / "security_master"
            folder.mkdir(parents=True)
            (folder / "delisted.csv").write_text(
                "Symbol,Company,Delisted Date,Type of Delisting\n"
                "OLDFAILED,Old Failed Ltd,09-Nov-20,Voluntary Delisting\n")
            history = [
                {"symbol": "OLDFAILED", "first_observed": date(2000, 1, 3),
                 "last_observed": date(2020, 11, 6)},
                {"symbol": "ENDED", "first_observed": date(2000, 1, 3),
                 "last_observed": date(2024, 1, 1)},
            ]
            result = audit_security_history(root, history, date(2026, 11, 7))
            coverage = result["delisting_coverage"]
            self.assertGreater(coverage["last_delisting_age_days"], 2000)
            self.assertEqual(coverage["matched_symbols"], 1)
            self.assertEqual(coverage["matched_last_observed_within_30_days"], 1)
            self.assertEqual(coverage["db_symbols_ending_before_cutoff_30d"], 2)
            self.assertEqual(coverage["db_ending_symbols_not_in_official_list"], 1)
            self.assertEqual(coverage["db_ending_symbols_not_in_official_list_examples"][0]["symbol"], "ENDED")


def external_payload(entries, sources=None):
    return {"schema_version": 1,
            "sources": sources if sources is not None else [
                {"year": 2000, "official_url": "https://www.nseindia.com/resources/exchange-communication-holidays",
                 "publisher": "NSE"}],
            "entries": entries}


class ExternalCalendarTests(unittest.TestCase):
    def test_absent_manifest_resolves_nothing(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            closures, special, inventory = load_external_calendar(Path(temp))
            self.assertEqual(closures, {})
            self.assertEqual(special, {})
            self.assertFalse(inventory["external_calendar_present"])
            self.assertEqual(inventory["external_calendar_entry_count"], 0)

    def test_empty_manifest_validates_to_no_evidence(self):
        closures, special, inventory = validate_external_calendar_payload(external_payload([]))
        self.assertEqual((closures, special), ({}, {}))
        self.assertEqual(inventory["external_calendar_entry_count"], 0)

    def test_exact_official_closure_entry_resolves_date(self):
        payload = external_payload([{
            "date": "2000-01-26", "description": "Republic Day", "session_type": "closed",
            "official_url": "https://www.nseindia.com/resources/exchange-communication-holidays",
            "source_year": 2000}])
        closures, special, inventory = validate_external_calendar_payload(payload)
        self.assertEqual(inventory["external_calendar_years"], [2000])
        result = classify_dates({}, date(2000, 1, 26), date(2000, 1, 26), {}, closures, special)
        self.assertEqual(result["missing_date_counts"], {"confirmed_exchange_closure": 1})
        self.assertEqual(result["missing_dates"][0]["evidence"][0]["kind"], "external_official_cm_holiday")

    def test_special_session_entry_does_not_resolve_as_closure(self):
        payload = external_payload([{
            "date": "2000-10-26", "description": "Diwali Laxmi Pujan", "session_type": "special_session",
            "official_url": "https://www.nseindia.com/resources/exchange-communication-holidays"}])
        closures, special, _ = validate_external_calendar_payload(payload)
        self.assertNotIn(date(2000, 10, 26), closures)
        result = classify_dates({}, date(2000, 10, 26), date(2000, 10, 26), {}, closures, special)
        self.assertEqual(result["missing_date_counts"], {"unresolved": 1})

    def test_validator_rejects_non_official_and_weak_evidence(self):
        base = {"date": "2000-01-26", "description": "Republic Day", "session_type": "closed",
                "official_url": "https://www.nseindia.com/resources/exchange-communication-holidays"}
        for bad in (
            {**base, "official_url": "https://example.test/holidays"},
            {**base, "description": " "},
            {**base, "date": "26-01-2000"},
            {**base, "source_year": 2001},
            {**base, "session_type": "probably_closed"},
            {**base, "basis": "http_404", "official_url": "https://example.test/x"},
            {**base, "http_status": 404, "official_url": "https://example.test/x"},
        ):
            with self.assertRaises(ValueError, msg=str(bad)):
                validate_external_calendar_payload(external_payload([bad]))
        with self.assertRaises(ValueError):
            validate_external_calendar_payload({"schema_version": 99, "sources": [], "entries": []})

    def test_validator_rejects_conflicting_and_dedupes_identical(self):
        entry = {"date": "2000-01-26", "description": "Republic Day", "session_type": "closed",
                 "official_url": "https://www.nseindia.com/x"}
        closures, _, inventory = validate_external_calendar_payload(external_payload([entry, dict(entry)]))
        self.assertEqual(len(closures[date(2000, 1, 26)]), 1)
        self.assertEqual(inventory["external_calendar_entry_count"], 1)
        with self.assertRaises(ValueError):
            validate_external_calendar_payload(external_payload(
                [entry, {**entry, "session_type": "special_session"}]))

    def test_load_calendar_merges_external_manifest(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            root = Path(temp)
            calendar = root / "raw" / "calendar"
            calendar.mkdir(parents=True)
            (calendar / "external-official-holidays.json").write_text(json.dumps(external_payload([{
                "date": "2007-01-26", "description": "Republic Day", "session_type": "closed",
                "official_url": "https://www.nseindia.com/resources/exchange-communication-holidays"}])))
            closures, _, inventory = load_calendar(root)
            self.assertIn(date(2007, 1, 26), closures)
            self.assertTrue(inventory["external_calendar_present"])
            self.assertEqual(inventory["external_calendar_years"], [2000, 2007])


class DerivedBreakdownTests(unittest.TestCase):
    def test_coverage_breakdown_is_derived_not_hardcoded(self):
        inventory = {"years_with_cm_calendar": [2011, 2012],
                     "archived_years_with_cm_calendar": [2002],
                     "external_calendar_years": [2007], "external_calendar_present": True,
                     "external_calendar_entry_count": 3}
        sessions = {"missing_dates": [
            {"date": date(2000, 1, 26), "classification": "unresolved", "weekend": False},
            {"date": date(2002, 12, 25), "classification": "unresolved", "weekend": False},
            {"date": date(2011, 1, 26), "classification": "confirmed_exchange_closure", "weekend": False}],
            "unassessed_weekends_by_year": {"2000": 100, "2011": 50}}
        result = calendar_coverage_breakdown(date(2000, 1, 3), date(2012, 12, 31), inventory, sessions)
        self.assertEqual(result["years_without_any_cm_calendar"],
                         [2000, 2001, 2003, 2004, 2005, 2006, 2008, 2009, 2010])
        self.assertEqual(result["unresolved_by_year"], {2000: 1, 2002: 1})
        self.assertEqual(result["unresolved_in_years_without_any_calendar"], 1)
        self.assertEqual(result["unresolved_in_years_with_calendar"], 1)
        self.assertEqual(result["unassessed_weekends_by_year"], {2000: 100, 2011: 50})

    def test_unassessed_weekends_counted_by_year(self):
        result = classify_dates(observed("2024-03-02"), date(2024, 3, 2), date(2024, 3, 4), {}, {}, {})
        self.assertEqual(result["unassessed_weekends_by_year"], {2024: 1})
        self.assertEqual(result["unobserved_weekends_without_independent_or_calendar_evidence"], 1)

    def test_remediation_report_derives_counts_from_report(self):
        report = {
            "snapshot": {"requested_start": "2000-01-03", "requested_cutoff": "2026-11-07",
                         "first_observed": "2000-01-03", "last_observed": "2026-11-06"},
            "sessions": {"missing_date_counts": {"unresolved": 2, "confirmed_exchange_closure": 1},
                         "within_observed_span_counts": {"unresolved": 2},
                         "missing_dates": [
                             {"date": date(2000, 1, 26), "classification": "unresolved", "weekend": False},
                             {"date": date(2026, 11, 7), "classification": "unresolved", "weekend": True},
                             {"date": date(2011, 1, 26), "classification": "confirmed_exchange_closure",
                              "weekend": False}],
                         "unobserved_weekends_without_independent_or_calendar_evidence": 7,
                         "unassessed_weekends_by_year": {2000: 7},
                         "observed_weekend_session_count": 1,
                         "observed_weekend_scope_counts": {"broad_equity_session": 1},
                         "observed_weekend_sessions": [{"date": date(2024, 3, 2)}]},
            "calendar_inventory": {"years_with_cm_calendar": [2011], "archived_years_with_cm_calendar": [],
                                   "external_calendar_present": False, "external_calendar_entry_count": 0,
                                   "external_calendar_years": []},
        }
        remediation = calendar_remediation_report(report)
        self.assertEqual(remediation["unresolved_count"], 2)
        self.assertEqual(remediation["unresolved_dates"], ["2000-01-26", "2026-11-07"])
        # 2000 has no calendar, 2026 has the 2011-API-only inventory absent -> also no calendar.
        self.assertEqual(remediation["unresolved_in_years_without_any_calendar"], 2)
        self.assertEqual(remediation["unresolved_in_years_with_calendar"], 0)
        self.assertIn(2000, remediation["years_without_any_cm_calendar"])
        self.assertEqual(remediation["unassessed_absent_weekends"], 7)
        self.assertFalse(remediation["external_evidence_manifest"]["present"])
        self.assertEqual(remediation["external_evidence_manifest"]["schema_version"], 1)
        search = remediation["official_source_search"]
        self.assertEqual(search["status"], "unresolved_no_official_source_obtained")
        self.assertEqual([item["year"] for item in search["per_year"]],
                         remediation["years_without_any_cm_calendar"])
        self.assertTrue(all(item["entries_added"] == 0 and not item["official_annual_list_obtained"]
                            for item in search["per_year"]))
        self.assertEqual(sum(item["unresolved_count"] for item in search["per_year"]),
                         remediation["unresolved_in_years_without_any_calendar"])


if __name__ == "__main__":
    unittest.main()
