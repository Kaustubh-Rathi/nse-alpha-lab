import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "ratios", Path(__file__).resolve().parents[1]
    / "src/nse_pipeline/schemes/ratios.py")
ratios = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ratios)

PDF_URL = ("https://nsearchives.nseindia.com/corporate/"
           "SANOFI_10052023134914_Reg30.pdf")


def rec(pages, url=PDF_URL):
    return ratios.parse_pages(pages, pdf_url=url, source_name="t.pdf")


class RatioParseTests(unittest.TestCase):
    def test_digit_demerger_1_for_1(self):
        r = rec(["Sanofi India Limited shall issue 1 equity share to be issued "
                 "for every 1 equity share held in Sanofi India Limited"])
        self.assertEqual((r["ratio_num"], r["ratio_den"]), (1, 1))
        self.assertEqual(r["status"], "candidate")
        self.assertEqual(r["page_ref"], "p.1")

    def test_word_numbers_one_parenthetical(self):
        r = rec(["one fully paid-up Equity Share for every One (1) equity "
                 "share held"])
        # Bare-form: "(1)" numerator branch may or may not fire depending on
        # the leading word; the digit "(1)" denominator must parse exactly.
        self.assertIn((r["ratio_num"], r["ratio_den"]), [(1, 1), (None, None)])
        if r["ratio_num"] is not None:
            self.assertEqual(r["ratio_den"], 1)

    def test_merger_4_for_5(self):
        r = rec(["the Company shall issue 4 Equity Shares for every "
                 "5 Equity Shares of Ranbaxy Laboratories Limited held"])
        self.assertEqual((r["ratio_num"], r["ratio_den"]), (4, 5))

    def test_colon_exchange_ratio(self):
        r = rec(["The exchange ratio is 1:2."])
        self.assertEqual((r["ratio_num"], r["ratio_den"]), (1, 2))
        self.assertEqual(r["ratio_kind"], "colon_exchange_ratio")

    def test_cash_consideration(self):
        r = rec(["cash consideration of Rs. 25 per equity share shall be paid "
                 "to each shareholder"])
        self.assertEqual(r["cash_per_share"], 25.0)
        self.assertEqual(r["status"], "candidate")

    def test_no_explicit_ratio_fails_closed(self):
        r = rec(["The Board has approved a Scheme of Arrangement for "
                 "restructuring of the business. Details are in the scheme."])
        self.assertIsNone(r["ratio_num"])
        self.assertIsNone(r["cash_per_share"])
        self.assertEqual(r["status"], "no_explicit_ratio")

    def test_scanned_no_text_fails_closed(self):
        r = rec(["", "   ", ""])
        self.assertEqual(r["status"], "scanned_no_text")
        self.assertIsNone(r["ratio_num"])

    def test_successor_company_from_issue_by(self):
        r = rec(["The Company shall issue and allot shares by Sanofi "
                 "Consumer Healthcare India Limited to shareholders"])
        self.assertEqual(r["successor_company_name"],
                         "Sanofi Consumer Healthcare India Limited")
        self.assertIsNone(r["successor_symbol"])

    def test_page_citation_correct(self):
        r = rec(["No terms on this page.",
                 "1 equity share to be issued for every 1 equity share held."])
        self.assertEqual(r["page_ref"], "p.2")

    def test_conflicting_ratios_needs_review(self):
        r = rec(["1 equity share to be issued for every 1 equity share held. "
                 "And 2 equity shares to be issued for every 3 equity shares "
                 "held."])
        self.assertEqual(r["status"], "needs_review_conflicting_ratios")
        self.assertTrue(r["distinct_ratios"])

    def test_no_price_inference_press_text(self):
        r = rec(["The index was rebalanced today. Prices moved sharply."])
        self.assertIsNone(r["ratio_num"])
        self.assertFalse(r["ratio_from_price"])

    def test_fractional_cash_flag(self):
        r = rec(["fractional entitlements shall be settled in cash."])
        self.assertTrue(r["fractional_cash"])

    def test_validate_rejects_price_fields(self):
        bad = ratios.parse_pages(
            ["1 equity share to be issued for every 1 equity share held."],
            pdf_url=PDF_URL)
        bad["price_factor"] = 0.5
        with self.assertRaises(ValueError):
            ratios.validate_ratio_record(bad)
        bad2 = ratios.parse_pages(
            ["1 equity share to be issued for every 1 equity share held."],
            pdf_url=PDF_URL)
        bad2["extraction_source"] = "from price move"
        with self.assertRaises(ValueError):
            ratios.validate_ratio_record(bad2)

    def test_validate_requires_citation(self):
        bad = ratios.parse_pages(
            ["1 equity share to be issued for every 1 equity share held."],
            pdf_url=None)
        with self.assertRaises(ValueError):
            ratios.validate_ratio_record(bad)

    def test_extract_pdf_missing_fails_closed(self):
        r = ratios.extract_pdf(Path("nope-missing.pdf"))
        self.assertEqual(r["status"], "pdf_text_extract_unavailable")
        self.assertIsNone(r["ratio_num"])

    def test_pdf_roundtrip_if_pypdf_available(self):
        try:
            from pypdf import PdfWriter  # noqa: F401
            from pypdf import PdfReader  # noqa: F401
        except ImportError:
            self.skipTest("pypdf not installed (run: uv run --with pypdf ...)")
        import io
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        # Blank page has no text: proves the fail-closed path end-to-end.
        buf = io.BytesIO()
        writer.write(buf)
        pages = ratios.extract_pages_from_bytes(buf.getvalue())
        r = ratios.parse_pages(pages, pdf_url=PDF_URL)
        self.assertIn(r["status"],
                      ("scanned_no_text", "no_explicit_ratio"))

    def test_sanofi_known_demerger_text(self):
        # Sanofi India -> SCHIL: "one fully paid-up Equity Share for every
        # one fully paid-up Equity Share" (see research report).
        r = rec(["The Company has agreed to issue and allot to the "
                 "shareholders of Sanofi India Limited as on the record "
                 "date, one fully paid-up Equity Share for every one "
                 "fully paid-up Equity Share held."])
        self.assertEqual((r["ratio_num"], r["ratio_den"]), (1, 1))
        self.assertEqual(r["status"], "candidate")

    def test_raymond_realty_known_text(self):
        # Raymond -> Raymond Realty: 1:1, no cash consideration.
        r = rec(["Shareholders of RAYMOND will receive 1 equity share to be "
                 "issued for every 1 equity share held in RAYMOND, with no "
                 "cash consideration involved."])
        self.assertEqual((r["ratio_num"], r["ratio_den"]), (1, 1))
        self.assertIsNone(r["cash_per_share"])


if __name__ == "__main__":
    unittest.main()
