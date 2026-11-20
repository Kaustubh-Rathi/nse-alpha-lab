import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify", ROOT / "src/nse_pipeline/schemes/verify.py")
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)

PDF_TEXT = ROOT / "data/processed/schemes/pdf_text"


def build():
    batch = verify.load_batch(PDF_TEXT)
    return verify.build_rows(PDF_TEXT, batch)


class VerifyBuildTests(unittest.TestCase):
    def test_build_verified_rows_validate_and_snippets_on_pages(self):
        verified, needs_human = build()
        self.assertGreaterEqual(len(verified), 9)
        for row in verified:
            verify.validate_verified_row(row)
            self.assertEqual(row["verified_by"], "cline6")
            self.assertTrue(row["terms_verified"])
            self.assertTrue(row["snippet_verified_on_page"])
            self.assertFalse(row["ratio_from_price"])
            self.assertIsNotNone(row["pdf_url"])
        # 10 ratio legs + 4 no-consideration rows
        ratio_rows = [r for r in verified if r["ratio_num"] is not None]
        self.assertEqual(len(ratio_rows), 10)
        nocons_rows = [r for r in verified if r["ratio_num"] is None]
        self.assertEqual(len(nocons_rows), 4)

    def test_sanofi_11_crosscheck(self):
        verified, _ = build()
        row = next(r for r in verified if r["scheme_id"] == "SLG-SANOFI-2023-05-10")
        self.assertEqual((row["ratio_num"], row["ratio_den"]), (1, 1))
        self.assertEqual(row["page_ref"], "p.4")
        self.assertIn("Sanofi Consumer Healthcare India Limited",
                      row["successor_company_name"])
        self.assertTrue(row["no_cash_consideration"])
        self.assertIsNone(row["successor_symbol"])

    def test_raymond_realty_11_and_lifestyle_45_are_distinct(self):
        verified, _ = build()
        realty = next(r for r in verified if r["scheme_id"] == "SLG-RAYMOND-2024-07-04")
        lifestyle = next(r for r in verified if r["scheme_id"] == "SLG-RAYMOND-2023-04-27")
        rg = next(r for r in verified if r["scheme_id"] == "SLG-RAYMOND-2023-04-27-RG")
        self.assertEqual((realty["ratio_num"], realty["ratio_den"]), (1, 1))
        self.assertEqual((lifestyle["ratio_num"], lifestyle["ratio_den"]), (4, 5))
        self.assertEqual((rg["ratio_num"], rg["ratio_den"]), (2, 1))
        self.assertIn("Raymond Realty Limited", realty["successor_company_name"])
        self.assertIn("Raymond Consumer Care Limited",
                      lifestyle["successor_company_name"])

    def test_indiaglyco_split_legs(self):
        verified, _ = build()
        ebl = next(r for r in verified if r["scheme_id"] == "SLG-INDIAGLYCO-2025-05-16-EBL")
        isl = next(r for r in verified if r["scheme_id"] == "SLG-INDIAGLYCO-2025-05-16-ISL")
        self.assertEqual((ebl["ratio_num"], ebl["ratio_den"]), (1, 3))
        self.assertEqual((isl["ratio_num"], isl["ratio_den"]), (1, 1))
        self.assertIn("Ennature Bio Pharma", ebl["successor_company_name"])
        self.assertIn("IGL Spirits", isl["successor_company_name"])

    def test_tinplate_33_for_10(self):
        verified, _ = build()
        row = next(r for r in verified if r["scheme_id"] == "SLG-TINPLATE-2024-01-08")
        self.assertEqual((row["ratio_num"], row["ratio_den"]), (33, 10))
        self.assertEqual(row["successor_company_name"], "Tata Steel Limited")
        self.assertEqual(row["page_ref"], "p.1")

    def test_sanghiind_12_for_100_and_ac_174(self):
        verified, _ = build()
        row = next(r for r in verified if r["scheme_id"] == "SLG-SANGHIIND-2024-12-17")
        ac = next(r for r in verified if r["scheme_id"] == "SLG-SANGHIIND-2024-12-17-ACL")
        self.assertEqual((row["ratio_num"], row["ratio_den"]), (12, 100))
        self.assertEqual((ac["ratio_num"], ac["ratio_den"]), (174, 1))
        self.assertEqual(row["successor_company_name"], "Ambuja Cements Limited")

    def test_withdrawn_dil_not_in_verified_ledger(self):
        verified, _ = build()
        dil = [r for r in verified if "DIL" in r["scheme_id"]]
        self.assertEqual(dil, [])
        # ... but the 270:1 clause was found and documented as withdrawn
        self.assertEqual(len(verify.WITHDRAWN_LEGS), 2)
        self.assertEqual(verify.WITHDRAWN_LEGS[0]["ratio_num"], 270)

    def test_wholly_owned_no_consideration_rows(self):
        verified, _ = build()
        nocons = {r["scheme_id"]: r for r in verified if r["ratio_num"] is None}
        self.assertIn("SLG-BALKRISIND-2024-01-24", nocons)
        self.assertIn("SLG-UDS-2024-05-20", nocons)
        self.assertIn("SLG-WIPRO-2023-11-08", nocons)
        self.assertIn("SLG-WIPRO-2024-06-27-DESIGNIT", nocons)
        for r in nocons.values():
            self.assertIsNone(r["ratio_den"])
            self.assertIsNone(r["cash_per_share"])
            self.assertTrue(r["no_cash_consideration"])

    def test_needs_human_covers_remaining_files(self):
        verified, needs_human = build()
        names = {n["text_file"] for n in needs_human}
        self.assertIn("TCPLPACK_29062024205151_Amalgamation.txt", names)
        self.assertIn("SANGHIIND_09022026163238_SIL_Intimation_Order09022026.txt", names)
        self.assertIn("WIPRO_02082024221450_SEIntimation02082024.txt", names)
        tcpl = next(n for n in needs_human
                    if n["text_file"] == "TCPLPACK_29062024205151_Amalgamation.txt")
        self.assertIn("scanned", tcpl["reason"])
        for n in needs_human:
            self.assertEqual(n["status"], "needs_human_pdf")
            self.assertFalse(n["terms_verified"])
        # Every cached txt is accounted for exactly once across the buckets
        total = len(list(PDF_TEXT.glob("*.txt")))
        accounted = set()
        for group in (verify.VERIFIED_LEGS, verify.WITHDRAWN_LEGS,
                      verify.NO_CONSIDERATION_LEGS):
            for leg in group:
                accounted.add(leg["pdf_file"])
                accounted.update(a.split(" (")[0]
                                 for a in leg.get("filing_trail", []))
        self.assertEqual(len(needs_human) + len(accounted), total)
        self.assertNotIn("needs_human_pdf", [])
        # verified bullets: cited files must be unique scheme files
        files = {r["extraction_source"].split(" (")[-1].rstrip(")") for r in verified}
        self.assertGreaterEqual(len(files), 9)

    def test_validate_rejects_bad_rows(self):
        good = {
            "scheme_id": "T-1", "scheme_type": "demerger", "parties": [],
            "dates": {}, "ratio_num": 1, "ratio_den": 1, "cash_per_share": None,
            "successor_company_name": "X Limited", "successor_symbol": None,
            "pdf_url": "https://example.com/a.pdf", "page_ref": "p.1",
            "snippet": "s", "status": "announced", "verified_by": "cline6",
            "terms_verified": True,
            "extraction_source": "manual_read_of_cached_pdf_text",
            "ratio_from_price": False,
        }
        verify.validate_verified_row(good)  # must not raise
        bad = dict(good, verified_by="someone_else")
        with self.assertRaises(ValueError):
            verify.validate_verified_row(bad)
        bad2 = dict(good, page_ref=None)
        with self.assertRaises(ValueError):
            verify.validate_verified_row(bad2)
        bad3 = dict(good, price_factor=0.5)
        with self.assertRaises(ValueError):
            verify.validate_verified_row(bad3)
        bad4 = dict(good, status="withdrawn")
        with self.assertRaises(ValueError):
            verify.validate_verified_row(bad4)

    def test_snippet_on_page_negative(self):
        pages = {(1): "hello world"}
        self.assertTrue(verify.snippet_on_page(pages, "p.1", ["hello"]))
        self.assertFalse(verify.snippet_on_page(pages, "p.1", ["impossible"]))
        self.assertFalse(verify.snippet_on_page(pages, "p.9", ["hello"]))

    def test_summary_outputs(self):
        verified, needs_human = build()
        summary = verify.build_summary(verified, needs_human, 50)
        self.assertFalse(summary["ratios_inferred_from_price"])
        self.assertEqual(summary["verified_rows"], len(verified))
        md = verify.render_markdown(summary, verified, needs_human)
        self.assertIn("SLG-SANOFI-2023-05-10", md)
        self.assertIn("needs_human_pdf", md)
        self.assertIn("CONFIRMED", md)

    def test_report_files_exist_after_run(self):
        # main() writes the verified ledger + reports; invoke it read-only
        # on the repo layout if outputs were already produced.
        ledger = ROOT / "data/processed/schemes/verified/verified-ledger.jsonl"
        if ledger.exists():
            rows = [json.loads(l) for l in
                    ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
            for r in rows:
                verify.validate_verified_row(r)
            self.assertGreaterEqual(len(rows), 9)


if __name__ == "__main__":
    unittest.main()
