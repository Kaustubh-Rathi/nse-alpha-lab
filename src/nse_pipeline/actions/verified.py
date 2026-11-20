"""Cited, fail-closed pre-2011 split/bonus adapter; never reads prices.

Official API ``subject`` supplies numeric terms and ``exDate`` supplies the
exchange ex-date. Annual-report record/effective dates and queue break dates
are retained as evidence, not substituted for ex-dates. Historical ISINs are
never backfilled from the API's present-day security identifiers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from nse_pipeline.paths import data_root
from nse_pipeline.build.portable import open_catalog
from urllib.parse import urlparse


def _date(value) -> date | None:
    if not value or str(value).strip() in ("", "-", "NaT", "None"):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            pass
    return None


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class VerifiedAction:
    action_id: str
    source_kind: str
    source_symbol: str
    symbol: str | None
    series: str | None
    ex_date: date | None
    record_date: date | None
    purpose: str
    action_type: str
    share_multiplier: float | None
    terms_verified: bool
    execution_ready: bool = False
    readiness_reason: str = "missing_exact_exchange_ex_date"
    ex_date_provenance: str | None = None
    identity_status: str = "not_resolved"
    identity_start: date | None = None
    identity_not_before: date | None = None
    historical_isin: str | None = None
    pre_ex_bars: int = 0
    numeric_terms: dict = field(default_factory=dict)
    identity_edges: list[str] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    duplicate_source_rows: int = 1
    actually_applied: bool | None = None
    non_factor: bool = False
    component: str | None = None
    evidence_required: list[str] = field(default_factory=list)

    def adjustment_row(self) -> dict:
        """Existing engine columns plus provenance; unready rows stay excluded."""
        return {
            "symbol": self.symbol, "series": self.series, "ex_date": self.ex_date,
            "record_date": self.record_date, "purpose": self.purpose,
            "action_type": self.action_type, "share_multiplier": self.share_multiplier,
            "dividend_per_share": None,
            "parse_status": "parsed_not_applied" if self.execution_ready else "needs_review",
            "first_observed": None, "last_observed": None,
            "source": "normalized_verified:" + self.action_id,
            "verified_action_id": self.action_id,
            "verified_readiness_reason": self.readiness_reason,
            "factor_start": self.identity_start,
            "ex_date_provenance": self.ex_date_provenance,
            "citations_json": json.dumps(self.citations, ensure_ascii=False, default=str),
        }


_EVIDENCE_BY_REASON: dict[str, list[str]] = {
    "compound_action_ambiguous": [
        "official document confirming each component's numeric terms and "
        "whether split and bonus apply sequentially to the same share base",
        "exact NSE exchange ex-date for the compound event",
        "dated identity evidence for the historical symbol/series on the ex-date",
    ],
    "missing_explicit_supported_numeric_terms": [
        "verbatim stated numeric terms (face values, ratio, or amount) with "
        "units from an official NSE API subject or primary issuer/exchange circular",
        "exact NSE exchange ex-date",
        "dated identity evidence for the historical symbol/series on the ex-date",
    ],
    "missing_exact_exchange_ex_date": [
        "NSE exchange circular/API row stating the exact ex-date; a record, "
        "effective, allotment or price-discontinuity date is not a substitute",
        "dated identity evidence for the historical symbol/series on that ex-date",
    ],
    "unobserved_historical_identity": [
        "dated official symbol-change edge resolving the historical symbol",
        "bar observation on the exact ex-date for the historical symbol and series",
        "historical ISIN observed on the ex-date (never backfilled from a present-day ISIN)",
    ],
    "no_pre_ex_bars": [
        "at least one observed bar before the exact ex-date for the "
        "historical symbol/series in its identity epoch",
    ],
    "conflicting_same_day_actions": [
        "official clarification reconciling the non-identical same-day "
        "split/bonus subjects (which terms supersede, or whether they are "
        "sequential components of one event)",
        "exact NSE exchange ex-date confirmed against the raw API rows",
    ],
    "non_factor_record_no_price_adjustment": [
        "no price factor to apply; stated terms are retained for audit only "
        "and must not be converted into a share multiplier or price adjustment",
    ],
}


def _evidence_for(reason: str | None) -> list[str]:
    return list(_EVIDENCE_BY_REASON.get(reason or "", []))


def _split_terms(subject: str, kinds: list[str]) -> dict:
    if "split" not in kinds:
        return {}
    patterns = [
        # Rs/Re on both face values: "Spl-Rs10 To Rs2", "Rs.10/- To Re.1/-"
        r"\b(?:split|spl)\s*-?\s*(?:from\s*)?(?:rs\.?|re\.?)\s*(\d+(?:\.\d+)?)"
        r"\s*(?:/-?|/)?\s*to\s*(?:rs\.?|re\.?)\s*(\d+(?:\.\d+)?)",
        # Currency on old value only, bare new value: "Rs10to2", "Rs 5/ To 2/"
        r"\b(?:split|spl)\s*-?\s*(?:from\s*)?(?:rs\.?|re\.?)\s*(\d+(?:\.\d+)?)"
        r"\s*(?:/-?|/)?\s*to\s*(\d+(?:\.\d+)?)\s*(?:/-?|/)?(?=\s*(?:/|$|and\b))",
        # "Fv Spl 100 To 10" / "Fv Spl-10 To 5": bare face values, but only
        # under an explicit face-value (Fv) label.
        r"\bfv\s+(?:split|spl)\s*-?\s*(\d+(?:\.\d+)?)\s*(?:/-?|/)?\s*to\s*(\d+(?:\.\d+)?)",
    ]
    for pattern in patterns:
        matches = re.findall(pattern, subject, re.I)
        if len(matches) == 1:
            old, new = map(float, matches[0])
            if math.isfinite(old) and math.isfinite(new) and old > new > 0:
                return {"split": {"old_face_value": old, "new_face_value": new}}
    return {}


def _dividend_terms(subject: str) -> dict:
    if not re.search(r"\bdiv(?:idend)?\b|\bdiv(?=\d)", subject, re.I):
        return {}
    terms: dict = {}
    if re.search(r"\bdiv(?:idend)?\s*-?\s*nil\b", subject, re.I):
        terms["dividend"] = {"nil": True}
        return terms
    amount = re.search(
        r"(?:div(?:idend)?|dividend)[^/]{0,50}?(?:rs\.?|re\.?)\s*(\d+(?:\.\d+)?)"
        r"\s*(?:/-\s*)?(?:per\s*(?:share|sh)|pr\s*sh|pershare)",
        subject, re.I)
    if amount:
        terms["dividend"] = {"amount_per_share": float(amount.group(1))}
        return terms
    amount = re.search(
        r"(?:rs\.?|re\.?)\s*(\d+(?:\.\d+)?)\s*(?:/-\s*)?(?:per\s*(?:share|sh)|pr\s*sh)",
        subject, re.I)
    if amount and re.search(r"\bdiv(?:idend)?\b", subject, re.I):
        # Credit a bare per-share amount only when no split face values are
        # present (split subjects carry Rs amounts that are not dividends).
        if not re.search(r"\b(?:split|spl)\b[^/]{0,50}?\bto\b", subject, re.I):
            terms["dividend"] = {"amount_per_share": float(amount.group(1))}
            return terms
    percent = re.search(r"\bdiv(?:idend)?\s*-?\s*(\d+(?:\.\d+)?)\s*%", subject, re.I) \
        or re.search(r"\bdiv(?:idend)?[^/]{0,30}?(\d+(?:\.\d+)?)\s*%", subject, re.I)
    if percent:
        terms["dividend"] = {"percent_of_face_value": float(percent.group(1))}
    return terms


def _rights_terms(subject: str) -> dict:
    if not re.search(r"\brights?\b", subject, re.I):
        return {}
    terms: dict = {"rights": {}}
    ratio = re.search(r"\brights?\b[^/]{0,50}?(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)",
                      subject, re.I)
    if ratio:
        terms["rights"]["rights_shares"] = float(ratio.group(1))
        terms["rights"]["existing_shares"] = float(ratio.group(2))
    price = re.search(r"(?:@|prem(?:ium)?\s*@?)\s*(?:rs\.?|re\.?)?\s*(\d+(?:\.\d+)?)",
                      subject, re.I)
    if price and ("prem" in subject.lower() or "@" in subject):
        terms["rights"]["issue_price_stated"] = float(price.group(1))
    if re.search(r"\bat\s*par\b", subject, re.I):
        terms["rights"]["at_par"] = True
    return terms


def _scheme_terms(subject: str) -> dict:
    match = re.search(r"\b(demerger|merger|amalgamation|arrangement|scheme|sch)\b",
                      subject, re.I)
    if not match:
        return {}
    detail = re.search(r"\b(capital\s*red(?:uction)?\.?|demerger|merger|amalgamation)\b",
                       subject, re.I)
    return {"scheme": {"scheme_kind": match.group(1).lower(),
                        "detail": detail.group(1).lower() if detail else None}}


def _has_explicit_terms(kind: str, terms: dict) -> bool:
    """True only when parsed numeric terms are explicitly stated.

    A component label alone is not verified terms: ``{"rights": {}}`` and a
    scheme ``scheme_kind`` say what kind of subject it is, not its terms.
    Rights terms are verified only from an explicit rights ratio
    (``rights_shares`` : ``existing_shares``); an issue price or ``at_par``
    without that ratio is retained in ``numeric_terms`` for audit but is
    not a structured rights ratio.  Dividends need an amount, a percent of
    face value, or an explicit nil.  Schemes have no numeric exchange ratio
    in the API parsing, so a bare scheme label never counts as verified
    terms.  No term is ever inferred from price.
    """
    if not terms:
        return False
    if "+" in kind:
        return all(_has_explicit_terms(part, terms) for part in kind.split("+"))
    inner = terms.get(kind)
    if not isinstance(inner, dict) or not inner:
        return False
    if kind in ("bonus", "split"):
        return True
    if kind == "dividend":
        return (inner.get("amount_per_share") is not None
                or inner.get("percent_of_face_value") is not None
                or inner.get("nil") is True)
    if kind == "rights":
        rights_shares = inner.get("rights_shares")
        existing_shares = inner.get("existing_shares")
        return (rights_shares is not None and existing_shares is not None
                and math.isfinite(float(rights_shares))
                and math.isfinite(float(existing_shares))
                and float(rights_shares) > 0 and float(existing_shares) > 0)
    if kind == "scheme":
        return False
    return False


def _api_components(subject: str) -> list[dict]:
    """One entry per explicit component: {kind, terms, multiplier, problem}.

    Split/bonus components carry a share multiplier; dividend/rights/scheme
    components are non-factor records (their terms are stated, never
    converted into a price factor).
    """
    subject = subject or ""
    kinds = [kind for kind, pattern in (
        ("split", r"\bsplit\b|\bsub.?division\b|\bfv\s+spl\b|\bspl(?=\s*-?\s*(?:rs|re)\.?\s*\d+(?:\.\d+)?\s*(?:/-?)?\s*to)|\bfv\s+spl\b"),
        ("bonus", r"\bbon(?:us)?\b"), ("dividend", r"\bdiv(?:idend)?\b|\bdiv(?=\d)"),
        ("rights", r"\brights?\b"),
        ("scheme", r"\b(?:scheme|sch|merger|demerger|amalgamation)\b"),
    ) if re.search(pattern, subject, re.I)]
    # A scheme subject that merely mentions a bonus debenture or a DVR
    # dividend ("Bonus Deb1:1", "Bon 1dvr:10eq") is not an equity bonus.
    equity_bonus = "bonus" in kinds and not re.search(
        r"\b(?:deb\w*|dvr|ads)\b", subject, re.I)
    components = []
    for kind in kinds:
        if kind == "bonus":
            if not equity_bonus:
                components.append({"kind": kind, "terms": {}, "multiplier": None,
                                   "problem": "missing_explicit_supported_numeric_terms"})
                continue
            matches = re.findall(r"\bbon(?:us)?\s*-?\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)",
                                 subject, re.I)
            terms, multiplier = {}, None
            if len(matches) == 1:
                added, held = map(float, matches[0])
                if math.isfinite(added) and math.isfinite(held) and added > 0 and held > 0:
                    terms = {"bonus": {"additional_shares": added, "existing_shares": held}}
                    multiplier = 1 + added / held
            components.append({"kind": kind, "terms": terms, "multiplier": multiplier,
                               "problem": None if terms else "missing_explicit_supported_numeric_terms"})
        elif kind == "split":
            terms = _split_terms(subject, kinds)
            multiplier = (terms["split"]["old_face_value"] / terms["split"]["new_face_value"]
                          if terms else None)
            components.append({"kind": kind, "terms": terms, "multiplier": multiplier,
                               "problem": None if terms else "missing_explicit_supported_numeric_terms"})
        elif kind == "dividend":
            terms = _dividend_terms(subject)
            components.append({"kind": kind, "terms": terms, "multiplier": None,
                               "problem": None, "non_factor": True})
        elif kind == "rights":
            terms = _rights_terms(subject)
            components.append({"kind": kind, "terms": terms, "multiplier": None,
                               "problem": None, "non_factor": True})
        elif kind == "scheme":
            terms = _scheme_terms(subject)
            components.append({"kind": kind, "terms": terms, "multiplier": None,
                               "problem": "missing_explicit_supported_numeric_terms"
                               if not any(k in terms.get("scheme", {}) for k in ()) else None,
                               "non_factor": True})
    return components


def _api_terms(subject: str) -> tuple[str, dict, float | None, str | None]:
    components = _api_components(subject)
    kinds = [c["kind"] for c in components]
    terms = {key: value for c in components for key, value in c["terms"].items()}
    kind = "+".join(kinds) or "other"
    factor = [c for c in components if not c.get("non_factor")]
    multiplier = factor[0]["multiplier"] if len(factor) == 1 else None
    if multiplier is not None and (not math.isfinite(multiplier) or multiplier <= 1):
        multiplier = None
        return kind, terms, None, "invalid_share_multiplier"
    if len(factor) > 1:
        reason = "compound_action_ambiguous"
    elif not terms and kind != "other":
        reason = "missing_explicit_supported_numeric_terms"
    elif factor and not factor[0]["terms"]:
        reason = "missing_explicit_supported_numeric_terms"
    else:
        reason = None
    return kind, terms, multiplier, reason


def _circular_terms(text: str, kind: str) -> tuple[dict, float | None]:
    """Narrow numeric constructions; no bare split word or isolated face value."""
    if kind == "bonus":
        match = re.search(r"\bbonus\b.{0,100}?\b(?:ratio\s+of\s+)?(\d+)\s*:\s*(\d+)", text, re.I)
        if match:
            added, held = map(float, match.groups())
        else:
            match = re.search(r"\b(one|two|three|four|\d+)\s+additional\s+equity\s+shares?\s+for\s+every\s+(?:(one|\d+)\s+)?equity\s+share", text, re.I)
            if not match:
                return {}, None
            words = {"one": 1, "two": 2, "three": 3, "four": 4}
            added = float(words.get(match[1].lower(), match[1]))
            held = float(words.get((match[2] or "").lower(), match[2] or 1))
        if added > 0 and held > 0:
            return {"bonus": {"additional_shares": added, "existing_shares": held}}, 1 + added / held
    elif kind == "split":
        currency = r"(?:rs\.?|re\.?)\s*(\d+(?:\.\d+)?)"
        patterns = (
            r"\b(?:split|sub[-\s]?divided)\b.{0,60}?\bfrom\b.{0,30}?" + currency + r".{0,60}?\bto\b.{0,30}?" + currency,
            r"\bshares\s+of\s+" + currency + r".{0,25}?\b(?:were\s+)?(?:split|sub[-\s]?divided)\s+into\b.{0,80}?\bshares\s+of\s+" + currency,
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                old, new = map(float, match.groups())
                if old > new > 0:
                    return {"split": {"old_face_value": old, "new_face_value": new}}, old / new
    return {}, None


def _exchange_source(url: str | None) -> bool:
    parsed = urlparse(url or "")
    return parsed.scheme == "https" and parsed.hostname in ("www.nseindia.com", "nseindia.com") \
        and parsed.path == "/api/corporates-corporateActions"


def _historical_symbol(symbol: str, ex: date | None, edges: list[dict]
                       ) -> tuple[str, date | None, list[str], str | None]:
    """Walk only official, unbranched, dated reverse rename edges.

    An API's contemporary symbol can be renamed backwards, never by name or
    by a later ISIN. Incoming edges effective before the event bound the
    identity epoch; a stale outgoing symbol at the event fails closed.
    """
    current, used, visited = symbol, [], set()
    if ex is None:
        return current, None, used, None
    while current not in visited:
        visited.add(current)
        incoming = [e for e in edges if e.get("new_symbol") == current]
        outgoing = [e for e in edges if e.get("old_symbol") == current]
        relevant = incoming + outgoing
        if any(e.get("verification") != "official_symbolchange_row" or not _date(e.get("effective_date")) for e in relevant):
            return current, None, used, "unverified_identity_edge"
        if len(incoming) > 1 or len(outgoing) > 1 or any(e.get("bars_order_violation") for e in relevant):
            return current, None, used, "ambiguous_identity_lineage"
        if outgoing and _date(outgoing[0]["effective_date"]) <= ex:
            return current, None, used, "symbol_not_active_at_ex_date"
        if incoming:
            edge = incoming[0]
            effective = _date(edge["effective_date"])
            if effective > ex:
                used.append(edge.get("edge_id") or f"{edge['old_symbol']}->{current}@{effective}")
                current = edge["old_symbol"]
                continue
            return current, effective, used, None
        return current, None, used, None
    return current, None, used, "cyclic_identity_lineage"


def _api_payload(row: dict) -> dict:
    metadata = {"source_file", "source_row", "source_url", "ex_date", "action_tags",
                "identity_status", "adjustment_status", "source_evidence_problem"}
    return {key: value for key, value in row.items() if key not in metadata}


def _api_citation(row: dict) -> dict:
    return {"url": row.get("source_url"), "source_file": row.get("source_file"),
            "source_row": row.get("source_row"), "snippet": row.get("subject"),
            "source_field": "subject; exDate", "exDate": row.get("exDate"),
            "recDate": row.get("recDate"), "asserted_current_isin": row.get("isin"),
            "inventory_tags": row.get("action_tags") or [],
            "source_symbol": row.get("symbol"), "source_series": row.get("series"),
            "source_payload_sha256": _hash(_api_payload(row)), "source_payload": _api_payload(row)}


def normalize_actions(api_rows: list[dict], circular_rows: list[dict],
                      observations: list[dict], edges: list[dict]) -> list[VerifiedAction]:
    """Normalize documented terms, using exact dated symbol/series observations.

    ``observations`` contains identifiers/dates only, never prices. An ex-date
    observation is mandatory; a first/last range is not evidence of that date.
    """
    output = []
    duplicates = {}
    for row in api_rows:
        subject = row.get("subject") or ""
        components = _api_components(subject)
        if not components:
            continue
        factor_components = [c for c in components if not c.get("non_factor")]
        # Split+bonus compounds stay a single fail-closed record: the
        # adapter does not guess whether they apply sequentially to the
        # same share base.  Every other combination decomposes.
        if len(factor_components) > 1:
            emit = [{"kind": "+".join(c["kind"] for c in components),
                     "terms": {k: v for c in components for k, v in c["terms"].items()},
                     "multiplier": None, "problem": "compound_action_ambiguous",
                     "non_factor": False}]
        elif len(factor_components) == 1:
            emit = components  # factor record + any non-factor components
        else:
            emit = components  # standalone dividend/rights/scheme records
        decomposed = len(emit) > 1
        ex = _date(row.get("exDate"))
        source_symbol, series = row.get("symbol"), row.get("series")
        symbol, floor, used_edges, identity_problem = _historical_symbol(source_symbol, ex, edges)
        fingerprint = _hash(_api_payload(row))
        citation = _api_citation(row)
        duplicate_key = (fingerprint, row.get("source_url"), row.get("ex_date"))
        date_problem = row.get("source_evidence_problem") or (
            "unverified_exchange_source" if not _exchange_source(row.get("source_url")) else
            "missing_exact_exchange_ex_date" if not ex else
            "outside_pre2011_adapter_scope" if not 2000 <= ex.year <= 2010 else
            "conflicting_exchange_ex_date" if row.get("ex_date") and _date(row["ex_date"]) != ex else
            None)
        for component in emit:
            comp_kind = component["kind"]
            comp_terms = component["terms"]
            comp_multiplier = component["multiplier"]
            is_non_factor = bool(component.get("non_factor"))
            if is_non_factor:
                comp_multiplier = None
            action_id = _hash(fingerprint + ":" + comp_kind) if decomposed else fingerprint
            full_citation = dict(citation)
            if decomposed:
                full_citation["component"] = comp_kind
                full_citation["decomposed_from_subject"] = subject
            dup_key = (action_id, row.get("source_url"), row.get("ex_date"))
            if dup_key in duplicates:
                existing = duplicates[dup_key]
                existing.duplicate_source_rows += 1
                existing.citations.append(full_citation)
                continue
            result = VerifiedAction(action_id, "official_nse_api", source_symbol, symbol, series, ex,
                                    _date(row.get("recDate")), subject, comp_kind,
                                    comp_multiplier, bool(comp_terms),
                                    numeric_terms=comp_terms, citations=[full_citation],
                                    ex_date_provenance="NSE API exDate" if ex else None,
                                    identity_edges=used_edges, identity_not_before=floor,
                                    non_factor=is_non_factor, component=comp_kind if decomposed else None)
            exact = [o for o in observations if o.get("symbol") == symbol and o.get("series") == series
                     and _date(o.get("date")) == ex]
            prior = [o for o in observations if o.get("symbol") == symbol and o.get("series") == series
                     and _date(o.get("date")) and ex and _date(o["date"]) < ex
                     and (not floor or _date(o["date"]) >= floor) and not o.get("summary")]
            result.pre_ex_bars = len(prior)
            result.identity_start = min((_date(o["date"]) for o in prior), default=None)
            summaries = [o for o in exact if o.get("summary")]
            if summaries:
                result.pre_ex_bars = int(summaries[0].get("pre_ex_bars", 0))
                result.identity_start = _date(summaries[0].get("identity_start"))
            observed_isins = {o["isin"] for o in exact if o.get("isin")}
            comp_identity_problem = identity_problem
            if len(observed_isins) > 1:
                comp_identity_problem = "conflicting_historical_isin"
            result.historical_isin = next(iter(observed_isins)) if len(observed_isins) == 1 else None
            result.identity_status = comp_identity_problem or (
                "dated_official_rename_exact_observed_series" if exact and used_edges else
                "exact_observed_symbol_series" if exact else "unobserved_historical_identity")
            result.terms_verified = _has_explicit_terms(comp_kind, comp_terms) and _exchange_source(row.get("source_url")) \
                and row.get("source_evidence_problem") != "processed_row_not_verified_against_raw_exchange_source"
            if is_non_factor:
                # Explicit dividend/rights/scheme terms are recorded for
                # audit; they never become a price factor, whatever the
                # identity/ex-date state.  A missing numeric term is still
                # flagged, but never blocks a sibling factor component.
                result.readiness_reason = (
                    "missing_explicit_supported_numeric_terms"
                    if not _has_explicit_terms(comp_kind, comp_terms) else
                    "non_factor_record_no_price_adjustment")
                result.execution_ready = False
            else:
                problem = component.get("problem")
                if comp_multiplier is not None and (
                        not math.isfinite(comp_multiplier) or comp_multiplier <= 1):
                    problem = "invalid_share_multiplier"
                    comp_multiplier = None
                    result.share_multiplier = None
                result.readiness_reason = date_problem or problem or comp_identity_problem or (
                    "unobserved_historical_identity" if not exact else
                    "no_pre_ex_bars" if result.pre_ex_bars <= 0 else "execution_ready")
                result.execution_ready = result.readiness_reason == "execution_ready"
            result.evidence_required = _evidence_for(result.readiness_reason)
            output.append(result)
            duplicates[dup_key] = result
    from nse_pipeline.actions.pre2011_circulars import is_primary_source

    for row in circular_rows:
        if not str(row.get("verification_status", "")).startswith("verified_"):
            continue
        citation = dict(row.get("citation") or {})
        text = row.get("stated_terms_verbatim") or ""
        kind = row.get("verified_event_type") or "other"
        terms, multiplier = _circular_terms(text, kind)
        cited = is_primary_source(citation.get("url")) and text == citation.get("snippet")
        citation["discontinuity_date_not_ex_date"] = row.get("date")
        citation["asserted_ex_date_without_exchange_provenance"] = row.get("ex_date")
        citation["date_warning"] = "stated dates may be record/effective/allotment dates, never NSE ex-dates"
        output.append(VerifiedAction(
            _hash(row), "issuer_document_terms_only", row.get("symbol"), row.get("symbol"),
            row.get("series"), None, _date(row.get("record_date")), text, kind, multiplier,
            _has_explicit_terms(kind, terms) and cited, numeric_terms=terms, citations=[citation],
            readiness_reason="missing_exact_exchange_ex_date",
            evidence_required=_evidence_for("missing_exact_exchange_ex_date")))
    groups = defaultdict(list)
    source_groups = defaultdict(dict)
    for row in api_rows:
        # Only rows carrying a factor (split/bonus) component can compete
        # for the same-day factor. A pure non-factor row (a decomposed
        # dividend is cash only; rights/scheme are likewise recorded as
        # non-factor audit records with no share multiplier) does not
        # alter the split/bonus multiplier, so it must not be counted as
        # a conflicting source alongside a factor row. A mixed row
        # (factor + non-factor) still counts via its factor component,
        # so a second distinct factor row remains a conflict.
        if not any(not c.get("non_factor") for c in _api_components(row.get("subject") or "")):
            continue
        ex = _date(row.get("exDate"))
        historical, _, _, problem = _historical_symbol(row.get("symbol"), ex, edges)
        if ex and historical and row.get("series") and not problem:
            source_groups[(historical, row["series"], ex)][_hash(_api_payload(row))] = row
    for result in output:
        # Only split/bonus factor records can conflict: a dividend, rights
        # or scheme record co-occurring on the same day is a separate
        # non-factor component, not a competing factor.
        if result.source_kind == "official_nse_api" and not result.non_factor \
                and result.ex_date and result.symbol and result.series:
            groups[(result.symbol, result.series, result.ex_date)].append(result)
    for key, group in groups.items():
        sources = source_groups[key]
        if len(group) > 1 or len(sources) > 1:
            for result in group:
                result.execution_ready = False
                result.readiness_reason = "conflicting_same_day_actions"
                result.evidence_required = _evidence_for("conflicting_same_day_actions")
                cited_hashes = {citation.get("source_payload_sha256") for citation in result.citations}
                result.citations.extend(_api_citation(source) for fingerprint, source in sources.items()
                                        if fingerprint not in cited_hashes)
    return output


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_verified_actions(data_root: Path, con=None) -> tuple[list[VerifiedAction], dict]:
    """Load cited sources and identifier-only observations, opening DB read-only.

    Only a small candidate-key aggregation is performed, not an adjustment
    rebuild. Processed API rows must match their exact cached official raw row.
    """
    import duckdb
    import pandas as pd

    root = Path(data_root)
    paths = {
        "official_api": root / "processed/older_actions/nse-api-actions.jsonl",
        "issuer_terms": root / "processed/actions/pre2011_circulars/circulars.jsonl",
        "dated_lineage": root / "processed/identity/identity_edges.jsonl",
    }
    api_rows, circulars, edges = (_read_jsonl(paths[name]) for name in paths)
    inputs = {name: {"path": str(path), "exists": path.exists(),
                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None}
              for name, path in paths.items()}
    raw_sources = {}
    raw_cache = {}
    for row in api_rows:
        filename = row.get("source_file") or ""
        if filename not in raw_cache:
            raw_path = root / "raw/older_actions" / filename
            safe = bool(re.fullmatch(r"nse-api-20(?:0\d|10)\.json", filename))
            content = raw_path.read_bytes() if safe and raw_path.is_file() else None
            raw_cache[filename] = json.loads(content) if content is not None else []
            raw_sources[filename] = {"exists": content is not None,
                                     "sha256": hashlib.sha256(content).hexdigest() if content is not None else None}
        source_row = row.get("source_row")
        raw_rows = raw_cache[filename]
        if not isinstance(source_row, int) or source_row < 0 or source_row >= len(raw_rows) \
                or _api_payload(row) != raw_rows[source_row]:
            row["source_evidence_problem"] = "processed_row_not_verified_against_raw_exchange_source"
        if not paths["dated_lineage"].exists() and not row.get("source_evidence_problem"):
            row["source_evidence_problem"] = "missing_dated_identity_lineage"
    candidates = set()
    for row in api_rows:
        kind, _, _, _ = _api_terms(row.get("subject") or "")
        if not ({"split", "bonus"} & set(kind.split("+"))):
            continue
        ex = _date(row.get("exDate"))
        historical, floor, _, problem = _historical_symbol(row.get("symbol"), ex, edges)
        if ex and historical and row.get("series") and not problem:
            candidates.add((historical, row["series"], ex, floor or date(1900, 1, 1)))
    own_connection = con is None
    observations = []
    if own_connection:
        con = open_catalog(root)
    try:
        if candidates:
            keys = pd.DataFrame(sorted(candidates), columns=["symbol", "series", "ex_date", "identity_floor"])
            con.register("verified_identity_keys", keys)
            try:
                records = con.execute("""
                    SELECT k.symbol, k.series, k.ex_date,
                        count(b.date) FILTER (WHERE b.date = k.ex_date) AS exact_bars,
                        count(b.date) FILTER (WHERE b.date < k.ex_date) AS pre_ex_bars,
                        min(b.date) FILTER (WHERE b.date < k.ex_date) AS identity_start,
                        list(DISTINCT b.isin) FILTER (WHERE b.date = k.ex_date AND b.isin IS NOT NULL) AS historical_isins
                    FROM verified_identity_keys k LEFT JOIN bars b
                      ON b.symbol = k.symbol AND b.series = k.series
                     AND b.date >= k.identity_floor AND b.date <= k.ex_date
                    GROUP BY k.symbol, k.series, k.ex_date
                """).fetchall()
            finally:
                con.unregister("verified_identity_keys")
            for symbol, series, ex, exact, pre, start, isins in records:
                if exact:
                    for isin in (isins or [None]):
                        observations.append({"symbol": symbol, "series": series, "date": ex,
                                             "isin": isin, "summary": True, "pre_ex_bars": pre,
                                             "identity_start": start})
    finally:
        if own_connection:
            con.close()
    normalized = normalize_actions(api_rows, circulars, observations, edges)
    stats = {
        "inputs": inputs, "raw_exchange_sources": raw_sources,
        "official_api_rows_examined": len(api_rows),
        "official_split_bonus_tagged_rows": sum(bool(set(row.get("action_tags") or []) & {"split", "bonus"}) for row in api_rows),
        "recognized_untagged_split_bonus_rows": sum(
            bool({"split", "bonus"} & set(_api_terms(row.get("subject") or "")[0].split("+")))
            and not bool(set(row.get("action_tags") or []) & {"split", "bonus"}) for row in api_rows),
        "issuer_prose_verified_rows": sum(str(row.get("verification_status", "")).startswith("verified_") for row in circulars),
        "official_non_factor_tagged_rows": sum(
            bool(set(row.get("action_tags") or []) & {"dividend", "rights", "scheme"})
            and not bool(set(row.get("action_tags") or []) & {"split", "bonus"})
            for row in api_rows),
        "database_read_only": True,
    }
    return normalized, stats


def write_outputs(data_root: Path, normalized: list[VerifiedAction], stats: dict,
                  applied_ids: set[str] | None = None) -> dict:
    """Report verified terms, executable events, and actual application separately."""
    import pandas as pd

    root = Path(data_root)
    out = root / "processed/actions/normalized_verified"
    reports = root / "reports"
    out.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    for row in normalized:
        row.actually_applied = row.action_id in applied_ids if applied_ids is not None else None
    records = [asdict(row) for row in normalized]
    for name, rows in (("normalized.jsonl", records),
                       ("execution_ready.jsonl", [row for row in records if row["execution_ready"]])):
        (out / name).write_text("".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows), encoding="utf-8")
    if normalized:
        pd.DataFrame([{**row.adjustment_row(), "terms_verified": row.terms_verified,
                       "execution_ready": row.execution_ready, "actually_applied": row.actually_applied,
                       "numeric_terms_json": json.dumps(row.numeric_terms),
                       "identity_status": row.identity_status, "historical_isin": row.historical_isin,
                       "identity_not_before": row.identity_not_before,
                       "observed_pre_ex_bars": row.pre_ex_bars,
                       "identity_edges_json": json.dumps(row.identity_edges),
                       "duplicate_source_rows": row.duplicate_source_rows}
                      for row in normalized]).to_parquet(out / "normalized.parquet", index=False, compression="zstd")
    ready = [row for row in normalized if row.execution_ready]
    coverage = {
        "generated_at": datetime.now(timezone.utc).isoformat(), **stats,
        "normalized_action_rows": len(normalized),
        "terms_verified": sum(row.terms_verified for row in normalized),
        "ready": len(ready),
        "verified_terms_rows": sum(row.terms_verified for row in normalized),
        "verified_terms_by_source": dict(Counter(row.source_kind for row in normalized if row.terms_verified)),
        "execution_ready_actions": len(ready),
        "execution_ready_by_year": dict(sorted(Counter(str(row.ex_date.year) for row in ready).items())),
        "execution_ready_by_type": dict(Counter(row.action_type for row in ready)),
        "execution_ready_by_identity": dict(Counter(row.identity_status for row in ready)),
        "execution_ready_originally_tagged": sum(
            bool(set(row.citations[0].get("inventory_tags") or []) & {"split", "bonus"}) for row in ready),
        "execution_ready_untagged_abbreviations": sum(
            not bool(set(row.citations[0].get("inventory_tags") or []) & {"split", "bonus"}) for row in ready),
        "exact_duplicate_source_rows_collapsed": sum(row.duplicate_source_rows - 1 for row in normalized),
        "non_factor_records": sum(row.non_factor for row in normalized),
        "non_factor_by_type": dict(Counter(row.action_type for row in normalized if row.non_factor)),
        "non_factor_terms_verified": sum(row.non_factor and row.terms_verified for row in normalized),
        # Pre-2011 execution gates (direct API parsing only; no price-derived terms).
        "split_bonus_terms_verified": sum(
            row.action_type in ("split", "bonus") and row.terms_verified for row in normalized),
        "split_bonus_execution_ready": len(ready),
        "split_bonus_blocked_by_identity": sum(
            row.action_type in ("split", "bonus")
            and row.readiness_reason == "unobserved_historical_identity" for row in normalized),
        "split_bonus_blocked_by_compound": sum(
            row.action_type in ("split", "bonus", "split+bonus")
            and row.readiness_reason == "compound_action_ambiguous" for row in normalized),
        "rights_structured_ratio_rows": sum(
            row.action_type == "rights" and _has_explicit_terms("rights", row.numeric_terms)
            for row in normalized),
        "rights_unstructured_rows": sum(
            row.action_type == "rights" and not _has_explicit_terms("rights", row.numeric_terms)
            for row in normalized),
        "dividend_terms_verified_rows": sum(
            row.action_type == "dividend" and row.terms_verified for row in normalized),
        "scheme_terms_verified_rows": sum(
            row.action_type == "scheme" and row.terms_verified for row in normalized),
        "decomposed_compound_records": sum(row.component is not None for row in normalized),
        "rows_with_evidence_required": sum(bool(row.evidence_required) for row in normalized),
        "evidence_required_by_reason": dict(Counter(
            row.readiness_reason for row in normalized if row.evidence_required)),
        "readiness_reason_counts": dict(Counter(row.readiness_reason for row in normalized)),
        "issuer_terms_execution_ready": sum(row.execution_ready and row.source_kind == "issuer_document_terms_only" for row in normalized),
        "actually_applied_actions": sum(row.actually_applied is True for row in normalized) if applied_ids is not None else None,
        "application_status": "adjustment_export_completed" if applied_ids is not None else "not_run; readiness is not application",
        "no_price_derived_terms_or_factors": True,
        "identity_rule": "official unbranched dated rename edges plus exact ex-date historical symbol/series observation; no present-day ISIN backfill",
        "date_rule": "only NSE API exDate; no queue discontinuity, record/effective/allotment date substitution",
        "limitations": [
            "The sparse public API is not proof of full historical corporate-action coverage, particularly 2000-2005.",
            "Issuer prose proves terms only; no promotion without an independently documented NSE exchange ex-date.",
            "Split+bonus compounds, revised/nonidentical same-day factor rows and identity branching fail closed, not multiplied or arbitrarily selected; factor+dividend/rights compounds decompose into one factor record plus non-factor records.",
            "Dividend/rights/scheme records are non-factor audit records: stated amounts, ratios and issue prices are never converted into share multipliers or price adjustments.",
            "Applied factors stop at the observed historical identity epoch; no cross-symbol stitching or later-ISIN backfill.",
        ],
    }
    encoded = json.dumps(coverage, indent=2, ensure_ascii=False, default=str) + "\n"
    (out / "coverage.json").write_text(encoded, encoding="utf-8")
    (reports / "verified-actions.json").write_text(encoded, encoding="utf-8")
    markdown = f"""# Normalized verified actions

- Official API rows examined: {stats['official_api_rows_examined']:,}; split/bonus-tagged: {stats['official_split_bonus_tagged_rows']:,}.
- Additional abbreviated split/bonus subjects explicitly recognized: {stats.get('recognized_untagged_split_bonus_rows', 0)} (including retained, non-executable compounds).
- Verified explicit numeric terms: {coverage['verified_terms_rows']:,} normalized rows.
- Exact execution readiness: {coverage['execution_ready_actions']:,} events.
- Actually applied by this run: {coverage['actually_applied_actions'] if applied_ids is not None else 'not run (unknown; not the ready count)'}.
- Issuer prose: {stats['issuer_prose_verified_rows']} verified rows, {coverage['issuer_terms_execution_ready']} execution-ready.
- Non-factor audit records (dividend/rights/scheme, never price-adjusted): {coverage['non_factor_records']:,} ({coverage['non_factor_terms_verified']:,} with explicit stated terms).
- Rights structured ratio rows (explicit rights:existing ratio in the API subject): {coverage['rights_structured_ratio_rows']:,} structured, {coverage['rights_unstructured_rows']:,} unstructured (price-only / no ratio retained for audit, never terms-verified).
- Split/bonus gates: {coverage['split_bonus_terms_verified']:,} terms-verified, {coverage['split_bonus_execution_ready']:,} execution-ready, {coverage['split_bonus_blocked_by_identity']:,} blocked by unobserved historical identity, {coverage['split_bonus_blocked_by_compound']:,} blocked as split+bonus compound-ambiguous.
- Compound components decomposed into separate records: {coverage['decomposed_compound_records']:,} (split+bonus compounds remain one fail-closed record).
- Unresolved rows carrying machine-readable evidence requirements: {coverage['rows_with_evidence_required']:,}.
- Exact duplicate source rows collapsed: {coverage['exact_duplicate_source_rows_collapsed']} (all citations retained).

Only cited explicit terms produce factors. NSE `exDate`, not record/effective dates or price discontinuities, controls execution. Dated official lineage must resolve to an exactly observed historical symbol and series. Pre-2011 historical ISIN stays null when absent. The database is opened read-only; no raw sources are overwritten.

## Readiness audit

| Reason | Rows |
|---|---:|
"""
    markdown += "".join(f"| {reason} | {count} |\n" for reason, count in sorted(coverage["readiness_reason_counts"].items()))
    markdown += "\n## Ready by year\n\n" + json.dumps(coverage["execution_ready_by_year"], indent=2) + "\n\n## Limits\n\n"
    markdown += "\n".join("- " + item for item in coverage["limitations"]) + "\n"
    (reports / "verified-actions.md").write_text(markdown, encoding="utf-8")
    return coverage


def build(data_root: Path) -> dict:
    """Build only normalized outputs/reports; never rebuild adjusted bars.

    The returned coverage includes simple ``terms_verified`` and ``ready``
    counts. ``actually_applied_actions`` stays null until the host performs
    and successfully completes ``run_adjustment``.
    """
    normalized, stats = load_verified_actions(data_root)
    return write_outputs(data_root, normalized, stats)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=data_root())
    args = parser.parse_args()
    print(json.dumps(build(args.data), indent=2, default=str))


if __name__ == "__main__":
    main()
