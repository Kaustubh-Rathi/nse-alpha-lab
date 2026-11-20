"""Bounded scheme-PDF download + text-extraction pipeline (fail-closed).

Only stdlib is required. ``pypdf`` is imported lazily by the sibling
``ratios`` module and is supplied by the caller (e.g.
``uv run --with pypdf python ...``); this module never installs or vendors
it. If ``pypdf`` is unavailable extraction fails closed and reports
``pdf_text_extract_unavailable`` instead of guessing.

Pipeline (see ``data/reports/pdf-extract-research.md``):

1. **Discover PDFs.** Two tiers, both bounded:
   - Tier 1 (proven): direct ``attchmntFile`` URLs from the cached scheme
     index (``data/processed/schemes/scheme-index.jsonl``, 21 rows).
   - Tier 2 (NSE announcements API): per-symbol, **half-year windows**
     (Jan-Jun, Jul-Dec) via
     ``/api/corporate-announcements?index=equities&symbol=...``, filtered
     to scheme-like rows whose attachment is a PDF. At most one request
     per window, rate-limited, and acquisition **stops on any error**
     (401/403/429 included — never retry with a different identity).
2. **Download** at most ``--max-downloads`` PDFs to a SHA256-checked
   cache (``pdf_text/pdfs/`` + ``pdf_text/manifest.json``). Rate-limited;
   stops immediately on 429/401/403. Responses that are not ``%PDF-``
   bytes (HTML block pages) are rejected as ``not_a_pdf``, never cached
   as if they were scheme documents.
3. **Extract text** with ``pypdf``'s text layer, per page, into
   ``pdf_text/<name>.txt`` with page markers. Scanned/image PDFs
   (many NCLT orders) carry **no text layer**: they are flagged
   ``scanned_no_text`` with an OCR-needed note. OCR is intentionally
   **not** performed in bulk (cost + accuracy); flagged files await a
   manual OCR + independent reading pass.
4. **Run the ratios extractor** (``ratios.parse_pages``) over extracted
   text. The batch output (``pdf_text/ratios_batch.jsonl``) emits one
   record per PDF with the ratio record's page citation
   (``page_ref`` = ``p.N``, verbatim ``snippet``). Candidates are
   **never** ledger-verified by this module — a human must read the
   cited page and supply ``verified_by``. Terms are **never inferred
   from a price move**; ``ratio_from_price`` is always ``False``.

Point-in-time note: ``announcement_date`` is the exchange announcement
date as returned by NSE and is the only safe "known from" date; the PDF
itself may contain an earlier sanction/effective date.

Licensing note: NSE Terms of Use prohibit systematic/automated
collection and redistribution without express written consent. This
pipeline is bounded (default 50 jobs max, 1 req/s, half-year windows,
stop-on-error) and is for personal research use only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from nse_pipeline.paths import data_root

# --------------------------------------------------------------------------
# Sibling ratios extractor (same directory; robust to CWD / package mode)
# --------------------------------------------------------------------------
_RATIOS_SPEC = importlib.util.spec_from_file_location(
    "scheme_ratios", Path(__file__).resolve().parent / "ratios.py")
ratios = importlib.util.module_from_spec(_RATIOS_SPEC)  # type: ignore[arg-type]
_RATIOS_SPEC.loader.exec_module(ratios)  # type: ignore[union-attr]

NSE_API = "https://www.nseindia.com/api/corporate-announcements"
USER_AGENT = ("PDFExtractResearch/1.0 (personal historical-data research; "
              "bounded, rate-limited, stop-on-429)")
SCAN_CHAR_THRESHOLD = 0  # any page with stripped text => text present
OCR_NOTE = ("Scanned/image PDF (likely a scanned NCLT order): pypdf found "
            "no usable text layer. OCR was intentionally NOT performed in "
            "bulk; this file needs manual OCR plus independent reading "
            "before any term is admitted.")
PARSER_VERSION = "pdf_extract_v1"
FULL_UNIVERSE_SCHEME_DAYS = 330


_CASH_NEGATION = re.compile(
    r"(no cash consideration|does not (?:involve|entail|provide)"
    r"|not involve payment of any cash|without any cash consideration)",
    re.I)
_FACE_VALUE_CASH = re.compile(r"face value", re.I)
_COST_CASH = re.compile(
    r"(as the cost of|cost of (?:the )?(?:petition|court)|"
    r"court costs|petition costs)", re.I)
SPARSE_CHARS_PER_PAGE = 150.0  # below this, the PDF is image-heavy


class HttpStatusError(Exception):
    """A fetch failed with an HTTP status (carries ``.status``)."""

    def __init__(self, status: int, url: str, message: str = "") -> None:
        super().__init__(message or f"HTTP {status} for {url}")
        self.status = status
        self.url = url


class RateLimitStop(Exception):
    """429/401/403 seen; acquisition must stop (never retried)."""


class RateLimiter:
    """One request per ``min_interval`` seconds (injectable clock/sleeper)."""

    def __init__(self, min_interval: float = 1.0, clock=None, sleeper=None) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._clock = clock or time.monotonic
        self._sleeper = time.sleep if sleeper is None else sleeper
        self._last: float | None = None
        self.calls = 0

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None and self.min_interval > 0:
            delta = self.min_interval - (now - self._last)
            if delta > 0:
                self._sleeper(delta)
                now = self._clock()
        self._last = now
        self.calls += 1


# --------------------------------------------------------------------------
# NSE announcements API discovery (half-year windows)
# --------------------------------------------------------------------------
_SCHEME_PATTERN = re.compile(
    r"\b(?:scheme of arrangement|scheme|amalgamation|merger|demerger|"
    r"restructuring|restructure)\b", re.I)
_SCHEME_TEXT_PATTERN = re.compile(
    r"\b(?:scheme|amalgamation|demerger|restructur\w*|NCLT)\b", re.I)
_WITHDRAWN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:withdraw\w*|cancel\w*|abandon\w*|scrap\w*|"
    r"not proceeding|did not materialise|did not materialize)\b", re.I)
_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9.\-]{1,30}$")


def validate_symbol(symbol: str) -> str:
    name = (symbol or "").strip().upper()
    if not _SYMBOL_PATTERN.match(name):
        raise ValueError(f"Invalid symbol: {symbol!r}")
    return name


def half_year_windows(year: int) -> list[tuple[str, str]]:
    """Two bounded windows: (01-01..30-06) and (01-07..31-12), DD-MM-YYYY."""
    year = int(year)
    return [(f"01-01-{year}", f"30-06-{year}"),
            (f"01-07-{year}", f"31-12-{year}")]


def api_url(symbol: str, from_date: str, to_date: str) -> str:
    name = validate_symbol(symbol)
    return (f"{NSE_API}?index=equities&symbol={urllib.parse.quote(name)}"
            f"&from_date={from_date}&to_date={to_date}")


def is_scheme_row(row: dict) -> bool:
    """Scheme-like NSE announcement, including NCLT filings.

    The ``desc`` field is matched against the full merger/scheme vocabulary.
    Announcement text and attachment names are matched against a narrower
    scheme/NCLT vocabulary: actual lifecycle filings often have a generic
    ``desc`` such as “Outcome of Board Meeting”, “Updates” or “Record Date”,
    while the text or file name carries the scheme/NCLT reference.
    """
    if _SCHEME_PATTERN.search(str(row.get("desc") or "")):
        return True
    haystack = " ".join(str(row.get(field) or "")
                        for field in ("attchmntText", "attchmntFile"))
    return bool(_SCHEME_TEXT_PATTERN.search(haystack))


def is_withdrawn_row(row: dict) -> bool:
    haystack = " ".join(str(row.get(field) or "")
                        for field in ("desc", "attchmntText", "attchmntFile"))
    return bool(_WITHDRAWN_PATTERN.search(haystack))


def parse_announcement_timestamp(value: str | None) -> datetime | None:
    """Parse NSE ``an_dt`` strings such as ``27-Apr-2023 20:46:19``."""
    if value is None or str(value).strip() in ("", "-", "null", "None"):
        return None
    text = str(value).strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%B-%Y %H:%M:%S", "%d-%b-%Y",
                "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def announcement_date_for(row: dict) -> str | None:
    """Derive ISO ``announcement_date`` from an NSE announcement object."""
    if row.get("announcement_date"):
        return str(row["announcement_date"])
    parsed = parse_announcement_timestamp(row.get("an_dt"))
    return parsed.date().isoformat() if parsed else None


def dedup_key(row: dict) -> tuple[str, str, str, str]:
    return (str(row.get("symbol") or "").strip(),
            str(row.get("an_dt") or "").strip(),
            str(row.get("attchmntFile") or "").strip(),
            str(row.get("desc") or "").strip())


def parse_api_response(data: bytes) -> list[dict]:
    """Validate an NSE announcements payload (list of objects)."""
    payload = json.loads(data)
    if not isinstance(payload, list):
        raise ValueError("Expected an NSE list response")
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError("Non-object NSE announcement row")
        if not {"symbol", "an_dt", "desc"}.issubset(row.keys()):
            raise ValueError("Missing required NSE announcement fields")
    return payload


def default_fetcher(url: str, timeout: float) -> bytes:
    """One bounded request; raises HttpStatusError on HTTP failures."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise HttpStatusError(error.code, url, str(error)) from error
    except urllib.error.URLError as error:
        # Proxy/DNS failures surface here; keep the reason chain.
        raise HttpStatusError(0, url, f"URLError: {error.reason!r}") from error


def discover_scheme_filings(symbol: str, years, fetcher, limiter,
                            timeout: float = 30.0) -> dict:
    """Half-year-window discovery for one symbol (bounded, stop-on-error).

    Returns ``{"rows": [...], "windows_tried": n, "stopped": str|None}``.
    Stops at the first window that errors (401/403/429 or anything else —
    per the indexer precedent, never retry with a different identity).
    """
    name = validate_symbol(symbol)
    seen: set[tuple[str, str, str, str]] = set()
    rows: list[dict] = []
    windows_tried = 0
    stopped: str | None = None
    windows = [w for year in years for w in half_year_windows(year)]
    for from_date, to_date in windows:
        url = api_url(name, from_date, to_date)
        limiter.wait()
        try:
            content = fetcher(url, timeout)
            payload = parse_api_response(content)
        except HttpStatusError as error:
            stopped = f"HTTP {error.status}" if error.status else str(error)
            break
        except Exception as error:  # malformed JSON / timeout / ...
            stopped = f"{type(error).__name__}: {error}"
            break
        windows_tried += 1
        for row in payload:
            if str(row.get("symbol") or "").strip().upper() != name:
                continue  # API must not leak another symbol's filings
            if not is_scheme_row(row):
                continue
            pdf_url = str(row.get("attchmntFile") or "").strip()
            if not pdf_url.lower().split("?")[0].endswith(".pdf"):
                continue  # no PDF to download — not actionable here
            key = dedup_key(row)
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return {"rows": rows, "windows_tried": windows_tried, "stopped": stopped}


# --------------------------------------------------------------------------
# Job building
# --------------------------------------------------------------------------
def load_jsonl(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    return [json.loads(line)
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def load_queue(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_ledger_url_map(path: Path) -> dict[str, str]:
    """Deterministic pdf_url -> scheme_id map from the ledger filing trails."""
    mapping: dict[str, str] = {}
    for entry in load_jsonl(Path(path)):
        for filing in entry.get("filing_trail") or []:
            url = str(filing.get("pdf_url") or "").strip()
            if url and url not in mapping and entry.get("scheme_id"):
                mapping[url] = str(entry["scheme_id"])
        url = str(entry.get("pdf_url") or "").strip()
        if url and url not in mapping and entry.get("scheme_id"):
            mapping[url] = str(entry["scheme_id"])
    return mapping


def jobs_from_index(rows: list[dict], limit: int,
                    ledger_map: dict[str, str] | None = None) -> list[dict]:
    """Tier-1 jobs: deduped ``attchmntFile`` PDF URLs from cached index rows."""
    ledger_map = ledger_map or {}
    seen_urls: set[str] = set()
    jobs: list[dict] = []
    for row in sorted(rows, key=lambda r: (str(r.get("announcement_date")),
                                            str(r.get("attchmntFile") or ""))):
        pdf_url = str(row.get("attchmntFile") or "").strip()
        if not pdf_url or not pdf_url.lower().split("?")[0].endswith(".pdf"):
            continue
        if pdf_url in seen_urls:
            continue
        seen_urls.add(pdf_url)
        jobs.append({
            "symbol": row.get("symbol"),
            "announcement_date": announcement_date_for(row),
            "an_dt": row.get("an_dt"),
            "pdf_url": pdf_url,
            "source": "scheme-index",
            "scheme_id": ledger_map.get(pdf_url),
            "withdrawn": bool(row.get("withdrawn")),
            "desc": row.get("desc"),
        })
    return jobs[:limit]


def jobs_from_api_rows(rows: list[dict],
                       ledger_map: dict[str, str] | None = None) -> list[dict]:
    """Tier-2 jobs: same shape, from freshly discovered API rows."""
    jobs = jobs_from_index(rows, limit=10_000, ledger_map=ledger_map)
    for job in jobs:
        job["source"] = "nse-api"
    return jobs


def symbols_needing_discovery(queue: list[dict], index_symbols: set[str],
                              max_symbols: int) -> list[str]:
    """Queue symbols (priority order) without a cached index filing."""
    out: list[str] = []
    for item in queue:
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol or symbol in index_symbols or symbol in out:
            continue
        if not _SYMBOL_PATTERN.match(symbol):
            continue
        out.append(symbol)
        if len(out) >= max_symbols:
            break
    return out


# --------------------------------------------------------------------------
# Download with SHA256 cache
# --------------------------------------------------------------------------
def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cache_filename(url: str) -> str:
    base = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] or "scheme.pdf"
    base = re.sub(r"[^A-Za-z0-9._\-]", "_", base)
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base


def urlopen_fetcher(url: str, timeout: float) -> bytes:
    return default_fetcher(url, timeout)


def download_one(url: str, cache_dir: Path, manifest: dict, fetcher,
                 limiter: RateLimiter, timeout: float = 30.0) -> dict:
    """Download (or reuse cache) one PDF; returns a record dict.

    Never raises for content/HTTP problems except ``RateLimitStop`` on
    401/403/429, which the caller must treat as a hard stop.
    """
    path = Path(cache_dir) / cache_filename(url)
    record: dict = {"url": url, "cache_path": str(path)}
    entry = manifest.get(url, {})
    if path.exists():
        content = path.read_bytes()
        digest = compute_sha256(content)
        if entry.get("sha256") and entry["sha256"] != digest:
            # Hash drift vs manifest: fail-closed, re-download below.
            record["hash_drift"] = True
        elif content[:5] == b"%PDF-":
            record.update(status="cached", bytes=len(content), sha256=digest)
            return record
        # Non-PDF cache entry: re-download and validate again.
    limiter.wait()
    try:
        content = fetcher(url, timeout)
    except HttpStatusError as error:
        if error.status in (401, 403, 429):
            record.update(status="stopped_forbidden_or_rate_limited",
                          http_status=error.status, error=str(error))
            raise RateLimitStop(str(error)) from error
        record.update(status="fetch_failed", http_status=error.status,
                      error=str(error))
        return record
    if content[:5] != b"%PDF-":
        record.update(status="not_a_pdf", bytes=len(content),
                      error="Response is not %PDF- bytes (likely HTML block)")
        return record
    digest = compute_sha256(content)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    record.update(status="downloaded", bytes=len(content), sha256=digest)
    return record


def download_jobs(jobs: list[dict], cache_dir: Path, manifest: dict,
                  fetcher, limiter: RateLimiter,
                  max_downloads: int | None = None,
                  timeout: float = 30.0) -> tuple[list[dict], bool]:
    """Download jobs in order; returns (records, stopped_flag)."""
    records: list[dict] = []
    stopped = False
    limit = len(jobs) if max_downloads is None else max(0, max_downloads)
    for job in jobs[:limit]:
        try:
            records.append(download_one(job["pdf_url"], cache_dir, manifest,
                                        fetcher, limiter, timeout))
        except RateLimitStop as error:
            records.append({"url": job["pdf_url"], "status": "stopped",
                            "error": str(error)})
            stopped = True
            break
    return records, stopped


# --------------------------------------------------------------------------
# Text extraction (pypdf text layer; scanned flagged, never OCR'd in bulk)
# --------------------------------------------------------------------------
def extract_pages_safe(pdf_path: Path) -> tuple[list[str], str]:
    """Return (pages, extract_status) using the sibling ratios extractor."""
    try:
        pages = ratios.extract_pages(Path(pdf_path))
    except ValueError:
        return [], "pdf_text_extract_unavailable"
    if not any(p.strip() for p in pages):
        return pages, "scanned_no_text"
    return pages, "text_extracted"


def build_text_content(pages: list[str], meta: dict) -> str:
    """Page-separated text with a provenance header (no ratio claims)."""
    header = "\n".join([
        f"# Scheme PDF extracted text",
        f"# pdf_url: {meta.get('pdf_url')}",
        f"# symbol: {meta.get('symbol')}  announcement_date: "
        f"{meta.get('announcement_date')}",
        f"# sha256: {meta.get('sha256')}  pages: {len(pages)}  "
        f"chars: {sum(len(p) for p in pages)}",
        f"# method: pypdf text layer ({PARSER_VERSION}); OCR not performed",
    ])
    if meta.get("extract_status") == "scanned_no_text":
        header += "\n# " + OCR_NOTE
    parts = [header, ""]
    for i, page in enumerate(pages, start=1):
        parts.append(f"===== PAGE {i} =====")
        parts.append(page.strip())
        parts.append("")
    return "\n".join(parts)


def write_text_file(out_dir: Path, filename: str, pages: list[str],
                    meta: dict) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    # Idempotent reruns (same URL + SHA256): overwrite the same file.
    # Disambiguate only on a genuine collision (same basename, new content).
    if path.exists() and meta.get("sha256") and (
            f"# sha256: {meta['sha256']}" not in
            path.read_text(encoding="utf-8")[:2048]):
        digest = str(meta.get("sha256") or "")[:8] or "dup"
        path = out_dir / f"{Path(filename).stem}-{digest}.txt"
    path.write_text(build_text_content(pages, meta), encoding="utf-8")
    return path


def text_filename(pdf_url: str, cache_path: str) -> str:
    stem = Path(cache_filename(pdf_url)).stem
    return f"{stem}.txt"


# --------------------------------------------------------------------------
# Ratios over extracted text
# --------------------------------------------------------------------------
def build_batch_row(job: dict, download: dict, pages: list[str],
                    extract_status: str, text_path: str | None) -> dict:
    """Run the ratios extractor; return one ``ratios_batch.jsonl`` row.

    The ratio record's own fields (``page_ref``, ``snippet``, ``status``,
    ``ratio_from_price``) are preserved verbatim; download/extraction
    metadata is nested so ``validate_ratio_record`` still applies to the
    top-level ratio fields.
    """
    record = ratios.parse_pages(
        pages, pdf_url=job["pdf_url"],
        source_name=Path(str(download.get("cache_path") or "scheme.pdf")).name)
    negated_cash = False
    if (record.get("status") == "candidate"
            and record.get("ratio_num") is None
            and record.get("cash_per_share") is not None):
        # Three bare-amount classes are demonstrably not cash legs:
        # expressly negated cash, par/face-value language, and litigation
        # costs. Demote them (never promote or admit).
        joined = " ".join(ratios._norm_ws(p) for p in pages)
        snippet = str(record.get("snippet") or "")
        weak_review_status = None
        if _CASH_NEGATION.search(joined):
            weak_review_status = "needs_review_negated_cash"
        elif _FACE_VALUE_CASH.search(snippet):
            weak_review_status = "needs_review_face_value_cash"
        elif _COST_CASH.search(snippet):
            weak_review_status = "needs_review_cost_cash"
        if weak_review_status:
            cash_rejected = record.get("cash_per_share")
            record = {**record, "status": weak_review_status,
                      "cash_per_share": None,
                      "cash_amount_rejected": cash_rejected,
                      "cash_demoted": True}
            negated_cash = True
    try:
        ratios.validate_ratio_record(record)
    except ValueError:
        record = {**record, "status": "invalid_record"}
    total_chars = sum(len(p) for p in pages)
    chars_per_page = round(total_chars / max(1, len(pages)), 1)
    row = {
        **record,
        "symbol": job.get("symbol"),
        "scheme_id": job.get("scheme_id"),
        "announcement_date": job.get("announcement_date"),
        "job_source": job.get("source"),
        "scheme_withdrawn": bool(job.get("withdrawn")),
        "download": {k: v for k, v in download.items() if k != "url"},
        "extraction": {
            "status": extract_status,
            "pages": len(pages),
            "chars": total_chars,
            "chars_per_page": chars_per_page,
            "sparse_text": chars_per_page < SPARSE_CHARS_PER_PAGE,
            "text_file": text_path,
            "ocr_performed": False,
            "ocr_note": OCR_NOTE if extract_status == "scanned_no_text" else None,
        },
        "pipeline_version": PARSER_VERSION,
        "ratio_from_price": False,
    }
    return row


# --------------------------------------------------------------------------
# Coverage / throughput
# --------------------------------------------------------------------------
def summarize(batch_rows: list[dict], download_records: list[dict],
              api_summary: dict, elapsed_sec: float) -> dict:
    downloaded = sum(1 for r in download_records if r.get("status") == "downloaded")
    cached = sum(1 for r in download_records if r.get("status") == "cached")
    failed = sum(1 for r in download_records
                 if r.get("status") in ("fetch_failed", "not_a_pdf"))
    stopped = sum(1 for r in download_records
                  if r.get("status") in ("stopped_forbidden_or_rate_limited",
                                         "stopped"))
    text_extracted = sum(1 for r in batch_rows
                         if r["extraction"]["status"] == "text_extracted")
    scanned = sum(1 for r in batch_rows
                  if r["extraction"]["status"] == "scanned_no_text")
    candidates = sum(1 for r in batch_rows if r.get("status") == "candidate")
    n = len(batch_rows)
    sec_per_job = (elapsed_sec / n) if n else 0.0
    return {
        "jobs_requested": n,
        "pdfs_downloaded": downloaded,
        "pdfs_cached": cached,
        "pdfs_failed": failed,
        "download_stopped": stopped,
        "api_discovery": api_summary,
        "text_extracted": text_extracted,
        "text_extracted_pct": round(100.0 * text_extracted / n, 1) if n else 0.0,
        "scanned_no_text": scanned,
        "scanned_pct": round(100.0 * scanned / n, 1) if n else 0.0,
        "no_explicit_ratio": sum(1 for r in batch_rows
                                 if r.get("status") == "no_explicit_ratio"),
        "ratios_found_candidates": candidates,
        "needs_review_conflicting_ratios": sum(
            1 for r in batch_rows
            if r.get("status") == "needs_review_conflicting_ratios"),
        "needs_review_negated_cash": sum(
            1 for r in batch_rows
            if r.get("status") == "needs_review_negated_cash"),
        "needs_review_face_value_cash": sum(
            1 for r in batch_rows
            if r.get("status") == "needs_review_face_value_cash"),
        "needs_review_cost_cash": sum(
            1 for r in batch_rows
            if r.get("status") == "needs_review_cost_cash"),
        "sparse_text_files": [
            f"{r.get('symbol')} {r.get('announcement_date')}"
            for r in batch_rows if r["extraction"].get("sparse_text")],
        "pdf_text_extract_unavailable": sum(
            1 for r in batch_rows
            if r.get("status") == "pdf_text_extract_unavailable"),
        "ratios_inferred_from_price": False,
        "ocr_performed": False,
        "elapsed_sec": round(elapsed_sec, 2),
        "sec_per_job": round(sec_per_job, 3),
        "jobs_per_hour": round(3600.0 / sec_per_job, 1) if sec_per_job > 0 else None,
        "extrapolated_330_jobs_hours": (
            round(FULL_UNIVERSE_SCHEME_DAYS * sec_per_job / 3600.0, 2)
            if sec_per_job > 0 else None),
        "full_universe_scheme_days": FULL_UNIVERSE_SCHEME_DAYS,
        "limitations": [
            "Candidates are NOT ledger-verified: a human must read the cited page and supply verified_by + page_ref",
            "Scanned PDFs are flagged scanned_no_text; bulk OCR is intentionally not performed",
            "API discovery (NSE announcements) is bounded to half-year windows and stops on any HTTP error",
            "NSE ToS restrict automated collection/redistribution; bounded personal-research use only",
            "This proof run covers Tier-1 index URLs (cap 50); the remaining 330-scheme-day queue needs (symbol, year) pairs from the reorganization universe to feed Tier-2 discovery",
        ],
    }


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def run(data: Path, index_path: Path, queue_path: Path, ledger_path: Path,
        out_dir: Path, limit: int = 50, max_downloads: int | None = None,
        min_interval: float = 1.0, timeout: float = 30.0, api_years=(2023, 2024),
        api_max_symbols: int = 14, use_api: bool = True, dry_run: bool = False,
        fetcher=None) -> dict:
    """Bounded end-to-end run; writes txt + ratios_batch.jsonl + manifest."""
    data, out_dir = Path(data), Path(out_dir)
    started = time.monotonic()

    ledger_map = load_ledger_url_map(Path(ledger_path))
    index_rows = load_jsonl(Path(index_path))
    jobs = jobs_from_index(index_rows, limit, ledger_map)

    api_summary: dict = {"symbols_tried": [], "windows_tried": 0,
                         "new_filings": 0, "stopped": None, "symbols": []}
    use_fetcher = fetcher or urlopen_fetcher
    limiter = RateLimiter(min_interval)

    if use_api and not dry_run and len(jobs) < limit:
        queue = load_queue(Path(queue_path))
        index_symbols = {str(r.get("symbol") or "").strip().upper()
                         for r in index_rows if r.get("symbol")}
        symbols = symbols_needing_discovery(queue, index_symbols,
                                            api_max_symbols)
        seen_urls = {j["pdf_url"] for j in jobs}
        for symbol in symbols:
            if len(jobs) >= limit:
                break
            found = discover_scheme_filings(symbol, api_years, use_fetcher,
                                            limiter, timeout)
            api_summary["symbols_tried"].append(symbol)
            api_summary["windows_tried"] += found["windows_tried"]
            api_summary["new_filings"] += len(found["rows"])
            if found["stopped"]:
                api_summary["stopped"] = f"{symbol}: {found['stopped']}"
                break  # stop-on-error: never hammer a failing endpoint
            for job in jobs_from_api_rows(found["rows"], ledger_map):
                if job["pdf_url"] not in seen_urls and len(jobs) < limit:
                    seen_urls.add(job["pdf_url"])
                    jobs.append(job)
        api_summary["symbols"] = api_summary["symbols_tried"]
    elif not use_api:
        api_summary["stopped"] = "API discovery disabled (--no-api)"

    jobs = jobs[:limit]

    cache_dir = out_dir / "pdfs"
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    manifest: dict = (json.loads(manifest_path.read_text(encoding="utf-8"))
                      if manifest_path.exists() else {})

    if dry_run:
        # No network at all: cache-hit inside download_one is bypassed by a
        # fetcher that always fails; cached files still count via records
        # only if present. Simplest honest dry-run: mark uncached as skipped.
        def _no_fetch(url: str, timeout: float) -> bytes:
            raise HttpStatusError(0, url, "dry-run: network disabled")
        download_records, _ = download_jobs(jobs, cache_dir, manifest,
                                            _no_fetch, limiter,
                                            max_downloads, timeout)
    else:
        download_records, _ = download_jobs(jobs, cache_dir, manifest,
                                            use_fetcher, limiter,
                                            max_downloads, timeout)

    # Persist refreshed SHA256 manifest (url -> record).
    for record in download_records:
        if record.get("sha256"):
            manifest[record["url"]] = {
                "sha256": record["sha256"], "bytes": record.get("bytes"),
                "cache_path": record.get("cache_path"),
                "downloaded_at": datetime.now(timezone.utc).isoformat(),
            }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False)
                             + "\n", encoding="utf-8")

    by_url = {r["url"]: r for r in download_records}
    batch_rows: list[dict] = []
    for job in jobs:
        record = by_url.get(job["pdf_url"], {"status": "not_attempted",
                                             "url": job["pdf_url"]})
        pages: list[str] = []
        extract_status = "not_downloaded"
        text_path: str | None = None
        cache_path = Path(str(record.get("cache_path") or
                              cache_dir / cache_filename(job["pdf_url"])))
        if record.get("status") in ("downloaded", "cached") and cache_path.exists():
            pages, extract_status = extract_pages_safe(cache_path)
            text_path = str(write_text_file(
                out_dir, text_filename(job["pdf_url"], str(cache_path)),
                pages,
                {"pdf_url": job["pdf_url"], "symbol": job.get("symbol"),
                 "announcement_date": job.get("announcement_date"),
                 "sha256": record.get("sha256"), "extract_status": extract_status}))
        batch_rows.append(build_batch_row(job, record, pages,
                                          extract_status, text_path))

    ratios_path = out_dir / "ratios_batch.jsonl"
    with ratios_path.open("w", encoding="utf-8") as stream:
        for row in batch_rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    elapsed = time.monotonic() - started
    coverage = summarize(batch_rows, download_records, api_summary, elapsed)
    coverage["files"] = {
        "texts": f"{out_dir.as_posix()}/*.txt",
        "ratios_batch": ratios_path.as_posix(),
        "manifest": manifest_path.as_posix(),
        "pdf_cache": cache_dir.as_posix(),
        "pipeline": "src/nse_pipeline/schemes/pdf_extract.py",
        "tests": "tests/test_pdf_extract.py",
    }
    return coverage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=data_root())
    parser.add_argument("--index", type=Path, default=None,
                        help="scheme-index.jsonl (default: <data>/processed/schemes/...)")
    parser.add_argument("--queue", type=Path, default=None,
                        help="pdf-reading-queue.json (default: <data>/processed/ledger/...)")
    parser.add_argument("--ledger", type=Path, default=None,
                        help="scheme-ledger.jsonl for URL->scheme_id join")
    parser.add_argument("--out", type=Path, default=None,
                        help="output dir (default: <data>/processed/schemes/pdf_text)")
    parser.add_argument("--limit", type=int, default=50,
                        help="max PDF jobs to process (top-50 proof run)")
    parser.add_argument("--max-downloads", type=int, default=None,
                        help="max fresh downloads (default: --limit)")
    parser.add_argument("--min-interval", type=float, default=1.0,
                        help="rate limit: min seconds between requests")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--api-years", default="2023,2024",
                        help="comma-separated years for API half-year windows")
    parser.add_argument("--api-max-symbols", type=int, default=14)
    parser.add_argument("--no-api", action="store_true",
                        help="skip NSE announcements API discovery")
    parser.add_argument("--dry-run", action="store_true",
                        help="no network; reuse cache only")
    args = parser.parse_args()

    data = args.data
    index = args.index or data / "processed/schemes/scheme-index.jsonl"
    queue = args.queue or data / "processed/ledger/pdf-reading-queue.json"
    ledger = args.ledger or data / "processed/ledger/scheme-ledger.jsonl"
    out = args.out or data / "processed/schemes/pdf_text"
    years = tuple(int(y) for y in args.api_years.split(",") if y.strip())
    max_dl = args.max_downloads if args.max_downloads is not None else args.limit
    coverage = run(data, index, queue, ledger, out,
                   limit=args.limit, max_downloads=max_dl,
                   min_interval=args.min_interval, timeout=args.timeout,
                   api_years=years, api_max_symbols=args.api_max_symbols,
                   use_api=not args.no_api, dry_run=args.dry_run)
    print(json.dumps(coverage, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
