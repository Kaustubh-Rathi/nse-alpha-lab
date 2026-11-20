"""Dated constituent snapshots, never a price-based or forward-filled universe.

Only the standard library is needed. Source metadata is an auditable attestation,
not proof of authenticity: validate the document's date and completeness before
creating a manifest. Neither 50 nor 500 is a hard security-count constraint (DVRs
and temporary corporate-action constituents can increase the security count).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_CUTOFF = date(2026, 11, 7)
INDEX_CODES = {"NIFTY50", "NIFTY500"}
OFFICIAL_HOSTS = {"niftyindices.com", "www.niftyindices.com", "nsearchives.nseindia.com"}
SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9&.\-]*\Z")
MONTHS = {name.lower(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"), 1)}
# Official MCWB titles changed over time: S&P CNX Nifty, CNX Nifty, Nifty 50.
MCWB_TITLE = re.compile(r"(?:nifty\s*50|s&p\s+cnx\s+nifty|cnx\s+nifty)\s+index", re.I)
# The closing date is the report's own footnote, never the filename or retrieval.
MCWB_DATE = re.compile(
    r"Last day of trading was\s+(\d{1,2})[-\s]+([A-Za-z]+)[-\s]+(\d{4})", re.I)
PDF_HEADER = re.compile(
    r"Constituents of (?:Nifty|NIFTY)\s+500\s+([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})")
PDF_ROW = re.compile(r"^([A-Z0-9][A-Z0-9&.\-]*)\s+.*?\s+\d+\.\d{2}\s+\d")


@dataclass(frozen=True)
class Member:
    symbol: str
    company_name: str = ""
    isin: str = ""


@dataclass(frozen=True)
class Snapshot:
    index_code: str
    as_of: date
    members: tuple[Member, ...]
    metadata: dict


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def validate(snapshot: Snapshot, cutoff: date = DEFAULT_CUTOFF) -> None:
    """Fail closed on missing dates/provenance, duplicate identities and counts."""
    meta = snapshot.metadata
    if snapshot.index_code not in INDEX_CODES:
        raise ValueError("unsupported index_code")
    if snapshot.as_of > cutoff:
        raise ValueError("snapshot is after cutoff")
    if meta.get("membership_kind") != "dated_official_snapshot":
        raise ValueError("only dated official snapshots are accepted")
    if meta.get("as_of_basis") != "document_explicit_date" or not meta.get("as_of_evidence"):
        raise ValueError("explicit document date evidence is required")
    if meta.get("as_of") != snapshot.as_of.isoformat():
        raise ValueError("as_of differs from metadata")
    if meta.get("index_code") != snapshot.index_code:
        raise ValueError("index_code differs from metadata")
    url = urlparse(meta.get("source_url", ""))
    if url.scheme != "https" or url.hostname not in OFFICIAL_HOSTS or url.username or url.password:
        raise ValueError("source_url must be an official HTTPS source")
    try:
        retrieved = datetime.fromisoformat(meta["retrieved_at_utc"].replace("Z", "+00:00"))
        if retrieved.utcoffset() is None:
            raise ValueError("retrieved_at_utc needs a timezone")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid retrieved_at_utc") from exc
    if retrieved.date() < snapshot.as_of:
        raise ValueError("retrieval precedes snapshot date")
    if not re.fullmatch(r"[0-9a-f]{64}", meta.get("source_sha256", "")):
        raise ValueError("source_sha256 is required")
    count = meta.get("expected_security_count")
    if type(count) is not int or count <= 0 or count != len(snapshot.members):
        raise ValueError("security count differs from explicitly declared count")
    symbols, isins = set(), set()
    for member in snapshot.members:
        if not SYMBOL.fullmatch(member.symbol):
            raise ValueError(f"invalid symbol: {member.symbol!r}")
        if member.symbol in symbols:
            raise ValueError("duplicate symbol")
        symbols.add(member.symbol)
        if member.isin:
            if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", member.isin):
                raise ValueError("invalid ISIN")
            if member.isin in isins:
                raise ValueError("duplicate ISIN")
            isins.add(member.isin)


def import_mcwb(source: Path, member_name: str, provenance: dict,
                cutoff: date = DEFAULT_CUTOFF) -> Snapshot:
    """Import an official Nifty 50 monthly ZIP using its embedded trading date.

    The monthly report's stated last trading day is preserved verbatim, even on
    a Sunday (e.g. the 2016 Diwali session). ZIP member filenames are not dates.
    The count is derived from a complete contiguous numbered table, not 50.
    """
    content = source.read_bytes()
    if provenance.get("sha256") != sha256(content):
        raise ValueError("source digest mismatch")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        report = archive.read(member_name)
    text = report.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    title = " ".join(rows[0]).lower() if rows else ""
    if not MCWB_TITLE.search(title):
        raise ValueError("not a Nifty 50 MCWB report")
    matches = list(MCWB_DATE.finditer(text))
    if len(matches) != 1:
        raise ValueError("one explicit last trading date is required")
    match = matches[0]
    day, month, year = match.groups()
    month_number = next((number for name, number in MONTHS.items()
                         if month.lower() in (name, name[:3])), None)
    if month_number is None:
        raise ValueError("unknown report month")
    as_of = date(int(year), month_number, int(day))
    header = next((row for row in rows
                   if any(cell.strip() == "Security Symbol" for cell in row)), None)
    if header is None:
        raise ValueError("missing constituent table header")
    symbol_column = [cell.strip() for cell in header].index("Security Symbol")
    name_column = next((index for index, cell in enumerate(header)
                        if cell.strip() == "Security Name"), None)
    numbered = [row for row in rows if row and row[0].strip().isdigit()]
    if [int(row[0]) for row in numbered] != list(range(1, len(numbered) + 1)):
        raise ValueError("non-contiguous constituent table")
    if any(len(row) <= symbol_column for row in numbered):
        raise ValueError("incomplete constituent row")
    if name_column is not None and any(len(row) <= name_column for row in numbered):
        raise ValueError("incomplete constituent row")
    members = tuple(
        Member(row[symbol_column].strip(),
               row[name_column].strip() if name_column is not None else "")
        for row in numbered)
    meta = {
        "index_code": "NIFTY50", "as_of": as_of.isoformat(),
        "membership_kind": "dated_official_snapshot",
        "as_of_basis": "document_explicit_date",
        "as_of_evidence": " ".join(match.group(0).split()),
        "expected_security_count": len(members), "source_url": provenance["url"],
        "source_sha256": sha256(content), "source_member": member_name,
        "source_member_sha256": sha256(report),
        "retrieved_at_utc": provenance["retrieved_at_utc"],
        "extraction": "Complete numbered MCWB table; no inferred ISINs or membership intervals.",
    }
    snapshot = Snapshot("NIFTY50", as_of, members, meta)
    validate(snapshot, cutoff)
    return snapshot


def join_pdf_pages(pages: list[str]) -> str:
    """Canonical retained text for per-page PDF extraction."""
    return "\n\f\n".join(page.rstrip("\n") for page in pages) + "\n"


def import_pdf_pages(pages: list[str], index_code: str, pdf_bytes: bytes,
                     provenance: dict, source_member: str,
                     source_text_name: str = "",
                     cutoff: date = DEFAULT_CUTOFF) -> Snapshot:
    """Import an official Nifty 500 PDF from retained per-page extracted text.

    Extraction is verified separately; this stdlib importer only accepts a
    document-stated index name and date, and requires a regular page table.
    The security count is derived from the complete table, not fixed at 500.
    """
    if index_code not in INDEX_CODES:
        raise ValueError("unsupported index_code")
    text = join_pdf_pages(pages)
    headers = list(PDF_HEADER.finditer(text))
    if not headers:
        raise ValueError("one explicit document-stated date is required")
    header = headers[0]
    if len({" ".join(match.group(0).split()) for match in headers}) != 1:
        raise ValueError("conflicting document-stated dates")
    month, day, year = header.groups()
    month_number = MONTHS.get(month.lower())
    if month_number is None:
        raise ValueError("unknown report month")
    as_of = date(int(year), month_number, int(day))
    counts, symbols = [], []
    for page in pages:
        page_symbols = [match.group(1) for match in
                        (PDF_ROW.match(line) for line in page.splitlines()) if match]
        if not page_symbols:
            raise ValueError("empty constituent page")
        counts.append(len(page_symbols))
        symbols.extend(page_symbols)
    if len(counts) > 1 and (len(set(counts[:-1])) != 1 or counts[-1] > counts[0]):
        raise ValueError("irregular constituent page table")
    members = tuple(Member(symbol) for symbol in symbols)
    meta = {
        "index_code": index_code, "as_of": as_of.isoformat(),
        "membership_kind": "dated_official_snapshot",
        "as_of_basis": "document_explicit_date",
        "as_of_evidence": " ".join(header.group(0).split()),
        "expected_security_count": len(members),
        "source_url": provenance["url"], "source_sha256": provenance["sha256"],
        "source_member": source_member,
        "source_member_sha256": sha256(pdf_bytes),
        "retrieved_at_utc": provenance["retrieved_at_utc"],
        "extraction": ("Symbol column only from every PDF page; company name and "
                       "ISIN left empty, not inferred."),
        "symbol_row_regex": PDF_ROW.pattern,
        "rows_per_page": counts, "source_pdf_pages": len(pages),
        "source_text_sha256": sha256(text.encode("utf-8")),
    }
    if source_text_name:
        meta["source_text_name"] = source_text_name
    snapshot = Snapshot(index_code, as_of, members, meta)
    validate(snapshot, cutoff)
    return snapshot


def write_snapshot(snapshot: Snapshot, directory: Path,
                   cutoff: date = DEFAULT_CUTOFF) -> tuple[Path, Path]:
    """Write a new canonical CSV and manifest without overwriting existing work."""
    validate(snapshot, cutoff)
    stem = f"{snapshot.index_code}_{snapshot.as_of.isoformat()}"
    csv_path, manifest_path = directory / (stem + ".csv"), directory / (stem + ".json")
    if csv_path.exists() or manifest_path.exists():
        raise FileExistsError(stem)
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("symbol", "company_name", "isin"))
    writer.writerows((m.symbol, m.company_name, m.isin) for m in snapshot.members)
    content = output.getvalue().encode("utf-8")
    metadata = dict(snapshot.metadata, snapshot_sha256=sha256(content))
    directory.mkdir(parents=True, exist_ok=True)
    with csv_path.open("xb") as target:
        target.write(content)
    with manifest_path.open("x", encoding="utf-8") as target:
        json.dump(metadata, target, indent=2)
        target.write("\n")
    return csv_path, manifest_path


def load_snapshot(csv_path: Path, manifest_path: Path, source_path: Path,
                  cutoff: date = DEFAULT_CUTOFF) -> Snapshot:
    """Validate stored CSV and original official artifact, including ZIP member."""
    meta = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = source_path.read_bytes()
    if sha256(source) != meta.get("source_sha256"):
        raise ValueError("source digest mismatch")
    if meta.get("source_member"):
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            member = archive.read(meta["source_member"])
        if sha256(member) != meta.get("source_member_sha256"):
            raise ValueError("source member digest mismatch")
    if meta.get("source_text_name"):
        retained_text = (source_path.parent / meta["source_text_name"]).read_bytes()
        if sha256(retained_text) != meta.get("source_text_sha256"):
            raise ValueError("source text digest mismatch")
    content = csv_path.read_bytes()
    if sha256(content) != meta.get("snapshot_sha256"):
        raise ValueError("snapshot digest mismatch")
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    if reader.fieldnames != ["symbol", "company_name", "isin"]:
        raise ValueError("unexpected snapshot columns")
    members = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError("malformed snapshot row")
        members.append(Member(**row))
    snapshot = Snapshot(meta["index_code"], date.fromisoformat(meta["as_of"]), tuple(members), meta)
    validate(snapshot, cutoff)
    return snapshot


def members_on(snapshots: list[Snapshot], index_code: str, day: date) -> tuple[Member, ...]:
    """Exact-date lookup only: gaps are unknown, never forward/backward filled."""
    matches = [snapshot for snapshot in snapshots
               if snapshot.index_code == index_code and snapshot.as_of == day]
    if not matches:
        raise LookupError(f"unknown membership for {index_code} on {day}")
    if len(matches) != 1:
        raise ValueError("ambiguous duplicate snapshots")
    validate(matches[0], cutoff=day)
    return matches[0].members
