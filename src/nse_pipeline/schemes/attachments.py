"""Offline, bounded ZIP/PDF attachment adapter; text is evidence, not verification.

No network or dependency installation is performed. Optional pypdf/PyMuPDF
provide text extraction; explicit local OCR additionally needs PyMuPDF and
Tesseract. Archive members are read in memory, never extracted to their paths.
See docs/scheme-attachments.md for the manifest contract and resource limits.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import io
import json
import math
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import zipfile
import zlib
from collections import Counter
from pathlib import Path, PureWindowsPath
from typing import NamedTuple

VERSION = "scheme_attachments_v1"

SIGNOFF_SCHEMA_VERSION = "canonical_manual_signoff_v1"
SIGNOFF_FIELDS = (
    "event_id", "filing_url", "page_ref", "snippet_verified_on_page", "verified_by",
    "ratio_num", "ratio_den", "cash_per_share", "ratio_direction",
    "transferor_symbol", "transferor_isin", "transferee_symbol", "transferee_isin",
    "successor_symbol", "successor_isin", "official_ex_date", "listing_date",
)
SIGNOFF_RATIO_DIRECTION = "successor_shares_per_holder_share"


def validate_signoff_row(row) -> list:
    """Pure fail-closed signoff validation (no network, no promotion of terms).

    Returns error codes; empty means the row alone is well-formed. Event-level
    consistency (symbol, ex-date, ratio) is enforced by canonical.validate_manual_signoff.
    """
    from urllib.parse import urlparse as _urlparse
    from datetime import date as _date

    if not isinstance(row, dict):
        return ["manual_signoff_not_an_object"]
    errors = []
    if row.get("_pending") is True:
        errors.append("manual_signoff_pending_template")
    if not row.get("event_id"):
        errors.append("manual_signoff_event_id_missing")
    try:
        parsed = _urlparse(str(row.get("filing_url") or ""))
        host = parsed.hostname or ""
        official = (parsed.scheme == "https"
                    and (host == "nseindia.com" or host.endswith(".nseindia.com"))
                    and parsed.path.lower().endswith(".pdf"))
    except ValueError:
        official = False
    if not official:
        errors.append("manual_signoff_filing_url_not_official_pdf")
    if not isinstance(row.get("page_ref"), str) or not re.fullmatch(r"p\.\d+", row["page_ref"]):
        errors.append("manual_signoff_page_ref_missing")
    if row.get("snippet_verified_on_page") is not True:
        errors.append("manual_signoff_snippet_not_verified_on_page")
    if not isinstance(row.get("verified_by"), str) or not row["verified_by"].strip():
        errors.append("manual_signoff_verified_by_missing")
    num, den, cash = (row.get(f) for f in ("ratio_num", "ratio_den", "cash_per_share"))
    ratio_ok = ((num is None and den is None and type(cash) in (int, float)
                 and math.isfinite(cash) and cash >= 0) if cash is not None
                else (type(num) is int and type(den) is int and num > 0 and den > 0))
    if not ratio_ok:
        errors.append("manual_signoff_ratio_invalid")
    if row.get("ratio_direction") != SIGNOFF_RATIO_DIRECTION:
        errors.append("manual_signoff_ratio_direction_invalid")
    for field in ("transferor_symbol", "transferee_symbol", "successor_symbol"):
        if not isinstance(row.get(field), str) or not re.fullmatch(r"[A-Z0-9.\-]{1,30}", row[field]):
            errors.append(f"manual_signoff_{field}_missing_or_invalid")
    for field in ("transferor_isin", "transferee_isin", "successor_isin"):
        if not isinstance(row.get(field), str) or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", row[field]):
            errors.append(f"manual_signoff_{field}_missing_or_invalid")
    for field in ("official_ex_date", "listing_date"):
        try:
            ok = _date.fromisoformat(row.get(field)).isoformat() == row.get(field)
        except (TypeError, ValueError):
            ok = False
        if not ok:
            errors.append(f"manual_signoff_{field}_invalid")
    return errors


def signoff_template(event_id: str, symbol: str, ex_date: str, filing_url: str | None = None) -> dict:
    """Pending reviewer template; intentionally incomplete (fails validation until filled)."""
    return {"event_id": event_id, "filing_url": filing_url, "page_ref": None,
            "snippet_verified_on_page": None, "verified_by": None,
            "ratio_num": None, "ratio_den": None, "cash_per_share": None,
            "ratio_direction": SIGNOFF_RATIO_DIRECTION,
            "transferor_symbol": symbol, "transferor_isin": None,
            "transferee_symbol": None, "transferee_isin": None,
            "successor_symbol": None, "successor_isin": None,
            "official_ex_date": ex_date, "listing_date": None}


class Limits(NamedTuple):
    max_attachment_bytes: int = 32 * 1024 * 1024
    max_members: int = 200
    max_member_bytes: int = 32 * 1024 * 1024
    max_total_bytes: int = 128 * 1024 * 1024
    max_compression_ratio: float = 100.0
    max_pages: int = 300
    max_text_chars: int = 2_000_000
    max_ocr_pixels: int = 16_000_000


class AttachmentError(ValueError):
    """Fail-closed rejection with a stable machine-readable ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _validate_limits(limits: Limits) -> None:
    if any(not math.isfinite(value) or value <= 0 for value in limits):
        raise ValueError("All resource limits must be positive and finite")
    if any(type(value) is not int for field, value in zip(limits._fields, limits)
           if field != "max_compression_ratio"):
        raise ValueError("Byte, member, page, character and pixel limits must be integers")


def capabilities() -> dict:
    """Report local optional dependencies without importing or installing them."""
    return {
        "pypdf": importlib.util.find_spec("pypdf") is not None,
        "pymupdf": importlib.util.find_spec("pymupdf") is not None,
        "tesseract": shutil.which("tesseract"),
    }


def _safe_member(info: zipfile.ZipInfo) -> None:
    # orig_filename retains NULs which ZipInfo.filename silently truncates.
    name = info.orig_filename
    normalized = name.replace("\\", "/")
    parts = normalized.rstrip("/").split("/")
    if (not name or "\x00" in name or normalized.startswith("/")
            or PureWindowsPath(name).drive or ":" in name
            or any(part in ("", ".", "..") for part in parts)
            or any(part.endswith((".", " ")) for part in parts)
            or any(ord(char) < 32 for char in name)
            or any(re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)",
                            part, re.I) for part in parts)):
        raise AttachmentError("unsafe_member_path", repr(name))
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise AttachmentError("unsafe_member_type", repr(name))
    if info.external_attr & 0x400:  # Windows FILE_ATTRIBUTE_REPARSE_POINT
        raise AttachmentError("unsafe_member_type", repr(name))
    if info.flag_bits & (1 | 64):
        raise AttachmentError("encrypted_archive", repr(name))
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise AttachmentError("unsupported_compression", repr(name))


def _zip_directory_bound(content: bytes, limits: Limits) -> None:
    """Reject huge declared member counts before ZipFile allocates ZipInfos.

    Deliberately accept only single-disk, non-ZIP64 archives with no trailing
    data. Small historical corporate attachments do not require ZIP64.
    """
    offset = content.rfind(b"PK\x05\x06", max(0, len(content) - 65557))
    if offset < 0 or offset + 22 > len(content):
        raise AttachmentError("corrupt_archive", "Missing ZIP end record")
    _, disk, directory_disk, disk_count, count, size, start, comment = (
        struct.unpack_from("<4s4H2LH", content, offset))
    if offset + 22 + comment != len(content):
        raise AttachmentError("corrupt_archive", "Invalid ZIP end/comment length")
    if disk or directory_disk or disk_count != count:
        raise AttachmentError("unsupported_archive", "Multi-disk ZIP is unsupported")
    if count == 65535 or size == 0xFFFFFFFF or start == 0xFFFFFFFF:
        raise AttachmentError("unsupported_archive", "ZIP64 is unsupported")
    if count > limits.max_members:
        raise AttachmentError("member_count_limit", f"{count} ZIP members")
    if start + size != offset:
        raise AttachmentError("corrupt_archive", "Invalid central directory bounds")
    position = start
    actual_count = 0
    while position < offset:
        if position + 46 > offset or content[position:position + 4] != b"PK\x01\x02":
            raise AttachmentError("corrupt_archive", "Invalid central directory entry")
        lengths = struct.unpack_from("<3H", content, position + 28)
        position += 46 + sum(lengths)
        actual_count += 1
        if actual_count > limits.max_members:
            raise AttachmentError("member_count_limit", str(actual_count))
    if position != offset or actual_count != count:
        raise AttachmentError("corrupt_archive", "Central directory count/size mismatch")


def pdf_members(content: bytes, limits: Limits = Limits()) -> list[dict]:
    """Validate a direct PDF or ZIP; return PDF bytes with member identities.

    The complete ZIP is checked (including CRC of ignored members) before
    returning any PDFs. Nested archives are rejected, not recursively expanded.
    Repeated names are preserved with their original zero-based member index;
    sorting by (name, index) makes output deterministic. No filesystem writes.
    """
    _validate_limits(limits)
    if len(content) > limits.max_attachment_bytes:
        raise AttachmentError("attachment_size_limit", str(len(content)))
    if content.startswith(b"%PDF-"):
        if len(content) > min(limits.max_member_bytes, limits.max_total_bytes):
            raise AttachmentError("member_size_limit", "Direct PDF exceeds limit")
        return [{"archive_member": None, "member_index": None, "content": content}]
    if not content.startswith(b"PK"):
        raise AttachmentError("unsupported_attachment", "Expected %PDF- or ZIP bytes")
    _zip_directory_bound(content, limits)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > limits.max_members:
                raise AttachmentError("member_count_limit", str(len(infos)))
            total = 0
            for info in infos:
                _safe_member(info)
                offset = info.header_offset
                if (offset < 0 or offset + 30 > len(content)
                        or content[offset:offset + 4] != b"PK\x03\x04"):
                    raise AttachmentError("corrupt_archive", "Invalid local header")
                if struct.unpack_from("<H", content, offset + 6)[0] & (1 | 64):
                    raise AttachmentError("encrypted_archive", info.filename)
                if info.file_size > limits.max_member_bytes:
                    raise AttachmentError("member_size_limit", info.filename)
                total += info.file_size
                if total > limits.max_total_bytes:
                    raise AttachmentError("total_size_limit", str(total))
                if (info.file_size > 0 and (info.compress_size == 0
                        or info.file_size / info.compress_size > limits.max_compression_ratio)):
                    raise AttachmentError("compression_ratio_limit", info.filename)
            pdfs = []
            actual_total = 0
            for index, info in sorted(enumerate(infos), key=lambda item: (item[1].filename, item[0])):
                # A bounded read also catches forged size metadata; ZipFile checks
                # CRC/local headers and overlapped members as the stream is read.
                with archive.open(info) as stream:
                    payload = stream.read(min(limits.max_member_bytes,
                                              limits.max_total_bytes - actual_total) + 1)
                actual_total += len(payload)
                if len(payload) > limits.max_member_bytes or actual_total > limits.max_total_bytes:
                    raise AttachmentError("actual_size_limit", info.filename)
                if len(payload) != info.file_size:
                    raise AttachmentError("corrupt_archive", "Member size mismatch")
                if info.is_dir():
                    if payload:
                        raise AttachmentError("corrupt_archive", "Nonempty directory member")
                    continue
                if payload.startswith(b"PK") or info.filename.lower().endswith(".zip"):
                    raise AttachmentError("nested_archive", info.filename)
                if payload.startswith(b"%PDF-"):
                    pdfs.append({"archive_member": info.filename,
                                 "member_index": index, "content": payload})
                elif info.filename.lower().endswith(".pdf"):
                    raise AttachmentError("invalid_pdf_signature", info.filename)
            return pdfs
    except AttachmentError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, EOFError,
            OSError, NotImplementedError, zlib.error, ValueError) as error:
        raise AttachmentError("corrupt_archive", str(error)) from error


def _require_ocr(deps: dict) -> None:
    missing = [name for name in ("pymupdf", "tesseract") if not deps.get(name)]
    if missing:
        raise AttachmentError("ocr_unavailable", "Missing local OCR dependencies: " + ", ".join(missing))


def _ocr_page(page, executable: str, language: str, dpi: int,
              timeout: float, limits: Limits) -> str:
    pixels = math.ceil(page.rect.width * dpi / 72) * math.ceil(page.rect.height * dpi / 72)
    if not math.isfinite(pixels) or pixels <= 0 or pixels > limits.max_ocr_pixels:
        raise AttachmentError("ocr_pixel_limit", str(pixels))
    with tempfile.TemporaryDirectory(prefix="scheme-ocr-") as directory:
        image = Path(directory) / "page.png"
        page.get_pixmap(dpi=dpi, alpha=False).save(str(image))
        try:
            result = subprocess.run(
                [executable, str(image), "stdout", "-l", language],
                capture_output=True, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise AttachmentError("ocr_timeout", f"Tesseract exceeded {timeout}s") from error
        except OSError as error:
            raise AttachmentError("ocr_unavailable", str(error)) from error
    if result.returncode:
        raise AttachmentError("ocr_failed", result.stderr.decode("utf-8", "replace")[:1000])
    return result.stdout.decode("utf-8", "replace")


def extract_pdf_pages(content: bytes, *, limits: Limits = Limits(), ocr: bool = False,
                      ocr_language: str = "eng", ocr_dpi: int = 150,
                      ocr_timeout: float = 30.0) -> list[dict]:
    """Extract each page; OCR only blank native-text pages on explicit opt-in.

    OCR text is labelled separately, is unverified, and never replaces a native
    text layer. Missing requested dependencies are errors, not silent fallbacks.
    Page/text/image limits bound ordinary jobs, not hostile PDF parser CPU/RAM;
    run untrusted PDFs in an OS sandbox for hard process resource isolation.
    """
    _validate_limits(limits)
    if not content.startswith(b"%PDF-"):
        raise AttachmentError("invalid_pdf_signature", "Expected %PDF- bytes")
    if len(content) > limits.max_member_bytes:
        raise AttachmentError("member_size_limit", "PDF exceeds byte limit")
    if (not re.fullmatch(r"[A-Za-z0-9_+\-]{1,80}", ocr_language)
            or not 72 <= ocr_dpi <= 300 or not 0 < ocr_timeout <= 300):
        raise ValueError("Invalid OCR language, DPI (72..300), or timeout (0..300)")
    deps = capabilities()
    if ocr:
        _require_ocr(deps)
    document = None
    try:
        if deps["pypdf"]:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted:
                raise AttachmentError("encrypted_pdf", "Encrypted PDF is not accepted")
            native_pages = reader.pages
            backend = "pypdf"
        elif deps["pymupdf"]:
            import pymupdf
            document = pymupdf.open(stream=content, filetype="pdf")
            if document.is_encrypted:
                raise AttachmentError("encrypted_pdf", "Encrypted PDF is not accepted")
            native_pages = document
            backend = "pymupdf"
        else:
            raise AttachmentError("pdf_text_extract_unavailable", "Install optional pypdf or PyMuPDF explicitly")
        count = len(native_pages)
        if count > limits.max_pages:
            raise AttachmentError("page_count_limit", str(count))
        if not count:
            raise AttachmentError("invalid_pdf", "PDF has no pages")
        if ocr and document is None:
            import pymupdf
            document = pymupdf.open(stream=content, filetype="pdf")
            if document.is_encrypted or len(document) != count:
                raise AttachmentError("invalid_pdf", "OCR renderer page count/encryption mismatch")
        pages = []
        chars = 0
        for index in range(count):
            native = (native_pages[index].extract_text() if backend == "pypdf"
                      else native_pages[index].get_text()) or ""
            ocr_text = None
            if ocr and not native.strip():
                ocr_text = _ocr_page(document[index], deps["tesseract"], ocr_language,
                                     ocr_dpi, ocr_timeout, limits)
            chars += len(native) + len(ocr_text or "")
            if chars > limits.max_text_chars:
                raise AttachmentError("text_size_limit", str(chars))
            pages.append({"page": index + 1, "page_ref": f"p.{index + 1}",
                          "text": ocr_text if ocr_text is not None else native,
                          "native_text": native, "ocr_text": ocr_text,
                          "method": "tesseract_local" if ocr_text is not None else backend,
                          "ocr_performed": ocr_text is not None})
        return pages
    except AttachmentError:
        raise
    except Exception as error:
        raise AttachmentError("pdf_extract_failed", f"{type(error).__name__}: {error}") from error
    finally:
        if document is not None:
            document.close()


def adapt_attachment(content: bytes, *, source_url: str, source_path: str | None = None,
                     expected_sha256: str | None = None, metadata: dict | None = None,
                     limits: Limits = Limits(), ocr: bool = False,
                     ocr_language: str = "eng", ocr_dpi: int = 150,
                     ocr_timeout: float = 30.0, text_cache: dict | None = None) -> dict:
    """Return cited documents from cached bytes; never verify/promote terms.

    ``text_cache`` is caller-owned, in-memory, SHA256/settings-keyed and contains
    only raw page extraction (never source provenance). Duplicate PDF content
    is parsed once while every member/source keeps its own page citations.
    """
    if not isinstance(source_url, str) or not source_url.strip():
        raise AttachmentError("source_url_missing", "A source URL is required for cited evidence")
    digest = sha256(content)
    if expected_sha256 is not None:
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", expected_sha256):
            raise AttachmentError("invalid_manifest_hash", "Expected a 64-character SHA256")
        if expected_sha256.lower() != digest:
            raise AttachmentError("hash_mismatch", "Cached attachment differs from manifest SHA256")
    members = pdf_members(content, limits)
    if ocr:
        _require_ocr(capabilities())
    cache = text_cache if text_cache is not None else {}
    result = {"pipeline_version": VERSION, "source_url": source_url,
              "source_path": source_path, "attachment_sha256": digest,
              "attachment_bytes": len(content), "manifest_hash_verified": expected_sha256 is not None,
              "source_metadata": copy.deepcopy(metadata or {}), "verified": False,
              "requires_independent_review": True, "documents": []}
    seen = {}
    total_pages = 0
    total_chars = 0
    for member in members:
        pdf_digest = sha256(member["content"])
        key = (VERSION, pdf_digest, limits, ocr, ocr_language, ocr_dpi, ocr_timeout)
        cached = key in cache
        if not cached:
            cache[key] = extract_pdf_pages(member["content"], limits=limits, ocr=ocr,
                                           ocr_language=ocr_language, ocr_dpi=ocr_dpi,
                                           ocr_timeout=ocr_timeout)
        total_pages += len(cache[key])
        total_chars += sum(len(page["native_text"]) + len(page["ocr_text"] or "") for page in cache[key])
        if total_pages > limits.max_pages:
            raise AttachmentError("page_count_limit", f"Attachment total: {total_pages}")
        if total_chars > limits.max_text_chars:
            raise AttachmentError("text_size_limit", f"Attachment total: {total_chars}")
        provenance = {"source_url": source_url, "source_path": source_path,
                      "attachment_sha256": digest, "pdf_sha256": pdf_digest,
                      "archive_member": member["archive_member"],
                      "member_index": member["member_index"]}
        pages = []
        for page in copy.deepcopy(cache[key]):
            page.update(provenance)
            page["verified"] = False
            page["requires_independent_review"] = True
            pages.append(page)
        document_id = f"{digest}:{member['member_index']}:{pdf_digest}"
        result["documents"].append({
            **provenance, "document_id": document_id,
            "duplicate_of": seen.get(pdf_digest),
            "status": "text_extracted" if any(p["text"].strip() for p in pages) else "scanned_no_text",
            "pages": pages, "verified": False,
        })
        seen.setdefault(pdf_digest, document_id)
    result["status"] = "extracted" if members else "no_pdf_members"
    return result


def _read_bounded(path: Path, maximum: int) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(maximum + 1)
    if len(content) > maximum:
        raise AttachmentError("input_size_limit", str(path))
    return content


def load_manifest(path: Path, *, root: Path, limit: int = 50) -> list[dict]:
    """Read URL-keyed cache manifests, JSON lists/attachments, or JSONL.

    Paths are root-relative (not manifest-relative), matching pdf_extract's
    existing manifests. All local input paths must remain inside explicit root.
    Missing paths are retained as per-record failures; no URL is fetched.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("Manifest limit must be 1..1000")
    path, root = Path(path), Path(root).resolve()
    text = _read_bounded(path, 8 * 1024 * 1024).decode("utf-8-sig")
    if path.suffix.lower() == ".jsonl":
        payload = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        payload = json.loads(text)
    if isinstance(payload, dict) and isinstance(payload.get("attachments"), list):
        rows = payload["attachments"]
    elif isinstance(payload, dict):
        rows = []
        for url, record in sorted(payload.items()):
            if not isinstance(record, dict):
                raise ValueError("Manifest records must be objects")
            rows.append({**record, "source_url": url})
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError("Expected manifest list, URL map, or attachments object")
    jobs = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            raise ValueError("Manifest records must be objects")
        url = next((row[field] for field in ("attchmntFile", "attachment_url", "pdf_url", "url", "source_url")
                    if row.get(field)), "")
        local = row.get("cache_path") or row.get("path")
        job = {"source_url": str(url), "metadata": row, "sha256": row.get("sha256"), "path": None}
        if local:
            candidate = (root / str(local)).resolve()
            if not candidate.is_relative_to(root):
                job["error"] = "cache_path_outside_root"
            else:
                job["path"] = candidate
        else:
            job["error"] = "cache_path_missing"
        jobs.append(job)
    return jobs


def write_artifact(out: Path, value: dict) -> Path:
    """Immutable, content-addressed JSON; never overwrite an existing file."""
    payload = (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    path = Path(out) / f"{sha256(payload)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
    except FileExistsError:
        if _read_bounded(path, len(payload)) != payload:
            raise AttachmentError("artifact_collision", str(path))
    return path


def scan_manifest(manifest: Path, *, root: Path, out: Path | None = None,
                  limit: int = 50, limits: Limits = Limits(), ocr: bool = False,
                  ocr_language: str = "eng", ocr_dpi: int = 150,
                  ocr_timeout: float = 30.0) -> dict:
    """Bounded, offline scan. Record explicit failures; originals stay untouched."""
    _validate_limits(limits)
    jobs = load_manifest(manifest, root=root, limit=limit)
    records = []
    cache = {}
    for job in jobs:
        record = {"source_url": job["source_url"], "source_path": str(job["path"]) if job["path"] else None}
        try:
            if job.get("error"):
                raise AttachmentError(job["error"], "Manifest must identify a cached file within root")
            content = _read_bounded(job["path"], limits.max_attachment_bytes)
            result = adapt_attachment(
                content, source_url=job["source_url"], source_path=str(job["path"]),
                expected_sha256=job["sha256"], metadata=job["metadata"], limits=limits,
                ocr=ocr, ocr_language=ocr_language, ocr_dpi=ocr_dpi,
                ocr_timeout=ocr_timeout, text_cache=cache,
            )
            record.update(status=result["status"], attachment_sha256=result["attachment_sha256"],
                          documents=len(result["documents"]),
                          pages=sum(len(doc["pages"]) for doc in result["documents"]),
                          text_pages=sum(bool(page["text"].strip()) for doc in result["documents"] for page in doc["pages"]),
                          ocr_pages=sum(page["ocr_performed"] for doc in result["documents"] for page in doc["pages"]))
            if out is not None:
                record["artifact"] = str(write_artifact(out, result))
        except (AttachmentError, OSError, ValueError) as error:
            record.update(status="failed", error_code=getattr(error, "code", type(error).__name__),
                          error=str(error))
        # Bound deduplication memory after successes AND rejected attachments.
        # Eviction changes performance, never cited output or source identity.
        if len(cache) > 16 or sum(len(page["text"]) + len(page["native_text"])
                                  for pages in cache.values() for page in pages) > 8_000_000:
            cache.clear()
        records.append(record)
    return {"pipeline_version": VERSION, "offline": True, "ocr_requested": ocr,
            "capabilities": capabilities(), "jobs": len(records),
            "statuses": dict(Counter(record["status"] for record in records)),
            "documents": sum(record.get("documents", 0) for record in records),
            "manual_signoffs": {"schema_version": SIGNOFF_SCHEMA_VERSION,
                               "fields": list(SIGNOFF_FIELDS), "validated": 0,
                               "note": "text extraction never validates terms; signoffs are validated by canonical"},
            "pages": sum(record.get("pages", 0) for record in records),
            "text_pages": sum(record.get("text_pages", 0) for record in records),
            "ocr_pages": sum(record.get("ocr_pages", 0) for record in records),
            "verified_terms": 0, "records": records}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Explicit boundary for local cache paths")
    parser.add_argument("--out", type=Path, help="Write immutable per-attachment cited JSON (otherwise summary only)")
    parser.add_argument("--limit", type=int, default=50, help="Maximum manifest records, 1..1000")
    parser.add_argument("--ocr", action="store_true", help="Explicit local OCR of blank pages; never verifies terms")
    parser.add_argument("--ocr-language", default="eng")
    parser.add_argument("--ocr-dpi", type=int, default=150)
    parser.add_argument("--ocr-timeout", type=float, default=30.0)
    parser.add_argument("--max-members", type=int, default=Limits().max_members)
    parser.add_argument("--max-attachment-bytes", type=int, default=Limits().max_attachment_bytes)
    parser.add_argument("--max-member-bytes", type=int, default=Limits().max_member_bytes)
    parser.add_argument("--max-total-bytes", type=int, default=Limits().max_total_bytes)
    parser.add_argument("--max-compression-ratio", type=float, default=Limits().max_compression_ratio)
    parser.add_argument("--max-pages", type=int, default=Limits().max_pages)
    args = parser.parse_args(argv)
    limits = Limits(max_attachment_bytes=args.max_attachment_bytes, max_members=args.max_members,
                    max_member_bytes=args.max_member_bytes, max_total_bytes=args.max_total_bytes,
                    max_compression_ratio=args.max_compression_ratio, max_pages=args.max_pages)
    try:
        summary = scan_manifest(args.manifest, root=args.root, out=args.out, limit=args.limit,
                                limits=limits, ocr=args.ocr, ocr_language=args.ocr_language,
                                ocr_dpi=args.ocr_dpi, ocr_timeout=args.ocr_timeout)
    except (AttachmentError, OSError, ValueError) as error:
        print(json.dumps({"status": "failed", "error_code": getattr(error, "code", type(error).__name__),
                          "error": str(error)}, indent=2))
        return 2
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if summary["statuses"].get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
