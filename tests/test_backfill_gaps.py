from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.audit.gaps import backfill, candidate_dates, old_bhavcopy_url

TEMP_ROOT = Path.home() / "AppData" / "Local" / "Temp" / "opencode"
if not TEMP_ROOT.exists():
    TEMP_ROOT = None


def audit_with(*dates: str) -> dict:
    return {"sessions": {"missing_dates": [
        {"date": day, "classification": "unresolved"} for day in dates
    ]}}


class BackfillTests(unittest.TestCase):
    def test_old_bhavcopy_url_uses_locale_independent_month(self):
        self.assertEqual(
            old_bhavcopy_url(date(2000, 1, 3)),
            "https://nsearchives.nseindia.com/content/historical/EQUITIES/2000/JAN/cm03JAN2000bhav.csv.zip",
        )
        self.assertEqual(
            old_bhavcopy_url(date(2010, 12, 31)),
            "https://nsearchives.nseindia.com/content/historical/EQUITIES/2010/DEC/cm31DEC2010bhav.csv.zip",
        )

    def test_candidate_dates_skip_confirmed_closures_by_default(self):
        audit = {"sessions": {"missing_dates": [
            {"date": "2000-01-26", "classification": "confirmed_exchange_closure"},
            {"date": "2000-03-17", "classification": "unresolved"},
            {"date": "2022-05-11", "classification": "confirmed_source_gap"},
        ]}}
        self.assertEqual(candidate_dates(audit), [date(2000, 3, 17), date(2022, 5, 11)])
        self.assertEqual(len(candidate_dates(audit, recheck_closures=True)), 3)

    def test_downloaded_report_is_recorded_and_absent_is_not_a_holiday(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            root = Path(temp)
            (root / "reports").mkdir()
            (root / "manifests").mkdir()
            audit_path = root / "reports" / "coverage-audit.json"
            audit_path.write_text(json.dumps(audit_with("2000-03-17", "2000-01-26")))
            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w") as archive:
                archive.writestr("cm17MAR2000bhav.csv", b"SYMBOL,SERIES\nTEST,EQ\n")

            def fake_download(url, target):
                if "17MAR" in url:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload.getvalue())
                    return {"bytes": len(payload.getvalue()), "sha256": "fixture"}
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

            with patch("nse_pipeline.audit.gaps.download", side_effect=fake_download), patch("nse_pipeline.audit.gaps.time.sleep"):
                report = backfill(root, audit_path, delay=0)
            self.assertEqual(report["summary"]["downloaded"], 1)
            self.assertEqual(report["summary"]["absent"], 1)
            self.assertEqual(report["downloaded_sessions"], ["2000-03-17"])
            self.assertEqual(report["absent_dates"], ["2000-01-26"])


if __name__ == "__main__":
    unittest.main()
