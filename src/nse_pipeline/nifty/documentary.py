"""Exhaustive public documentary assembly for historical Nifty membership.

Scope (own files only):
  * Raw artifacts:      data/raw/nifty_doc/
  * Canonical output:   data/processed/nifty_doc/
  * Reports:            data/reports/nifty-doc-research.md,
                        data/reports/nifty-doc-coverage.json

Official source: NSE Indices historical-data lookup
(POST https://www.niftyindices.com/reports/historical-data/Index/,
report type 2 = Nifty 50 MCWB ZIP, type 4 = market-cap/weightage ZIP)
and the press-release archive (ind_prs*.pdf).

Hard rules, inherited from src/index_membership.py and enforced here:
  * A snapshot is only a membership record on its own document-stated date.
  * Change notices are parsed into an *unapplied* changelog. No event is
    ever applied to synthesize a list; every event requires a manual
    reconciliation checkpoint (symbol mapping, effective-date exceptions,
    reversals/corrections) before it can be used.
  * No fabrication, no forward/backward fill. Failed imports are recorded
    with their reason and never repaired by guessing.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from nse_pipeline.paths import REPO_ROOT, data_root
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from nse_pipeline.nifty.snapshots import (  # noqa: E402
    Member, Snapshot, import_mcwb, import_pdf_pages, join_pdf_pages, sha256,
    validate, write_snapshot,
)

OFFICIAL_BASE = "https://www.niftyindices.com"
LOOKUP_URL = OFFICIAL_BASE + "/reports/historical-data/Index/"
PRESS_PAGE_URL = OFFICIAL_BASE + "/press-release"
PRESS_BASE = OFFICIAL_BASE + "/Press_Release/"
USER_AGENT = "Mozilla/5.0 (nifty-doc research; stdlib urllib)"
MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTHS_FULL = {name.lower(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"), 1)}
SYMBOL_RE = re.compile(r"[A-Z0-9][A-Z0-9&.\-]*\Z")
PRESS_FILE_RE = re.compile(r"ind_prs(\d{8})(?:_(\d+))?\.pdf\Z", re.I)
PRESS_LINK_RE = re.compile(r"ind_prs\d{8}(?:_\d+)?\.pdf", re.I)
PDF_HEADER_50_RE = re.compile(
    r"Constituents of (?:Nifty|NIFTY)\s+50\s+([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})")
PDF_ROW_RE = re.compile(r"^([A-Z0-9][A-Z0-9&.\-]*)\s+.*?\s+\d+\.\d{2}\s+\d")

ROOT = REPO_ROOT
RAW = data_root() / "raw" / "nifty_doc"
PROCESSED = data_root() / "processed" / "nifty_doc"
EXISTING_CATALOG = data_root() / "processed" / "index_membership" / "catalog.json"


def _reference_path(path: Path) -> str:
    """Keep repo-relative provenance where possible, explicit external paths otherwise."""
    resolved = path.resolve()
    return str(resolved.relative_to(ROOT)) if resolved.is_relative_to(ROOT) else str(resolved)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Historical-data lookup (report types 2 and 4)
# --------------------------------------------------------------------------

def iter_months(first: tuple[int, int], last: tuple[int, int]):
    """Yield (year, month) from first to last inclusive."""
    year, month = first
    while (year, month) <= last:
        yield year, month
        month += 1
        if month > 12:
            year, month = year + 1, 1


def month_year_label(year: int, month: int) -> str:
    return f"{MONTH_ABBR[month - 1]} {year}"


def parse_probe_response(body: bytes, report_type: str, year: int,
                         month: int, retrieved_at: str) -> dict:
    """Parse one lookup response; never guess a download link."""
    record: dict = {"report_type": str(report_type), "year": year,
                    "month": month, "month_year": month_year_label(year, month),
                    "success": False, "download_link": None, "filename": None,
                    "report_name": None, "message": None,
                    "retrieved_at_utc": retrieved_at,
                    "response_sha256": sha256(body)}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        record["message"] = f"malformed_json: {exc}"
        return record
    if payload.get("success") is True:
        data = payload.get("data") or []
        if len(data) != 1 or not isinstance(data[0], dict):
            record["message"] = "unexpected_success_payload_shape"
            return record
        record["success"] = True
        record["download_link"] = data[0].get("DownloadLink")
        record["filename"] = data[0].get("FileName")
        record["report_name"] = payload.get("reportName")
        if not record["download_link"] or not record["filename"]:
            record["success"] = False
            record["message"] = "success_without_download_link"
    else:
        record["message"] = payload.get("message", "no_data")
    return record


def lookup_month(report_type: str, year: int, month: int,
                 retries: int = 3) -> dict:
    body = json.dumps({"SelectedReportType": str(report_type),
                       "SelectedDate": "",
                       "MonthYear": month_year_label(year, month)}).encode()
    last_error = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                LOOKUP_URL, data=body,
                headers={"Content-Type": "application/json",
                         "User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response:
                return parse_probe_response(response.read(), report_type,
                                            year, month, now_utc())
        except Exception as exc:  # noqa: BLE001 - recorded, retried
            last_error = exc
            time.sleep(2 * (attempt + 1))
    return {"report_type": str(report_type), "year": year, "month": month,
            "month_year": month_year_label(year, month), "success": False,
            "download_link": None, "filename": None, "report_name": None,
            "message": f"request_failed_after_{retries}_tries: {last_error}",
            "retrieved_at_utc": now_utc(), "response_sha256": None}


def cmd_probe(args) -> int:
    jobs = [(t, y, m) for t in ("2", "4")
            for (y, m) in iter_months((args.from_year, args.from_month),
                                      (args.to_year, args.to_month))]
    records: list[dict] = []
    print(f"probing {len(jobs)} lookups "
          f"({args.from_year}-{args.from_month:02d}..{args.to_year}-{args.to_month:02d})")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(lookup_month, t, y, m) for (t, y, m) in jobs]
        for future in as_completed(futures):
            records.append(future.result())
    records.sort(key=lambda r: (r["report_type"], r["year"], r["month"]))
    RAW.mkdir(parents=True, exist_ok=True)
    out = RAW / "archive_probes.json"
    out.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    ok = sum(1 for r in records if r["success"])
    print(f"wrote {out} ({ok}/{len(records)} success)")
    for t in ("2", "4"):
        hits = [r for r in records if r["report_type"] == t and r["success"]]
        if hits:
            print(f"  type {t}: {len(hits)} available, "
                  f"{hits[0]['month_year']} .. {hits[-1]['month_year']}")
        else:
            print(f"  type {t}: none available")
    return 0


# --------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------

def download(url: str, destination: Path, retries: int = 3) -> dict:
    """Download to destination (idempotent skip) and write provenance."""
    if destination.exists() and (destination.with_suffix(".provenance.json")
                                 .exists() or destination.suffix == ".json"):
        return {"file": str(destination), "skipped": True}
    last_error = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=120) as response:
                content = response.read()
                content_type = response.headers.get("Content-Type", "")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            provenance = {"file": destination.name, "url": url,
                          "retrieved_at_utc": now_utc(),
                          "sha256": sha256(content), "bytes": len(content),
                          "content_type": content_type}
            (destination.with_name(destination.name + ".provenance.json")
             ).write_text(json.dumps(provenance, indent=2) + "\n",
                          encoding="utf-8")
            return {"file": str(destination), "skipped": False,
                    **provenance}
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"download failed for {url}: {last_error}")


def fetch_url(url: str, timeout: int = 120) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def cmd_fetch_monthly(args) -> int:
    records = json.loads((RAW / "archive_probes.json").read_text())
    hits = [r for r in records if r["success"]]
    # Skip re-downloading files already present (provenance check inside).
    todo = []
    for record in hits:
        subdir = RAW / ("zips_type" + record["report_type"])
        dest = subdir / record["filename"]
        if not (dest.with_name(dest.name + ".provenance.json").exists()):
            todo.append((OFFICIAL_BASE + record["download_link"], dest))
    print(f"downloading {len(todo)} monthly ZIPs "
          f"({len(hits)} successful probes)")
    done, failed = 0, []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download, url, dest): dest
                   for url, dest in todo}
        for future in as_completed(futures):
            try:
                future.result()
                done += 1
            except Exception as exc:  # noqa: BLE001
                failed.append(str(exc))
    print(f"downloaded {done}, failed {len(failed)}")
    for item in failed[:20]:
        print("  FAILED:", item)
    return 0


# --------------------------------------------------------------------------
# ZIP member selection / snapshot import
# --------------------------------------------------------------------------

def select_mcwb_member(namelist: list[str]) -> str | None:
    """Pick the Nifty 50 MCWB member (never Next 50 / Junior)."""
    candidates = [n for n in namelist
                  if n.lower().endswith(".csv")
                  and "nifty" in n.lower()
                  and "next" not in n.lower()
                  and "jr" not in n.lower().split("_")]
    candidates.sort(key=lambda n: (n.lower().endswith("nifty50_mcwb.csv"),
                                   n.lower()))
    return candidates[-1] if candidates else None


def select_type4_member(namelist: list[str], index_code: str) -> str | None:
    """Pick the Nifty 50 / Nifty 500 PDF member from a type-4 ZIP."""
    pattern = (r"nifty[_ ]?500" if index_code == "NIFTY500"
               else r"nifty[_ ]?50(?![0-9])")
    hits = []
    for name in namelist:
        low = name.lower()
        if not low.endswith(".pdf"):
            continue
        if "next" in low or "value" in low:
            continue
        if index_code == "NIFTY500":
            if re.search(r"nifty[_ ]?500|cnx500", low):
                hits.append(name)
        elif re.search(pattern, low) or "cnxnifty_" in low or low.endswith(
                "cnxnifty.pdf"):
            hits.append(name)
    hits.sort()
    return hits[0] if hits else None


def import_pdf_pages_50(pages: list[str], pdf_bytes: bytes,
                        provenance: dict, source_member: str,
                        source_text_name: str = "",
                        cutoff: date = date(2026, 11, 7)) -> Snapshot:
    """Nifty 50 analogue of index_membership.import_pdf_pages.

    Mirrors its fail-closed semantics (explicit document date, regular
    page table, derived count, official HTTPS source provenance).
    """
    text = join_pdf_pages(pages)
    headers = list(PDF_HEADER_50_RE.finditer(text))
    if not headers:
        raise ValueError("one explicit document-stated date is required")
    header = headers[0]
    if len({" ".join(m.group(0).split()) for m in headers}) != 1:
        raise ValueError("conflicting document-stated dates")
    month, day, year = header.groups()
    month_number = MONTHS_FULL.get(month.lower())
    if month_number is None:
        raise ValueError("unknown report month")
    as_of = date(int(year), month_number, int(day))
    counts, symbols = [], []
    for page in pages:
        page_symbols = [m.group(1) for m in
                        (PDF_ROW_RE.match(line) for line in page.splitlines())
                        if m]
        if not page_symbols:
            raise ValueError("empty constituent page")
        counts.append(len(page_symbols))
        symbols.extend(page_symbols)
    if len(counts) > 1 and (len(set(counts[:-1])) != 1
                            or counts[-1] > counts[0]):
        raise ValueError("irregular constituent page table")
    members = tuple(Member(symbol) for symbol in symbols)
    meta = {
        "index_code": "NIFTY50", "as_of": as_of.isoformat(),
        "membership_kind": "dated_official_snapshot",
        "as_of_basis": "document_explicit_date",
        "as_of_evidence": " ".join(header.group(0).split()),
        "expected_security_count": len(members),
        "source_url": provenance["url"], "source_sha256": provenance["sha256"],
        "source_member": source_member,
        "source_member_sha256": sha256(pdf_bytes),
        "retrieved_at_utc": provenance["retrieved_at_utc"],
        "extraction": ("Symbol column only from every PDF page (Nifty 50 "
                       "header variant); company name and ISIN left empty, "
                       "not inferred."),
        "symbol_row_regex": PDF_ROW_RE.pattern,
        "rows_per_page": counts, "source_pdf_pages": len(pages),
        "source_text_sha256": sha256(text.encode("utf-8")),
    }
    if source_text_name:
        meta["source_text_name"] = source_text_name
    snapshot = Snapshot("NIFTY50", as_of, members, meta)
    validate(snapshot, cutoff)
    return snapshot


PDF_HEADER_GENERIC_RE = re.compile(
    r"Constituents of (S&P CNX Nifty|S&P CNX 500|CNX Nifty|CNX 500|"
    r"Nifty 50|NIFTY 50|Nifty 500|NIFTY 500)\s+([A-Z][a-z]+)\s+"
    r"(\d{1,2}),\s*(\d{4})", re.I)
GENERIC_INDEX_MAP = {"s&p cnx nifty": "NIFTY50", "cnx nifty": "NIFTY50",
                     "nifty 50": "NIFTY50", "nifty 500": "NIFTY500",
                     "s&p cnx 500": "NIFTY500", "cnx 500": "NIFTY500"}
MCWB_TITLE_RE = re.compile(
    r"(?:nifty\s*50|s&p\s+cnx\s+nifty|cnx\s+nifty)\s+index", re.I)
MCWB_DATE_RE = re.compile(
    r"Last day of trading was\s+(\d{1,2})[-\s]+([A-Za-z]+)[-\s]+(\d{4})", re.I)


def import_mcwb_doc(source: Path, member_name: str, provenance: dict,
                    cutoff: date = date(2026, 11, 7)) -> Snapshot:
    """MCWB import for official variants index_membership rejects.

    Same fail-closed core as index_membership.import_mcwb (explicit
    last-trading-day date, header row, complete 1..n serial set), with
    three documented relaxations, each noted in the manifest:
      * single-byte (cp1252) fallback when the file is not UTF-8;
      * serials in any file order if the set is exactly 1..n (output
        sorted by serial number);
      * report title searched in the first five lines, not only row 0.
    A file with no explicit date still fails; filenames are never dates.
    """
    content = source.read_bytes()
    if provenance.get("sha256") != sha256(content):
        raise ValueError("source digest mismatch")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        report = archive.read(member_name)
    try:
        text = report.decode("utf-8-sig")
        encoding = "utf-8-sig"
    except UnicodeDecodeError:
        text = report.decode("cp1252")
        encoding = "cp1252 (utf-8 failed)"
    rows = list(csv.reader(io.StringIO(text)))
    head = " ".join(" ".join(row) for row in rows[:5]).lower()
    if not MCWB_TITLE_RE.search(head):
        raise ValueError("not a Nifty 50 MCWB report")
    matches = list(MCWB_DATE_RE.finditer(text))
    if len(matches) != 1:
        raise ValueError("one explicit last trading date is required")
    match = matches[0]
    day, month, year = match.groups()
    month_number = next((n for name, n in MONTHS_FULL.items()
                         if month.lower() in (name, name[:3])), None)
    if month_number is None:
        raise ValueError("unknown report month")
    as_of = date(int(year), month_number, int(day))
    header = next((row for row in rows
                   if any(cell.strip() == "Security Symbol" for cell in row)),
                  None)
    if header is None:
        raise ValueError("missing constituent table header")
    symbol_column = [c.strip() for c in header].index("Security Symbol")
    name_column = next((i for i, c in enumerate(header)
                        if c.strip() == "Security Name"), None)
    numbered = [row for row in rows if row and row[0].strip().isdigit()]
    serials = [int(row[0]) for row in numbered]
    if sorted(serials) != list(range(1, len(numbered) + 1)):
        raise ValueError("incomplete constituent table")
    reordered = serials != sorted(serials)
    numbered.sort(key=lambda row: int(row[0]))
    members = tuple(
        Member(row[symbol_column].strip(),
               row[name_column].strip() if name_column is not None else "")
        for row in numbered)
    meta = {
        "index_code": "NIFTY50", "as_of": as_of.isoformat(),
        "membership_kind": "dated_official_snapshot",
        "as_of_basis": "document_explicit_date",
        "as_of_evidence": " ".join(match.group(0).split()),
        "expected_security_count": len(members),
        "source_url": provenance["url"], "source_sha256": sha256(content),
        "source_member": member_name,
        "source_member_sha256": sha256(report),
        "retrieved_at_utc": provenance["retrieved_at_utc"],
        "extraction": ("nifty_doc variant import: complete 1..n serial set; "
                       f"text decoded as {encoding}"
                       + ("; serials re-sorted (official file order differed)"
                          "; no inferred ISINs." if reordered else "; no "
                       "inferred ISINs.")),
    }
    snapshot = Snapshot("NIFTY50", as_of, members, meta)
    validate(snapshot, cutoff)
    return snapshot


def import_pdf_pages_generic(pages: list[str], index_code: str,
                               pdf_bytes: bytes, provenance: dict,
                               source_member: str, source_text_name: str = "",
                               cutoff: date = date(2026, 11, 7)) -> Snapshot:
    """PDF import accepting CNX/S&P CNX/Nifty header variants.

    Same fail-closed core as index_membership.import_pdf_pages. The
    document's index name is preserved in the manifest so a CNX-era
    document is never silently indistinguishable from a Nifty-era one.
    """
    if index_code not in ("NIFTY50", "NIFTY500"):
        raise ValueError("unsupported index_code")
    text = join_pdf_pages(pages)
    headers = list(PDF_HEADER_GENERIC_RE.finditer(text))
    if not headers:
        raise ValueError("one explicit document-stated date is required")
    header = headers[0]
    if len({" ".join(m.group(0).split()) for m in headers}) != 1:
        raise ValueError("conflicting document-stated dates")
    doc_index, month, day, year = header.groups()
    if GENERIC_INDEX_MAP.get(" ".join(doc_index.lower().split())) != index_code:
        raise ValueError(f"document index {doc_index!r} is not {index_code}")
    month_number = MONTHS_FULL.get(month.lower())
    if month_number is None:
        raise ValueError("unknown report month")
    as_of = date(int(year), month_number, int(day))
    counts, symbols = [], []
    for page in pages:
        page_symbols = [m.group(1) for m in
                        (PDF_ROW_RE.match(line) for line in page.splitlines())
                        if m]
        if not page_symbols:
            raise ValueError("empty constituent page")
        counts.append(len(page_symbols))
        symbols.extend(page_symbols)
    if len(counts) > 1 and (len(set(counts[:-1])) != 1
                            or counts[-1] > counts[0]):
        raise ValueError("irregular constituent page table")
    members = tuple(Member(symbol) for symbol in symbols)
    meta = {
        "index_code": index_code, "as_of": as_of.isoformat(),
        "membership_kind": "dated_official_snapshot",
        "as_of_basis": "document_explicit_date",
        "as_of_evidence": " ".join(header.group(0).split()),
        "index_name_in_document": doc_index,
        "expected_security_count": len(members),
        "source_url": provenance["url"], "source_sha256": provenance["sha256"],
        "source_member": source_member,
        "source_member_sha256": sha256(pdf_bytes),
        "retrieved_at_utc": provenance["retrieved_at_utc"],
        "extraction": ("Symbol column only from every PDF page; company name "
                       "and ISIN left empty, not inferred. "
                       f"Document index name preserved: {doc_index}."),
        "symbol_row_regex": PDF_ROW_RE.pattern,
        "rows_per_page": counts, "source_pdf_pages": len(pages),
        "source_text_sha256": sha256(text.encode("utf-8")),
    }
    if source_text_name:
        meta["source_text_name"] = source_text_name
    snapshot = Snapshot(index_code, as_of, members, meta)
    validate(snapshot, cutoff)
    return snapshot


def select_type4_csv_member(namelist: list[str], index_code: str) -> str | None:
    """Pick the Nifty 50 / Nifty 500 CSV member from a type-4 ZIP."""
    hits = []
    for name in namelist:
        low = name.lower()
        if not low.endswith(".csv"):
            continue
        if "next" in low or "value" in low:
            continue
        if index_code == "NIFTY500":
            if "cnx500" in low or "nifty500" in low or "nifty_500" in low:
                hits.append(name)
        elif "cnxnifty_" in low or "nifty50" in low or "nifty_50" in low:
            hits.append(name)
    hits.sort()
    return hits[0] if hits else None


def import_type4_csv(csv_bytes: bytes, provenance: dict, source_member: str,
                     cutoff: date = date(2026, 11, 7)) -> Snapshot:
    """Import a type-4 constituent CSV (Date/Index Name/Symbol columns).

    Fail-closed: every row must carry the same explicit date and index
    name; the date is never inferred from the filename or ZIP name.
    Only the April-2013 type-4 ZIP uses CSV members.
    """
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = csv_bytes.decode("cp1252")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "Date" not in rows[0] or "Symbol" not in rows[0] \
            or "Index Name" not in rows[0]:
        raise ValueError("type-4 CSV lacks Date/Index Name/Symbol columns")
    dates = {row["Date"].strip() for row in rows}
    names = {row["Index Name"].strip() for row in rows}
    if len(dates) != 1 or len(names) != 1:
        raise ValueError("type-4 CSV must carry one date and one index name")
    day, month, year = next(iter(dates)).split("-")
    as_of = date(int(year), int(month), int(day))
    index_code = GENERIC_INDEX_MAP.get(
        " ".join(next(iter(names)).lower().split()))
    if index_code is None:
        raise ValueError("unsupported document index name")
    symbols = [row["Symbol"].strip() for row in rows]
    name_by_symbol = {row["Symbol"].strip(): row["Security Name"].strip()
                      for row in rows if row.get("Security Name")}
    if any(not SYMBOL_RE.fullmatch(s) for s in symbols):
        raise ValueError("invalid symbol in type-4 CSV")
    members = tuple(Member(s, name_by_symbol.get(s, "")) for s in symbols)
    meta = {
        "index_code": index_code, "as_of": as_of.isoformat(),
        "membership_kind": "dated_official_snapshot",
        "as_of_basis": "document_explicit_date",
        "as_of_evidence": (f"{next(iter(names))} / "
                           f"{next(iter(dates))} (Date column, all rows)"),
        "index_name_in_document": next(iter(names)),
        "expected_security_count": len(members),
        "source_url": provenance["url"], "source_sha256": provenance["sha256"],
        "source_member": source_member,
        "source_member_sha256": sha256(csv_bytes),
        "retrieved_at_utc": provenance["retrieved_at_utc"],
        "extraction": ("Complete type-4 CSV table; Symbol and Security "
                       "Name columns kept, no inferred ISINs."),
    }
    snapshot = Snapshot(index_code, as_of, members, meta)
    validate(snapshot, cutoff)
    return snapshot


def extract_pdf_pages(pdf_bytes: bytes) -> list[str]:
    """Per-page text via pypdf (optional dependency, never in pyproject)."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "pypdf is required for PDF text extraction; run with "
            "`uv run --with pypdf` (does not modify pyproject/uv.lock)"
        ) from exc
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [(page.extract_text() or "") for page in reader.pages]


def cmd_texts(args) -> int:
    """Extract retained text for type-4 member PDFs and press releases."""
    # Type-4 member PDFs inside ZIPs.
    for zip_path in sorted((RAW / "zips_type4").glob("*.zip")):
        with zipfile.ZipFile(zip_path) as archive:
            for index_code in ("NIFTY50", "NIFTY500"):
                member = select_type4_member(archive.namelist(), index_code)
                if member is None:
                    continue
                stem = f"{zip_path.stem}__{Path(member).stem}"
                pdf_dest = RAW / "pdfs" / (stem + ".pdf")
                txt_dest = RAW / "pdfs" / (stem + ".txt")
                if txt_dest.exists():
                    continue
                pdf_bytes = archive.read(member)
                pdf_dest.parent.mkdir(parents=True, exist_ok=True)
                pdf_dest.write_bytes(pdf_bytes)
                pages = extract_pdf_pages(pdf_bytes)
                txt_dest.write_bytes(join_pdf_pages(pages).encode("utf-8"))
                print(f"text {stem}: {len(pages)} pages")
    # Press releases.
    press_dir = RAW / "press"
    failures: list[dict] = []
    if press_dir.exists():
        for pdf_path in sorted(press_dir.glob("*.pdf")):
            txt_path = pdf_path.with_suffix(".txt")
            if txt_path.exists():
                continue
            try:
                pages = extract_pdf_pages(pdf_path.read_bytes())
            except Exception as exc:  # noqa: BLE001 - record, never hide
                failures.append({"file": pdf_path.name,
                                 "reason": f"{type(exc).__name__}: {exc}"})
                continue
            txt_path.write_bytes(
                ("\n\f\n".join(p.rstrip("\n") for p in pages) + "\n"
                 ).encode("utf-8"))
    (RAW / "press_text_failures.json").write_text(
        json.dumps({"generated_at_utc": now_utc(), "failures": failures},
                   indent=2) + "\n", encoding="utf-8")
    print(f"text extraction done; press failures {len(failures)}")
    return 0


def existing_keys() -> set[tuple[str, str]]:
    try:
        catalog = json.loads(EXISTING_CATALOG.read_text())
    except (OSError, ValueError):
        return set()
    return {(row["index_code"], row["as_of"]) for row in catalog}


def cmd_snapshots(args) -> int:
    """Import every downloaded monthly ZIP into processed/nifty_doc/."""
    imported: list[dict] = []
    failed: list[dict] = []
    seen = existing_keys()

    def record(snapshot: Snapshot, source_path: Path, source_url: str,
               importer: str = "index_membership"):
        key = (snapshot.index_code, snapshot.as_of.isoformat())
        csv_path = PROCESSED / f"{key[0]}_{key[1]}.csv"
        if csv_path.exists():
            # Type-2 MCWB and type-4 PDF for the same month-end both exist.
            # Keep the first import; verify the duplicate agrees symbol-wise
            # and record it as a cross-source duplicate, never overwrite.
            with csv_path.open(encoding="utf-8", newline="") as handle:
                existing = {r["symbol"] for r in csv.DictReader(handle)}
            if existing != {m.symbol for m in snapshot.members}:
                raise ValueError(
                    f"cross-source disagreement for {key[0]} {key[1]}: "
                    f"only in first={sorted(existing - {m.symbol for m in snapshot.members})[:5]} "
                    f"only in second={sorted({m.symbol for m in snapshot.members} - existing)[:5]}")
            manifest = PROCESSED / f"{key[0]}_{key[1]}.json"
            imported.append({
                "index_code": snapshot.index_code,
                "as_of": snapshot.as_of.isoformat(),
                "security_rows": len(snapshot.members),
                "duplicate_of_existing_catalog": key in seen,
                "duplicate_cross_source": True,
                "source_path": _reference_path(source_path),
                "source_url": source_url,
                "source_sha256": snapshot.metadata.get("source_sha256"),
                "source_member": snapshot.metadata.get("source_member"),
                "snapshot_csv": _reference_path(csv_path),
                "snapshot_manifest": _reference_path(manifest),
                "importer": importer,
            })
            return
        csv_path, manifest = write_snapshot(snapshot, PROCESSED)
        imported.append({
            "index_code": snapshot.index_code,
            "as_of": snapshot.as_of.isoformat(),
            "security_rows": len(snapshot.members),
            "duplicate_of_existing_catalog": key in seen,
            "source_path": _reference_path(source_path),
            "source_url": source_url, "source_sha256": snapshot.metadata.get(
                "source_sha256"), "source_member": snapshot.metadata.get(
                    "source_member"),
            "snapshot_csv": _reference_path(csv_path),
            "snapshot_manifest": _reference_path(manifest),
            "importer": importer,
        })

    # Report type 2: MCWB ZIPs -> Nifty 50 snapshots.
    for zip_path in sorted((RAW / "zips_type2").glob("*.zip")):
        provenance = json.loads(zip_path.with_name(
            zip_path.name + ".provenance.json").read_text())
        with zipfile.ZipFile(zip_path) as archive:
            member = select_mcwb_member(archive.namelist())
        if member is None:
            failed.append({"source": zip_path.name,
                           "reason": "no_nifty50_mcwb_member"})
            continue
        importer = "index_membership.import_mcwb"
        try:
            snapshot = import_mcwb(zip_path, member, provenance)
        except (ValueError, UnicodeDecodeError, KeyError,
                zipfile.BadZipFile) as primary_error:
            try:
                snapshot = import_mcwb_doc(zip_path, member, provenance)
                importer = "nifty_doc.import_mcwb_doc"
            except (ValueError, UnicodeDecodeError, KeyError,
                    zipfile.BadZipFile):
                failed.append({"source": zip_path.name, "member": member,
                               "importer": importer,
                               "reason": f"{type(primary_error).__name__}: "
                                         f"{primary_error}"})
                continue
        try:
            record(snapshot, zip_path, provenance["url"], importer)
        except ValueError as exc:
            failed.append({"source": zip_path.name, "member": member,
                           "importer": importer,
                           "reason": f"{type(exc).__name__}: {exc}"})

    # Report type 4: member PDFs -> Nifty 50 / Nifty 500 snapshots.
    for zip_path in sorted((RAW / "zips_type4").glob("*.zip")):
        provenance = json.loads(zip_path.with_name(
            zip_path.name + ".provenance.json").read_text())
        with zipfile.ZipFile(zip_path) as archive:
            members = {code: select_type4_member(archive.namelist(), code)
                       for code in ("NIFTY50", "NIFTY500")}
            csv_members = {
                code: select_type4_csv_member(archive.namelist(), code)
                for code in ("NIFTY50", "NIFTY500")}
        for index_code, member in csv_members.items():
            if member is None:
                continue
            with zipfile.ZipFile(zip_path) as archive:
                csv_bytes = archive.read(member)
            try:
                snapshot = import_type4_csv(csv_bytes, provenance, member)
            except (ValueError, KeyError) as exc:
                failed.append({"source": zip_path.name, "member": member,
                               "importer": "nifty_doc.import_type4_csv",
                               "reason": f"{type(exc).__name__}: {exc}"})
                continue
            try:
                record(snapshot, zip_path, provenance["url"],
                       "nifty_doc.import_type4_csv")
            except ValueError as exc:
                failed.append({"source": zip_path.name, "member": member,
                               "importer": "nifty_doc.import_type4_csv",
                               "reason": f"{type(exc).__name__}: {exc}"})
        for index_code, member in members.items():
            if member is None:
                continue
            stem = f"{zip_path.stem}__{Path(member).stem}"
            pdf_path = RAW / "pdfs" / (stem + ".pdf")
            txt_path = RAW / "pdfs" / (stem + ".txt")
            if not txt_path.exists():
                failed.append({"source": zip_path.name, "member": member,
                               "reason": "missing_extracted_text_run_texts"})
                continue
            text = txt_path.read_text(encoding="utf-8")
            pages = text.rstrip("\n").split("\n\f\n")
            pdf_bytes = pdf_path.read_bytes()
            zip_provenance = dict(provenance)
            importer = "index_membership.import_pdf_pages"
            try:
                if index_code == "NIFTY500":
                    snapshot = import_pdf_pages(
                        pages, "NIFTY500", pdf_bytes, zip_provenance, member,
                        txt_path.name)
                else:
                    snapshot = import_pdf_pages_50(
                        pages, pdf_bytes, zip_provenance, member, txt_path.name)
                    importer = "nifty_doc.import_pdf_pages_50"
            except (ValueError, KeyError) as primary_error:
                try:
                    snapshot = import_pdf_pages_generic(
                        pages, index_code, pdf_bytes, zip_provenance, member,
                        txt_path.name)
                    importer = "nifty_doc.import_pdf_pages_generic"
                except (ValueError, KeyError):
                    failed.append({"source": zip_path.name, "member": member,
                                   "importer": importer,
                                   "reason": f"{type(primary_error).__name__}"
                                             f": {primary_error}"})
                    continue
            try:
                record(snapshot, zip_path, provenance["url"], importer)
            except ValueError as exc:
                failed.append({"source": zip_path.name, "member": member,
                               "importer": importer,
                               "reason": f"{type(exc).__name__}: {exc}"})
    PROCESSED.mkdir(parents=True, exist_ok=True)
    extension = {"generated_at_utc": now_utc(),
                 "policy": ("New snapshots only; duplicates of the existing "
                            "26-October catalog are flagged, never silently "
                            "merged. Exact-date membership only."),
                 "imported": imported, "failed": failed}
    (PROCESSED / "catalog_extension.json").write_text(
        json.dumps(extension, indent=2) + "\n", encoding="utf-8")
    print(f"imported {len(imported)} snapshots, failed {len(failed)}")
    for item in failed[:30]:
        print("  FAILED:", item)
    return 0


# --------------------------------------------------------------------------
# Press releases: inventory, download, changelog parsing
# --------------------------------------------------------------------------

def extract_press_links(html: str) -> list[str]:
    """All unique ind_prs*.pdf filenames linked from the archive page."""
    return sorted(set(PRESS_LINK_RE.findall(html)))


def parse_press_filename_date(filename: str) -> date | None:
    match = PRESS_FILE_RE.fullmatch(filename)
    if not match:
        return None
    digits = match.group(1)
    try:
        return date(int(digits[4:8]), int(digits[2:4]), int(digits[0:2]))
    except ValueError:
        return None


def cmd_press_inventory(args) -> int:
    html = fetch_url(PRESS_PAGE_URL, timeout=120).decode("utf-8", "ignore")
    (RAW / "press_releases.html").write_text(html, encoding="utf-8")
    (RAW / "press_releases.html.provenance.json").write_text(json.dumps(
        {"file": "press_releases.html", "url": PRESS_PAGE_URL,
         "retrieved_at_utc": now_utc(), "sha256": sha256(
             html.encode("utf-8")), "bytes": len(html.encode("utf-8"))},
        indent=2) + "\n", encoding="utf-8")
    links = extract_press_links(html)
    inventory = []
    for filename in links:
        announced = parse_press_filename_date(filename)
        inventory.append({
            "filename": filename, "url": PRESS_BASE + filename,
            "announcement_date": announced.isoformat() if announced else None,
            "filename_date_parsed": announced is not None})
    (RAW / "press_release_inventory.json").write_text(
        json.dumps({"generated_at_utc": now_utc(), "count": len(inventory),
                    "items": inventory}, indent=2) + "\n", encoding="utf-8")
    print(f"press-release inventory: {len(inventory)} links")
    return 0


def cmd_fetch_press(args) -> int:
    inventory = json.loads(
        (RAW / "press_release_inventory.json").read_text())["items"]
    press_dir = RAW / "press"
    todo = [item for item in inventory
            if not (press_dir / item["filename"]).with_name(
                item["filename"] + ".provenance.json").exists()]
    print(f"downloading {len(todo)} press-release PDFs")
    done, failed = 0, []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download, item["url"],
                               press_dir / item["filename"]): item["filename"]
                   for item in todo}
        for future in as_completed(futures):
            try:
                future.result()
                done += 1
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{futures[future]}: {exc}")
    print(f"downloaded {done}, failed {len(failed)}")
    for item in failed[:20]:
        print("  FAILED:", item)
    return 0


# --- Changelog parsing ----------------------------------------------------

INDEX_NAME_MAP = {
    "s&p cnx nifty": "NIFTY50", "cnx nifty": "NIFTY50",
    "nifty 50": "NIFTY50", "nifty fifty": "NIFTY50",
    "s&p cnx 500": "NIFTY500", "cnx 500": "NIFTY500",
    "nifty 500": "NIFTY500", "nifty five hundred": "NIFTY500",
}
OTHER_INDEXES = ("nifty next 50", "cnx nifty junior", "nifty junior",
                 "cnx midcap 200", "cnx 100", "nifty 100", "cnx 200",
                 "nifty 200", "nifty midcap", "nifty smallcap",
                 "nifty psu", "nifty mnc", "cnx pse", "cnx mnc",
                 "cnx infra", "cnx realty", "cnx auto", "cnx media",
                 "cnx metal", "nifty bank", "shariah", "sme")
SECTION_RE = re.compile(
    r"^\s*(?:\(\d+\)|[a-z]\)|\d+\))\s*(.+?)\s*$", re.I)
DATE_TOKEN = (r"(?:January|February|March|April|May|June|July|August|"
              r"September|October|November|December)\s+\d{1,2},?\s+\d{4}")
EFFECTIVE_RES = [
    re.compile(r"w\.e\.f\.?\s+(" + DATE_TOKEN + r")", re.I),
    re.compile(r"effective\s+from\s+(" + DATE_TOKEN + r")", re.I),
    re.compile(r"become\s+effective\s+(?:from\s+)?(" + DATE_TOKEN + r")", re.I),
    re.compile(r"changes.*?effective\s+(?:from\s+)?(" + DATE_TOKEN + r")",
               re.I),
    re.compile(r"effective\s+(" + DATE_TOKEN + r")\s*\(close of", re.I),
]
EXCEPTION_RE = re.compile(
    r"with respect to\s+(.+?)[,\.]\s+changes.*?effected\s+from\s+("
    + DATE_TOKEN + r")", re.I | re.S)
EXCLUDE_RE = re.compile(r"exclud|remov|delet|dropp", re.I)
INCLUDE_RE = re.compile(r"includ|admit|addition|enter", re.I)
ROW_RE = re.compile(r"^\s*(\d+)[\.\)]?\s+(.+?)\s*$")
SYMBOL_SUFFIX_RE = re.compile(r"\s+([A-Z][A-Z0-9&.\-]{1,11})\s*$")
NUMERIC_TAIL_RE = re.compile(r"\s+\d+\.\d+\s+[\d,\.\s]+$")
HEADER_ROW_RE = re.compile(
    r"^(Sr\.? ?No\.?|Company Name|Symbol|S\.? ?No\.?)\b", re.I)


def clean_company_name(raw: str) -> tuple[str, str | None]:
    """Split a table row into (company name, symbol-or-None).

    Conservative: only strips a trailing impact-cost/market-cap numeric
    tail and an unambiguous uppercase symbol column. Industry text is
    left intact when it cannot be separated without guessing.
    """
    company = raw.split(" - ")[0].strip()
    symbol = None
    suffix = SYMBOL_SUFFIX_RE.search(company)
    if suffix and " " in company[:suffix.start()]:
        candidate = suffix.group(1)
        if SYMBOL_RE.fullmatch(candidate):
            company = company[:suffix.start()].strip()
            symbol = candidate
    company = NUMERIC_TAIL_RE.sub("", company).strip()
    return company, symbol


def parse_date_token(token: str) -> date | None:
    cleaned = " ".join(token.replace(",", "").split())
    for fmt in ("%B %d %Y", "%d %B %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def find_effective_date(text: str) -> tuple[date | None, str | None]:
    """First explicit effective date in the notice, with verbatim evidence.

    PDF extraction sometimes splits words across lines ('effectiv e').
    A cleaned copy is tried second; evidence quotes the matched span.
    """
    cleaned = (text.replace("effectiv e", "effective")
               .replace("becom e", "become")
               .replace("chang e", "change"))
    for candidate in (text, cleaned):
        for pattern in EFFECTIVE_RES:
            match = pattern.search(candidate)
            if match:
                parsed = parse_date_token(match.group(1))
                if parsed:
                    return parsed, " ".join(match.group(0).split())
    return None, None


def find_exceptions(text: str) -> dict[str, date]:
    """Per-company effective-date exceptions (e.g. Wockhardt 2000-01-12)."""
    exceptions: dict[str, date] = {}
    for match in EXCEPTION_RE.finditer(text):
        parsed = parse_date_token(match.group(2))
        if parsed:
            exceptions[" ".join(match.group(1).split())] = parsed
    return exceptions


def normalize_heading(name: str) -> str:
    """Lowercase + drop a trailing ' index' so 'CNX Nifty Index' matches."""
    low = " ".join(name.lower().split())
    low = re.sub(r"\s+index\.?$", "", low)
    return low.strip()


def split_sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Split notice text into (heading, body-lines) at numbered headings."""
    sections: list[tuple[str, list[str]]] = []
    heading, body = None, []
    for line in lines:
        match = SECTION_RE.match(line)
        name = match.group(1) if match else ""
        low = normalize_heading(name)
        is_index = (low in INDEX_NAME_MAP
                    or any(low.startswith(o) for o in OTHER_INDEXES)
                    or low.startswith(("replacements", "revision",
                                       "changes in", "inclusion",
                                       "exclusion")))
        if match and is_index and len(name) <= 60:
            if heading is not None:
                sections.append((heading, body))
            heading, body = name, []
        elif heading is not None:
            body.append(line)
    if heading is not None:
        sections.append((heading, body))
    return sections


def parse_action_rows(body: list[str], action: str) -> list[dict]:
    """Numbered company rows under one exclusion/inclusion subsection."""
    rows, in_block = [], False
    for line in body:
        row_match = ROW_RE.match(line)
        if row_match and in_block:
            raw = row_match.group(2).strip()
            if HEADER_ROW_RE.match(raw):
                continue
            company, symbol = clean_company_name(raw)
            if company and len(company) <= 90 and not company.startswith(
                    ("Number", "Companies", "Net Change", "Sr.", "S. No")):
                rows.append({"action": action, "company_name": company,
                             "symbol": symbol, "verbatim_row": raw})
            continue
        low = line.lower()
        if action == "exclusion" and EXCLUDE_RE.search(low) and (
                "being excluded" in low or low.strip() in ("exclusion",)
                or "following companies are being excluded" in low
                or "companies being excluded" in low
                or "excluded:" in low or "excluded from" in low):
            in_block = True
            continue
        if action == "inclusion" and INCLUDE_RE.search(low) and (
                "being included" in low or low.strip() in ("inclusion",)
                or "following companies are being included" in low
                or "companies being included" in low
                or "included:" in low or "included in" in low):
            in_block = True
            continue
        if in_block and (EXCLUDE_RE.search(low) or INCLUDE_RE.search(low)) \
                and "being" in low:
            in_block = False
        if in_block and re.match(r"\s*(The changes in|With the above|Note:|"
                                 r"Stocks that|The above|These|This|"
                                 r"Changes in the|Number Market)", line):
            in_block = False
    return rows


def parse_changelog_text(text: str, source: dict) -> dict:
    """Parse one notice text into notice-level + event records.

    Never invents events: rows are only emitted under an explicit index
    section and exclusion/inclusion subsection, with verbatim evidence.
    """
    flat = " ".join(text.split())
    effective, evidence = find_effective_date(flat)
    exceptions = find_exceptions(flat)
    sections = split_sections(text.splitlines())
    events: list[dict] = []
    target_sections = 0
    for heading, body in sections:
        low = normalize_heading(heading)
        index_code = INDEX_NAME_MAP.get(low)
        if index_code is None:
            continue
        target_sections += 1
        for action in ("exclusion", "inclusion"):
            for row in parse_action_rows(body, action):
                eff, eff_evidence = effective, evidence
                if row["company_name"] in exceptions:
                    eff = exceptions[row["company_name"]]
                    eff_evidence = ("per-company exception in notice "
                                    "(verbatim in source text)")
                if eff is None:
                    eff_evidence = ("no explicit effective date found in "
                                    "notice text; announcement date must "
                                    "not be assumed")
                has_symbol = row["symbol"] is not None
                events.append({
                    "event_kind": "index_change",
                    "index_code": index_code,
                    "index_name_in_notice": heading,
                    "action": action,
                    "company_name": row["company_name"],
                    "symbol": row["symbol"],
                    "effective_date": eff.isoformat() if eff else None,
                    "effective_date_evidence": eff_evidence,
                    "announcement_date": source.get("announcement_date"),
                    "source_file": source.get("filename"),
                    "source_sha256": source.get("source_sha256"),
                    "verbatim_row": row["verbatim_row"],
                    "status": ("parsed_with_symbol_unreconciled" if has_symbol
                               else "parsed_unreconciled_name_only"),
                    "applied": False,
                    "reconciliation_checkpoint_required": True,
                })
    if target_sections == 0:
        notice_status = "not_target_index_notice"
    elif events:
        notice_status = "parsed_events_unreconciled"
    else:
        notice_status = "needs_manual_extraction"
    return {"source_file": source.get("filename"),
            "announcement_date": source.get("announcement_date"),
            "notice_status": notice_status,
            "target_sections": target_sections,
            "effective_date": effective.isoformat() if effective else None,
            "events": events}


def validate_event(event: dict) -> None:
    """Fail closed on any event that could be applied without a checkpoint."""
    if event.get("applied") is not False:
        raise ValueError("events must never be marked applied")
    if event.get("reconciliation_checkpoint_required") is not True:
        raise ValueError("reconciliation checkpoint is required")
    if event.get("index_code") not in ("NIFTY50", "NIFTY500"):
        raise ValueError("unsupported index_code")
    if event.get("action") not in ("inclusion", "exclusion"):
        raise ValueError("unsupported action")
    if not event.get("company_name"):
        raise ValueError("company_name is required")
    if event.get("status") not in ("parsed_unreconciled_name_only",
                                   "parsed_with_symbol_unreconciled"):
        raise ValueError("unsupported event status")
    if event.get("effective_date") is None and not event.get(
            "effective_date_evidence"):
        # Effective date may be unknown only if the parser said so explicitly.
        raise ValueError("effective date needs evidence")
    if event.get("effective_date"):
        date.fromisoformat(event["effective_date"])


def cmd_changelog(args) -> int:
    press_dir = RAW / "press"
    inventory = json.loads(
        (RAW / "press_release_inventory.json").read_text())["items"]
    notices, events = [], []
    for item in inventory:
        txt_path = press_dir / (Path(item["filename"]).stem + ".txt")
        pdf_path = press_dir / item["filename"]
        if not txt_path.exists():
            notices.append({"source_file": item["filename"],
                            "announcement_date": item["announcement_date"],
                            "notice_status": "missing_extracted_text",
                            "events": []})
            continue
        provenance = {}
        prov_path = pdf_path.with_name(pdf_path.name + ".provenance.json")
        if prov_path.exists():
            provenance = json.loads(prov_path.read_text())
        source = {"filename": item["filename"],
                  "announcement_date": item["announcement_date"],
                  "source_sha256": provenance.get("sha256")}
        parsed = parse_changelog_text(txt_path.read_text(encoding="utf-8"),
                                      source)
        for event in parsed["events"]:
            validate_event(event)
            events.append(event)
        notices.append({k: parsed[k] for k in
                        ("source_file", "announcement_date", "notice_status",
                         "target_sections", "effective_date")})
        notices[-1]["event_count"] = len(parsed["events"])
    events.sort(key=lambda e: (e["effective_date"] or "9999-12-31",
                               e["index_code"], e["company_name"]))
    with (PROCESSED / "changelog.jsonl").open("w", encoding="utf-8") as out:
        for event in events:
            out.write(json.dumps(event) + "\n")
    (PROCESSED / "notices.json").write_text(
        json.dumps({"generated_at_utc": now_utc(), "notices": notices},
                   indent=2) + "\n", encoding="utf-8")
    print(f"notices {len(notices)}, changelog events {len(events)} "
          f"(all unapplied)")
    return 0


# --------------------------------------------------------------------------
# Reconciliation / coverage report
# --------------------------------------------------------------------------

def load_all_snapshots() -> list[tuple[str, date, frozenset[str]]]:
    """Existing 26 + new nifty_doc snapshots as (index, as_of, symbols)."""
    out: list[tuple[str, date, frozenset[str]]] = []
    existing = json.loads(EXISTING_CATALOG.read_text())
    for row in existing:
        with (ROOT / row["snapshot_csv"]).open(encoding="utf-8",
                                               newline="") as handle:
            symbols = frozenset(r["symbol"] for r in csv.DictReader(handle))
        out.append((row["index_code"], date.fromisoformat(row["as_of"]),
                    symbols))
    for csv_path in sorted(PROCESSED.glob("NIFTY*.csv")):
        stem = csv_path.stem
        index_code, as_of_text = stem.split("_", 1)
        with csv_path.open(encoding="utf-8", newline="") as handle:
            symbols = frozenset(r["symbol"] for r in csv.DictReader(handle))
        out.append((index_code, date.fromisoformat(as_of_text), symbols))
    # Deduplicate identical (index, as_of) pairs (duplicate catalog rows).
    unique: dict[tuple[str, date], frozenset[str]] = {}
    for index_code, as_of, symbols in out:
        unique.setdefault((index_code, as_of), symbols)
        if unique[(index_code, as_of)] != symbols:
            raise ValueError(f"conflicting snapshot symbols for "
                             f"{index_code} {as_of}")
    return [(i, d, s) for (i, d), s in unique.items()]


def diff_snapshots(snapshots) -> list[dict]:
    """Consecutive-snapshot membership deltas per index."""
    deltas = []
    for index_code in ("NIFTY50", "NIFTY500"):
        series = sorted((d, s) for (i, d, s) in snapshots
                        if i == index_code)
        for (prev_date, prev), (curr_date, curr) in zip(series, series[1:]):
            deltas.append({"index_code": index_code,
                           "from": prev_date.isoformat(),
                           "to": curr_date.isoformat(),
                           "added_symbols": sorted(curr - prev),
                           "removed_symbols": sorted(prev - curr)})
    return deltas


def months_between(first: date, last: date) -> list[str]:
    return [month_year_label(y, m) for (y, m) in
            iter_months((first.year, first.month), (last.year, last.month))]


def compute_gaps(snapshots, start: date, end: date) -> dict:
    """Months with no snapshot, per index, inside [start, end]."""
    gaps: dict[str, dict] = {}
    for index_code in ("NIFTY50", "NIFTY500"):
        have = {(d.year, d.month) for (i, d, _s) in snapshots
                if i == index_code}
        missing = [f"{y}-{m:02d}" for (y, m) in
                   iter_months((start.year, start.month),
                               (end.year, end.month))
                   if (y, m) not in have]
        dates = sorted(d for (i, d, _s) in snapshots if i == index_code)
        gaps[index_code] = {
            "first_snapshot": dates[0].isoformat() if dates else None,
            "last_snapshot": dates[-1].isoformat() if dates else None,
            "snapshot_months": len(have),
            "missing_months": missing,
            "missing_month_count": len(missing),
        }
    return gaps


def cmd_report(args) -> int:
    probes = json.loads((RAW / "archive_probes.json").read_text())
    extension = json.loads(
        (PROCESSED / "catalog_extension.json").read_text())
    notices = json.loads((PROCESSED / "notices.json").read_text())["notices"]
    events = [json.loads(line) for line in
              (PROCESSED / "changelog.jsonl").read_text().splitlines()
              if line.strip()]
    snapshots = load_all_snapshots()
    deltas = diff_snapshots(snapshots)
    # Checkpoint reconciliation: which snapshot deltas are explained by
    # changelog events with symbols inside the (prev, curr] window?
    explained, unexplained, name_only = 0, 0, 0
    for delta in deltas:
        window_events = [e for e in events
                         if e["index_code"] == delta["index_code"]
                         and e["effective_date"]
                         and delta["from"] < e["effective_date"]
                         <= delta["to"]]
        delta_symbols = set(delta["added_symbols"]) | set(delta["removed_symbols"])
        matched = {e["symbol"] for e in window_events if e["symbol"]}
        if delta_symbols and delta_symbols <= matched:
            explained += 1
        elif any(e["symbol"] is None for e in window_events) and delta_symbols:
            name_only += 1
        else:
            unexplained += 1
    gaps = compute_gaps(snapshots, date(2000, 1, 1), date(2026, 10, 31))
    by_type = {}
    for t in ("2", "4"):
        hits = [r for r in probes if r["report_type"] == t and r["success"]]
        by_type[t] = {"probes": sum(1 for r in probes
                                    if r["report_type"] == t),
                      "available": len(hits),
                      "first_available": (hits[0]["month_year"] if hits
                                          else None),
                      "last_available": (hits[-1]["month_year"] if hits
                                         else None)}
    coverage = {
        "generated_at_utc": now_utc(),
        "policy": ("Exact-date snapshots + unapplied changelog only. "
                   "No fill, no fabrication, no automatic application. "
                   "A changelog event becomes usable only after a manual "
                   "reconciliation checkpoint (symbol mapping, "
                   "effective-date exceptions, reversals)."),
        "archive_probes": by_type,
        "new_snapshots_imported": len(extension["imported"]),
        "new_snapshot_import_failures": len(extension["failed"]),
        "total_snapshots_including_existing_26": len(snapshots),
        "press_release_notices": len(notices),
        "press_notices_with_parsed_events": sum(
            1 for n in notices if n.get("event_count", 0) > 0),
        "press_notices_needing_manual_extraction": sum(
            1 for n in notices if n["notice_status"]
            == "needs_manual_extraction"),
        "press_notices_not_target_index": sum(
            1 for n in notices if n["notice_status"]
            == "not_target_index_notice"),
        "changelog_events_unapplied": len(events),
        "changelog_events_with_symbol": sum(1 for e in events
                                           if e["symbol"]),
        "changelog_events_name_only": sum(1 for e in events
                                         if not e["symbol"]),
        "changelog_events_without_effective_date": sum(
            1 for e in events if not e["effective_date"]),
        "snapshot_deltas_total": len(deltas),
        "snapshot_deltas_explained_by_changelog": explained,
        "snapshot_deltas_name_only_window": name_only,
        "snapshot_deltas_without_changelog_match": unexplained,
        "gaps": gaps,
        "earliest_baseline": {code: gaps[code]["first_snapshot"]
                              for code in gaps},
        "bridge_2000_2008": ("No complete public baseline exists before "
                             "2008-01 for Nifty 50 (whichever month the "
                             "earliest MCWB snapshot lands on) and none "
                             "before the first type-4 month for Nifty 500/"
                             "CNX 500. 2000 press releases give changes "
                             "against an unknown baseline; orphan events "
                             "cannot be anchored without purchased or "
                             "otherwise sourced history."),
        "effort_to_full": {
            "missing_snapshot_months": {
                code: gaps[code]["missing_month_count"] for code in gaps},
            "manual_work_remaining": [
                "Resolve every needs_manual_extraction notice by hand.",
                "Map every name-only changelog event to a stable symbol "
                "identity (renames, mergers, DVR/DUMMY classes).",
                "Reconcile each changelog event against bracketing "
                "monthly snapshots; investigate unexplained deltas "
                "(off-cycle changes, corrections, reversals).",
                "Obtain or confirm a pre-2008 complete baseline for "
                "Nifty 50 and a pre-type-4 baseline for Nifty 500 "
                "(purchase/vendor or other official source); public "
                "archives do not contain one.",
                "Establish publication/availability timestamps before "
                "using any month-end snapshot as a point-in-time signal.",
            ],
        },
    }
    (data_root() / "reports" / "nifty-doc-coverage.json").write_text(
        json.dumps(coverage, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: coverage[k] for k in (
        "new_snapshots_imported", "total_snapshots_including_existing_26",
        "press_release_notices", "changelog_events_unapplied",
        "snapshot_deltas_total", "snapshot_deltas_explained_by_changelog",
        "earliest_baseline")}, indent=2))
    print("gaps:",
          {c: g["missing_month_count"] for c, g in gaps.items()})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--from-year", type=int, default=2000)
    p.add_argument("--from-month", type=int, default=1)
    p.add_argument("--to-year", type=int, default=2026)
    p.add_argument("--to-month", type=int, default=10)
    p.add_argument("--workers", type=int, default=12)
    p.set_defaults(func=cmd_probe)
    for name, func in (("fetch-monthly", cmd_fetch_monthly),
                       ("fetch-press", cmd_fetch_press)):
        p = sub.add_parser(name)
        p.add_argument("--workers", type=int, default=12)
        p.set_defaults(func=func)
    for name, func in (("texts", cmd_texts), ("snapshots", cmd_snapshots),
                       ("press-inventory", cmd_press_inventory),
                       ("changelog", cmd_changelog), ("report", cmd_report)):
        p = sub.add_parser(name)
        p.set_defaults(func=func)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
