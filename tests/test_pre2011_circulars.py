"""Tests for issuer-circular verification (pre2011_circulars).

Pure in-memory evaluation is tested directly; build() is tested on
synthetic tmp roots.  No test reads market.duckdb or the real queue.
Fail-closed invariants: nothing verifies from price alone, secondary
hosts never verify, and stated dates must be proximate.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.actions.pre2011_circulars import (  # noqa: E402
    NEEDS,
    assert_no_forbidden_keys,
    download_bounded,
    evaluate_document,
    find_term_matches,
    is_primary_source,
    verify_one,
)

BSE_URL = ("https://www.bseindia.com/HIS_ANN_RPT/HISTANNR/2001/"
           "SATYAM_COMPUTER_SERVICES_LTD-500376-MARCH-2001.PDF")
SEC_URL = "https://www.sec.gov/Archives/edgar/data/1067491/x/d20f.htm"


def queue_row(symbol="SATYAMCOMP", day="2000-08-09", rank=1, gap=1,
              prior=2337.7, close=499.9):
    return {
        "symbol": symbol, "date": day, "prior_trading_date": "2000-08-08",
        "trading_gap_days": gap, "prior_close": prior, "close": close,
        "previous_close": prior, "abs_close_change_pct": 78.62,
        "signed_close_change_pct": -78.62, "priority_rank": rank,
        "priority_score_abs_return_x_volume": 123.0, "review_flags": [],
    }


def doc(local="satyam_ar2000_01_bse", url=BSE_URL, symbol="SATYAMCOMP"):
    return {"symbol": symbol, "title": "Satyam Annual Report 2000-01",
            "url": url, "kind": "exchange_annual_report_archive",
            "local": local}


SATYAM_TEXT = (
    "Satyam Computer Services Ltd. During the year the Company split "
    "its one equity share of Rs 10/- into five equity shares of Rs 2/- "
    "each as approved by the members at the Annual General Meeting held "
    "on May 26, 2000. Prices have been adjusted for stock split of "
    "5-for-1 effected on August 25, 2000.")

INFOSYS_TEXT = (
    "Infosys Technologies Limited. Bonus share issuance of three "
    "additional equity shares for every equity share held on July 2, "
    "2004, the record date. The bonus shares were allotted to all "
    "shareholders on July 3, 2004.")


class TermDetectionTests(unittest.TestCase):
    def test_split_face_value_change_detected(self):
        m = find_term_matches(
            "Effective 8th December 2003, the par value of the shares "
            "was split from Rs.10/- per share to Re.1/- per share.")
        self.assertTrue(any(x["kind"] == "split" for x in m))
        self.assertIn("2003-12-08", m[0]["stated_dates"])

    def test_bonus_ratio_and_additional_shares_detected(self):
        m = find_term_matches(INFOSYS_TEXT)
        self.assertTrue(any(x["kind"] == "bonus" for x in m))
        m2 = find_term_matches(
            "Bonus equity shares of Rs. 2/- each in the ratio of 2:1 "
            "(two bonus shares for every one share held) as at "
            "June 28, 2004.")
        self.assertTrue(any(x["kind"] == "bonus" for x in m2))

    def test_generic_words_are_not_terms(self):
        self.assertEqual(find_term_matches(
            "gains from bonus issues are reinvested; weight split "
            "between indices; guar splits closing stock"), [])
        # A bare Re.1 face value with no stated change is not a term.
        self.assertEqual(find_term_matches(
            "Earnings Per Share (Face value of Re.1 per share) 7.98"), [])

    def test_primary_host_gate(self):
        self.assertTrue(is_primary_source(BSE_URL))
        self.assertTrue(is_primary_source(SEC_URL))
        self.assertTrue(is_primary_source(
            "https://www.dabur.com/Investors/Reports/Archive/2000-01/x.pdf"))
        self.assertFalse(is_primary_source(
            "https://trendlyne.com/equity/corporate-actions/DABUR/303/"))
        self.assertFalse(is_primary_source(
            "https://www.moneycontrol.com/company-facts/daburindia/splits/DI/"))
        self.assertFalse(is_primary_source("https://example.com/x.pdf"))
        self.assertFalse(is_primary_source(None))


class EvaluateTests(unittest.TestCase):
    def test_proximate_split_verifies(self):
        ev = evaluate_document(doc(), SATYAM_TEXT,
                               ["Satyam Computer Services"],
                               parse := __import__("datetime").date(2000, 8, 9))
        self.assertEqual(ev["outcome"], "proximate_explicit_terms")
        self.assertTrue(ev["mentions_company"])
        self.assertTrue(ev["primary_host"])

    def test_distant_stated_date_does_not_verify(self):
        text = ("Satyam Computer Services Ltd. Bonus Issue of 1-for-1 "
                "as on September 1, 1999. " + "x " * 120 +
                "stock split of 5-for-1 as on August 25, 2000.")
        from datetime import date
        ev = evaluate_document(doc(), text, ["Satyam Computer Services"],
                               date(2000, 8, 9))
        # The 1999 bonus must not verify; the (proximate) split may.
        self.assertEqual(ev["outcome"], "proximate_explicit_terms")
        kinds = [m["kind"] for m in ev["proximate_term_matches"]]
        self.assertEqual(kinds, ["split"])

    def test_company_mismatch_does_not_verify(self):
        text = ("Hindustan Lever Limited split from Rs.10/- per share "
                "to Re.1/- per share effective 3 July 2000.")
        from datetime import date
        ev = evaluate_document(doc(), text, ["Satyam Computer Services"],
                               date(2000, 8, 9))
        self.assertEqual(ev["outcome"], "company_not_mentioned")


class VerifyTests(unittest.TestCase):
    def test_verified_split_row_with_citation(self):
        row = verify_one(queue_row(), [doc()],
                         {"satyam_ar2000_01_bse": SATYAM_TEXT})
        self.assertEqual(row["verification_status"], "verified_split")
        self.assertEqual(row["verified_event_type"], "split")
        self.assertIn("5-for-1", row["stated_terms_verbatim"])
        self.assertIn("2000-08-25", row["citation"]["stated_dates"])
        self.assertFalse(row["ratio_from_price"])
        assert_no_forbidden_keys(row)
        self.assertNotIn("adjustment_factor", json.dumps(row))

    def test_verified_bonus_row(self):
        d = doc(local="infosys_20f2005", url=SEC_URL, symbol="INFOSYSTCH")
        row = verify_one(
            queue_row(symbol="INFOSYSTCH", day="2004-07-01",
                      prior=5524.1, close=1408.8), [d],
            {"infosys_20f2005": INFOSYS_TEXT})
        self.assertEqual(row["verification_status"], "verified_bonus")
        self.assertEqual(row["verified_event_type"], "bonus")

    def test_price_alone_never_verifies(self):
        row = verify_one(queue_row(), [], {})
        self.assertEqual(row["verification_status"], NEEDS)
        self.assertEqual(row["needs_reason"], "no_primary_doc_found")
        self.assertIsNone(row["citation"])
        self.assertTrue(row["no_ratio_inferred_from_price"])

    def test_no_explicit_terms_stays_needs(self):
        text = ("Hindustan Lever Limited Annual Report 2002. Earnings "
                "Per Share (Face value of Re.1 per share) 7.98. Bonus "
                "debentures are described in the notes.")
        d = doc(local="hul_ar2002",
                url="https://www.hul.co.in/files/x/annual-report-2002.pdf",
                symbol="HINDLEVER")
        row = verify_one(queue_row(symbol="HINDLEVER", day="2000-07-05"),
                         [d], {"hul_ar2002": text})
        self.assertEqual(row["verification_status"], NEEDS)
        self.assertEqual(row["needs_reason"], "no_explicit_terms")

    def test_stated_date_too_far_stays_needs(self):
        text = ("Satyam Computer Services Ltd. split its one equity "
                "share of Rs 10/- into five equity shares of Rs 2/- "
                "each on 25 August 2006.")
        row = verify_one(queue_row(), [doc()],
                         {"satyam_ar2000_01_bse": text})
        self.assertEqual(row["verification_status"], NEEDS)
        self.assertEqual(row["needs_reason"], "stated_date_not_proximate")

    def test_gap_row_never_promoted_on_split_alone(self):
        row = verify_one(queue_row(gap=96), [doc()],
                         {"satyam_ar2000_01_bse": SATYAM_TEXT})
        self.assertEqual(row["verification_status"], NEEDS)
        self.assertEqual(row["needs_reason"],
                         "gap_row_suspension_check_required")
        self.assertIn("suspension/resumption", row["trading_gap_note"])


class DownloadTests(unittest.TestCase):
    def test_non_primary_host_refused(self):
        with self.assertRaises(ValueError):
            download_bounded("https://trendlyne.com/equity/x.pdf")

    def test_oversize_and_bad_payload_refused(self):
        big = lambda u, t: (200, "application/pdf", b"%PDF" + b"x" * 100)
        with self.assertRaises(ValueError):
            download_bounded(BSE_URL, max_bytes=10, opener=big)
        bad = lambda u, t: (200, "text/plain", b"not a pdf")
        with self.assertRaises(ValueError):
            download_bounded(BSE_URL, opener=bad)
        ok = lambda u, t: (200, "application/pdf", b"%PDF-1.4 fake")
        self.assertEqual(download_bounded(BSE_URL, opener=ok),
                         b"%PDF-1.4 fake")


class BuildTests(unittest.TestCase):
    def test_build_writes_rows_and_coverage_on_tmp_root(self):
        from nse_pipeline.actions.pre2011_circulars import build
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "processed" / "actions" / "pre2011_queue"
            proc.mkdir(parents=True)
            out = root / "processed" / "actions" / "pre2011_circulars"
            (out / "raw_texts").mkdir(parents=True)
            (root / "reports").mkdir(parents=True)
            rows = [queue_row(rank=1),
                    queue_row(symbol="VIKASWSP", day="2001-01-24", rank=2)]
            (proc / "queue.jsonl").write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n")
            (out / "raw_texts" / "satyam_ar2000_01_bse.txt").write_text(
                SATYAM_TEXT)
            coverage = build(root, top_n=2)
            self.assertEqual(coverage["queue_rows_considered"], 2)
            self.assertEqual(coverage["verified_rows"], 1)
            self.assertEqual(coverage["needs_issuer_circular_rows"], 1)
            written = (out / "circulars.jsonl").read_text().splitlines()
            self.assertEqual(len(written), 2)
            first = json.loads(written[0])
            self.assertEqual(first["verification_status"], "verified_split")
            assert_no_forbidden_keys(first)
            self.assertTrue((out / "coverage.json").exists())
            self.assertTrue((root / "reports" / "pre2011-circulars.json").exists())
            self.assertTrue((root / "reports" / "pre2011-circulars.md").exists())


if __name__ == "__main__":
    unittest.main()
