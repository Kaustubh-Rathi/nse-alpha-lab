"""Download original exchange reports without scraping a paid data service."""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPOSITORY = "BennyThadikaran/eod2_utils"
ARCHIVES = {
    "prices": ("2025-master", "nseBhav.zip"),
    "delivery": ("2025-master", "nseDelivery.zip"),
    "indices": ("2025-master", "nseIndices.zip"),
    "actions": ("pr-bhav-master", "PR_bhav-master.zip"),
}
USER_AGENT = "NSEHistoricalResearch/1.0 (personal historical-data download)"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request(url: str, headers: dict | None = None):
    accept = {"Accept": "application/octet-stream"} if "/releases/assets/" in url else {}
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **accept, **(headers or {})}),
        timeout=120,
    )


def download(url: str, target: Path, expected_size: int | None = None,
             expected_digest: str | None = None) -> dict:
    """Resume interrupted downloads and publish only verified, complete ZIP files."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    if target.exists():
        if expected_size is not None and target.stat().st_size != expected_size:
            raise ValueError(f"Existing file has unexpected size: {target}")
        print(f"Checking cached {target.name}", flush=True)
    else:
        for attempt in range(4):
            offset = partial.stat().st_size if partial.exists() else 0
            try:
                if expected_size is not None and offset == expected_size:
                    break
                headers = {"Range": f"bytes={offset}-"} if offset else {}
                with request(url, headers) as response:
                    status = response.status
                    if status == 206:
                        content_range = response.headers.get("Content-Range", "")
                        if not content_range.startswith(f"bytes {offset}-"):
                            raise ValueError("Server returned the wrong resume offset")
                    elif status == 200:
                        offset = 0  # The server ignored Range: start over, never append.
                    else:
                        raise ValueError(f"Unexpected HTTP status: {status}")
                    total = expected_size
                    if total is None and response.headers.get("Content-Length"):
                        total = offset + int(response.headers["Content-Length"])
                    received = offset
                    last_report = offset
                    with partial.open("ab" if offset else "wb") as stream:
                        while chunk := response.read(1024 * 1024):
                            stream.write(chunk)
                            received += len(chunk)
                            if received - last_report >= 64 * 1024 * 1024:
                                suffix = f" / {total / 1e6:.0f} MB" if total else ""
                                print(f"{target.name}: {received / 1e6:.0f} MB{suffix}", flush=True)
                                last_report = received
                    if total is not None and received != total:
                        raise OSError(f"Incomplete response: {received} of {total} bytes")
                break
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                if isinstance(error, urllib.error.HTTPError) and error.code in (401, 403, 404, 429):
                    raise  # Respect access restrictions and rate limits.
                if attempt == 3:
                    raise
                print(f"Download interrupted ({type(error).__name__}); retrying", flush=True)
                time.sleep(2 ** attempt)
        if not partial.exists():
            raise OSError(f"Download produced no file: {target}")

    candidate = target if target.exists() else partial
    if expected_size is not None and candidate.stat().st_size != expected_size:
        raise ValueError(f"Size verification failed: {candidate}")
    digest = sha256(candidate)
    if expected_digest and expected_digest.removeprefix("sha256:") != digest:
        raise ValueError(f"SHA256 verification failed: {candidate}")
    with zipfile.ZipFile(candidate) as archive:
        if not archive.infolist():
            raise ValueError(f"Empty archive: {candidate}")
    if candidate == partial:
        partial.replace(target)
    print(f"Ready: {target.name} ({target.stat().st_size / 1e6:.1f} MB)", flush=True)
    return {"file": target.name, "bytes": target.stat().st_size, "sha256": digest,
            "url": url, "verified_at": datetime.now(timezone.utc).isoformat(),
            "publisher_digest_verified": bool(expected_digest)}


def fetch_archives(data: Path, datasets: list[str], workers: int = 4) -> None:
    if not 1 <= workers <= 4:
        raise ValueError("Archive workers must be between 1 and 4")
    releases = {}
    manifests = data / "manifests"
    manifest_path = manifests / "downloads.json"
    records = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    jobs = []
    for dataset in dict.fromkeys(datasets):
        tag, filename = ARCHIVES[dataset]
        if tag not in releases:
            url = f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{tag}"
            with request(url) as response:
                releases[tag] = json.load(response)
            write_json(manifests / f"release-{tag}.json", releases[tag])
        release = releases[tag]
        asset = next(asset for asset in release["assets"] if asset["name"] == filename)
        jobs.append((dataset, filename, asset, release))
    errors = []
    # Only the coordinator updates the shared manifest, avoiding lost records.
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(download, asset["url"], data / "raw" / filename,
                            asset["size"], asset.get("digest")): (dataset, asset, release)
            for dataset, filename, asset, release in jobs
        }
        for future in as_completed(futures):
            dataset, asset, release = futures[future]
            try:
                record = future.result()
            except Exception as error:
                errors.append((dataset, error))
                print(f"{dataset} failed: {type(error).__name__}", flush=True)
                continue
            record.update(dataset=dataset, release=release["html_url"],
                          browser_download_url=asset["browser_download_url"],
                          release_published_at=release["published_at"])
            records[dataset] = record
            write_json(manifest_path, records)
    if errors:
        names = ", ".join(name for name, _ in errors)
        raise RuntimeError(f"Archive downloads incomplete: {names}") from errors[0][1]


def fetch_recent(data: Path, start: date, end: date, include_actions: bool = True,
                 include_prices: bool = True) -> None:
    """Fetch official modern NSE reports; absent dates are logged, never fabricated."""
    if start > end or end > date.today():
        raise ValueError("Require start <= end <= today")
    if not include_prices and not include_actions:
        raise ValueError("Select at least one report stream")
    manifest_path = data / "manifests" / "recent-downloads.json"
    records = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    blocked = set()
    day = start
    while day <= end:
        # Include weekends: NSE sometimes holds special weekend trading sessions.
        reports = []
        if include_prices:
            reports.append(("prices", f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip",
                            "https://nsearchives.nseindia.com/content/cm/"))
        if include_actions:
            reports.append(("actions", f"PR{day:%d%m%y}.zip",
                            "https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/"))
        for kind, name, base in reports:
            if kind in blocked:
                continue
            key = f"{kind}/{day}"
            target = data / "raw" / "recent" / kind / name
            if target.exists() and records.get(key, {}).get("status") == "downloaded":
                continue
            try:
                record = download(base + name, target)
                records[key] = {**record, "date": day.isoformat(), "status": "downloaded"}
            except urllib.error.HTTPError as error:
                records[key] = {"date": day.isoformat(), "status": "http_error",
                                "http_status": error.code, "url": base + name}
                print(f"{kind} {day}: HTTP {error.code}", flush=True)
                if error.code in (401, 403, 429):
                    blocked.add(kind)
                    print(f"Stopping {kind} requests: access denied/rate limited", flush=True)
                elif error.code >= 500:
                    blocked.add(kind)
                    print(f"Stopping {kind} requests: upstream is unavailable", flush=True)
                elif error.code != 404:
                    write_json(manifest_path, records)
                    raise
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                records[key] = {"date": day.isoformat(), "status": "transport_error",
                                "error_type": type(error).__name__, "url": base + name}
                blocked.add(kind)
                print(f"Stopping {kind} requests: {type(error).__name__}", flush=True)
            write_json(manifest_path, records)
            time.sleep(0.5)
        if len(blocked) == len(reports):
            break
        day += timedelta(days=1)
    if blocked:
        raise RuntimeError(f"Recent update incomplete: stopped {', '.join(sorted(blocked))}. See {manifest_path}")


def report_date(name: str) -> date | None:
    """Dates in filenames do not depend on the machine's language/locale."""
    base = Path(name).name
    modern = re.search(r"_(\d{8})_F_", base, re.I)
    if modern:
        return date.fromisoformat(f"{modern[1][:4]}-{modern[1][4:6]}-{modern[1][6:]}")
    old = re.fullmatch(r"cm(\d{2})([A-Z]{3})(\d{4})bhav\.csv(?:\.zip)?", base, re.I)
    if old:
        months = dict(zip("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), range(1, 13)))
        return date(int(old[3]), months[old[2].upper()], int(old[1]))
    pr = re.fullmatch(r"PR(\d{2})(\d{2})(\d{2})\.zip", base, re.I)
    if pr:
        return date(2000 + int(pr[3]), int(pr[2]), int(pr[1]))
    return None
