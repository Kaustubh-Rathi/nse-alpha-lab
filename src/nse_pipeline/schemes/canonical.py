"""Offline, fail-closed reconciliation of official scheme-days and PDF terms.

``build(data_root)`` preserves every input and writes only canonical-* ledger
artifacts and canonical-schemes reports. An event is a distinct official
(symbol, ex_date), NOT a filing or a document leg. Document verification alone
does not establish a trading entitlement or an exchange successor identity.

The ranked queue and its links retain their original IDs/ranks. Additional
pre-2011 ``scheme`` labels from ``older_actions/nse-api-actions.jsonl`` are
appended as unresolved, unranked stubs; complete original source rows and
returned-identity uncertainties remain available on each stub. Coverage
separates ``ranked_scheme_days`` from ``pre2011_scheme_days``; ``scheme_days``
and ``unresolved_events`` cover their deduplicated union, not just the ranks.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from nse_pipeline.paths import data_root
from urllib.parse import urlparse

BANNED_PRICE_FIELDS = {
    "price_factor", "share_multiplier", "inferred_ratio", "inferred_from_price",
    "price_discontinuity_ratio", "implied_ratio_from_return", "raw_return_used",
}

EVIDENCE_CHECKLIST_VERSION = "canonical_evidence_checklist_v1"
SIGNOFF_SCHEMA_VERSION = "canonical_manual_signoff_v1"
MANUAL_SIGNOFF_PATH = "processed/schemes/manual-signoffs.jsonl"
RATIO_DIRECTION = "successor_shares_per_holder_share"
CHECKLIST_ITEMS = (
    "holder_terms_verified",
    "official_ex_date_binding_verified",
    "ratio_direction_verified",
    "successor_identity_verified",
    "listing_date_verified",
    "source_citation_complete",
    "doc_event_binding_clean",
    "security_identity_verified",
)
SIGNOFF_FIELDS = (
    "event_id", "filing_url", "page_ref", "snippet_verified_on_page", "verified_by",
    "ratio_num", "ratio_den", "cash_per_share", "ratio_direction",
    "transferor_symbol", "transferor_isin", "transferee_symbol", "transferee_isin",
    "successor_symbol", "successor_isin", "official_ex_date", "listing_date",
)
_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")


def _official_pdf(url) -> bool:
    try:
        parsed = urlparse(str(url or ""))
        host = parsed.hostname or ""
        return (parsed.scheme == "https"
                and (host == "nseindia.com" or host.endswith(".nseindia.com"))
                and parsed.path.lower().endswith(".pdf"))
    except ValueError:
        return False


def _iso_date(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _valid_ratio(row: dict) -> bool:
    num, den, cash = (row.get(f) for f in ("ratio_num", "ratio_den", "cash_per_share"))
    if cash is not None:
        return (num is None and den is None and type(cash) in (int, float)
                and math.isfinite(cash) and cash >= 0)
    return (type(num) is int and type(den) is int and num > 0 and den > 0)


def validate_manual_signoff(row: dict, event: dict | None = None) -> list[str]:
    """Strictly validate one reviewer signoff (fail-closed; returns error codes)."""
    if not isinstance(row, dict):
        return ["manual_signoff_not_an_object"]
    errors = []
    if row.get("_pending") is True:
        errors.append("manual_signoff_pending_template")
    if row.get("event_id") is None or row.get("event_id") == "":
        errors.append("manual_signoff_event_id_missing")
    if not _official_pdf(row.get("filing_url")):
        errors.append("manual_signoff_filing_url_not_official_pdf")
    page_ref = row.get("page_ref")
    if not isinstance(page_ref, str) or not re.fullmatch(r"p\.\d+", page_ref):
        errors.append("manual_signoff_page_ref_missing")
    if row.get("snippet_verified_on_page") is not True:
        errors.append("manual_signoff_snippet_not_verified_on_page")
    if not isinstance(row.get("verified_by"), str) or not row["verified_by"].strip():
        errors.append("manual_signoff_verified_by_missing")
    if not _valid_ratio(row):
        errors.append("manual_signoff_ratio_invalid")
    if row.get("ratio_direction") != RATIO_DIRECTION:
        errors.append("manual_signoff_ratio_direction_invalid")
    for field in ("transferor_symbol", "transferee_symbol", "successor_symbol"):
        value = row.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9.\-]{1,30}", value):
            errors.append(f"manual_signoff_{field}_missing_or_invalid")
    for field in ("transferor_isin", "transferee_isin", "successor_isin"):
        value = row.get(field)
        if not isinstance(value, str) or not _ISIN.fullmatch(value):
            errors.append(f"manual_signoff_{field}_missing_or_invalid")
    if not _iso_date(row.get("official_ex_date")):
        errors.append("manual_signoff_official_ex_date_invalid")
    if not _iso_date(row.get("listing_date")):
        errors.append("manual_signoff_listing_date_invalid")
    if event is not None:
        if row.get("event_id") != event["event_id"]:
            errors.append("manual_signoff_event_id_mismatches_event")
        if row.get("official_ex_date") != event.get("ex_date"):
            errors.append("manual_signoff_official_ex_date_mismatches_event")
        if row.get("transferor_symbol") != event.get("symbol"):
            errors.append("manual_signoff_transferor_symbol_mismatches_event")
        legs = event.get("legs") or []
        if legs and not any(leg.get("ratio_num") == row.get("ratio_num")
                            and leg.get("ratio_den") == row.get("ratio_den")
                            and leg.get("cash_per_share") == row.get("cash_per_share")
                            for leg in legs):
            errors.append("manual_signoff_ratio_mismatches_verified_legs")
    return errors


def _load_manual_signoffs(data_root: Path) -> tuple[dict, list[dict]]:
    """Load optional reviewer signoffs; missing file means zero signoffs (never guess)."""
    path = data_root / MANUAL_SIGNOFF_PATH
    if not path.exists():
        return {}, []
    try:
        raw = path.read_text(encoding="utf-8")
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    except (OSError, ValueError) as error:
        return {}, [{"code": "manual_signoff_file_unparseable", "error": str(error)}]
    if len(rows) > 1000:
        return {}, [{"code": "manual_signoff_file_too_large", "count": len(rows)}]
    by_event, seen = {}, set()
    errors = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("event_id") in seen:
            errors.append({"index": idx, "code": "manual_signoff_duplicate_or_not_object",
                           "event_id": row.get("event_id") if isinstance(row, dict) else None})
            continue
        seen.add(row.get("event_id"))
        by_event[row.get("event_id")] = row
    return by_event, errors


def _binding_errors(event: dict, rejected_signoffs: list[dict]) -> list[dict]:
    """Translate doc->event linkage/document issues + signoff failures into machine errors."""
    binding = []
    for leg in event.get("related_document_legs", []):
        for reason in leg.get("linkage_reasons", []):
            binding.append({"code": reason, "document_id": leg.get("scheme_id"),
                            "detail": "doc->event binding rejected"})
        for issue in leg.get("document_issues", []):
            binding.append({"code": issue, "document_id": leg.get("scheme_id"),
                            "detail": "document issue blocks binding"})
    for leg in event.get("legs", []):
        for issue in leg.get("document_issues", []):
            binding.append({"code": issue, "document_id": leg.get("scheme_id"),
                            "detail": "document issue on candidate leg"})
    for entry in rejected_signoffs:
        for code in entry.get("errors", []):
            binding.append({"code": code, "document_id": None,
                            "detail": "manual signoff rejected"})
    deduped, seen = [], set()
    for item in binding:
        key = (item["code"], item["document_id"])
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def _evidence_checklist(event: dict, signoff: dict | None, binding: list[dict]) -> list[dict]:
    """Machine-readable per-event checklist; ``satisfied`` drives execution_ready only."""
    legs = event.get("legs") or []
    have_terms = bool(event.get("terms_verified")) and bool(legs)
    cited = bool(legs) and all(leg.get("pdf_url") and leg.get("page_ref") and leg.get("snippet")
                               for leg in legs)

    def item(name, status, detail):
        return {"item": name, "status": status, "detail": detail}

    return [
        item("holder_terms_verified",
             "satisfied" if have_terms else "missing",
             "candidate holder terms verified" if have_terms else "no verified holder leg"),
        item("official_ex_date_binding_verified",
             "satisfied" if signoff and signoff.get("official_ex_date") == event.get("ex_date")
             else "missing",
             "signoff official_ex_date matches queue ex_date" if signoff else "no signoff with official_ex_date"),
        item("ratio_direction_verified",
             "satisfied" if signoff and signoff.get("ratio_direction") == RATIO_DIRECTION
             else "missing",
             RATIO_DIRECTION if signoff else "no signoff ratio direction"),
        item("successor_identity_verified",
             "satisfied" if signoff and signoff.get("successor_symbol") and signoff.get("successor_isin")
             else "missing",
             "successor symbol+ISIN signed off" if signoff else "no successor symbol/ISIN signoff"),
        item("listing_date_verified",
             "satisfied" if signoff and _iso_date(signoff.get("listing_date")) else "missing",
             "listing_date signed off" if signoff else "no listing_date signoff"),
        item("source_citation_complete",
             "satisfied" if cited and (signoff is None or bool(signoff.get("filing_url")))
             else "missing",
             "leg filing URL/page/snippet cited" if cited else "leg citation incomplete"),
        item("doc_event_binding_clean",
             "satisfied" if not binding else "blocked",
             "no doc->event binding errors" if not binding else f"{len(binding)} binding error(s)"),
        item("security_identity_verified",
             "satisfied" if signoff and signoff.get("transferor_symbol") == event.get("symbol")
             and _ISIN.fullmatch(str(signoff.get("transferor_isin") or ""))
             else "missing",
             "transferor symbol/ISIN signed off" if signoff else "no transferor symbol/ISIN signoff"),
    ]


def _finalize_signoffs(events: list[dict], by_event: dict, load_errors: list[dict]) -> dict:
    """Attach binding errors + checklist; promote to execution_ready only if fully signed off."""
    rejected, valid = [], []
    applied = 0
    for event in events:
        entry = by_event.get(event["event_id"])
        signoff = None
        rejected_signoffs: list[dict] = []
        if entry is not None:
            errs = validate_manual_signoff(entry, event)
            if errs:
                rejected_signoffs.append({"errors": errs, "signoff": entry})
                rejected.append({"event_id": event["event_id"], "errors": errs})
            else:
                signoff = copy.deepcopy(entry)
                valid.append(event["event_id"])
                applied += 1
        binding = _binding_errors(event, rejected_signoffs)
        checklist = _evidence_checklist(event, signoff, binding)
        event["binding_errors"] = binding
        event["evidence_checklist"] = checklist
        event["evidence_checklist_version"] = EVIDENCE_CHECKLIST_VERSION
        event["manual_signoff_status"] = ("valid" if signoff else "rejected" if rejected_signoffs
                                          else "none")
        event["manual_signoff"] = signoff
        if signoff and all(item["status"] == "satisfied" for item in checklist):
            event["execution_ready"] = True
            event["status"] = "execution_ready"
            event["unresolved_reasons"] = []
            for leg in event.get("legs", []):
                leg["ex_date_link_verified"] = True
    return {
        "manual_signoffs_loaded": len(by_event),
        "manual_signoffs_valid": len(valid),
        "manual_signoffs_rejected": len(rejected),
        "manual_signoff_load_errors": load_errors,
        "manual_signoff_rejected": rejected,
        "manual_signoff_valid_event_ids": valid,
        "manual_signoff_schema_version": SIGNOFF_SCHEMA_VERSION,
        "manual_signoff_path": MANUAL_SIGNOFF_PATH,
        "manual_signoff_fields": list(SIGNOFF_FIELDS),
        "manual_signoff_ratio_direction": RATIO_DIRECTION,
        "evidence_checklist_version": EVIDENCE_CHECKLIST_VERSION,
        "evidence_checklist_items": list(CHECKLIST_ITEMS),
        "checklist_satisfied": dict(sorted(Counter(
            item["item"] for event in events for item in event["evidence_checklist"]
            if item["status"] == "satisfied").items())),
        "checklist_blocked": dict(sorted(Counter(
            item["item"] for event in events for item in event["evidence_checklist"]
            if item["status"] == "blocked").items())),
        "binding_error_events": sum(1 for event in events if event["binding_errors"]),
    }


def _json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                    encoding="utf-8")


def _scheme_type(purpose: str) -> str:
    text = re.sub(r"de[-\s]+merger", "demerger", purpose.lower())
    if "cap red" in text or "capital red" in text:
        return "capital_reduction"
    if "consol" in text:
        return "capital_consolidation"
    if "demerg" in text:
        return "demerger"
    if "merger" in text or "amalgam" in text:
        return "merger"
    return "scheme_of_arrangement"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _cached_withdrawals(data_root: Path) -> list[dict]:
    """Two bounded readings; clauses are rechecked in cached official pages.

    The modified India Glycols scheme preserves its two demergers: only KHL
    is dropped. A search for 'withdrawal' on a symbol would incorrectly cancel
    those valid legs, so neither generic keyword matching nor symbol demotion
    is used here.
    """
    readings = [
        ("SLG-DIL-2024-06-28", "DIL_08112024121416_Intimation_-_Withdrawal_of_Scheme.txt",
         "2024-06-28", "2024-11-08", "DIL", "Debock Industries Limited",
         "Debock Ventures Limited",
         "The withdrawal was necessitated by the directive issued by the Securities and Exchange Board"),
        ("SLG-INDIAGLYCO-2025-02-04-KHL", "INDIAGLYCO_16052025174230_Reg30Demerger.txt",
         "2025-02-04", "2025-05-16", "INDIAGLYCO", "India Glycols Limited",
         "Kashipur Holding Limited",
         "Accordingly, Part I which deals with the amalgamation of KHL into IGL will no longer form part of the Modified Scheme and has been deleted from the Modified Scheme."),
    ]
    result = []
    for key, filename, announced, withdrawn_on, symbol, survivor, origin, snippet in readings:
        path = data_root / "processed/schemes/pdf_text" / filename
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        parts = re.split(r"===== PAGE (\d+) =====", text)
        pages = {int(no): re.sub(r"\s+", " ", body).strip()
                 for no, body in zip(parts[1::2], parts[2::2])}
        citation = re.search(r"^# pdf_url: (\S+)", text, re.M)
        if not citation or snippet not in pages.get(1, ""):
            continue
        result.append({
            "scheme_id": key, "scheme_type": "merger", "status": "withdrawn",
            "parties": [{"symbol": symbol, "company_name": survivor, "role": "transferee"},
                        {"company_name": origin, "role": "transferor"}],
            "dates": {"announced": announced, "withdrawal_announced": withdrawn_on},
            "ratio_num": None, "ratio_den": None, "cash_per_share": None,
            "successor_symbol": None, "terms_verified": False,
            "withdrawal_verified": True, "pdf_url": citation.group(1),
            "page_ref": "p.1", "snippet": snippet, "verified_by": "canonical_cached_clause_check",
            "snippet_verified_on_page": True,
            "extraction_source": f"cached_official_withdrawal_clause ({filename})",
            "source_cache": str(path.relative_to(data_root)).replace("\\", "/"),
        })
    return result


def _merge_evidence(existing: dict, incoming: dict, source: str) -> None:
    if source not in existing["source_files"]:
        existing["source_files"].append(source)
    alias = incoming["scheme_id"]
    if alias not in existing["source_scheme_ids"]:
        existing["source_scheme_ids"].append(alias)
    for field in ("parties", "filing_trail"):
        for item in incoming.get(field, []):
            if item not in existing.setdefault(field, []):
                existing[field].append(copy.deepcopy(item))


def _filenames(doc: dict) -> set[str]:
    values = [doc.get("pdf_url")]
    for item in doc.get("filing_trail", []):
        values.append(item.get("pdf_url") if isinstance(item, dict)
                      else item.split(" (")[0])
    return {Path(urlparse(str(value)).path).stem for value in values if value}


def _documents(data_root: Path) -> list[dict]:
    docs = {}
    paths = [data_root / f"processed/schemes/{batch}/verified-ledger.jsonl"
             for batch in ("verified", "verified2", "verified3")]
    paths.append(data_root / "processed/ledger/scheme-ledger.jsonl")
    inputs = [(path, _load_jsonl(path)) for path in paths]
    inputs.insert(0, (data_root / "processed/schemes/pdf_text", _cached_withdrawals(data_root)))
    for path, rows in inputs:
        for raw in rows:
            doc = copy.deepcopy(raw)
            key = doc["scheme_id"]
            source = str(path.relative_to(data_root)).replace("\\", "/")
            if key not in docs and not doc.get("terms_verified") and doc.get("status") != "withdrawn":
                filename = Path(urlparse(str(doc.get("pdf_url") or "")).path).stem
                matches = [d for d in docs.values() if filename and filename in _filenames(d)]
                if matches:
                    for existing in matches:
                        _merge_evidence(existing, doc, source)
                    continue
            if key in docs:
                existing = docs[key]
                if existing.get("terms_verified") and doc.get("terms_verified"):
                    fields = ("scheme_type", "parties", "ratio_num", "ratio_den", "cash_per_share",
                              "successor_company_name")
                    if any(existing.get(f) != doc.get(f) for f in fields):
                        if "conflicting_verified_terms" not in existing["document_issues"]:
                            existing["document_issues"].append("conflicting_verified_terms")
                            existing["conflicting_source_rows"] = [copy.deepcopy(existing)]
                        existing["conflicting_source_rows"].append(doc)
                _merge_evidence(existing, doc, source)
                if doc.get("status") == "withdrawn":
                    docs[key]["status"] = "withdrawn"
                continue
            doc["source_files"] = [source]
            doc["source_scheme_ids"] = [key]
            doc["document_id"] = key
            doc["execution_ready"] = False
            doc["linked_event_ids"] = []
            doc["document_issues"] = []
            required = ("pdf_url", "page_ref", "snippet", "verified_by")
            verified = (doc.get("terms_verified") is True
                        and doc.get("snippet_verified_on_page") is True
                        and all(doc.get(k) for k in required))
            if not verified and not doc.get("withdrawal_verified"):
                doc["document_issues"].append("incomplete_document_verification")
            doc["terms_verified"] = verified
            docs[key] = doc
    for doc in docs.values():
        issues = doc["document_issues"]
        num, den, cash = (doc.get(f) for f in ("ratio_num", "ratio_den", "cash_per_share"))
        if ((num is None) != (den is None) or any(
                v is not None and (type(v) is not int or v <= 0) for v in (num, den))):
            issues.append("invalid_share_ratio")
        if cash is not None and (type(cash) not in (float, int)
                                 or not math.isfinite(cash) or cash < 0):
            issues.append("invalid_cash_consideration")
            doc["cash_per_share"] = None
        source = str(doc.get("extraction_source") or "").lower()
        if any(f in doc for f in BANNED_PRICE_FIELDS) or any(
                token in source for token in ("price", "raw_return", "discontinuity")):
            issues.append("price_derived_terms_forbidden")
        host = urlparse(str(doc.get("pdf_url") or "")).hostname or ""
        if not (host == "nseindia.com" or host.endswith(".nseindia.com")):
            issues.append("official_document_citation_missing")
        for value in (doc.get("dates") or {}).values():
            if value is not None:
                try:
                    date.fromisoformat(value)
                except (TypeError, ValueError):
                    issues.append("invalid_document_date")
                    break
        if issues:
            doc["terms_verified"] = False
        if doc.get("successor_symbol"):
            doc["unverified_successor_symbol"] = doc["successor_symbol"]
            doc["successor_symbol"] = None
            doc["document_issues"].append("successor_exchange_identity_not_verified")
        if doc.get("status") == "withdrawn":
            for field in ("ratio_num", "ratio_den", "cash_per_share", "successor_symbol"):
                doc[field] = None
    return list(docs.values())


def _holder_symbols(doc: dict) -> set[str]:
    return {p["symbol"] for p in doc.get("parties", [])
            if p.get("symbol") and p.get("role") in
            ("transferor", "demerged_company", "target", "dissolved")}


def _reconcile(event: dict, documents: list[dict], events: list[dict]) -> None:
    symbol, ex_date = event["symbol"], event["ex_date"]
    for doc in documents:
        holder = symbol in _holder_symbols(doc)
        rank_match = doc.get("rank") is not None and doc["rank"] == event["rank"]
        party_match = any(p.get("symbol") == symbol for p in doc.get("parties", []))
        if not rank_match and not party_match:
            continue
        reasons = []
        if doc.get("status") == "withdrawn":
            reasons.append("withdrawn_scheme_leg")
        if not holder:
            reasons.append("holder_party_not_linked")
        elif _holder_symbols(doc) != {symbol}:
            reasons.append("multiple_holder_parties_not_disambiguated")
        if doc.get("rank") is not None and not rank_match:
            reasons.append("different_ranked_scheme_day")
        if not doc["terms_verified"]:
            reasons.append("incomplete_document_verification")
        if doc.get("ratio_num") is None and doc.get("cash_per_share") is None:
            reasons.append("no_holder_entitlement")
        if event["scheme_type"] in ("capital_reduction", "capital_consolidation"):
            reasons.append("capital_action_not_documented_by_holder_swap")
        if (event["scheme_type"] in ("merger", "demerger")
                and doc.get("scheme_type") != event["scheme_type"]):
            reasons.append("scheme_type_conflicts_with_official_purpose")
        dates = doc.get("dates") or {}
        if dates.get("ex_date") and dates["ex_date"] != ex_date:
            reasons.append("document_ex_date_conflicts_with_official_queue")
        announced = dates.get("announced") or dates.get("announcement")
        if isinstance(announced, str) and announced > ex_date:
            reasons.append("filing_after_scheme_day_requires_explicit_link")
        if isinstance(dates.get("record_date"), str) and dates["record_date"] < ex_date:
            reasons.append("record_date_precedes_official_ex_date")
        if not doc.get("rank") and sum(e["symbol"] == symbol for e in events) > 1:
            reasons.append("multiple_same_symbol_days_require_explicit_link")
        leg = copy.deepcopy(doc)
        leg["linkage_reasons"] = reasons
        leg["holder_party_linked"] = holder
        leg["ex_date_link_verified"] = False
        if reasons:
            event["related_document_legs"].append(leg)
        else:
            event["legs"].append(leg)
            doc["linked_event_ids"].append(event["event_id"])
        for party in doc.get("parties", []):
            if party not in event["parties"]:
                event["parties"].append(copy.deepcopy(party))
    legs = event["legs"]
    event["terms_verified"] = bool(legs) and all(leg["terms_verified"] for leg in legs)
    if not legs:
        if event["related_document_legs"]:
            event["unresolved_reasons"].append("document_event_link_unresolved")
        return
    types = {leg["scheme_type"] for leg in legs}
    if len(types) == 1 and event["scheme_type"] == "scheme_of_arrangement":
        event["scheme_type"] = types.pop()
    event["unresolved_reasons"] = ["official_ex_date_to_document_link_unverified"]
    if any(leg.get("ratio_num") is not None and not leg.get("successor_symbol")
           for leg in legs):
        event["unresolved_reasons"].append("successor_symbol_unverified")
    if len(legs) == 1:
        for field in ("ratio_num", "ratio_den", "cash_per_share"):
            event[field] = legs[0].get(field)


def _markdown(coverage: dict) -> str:
    lines = [
        "# Canonical NSE scheme reconciliation", "",
        f"**{coverage['scheme_days']} official scheme-day events; "
        f"{coverage['terms_verified']} with candidate verified holder terms; "
        f"{coverage['execution_ready']} execution-ready; "
        f"{coverage['unresolved_events']} unresolved.**", "",
        f"The three batches contain {coverage['document_terms_verified']} verified document legs, "
        "not that many executable events. Filing rank is a research locator, not an entitlement.", "",
        "## Scope", "",
        f"The ranked 2011+ queue contributes {coverage['ranked_scheme_days']} distinct days "
        f"({coverage['queue_ex_date_min']} to {coverage['queue_ex_date_max']}). "
        f"The cached older-actions NSE API labels contribute {coverage['pre2011_scheme_days']} "
        f"distinct pre-2011 scheme-days from {coverage['pre2011_scheme_input_rows']} source rows. "
        f"The combined event span is {coverage['event_ex_date_min']} to {coverage['event_ex_date_max']}. "
        "Full 2000–2026 scheme coverage is **not established**: dated older labels do not establish "
        "exhaustive historical coverage, historical security identity, or scheme terms. Absence from "
        "either input does not prove absence of historical reorganizations. Fundamentals are outside this repair.", "",
        "## Meaning of the flags", "",
        "- `terms_verified` on a document means a cited, manually checked clause; it does not verify lifecycle execution.",
        "- `terms_verified` on an event means at least one plausible holder-specific document leg passed "
        "the party/rank/date filters. These are provisional terms, not confirmed ex-date bindings.",
        "- `execution_ready` additionally needs a cited official ex-date-to-scheme binding and dated "
        "source/successor security identities. It approves only fully validated manual signoffs.",
        "- Event dates come only from the cached ranked queue or older NSE API `ex_date` labels. "
        "Board, announcement, NCLT, effective, "
        "and record dates never replace a trading ex-date.",
        "- Ranked events retain their IDs and document links. `rank_source` names the ranked "
        "queue; pre-2011 additions have `rank = null` and `rank_source = null` and are not "
        "matched to ranked document legs. `older_action_source_rows` retains every original "
        "label and its API URL/file/row, returned name/ISIN/series, and identity uncertainty.",
        "- Multi-child entitlements remain separate `legs`; the event's scalar ratio stays null.",
        "- No-consideration clauses, other transferors' swaps and withdrawals never become shareholder entitlements.",
        "- All original files are preserved; the stale live ledger is not overwritten.", "",
        "## Unresolved reason counts", "", "Counts overlap: one event may have several blockers.", "",
        "| reason | events |", "|---|---:|",
    ]
    lines += [f"| {reason} | {count} |" for reason, count in coverage["unresolved_reasons"].items()]
    lines += ["", "## Specific hazards retained", "",
              "- Raymond Realty 1:1 and Lifestyle/RCCL 4:5 are different schemes. "
              "Neither is assigned by nearest same-symbol date; both remain unbound where cached evidence is ambiguous.",
              "- Gallantt admits only the listed Gallantt Ispat 13:14 candidate leg, "
              "not the four other transferors' ratios on the same page.",
              "- Monnet's Milloret 1:1 swap is not a Monnet shareholder entitlement "
              "and does not resolve its capital reduction.",
              "- Man Industries' cited 2019 record date predates its queued 2020 ex-date; "
              "that document/event link remains unresolved.",
              "- Adani Enterprises' retrospective allotment filing is not attached automatically "
              "to either earlier scheme-day. Monsanto's repeated merger days do not duplicate its candidate leg.",
              "- The cached India Glycols amendment drops KHL alone; both surviving demerger "
              "legs remain separate. DIL is withdrawn. Withdrawal checks reread the cached cited page.", "",
              "## Bounded successor verification proposal", "",
              "No successor ticker is filled from a company name, a brand occurrence, prices, "
              "or a current symbol list. The inspected scheme text caches do not establish the required "
              "dated exchange listing identities and event-specific ex-date bindings. Start with the "
              "single-holder Tinplate/Tata Steel and Monsanto/Bayer document legs: require a cached NSE "
              "admission/merger circular naming both legal entities, source and successor symbols/ISINs, "
              "and trading ex-date; then resolve each to the historical dataset security key and separately "
              "verify listing/tradability, fractions, and cash treatment. This is a verification queue, "
              "not an approved alias map or a fabricated fill.", "",
              "## Evidence checklist and manual signoff", "",
              "Each event carries an `evidence_checklist` (8 items) and `binding_errors` "
              "(doc->event linkage, document, and rejected-signoff errors). "
              "`execution_ready` requires every checklist item satisfied via a valid signoff "
              "(official filing URL/page, ratio direction, "
              "transferor/transferee/successor symbols+ISINs, official ex-date matching the "
              "queue, and listing date). Missing signoff = missing, contradicted binding = "
              "blocked; neither is guessed.", "",
              "No cached SANOFI/Raymond filing contains an explicit NSE ex-date sentence plus "
              "successor symbol and listing date, so no binding was added for them; they "
              "remain unresolved pending a reviewer signoff.", "",
              "## Artifacts", "",
              "- `data/processed/ledger/canonical-scheme-ledger.jsonl`: preferred integration ledger, one row per official day.",
              "- `data/processed/ledger/canonical-events.jsonl`: identical event export.",
              "- `data/processed/ledger/canonical-document-legs.jsonl`: all deduplicated document legs, including unbound terms and withdrawals.",
              "- `data/processed/ledger/canonical-coverage.json` and `data/reports/canonical-schemes.json`: exact coverage and per-event unresolved details.", ""]
    return "\n".join(lines)


def _append_pre2011(data_root: Path, events: list[dict]) -> dict:
    """Append dated labels only after ranked reconciliation, never changing its links."""
    relative = "processed/older_actions/nse-api-actions.jsonl"
    path = data_root / relative
    rows = _load_jsonl(path)
    groups = {}
    rejected = []
    out_of_scope = 0
    for line, row in enumerate(rows, 1):
        if "scheme" not in (row.get("action_tags") or []):
            continue
        reasons = []
        ex_date = row.get("ex_date")
        try:
            parsed = date.fromisoformat(ex_date)
            if parsed.isoformat() != ex_date:
                raise ValueError("non-canonical ISO date")
        except (TypeError, ValueError):
            parsed = None
            reasons.append("missing_or_invalid_official_ex_date")
        if parsed is not None and not date(2000, 1, 1) <= parsed < date(2011, 1, 1):
            out_of_scope += 1
            continue
        if not isinstance(row.get("symbol"), str) or not row["symbol"].strip():
            reasons.append("missing_NSE_returned_symbol")
        if row.get("exDate") not in (None, "", "-"):
            try:
                original = datetime.strptime(row["exDate"], "%d-%b-%Y").date()
                if parsed is not None and original != parsed:
                    reasons.append("official_ex_date_fields_conflict")
            except (TypeError, ValueError):
                reasons.append("invalid_original_NSE_exDate")
        try:
            url = urlparse(str(row.get("source_url") or ""))
            host = url.hostname or ""
            official = (url.scheme == "https"
                        and (host == "nseindia.com" or host.endswith(".nseindia.com"))
                        and url.path == "/api/corporates-corporateActions")
        except ValueError:
            official = False
        if not official:
            reasons.append("official_NSE_action_source_missing")
        if reasons:
            rejected.append({"source_line": line, "reasons": reasons,
                             "source_record": copy.deepcopy(row)})
            continue
        groups.setdefault((row["symbol"], ex_date), []).append(row)
    by_key = {(e["symbol"], e["ex_date"]): e for e in events}
    overlap = 0
    for (symbol, ex_date), source_rows in sorted(groups.items(), key=lambda item: (item[0][1], item[0][0])):
        if (symbol, ex_date) in by_key:
            by_key[symbol, ex_date]["older_action_source_rows"] = copy.deepcopy(source_rows)
            overlap += 1
            continue
        purposes = " | ".join(sorted({r.get("subject") or "" for r in source_rows}))
        kind = _scheme_type(purposes)
        statuses = sorted({r.get("identity_status") or "not_supplied" for r in source_rows})
        parties = []
        for row in source_rows:
            party = {"symbol": symbol, "company_name": row.get("comp"),
                     "isin": row.get("isin"), "series": row.get("series"), "role": "primary"}
            if party not in parties:
                parties.append(party)
        reasons = ["no_verified_holder_terms", "pre2011_scheme_terms_unverified",
                   "historical_security_identity_unresolved"]
        if kind == "capital_reduction":
            reasons.append("capital_reduction_terms_missing")
        if kind == "capital_consolidation":
            reasons.append("capital_consolidation_terms_missing")
        events.append({
            "scheme_id": f"CANON-{symbol}-{ex_date}", "event_id": f"CANON-{symbol}-{ex_date}",
            "symbol": symbol, "rank": None, "rank_source": None,
            "event_source": "pre2011_nse_api_label", "scheme_type": kind,
            "queue_scheme_type": kind, "dates": {"ex_date": ex_date}, "ex_date": ex_date,
            "ex_date_source": relative, "purposes": purposes, "series_rows": len(source_rows),
            "parties": parties, "identity_status": statuses[0] if len(statuses) == 1 else "conflicting_identity_statuses",
            "identity_statuses": statuses, "historical_identity_verified": False,
            "older_action_source_rows": copy.deepcopy(source_rows),
            "ratio_num": None, "ratio_den": None, "cash_per_share": None,
            "successor_symbol": None, "terms_verified": False, "execution_ready": False,
            "status": "unresolved", "unresolved_reasons": reasons,
            "legs": [], "related_document_legs": [],
        })
    input_rows = sum(len(group) for group in groups.values())
    return {
        "pre2011_source_available": path.exists(), "pre2011_source": relative,
        "pre2011_scheme_input_rows": input_rows, "pre2011_scheme_days": len(groups),
        "pre2011_duplicate_rows_collapsed": input_rows - len(groups),
        "pre2011_ranked_overlap_days": overlap, "pre2011_events_added": len(groups) - overlap,
        "pre2011_rejected_scheme_row_count": len(rejected),
        "pre2011_rejected_scheme_rows": rejected,
        "pre2011_out_of_scope_scheme_rows": out_of_scope,
    }


def build(data_root: Path) -> dict:
    """Build canonical ledger/coverage from cached official sources, without I/O to the network.

    Returns schema-v2 coverage. ``queue_ex_date_min/max`` continue to refer
    to the ranked input; ``event_ex_date_min/max`` span the combined events.
    Missing older-action caches are explicitly reported and never imply
    complete pre-2011 coverage. The ranked queue remains mandatory.
    """
    data_root = Path(data_root)
    queue = data_root / "processed/schemes/verified3/ranked-scheme-days.json"
    ranked = _json(queue, None)
    if ranked is None:
        raise FileNotFoundError(f"Official ranked scheme-day queue missing: {queue}")
    if not ranked.get("rows"):
        raise ValueError(f"Official ranked scheme-day queue is empty: {queue}")
    events = []
    seen = set()
    for day in ranked["rows"]:
        symbol, ex_date = day["symbol"], day["ex_date"]
        date.fromisoformat(ex_date)
        if (symbol, ex_date) in seen:
            continue
        seen.add((symbol, ex_date))
        kind = _scheme_type(day.get("purposes") or "")
        reasons = ["no_verified_holder_terms"]
        if kind == "capital_reduction":
            reasons.append("capital_reduction_terms_missing")
        if kind == "capital_consolidation":
            reasons.append("capital_consolidation_terms_missing")
        events.append({
            "scheme_id": f"CANON-{symbol}-{ex_date}", "event_id": f"CANON-{symbol}-{ex_date}",
            "symbol": symbol, "rank": day.get("rank"), "scheme_type": kind,
            "rank_source": str(queue.relative_to(data_root)).replace("\\", "/"),
            "event_source": "ranked_scheme_day",
            "queue_scheme_type": kind,
            "dates": {"ex_date": ex_date}, "ex_date": ex_date,
            "ex_date_source": str(queue.relative_to(data_root)).replace("\\", "/"),
            "purposes": day.get("purposes"), "series_rows": day.get("series_rows"),
            "parties": [{"symbol": symbol, "role": "primary"}],
            "ratio_num": None, "ratio_den": None, "cash_per_share": None,
            "successor_symbol": None, "terms_verified": False,
            "execution_ready": False, "status": "unresolved",
            "unresolved_reasons": reasons, "legs": [], "related_document_legs": [],
        })
    documents = _documents(data_root)
    for event in events:
        _reconcile(event, documents, events)
    ranked_count = len(events)
    ranked_min = min(e["ex_date"] for e in events)
    ranked_max = max(e["ex_date"] for e in events)
    older = _append_pre2011(data_root, events)
    by_event, load_errors = _load_manual_signoffs(data_root)
    signoff_stats = _finalize_signoffs(events, by_event, load_errors)
    coverage = {
        "schema_version": 2,
        "ranked_scheme_days": ranked_count,
        **older,
        **signoff_stats,
        "execution_ready_event_ids": [e["event_id"] for e in events if e["execution_ready"]],
        "scheme_days": len(events),
        "execution_ready": sum(1 for e in events if e["execution_ready"]),
        "terms_verified": sum(e["terms_verified"] for e in events),
        "unresolved": sum(1 for e in events if not e["execution_ready"]),
        "unresolved_events": sum(1 for e in events if not e["execution_ready"]),
        "document_legs": len(documents),
        "document_terms_verified": sum(d["terms_verified"] for d in documents),
        "withdrawn_document_legs": sum(d.get("status") == "withdrawn" for d in documents),
        "unresolved_reasons": dict(sorted(Counter(
            reason for event in events for reason in event["unresolved_reasons"]).items())),
        "ratios_inferred_from_price": False,
        "requested_years": [2000, 2026],
        "queue_ex_date_min": ranked_min,
        "queue_ex_date_max": ranked_max,
        "event_ex_date_min": min(e["ex_date"] for e in events),
        "event_ex_date_max": max(e["ex_date"] for e in events),
        "full_requested_span_ready": False,
        "pre_2011_coverage_established": False,
        "fundamentals_deferred": True,
        "queue_input_rows": len(ranked["rows"]),
        "duplicate_queue_rows_collapsed": len(ranked["rows"]) - ranked_count,
        "candidate_holder_legs": sum(len(e["legs"]) for e in events),
        "document_verified_with_ratio": sum(d["terms_verified"] and d.get("ratio_num") is not None
                                            for d in documents),
        "document_verified_no_consideration": sum(d["terms_verified"] and d.get("ratio_num") is None
                                                  and d.get("cash_per_share") is None for d in documents),
        "events_by_type": dict(sorted(Counter(e["scheme_type"] for e in events).items())),
        "events_by_year": dict(sorted(Counter(e["ex_date"][:4] for e in events).items())),
        "document_linkage_reasons": dict(sorted(Counter(
            reason for e in events for d in e["related_document_legs"]
            for reason in d["linkage_reasons"]).items())),
        "unresolved_event_details": [
            {"event_id": e["event_id"], "symbol": e["symbol"], "ex_date": e["ex_date"],
             "reasons": e["unresolved_reasons"]} for e in events if not e["execution_ready"]],
        "definitions": {
            "terms_verified": "event has candidate verified holder terms; ex-date binding remains unverified",
            "document_terms_verified": "deduplicated manually verified document clauses, including non-holder legs",
            "execution_ready": "verified holder terms AND fully validated manual signoff satisfying every evidence checklist item",
            "evidence_checklist": "per-event list of 8 items (satisfied/missing/blocked); only all-satisfied permits execution_ready",
            "binding_errors": "machine-readable doc->event linkage + document + rejected-signoff errors per event",
            "manual_signoff": "reviewer row at processed/schemes/manual-signoffs.jsonl; missing file means zero signoffs",
            "rank_source": "ranked queue path for ranked events; null for additional older NSE API labels",
            "queue_ex_date_min_max": "ranked input only; event_ex_date_min_max spans both inputs",
            "pre_2011_coverage_established": "exhaustive historical coverage, not merely existence of dated labels; remains false",
        },
    }
    _jsonl(data_root / "processed/ledger/canonical-events.jsonl", events)
    _jsonl(data_root / "processed/ledger/canonical-scheme-ledger.jsonl", events)
    _jsonl(data_root / "processed/ledger/canonical-document-legs.jsonl", documents)
    _write(data_root / "processed/ledger/canonical-coverage.json", coverage)
    _write(data_root / "reports/canonical-schemes.json", coverage)
    (data_root / "reports/canonical-schemes.md").write_text(_markdown(coverage), encoding="utf-8")
    return coverage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=data_root())
    args = parser.parse_args()
    coverage = build(args.data)
    print(json.dumps({key: coverage[key] for key in (
        "scheme_days", "ranked_scheme_days", "pre2011_scheme_days", "pre2011_events_added",
        "document_legs", "document_terms_verified", "terms_verified",
        "execution_ready", "unresolved_events", "withdrawn_document_legs",
        "manual_signoffs_loaded", "manual_signoffs_valid", "manual_signoffs_rejected",
        "binding_error_events", "checklist_satisfied", "checklist_blocked",
        "unresolved_reasons", "queue_ex_date_min", "queue_ex_date_max",
        "full_requested_span_ready")}, indent=2))


if __name__ == "__main__":
    main()
