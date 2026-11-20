"""Check unresolved NSE dates against the official pre-2011 bhavcopy archive.

A downloaded report proves a session existed and that our earlier source omitted
it. A 404 is recorded as absence, not promoted to a holiday claim. The audit
module remains responsible for classifying closures from official holiday data.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import zipfile
from datetime import date
from pathlib import Path
from nse_pipeline.paths import data_root

from nse_pipeline.download.client import download, report_date, write_json

DEFAULT_DATA = data_root()
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
OLD_BASE = "https://nsearchives.nseindia.com/content/historical/EQUITIES"


def old_bhavcopy_url(day: date) -> str:
    month = MONTHS[day.month - 1]
    name = f"cm{day.day:02d}{month}{day.year}bhav.csv.zip"
    return f"{OLD_BASE}/{day.year}/{month}/{name}"


def candidate_dates(audit: dict, recheck_closures: bool = False) -> list[date]:
    allowed = {"unresolved", "confirmed_source_gap"}
    if recheck_closures:
        allowed.add("confirmed_exchange_closure")
    dates = []
    for item in audit["sessions"]["missing_dates"]:
        if item["classification"] not in allowed:
            continue
        dates.append(date.fromisoformat(item["date"]))
    return sorted(set(dates))


def processed_dates(data: Path) -> set[date]:
    inventory = data / "manifests" / "price-report-inventory.json"
    if not inventory.exists():
        return set()
    payload = json.loads(inventory.read_text())
    return {date.fromisoformat(row["date"]) for row in payload["reports"]}


def backfill(data: Path, audit_path: Path, recheck_closures: bool = False,
             delay: float = 0.5) -> dict:
    audit = json.loads(audit_path.read_text())
    already = processed_dates(data)
    results = []
    blocked = False
    for day in candidate_dates(audit, recheck_closures):
        if day in already:
            results.append({"date": day.isoformat(), "status": "already_present"})
            continue
        url = old_bhavcopy_url(day)
        name = url.rsplit("/", 1)[-1]
        target = data / "raw" / "recent" / "prices" / name
        if target.exists() and report_date(target.name) == day:
            results.append({"date": day.isoformat(), "status": "cached",
                            "url": url, "file": target.name})
            continue
        try:
            record = download(url, target)
        except urllib.error.HTTPError as error:
            status = "absent" if error.code == 404 else "http_error"
            results.append({"date": day.isoformat(), "status": status,
                            "http_status": error.code, "url": url})
            if error.code in (401, 403, 429) or error.code >= 500:
                blocked = True
                break
        else:
            with zipfile.ZipFile(target) as archive:
                members = [m for m in archive.infolist() if m.filename.lower().endswith(".csv")]
            if len(members) != 1:
                results.append({"date": day.isoformat(), "status": "unexpected_zip",
                                "url": url, "members": [m.filename for m in members]})
                blocked = True
                break
            results.append({"date": day.isoformat(), "status": "downloaded",
                            "url": url, "file": target.name, **record})
        time.sleep(delay)
    summary = {}
    for row in results:
        summary[row["status"]] = summary.get(row["status"], 0) + 1
    report = {
        "audit": str(audit_path),
        "candidate_count": len(results),
        "summary": summary,
        "blocked": blocked,
        "downloaded_sessions": [r["date"] for r in results if r["status"] == "downloaded"],
        "absent_dates": [r["date"] for r in results if r["status"] == "absent"],
        "unexpected": [r for r in results if r["status"] in ("http_error", "unexpected_zip")],
        "results": results,
        "note": "Absence from this archive is evidence, not proof that the exchange was closed.",
    }
    write_json(data / "reports" / "official-gap-backfill.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill missing NSE sessions from official archives")
    parser.add_argument("--data-dir", type=Path, default=data_root())
    parser.add_argument("--audit", type=Path, default=None)
    parser.add_argument("--recheck-closures", action="store_true",
                        help="Also probe dates already classified as closures")
    parser.add_argument("--delay", type=float, default=0.5)
    args = parser.parse_args()
    audit_path = args.audit or args.data_dir / "reports" / "coverage-audit.json"
    report = backfill(args.data_dir, audit_path, args.recheck_closures, args.delay)
    print(json.dumps({k: report[k] for k in ("candidate_count", "summary", "blocked")}, indent=2))
    print(f"Report: {args.data_dir / 'reports' / 'official-gap-backfill.json'}")


if __name__ == "__main__":
    main()
