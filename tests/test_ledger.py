import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "ledger", Path(__file__).resolve().parents[1] / "src/nse_pipeline/schemes/ledger.py")
ledger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ledger)

PDF = "https://nsearchives.nseindia.com/corporate/RAYMOND_19122024182010_SEintimationsigned.pdf"


def make_entry(**overrides):
    entry = {
        "scheme_id": "SLG-TEST-2024-01-01",
        "scheme_type": "demerger",
        "parties": [{"symbol": "TEST", "company_name": "Test Limited",
                     "isin": "INE000A01000", "role": "demerged_company"}],
        "dates": {"announced": "2024-01-01", "board_date": "2024-01-01"},
        "ratio_num": None, "ratio_den": None, "cash_per_share": None,
        "successor_symbol": None, "pdf_url": PDF, "page_ref": None,
        "status": "announced", "verified_by": None,
        "extraction_source": "announcement_index_text_only; PDF not read",
        "terms_verified": False,
    }
    entry.update(overrides)
    return entry


def index_row(symbol="SANOFI", date="2023-05-10",
              desc="Scheme of Arrangement", text="", pdf="x.pdf"):
    return {"symbol": symbol, "sm_name": "Sanofi India Limited",
            "sm_isin": "INE058A01010", "announcement_date": date,
            "desc": desc, "attchmntText": text, "attchmntFile": pdf}


class LedgerSchemaTests(unittest.TestCase):
    def test_valid_unverified_stub_passes(self):
        self.assertEqual(ledger.validate_entry(make_entry())["scheme_id"],
                         "SLG-TEST-2024-01-01")

    def test_missing_schema_field_rejected(self):
        bad = make_entry()
        del bad["pdf_url"]
        with self.assertRaises(ValueError):
            ledger.validate_entry(bad)

    def test_ratio_requires_citation_and_verifier(self):
        for missing in ("pdf_url", "page_ref", "verified_by"):
            bad = make_entry(ratio_num=1, ratio_den=1, **{missing: None})
            with self.assertRaises(ValueError, msg=missing):
                ledger.validate_entry(bad)
        ok = make_entry(ratio_num=1, ratio_den=2, page_ref="p.42",
                        verified_by="manual-reader", status="effective",
                        extraction_source="manual PDF reading")
        self.assertEqual(ledger.validate_entry(ok)["ratio_den"], 2)

    def test_ratio_never_inferred_from_price(self):
        bad = make_entry(ratio_num=1, ratio_den=1, page_ref="p.1",
                         verified_by="x", extraction_source="from raw price move")
        with self.assertRaises(ValueError):
            ledger.validate_entry(bad)
        for extra in ({"price_factor": 0.5}, {"inferred_from_price": True}):
            with self.assertRaises(ValueError, msg=str(extra)):
                ledger.validate_entry(make_entry(**extra))

    def test_withdrawn_carries_no_terms(self):
        bad = make_entry(status="withdrawn", ratio_num=1, ratio_den=1,
                         page_ref="p.1", verified_by="x")
        with self.assertRaises(ValueError):
            ledger.validate_entry(bad)
        self.assertEqual(ledger.validate_entry(
            make_entry(status="withdrawn"))["status"], "withdrawn")

    def test_dates_must_be_ordered_and_iso(self):
        bad = make_entry(dates={"announced": "2024-02-01", "board_date": "2024-01-01",
                                "effective_date": "2024-01-15"})
        with self.assertRaises(ValueError):
            ledger.validate_entry(bad)
        bad2 = make_entry(dates={"announced": "01 Jan 2024"})
        with self.assertRaises(ValueError):
            ledger.validate_entry(bad2)

    def test_unknown_type_and_status_rejected(self):
        for key, val in (("scheme_type", "takeover"), ("status", "closed")):
            with self.assertRaises(ValueError, msg=f"{key}={val}"):
                ledger.validate_entry(make_entry(**{key: val}))


class CollapseTests(unittest.TestCase):
    def test_multi_filing_scheme_collapses_with_trail(self):
        rows = [
            index_row(date="2023-05-10", text=(
                "The Board of Directors of Sanofi India Limited, at its meeting "
                "held today, has approved a scheme of arrangement to demerge its "
                "Consumer Healthcare Business from Sanofi India Limited into "
                "Sanofi Consumer Healthcare India Limited")),
            index_row(date="2024-05-09", text=(
                "Judicial pronouncement by the Honble National Company Law "
                "Tribunal approving the Scheme of Arrangement between "
                "Sanofi India Limited and Sanofi Consumer Healthcare India Limited")),
            index_row(date="2024-06-01", text=(
                "effectiveness of the Scheme of Arrangement between "
                "Sanofi India Limited and Sanofi Consumer Healthcare India Limited")),
        ]
        entries = ledger.collapse_filings(rows)
        self.assertEqual(len(entries), 1, entries)
        (e,) = entries
        self.assertEqual(e["scheme_type"], "demerger")
        self.assertEqual(e["status"], "effective")
        self.assertEqual(len(e["filing_trail"]), 3)
        self.assertIsNone(e["ratio_num"])
        self.assertFalse(e["terms_verified"])
        self.assertEqual(e["dates"]["board_date"], "2023-05-10")
        self.assertEqual(e["dates"]["nclt_order_date"], "2024-05-09")

    def test_withdrawal_demotes_prior_filing(self):
        rows = [
            index_row(symbol="DIL", date="2024-06-28",
                      text="Debock Industries Limited has informed about Scheme of Arrangement"),
            index_row(symbol="DIL", date="2024-11-08",
                      text="Debock Industries Limited has informed about Scheme of Arrangement",
                      pdf="DIL_08112024121416_Intimation_-_Withdrawal_of_Scheme.pdf"),
        ]
        entries = ledger.collapse_filings(rows)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "withdrawn")
        self.assertIsNone(entries[0]["ratio_num"])
        self.assertIsNone(entries[0]["successor_symbol"])

    def test_generic_filings_do_not_merge_on_symbol_alone(self):
        rows = [index_row(symbol="RAYMOND", date=d,
                          text="Raymond Limited has informed the Exchange about Scheme of Arrangement")
                for d in ("2024-01-18", "2024-03-21", "2024-12-19")]
        entries = ledger.collapse_filings(rows)
        self.assertEqual(len(entries), 3, "generic filings must stay singletons")
        self.assertEqual(len({e["scheme_id"] for e in entries}), 3)

    def test_same_day_pair_merges(self):
        rows = [index_row(symbol="UDS", date="2024-05-20", text=t) for t in
                ("Intimation under Regulation 30 - Draft Scheme of Amalgamation",
                 "Intimation under Regulation 30 - Draft Scheme of Amalgamation")]
        entries = ledger.collapse_filings(rows)
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0]["filing_trail"]), 2)


class CoverageTests(unittest.TestCase):
    def test_coverage_counts_and_no_price_inference(self):
        entries = [make_entry(status="withdrawn"),
                   make_entry(scheme_id="SLG-TEST2-2024-02-01", scheme_type="merger")]
        coverage = ledger.build_coverage(
            entries, [index_row(), index_row()],
            {"rows": 815, "scheme_days": 346, "symbols": 303})
        self.assertEqual(coverage["ledger_rows"], 2)
        self.assertEqual(coverage["terms_verified"], 0)
        self.assertEqual(coverage["needs_pdf_ratios"], 2)
        self.assertEqual(coverage["withdrawn"], 1)
        self.assertEqual(coverage["scheme_days_needs_pdf"], 344)
        self.assertFalse(coverage["ratios_inferred_from_price"])
        self.assertEqual(coverage["pdfs_downloaded"], 0)
        self.assertEqual(coverage["effort_to_full_coverage"]["person_weeks_high_at_40h"], 8.65)

    def test_roundtrip_jsonl_preserves_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            entry = make_entry()
            ledger.write_jsonl(path, [entry])
            loaded = [json.loads(x) for x in path.read_text().splitlines()]
            self.assertEqual(ledger.validate_entry(loaded[0])["scheme_id"],
                             entry["scheme_id"])
            for field in ledger.SCHEMA_FIELDS:
                self.assertIn(field, loaded[0])

    def test_priority_queue_uses_price_as_signal_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            moves = Path(tmp) / "moves.csv"
            moves.write_text("date,symbol,series,isin,close,prior_close,raw_return\n"
                             "2024-01-02,BIG,EQ,,10,1,9.0\n"
                             "2024-01-02,SMALL,EQ,,10,8,0.25\n")
            entries = [make_entry()]
            entries[0]["parties"][0]["symbol"] = "SMALL"
            queue = ledger.priority_queue(moves, entries, {"BIG", "SMALL"})
            self.assertEqual(queue[0]["symbol"], "SMALL")
            self.assertEqual(queue[1]["symbol"], "BIG")
            self.assertEqual(queue[1]["priority_reason"], "price_discontinuity")
            self.assertTrue(all(q["ratio_from_price"] is False for q in queue))

    def test_builtin_sample_passes_validation(self):
        rows = ledger.load_index(
            Path(__file__).resolve().parents[1] / "data/processed/schemes/scheme-index.jsonl")
        entries = ledger.collapse_filings(rows)
        self.assertTrue(all(ledger.validate_entry(e) for e in entries))
        self.assertTrue(all(e["ratio_num"] is None for e in entries))


if __name__ == "__main__":
    unittest.main()
