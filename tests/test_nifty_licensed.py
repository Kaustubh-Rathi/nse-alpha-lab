"""Synthetic fixtures only: these are not historical Nifty constituents."""
from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.nifty.licensed import (  # noqa: E402
    OPTIONAL_COLUMNS, REQUIRED_COLUMNS, import_licensed_csv, main,
)
from nse_pipeline.nifty.purchase import (  # noqa: E402
    EMAIL_BODY, RECIPIENT, SUBJECT, build_pack, decide_purchase,
    render_sample_request, validate_email, validate_gap_request,
)

TEMP_ROOT = Path.home() / "AppData" / "Local" / "Temp" / "opencode"
if not TEMP_ROOT.exists():
    TEMP_ROOT = None


def member(**changes) -> dict:
    result = {
        "index": "NIFTY50", "symbol": "TESTEQ", "isin": "INE000A01010",
        "effective_from": "2008-01-31", "effective_to": "2009-01-01",
        "source": "synthetic-test-delivery",
    }
    result.update(changes)
    return result


def claim(index="NIFTY50", start="2008-01-31", end="2009-01-01", complete=True):
    return {"index": index, "start": start, "end": end,
            "complete": complete, "reference": "test-only-coverage-attestation"}


class LicensedImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / "licensed.csv"

    def manifest(self, coverage=None):
        return {
            "licence_reference": "external-contract-reference-TEST-ONLY",
            "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "membership_kind": "licensed_effective_intervals",
            "interval_semantics": "[effective_from,effective_to)",
            "coverage": [claim()] if coverage is None else coverage,
        }

    def write_rows(self, rows, columns=REQUIRED_COLUMNS):
        with self.source.open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)

    def load(self, rows=None, coverage=None, columns=REQUIRED_COLUMNS):
        self.write_rows([member()] if rows is None else rows, columns)
        return import_licensed_csv(self.source, self.manifest(coverage))

    def test_six_column_schema_imports_without_promotion(self):
        history = self.load()
        self.assertEqual(history.members[0].symbol, "TESTEQ")
        self.assertFalse(history.has_known_at)
        self.assertEqual(history.licence_reference, "external-contract-reference-TEST-ONLY")
        self.assertEqual(len(list(Path(self.tmp.name).iterdir())), 1)

    def test_half_open_boundaries_and_adjacent_rename(self):
        rows = [member(effective_to="2008-06-01"),
                member(symbol="TESTNEW", effective_from="2008-06-01")]
        history = self.load(rows)
        self.assertEqual(history.membership_asof("NIFTY50", "2008-05-31")[0].symbol, "TESTEQ")
        self.assertEqual(history.membership_asof("NIFTY50", "2008-06-01")[0].symbol, "TESTNEW")
        for day in ("2008-01-30", "2009-01-01"):
            with self.subTest(day=day), self.assertRaisesRegex(ValueError, "no complete licensed"):
                history.membership_asof("NIFTY50", day)
            self.assertEqual(history.membership_asof("NIFTY50", day, require_complete=False), ())

    def test_no_today_member_backfill(self):
        history = self.load([member(effective_from="2026-01-01", effective_to="")],
                            [claim(start="2026-01-01", end="2026-11-08")])
        self.assertEqual(history.membership_asof("NIFTY50", "2000-01-03", require_complete=False), ())
        with self.assertRaisesRegex(ValueError, "full-history claim rejected"):
            history.require_full_history("2000-01-01", "2026-11-08")
        # An open-ended row is not an assertion of delivery completeness forever.
        with self.assertRaisesRegex(ValueError, "no complete licensed"):
            history.membership_asof("NIFTY50", "2027-01-01")

    def test_missing_2000_coverage_rejects_full_history(self):
        rows = [member(effective_to="2026-11-08"),
                member(index="NIFTY500", effective_from="2013-04-18", effective_to="2026-11-08")]
        history = self.load(rows, [claim(end="2026-11-08"),
                                  claim("NIFTY500", "2013-04-18", "2026-11-08")])
        report = history.coverage_report("2000-01-01", "2026-11-08")
        self.assertFalse(report["full_history"])
        self.assertEqual(report["indices"]["NIFTY50"]["missing"],
                         [{"start": "2000-01-01", "end": "2008-01-31"}])
        self.assertEqual(report["indices"]["NIFTY500"]["missing"],
                         [{"start": "2000-01-01", "end": "2013-04-18"}])
        with self.assertRaisesRegex(ValueError, "full-history claim rejected"):
            history.require_full_history("2000-01-01", "2026-11-08")

    def test_coverage_reports_internal_gaps_and_absent_index(self):
        history = self.load(coverage=[claim(end="2008-06-01"),
                                     claim(start="2008-07-01")])
        report = history.coverage_report("2008-01-31", "2009-01-01")
        self.assertEqual(report["indices"]["NIFTY50"]["missing"],
                         [{"start": "2008-06-01", "end": "2008-07-01"}])
        self.assertFalse(report["indices"]["NIFTY500"]["complete"])

    def test_partial_or_empty_attestations_never_prove_coverage(self):
        for coverage in ([], [claim(complete=False)]):
            with self.subTest(coverage=coverage):
                history = self.load(coverage=coverage)
                self.assertFalse(history.coverage_report("2008-01-31", "2009-01-01")["full_history"])
                with self.assertRaisesRegex(ValueError, "no complete licensed"):
                    history.membership_asof("NIFTY50", "2008-02-01")
                self.assertEqual(len(history.membership_asof(
                    "NIFTY50", "2008-02-01", require_complete=False)), 1)

    def test_attested_full_effective_coverage_not_point_in_time_proof(self):
        history = self.load()
        report = history.require_full_history("2008-01-31", "2009-01-01", ("NIFTY50",))
        self.assertTrue(report["full_history"])
        self.assertFalse(report["point_in_time_completeness_proven"])

    def test_attestation_cannot_extend_beyond_rows_or_ignore_empty_dates(self):
        for rows, coverage in (
                ([member()], [claim(start="2000-01-01")]),
                ([member(effective_to="2008-02-01"),
                  member(effective_from="2008-03-01")], [claim()]),
                ([member()], [claim("NIFTY500")])):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "no constituent rows"):
                self.load(rows, coverage)

    def test_known_at_filter_is_timezone_aware_and_inclusive(self):
        history = self.load([member(known_at="2008-02-01T09:00:00+05:30")],
                            columns=REQUIRED_COLUMNS + ("known_at",))
        self.assertTrue(history.has_known_at)
        self.assertEqual(history.membership_asof(
            "NIFTY50", "2008-02-01", known_at="2008-02-01T03:29:59Z"), ())
        self.assertEqual(len(history.membership_asof(
            "NIFTY50", "2008-02-01", known_at="2008-02-01T03:30:00Z")), 1)

    def test_known_at_query_without_evidence_fails(self):
        history = self.load()
        with self.assertRaisesRegex(ValueError, "requires timestamps"):
            history.membership_asof("NIFTY50", "2008-02-01", known_at="2008-02-01T00:00:00Z")

    def test_partial_unknown_or_naive_timestamps_fail(self):
        for timestamp in ("", "2008-01-01", "2008-01-01T12:00:00", "2008-01-01 12:00:00Z",
                          "2008-13-01T00:00:00Z", "20080101T120000Z"):
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(ValueError, "known_at"):
                self.load([member(known_at=timestamp)], columns=REQUIRED_COLUMNS + ("known_at",))

    def test_dummy_and_dvr_are_preserved_without_company_level_collapsing(self):
        rows = [member(security_type="equity", tradable="true"),
                member(symbol="TESTDVR", isin="INE000B01018", security_type="dvr", tradable="true"),
                member(symbol="DUMMYTEST", isin="", security_type="dummy", tradable="false"),
                member(symbol="TESTHALT", isin="INE000C01016", security_type="equity", tradable="false")]
        history = self.load(rows, columns=REQUIRED_COLUMNS + ("security_type", "tradable"))
        audit = history.membership_asof("NIFTY50", "2008-02-01")
        self.assertEqual(len(audit), 4)
        self.assertEqual({r.symbol for r in history.membership_asof(
            "NIFTY50", "2008-02-01", tradable_only=True)}, {"TESTEQ", "TESTDVR"})
        self.assertEqual({r.isin for r in audit if r.tradable}, {"INE000A01010", "INE000B01018"})

    def test_dummy_must_be_explicitly_nontrading(self):
        for row in (member(symbol="DUMMYTEST"),
                    member(symbol="DUMMYTEST", isin="", security_type="dummy", tradable="true")):
            columns = REQUIRED_COLUMNS + tuple(k for k in OPTIONAL_COLUMNS if k in row)
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, "dummy|DUMMY"):
                self.load([row], columns=columns)

    def test_no_blank_tradable_isin_or_placeholder_isin(self):
        for isin in ("", "UNKNOWN", "ine000a01010", "INE000A0101X"):
            with self.subTest(isin=isin), self.assertRaisesRegex(ValueError, "ISIN"):
                self.load([member(isin=isin)])

    def test_duplicate_rows_rejected_even_with_different_provenance(self):
        for second in (member(), member(source="another-file")):
            with self.subTest(second=second), self.assertRaisesRegex(ValueError, "duplicate"):
                self.load([member(), second])

    def test_overlap_rejected_by_symbol_or_isin_including_open_interval(self):
        for first, second in (
                (member(), member(isin="INE000B01018", effective_from="2008-02-01")),
                (member(), member(symbol="RENAMED", effective_from="2008-02-01")),
                (member(effective_to=""), member(effective_from="2009-01-01", effective_to="2010-01-01"))):
            with self.subTest(first=first, second=second), self.assertRaisesRegex(ValueError, "overlapping"):
                self.load([first, second])

    def test_same_security_in_two_indices_is_not_a_duplicate(self):
        history = self.load([member(), member(index="NIFTY500")], [claim(), claim("NIFTY500")])
        self.assertEqual(len(history.members), 2)

    def test_overlapping_correction_versions_are_not_silently_selected(self):
        rows = [member(known_at="2008-01-01T00:00:00Z"),
                member(effective_to="2008-12-01", known_at="2008-02-01T00:00:00Z")]
        with self.assertRaisesRegex(ValueError, "resolve corrections externally"):
            self.load(rows, columns=REQUIRED_COLUMNS + ("known_at",))

    def test_dates_are_strict_iso_not_basic_week_or_whitespace(self):
        for field in ("effective_from", "effective_to"):
            for value in ("20080131", "2008-W05-4", "2008-1-31", "2008-02-30", " 2008-01-31",
                          "2008-01-31T00:00:00", "2008-01-31 "):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, "ISO"):
                    self.load([member(**{field: value})])

    def test_inverted_or_zero_length_intervals_fail(self):
        for end in ("2008-01-31", "2008-01-30"):
            with self.subTest(end=end), self.assertRaisesRegex(ValueError, "before effective_to"):
                self.load([member(effective_to=end)])

    def test_external_licence_digest_and_kind_are_mandatory(self):
        self.write_rows([member()])
        for field, bad in (("licence_reference", ""), ("source_sha256", "0" * 64),
                           ("membership_kind", "dated_official_snapshot"),
                           ("interval_semantics", "inclusive")):
            metadata = self.manifest()
            metadata[field] = bad
            with self.subTest(field=field), self.assertRaises(ValueError):
                import_licensed_csv(self.source, metadata)

    def test_changed_bytes_invalidate_manifest(self):
        self.write_rows([member()])
        metadata = self.manifest()
        self.write_rows([member(symbol="CHANGED")])
        with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
            import_licensed_csv(self.source, metadata)

    def test_metadata_coverage_must_be_explicit_and_consistent(self):
        self.write_rows([member()])
        for coverage in (None, [claim(), claim()], [claim(complete="true")],
                         [claim(start="20080131")], [claim(end="2008-01-31")],
                         [dict(claim(), reference="")]):
            metadata = self.manifest()
            metadata["coverage"] = coverage
            with self.subTest(coverage=coverage), self.assertRaises(ValueError):
                import_licensed_csv(self.source, metadata)

    def test_bad_schema_duplicate_headers_extra_cells_and_empty_file_fail(self):
        header = ",".join(REQUIRED_COLUMNS)
        for text in ("", header + "\n", header + ",symbol\n", header + ",price\n",
                     header.replace("effective_from", "asof") + "\n",
                     header + "\nNIFTY50,TESTEQ\n",
                     header + "\nNIFTY50,TESTEQ,INE000A01010,2008-01-31,,s,extra\n"):
            self.source.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(ValueError):
                import_licensed_csv(self.source, self.manifest())

    def test_unknown_index_symbol_source_and_classification_fail(self):
        for row in (member(index="CNX Nifty"), member(index="RESEARCH"), member(symbol="today symbol"),
                    member(source=""), member(security_type="guessed"), member(tradable="yes")):
            columns = REQUIRED_COLUMNS + tuple(k for k in OPTIONAL_COLUMNS if k in row)
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.load([row], columns=columns)

    def test_cli_fails_closed_for_full_2000_2026_claim(self):
        self.write_rows([member()])
        metadata = Path(self.tmp.name) / "metadata.json"
        metadata.write_text(json.dumps(self.manifest()), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(SystemExit) as raised:
                main([str(self.source), str(metadata), "--require-full-history"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("full-history claim rejected", error.getvalue())

    def test_cli_report_and_asof_query(self):
        self.write_rows([member()])
        metadata = Path(self.tmp.name) / "metadata.json"
        metadata.write_text(json.dumps(self.manifest()), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main([str(self.source), str(metadata), "--index", "NIFTY50",
                                   "--asof", "2008-02-01"]), 0)
        report = json.loads(out.getvalue())
        self.assertFalse(report["coverage"]["full_history"])
        self.assertEqual(report["members"][0]["symbol"], "TESTEQ")


class PurchaseScopeRegressionTests(unittest.TestCase):
    def test_actual_windows_include_january_2008_and_nifty500_through_2013(self):
        request = build_pack("2026-11-09T00:00:00Z")["gap_request"]
        validate_gap_request(request)
        self.assertEqual(request["missing_windows"]["NIFTY50"]["end"], "2008-01-30")
        self.assertEqual(request["missing_windows"]["NIFTY500"]["end"], "2013-04-17")
        self.assertTrue(request["exact_effective_history"]["include_post_baseline_exact_intervals"])
        self.assertIn("snapshots are not continuous", EMAIL_BODY.lower())
        for term in ("2008-01-30", "2013-04-17", "2010-01-04", "NOT the size"):
            self.assertIn(term, render_sample_request())

    def test_shrinking_either_window_to_old_common_gap_fails(self):
        for index in ("NIFTY50", "NIFTY500"):
            request = build_pack("2026-11-09T00:00:00Z")["gap_request"]
            request["missing_windows"][index]["end"] = "2007-12-31"
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "missing_windows"):
                validate_gap_request(request)

    def test_excluding_exact_dates_due_to_snapshot_overlap_fails(self):
        request = build_pack("2026-11-09T00:00:00Z")["gap_request"]
        request["exact_effective_history"]["include_post_baseline_exact_intervals"] = False
        with self.assertRaisesRegex(ValueError, "exact_effective_history"):
            validate_gap_request(request)

    def test_old_gap_only_email_is_not_accepted(self):
        old = EMAIL_BODY.replace("2008-01-30", "2007-12-31").replace("2013-04-17", "2007-12-31")
        with self.assertRaisesRegex(ValueError, "per-index"):
            validate_email(RECIPIENT, SUBJECT, old)

    def test_purchase_date_parser_rejects_basic_iso_and_week_dates(self):
        for earliest in ("20000101", "1999-W52-6"):
            with self.subTest(earliest=earliest):
                result = decide_purchase(written_2000_confirmation=True, earliest_date=earliest,
                                         gap_report_received=True, sample_verified=True,
                                         licence_confirmed=True)
                self.assertFalse(result["pay_allowed"])


if __name__ == "__main__":
    unittest.main()
