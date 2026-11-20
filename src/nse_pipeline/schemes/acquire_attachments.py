"""Explicit, bounded acquisition bridge for official NSE PDF AND ZIP filings.

Offline by default. No cookies, identity changes, retries or verification of
scheme terms. Every HTTP hop shares one sequential request budget. Any HTTP
error (especially 401/403/429) stops the whole network batch. Source bytes are
published atomically under content hashes, never over existing source names.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

_SPEC = importlib.util.spec_from_file_location(
    "scheme_attachment_adapter", Path(__file__).with_name("attachments.py"))
attachments = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(attachments)

VERSION = "acquire_scheme_attachments_v1"
SIGNOFF_SCHEMA_VERSION = "canonical_manual_signoff_v1"
SIGNOFF_RATIO_DIRECTION = "successor_shares_per_holder_share"
API = "https://www.nseindia.com/api/corporate-announcements"
ARCHIVE_HOSTS = frozenset({"nsearchives.nseindia.com", "archives.nseindia.com"})
MAX_BYTES = 32 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
SCHEME = re.compile(r"\b(?:scheme|amalgamation|merger|demerger|restructur\w*|NCLT)\b", re.I)
USER_AGENT = "SchemeAttachmentResearch/1.0 (bounded personal research; stop-on-denial)"


class AcquisitionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class BatchStopped(AcquisitionError):
    pass


class Response(NamedTuple):
    status: int
    body: bytes = b""
    location: str | None = None


def validate_url(url: str, kind: str = "attachment") -> str:
    """Exact official HTTPS allowlist, no credentials/nonstandard ports."""
    if not isinstance(url, str) or not url or any(ord(c) <= 32 for c in url) or "\\" in url:
        raise AcquisitionError("unsafe_url", repr(url))
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise AcquisitionError("unsafe_url", str(error)) from error
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or port not in (None, 443) or parsed.fragment):
        raise AcquisitionError("unsafe_url", url)
    if kind == "api":
        valid = parsed.hostname == "www.nseindia.com" and parsed.path == "/api/corporate-announcements"
    elif kind == "attachment":
        valid = (parsed.hostname in ARCHIVE_HOSTS
                 and parsed.path.lower().endswith((".pdf", ".zip")))
    else:
        raise ValueError("Unknown request kind")
    if not valid:
        raise AcquisitionError("url_not_allowlisted", url)
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_fetch(url: str, timeout: float, max_bytes: int) -> Response:
    """One HTTP hop only; caller validates host, counts and handles redirects."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity",
    })
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        try:
            return Response(error.code, location=error.headers.get("Location"))
        finally:
            error.close()
    with response:
        if response.status != 200:
            return Response(response.status, location=response.headers.get("Location"))
        encoding = response.headers.get("Content-Encoding", "identity").lower()
        if encoding not in ("identity", ""):
            raise AcquisitionError("unsupported_http_encoding", encoding)
        length = response.headers.get("Content-Length")
        try:
            declared = int(length) if length is not None else None
        except ValueError as error:
            raise AcquisitionError("invalid_content_length", str(length)) from error
        if declared is not None and (declared < 0 or declared > max_bytes):
            raise AcquisitionError("response_size_limit", str(declared))
        content = response.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise AcquisitionError("response_size_limit", str(len(content)))
        if declared is not None and len(content) != declared:
            raise AcquisitionError("truncated_response", f"{len(content)} / {declared}")
        return Response(200, content)


class NetworkBatch:
    """Single sequential budget/limiter shared by discovery and downloads.

    Injected ``fetcher(url, timeout, max_bytes) -> Response`` must do exactly
    one hop, never follow redirects or retry. No fetcher is called offline.
    """

    def __init__(self, *, fetch: bool = False, max_requests: int = 10,
                 min_interval: float = 1.0, timeout: float = 30.0,
                 fetcher=None, clock=None, sleeper=None):
        if type(max_requests) is not int or not 1 <= max_requests <= 100:
            raise ValueError("max_requests must be 1..100")
        if not math.isfinite(min_interval) or min_interval < 1:
            raise ValueError("min_interval must be finite and >= 1 second")
        if not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("timeout must be (0, 120] seconds")
        self.enabled = fetch
        self.max_requests = max_requests
        self.min_interval = min_interval
        self.timeout = timeout
        self.fetcher = fetcher or http_fetch
        self.clock = clock or time.monotonic
        self.sleeper = sleeper or time.sleep
        self.last = None
        self.requests = 0
        self.log = []
        self.stopped = None

    def stop(self, code: str, message: str, **fields) -> None:
        if self.stopped is None:
            self.stopped = {"code": code, "message": message, **fields}

    def request(self, url: str, kind: str = "attachment") -> bytes:
        if not self.enabled:
            raise BatchStopped("offline", "Network requires explicit fetch=True / --fetch")
        if self.stopped is not None:
            raise BatchStopped(self.stopped["code"], self.stopped["message"])
        redirects = 0
        while True:
            try:
                validate_url(url, kind)
            except AcquisitionError as error:
                self.stop(error.code, str(error), url=url)
                raise BatchStopped(error.code, str(error)) from error
            if self.requests >= self.max_requests:
                self.stop("request_budget_exhausted", "No requests remain", url=url)
                raise BatchStopped("request_budget_exhausted", "No requests remain")
            if self.last is not None:
                wait = self.min_interval - (self.clock() - self.last)
                if wait > 0:
                    self.sleeper(wait)
            self.last = self.clock()
            self.requests += 1
            record = {"request": self.requests, "url": url, "kind": kind}
            self.log.append(record)
            try:
                response = self.fetcher(url, self.timeout, MAX_JSON_BYTES if kind == "api" else MAX_BYTES)
                # Supporting HTTPError from a mock/custom one-hop transport also
                # preserves the mandatory denial stop policy.
            except urllib.error.HTTPError as error:
                response = Response(error.code, location=error.headers.get("Location") if error.headers else None)
                error.close()
            except Exception as error:
                code = getattr(error, "code", "transport_error")
                record.update(status="failed", error_code=code, error=str(error))
                self.stop(code, str(error), url=url)
                raise BatchStopped(code, str(error)) from error
            record["http_status"] = response.status
            if response.status in (401, 403, 429):
                record["status"] = "stopped_denied_or_rate_limited"
                self.stop("http_denied_or_rate_limited", f"HTTP {response.status}",
                          http_status=response.status, url=url)
                raise BatchStopped("http_denied_or_rate_limited", f"HTTP {response.status}")
            if response.status in (301, 302, 303, 307, 308):
                record["status"] = "redirect"
                if not response.location or redirects >= 2:
                    self.stop("redirect_limit", "Missing redirect location or more than two redirects", url=url)
                    raise BatchStopped("redirect_limit", self.stopped["message"])
                target = urllib.parse.urljoin(url, response.location)
                record["redirect_to"] = target
                # Validation happens before the next hop is counted or sent.
                url = target
                redirects += 1
                continue
            if response.status != 200:
                record["status"] = "stopped_http_error"
                self.stop("http_error", f"HTTP {response.status}", http_status=response.status, url=url)
                raise BatchStopped("http_error", f"HTTP {response.status}")
            maximum = MAX_JSON_BYTES if kind == "api" else MAX_BYTES
            if not isinstance(response.body, bytes) or len(response.body) > maximum:
                record["status"] = "stopped_size_or_type_error"
                self.stop("response_size_or_type_limit", "Invalid/oversized response", url=url)
                raise BatchStopped("response_size_or_type_limit", "Invalid/oversized response")
            record.update(status="received", bytes=len(response.body))
            return response.body


def _bounded_json(path: Path):
    with Path(path).open("rb") as stream:
        content = stream.read(MAX_JSON_BYTES + 1)
    if len(content) > MAX_JSON_BYTES:
        raise AcquisitionError("json_size_limit", str(path))
    return json.loads(content.decode("utf-8-sig"))


def _source_url(row: dict) -> str:
    return next((row[key] for key in ("attchmntFile", "attachment_url", "pdf_url", "url", "source_url")
                 if row.get(key)), "")


def load_sources(path: Path) -> list[dict]:
    """Explicit JSON URLs, attachment objects, or existing URL-keyed manifests."""
    payload = _bounded_json(path)
    if isinstance(payload, dict) and isinstance(payload.get("attachments"), list):
        rows = payload["attachments"]
    elif isinstance(payload, dict):
        rows = [{**value, "source_url": url} for url, value in payload.items() if isinstance(value, dict)]
        if len(rows) != len(payload):
            raise ValueError("Source map values must be objects")
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError("Expected source URL list, attachment list/object, or URL map")
    candidates = []
    for row in rows:
        if isinstance(row, str):
            row = {"source_url": row}
        if not isinstance(row, dict):
            raise ValueError("Source records must be URLs or objects")
        url = validate_url(_source_url(row))
        candidates.append({**row, "source_url": url})
    return candidates


def candidates_from_rows(rows: list[dict], symbol: str | None = None) -> tuple[list[dict], list[dict]]:
    """Keep scheme-like .pdf AND .zip rows, preserving filing metadata."""
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Expected announcement list of objects")
    candidates, rejected = [], []
    for row in rows:
        if symbol and str(row.get("symbol", "")).strip().upper() != symbol:
            continue
        text = " ".join(str(row.get(field) or "") for field in ("desc", "attchmntText", "attchmntFile"))
        if not SCHEME.search(text):
            continue
        url = _source_url(row)
        try:
            validate_url(url)
        except AcquisitionError as error:
            rejected.append({"source_url": url, "error_code": error.code})
            continue
        date = row.get("announcement_date")
        if not date:
            for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%B-%Y %H:%M:%S", "%d-%b-%Y", "%Y-%m-%d"):
                try:
                    date = datetime.strptime(str(row.get("an_dt") or ""), fmt).date().isoformat()
                    break
                except ValueError:
                    continue
        candidates.append({**row, "source_url": url, "announcement_date": date})
    return candidates, rejected


def manual_signoff_templates(candidates: list[dict], event_id: str, symbol: str,
                               ex_date: str) -> list[dict]:
    """Pending signoff templates from discovered candidates (incomplete by design).

    Each template carries the filing URL for reviewer convenience but leaves ratio,
    successor, ISINs and listing date null so an unfinished row fails canonical
    validation instead of silently promoting an event.
    """
    templates = []
    seen = set()
    for row in candidates or []:
        if not isinstance(row, dict):
            continue
        url = _source_url(row)
        if url in seen:
            continue
        try:
            validate_url(url)
        except AcquisitionError:
            continue
        seen.add(url)
        templates.append({
            "event_id": event_id, "filing_url": url, "page_ref": None,
            "snippet_verified_on_page": None, "verified_by": None,
            "ratio_num": None, "ratio_den": None, "cash_per_share": None,
            "ratio_direction": SIGNOFF_RATIO_DIRECTION,
            "transferor_symbol": symbol, "transferor_isin": None,
            "transferee_symbol": None, "transferee_isin": None,
            "successor_symbol": None, "successor_isin": None,
            "official_ex_date": ex_date, "listing_date": None,
            "_pending": True, "_schema": SIGNOFF_SCHEMA_VERSION,
        })
    return templates


def _windows(symbols, years, max_symbols: int) -> list[dict]:
    if type(max_symbols) is not int or not 1 <= max_symbols <= 25:
        raise ValueError("max_symbols must be 1..25")
    symbols = list(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols))
    if len(symbols) > max_symbols:
        raise ValueError("Explicit symbol selection exceeds max_symbols")
    if any(not re.fullmatch(r"[A-Z0-9.\-]{1,30}", symbol) for symbol in symbols):
        raise ValueError("Invalid symbol")
    years = sorted(set(int(year) for year in years))
    if any(not 2000 <= year <= 2026 for year in years) or (symbols and not years):
        raise ValueError("Select explicit discovery years in 2000..2026")
    return [{"symbol": symbol, "year": year, "half": half,
             "url": API + "?" + urllib.parse.urlencode({"index": "equities", "symbol": symbol,
                    "from_date": f"01-{'01' if half == 1 else '07'}-{year}",
                    "to_date": f"{'30-06' if half == 1 else '31-12'}-{year}"})}
            for symbol in symbols for year in years for half in (1, 2)]


def discover_scheme_filings(symbols, years, *, network: NetworkBatch,
                            max_symbols: int = 3, max_attachments: int = 10) -> dict:
    """Bounded per-symbol half-year API discovery; offline returns only plan."""
    if type(max_attachments) is not int or not 1 <= max_attachments <= 100:
        raise ValueError("max_attachments must be 1..100")
    windows = _windows(symbols, years, max_symbols)
    result = {"windows_planned": windows, "windows_completed": 0, "candidates": [], "rejected": []}
    if not network.enabled:
        return result
    for window in windows:
        if network.stopped or len({row["source_url"] for row in result["candidates"]}) >= max_attachments:
            break
        try:
            payload = network.request(window["url"], "api")
            rows = json.loads(payload)
            candidates, rejected = candidates_from_rows(rows, window["symbol"])
            merged = _merge_candidates(result["candidates"] + [
                {**row, "discovery_url": window["url"]} for row in candidates], max_attachments)
        except BatchStopped:
            break
        except (ValueError, UnicodeError) as error:
            network.stop("invalid_api_response", str(error), url=window["url"])
            break
        result["windows_completed"] += 1
        result["candidates"] = merged
        # Response bytes are bounded; rejection diagnostics are count-bounded too.
        result["rejected"].extend(rejected[:100 - len(result["rejected"])])
    # Bound output as well as requests; keep metadata of all same-URL filings.
    return result


def _merge_candidates(rows: list[dict], limit: int) -> list[dict]:
    merged = {}
    for row in rows:
        url = validate_url(_source_url(row))
        if url not in merged:
            if len(merged) >= limit:
                continue
            merged[url] = {**row, "source_url": url, "filings": list(row.get("filings") or [row])}
        elif row not in merged[url]["filings"]:
            merged[url]["filings"].append(row)
    candidates = list(merged.values())
    if len(json.dumps(candidates, ensure_ascii=False).encode("utf-8")) > MAX_JSON_BYTES:
        raise AcquisitionError("candidate_manifest_size_limit", "Candidate metadata exceeds 8 MiB")
    return candidates


def _within(root: Path, path: Path) -> Path:
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise AcquisitionError("path_outside_root", str(path))
    return resolved


def _read_source(path: Path) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise AcquisitionError("attachment_size_limit", str(path))
    return content


def _publish(path: Path, content: bytes) -> Path:
    """Atomic no-clobber publication using a same-directory temporary hardlink.

    Unlike replace/rename, os.link cannot overwrite a pre-existing source.
    Filesystems without hardlinks fail explicitly rather than weakening this.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".attachment-", suffix=".part", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.stat().st_size != len(content):
                raise AcquisitionError("cache_collision", str(path))
            with path.open("rb") as stream:
                if stream.read(len(content) + 1) != content:
                    raise AcquisitionError("cache_collision", str(path))
        except OSError as error:
            raise AcquisitionError("atomic_publish_unavailable", str(error)) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def _write_manifest(directory: Path, payload: dict) -> Path:
    content = (json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return _publish(directory / f"{hashlib.sha256(content).hexdigest()}.json", content)


def run(*, root: Path, sources: Path | None = None, announcements: Path | None = None,
        symbols=(), years=(), out: Path | None = None, fetch: bool = False,
        extract: bool = False, ocr: bool = False, max_requests: int = 10,
        max_symbols: int = 3, max_attachments: int = 10, min_interval: float = 1.0,
        timeout: float = 30.0, fetcher=None, clock=None, sleeper=None) -> dict:
    """Plan/acquire, write adapter-compatible manifests, optionally scan them.

    Discovery calls consume the SAME budget as downloads/redirects. A stopped
    network never resumes, even if earlier windows already found candidates.
    Offline extraction of already cached successes can still run after a stop.
    """
    if type(max_attachments) is not int or not 1 <= max_attachments <= 100:
        raise ValueError("max_attachments must be 1..100")
    if (fetch or extract) and out is None:
        raise ValueError("Explicit out / --out is required for fetch or extraction")
    if ocr and not extract:
        raise ValueError("OCR requires explicit extraction")
    root = Path(root).resolve()
    out = _within(root, Path(out)) if out is not None else None
    # Validate all plans/explicit URLs before performing any network request.
    _windows(symbols, years, max_symbols)
    rows = load_sources(sources) if sources is not None else []
    rejected = []
    if announcements is not None:
        cached, rejected = candidates_from_rows(_bounded_json(announcements))
        rows.extend(cached)
    rows = _merge_candidates(rows, max_attachments)
    for row in rows:
        local = row.get("cache_path") or row.get("path")
        if local:
            _within(root, Path(local))
    network = NetworkBatch(fetch=fetch, max_requests=max_requests, min_interval=min_interval,
                           timeout=timeout, fetcher=fetcher, clock=clock, sleeper=sleeper)
    discovery = discover_scheme_filings(symbols, years, network=network,
                                        max_symbols=max_symbols, max_attachments=max_attachments)
    rows.extend(discovery.pop("candidates"))
    candidates = _merge_candidates(rows, max_attachments)
    cached_rows = []
    for row in candidates:
        try:
            local = row.get("cache_path") or row.get("path")
            path = _within(root, Path(local)) if local else None
            if path is not None and path.is_file():
                content = _read_source(path)
                status = "cached"
            elif not fetch:
                row["acquisition_status"] = "pending_offline"
                continue
            elif network.stopped:
                row["acquisition_status"] = "not_attempted_network_stopped"
                continue
            else:
                content = network.request(row["source_url"])
                path = None
                status = "downloaded"
            digest = hashlib.sha256(content).hexdigest()
            expected = row.get("sha256")
            if expected is not None and (not isinstance(expected, str) or expected.lower() != digest):
                raise AcquisitionError("hash_mismatch", "Original bytes differ from supplied SHA256")
            # Full archive safety/CRC validation BEFORE publishing any source.
            attachments.pdf_members(content)
            if path is None:
                suffix = ".pdf" if content.startswith(b"%PDF-") else ".zip"
                path = _publish(out / "blobs" / f"{digest}{suffix}", content)
            row.update(acquisition_status=status, cache_path=path.relative_to(root).as_posix(),
                       sha256=digest, bytes=len(content))
            cached_rows.append(dict(row))
        except BatchStopped as error:
            row.update(acquisition_status="not_acquired", error_code=error.code, error=str(error))
        except (AcquisitionError, attachments.AttachmentError, OSError, ValueError) as error:
            row.update(acquisition_status="failed", error_code=getattr(error, "code", type(error).__name__), error=str(error))
            # Fail closed on an apparent HTML block page, bad hash/ZIP, etc.
            if fetch:
                network.stop("attachment_validation_failed", str(error), url=row["source_url"])
    result = {"pipeline_version": VERSION, "fetch_enabled": fetch,
              "requests": network.requests, "request_log": network.log, "stopped": network.stopped,
              "discovery": discovery, "rejected_cached_announcements": rejected,
              "candidate_count": len(candidates), "cached_count": len(cached_rows),
              "candidates": candidates, "verified_terms": 0, "extraction": None}
    if out is not None:
        result["candidate_manifest"] = str(_write_manifest(out / "candidates", {"attachments": candidates}))
        manifest = _write_manifest(out / "manifests", {"attachments": cached_rows})
        result["cached_manifest"] = str(manifest)
        if extract:
            result["extraction"] = attachments.scan_manifest(
                manifest, root=root, out=out / "text", limit=max_attachments, ocr=ocr)
        result["report"] = str(_write_manifest(out / "reports", result))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, help="Explicit URL JSON or existing cache manifest")
    parser.add_argument("--announcements", type=Path, help="Offline cached announcement JSON list")
    parser.add_argument("--symbols", default="", help="Comma-separated explicit symbols for half-year discovery")
    parser.add_argument("--years", default="", help="Comma-separated explicit years, 2000..2026")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, help="Root-contained output directory; required with --fetch/--extract")
    parser.add_argument("--fetch", action="store_true", help="Explicit opt-in to bounded official HTTPS requests")
    parser.add_argument("--extract", action="store_true", help="Run attachments.scan_manifest on cached successes")
    parser.add_argument("--ocr", action="store_true", help="Explicit optional local OCR; requires --extract and local tools")
    parser.add_argument("--max-requests", type=int, default=10)
    parser.add_argument("--max-symbols", type=int, default=3)
    parser.add_argument("--max-attachments", type=int, default=10)
    parser.add_argument("--min-interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        result = run(root=args.root, sources=args.sources, announcements=args.announcements,
                     symbols=[s for s in args.symbols.split(",") if s],
                     years=[int(y) for y in args.years.split(",") if y], out=args.out,
                     fetch=args.fetch, extract=args.extract, ocr=args.ocr,
                     max_requests=args.max_requests, max_symbols=args.max_symbols,
                     max_attachments=args.max_attachments, min_interval=args.min_interval,
                     timeout=args.timeout)
    except (AcquisitionError, attachments.AttachmentError, OSError, ValueError) as error:
        print(json.dumps({"status": "failed", "error_code": getattr(error, "code", type(error).__name__), "error": str(error)}, indent=2))
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    failed = any(row["acquisition_status"] == "failed" for row in result["candidates"])
    extract_failed = result["extraction"] and result["extraction"]["statuses"].get("failed")
    return 1 if result["stopped"] or failed or extract_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
