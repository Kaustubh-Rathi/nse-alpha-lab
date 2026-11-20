"""Fail-closed tests for the public documentary assembly (nifty_doc)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.nifty.documentary import (  # noqa: E402
    clean_company_name, compute_gaps, diff_snapshots, extract_press_links,
    find_effective_date, import_mcwb_doc, import_pdf_pages_generic,
    import_type4_csv, iter_months, month_year_label, normalize_heading,
    parse_changelog_text, parse_press_filename_date, parse_probe_response,
    select_mcwb_member, select_type4_csv_member, select_type4_member,
    validate_event,
)

TEMP_ROOT = Path.home() / "AppData" / "Local" / "Temp" / "opencode"
if not TEMP_ROOT.exists():
    TEMP_ROOT = None


class ProbeTests(unittest.TestCase):
    def test_success_response_yields_download_link(self):
        body = json.dumps({"success": True, "reportName": "MCWB", "data": [
            {"DownloadLink": "/x/mcwb_jan08.zip", "FileName": "mcwb_jan08.zip"}]
        }).encode()
        record = parse_probe_response(body, "2", 2008, 1,
                                      "2026-11-08T00:00:00+00:00")
        self.assertTrue(record["success"])
        self.assertEqual(record["filename"], "mcwb_jan08.zip")
        self.assertEqual(record["month_year"], "Jan 2008")

    def test_no_data_and_malformed_bodies_never_guess(self):
        record = parse_probe_response(b'{"success":false,"message":"No Data Found"}',
                                      "4", 2000, 10, "2026-11-08T00:00:00+00:00")
        self.assertFalse(record["success"])
        self.assertIsNone(record["download_link"])
        bad = parse_probe_response(b"not json", "2", 2008, 1,
                                   "2026-11-08T00:00:00+00:00")
        self.assertFalse(bad["success"])
        self.assertTrue(bad["message"].startswith("malformed_json"))

    def test_iter_months_crosses_year_boundary(self):
        self.assertEqual(list(iter_months((2025, 11), (2026, 2))),
                         [(2025, 11), (2025, 12), (2026, 1), (2026, 2)])
        self.assertEqual(month_year_label(2026, 12), "Dec 2026")


class PressInventoryTests(unittest.TestCase):
    def test_filename_dates(self):
        self.assertEqual(parse_press_filename_date("ind_prs10012000.pdf"),
                         date(2000, 1, 10))
        self.assertEqual(parse_press_filename_date("ind_prs23082024_1.pdf"),
                         date(2024, 8, 23))
        self.assertIsNone(parse_press_filename_date("ind_prs99999999.pdf"))
        self.assertIsNone(parse_press_filename_date("random.pdf"))

    def test_link_extraction_dedupes_and_sorts(self):
        html = ("<a href='/Press_Release/ind_prs10012000.pdf'>a</a>"
                "<a href='/Press_Release/ind_prs10012000.pdf'>b</a>"
                "<a href='/Press_Release/ind_prs24042000.pdf'>c</a>")
        self.assertEqual(extract_press_links(html),
                         ["ind_prs10012000.pdf", "ind_prs24042000.pdf"])


class MemberSelectionTests(unittest.TestCase):
    def test_mcwb_selection_never_picks_next_or_junior(self):
        names = ["jrniftymcwb.csv", "niftymcwb.csv"]
        self.assertEqual(select_mcwb_member(names), "niftymcwb.csv")
        names = ["nifty50_mcwb.csv", "niftynext50_mcwb.csv"]
        self.assertEqual(select_mcwb_member(names), "nifty50_mcwb.csv")
        self.assertIsNone(select_mcwb_member(["niftynext50_mcwb.csv"]))

    def test_type4_selection(self):
        members = ["NIFTY_50_Oct2021.pdf", "NIFTY_Next_50_Oct2021.pdf",
                   "NIFTY_500_Oct2021.pdf", "NIFTY50_Value_20_Oct2021.pdf"]
        self.assertEqual(select_type4_member(members, "NIFTY50"),
                         "NIFTY_50_Oct2021.pdf")
        self.assertEqual(select_type4_member(members, "NIFTY500"),
                         "NIFTY_500_Oct2021.pdf")
        cnx = ["cnx500_Oct2013.pdf", "cnxnifty_Oct2013.pdf"]
        self.assertEqual(select_type4_member(cnx, "NIFTY500"),
                         "cnx500_Oct2013.pdf")
        self.assertEqual(select_type4_member(cnx, "NIFTY50"),
                         "cnxnifty_Oct2013.pdf")

    def test_type4_csv_selection(self):
        members = ["indices_dataApr2013/cnx500_Apr2013.csv",
                   "indices_dataApr2013/cnxnifty_Apr2013.csv"]
        self.assertEqual(select_type4_csv_member(members, "NIFTY500"),
                         "indices_dataApr2013/cnx500_Apr2013.csv")
        self.assertEqual(select_type4_csv_member(members, "NIFTY50"),
                         "indices_dataApr2013/cnxnifty_Apr2013.csv")


class FormatVariantImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write_zip(self, member: str, content: bytes):
        from nse_pipeline.nifty.snapshots import sha256
        path = self.root / "report.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(member, content)
        provenance = {"sha256": sha256(path.read_bytes()),
                      "url": "https://www.niftyindices.com/monthly/report.zip",
                      "retrieved_at_utc": "2026-11-08T10:00:00+00:00"}
        return path, provenance

    def test_mcwb_doc_accepts_reordered_serials(self):
        rows = "".join(f"{i},STOCK{i},Company {i}\n" for i in range(1, 51))
        lines = rows.splitlines()
        lines[8], lines[9] = lines[9], lines[8]  # swap serials 9 and 10
        report = ("S&P CNX Nifty Index : February 2012\n\n"
                  "Sr. No,Security Symbol,Security Name\n"
                  + "\n".join(lines)
                  + "\nLast day of trading was 29-February-2012\n")
        path, provenance = self.write_zip("niftymcwb.csv",
                                          report.encode())
        snapshot = import_mcwb_doc(path, "niftymcwb.csv", provenance)
        self.assertEqual(snapshot.as_of, date(2012, 2, 29))
        self.assertEqual([m.symbol for m in snapshot.members[:2]],
                         ["STOCK1", "STOCK2"])
        self.assertEqual(len(snapshot.members), 50)

    def test_mcwb_doc_rejects_missing_explicit_date(self):
        report = ("S&P CNX Nifty Index : September 2010\n\n"
                  "Sr. No,Security Symbol,Security Name\n"
                  + "".join(f"{i},STOCK{i},Company {i}\n"
                            for i in range(1, 51)))
        path, provenance = self.write_zip("niftymcwb.csv.csv",
                                          report.encode())
        with self.assertRaisesRegex(ValueError, "last trading date"):
            import_mcwb_doc(path, "niftymcwb.csv.csv", provenance)

    def test_mcwb_doc_cp1252_fallback(self):
        report = ("S&P CNX Nifty Index : July 2010\n\n"
                  "Sr. No,Security Symbol,Security Name\n"
                  + "".join(f"{i},STOCK{i},Company {i}\n"
                            for i in range(1, 51))
                  + "\xa7 footnote\nLast day of trading was 30-July-2010\n")
        content = report.encode("cp1252")
        path, provenance = self.write_zip("niftymcwb.csv", content)
        snapshot = import_mcwb_doc(path, "niftymcwb.csv", provenance)
        self.assertEqual(snapshot.as_of, date(2010, 7, 30))
        self.assertIn("cp1252", snapshot.metadata["extraction"])

    def test_generic_pdf_import_cnx_headers(self):
        page = ("Symbol Security Name Industry Close Price Index Mcap "
                "(Rs. Crores) Weightage (%)\n \n"
                "Constituents of CNX 500\nMay 31, 2013\n"
                + "".join(f"STOCK{i} Company {i} IND 100.00 0.1{i}\n"
                          for i in range(1, 6)))
        provenance = {"sha256": "0" * 64,
                      "url": "https://www.niftyindices.com/indices.zip",
                      "retrieved_at_utc": "2026-11-08T10:00:00+00:00"}
        snapshot = import_pdf_pages_generic(
            [page], "NIFTY500", b"pdf", provenance, "cnx500_May2013.pdf")
        self.assertEqual(snapshot.as_of, date(2013, 5, 31))
        self.assertEqual(snapshot.metadata["index_name_in_document"], "CNX 500")
        with self.assertRaisesRegex(ValueError, "is not NIFTY50"):
            import_pdf_pages_generic(
                [page], "NIFTY50", b"pdf", provenance, "cnx500_May2013.pdf")

    def test_type4_csv_import_single_date_only(self):
        header = ("Date,Index Name,Symbol,Security Name,Industry,"
                  "Close Price,Index MCap(Rs. Crores),Weightage(%)\n")
        rows = "".join(f"18-04-2013,CNX NIFTY,STOCK{i},Company {i},IND,"
                       f"100.0,1.0,0.1\n" for i in range(1, 6))
        provenance = {"sha256": "0" * 64,
                      "url": "https://www.niftyindices.com/indices.zip",
                      "retrieved_at_utc": "2026-11-08T10:00:00+00:00"}
        snapshot = import_type4_csv((header + rows).encode(), provenance,
                                    "cnxnifty_Apr2013.csv")
        self.assertEqual(snapshot.as_of, date(2013, 4, 18))
        self.assertEqual(snapshot.index_code, "NIFTY50")
        self.assertEqual(snapshot.members[0].company_name, "Company 1")
        mixed = rows + "19-04-2013,CNX NIFTY,STOCK6,Company 6,IND,1,1,1\n"
        with self.assertRaisesRegex(ValueError, "one date"):
            import_type4_csv((header + mixed).encode(), provenance,
                             "cnxnifty_Apr2013.csv")


class ChangelogTests(unittest.TestCase):
    OLD_NOTICE = """Press Release
Change in indices w.e.f. May 24, 2000
(1) S&P CNX Nifty
The companies being excluded from S&P CNX Nifty are:
Sr. No. Company Name Impact Cost Current Market Capitalisation (Rs.mil)
1. Industrial Development Bank of India 0.90 24,332
The companies being included in S&P CNX Nifty are:
Sr. No. Company Name Impact Cost Current Market Capitalisation (Rs.mil)
1. Zee Telefilms Ltd. 0.55 279,826
The above changes become effective from May 24, 2000.
"""
    MODERN_NOTICE = """PRESS RELEASE
Mumbai, August 23, 2024
1) Replacements on account of semi-annual review of broad market indices:
a) Nifty 50
The following companies are being excluded:
Sr. No. Company Name Symbol
1 Divi's Laboratories Ltd. DIVISLAB
The following companies are being included:
Sr. No. Company Name Symbol
1 Bharat Electronics Ltd. BEL
These changes shall become effective from September 30, 2024.
"""

    def test_old_style_notice_parses_without_symbol(self):
        parsed = parse_changelog_text(
            self.OLD_NOTICE, {"filename": "ind_prs24042000.pdf",
                              "announcement_date": "2000-04-24",
                              "source_sha256": "0" * 64})
        self.assertEqual(parsed["notice_status"], "parsed_events_unreconciled")
        self.assertEqual(parsed["effective_date"], "2000-05-24")
        self.assertEqual(len(parsed["events"]), 2)
        by_action = {e["action"]: e for e in parsed["events"]}
        self.assertEqual(by_action["exclusion"]["company_name"],
                         "Industrial Development Bank of India")
        self.assertIsNone(by_action["exclusion"]["symbol"])
        self.assertFalse(by_action["exclusion"]["applied"])
        self.assertTrue(
            by_action["exclusion"]["reconciliation_checkpoint_required"])

    def test_modern_style_notice_parses_with_symbol(self):
        parsed = parse_changelog_text(
            self.MODERN_NOTICE, {"filename": "ind_prs23082024.pdf",
                                 "announcement_date": "2024-08-23",
                                 "source_sha256": "0" * 64})
        self.assertEqual(parsed["effective_date"], "2024-09-30")
        by_action = {e["action"]: e for e in parsed["events"]}
        self.assertEqual(by_action["exclusion"]["symbol"], "DIVISLAB")
        self.assertEqual(by_action["inclusion"]["symbol"], "BEL")
        self.assertEqual(by_action["inclusion"]["status"],
                         "parsed_with_symbol_unreconciled")

    def test_cnx_nifty_index_heading_variant(self):
        self.assertEqual(normalize_heading("CNX Nifty Index"), "cnx nifty")
        self.assertEqual(normalize_heading("S&P CNX 500 Index"),
                         "s&p cnx 500")

    def test_clean_company_name_strips_numeric_tail(self):
        company, symbol = clean_company_name(
            "Industrial Development Bank of India 0.90 24,332")
        self.assertEqual(company, "Industrial Development Bank of India")
        self.assertIsNone(symbol)
        company, symbol = clean_company_name("Bharat Electronics Ltd. BEL")
        self.assertEqual((company, symbol), ("Bharat Electronics Ltd.", "BEL"))

    def test_validate_event_is_fail_closed(self):
        base = {"index_code": "NIFTY50", "action": "inclusion",
                "company_name": "Test Ltd.", "symbol": None,
                "effective_date": "2024-09-30",
                "effective_date_evidence": "effective from September 30, 2024",
                "status": "parsed_unreconciled_name_only", "applied": False,
                "reconciliation_checkpoint_required": True}
        validate_event(base)
        for mutation in ({"applied": True},
                         {"reconciliation_checkpoint_required": False},
                         {"index_code": "NIFTY_NEXT50"},
                         {"status": "applied"},
                         {"effective_date": "not-a-date"}):
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    validate_event(dict(base, **mutation))

    def test_effective_date_search_survives_line_breaks(self):
        flat = "These changes shall become effectiv e from April 1, 2013."
        parsed, _ = find_effective_date(flat)
        self.assertEqual(parsed, date(2013, 4, 1))
        self.assertEqual(find_effective_date("no date here"), (None, None))


class GapAndDiffTests(unittest.TestCase):
    def test_diff_and_gaps(self):
        snapshots = [
            ("NIFTY50", date(2008, 1, 31), frozenset({"AAA", "BBB"})),
            ("NIFTY50", date(2008, 2, 29), frozenset({"BBB", "CCC"})),
            ("NIFTY500", date(2008, 1, 31), frozenset({"AAA"})),
        ]
        deltas = diff_snapshots(snapshots)
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["added_symbols"], ["CCC"])
        self.assertEqual(deltas[0]["removed_symbols"], ["AAA"])
        gaps = compute_gaps(snapshots, date(2008, 1, 1), date(2008, 3, 31))
        self.assertEqual(gaps["NIFTY50"]["missing_months"], ["2008-03"])
        self.assertEqual(gaps["NIFTY500"]["first_snapshot"], "2008-01-31")
        self.assertEqual(gaps["NIFTY500"]["missing_month_count"], 2)


if __name__ == "__main__":
    unittest.main()
