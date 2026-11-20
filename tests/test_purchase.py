from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.nifty.purchase import (  # noqa: E402
    EMAIL_BODY,
    RECIPIENT,
    SUBJECT,
    build_pack,
    decide_purchase,
    estimate_gap_size,
    load_pack,
    render_checklist_md,
    render_checklist_pdf,
    render_eml,
    render_flowchart_mmd,
    render_sample_request,
    validate_checklist,
    validate_email,
    validate_eml_bytes,
    validate_flowchart,
    validate_gap_request,
    validate_pack,
    write_pack,
)

TEMP_ROOT = Path.home() / "AppData" / "Local" / "Temp" / "opencode"
if not TEMP_ROOT.exists():
    TEMP_ROOT = None

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "data" / "reports" / "nifty-purchase-pack.json"
RAW = ROOT / "data" / "raw" / "nifty_purchase"


def fresh_pack() -> dict:
    return build_pack(datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))


class PurchasePackTests(unittest.TestCase):
    def test_full_pack_validates_and_email_renders(self):
        pack = fresh_pack()
        validate_pack(pack)
        self.assertEqual(pack["recipient"], "indices@nseids.co.in")
        validate_eml_bytes(render_eml())
        validate_email(RECIPIENT, SUBJECT, EMAIL_BODY)

    def test_email_must_cover_load_bearing_terms(self):
        with self.assertRaisesRegex(ValueError, "email body"):
            validate_email(RECIPIENT, SUBJECT, "Please send your price list.")
        with self.assertRaisesRegex(ValueError, "recipient"):
            validate_email("sales@example.com", SUBJECT, EMAIL_BODY)
        bad = render_eml().replace(b"indices@nseids.co.in", b"other@example.com")
        with self.assertRaisesRegex(ValueError, "eml To|recipient"):
            validate_eml_bytes(bad)

    def test_gap_request_is_gap_only(self):
        req = fresh_pack()["gap_request"]
        validate_gap_request(req)
        self.assertEqual(req["start"], "2000-01-01")
        self.assertEqual(req["end"], "2007-12-31")
        for bad in (
            dict(req, end="2008-01-31"),
            dict(req, start="2008-01-01"),
            dict(req, include_post_baseline_repurchase=True),
            dict(req, cost_public="INR 500000"),
            dict(req, sample_dates=["2010-01-04"]),
        ):
            with self.assertRaises(ValueError):
                validate_gap_request(bad)

    def test_size_estimate_arithmetic_and_no_invented_cost(self):
        est = estimate_gap_size()
        self.assertEqual(est["months"], 96)
        self.assertEqual(est["monthly_rows_estimate"], 52800)
        self.assertEqual(est["daily_rows_estimate"], 1108800)
        pack = fresh_pack()
        self.assertIsNone(pack["gap_request"]["cost_public"])
        with self.assertRaisesRegex(ValueError, "start is after"):
            estimate_gap_size(date(2008, 1, 1), date(2007, 1, 1))

    def test_checklist_is_the_16_item_list(self):
        items = fresh_pack()["checklist"]
        validate_checklist(items)
        self.assertEqual(len(items), 16)
        self.assertGreaterEqual(sum(i["must_have"] for i in items), 10)
        with self.assertRaisesRegex(ValueError, "exactly"):
            validate_checklist(items[:15])
        dup = copy.deepcopy(items)
        dup[1]["check_id"] = dup[0]["check_id"]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_checklist(dup)

    def test_flowchart_pay_is_guarded_by_written_2000(self):
        flow = fresh_pack()["decision_flowchart"]
        validate_flowchart(flow)
        mmd = render_flowchart_mmd()
        self.assertIn("written_2000", mmd)
        self.assertIn("do_not_pay", mmd)
        broken = copy.deepcopy(flow)
        broken["edges"] = [e for e in broken["edges"]
                           if e["to_node"] != "pay_allowed"] + [
            {"from_node": "send_enquiry", "to_node": "pay_allowed", "label": "direct"}]
        with self.assertRaisesRegex(ValueError, "pay_allowed|direct"):
            validate_flowchart(broken)

    def test_decide_purchase_only_on_written_2000(self):
        ok = decide_purchase(written_2000_confirmation=True,
                             earliest_date="2000-01-01",
                             gap_report_received=True, sample_verified=True,
                             licence_confirmed=True)
        self.assertTrue(ok["pay_allowed"])
        self.assertEqual(ok["verdict"], "PAY_ALLOWED")
        # Auto-reply / no written confirmation -> never pay.
        no = decide_purchase(written_2000_confirmation=False,
                             earliest_date="2000-01-01",
                             gap_report_received=True, sample_verified=True,
                             licence_confirmed=True)
        self.assertFalse(no["pay_allowed"])
        # Written but coverage starts after 2000 -> never pay.
        late = decide_purchase(written_2000_confirmation=True,
                               earliest_date="2008-01-31",
                               gap_report_received=True, sample_verified=True,
                               licence_confirmed=True)
        self.assertFalse(late["pay_allowed"])
        self.assertTrue(any("after 2000" in r for r in late["reasons"]))

    def test_sample_request_and_checklist_rendering(self):
        sample = render_sample_request()
        for term in ("2000-01-03", "2007-12-31", "52,800", "enquiry_required"):
            self.assertIn(term, sample)
        md = render_checklist_md()
        self.assertEqual(md.count("★"), 13)  # 12 must-have items + header note
        pdf = render_checklist_pdf()
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn(b"earliest-date", pdf)
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))

    def test_write_refuses_overwrite_and_round_trips(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            target = Path(tmp) / "pack.json"
            write_pack(fresh_pack(), target)
            with self.assertRaises(FileExistsError):
                write_pack(fresh_pack(), target)
            self.assertEqual(load_pack(target)["recipient"], RECIPIENT)

    def test_delivered_pack_json_validates(self):
        pack = load_pack(PACK)
        self.assertEqual(pack["gap_request"]["end"], "2007-12-31")
        self.assertEqual(len(pack["checklist"]), 16)
        self.assertTrue(pack["notes"]["no_email_sent"])
        self.assertTrue(pack["notes"]["no_purchase_made"])

    def test_delivered_raw_files_match_module(self):
        eml = (RAW / "enquiry_indices_nseids.eml").read_bytes()
        self.assertEqual(eml, render_eml())
        validate_eml_bytes(eml)
        txt = (RAW / "enquiry_indices_nseids.txt").read_text(encoding="utf-8")
        self.assertIn(f"To: {RECIPIENT}", txt)
        self.assertIn(SUBJECT, txt)
        self.assertEqual((RAW / "sample_request_2000-2008.txt").read_text(encoding="utf-8"),
                         render_sample_request())
        self.assertEqual((RAW / "checklist.md").read_text(encoding="utf-8"),
                         render_checklist_md())
        pdf = (RAW / "checklist.pdf").read_bytes()
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual((RAW / "decision_flowchart.mmd").read_text(encoding="utf-8"),
                         render_flowchart_mmd())
        report = (ROOT / "data" / "reports" / "nifty-purchase-pack.md").read_text(encoding="utf-8")
        for term in ("indices@nseids.co.in", "2008-01-31", "written_2000",
                     "52,800", "DO NOT PAY"):
            self.assertIn(term, report)


if __name__ == "__main__":
    unittest.main()
