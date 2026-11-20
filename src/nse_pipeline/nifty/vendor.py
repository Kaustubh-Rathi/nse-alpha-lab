"""Validator for the NSE Indices vendor comparison (purchase-options research).

This module only validates the *comparison schema* for official NSE Indices
historical constituent data (Nifty 50 / Nifty 500, 2000-2026 research need).
It does not purchase, authenticate, or download vendor data.

Fail-closed rules:
- Every vendor record must carry exactly the fields in VENDOR_FIELDS.
- Dates that are known must be ISO YYYY-MM-DD and ordered; unknown
  coverage starts are JSON null, never a guessed date.
- Unknown provenance/contract fields stay null / "enquiry_required";
  a missing public price is null, never an invented number.
- No vendor is marked as meeting the 2000 requirement unless its
  coverage_start is on or before 2000-01-01.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_CUTOFF_START = date(2000, 1, 1)
DEFAULT_CUTOFF_END = date(2026, 11, 7)

VENDOR_FIELDS = (
    "vendor_id",
    "vendor_name",
    "product_name",
    "contact_email",
    "contact_url",
    "contact_phone",
    "is_nse_authorised_vendor",
    "coverage_start_nifty50",
    "coverage_start_nifty500",
    "coverage_end",
    "coverage_start_source",
    "identifiers",
    "provides_effective_dates",
    "provides_point_in_time_timestamps",
    "provides_gap_report",
    "provides_corrections",
    "provides_isin",
    "dvr_dummy_policy",
    "cost_public",
    "licensing_local_research",
    "meets_2000_requirement",
    "delivery_formats",
    "verdict",
    "next_action",
    "source_urls",
)

COVERAGE_START_SOURCES = (
    "public_page",
    "public_tariff",
    "public_catalogue",
    "enquiry_required",
    "not_applicable",
)

DVR_POLICIES = (
    "described",
    "unknown_enquiry_required",
    "not_applicable",
)

LICENSING_VALUES = (
    "allowed_with_subscription",
    "unknown_enquiry_required",
    "not_for_constituent_history",
    "current_only_not_sufficient",
)

TOP_FIELDS = (
    "generated_at_utc",
    "requested_range",
    "vendors",
    "enquiry_email_template",
    "checklist",
    "notes",
    "tests",
)

RANGE_FIELDS = ("index_codes", "start", "end")

TEMPLATE_FIELDS = ("to_email", "subject", "body")

CHECKLIST_FIELDS = ("check_id", "question", "why_matters", "must_have")

SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]\Z")


def _parse_iso_date(value: object) -> date | None:
    """Return a date for a valid YYYY-MM-DD string, else None."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _is_https_url(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.hostname)


def validate_vendor(vendor: dict) -> None:
    """Fail closed on a single vendor record."""
    if not isinstance(vendor, dict):
        raise ValueError("vendor record must be an object")
    keys = set(vendor)
    if tuple(sorted(keys)) != tuple(sorted(VENDOR_FIELDS)):
        missing = sorted(set(VENDOR_FIELDS) - keys)
        extra = sorted(keys - set(VENDOR_FIELDS))
        raise ValueError(f"vendor fields must be exactly {sorted(VENDOR_FIELDS)}; missing={missing} extra={extra}")

    vendor_id = vendor["vendor_id"]
    if not isinstance(vendor_id, str) or not SLUG_RE.fullmatch(vendor_id):
        raise ValueError("vendor_id must be a lowercase slug")
    for field in ("vendor_name", "product_name", "verdict", "next_action"):
        value = vendor[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")

    for field in ("contact_email",):
        value = vendor[field]
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{field} must be a string or null")
    if vendor["contact_email"] is not None and not re.fullmatch(
            r"[^@\s]+@[^@\s]+\.[^@\s]+", vendor["contact_email"]):
        raise ValueError("contact_email is not an email address")
    for field in ("contact_url",):
        value = vendor[field]
        if value is not None and not _is_https_url(value):
            raise ValueError(f"{field} must be an HTTPS URL or null")
    if vendor["contact_phone"] is not None and not isinstance(vendor["contact_phone"], str):
        raise ValueError("contact_phone must be a string or null")

    for field in ("is_nse_authorised_vendor", "provides_effective_dates",
                  "provides_point_in_time_timestamps", "provides_gap_report",
                  "provides_corrections", "provides_isin",
                  "meets_2000_requirement"):
        if not isinstance(vendor[field], bool):
            raise ValueError(f"{field} must be a boolean")

    for field in ("coverage_start_nifty50", "coverage_start_nifty500", "coverage_end"):
        value = vendor[field]
        if value is not None and _parse_iso_date(value) is None:
            raise ValueError(f"{field} must be YYYY-MM-DD or null")
    start50 = _parse_iso_date(vendor["coverage_start_nifty50"])
    start500 = _parse_iso_date(vendor["coverage_start_nifty500"])
    end = _parse_iso_date(vendor["coverage_end"])
    if start50 is not None and end is not None and start50 > end:
        raise ValueError("coverage_start_nifty50 is after coverage_end")
    if start500 is not None and end is not None and start500 > end:
        raise ValueError("coverage_start_nifty500 is after coverage_end")

    if vendor["coverage_start_source"] not in COVERAGE_START_SOURCES:
        raise ValueError("coverage_start_source is not a known value")
    if vendor["dvr_dummy_policy"] not in DVR_POLICIES:
        raise ValueError("dvr_dummy_policy is not a known value")
    if vendor["licensing_local_research"] not in LICENSING_VALUES:
        raise ValueError("licensing_local_research is not a known value")

    identifiers = vendor["identifiers"]
    if not isinstance(identifiers, list) or not identifiers:
        raise ValueError("identifiers must be a non-empty list")
    seen = set()
    for ident in identifiers:
        if not isinstance(ident, str) or not ident.strip():
            raise ValueError("identifiers must be non-empty strings")
        if ident in seen:
            raise ValueError("duplicate identifier")
        seen.add(ident)

    if vendor["cost_public"] is not None and not isinstance(vendor["cost_public"], str):
        raise ValueError("cost_public must be a string or null")

    # The 2000 requirement is earned only by a verified start date.
    if vendor["meets_2000_requirement"]:
        starts = [d for d in (start50, start500) if d is not None]
        if not starts:
            raise ValueError("meets_2000_requirement needs a coverage start date")
        if min(starts) > DEFAULT_CUTOFF_START:
            raise ValueError("meets_2000_requirement needs coverage on or before 2000-01-01")

    delivery = vendor["delivery_formats"]
    if not isinstance(delivery, list) or not delivery:
        raise ValueError("delivery_formats must be a non-empty list")
    for fmt in delivery:
        if not isinstance(fmt, str) or not fmt.strip():
            raise ValueError("delivery_formats entries must be non-empty strings")

    source_urls = vendor["source_urls"]
    if not isinstance(source_urls, list) or not source_urls:
        raise ValueError("source_urls must be a non-empty list")
    for url in source_urls:
        if not _is_https_url(url):
            raise ValueError("source_urls must be HTTPS URLs")


def validate_checklist_item(item: dict) -> None:
    if not isinstance(item, dict):
        raise ValueError("checklist item must be an object")
    if set(item) != set(CHECKLIST_FIELDS):
        raise ValueError(f"checklist fields must be exactly {sorted(CHECKLIST_FIELDS)}")
    if not isinstance(item["check_id"], str) or not SLUG_RE.fullmatch(item["check_id"]):
        raise ValueError("check_id must be a lowercase slug")
    for field in ("question", "why_matters"):
        if not isinstance(item[field], str) or not item[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    if not isinstance(item["must_have"], bool):
        raise ValueError("must_have must be a boolean")


def validate_template(template: dict) -> None:
    if not isinstance(template, dict):
        raise ValueError("enquiry_email_template must be an object")
    if set(template) != set(TEMPLATE_FIELDS):
        raise ValueError(f"template fields must be exactly {sorted(TEMPLATE_FIELDS)}")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", str(template["to_email"])):
        raise ValueError("template to_email is not an email address")
    for field in ("subject", "body"):
        if not isinstance(template[field], str) or not template[field].strip():
            raise ValueError(f"template {field} must be a non-empty string")
    # The template must ask the load-bearing questions; a bare
    # "send price list" note is not a usable enquiry.
    body = unicodedata.normalize("NFC", template["body"]).lower()
    required_terms = (
        "2000", "effective", "gap report", "corrections",
        "dvr", "dummy", "isin",
    )
    missing = [term for term in required_terms if term not in body]
    if missing:
        raise ValueError(f"template body must cover {missing}")


def validate_comparison(payload: dict) -> None:
    """Fail closed on the whole vendor-comparison payload."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if set(payload) != set(TOP_FIELDS):
        raise ValueError(f"top-level fields must be exactly {sorted(TOP_FIELDS)}")

    # generated_at_utc must be a timezone-aware ISO timestamp.
    stamp = payload["generated_at_utc"]
    if not isinstance(stamp, str):
        raise ValueError("generated_at_utc must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("generated_at_utc is not ISO-8601") from exc
    if parsed.utcoffset() is None:
        raise ValueError("generated_at_utc needs a timezone")

    requested = payload["requested_range"]
    if not isinstance(requested, dict) or set(requested) != set(RANGE_FIELDS):
        raise ValueError(f"requested_range fields must be exactly {sorted(RANGE_FIELDS)}")
    if not isinstance(requested["index_codes"], list) or not requested["index_codes"]:
        raise ValueError("requested_range.index_codes must be a non-empty list")
    start = _parse_iso_date(requested["start"])
    end = _parse_iso_date(requested["end"])
    if start is None or end is None or start > end:
        raise ValueError("requested_range start/end must be ordered ISO dates")

    vendors = payload["vendors"]
    if not isinstance(vendors, list) or len(vendors) < 7:
        raise ValueError("vendors must list at least 7 options")
    ids: set[str] = set()
    for vendor in vendors:
        validate_vendor(vendor)
        if vendor["vendor_id"] in ids:
            raise ValueError("duplicate vendor_id")
        ids.add(vendor["vendor_id"])

    validate_template(payload["enquiry_email_template"])

    checklist = payload["checklist"]
    if not isinstance(checklist, list) or len(checklist) < 10:
        raise ValueError("checklist must contain at least 10 questions")
    check_ids: set[str] = set()
    for item in checklist:
        validate_checklist_item(item)
        if item["check_id"] in check_ids:
            raise ValueError("duplicate check_id")
        check_ids.add(item["check_id"])
    must_haves = [i for i in checklist if i["must_have"]]
    if len(must_haves) < 5:
        raise ValueError("checklist needs at least 5 must-have questions")

    if not isinstance(payload["notes"], dict):
        raise ValueError("notes must be an object")
    if not isinstance(payload["tests"], dict):
        raise ValueError("tests must be an object")
    if not isinstance(payload["tests"].get("command"), str) or not payload["tests"]["command"]:
        raise ValueError("tests.command is required")


def write_coverage(payload: dict, path: Path) -> Path:
    """Validate then write a new coverage JSON without overwriting."""
    validate_comparison(payload)
    if path.exists():
        raise FileExistsError(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as target:
        json.dump(payload, target, indent=2, ensure_ascii=False)
        target.write("\n")
    return path


def load_coverage(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_comparison(payload)
    return payload
