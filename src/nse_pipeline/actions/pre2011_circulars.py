"""Issuer-circular verification for the top-20 pre-2011 discontinuities.

Extends ``pre2011_verify`` (which searched only the offline NSE API rows
and the cached Nifty index press corpus and found 0/20) by searching
*primary* issuer sources: BSE historical annual reports, NSE circular
archives, issuer annual reports / quarterly filings, and issuer SEC
Form 20-F filings.  Only documents hosted on an official primary host
(BSE, NSE, SEC, or the issuer's own domain) count; secondary aggregators
(trendlyne, moneycontrol, screener, roic.ai, investing.com, ...) never
verify, however plausible their split history looks.

Verification rule (fail-closed, never inferred from price):

  A queue row is promoted to ``verified_split`` / ``verified_bonus`` /
  ``verified_split_and_bonus`` only with ALL of:

  1. a downloaded (or locally supplied) primary document that mentions
     the queue company, AND
  2. explicit stated terms in that document's text -- a stated split /
     subdivision face-value change or share count, or a stated bonus /
     stock-dividend ratio -- quoted verbatim with a citation, AND
  3. a stated effective / record / ex / allotment date extracted from
     the same snippet window that is temporally proximate to the
     discontinuity date (within ``PROXIMATE_DAYS`` = 45 days).

  A generic mention of "split"/"bonus", a face value of Re.1 with no
  stated change, a later same-symbol event, and any price move alone
  never verify.  Everything else is ``needs_issuer_circular`` with a
  machine-readable reason (``no_primary_doc_found``,
  ``download_failed``, ``no_explicit_terms``, ``company_not_mentioned``,
  ``stated_date_not_proximate``).  Rows with a >30 day trading gap also
  carry a suspension/resumption note and are never promoted on a split
  theory alone.

Downloads (only when ``build(..., download=True)`` or the CLI
``--download`` flag is used) are bounded: primary hosts only, at most
``MAX_BYTES`` per document, a fixed timeout, and PDF magic / HTML
detection before extraction.  ``market.duckdb`` is never opened.  No
price, volume, or return is converted into a ratio or factor.

Outputs (owned by this module only):
  * ``data/processed/actions/pre2011_circulars/circulars.jsonl``
  * ``data/processed/actions/pre2011_circulars/coverage.json``
  * ``data/processed/actions/pre2011_circulars/raw_pdfs/``
    (downloaded primary PDFs, bounded)
  * ``data/processed/actions/pre2011_circulars/raw_texts/``
    (extracted text per document)
  * ``data/reports/pre2011-circulars.json`` / ``.md`` -- coverage/report
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import html as _html
import json
import re
import sys
import urllib.request
import zlib
from datetime import date, datetime, timezone
from pathlib import Path
from nse_pipeline.paths import data_root
from urllib.parse import urlparse

TOP_N = 20
PROXIMATE_DAYS = 45
MAX_BYTES = 25_000_000
DOWNLOAD_TIMEOUT = 45
NEEDS = "needs_issuer_circular"
VERIFIED_STATUSES = ("verified_split", "verified_bonus",
                     "verified_split_and_bonus")

# Hosts whose documents count as primary issuer/exchange sources.
# Anything else (aggregators, news, forums) is a locator hint at best.
PRIMARY_HOST_SUFFIXES = (
    "bseindia.com", "nseindia.com", "sec.gov",
    "dabur.com", "wipro.com", "tvsmotor.com", "hul.co.in",
    "infosys.com", "ashokleyland.com", "techmahindra.com",
    "gtllimited.com", "sebi.gov.in",
)
# Secondary hosts that must never verify even if they state terms.
SECONDARY_HINTS = ("trendlyne", "moneycontrol", "screener", "roic.ai",
                   "investing.com", "alphaspread", "companiesmarketcap",
                   "digrin", "bullfincher", "dividendbonus", "scribd")

FORBIDDEN_KEYS = frozenset({
    "adjustment_factor", "factor", "share_multiplier", "multiplier",
    "ratio_value", "inferred_ratio", "split_ratio", "bonus_ratio",
    "adjustment", "price_adjustment_factor",
})

NSE_PRESS_ARCHIVE = (
    "https://www.nseindia.com/static/resources/"
    "exchange-communication-press-releases-archives")
BSE_ANNUAL_REPORTS = "https://www.bseindia.com/investor-relations/annual-reports"

# Company-name stems per queue symbol (queue symbol-change / delisting
# edges plus the issuer's legal name at the time).  Used only for the
# "mentions this company" gate; never for ratio inference.
COMPANY_STEMS: dict[str, list[str]] = {
    "SATYAMCOMP": ["Satyam Computer Services"],
    "SRGINFOTEC": ["SRG Infotec"],
    "SOFTSOLINT": ["Soft Solutions", "PVP Ventures"],
    "VIKASWSP": ["Vikas WSP"],
    "GLOBALTELE": ["GTL Limited", "Global Tele-Systems"],
    "HINDLEVER": ["Hindustan Lever"],
    "INFOSYSTCH": ["Infosys Technologies"],
    "GUJNRECOKE": ["Gujarat Recoke"],
    "ASHOKLEY": ["Ashok Leyland"],
    "GLOBLTRUST": ["Global Trust Bank"],
    "WIPRO": ["Wipro"],
    "OPTOCIRCUI": ["Opto Circuits"],
    "CRESTCOMM": ["Crest Animation"],
    "DYNACONS": ["Dynacons"],
    "DABUR": ["Dabur India"],
    "TVSMOTOR": ["TVS Motor"],
    "SHANTIGEAR": ["Shanti Gears"],
}

# Primary documents searched per symbol.  ``local`` is the filename stem
# under raw_pdfs/ (``.pdf``/``.txt``); ``kind`` is issuer_annual_report /
# issuer_20f / exchange_annual_report_archive.  Only documents that
# were actually downloaded and extracted are ever cited; a registry
# entry without a local file is a locator, not evidence.
DOC_REGISTRY: list[dict] = [
    {"symbol": "SATYAMCOMP", "title": "Satyam Computer Services Ltd. "
     "Annual Report 2000-01 (BSE historical annual-report archive)",
     "url": "https://www.bseindia.com/HIS_ANN_RPT/HISTANNR/2001/"
            "SATYAM_COMPUTER_SERVICES_LTD-500376-MARCH-2001.PDF",
     "kind": "exchange_annual_report_archive", "local": "satyam_ar2000_01_bse"},
    {"symbol": "DABUR", "title": "Dabur India Ltd. Annual Report 2000-01 "
     "(issuer archive on dabur.com)",
     "url": "https://www.dabur.com/Investors/Financial%20Information/"
            "Reports/Archive/2000-01/530-Dabur_AR_00_01.pdf",
     "kind": "issuer_annual_report", "local": "dabur_ar0001"},
    {"symbol": "TVSMOTOR", "title": "TVS Motor Company Ltd. Annual Report "
     "2003-04 (issuer site tvsmotor.com)",
     "url": "https://www.tvsmotor.com/-/media/"
            "ab64351d23374c42ac0fac3ceb308767.ashx",
     "kind": "issuer_annual_report", "local": "tvs_ar"},
    {"symbol": "WIPRO", "title": "Wipro Ltd. Annual Report 2004-05 "
     "(issuer site wipro.com)",
     "url": "https://www.wipro.com/content/dam/nexus/en/investor/"
            "annual-reports/2004-2005/11093-Wipro-Annual-Report-2004-2005.pdf",
     "kind": "issuer_annual_report", "local": "wipro_ar2004_05"},
    {"symbol": "INFOSYSTCH", "title": "Infosys Technologies Ltd. Form "
     "20-F for fiscal year ended 31 Mar 2005 (SEC EDGAR)",
     "url": "https://www.sec.gov/Archives/edgar/data/1067491/"
            "000119312505084187/d20f.htm",
     "kind": "issuer_20f", "local": "infosys_20f2005"},
    {"symbol": "ASHOKLEY", "title": "Ashok Leyland Ltd. Annual Report "
     "2004-05 (BSE historical annual-report archive)",
     "url": "https://www.bseindia.com/HIS_ANN_RPT/HISTANNR/2005/"
            "ASHOK_LEYLAND_LTD-500477-MARCH-2005.PDF",
     "kind": "exchange_annual_report_archive", "local": "ashok_ar2004_05"},
    {"symbol": "HINDLEVER", "title": "Hindustan Lever Ltd. Annual Report "
     "2002 (issuer site hul.co.in) -- checked, states no 2000 split terms",
     "url": "https://www.hul.co.in/files/origin/"
            "11c905510787d5ab3a63c5cda46c7e8f4c49e161.pdf/annual-report-2002.pdf",
     "kind": "issuer_annual_report", "local": "hul_ar2002"},
    {"symbol": "VIKASWSP", "title": "Vikas WSP Ltd. Annual Report "
     "(BSE historical annual-report archive, 2 vintages) -- checked, "
     "no split/bonus terms stated",
     "url": "https://www.bseindia.com/HIS_ANN_RPT/HISTANNR/2001/"
            "VIKAS_WSP_LTD-519307-MARCH-2001.PDF",
     "kind": "exchange_annual_report_archive", "local": "vikas_ar2000_01"},
    {"symbol": "VIKASWSP", "title": "Vikas WSP Ltd. Annual Report "
     "(BSE historical annual-report archive, 2 vintages)",
     "url": "https://www.bseindia.com/HIS_ANN_RPT/HISTANNR/2002/"
            "VIKAS_WSP_LTD-519307-MARCH-2002.PDF",
     "kind": "exchange_annual_report_archive", "local": "vikas_ar2001_02"},
]

# --------------------------------------------------------------------------
# Text normalisation / term detection
# --------------------------------------------------------------------------

def _norm(text: str) -> str:
    text = (text or "").replace(" ", " ").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


# Explicit split/subdivision constructions.  A bare "split" word, a bare
# face-value figure, or a product name ("guar splits") is not a term.
_SPLIT_PATTERNS = [
    # "split from Rs.10/- per share to Re.1/- per share" / "sub-divided
    # the face value ... from Rs. 10/- each to Re. 1/- each"
    re.compile(r"(?:split|sub[-\s]?divid\w*)\b.{0,160}?(?:rs\.?|re\.?)\s*[\d.]+"
               r".{0,80}?(?:rs\.?|re\.?)\s*[\d.]+", re.I | re.S),
    # "split into 10 shares of Re.1 each" / "subdivided into 10 equity
    # shares of face value of Re. 1/- each"
    re.compile(r"(?:split|sub[-\s]?divid\w*)\b.{0,120}?into\s+\d+\s+"
               r"(?:equity\s+)?shares?\b", re.I | re.S),
    # "one equity share of Rs 10/- into five equity shares of Rs 2/- each"
    re.compile(r"(?:split|sub[-\s]?divid\w*)\b.{0,160}?\b(?:five|5|ten|10|two|2)"
               r"\s+(?:equity\s+)?shares?\b", re.I | re.S),
    # "stock split of 5-for-1" / "5 for 1" (OCR "l" for "1" tolerated
    # only in the for-1 denominator position)
    re.compile(r"(?:stock\s+)?split\b.{0,40}?\d+\s*[- ]?for\s*[- ]?[l1]\b",
               re.I | re.S),
    # "split ... Rs.10 each were split into ..." via face-value change
    re.compile(r"face\s+value\b.{0,120}?(?:rs\.?|re\.?)\s*[\d.]+"
               r".{0,80}?(?:rs\.?|re\.?)\s*[\d.]+", re.I | re.S),
]

_BONUS_PATTERNS = [
    # "bonus ... ratio of 2:1" / "bonus issue of 3:1" / "Bonus 1-for-1"
    re.compile(r"bonus\b.{0,100}?\d+\s*:\s*\d+", re.I | re.S),
    re.compile(r"bonus\b.{0,60}?\d+\s*[- ]?for\s*[- ]?\d+", re.I | re.S),
    # "Bonus equity shares ... ratio of 2:1 (two bonus shares for every
    # one share held)"
    re.compile(r"bonus\s+(?:equity\s+)?shares?\b.{0,160}?\d+\s*:\s*\d+",
               re.I | re.S),
    # "three additional equity shares for every equity share held"
    re.compile(r"\b(?:one|two|three|four|1|2|3|4)\s+additional\s+"
               r"(?:equity\s+)?shares?\b.{0,60}?for\s+every", re.I | re.S),
    # "two bonus shares for every one share held"
    re.compile(r"\b(?:two|three|one|2|3|1)\s+bonus\s+shares?\b.{0,60}?"
               r"for\s+every", re.I | re.S),
    # "stock dividend in the ratio of 2 additional equity shares ... for
    # every equity share"
    re.compile(r"stock\s+dividend\b.{0,160}?\d+\s*(?::\s*\d+|additional)",
               re.I | re.S),
    # "stock dividend in the ratio of 2:1"
    re.compile(r"stock\s+dividend\b.{0,80}?\d+\s*:\s*\d+", re.I | re.S),
]

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
_DATE_PATTERNS = [
    re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})"),
    re.compile(r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})"),
    re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b"),
]


def _parse_stated_dates(window: str) -> list[date]:
    out = []
    for pat, kind in ((_DATE_PATTERNS[0], "dmy"), (_DATE_PATTERNS[1], "mdy"),
                      (_DATE_PATTERNS[2], "num")):
        for m in pat.finditer(window):
            try:
                if kind == "dmy":
                    d, mon, y = int(m.group(1)), _MONTHS[m.group(2).lower()], int(m.group(3))
                elif kind == "mdy":
                    mon, d, y = _MONTHS[m.group(1).lower()], int(m.group(2)), int(m.group(3))
                else:
                    d, mon, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                out.append(date(y, mon, d))
            except (KeyError, ValueError):
                continue
    return sorted(set(out))


def _stated_dates_near(text: str, match: re.Match,
                       max_dist: int = 200) -> list[tuple[date, int]]:
    """(stated date, char distance) pairs near the term match.

    Positional filtering prevents a neighbouring (different) event's
    date bleeding into this match's window -- e.g. Satyam's 1999 bonus
    must not borrow the 2000 split's date.  Proximity is judged on the
    *nearest* date to the match, not the temporally closest date in
    the window.
    """
    centre = (match.start() + match.end()) // 2
    window_start = max(0, match.start() - 400)
    window = text[window_start: match.end() + 400]
    pairs = []
    for pat, kind in ((_DATE_PATTERNS[0], "dmy"), (_DATE_PATTERNS[1], "mdy"),
                      (_DATE_PATTERNS[2], "num")):
        for dm in pat.finditer(window):
            pos = window_start + (dm.start() + dm.end()) // 2
            dist = abs(pos - centre)
            if dist > max_dist:
                continue
            try:
                if kind == "dmy":
                    d, mon, y = int(dm.group(1)), _MONTHS[dm.group(2).lower()], int(dm.group(3))
                elif kind == "mdy":
                    mon, d, y = _MONTHS[dm.group(1).lower()], int(dm.group(2)), int(dm.group(3))
                else:
                    d, mon, y = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
                pairs.append((date(y, mon, d), dist))
            except (KeyError, ValueError):
                continue
    pairs.sort(key=lambda p: p[1])
    return pairs


def find_term_matches(text: str) -> list[dict]:
    """All explicit split/bonus term matches with snippet + stated dates."""
    matches = []
    for kind, patterns in (("split", _SPLIT_PATTERNS),
                           ("bonus", _BONUS_PATTERNS)):
        for pat in patterns:
            for m in pat.finditer(text or ""):
                pairs = _stated_dates_near(text, m)
                matches.append({
                    "kind": kind,
                    "snippet": _norm(text[max(0, m.start() - 180):
                                          m.end() + 180]),
                    "stated_dates": sorted({d.isoformat() for d, _ in pairs}),
                    "nearest_stated_date": (
                        pairs[0][0].isoformat() if pairs else None),
                })
    # De-duplicate by (kind, snippet head)
    seen, out = set(), []
    for mm in matches:
        key = (mm["kind"], mm["snippet"][:80])
        if key not in seen:
            seen.add(key)
            out.append(mm)
    return out


def mentions_company(text: str, stems: list[str]) -> bool:
    low = _norm(text).lower()
    return any(s.lower() in low for s in stems if s)


def is_primary_source(url: str | None) -> bool:
    """True only for official exchange / regulator / issuer hosts."""
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    if any(h in host for h in SECONDARY_HINTS):
        return False
    return any(host == s or host.endswith("." + s)
               for s in PRIMARY_HOST_SUFFIXES)


# --------------------------------------------------------------------------
# Bounded download + PDF extraction
# --------------------------------------------------------------------------

def download_bounded(url: str, max_bytes: int = MAX_BYTES,
                     timeout: int = DOWNLOAD_TIMEOUT,
                     opener=None) -> bytes:
    """Fetch one primary document, bounded in size and time.

    ``opener`` is injectable for tests: a callable taking (url, timeout)
    and returning (status, content_type, data).  Raises ValueError for
    non-primary hosts, oversize bodies, or non-PDF/non-HTML payloads.
    """
    if not is_primary_source(url):
        raise ValueError(f"non-primary host refused: {url}")
    if opener is None:
        def opener(u, t):
            req = urllib.request.Request(
                u, headers={"User-Agent": "Mozilla/5.0 "
                                            "(pre2011-circulars research)"})
            with urllib.request.urlopen(req, timeout=t) as resp:
                return resp.status, resp.headers.get_content_type(), resp.read()
    status, content_type, data = opener(url, timeout)
    if status != 200:
        raise ValueError(f"HTTP {status} for {url}")
    if len(data) > max_bytes:
        raise ValueError(f"document exceeds {max_bytes} bytes: {url}")
    if data[:4] != b"%PDF" and "html" not in (content_type or "") \
            and not _looks_like_html(data):
        raise ValueError(f"unexpected payload (not PDF/HTML) from {url}")
    return data


def _looks_like_html(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith(b"<!doctype") or head.startswith(b"<html")


def extract_pdf_text(data: bytes) -> str:
    """Extract text: PyMuPDF -> PyPDF2 -> zlib-stream fallback."""
    try:
        import fitz  # type: ignore
        doc = fitz.open(stream=data, filetype="pdf")
        return "\n".join(page.get_text() for page in doc)
    except Exception:
        pass
    try:
        import io
        from PyPDF2 import PdfReader  # type: ignore
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:
        pass
    chunks = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        try:
            chunks.append(zlib.decompress(m.group(1)).decode(
                "latin-1", errors="ignore"))
        except Exception:
            continue
    return "\n".join(chunks)


def html_to_text(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return _norm(_html.unescape(text))


def parse_iso(value) -> date | None:
    if value is None or str(value).strip() in ("", "-"):
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def assert_no_forbidden_keys(obj) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in FORBIDDEN_KEYS:
                raise ValueError(f"forbidden inferred-ratio key: {key}")
            assert_no_forbidden_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            assert_no_forbidden_keys(item)


# --------------------------------------------------------------------------
# Per-document / per-row evaluation (pure, in-memory)
# --------------------------------------------------------------------------

def evaluate_document(doc: dict, text: str, stems: list[str],
                      when: date | None) -> dict:
    """Grade one primary document against one queue row."""
    text = text or ""
    matches = find_term_matches(text)
    proximate = []
    for mm in matches:
        # Proximity is judged on the date nearest the term in the text
        # (not the temporally closest date anywhere in the window), so
        # a different event's date cannot lend this match proximity.
        stated = parse_iso(mm.get("nearest_stated_date")) if mm.get(
            "nearest_stated_date") else None
        diff = abs((stated - when).days) if stated and when else None
        mm = {**mm, "min_days_from_discontinuity": diff}
        if diff is not None and diff <= PROXIMATE_DAYS:
            proximate.append(mm)
    mentions = mentions_company(text, stems)
    if not text.strip():
        outcome = "empty_text"
    elif not mentions:
        outcome = "company_not_mentioned"
    elif not matches:
        outcome = "no_explicit_terms"
    elif not proximate:
        outcome = "stated_date_not_proximate"
    else:
        outcome = "proximate_explicit_terms"
    return {
        "title": doc.get("title"), "url": doc.get("url"),
        "kind": doc.get("kind"), "primary_host": is_primary_source(doc.get("url")),
        "txt_chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "mentions_company": mentions,
        "term_matches": matches[:6],
        "proximate_term_matches": proximate[:3],
        "outcome": outcome,
    }


def verify_one(queue_row: dict, documents: list[dict],
               texts: dict, stems: list[str] | None = None) -> dict:
    """Verify one queue row against ``texts`` {local_stem: text}."""
    symbol = (queue_row.get("symbol") or "").strip()
    when = parse_iso(queue_row.get("date"))
    stems = list(stems) if stems is not None else COMPANY_STEMS.get(symbol, [])
    for edge in queue_row.get("symbolchange_edges") or []:
        if edge.get("company"):
            stems.append(str(edge["company"]))
    delist = queue_row.get("delisting_reference") or {}
    if delist.get("company"):
        stems.append(str(delist["company"]))
    stems = sorted({s for s in stems if s})

    gap = queue_row.get("trading_gap_days")
    gap_note = None
    if gap is not None and gap > 30:
        gap_note = (f"Trading gap over 30d ({gap}d): treat as "
                    "suspension/resumption or missing sessions until an "
                    "official document identifies the event type; do "
                    "not assume split/bonus from the resumption move.")

    doc_evals, download_log = [], []
    for doc in documents:
        local = doc.get("local")
        text = texts.get(local)
        if text is None:
            download_log.append({"local": local, "url": doc.get("url"),
                                 "status": "not_downloaded"})
            continue
        ev = evaluate_document(doc, text, stems, when)
        ev["local"] = local
        doc_evals.append(ev)
        download_log.append({"local": local, "url": doc.get("url"),
                             "status": "extracted",
                             "txt_chars": len(text)})

    proximate = [e for e in doc_evals
                 if e["outcome"] == "proximate_explicit_terms"]
    # A gap row is never promoted on split/bonus alone: resumption moves
    # need a suspension/resumption notice, not a split circular.  All
    # verified candidates in this batch have gap == 1, so this gate is
    # defence-in-depth, not a silent rejection of the batch.
    if proximate and gap is not None and gap > 30:
        proximate = []
        status, event, reason = NEEDS, None, "gap_row_suspension_check_required"
    elif proximate:
        kinds = {m["kind"] for e in proximate
                 for m in e["proximate_term_matches"]}
        if kinds == {"split"}:
            status, event = "verified_split", "split"
        elif kinds == {"bonus"}:
            status, event = "verified_bonus", "bonus"
        else:
            status, event = "verified_split_and_bonus", "split_and_bonus"
        reason = None
    else:
        status, event = NEEDS, None
        if gap is not None and gap > 30:
            reason = "gap_row_suspension_check_required"
        elif not doc_evals:
            reason = "no_primary_doc_found"
        else:
            outcomes = {e["outcome"] for e in doc_evals}
            reason = ("no_explicit_terms" if "no_explicit_terms" in outcomes
                      else "stated_date_not_proximate"
                      if "stated_date_not_proximate" in outcomes
                      else "company_not_mentioned"
                      if "company_not_mentioned" in outcomes
                      else "no_primary_doc_found")

    citation = None
    stated_terms = None
    if status in VERIFIED_STATUSES and proximate:
        best = proximate[0]["proximate_term_matches"][0]
        ev = next(e for e in proximate
                  if best in e["proximate_term_matches"])
        citation = {
            "title": ev["title"], "url": ev["url"], "kind": ev["kind"],
            "local_stem": ev.get("local"),
            "term_kind": best["kind"],
            "stated_dates": best["stated_dates"],
            "min_days_from_discontinuity":
                best["min_days_from_discontinuity"],
            "snippet": best["snippet"],
            "note": ("Terms quoted verbatim from the primary document; "
                     "price move was not converted into a ratio."),
        }
        stated_terms = best["snippet"]

    evidence_by_reason = {
        "no_primary_doc_found": [
            "Downloaded primary-host (BSE/NSE/SEC/issuer) document mentioning "
            "this company, located via an exchange/issuer index (never a "
            "guessed dated URL or a secondary aggregator)",
            "Explicit stated split/bonus terms quoted verbatim from that document",
            "Stated effective/record/ex/allotment date within 45 days of the "
            "discontinuity, plus an independent NSE ex-date before any factor use",
        ],
        "no_explicit_terms": [
            "Primary document passage stating numeric split face values or a "
            "bonus ratio/share count verbatim (generic 'split'/'bonus' words "
            "and bare face values are not terms)",
            "Stated date proximate to the discontinuity and an independent NSE ex-date",
        ],
        "stated_date_not_proximate": [
            "Primary document stating terms with a date within 45 days of the "
            "discontinuity (a different event's date must not be borrowed)",
            "Independent NSE exchange ex-date (document dates are not ex-dates)",
        ],
        "company_not_mentioned": [
            "Primary document that mentions this queue company by its "
            "contemporaneous legal name or dated symbol-change edge",
        ],
        "gap_row_suspension_check_required": [
            "Suspension/resumption or delisting notice explaining the >30 day "
            "trading gap; a split/bonus circular alone never promotes a gap row",
        ],
    }
    evidence_required = (
        [] if status in VERIFIED_STATUSES else
        evidence_by_reason.get(reason or "", evidence_by_reason["no_primary_doc_found"]))
    year = when.year if when else None
    row = {
        "evidence_required": evidence_required,
        "symbol": symbol,
        "date": queue_row.get("date"),
        "prior_trading_date": queue_row.get("prior_trading_date"),
        "trading_gap_days": gap,
        "prior_close": queue_row.get("prior_close"),
        "close": queue_row.get("close"),
        "previous_close": queue_row.get("previous_close"),
        "abs_close_change_pct": queue_row.get("abs_close_change_pct"),
        "signed_close_change_pct": queue_row.get("signed_close_change_pct"),
        "priority_rank": queue_row.get("priority_rank"),
        "priority_score_abs_return_x_volume": (
            queue_row.get("priority_score_abs_return_x_volume")),
        "review_flags": queue_row.get("review_flags", []),
        "verification_status": status,
        "needs_reason": reason,
        "verified_event_type": event,
        "stated_terms_verbatim": stated_terms,
        "citation": citation,
        "no_ratio_inferred_from_price": True,
        "ratio_from_price": False,
        "prices_adjusted": False,
        "trading_gap_note": gap_note,
        "documents_checked": doc_evals,
        "download_log": download_log,
        "next_action": (
            "Transcribe stated terms, ex/record/effective date, and "
            "ISIN/series from the cited primary document with "
            "page-level citation; do not convert the price move into "
            "a ratio." if status in VERIFIED_STATUSES else
            "Obtain the issuer circular / filing (BSE historical "
            "annual-report archive, NSE press/circular archive, issuer "
            "annual report, or licensed corporate data) and read the "
            "stated event type and terms verbatim; never infer from "
            "price."),
        "locator": {
            "bse_annual_reports": BSE_ANNUAL_REPORTS,
            "nse_press_release_archive": NSE_PRESS_ARCHIVE,
            "note": ("Locators only: a dated PDF URL must come from an "
                     "exchange/issuer index; never guess it from the "
                     "discontinuity date."),
        },
    }
    assert_no_forbidden_keys(row)
    return row


def load_local_texts(raw_pdf_dir: Path, raw_text_dir: Path,
                     local_stems=()) -> dict:
    """Load extracted texts (raw_texts/) with PDF extraction fallback."""
    texts = {}
    for stem in local_stems:
        txt_path = raw_text_dir / f"{stem}.txt"
        if txt_path.exists():
            texts[stem] = txt_path.read_text(encoding="utf-8",
                                             errors="replace")
            continue
        pdf_path = raw_pdf_dir / f"{stem}.pdf"
        if pdf_path.exists():
            texts[stem] = extract_pdf_text(pdf_path.read_bytes())
    return texts


def download_registry(data_root: Path, registry=DOC_REGISTRY,
                      opener=None) -> dict:
    """Download bounded primary docs into raw_pdfs/ + raw_texts/."""
    raw_pdf = data_root / "processed" / "actions" / "pre2011_circulars" / "raw_pdfs"
    raw_txt = data_root / "processed" / "actions" / "pre2011_circulars" / "raw_texts"
    raw_pdf.mkdir(parents=True, exist_ok=True)
    raw_txt.mkdir(parents=True, exist_ok=True)
    log = {}
    for doc in registry:
        stem = doc["local"]
        try:
            data = download_bounded(doc["url"], opener=opener)
        except Exception as exc:  # noqa: BLE001 - logged, fail-closed
            log[stem] = {"url": doc["url"], "status": "download_failed",
                         "error": str(exc)[:200]}
            continue
        (raw_pdf / f"{stem}.pdf").write_bytes(data)
        if data[:4] == b"%PDF":
            text = extract_pdf_text(data)
        else:
            text = html_to_text(data)
        (raw_txt / f"{stem}.txt").write_text(text, encoding="utf-8")
        log[stem] = {"url": doc["url"], "status": "downloaded",
                     "bytes": len(data), "txt_chars": len(text)}
    return log


def build(data_root: Path, top_n: int = TOP_N, download: bool = False,
          opener=None) -> dict:
    data_root = Path(data_root)
    queue_path = (data_root / "processed" / "actions" / "pre2011_queue"
                  / "queue.jsonl")
    queue_rows = read_jsonl(queue_path)[:top_n]
    out_dir = (data_root / "processed" / "actions" / "pre2011_circulars")
    raw_pdf, raw_txt = out_dir / "raw_pdfs", out_dir / "raw_texts"

    dl_log = {}
    if download:
        dl_log = download_registry(data_root, opener=opener)

    by_symbol: dict[str, list[dict]] = collections.defaultdict(list)
    for doc in DOC_REGISTRY:
        by_symbol[doc["symbol"]].append(doc)
    stems = sorted({d["local"] for d in DOC_REGISTRY})
    texts = load_local_texts(raw_pdf, raw_txt, stems)

    rows = [verify_one(r, by_symbol.get((r.get("symbol") or "").strip(), []),
                       texts) for r in queue_rows]

    coverage = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "queue_rows_considered": len(rows),
        "top_n": top_n,
        "verified_rows": sum(1 for r in rows
                             if r["verification_status"] in VERIFIED_STATUSES),
        "verified_split_rows": sum(1 for r in rows
                                   if r["verification_status"] == "verified_split"),
        "verified_bonus_rows": sum(1 for r in rows
                                   if r["verification_status"] == "verified_bonus"),
        "needs_issuer_circular_rows": sum(1 for r in rows
                                          if r["verification_status"] == NEEDS),
        "by_status": dict(collections.Counter(
            r["verification_status"] for r in rows)),
        "by_needs_reason": dict(collections.Counter(
            r["needs_reason"] for r in rows if r["needs_reason"])),
        "rows_with_evidence_required": sum(1 for r in rows if r.get("evidence_required")),
        "by_year": dict(sorted(collections.Counter(
            str(r["date"])[:4] for r in rows).items())),
        "documents_in_registry": len(DOC_REGISTRY),
        "documents_extracted": sorted(texts),
        "download": dl_log,
        "proximate_days": PROXIMATE_DAYS,
        "max_bytes_per_document": MAX_BYTES,
        "no_ratio_inference": True,
        "prices_adjusted": False,
        "primary_hosts": list(PRIMARY_HOST_SUFFIXES),
        "limitations": [
            "Only primary-host documents (BSE/NSE/SEC/issuer) with "
            "explicit stated terms and a stated date within 45 days "
            "of the discontinuity verify; secondary aggregators never do.",
            "BSE/issuer sites that block bots (e.g. infosys.com, "
            "hul.co.in) could not be downloaded here; those rows stay "
            "needs_issuer_circular unless an accessible primary filing "
            "(e.g. SEC 20-F) states the terms.",
            "Annual reports state split/bonus terms retrospectively; "
            "the stated effective/record date in the snippet, not the "
            "report date, is the temporal anchor.",
            "No ISIN in host bars before 2011; identity stays "
            "exact-symbol and is not ISIN-verified.",
            "No ratio is inferred from price; verified rows quote "
            "stated terms verbatim.",
        ],
        "rows": [
            {"rank": r["priority_rank"], "symbol": r["symbol"],
             "date": r["date"], "abs_close_change_pct": r["abs_close_change_pct"],
             "verification_status": r["verification_status"],
             "needs_reason": r["needs_reason"],
             "verified_event_type": r["verified_event_type"],
             "citation_url": (r["citation"] or {}).get("url")}
            for r in rows
        ],
    }
    write_jsonl(out_dir / "circulars.jsonl", rows)
    write_json(out_dir / "coverage.json", coverage)
    write_json(data_root / "reports" / "pre2011-circulars.json", coverage)
    write_report(data_root / "reports" / "pre2011-circulars.md", rows, coverage)
    return coverage


def write_report(path: Path, rows: list[dict], coverage: dict) -> None:
    lines = [
        "# Pre-2011 top-20 issuer circulars (primary sources)",
        "",
        "## Outcome",
        "",
        f"Verified the first **{coverage['queue_rows_considered']} rows** of "
        f"the prioritised pre-2011 queue against **primary issuer sources** "
        f"(BSE historical annual-report archive, issuer annual reports, "
        f"SEC Form 20-F): **{coverage['verified_rows']} verified "
        f"({coverage['verified_split_rows']} split, "
        f"{coverage['verified_bonus_rows']} bonus), "
        f"{coverage['needs_issuer_circular_rows']} `needs_issuer_circular`.** "
        "No price was adjusted, no ratio was inferred, and secondary "
        "aggregators (trendlyne/moneycontrol/...) were never used as "
        "evidence -- only as locators.",
        "",
        "- Rows: `data/processed/actions/pre2011_circulars/circulars.jsonl`",
        "- Coverage: `data/processed/actions/pre2011_circulars/coverage.json`",
        "- Builder: `src/nse_pipeline/actions/pre2011_circulars.py`",
        "- Tests: `tests/test_pre2011_circulars.py`",
        "",
        "## Verification rule (fail-closed)",
        "",
        "A row verifies only with (1) a primary-host document mentioning "
        "the queue company, (2) explicit stated split/subdivision or "
        "bonus/stock-dividend terms quoted verbatim, and (3) a stated "
        "effective/record/ex/allotment date within 45 days of the "
        "discontinuity date. Gap rows (>30d) are never promoted on a "
        "split theory alone.",
        "",
        "## Per-row results",
        "",
        "| # | Symbol | Date | Prior -> close | |move| | Status | Stated terms (primary) |",
        "|---:|---|---|---|---:|---|---|",
    ]
    for r in rows:
        terms = ""
        if r["citation"]:
            terms = (f"{r['citation']['term_kind']}: "
                     f"{r['stated_terms_verbatim'][:140]} "
                     f"[{r['citation']['url']}]")
        elif r["needs_reason"]:
            terms = f"needs: {r['needs_reason']}"
        lines.append(
            f"| {r['priority_rank']} | {r['symbol']} | {r['date']} | "
            f"{r['prior_close']} -> {r['close']} | "
            f"{r['abs_close_change_pct']:.2f}% | "
            f"{r['verification_status']} | {terms} |")
    lines += ["", "## Verified rows (verbatim stated terms)", ""]
    for r in rows:
        if r["citation"]:
            c = r["citation"]
            lines.append(
                f"- **{r['symbol']} {r['date']}** -- {c['term_kind']}, "
                f"stated dates {c['stated_dates']} "
                f"({c['min_days_from_discontinuity']}d from discontinuity). "
                f"\"{r['stated_terms_verbatim']}\" -- {c['title']} ({c['url']})")
    lines += ["", "## Limitations", ""]
    lines += [f"- {lim}" for lim in coverage["limitations"]]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=data_root())
    parser.add_argument("--top-n", type=int, default=TOP_N)
    parser.add_argument("--download", action="store_true",
                        help="download bounded primary PDFs first")
    args = parser.parse_args(argv)
    coverage = build(args.data, top_n=args.top_n, download=args.download)
    print(json.dumps({
        "queue_rows_considered": coverage["queue_rows_considered"],
        "verified_rows": coverage["verified_rows"],
        "needs_issuer_circular_rows": coverage["needs_issuer_circular_rows"],
        "by_status": coverage["by_status"],
        "by_needs_reason": coverage["by_needs_reason"],
    }, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
