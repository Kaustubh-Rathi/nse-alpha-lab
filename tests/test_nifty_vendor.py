from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.nifty.vendor import (  # noqa: E402
    load_coverage,
    validate_checklist_item,
    validate_comparison,
    validate_template,
    validate_vendor,
    write_coverage,
)

TEMP_ROOT = Path.home() / "AppData" / "Local" / "Temp" / "opencode"
if not TEMP_ROOT.exists():
    TEMP_ROOT = None

ROOT = Path(__file__).resolve().parents[1]


def vendor(**overrides) -> dict:
    base = {
        "vendor_id": "test-vendor",
        "vendor_name": "Test Vendor",
        "product_name": "Test Product",
        "contact_email": "sales@example.com",
        "contact_url": "https://example.com/data",
        "contact_phone": "+91-22-00000000",
        "is_nse_authorised_vendor": False,
        "coverage_start_nifty50": "2008-10-31",
        "coverage_start_nifty500": "2015-10-30",
        "coverage_end": "2026-10-30",
        "coverage_start_source": "public_page",
        "identifiers": ["symbol", "company_name"],
        "provides_effective_dates": True,
        "provides_point_in_time_timestamps": False,
        "provides_gap_report": True,
        "provides_corrections": False,
        "provides_isin": False,
        "dvr_dummy_policy": "unknown_enquiry_required",
        "cost_public": None,
        "licensing_local_research": "unknown_enquiry_required",
        "meets_2000_requirement": False,
        "delivery_formats": ["CSV via SFTP"],
        "verdict": "Enquire before buying.",
        "next_action": "Send the enquiry template.",
        "source_urls": ["https://example.com/data"],
    }
    base.update(overrides)
    return base


def checklist_item(**overrides) -> dict:
    base = {
        "check_id": "earliest-date",
        "question": "What is the earliest date for Nifty 50 constituents?",
        "why_matters": "We need 2000 onwards.",
        "must_have": True,
    }
    base.update(overrides)
    return base


def template(**overrides) -> dict:
    base = {
        "to_email": "indices@nseids.co.in",
        "subject": "Historical Nifty 50/500 constituency enquiry 2000-2026",
        "body": (
            "Dear team, please quote point-in-time Nifty 50 and Nifty 500 "
            "constituents for 2000-01-01 to 2026-11-07 with effective dates, "
            "a gap report, corrections policy, DVR/dummy treatment, and ISINs. "
            "Please confirm local research licensing."
        ),
    }
    base.update(overrides)
    return base


def payload() -> dict:
    vendors = [vendor(vendor_id=f"vendor-{i}", vendor_name=f"Vendor {i}",
                      product_name=f"Product {i}") for i in range(7)]
    checklist = [checklist_item(check_id=f"check-{i}", question=f"Question {i}?",
                                why_matters=f"Why {i} matters.", must_have=i < 5)
                 for i in range(10)]
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "requested_range": {"index_codes": ["NIFTY50", "NIFTY500"],
                            "start": "2000-01-01", "end": "2026-11-07"},
        "vendors": vendors,
        "enquiry_email_template": template(),
        "checklist": checklist,
        "notes": {"method": "web search, no purchase"},
        "tests": {"command": "uv run python -m unittest tests.test_nifty_vendor -v"},
    }


class VendorValidatorTests(unittest.TestCase):
    def test_full_payload_validates(self):
        validate_comparison(payload())

    def test_missing_field_rejected(self):
        record = vendor()
        del record["contact_email"]
        with self.assertRaisesRegex(ValueError, "vendor fields"):
            validate_vendor(record)

    def test_extra_field_rejected(self):
        with self.assertRaisesRegex(ValueError, "vendor fields"):
            validate_vendor(vendor(unexpected=True))

    def test_invalid_date_rejected(self):
        with self.assertRaisesRegex(ValueError, "coverage_start"):
            validate_vendor(vendor(coverage_start_nifty50="31/10/2008"))

    def test_non_boolean_flag_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            validate_vendor(vendor(provides_isin="yes"))

    def test_empty_identifiers_rejected(self):
        with self.assertRaisesRegex(ValueError, "identifiers"):
            validate_vendor(vendor(identifiers=[]))

    def test_meets_2000_requires_early_start(self):
        with self.assertRaisesRegex(ValueError, "2000"):
            validate_vendor(vendor(coverage_start_nifty50="2008-10-31",
                                     coverage_start_nifty500="2015-10-30",
                                     meets_2000_requirement=True))

    def test_start_after_end_rejected(self):
        with self.assertRaisesRegex(ValueError, "after coverage_end"):
            validate_vendor(vendor(coverage_start_nifty50="2027-01-01",
                                   coverage_end="2026-10-30"))

    def test_checklist_item_schema(self):
        validate_checklist_item(checklist_item())
        with self.assertRaisesRegex(ValueError, "must-have|boolean"):
            validate_checklist_item(checklist_item(must_have="yes"))

    def test_template_must_cover_load_bearing_terms(self):
        with self.assertRaisesRegex(ValueError, "template body"):
            validate_template(template(body="Please send your price list."))

    def test_duplicate_vendor_id_rejected(self):
        data = payload()
        data["vendors"][1]["vendor_id"] = data["vendors"][0]["vendor_id"]
        with self.assertRaisesRegex(ValueError, "duplicate vendor_id"):
            validate_comparison(data)

    def test_short_checklist_rejected(self):
        data = payload()
        data["checklist"] = data["checklist"][:5]
        with self.assertRaisesRegex(ValueError, "at least 10"):
            validate_comparison(data)

    def test_write_refuses_overwrite(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            target = Path(tmp) / "nifty-vendor-coverage.json"
            write_coverage(payload(), target)
            with self.assertRaises(FileExistsError):
                write_coverage(payload(), target)

    def test_delivered_coverage_file_validates(self):
        path = ROOT / "data" / "reports" / "nifty-vendor-coverage.json"
        data = load_coverage(path)
        self.assertGreaterEqual(len(data["vendors"]), 7)
        self.assertGreaterEqual(len(data["checklist"]), 10)

    def test_public_price_only_where_genuinely_public(self):
        path = ROOT / "data" / "reports" / "nifty-vendor-coverage.json"
        data = load_coverage(path)
        by_id = {v["vendor_id"]: v for v in data["vendors"]}
        # Direct NSE Indices EOD price is not on any public page: null.
        self.assertIsNone(by_id["nse-indices-eod-direct"]["cost_public"])
        # NSE Corporate Data tariff is genuinely public.
        self.assertIsNotNone(by_id["nse-corporate-data"]["cost_public"])


if __name__ == "__main__":
    unittest.main()
