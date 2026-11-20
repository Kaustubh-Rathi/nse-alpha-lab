import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "canonical", ROOT / "src/nse_pipeline/schemes/canonical.py")
canonical = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(canonical)


class CanonicalBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)

    def write(self, relative, value, jsonl=False):
        path = self.data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        text = ("".join(json.dumps(row) + "\n" for row in value) if jsonl
                else json.dumps(value))
        path.write_text(text, encoding="utf-8")
        return path

    def queue(self, rows):
        return self.write("processed/schemes/verified3/ranked-scheme-days.json",
                          {"rows": rows})

    def read_events(self):
        return [json.loads(line) for line in (
            self.data / "processed/ledger/canonical-events.jsonl"
        ).read_text(encoding="utf-8").splitlines()]

    def leg(self, scheme_id="SLG-A-2019-01-01", **overrides):
        row = {
            "scheme_id": scheme_id, "scheme_type": "merger", "rank": 1,
            "parties": [{"symbol": "A", "company_name": "A Limited", "role": "transferor"},
                        {"company_name": "B Limited", "role": "transferee"}],
            "dates": {"announced": "2019-01-01"}, "ratio_num": 2, "ratio_den": 3,
            "cash_per_share": None, "successor_company_name": "B Limited",
            "successor_symbol": None, "pdf_url": "https://nsearchives.nseindia.com/corporate/a.pdf",
            "page_ref": "p.1", "snippet": "2 shares for every 3 shares",
            "verified_by": "reader", "terms_verified": True,
            "snippet_verified_on_page": True, "status": "approved",
        }
        row.update(overrides)
        return row

    def verified(self, rows, folder="verified3"):
        return self.write(f"processed/schemes/{folder}/verified-ledger.jsonl", rows, True)

    def test_all_official_days_get_one_unresolved_event_without_touching_inputs(self):
        path = self.queue([
            {"rank": 1, "symbol": "A", "ex_date": "2011-01-20",
             "purposes": "SCHEME OF ARRANGEMENT", "series_rows": 2},
            {"rank": 2, "symbol": "B", "ex_date": "2026-09-02",
             "purposes": "CAP REDN/CONSOLIDATION", "series_rows": 1},
        ])
        original = path.read_bytes()
        coverage = canonical.build(self.data)
        events = self.read_events()
        self.assertEqual(coverage["scheme_days"], 2)
        self.assertEqual(coverage["execution_ready"], 0)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["scheme_type"], "capital_reduction")
        self.assertIn("capital_reduction_terms_missing", events[1]["unresolved_reasons"])
        self.assertTrue(all(not e["terms_verified"] for e in events))
        self.assertEqual(events[0]["dates"], {"ex_date": "2011-01-20"})
        self.assertEqual(path.read_bytes(), original)
        first = (self.data / "processed/ledger/canonical-events.jsonl").read_bytes()
        self.assertEqual(canonical.build(self.data), coverage)
        self.assertEqual(first, (self.data / "processed/ledger/canonical-events.jsonl").read_bytes())

    def test_rank_does_not_apply_other_transferor_ratio_and_keeps_counterparties(self):
        self.queue([{"rank": 1, "symbol": "A", "ex_date": "2019-06-01", "purposes": "MERGER"}])
        own = self.leg()
        other = self.leg("SLG-A-2019-01-01-OTHER", ratio_num=99,
                         parties=[{"company_name": "Private Limited", "role": "transferor"},
                                  {"company_name": "B Limited", "role": "transferee"}])
        self.verified([own, other, own])
        coverage = canonical.build(self.data)
        event, = self.read_events()
        self.assertEqual(len(event["legs"]), 1)
        self.assertEqual(event["ratio_num"], 2)
        self.assertTrue(event["terms_verified"])
        self.assertFalse(event["execution_ready"])
        self.assertIn("successor_symbol_unverified", event["unresolved_reasons"])
        self.assertEqual(coverage["document_legs"], 2)
        related, = event["related_document_legs"]
        self.assertEqual(related["scheme_id"], other["scheme_id"])
        self.assertIn("holder_party_not_linked", related["linkage_reasons"])
        self.assertIn("Private Limited", [p.get("company_name") for p in event["parties"]])

    def test_ambiguous_raymond_and_future_filings_never_supply_earlier_entitlement(self):
        self.queue([
            {"rank": 1, "symbol": "A", "ex_date": "2024-07-11", "purposes": "DEMERGER"},
            {"rank": 2, "symbol": "A", "ex_date": "2025-05-14", "purposes": "DEMERGER"},
        ])
        lifestyle = self.leg("SLG-A-LIFESTYLE", rank=None, scheme_type="demerger",
                             ratio_num=4, ratio_den=5, dates={"announced": "2023-04-27"})
        realty = self.leg("SLG-A-REALTY", rank=None, scheme_type="demerger",
                          ratio_num=1, ratio_den=1, dates={"announced": "2024-07-04"})
        future = self.leg("SLG-A-FUTURE", rank=1, dates={"announced": "2026-01-01"})
        self.verified([lifestyle, realty, future])
        coverage = canonical.build(self.data)
        for event in self.read_events():
            self.assertEqual(event["legs"], [])
            self.assertIsNone(event["ratio_num"])
            self.assertFalse(event["terms_verified"])
            self.assertIn("document_event_link_unresolved", event["unresolved_reasons"])
        self.assertEqual(coverage["document_terms_verified"], 3)

    def test_no_consideration_incomplete_and_capital_reduction_are_not_entitlements(self):
        self.queue([
            {"rank": 1, "symbol": "A", "ex_date": "2019-06-01", "purposes": "MERGER"},
            {"rank": 2, "symbol": "B", "ex_date": "2019-06-02", "purposes": "CAP REDN/CONSOLIDATION"},
        ])
        no_issue = self.leg(ratio_num=None, ratio_den=None, no_cash_consideration=True)
        incomplete = self.leg("INCOMPLETE", page_ref=None)
        unrelated_swap = self.leg("CAPITAL", rank=2, parties=[
            {"symbol": "B", "role": "transferee", "company_name": "B Limited"},
            {"role": "transferor", "company_name": "Private Limited"}])
        self.verified([no_issue, incomplete, unrelated_swap])
        canonical.build(self.data)
        first, second = self.read_events()
        self.assertFalse(first["terms_verified"])
        self.assertEqual(first["legs"], [])
        self.assertTrue(all(not e["execution_ready"] for e in (first, second)))
        self.assertIsNone(first["cash_per_share"])
        self.assertIn("no_holder_entitlement", first["related_document_legs"][0]["linkage_reasons"])
        self.assertIn("capital_reduction_terms_missing", second["unresolved_reasons"])

    def test_withdrawal_is_deduplicated_and_never_demotes_other_scheme_same_symbol(self):
        self.queue([{"rank": 1, "symbol": "A", "ex_date": "2019-06-01", "purposes": "MERGER"}])
        old = self.leg()
        new = self.leg("SLG-A-OTHER", ratio_num=4)
        self.verified([old, new])
        withdrawn = dict(old, status="withdrawn", terms_verified=False,
                         ratio_num=None, ratio_den=None)
        self.write("processed/ledger/scheme-ledger.jsonl", [withdrawn, withdrawn], True)
        coverage = canonical.build(self.data)
        event, = self.read_events()
        self.assertEqual([leg["scheme_id"] for leg in event["legs"]], [new["scheme_id"]])
        self.assertEqual(coverage["document_legs"], 2)
        self.assertEqual(coverage["withdrawn_document_legs"], 1)
        docs = [json.loads(line) for line in (
            self.data / "processed/ledger/canonical-document-legs.jsonl"
        ).read_text().splitlines()]
        withdrawn_doc = next(d for d in docs if d["scheme_id"] == old["scheme_id"])
        self.assertIsNone(withdrawn_doc["ratio_num"])
        self.assertIsNone(withdrawn_doc["successor_symbol"])
        self.assertFalse(withdrawn_doc["execution_ready"])

    def test_stale_record_date_and_name_only_successor_are_not_execution_evidence(self):
        self.queue([{"rank": 1, "symbol": "A", "ex_date": "2020-09-24", "purposes": "DEMERGER"}])
        old = self.leg(scheme_type="demerger", successor_symbol="B",
                       dates={"announced": "2019-04-22", "record_date": "2019-05-06"})
        self.verified([old])
        canonical.build(self.data)
        event, = self.read_events()
        self.assertEqual(event["legs"], [])
        self.assertIsNone(event["successor_symbol"])
        related, = event["related_document_legs"]
        self.assertIn("record_date_precedes_official_ex_date", related["linkage_reasons"])
        self.assertIsNone(related["successor_symbol"])
        self.assertEqual(related["unverified_successor_symbol"], "B")

    def test_official_cached_withdrawal_demotes_only_dropped_khl_leg(self):
        self.queue([{"rank": 1, "symbol": "INDIAGLYCO", "ex_date": "2026-09-02",
                     "purposes": "DEMERGER"}])
        text = (ROOT / "data/processed/schemes/pdf_text/"
                "INDIAGLYCO_16052025174230_Reg30Demerger.txt").read_text(encoding="utf-8")
        cache = self.data / "processed/schemes/pdf_text/INDIAGLYCO_16052025174230_Reg30Demerger.txt"
        cache.parent.mkdir(parents=True)
        cache.write_text(text, encoding="utf-8")
        demerger = self.leg("SLG-INDIAGLYCO-2025-05-16-EBL", scheme_type="demerger",
                            parties=[{"symbol": "INDIAGLYCO", "role": "demerged_company"},
                                     {"company_name": "Ennature Bio Pharma Limited", "role": "resulting_company"}])
        self.verified([demerger])
        coverage = canonical.build(self.data)
        self.assertEqual(coverage["withdrawn_document_legs"], 1)
        event, = self.read_events()
        self.assertEqual([leg["scheme_id"] for leg in event["legs"]], [demerger["scheme_id"]])
        dropped, = event["related_document_legs"]
        self.assertEqual(dropped["scheme_id"], "SLG-INDIAGLYCO-2025-02-04-KHL")
        self.assertTrue(dropped["withdrawal_verified"])
        self.assertIsNone(dropped["ratio_num"])

    def test_invalid_or_conflicting_verified_terms_are_quarantined_not_rank_selected(self):
        self.queue([{"rank": 1, "symbol": "A", "ex_date": "2019-06-01", "purposes": "MERGER"}])
        variants = [self.leg(), self.leg(ratio_num=9)]
        invalid = [self.leg("ZERO", ratio_den=0), self.leg("HALF", ratio_den=None),
                   self.leg("PRICE", extraction_source="inferred from price"),
                   self.leg("NAN", cash_per_share=float("nan"))]
        self.verified(variants + invalid)
        coverage = canonical.build(self.data)
        event, = self.read_events()
        self.assertEqual(event["legs"], [])
        self.assertFalse(event["terms_verified"])
        self.assertEqual(coverage["document_terms_verified"], 0)
        conflict = next(d for d in event["related_document_legs"] if d["scheme_id"] == variants[0]["scheme_id"])
        self.assertIn("conflicting_verified_terms", conflict["document_issues"])
        self.assertEqual(len(conflict["conflicting_source_rows"]), 2)

    def test_later_stub_with_explicit_filing_trail_merges_without_losing_company_names(self):
        self.queue([{"rank": 1, "symbol": "A", "ex_date": "2019-06-01", "purposes": "MERGER"}])
        verified = self.leg(filing_trail=["later.txt (NCLT restatement of this leg)"])
        self.verified([verified])
        stub = self.leg("SLG-A-LATER", terms_verified=False,
                        pdf_url="https://nsearchives.nseindia.com/corporate/later.pdf",
                        parties=[{"company_name": "Extra Counterparty Limited", "role": "counterparty"}])
        self.write("processed/ledger/scheme-ledger.jsonl", [stub], True)
        coverage = canonical.build(self.data)
        self.assertEqual(coverage["document_legs"], 1)
        event, = self.read_events()
        self.assertEqual(len(event["legs"]), 1)
        self.assertIn("Extra Counterparty Limited", [p.get("company_name") for p in event["parties"]])

    def test_cached_integration_covers_346_ranked_plus_50_older_days_without_claiming_completeness(self):
        sources = ["processed/schemes/verified3/ranked-scheme-days.json",
                   "processed/ledger/scheme-ledger.jsonl",
                   "processed/older_actions/nse-api-actions.jsonl"]
        sources += [f"processed/schemes/{batch}/verified-ledger.jsonl"
                    for batch in ("verified", "verified2", "verified3")]
        sources += ["processed/schemes/pdf_text/" + filename for filename in (
            "DIL_08112024121416_Intimation_-_Withdrawal_of_Scheme.txt",
            "INDIAGLYCO_16052025174230_Reg30Demerger.txt")]
        for relative in sources:
            dest = self.data / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "data" / relative, dest)
        before = {rel: (self.data / rel).read_bytes() for rel in sources}
        coverage = canonical.build(self.data)
        self.assertEqual(coverage["scheme_days"], 396)
        self.assertEqual(coverage["ranked_scheme_days"], 346)
        self.assertEqual(coverage["pre2011_scheme_days"], 50)
        self.assertEqual(coverage["pre2011_events_added"], 50)
        self.assertEqual(coverage["document_terms_verified"], 47)
        self.assertEqual(coverage["terms_verified"], 25)
        self.assertEqual(coverage["execution_ready"], 0)
        self.assertEqual(coverage["unresolved_events"], 396)
        self.assertEqual(coverage["withdrawn_document_legs"], 2)
        self.assertFalse(coverage["full_requested_span_ready"])
        self.assertEqual(coverage["queue_ex_date_min"], "2011-01-20")
        self.assertEqual(coverage["event_ex_date_min"], "2000-09-27")
        events = self.read_events()
        self.assertEqual(len({(e["symbol"], e["ex_date"]) for e in events}), 396)
        older = [e for e in events if e["rank"] is None]
        self.assertEqual(len(older), 50)
        self.assertTrue(all(not e["execution_ready"] and not e["terms_verified"] for e in older))
        first = next(e for e in older if e["symbol"] == "STERLITIND")
        self.assertEqual(first["scheme_type"], "capital_reduction")
        self.assertEqual(first["older_action_source_rows"][0]["source_row"], 188)
        self.assertEqual(first["older_action_source_rows"][0]["source_file"], "nse-api-2000.json")
        gallantt = next(e for e in events if e["symbol"] == "GALLISPAT")
        self.assertEqual((gallantt["ratio_num"], gallantt["ratio_den"]), (13, 14))
        self.assertEqual(len(gallantt["related_document_legs"]), 4)
        monnet = next(e for e in events if e["symbol"] == "MONNETISPA")
        self.assertEqual(monnet["scheme_type"], "capital_reduction")
        self.assertEqual(monnet["legs"], [])
        self.assertTrue(all(e["legs"] == [] for e in events if e["symbol"] == "RAYMOND"))
        cesc = next(e for e in events if e["symbol"] == "CESC")
        self.assertEqual(cesc["scheme_type"], "demerger")
        self.assertEqual(len(cesc["legs"]), 2)
        self.assertIsNone(cesc["ratio_num"])
        self.assertEqual(before, {rel: (self.data / rel).read_bytes() for rel in sources})
        self.assertTrue((self.data / "reports/canonical-schemes.md").exists())
        self.assertEqual((self.data / "processed/ledger/canonical-scheme-ledger.jsonl").read_bytes(),
                         (self.data / "processed/ledger/canonical-events.jsonl").read_bytes())

    def test_conflicting_type_date_or_multiple_origin_symbols_need_explicit_leg_binding(self):
        self.queue([{"rank": 1, "symbol": "A", "ex_date": "2019-06-01", "purposes": "MERGER"}])
        wrong_type = self.leg("WRONG-TYPE", scheme_type="demerger")
        wrong_date = self.leg("WRONG-DATE", dates={"announced": "2019-01-01", "ex_date": "2019-07-01"})
        multi_origin = self.leg("MULTI-ORIGIN", parties=[
            {"symbol": "A", "role": "transferor"}, {"symbol": "C", "role": "transferor"},
            {"company_name": "B Limited", "role": "transferee"}])
        self.verified([wrong_type, wrong_date, multi_origin])
        canonical.build(self.data)
        event, = self.read_events()
        self.assertEqual(event["legs"], [])
        self.assertEqual(event["dates"], {"ex_date": "2019-06-01"})
        reasons = {r for d in event["related_document_legs"] for r in d["linkage_reasons"]}
        self.assertIn("scheme_type_conflicts_with_official_purpose", reasons)
        self.assertIn("document_ex_date_conflicts_with_official_queue", reasons)
        self.assertIn("multiple_holder_parties_not_disambiguated", reasons)

    def test_missing_or_empty_official_queue_fails_closed_instead_of_reporting_zero_gaps(self):
        with self.assertRaises(FileNotFoundError):
            canonical.build(self.data)
        self.queue([])
        with self.assertRaisesRegex(ValueError, "empty"):
            canonical.build(self.data)
        self.assertFalse((self.data / "reports/canonical-schemes.json").exists())

    def test_pre2011_scheme_labels_deduplicate_preserve_identity_and_do_not_change_ranked_links(self):
        self.queue([{"rank": 1, "symbol": "A", "ex_date": "2019-06-01", "purposes": "MERGER"}])
        self.verified([self.leg(rank=None)])
        canonical.build(self.data)
        ranked_before, = self.read_events()
        row = {"symbol": "A", "ex_date": "2000-09-27", "exDate": "27-Sep-2000",
               "subject": "Capital Red./Demerger", "comp": "Returned Current Name Limited",
               "isin": "INE000000001", "series": "EQ", "action_tags": ["scheme"],
               "source_file": "nse-api-2000.json", "source_row": 188,
               "source_url": "https://www.nseindia.com/api/corporates-corporateActions?index=equities",
               "identity_status": "NSE_returned_identity_not_historical_symbol_resolved",
               "adjustment_status": "not_applied"}
        second = dict(row, series="BE", source_row=189, isin="INE000000002")
        mixed = dict(row, symbol="CASHLABEL", ex_date="2006-06-09", exDate="09-Jun-2006",
                     action_tags=["dividend", "scheme"], subject="Scheme-Dist.-Rs.11 Per Sh")
        source = self.write("processed/older_actions/nse-api-actions.jsonl",
                            [row, second, mixed, dict(row, action_tags=["dividend"])], True)
        original = source.read_bytes()
        coverage = canonical.build(self.data)
        events = self.read_events()
        ranked_after = next(e for e in events if e["rank"] is not None)
        self.assertEqual(ranked_after, ranked_before)
        self.assertEqual(coverage["scheme_days"], 3)
        self.assertEqual(coverage["ranked_scheme_days"], 1)
        self.assertEqual(coverage["pre2011_scheme_days"], 2)
        self.assertEqual(coverage["pre2011_scheme_input_rows"], 3)
        self.assertEqual(coverage["pre2011_duplicate_rows_collapsed"], 1)
        early = next(e for e in events if e["symbol"] == "A" and e["rank"] is None)
        self.assertIsNone(early["rank_source"])
        self.assertEqual(early["ex_date_source"], "processed/older_actions/nse-api-actions.jsonl")
        self.assertEqual(early["older_action_source_rows"], [row, second])
        self.assertEqual(early["scheme_type"], "capital_reduction")
        self.assertEqual(early["identity_status"], row["identity_status"])
        self.assertFalse(early["historical_identity_verified"])
        self.assertIn("historical_security_identity_unresolved", early["unresolved_reasons"])
        self.assertTrue(all(not e["execution_ready"] for e in events))
        self.assertTrue(all(not e["terms_verified"] for e in events if e["rank"] is None))
        self.assertTrue(all(e["cash_per_share"] is None for e in events))
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(coverage["full_requested_span_ready"])

    def test_older_labels_require_valid_official_ex_dates_not_record_dates_or_unofficial_urls(self):
        self.queue([{"rank": 1, "symbol": "A", "ex_date": "2019-06-01", "purposes": "MERGER"}])
        good = {"symbol": "OLD", "ex_date": "2010-11-01", "exDate": "01-Nov-2010",
                "subject": "Scheme Of Arrangement", "action_tags": ["scheme"],
                "source_url": "https://www.nseindia.com/api/corporates-corporateActions?index=equities"}
        invalid = [dict(good, ex_date=None, recDate="01-Nov-2010"),
                   dict(good, ex_date="2010-02-30"),
                   dict(good, ex_date="2010-11-02"),
                   dict(good, symbol=""),
                   dict(good, source_url="https://example.com/corporates-corporateActions")]
        outside = dict(good, ex_date="2011-11-01", exDate="01-Nov-2011")
        self.write("processed/older_actions/nse-api-actions.jsonl", [good, *invalid, outside], True)
        coverage = canonical.build(self.data)
        self.assertEqual(coverage["scheme_days"], 2)
        self.assertEqual(coverage["pre2011_scheme_days"], 1)
        self.assertEqual(coverage["pre2011_rejected_scheme_row_count"], 5)
        rejected = coverage["pre2011_rejected_scheme_rows"]
        self.assertEqual(len(rejected), 5)
        self.assertEqual(rejected[0]["source_record"], invalid[0])
        self.assertEqual(coverage["pre2011_out_of_scope_scheme_rows"], 1)













    def signoff(self, **overrides):
        row = {
            "event_id": "CANON-A-2019-06-01",
            "filing_url": "https://nsearchives.nseindia.com/corporate/a.pdf",
            "page_ref": "p.1", "snippet_verified_on_page": True, "verified_by": "reviewer",
            "ratio_num": 2, "ratio_den": 3, "cash_per_share": None,
            "ratio_direction": canonical.RATIO_DIRECTION,
            "transferor_symbol": "A", "transferor_isin": "INE000A01004",
            "transferee_symbol": "B", "transferee_isin": "INE000B01003",
            "successor_symbol": "B", "successor_isin": "INE000B01003",
            "official_ex_date": "2019-06-01", "listing_date": "2019-08-01",
        }
        row.update(overrides)
        return row

    def test_unsigned_event_has_checklist_and_binding_errors(self):
        self.queue([{"rank": 1, "symbol": "A", "ex_date": "2019-06-01", "purposes": "MERGER"}])
        self.verified([self.leg()])
        coverage = canonical.build(self.data)
        event, = self.read_events()
        self.assertEqual([i["item"] for i in event["evidence_checklist"]],
                         list(canonical.CHECKLIST_ITEMS))
        self.assertEqual(event["manual_signoff_status"], "none")
        self.assertIsNone(event["manual_signoff"])
        self.assertFalse(event["execution_ready"])
        by_item = {i["item"]: i["status"] for i in event["evidence_checklist"]}
        self.assertEqual(by_item["holder_terms_verified"], "satisfied")
        self.assertEqual(by_item["official_ex_date_binding_verified"], "missing")
        self.assertEqual(by_item["successor_identity_verified"], "missing")
        self.assertEqual(coverage["manual_signoffs_loaded"], 0)
        self.assertEqual(coverage["evidence_checklist_version"], canonical.EVIDENCE_CHECKLIST_VERSION)

    def test_valid_signoff_promotes_to_execution_ready(self):
        self.queue([{"rank": 1, "symbol": "A", "ex_date": "2019-06-01", "purposes": "MERGER"}])
        self.verified([self.leg()])
        path = self.data / "processed/schemes/manual-signoffs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.signoff()) + "\n", encoding="utf-8")
        coverage = canonical.build(self.data)
        event, = self.read_events()
        self.assertTrue(event["execution_ready"])
        self.assertEqual(event["status"], "execution_ready")
        self.assertEqual(event["unresolved_reasons"], [])
        self.assertEqual(coverage["manual_signoffs_valid"], 1)
        self.assertEqual(coverage["execution_ready_event_ids"], ["CANON-A-2019-06-01"])
        self.assertTrue(all(i["status"] == "satisfied" for i in event["evidence_checklist"]))
        self.assertTrue(event["legs"][0]["ex_date_link_verified"])

    def test_mismatched_or_unofficial_signoff_never_promotes(self):
        self.queue([{"rank": 1, "symbol": "A", "ex_date": "2019-06-01", "purposes": "MERGER"}])
        self.verified([self.leg()])
        path = self.data / "processed/schemes/manual-signoffs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.signoff(official_ex_date="2019-06-02")) + "\n", encoding="utf-8")
        coverage = canonical.build(self.data)
        event, = self.read_events()
        self.assertFalse(event["execution_ready"])
        self.assertEqual(coverage["manual_signoffs_rejected"], 1)
        self.assertIn("manual_signoff_official_ex_date_mismatches_event",
                      coverage["manual_signoff_rejected"][0]["errors"])
        self.assertTrue(any(b["code"].startswith("manual_signoff") for b in event["binding_errors"]))
        self.assertEqual(coverage["binding_error_events"], 1)

    def test_binding_errors_reflect_doc_linkage_and_block_checklist(self):
        self.queue([{"rank": 1, "symbol": "A", "ex_date": "2019-06-01", "purposes": "MERGER"}])
        self.verified([self.leg("WRONG-TYPE", scheme_type="demerger")])
        canonical.build(self.data)
        event, = self.read_events()
        self.assertTrue(event["binding_errors"])
        self.assertTrue(any(b["code"] == "scheme_type_conflicts_with_official_purpose"
                            for b in event["binding_errors"]))
        by_item = {i["item"]: i["status"] for i in event["evidence_checklist"]}
        self.assertEqual(by_item["doc_event_binding_clean"], "blocked")

if __name__ == "__main__":
    unittest.main()
