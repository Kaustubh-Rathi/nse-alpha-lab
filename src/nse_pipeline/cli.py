"""Command line entry point: ``nse-alpha`` or ``python -m nse_pipeline``.

Design rules:

1. **Stdlib only.** ``argparse``, ``urllib``, ``hashlib`` and ``subprocess``
   are enough. No click, no typer, no requests.
2. **Never guess.** Invalid input is refused with an error that names every
   value the argument accepts. Nothing is silently coerced or defaulted to a
   value the caller did not ask for.
3. **Never shell out to git.** Data-pack verification is a pure content check
   (SHA256 + byte size) over the manifest, so it works from a tarball, a
   mounted volume or a CI cache with no ``.git`` directory at all.
4. **Clear failure budget.** Every subcommand returns 0 on success, 1 on a
   verification or download failure, and 2 on invalid usage (argparse's own
   convention). Missing optional backends are reported with the exact install
   command rather than a traceback.

The data pack is a directory plus a JSON manifest. The manifest is the only
thing that defines what "intact" means: a file is intact when its SHA256 digest
and its byte size both match the recorded values.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .paths import REPO_ROOT, data_root

#: Where the bundled example specs live, relative to the repository root.
EXAMPLES_DIR = REPO_ROOT / "examples" / "specs"

#: Where the research sweep scripts live, relative to the repository root.
RESEARCH_DIR = REPO_ROOT / "research"

#: Manifest file names tried, in order, when ``--manifest`` is not given.
DEFAULT_MANIFEST_NAMES = ("manifest.json", "pack.json", "checksums.json")

#: Transport accepted by ``fetch``. ``local`` copies, ``url`` downloads one
#: tarball, and the two registry backends need an optional third-party client.
FETCH_SOURCES = ("local", "url", "kaggle", "hf")

#: Where the Hugging Face backend looks, in order, for a repository id.
HF_ENV_VARS = ("NSE_HF_REPO", "HF_REPO_ID")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

#: Every value a strategy spec field may take. ``None`` means "validated by a
#: range check instead of an enumeration" and is documented in the spec table.
VALID_DIRECTIONS = ("top", "bottom")
VALID_CADENCES = ("monthly", "quarterly", "semiannual", "annual")
VALID_COST_MODELS = ("full_charges", "zero_cost")
VALID_BENCHMARKS = ("nifty50", "nifty500", "nifty200", "msci400", "midcap150",
                    "smallcap250", "anderson_cio_index")
VALID_SPEC_KEYS = frozenset({
    "name", "universe", "benchmark", "top_n", "direction", "lookback_sessions",
    "cadence", "stop_loss_pct", "take_profit_pct", "cost_model", "start", "end",
    "description",
})

#: Cost presets. ``zero_cost`` exists so the size of the charge assumption can
#: be measured; it is not an executable strategy and is labelled as such.
ZERO_COST_OVERRIDES = {
    "brokerage_bps": "0",
    "stt_bps_buy": "0",
    "stt_bps_sell": "0",
    "stamp_bps": "0",
    "exchange_bps": "0",
    "gst_on_brokerage_pct": "0",
    "slippage_bps": "0",
}


class CliError(Exception):
    """A refusal with a human-readable explanation and a process exit code."""

    def __init__(self, message: str, exit_code: int = EXIT_FAIL):
        super().__init__(message)
        self.exit_code = exit_code


class ManifestError(CliError):
    """The manifest itself is missing or malformed -- a usage error (exit 2).

    This is distinct from a *pack* that fails verification (exit 1): the caller
    handed us something that is not a usable manifest, so no verdict about the
    data has been reached.
    """

    def __init__(self, message: str):
        super().__init__(message, EXIT_USAGE)


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------

def _out(message: str = "") -> None:
    print(message)


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _headline(message: str) -> None:
    _out(message)
    _out("=" * len(message))


# ---------------------------------------------------------------------------
# sha256 / manifest helpers
# ---------------------------------------------------------------------------

def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """Hex SHA256 of a file, read in bounded chunks so large packs stream."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalise_entries(manifest: object, manifest_path: Path) -> list[dict]:
    """Extract ``[{path, sha256, size}]`` from any manifest shape we accept.

    Accepted shapes, in order of preference:

    * ``{"files": [...]}`` -- the documented form.
    * ``{"entries": [...]}`` or ``{"checksums": [...]}`` -- aliases.
    * a bare JSON list of entries.
    * ``{"files": {"<relpath>": {"sha256": ..., "size": ...}}}`` -- mapping
      form, where the key is the path.

    Anything else is refused, because guessing a manifest schema is how a
    verifier silently starts verifying nothing.
    """
    def coerce(record: object, fallback_path: str | None = None) -> dict:
        if not isinstance(record, dict):
            raise ManifestError(
                f"{manifest_path}: every manifest entry must be a JSON object, "
                f"got {type(record).__name__}")
        path = record.get("path", fallback_path)
        if not isinstance(path, str) or not path.strip():
            raise ManifestError(
                f"{manifest_path}: entry {record!r} has no non-empty 'path' string")
        sha = record.get("sha256", record.get("sha"))
        if not isinstance(sha, str) or not sha.strip():
            raise ManifestError(
                f"{manifest_path}: entry {path!r} has no 'sha256' string")
        size = record.get("size", record.get("bytes"))
        if isinstance(size, bool) or not isinstance(size, int):
            raise ManifestError(
                f"{manifest_path}: entry {path!r} has no integer 'size' "
                f"(got {size!r}); the manifest must record the byte size")
        if size < 0:
            raise ManifestError(
                f"{manifest_path}: entry {path!r} has negative size {size}")
        return {"path": path, "sha256": sha.strip().lower(), "size": size}

    records: object = manifest
    if isinstance(manifest, dict):
        for key in ("files", "entries", "checksums"):
            if key in manifest:
                records = manifest[key]
                break
        else:
            raise ManifestError(
                f"{manifest_path}: unrecognised manifest. Expected a 'files' list "
                "(or 'entries'/'checksums'), each item "
                '{\"path\": \"...\", \"sha256\": \"...\", \"size\": 123}. '
                "Run 'verify --help' for the full schema.")

    if isinstance(records, list):
        return [coerce(record) for record in records]
    if isinstance(records, dict):
        return [coerce(record, fallback_path=path)
                for path, record in records.items()]
    raise ManifestError(
        f"{manifest_path}: 'files' must be a list of entries or a "
        f"{'{path: {...}}'} mapping, got {type(records).__name__}")


def load_manifest(manifest_path: Path) -> list[dict]:
    """Read and normalise a manifest, refusing unreadable or malformed JSON.

    A missing or malformed manifest is a *usage* error (exit 2): the caller
    pointed at something that is not a manifest, which is different from a pack
    whose files are corrupt (exit 1).
    """
    if not manifest_path.exists():
        raise ManifestError(f"manifest not found: {manifest_path}")
    if not manifest_path.is_file():
        raise ManifestError(f"manifest is not a file: {manifest_path}")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{manifest_path}: not valid JSON -- {exc}") from exc
    except OSError as exc:
        raise ManifestError(f"{manifest_path}: cannot read -- {exc}") from exc
    return normalise_entries(raw, manifest_path)


def resolve_manifest(pack_dir: Path, explicit: Path | None) -> Path:
    """The manifest to use: the explicit path, else a default name in the pack."""
    if explicit is not None:
        return explicit.expanduser()
    for name in DEFAULT_MANIFEST_NAMES:
        candidate = pack_dir / name
        if candidate.is_file():
            return candidate
    raise ManifestError(
        f"no manifest found in {pack_dir}. Expected one of "
        f"{', '.join(DEFAULT_MANIFEST_NAMES)}, or pass --manifest PATH.")


def verify_pack(pack_dir: Path, manifest_path: Path) -> dict:
    """Check every manifest entry against the bytes on disk.

    The check is content-only: ``.git`` is never consulted, so a directory that
    arrived as a tarball or a CI cache verifies exactly like a clone.
    """
    entries = load_manifest(manifest_path)
    missing: list[dict] = []
    mismatched: list[dict] = []
    ok: list[dict] = []

    for entry in entries:
        target = (pack_dir / entry["path"]).resolve()
        # A manifest must never be able to read outside the pack directory;
        # a "../../etc/passwd" entry is a malformed manifest, not a file.
        try:
            target.relative_to(pack_dir.resolve())
        except ValueError:
            mismatched.append({
                "path": entry["path"],
                "reason": "path escapes the pack directory",
            })
            continue
        if not target.is_file():
            missing.append({"path": entry["path"]})
            continue
        actual_size = target.stat().st_size
        if actual_size != entry["size"]:
            mismatched.append({
                "path": entry["path"],
                "reason": f"size {actual_size} != manifest {entry['size']}",
            })
            continue
        actual_sha = sha256_file(target)
        if actual_sha != entry["sha256"]:
            mismatched.append({
                "path": entry["path"],
                "reason": f"sha256 {actual_sha} != manifest {entry['sha256']}",
            })
            continue
        ok.append({"path": entry["path"], "size": actual_size})

    return {
        "pack_dir": str(pack_dir),
        "manifest": str(manifest_path),
        "checked": len(entries),
        "ok": ok,
        "missing": missing,
        "mismatched": mismatched,
        "passed": not missing and not mismatched and bool(entries),
        "empty_manifest": not entries,
    }


# ---------------------------------------------------------------------------
# download backends
# ---------------------------------------------------------------------------

def _download_url(url: str, destination: Path) -> None:
    """Fetch ``url`` to ``destination`` with a clear error on network failure."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "nse-alpha-cli/0.1 (+data pack fetch)"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, \
                destination.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1 << 20)
    except urllib.error.HTTPError as exc:
        raise CliError(f"download failed: {url} returned HTTP {exc.code} "
                       f"{exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise CliError(f"download failed: {url} -- {exc.reason}") from exc
    except OSError as exc:
        raise CliError(f"download failed: {url} -- {exc}") from exc


def _kaggle_download(reference: str, destination_dir: Path) -> None:
    """Download a Kaggle dataset. Requires the ``kaggle`` package."""
    try:
        from kaggle.api.kaggle_api_extended import KaggleApi  # noqa: PLC0415
    except ImportError as exc:
        raise CliError(
            "the Kaggle backend needs the optional 'kaggle' package.\n"
            "Install it with:  pip install kaggle\n"
            "then authenticate with a ~/.kaggle/kaggle.json API token or the\n"
            "KAGGLE_USERNAME and KAGGLE_KEY environment variables.") from exc
    api = KaggleApi()
    try:
        api.authenticate()
    except Exception as exc:  # noqa: BLE001 - surface the backend's own message
        raise CliError(
            f"Kaggle authentication failed: {exc}\n"
            "Create an API token at https://www.kaggle.com/settings and save it "
            "as ~/.kaggle/kaggle.json, or set KAGGLE_USERNAME and KAGGLE_KEY.") from exc
    destination_dir.mkdir(parents=True, exist_ok=True)
    # ``dataset_download_files`` writes <name>.zip into path; unzip=True expands it.
    api.dataset_download_files(reference, path=str(destination_dir), unzip=True)


def _hf_download(repo_id: str, destination_dir: Path) -> None:
    """Download a Hugging Face dataset. Requires ``huggingface_hub``."""
    try:
        from huggingface_hub import snapshot_download  # noqa: PLC0415
    except ImportError as exc:
        raise CliError(
            "the Hugging Face backend needs the optional 'huggingface_hub' package.\n"
            "Install it with:  pip install huggingface_hub\n"
            "For a gated dataset run:  huggingface-cli login") from exc
    destination_dir.mkdir(parents=True, exist_ok=True)
    try:
        snapshot_download(repo_id=repo_id, repo_type="dataset",
                          local_dir=str(destination_dir))
    except Exception as exc:  # noqa: BLE001 - surface the backend's own message
        raise CliError(
            f"Hugging Face download of dataset {repo_id!r} failed: {exc}\n"
            "Check the repository id, and log in with 'huggingface-cli login' "
            "if the dataset is gated.") from exc


def _local_copy(source: Path, destination_dir: Path) -> None:
    """Copy a source pack into the destination as the pack root.

    A directory's *contents* are copied, not the directory itself, so that
    ``dest/a.bin`` matches a manifest entry ``"path": "a.bin"``. Copying the
    directory would produce ``dest/<name>/a.bin`` and every manifest lookup
    would miss.
    """
    if not source.exists():
        raise CliError(f"local source does not exist: {source}")
    destination_dir.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        for child in sorted(source.iterdir(), key=lambda p: p.name):
            target = destination_dir / child.name
            if child.is_dir():
                shutil.copytree(child, target, dirs_exist_ok=True)
            else:
                shutil.copy2(child, target)
    else:
        shutil.copy2(source, destination_dir / source.name)


# ---------------------------------------------------------------------------
# strategy spec validation
# ---------------------------------------------------------------------------

def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_iso_date(value: object, field: str, problems: list[str]) -> None:
    """Refuse anything that is not an ISO ``YYYY-MM-DD`` calendar date."""
    if value is None:
        return
    if not isinstance(value, str):
        problems.append(f"  {field}: must be a string 'YYYY-MM-DD', "
                        f"got {type(value).__name__} {value!r}")
        return
    try:
        # date.fromisoformat accepts the exact YYYY-MM-DD form we document.
        from datetime import date as _date  # noqa: PLC0415
        _date.fromisoformat(value)
    except ValueError:
        problems.append(f"  {field}: {value!r} is not an ISO date 'YYYY-MM-DD' "
                        "(for example \"2013-01-31\")")


def validate_spec(spec: object) -> list[str]:
    """Return a list of human-readable problems; empty means the spec is valid."""
    if not isinstance(spec, dict):
        return [f"  spec root must be a JSON object, got {type(spec).__name__}"]

    problems: list[str] = []
    unknown = sorted(set(spec) - VALID_SPEC_KEYS)
    if unknown:
        problems.append(
            f"  unknown field(s): {', '.join(unknown)}. "
            f"Valid fields: {', '.join(sorted(VALID_SPEC_KEYS))}")

    universe = spec.get("universe")
    if universe is None:
        problems.append("  universe: required (a non-empty string naming the "
                        "point-in-time universe, for example \"nifty500\")")
    elif not isinstance(universe, str) or not universe.strip():
        problems.append(f"  universe: must be a non-empty string, got {universe!r}")

    benchmark = spec.get("benchmark")
    if benchmark is not None and benchmark not in VALID_BENCHMARKS:
        problems.append(
            f"  benchmark: {benchmark!r} is not valid. "
            f"Valid values: {', '.join(VALID_BENCHMARKS)}")

    top_n = spec.get("top_n")
    if top_n is not None:
        if not isinstance(top_n, int) or isinstance(top_n, bool):
            problems.append(f"  top_n: must be an integer, got {top_n!r}")
        elif top_n < 1:
            problems.append(f"  top_n: must be at least 1, got {top_n}")

    direction = spec.get("direction")
    if direction is not None and direction not in VALID_DIRECTIONS:
        problems.append(
            f"  direction: {direction!r} is not valid. "
            f"Valid values: {', '.join(VALID_DIRECTIONS)} "
            "(top = strongest trailing return, bottom = weakest; bottom is a "
            "reversal counterfactual, not a recommendation)")

    cadence = spec.get("cadence")
    if cadence is not None and cadence not in VALID_CADENCES:
        problems.append(
            f"  cadence: {cadence!r} is not valid. "
            f"Valid values: {', '.join(VALID_CADENCES)}")

    lookback = spec.get("lookback_sessions")
    if lookback is not None:
        if not isinstance(lookback, int) or isinstance(lookback, bool):
            problems.append(
                f"  lookback_sessions: must be an integer count of sessions, "
                f"got {lookback!r}")
        elif lookback < 1:
            problems.append(
                f"  lookback_sessions: must be at least 1, got {lookback}")

    for field in ("stop_loss_pct", "take_profit_pct"):
        value = spec.get(field)
        if value is None:
            continue
        if not _is_number(value):
            problems.append(f"  {field}: must be a number or null, got {value!r}")
        elif not 0 < value < 100:
            problems.append(
                f"  {field}: must be a percentage in percent units strictly "
                f"between 0 and 100 (20 means 20%), got {value}")

    cost_model = spec.get("cost_model")
    if cost_model is not None and cost_model not in VALID_COST_MODELS:
        problems.append(
            f"  cost_model: {cost_model!r} is not valid. "
            f"Valid values: {', '.join(VALID_COST_MODELS)} "
            "(full_charges = brokerage+GST+STT+stamp+exchange+slippage; "
            "zero_cost = counterfactual with no charges)")

    for field in ("start", "end"):
        _check_iso_date(spec.get(field), field, problems)

    if not problems:
        start, end = spec.get("start"), spec.get("end")
        if start is not None and end is not None and start > end:
            problems.append(f"  start: {start!r} is after end {end!r}")

    return problems


def load_spec(path: Path) -> dict:
    """Read a spec file and refuse it if invalid, listing every problem."""
    if not path.exists():
        raise CliError(
            f"spec file not found: {path}\n"
            f"Bundled examples live in {EXAMPLES_DIR}. "
            "Run 'list' to see them.", EXIT_USAGE)
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CliError(f"{path}: not valid JSON -- {exc}", EXIT_USAGE) from exc
    except OSError as exc:
        raise CliError(f"{path}: cannot read -- {exc}", EXIT_USAGE) from exc

    problems = validate_spec(spec)
    if problems:
        raise CliError(
            f"invalid strategy spec: {path}\n" + "\n".join(problems), EXIT_USAGE)
    return spec


def _percent_to_fraction(value: object) -> "object":
    """Percent units in the spec, positive fractions for ``ExitPolicy``."""
    if value is None:
        return None
    from decimal import Decimal  # noqa: PLC0415
    return Decimal(str(value)) / Decimal(100)


def build_engine_config(spec: dict):
    """Map a validated spec onto the momentum engine's ``EngineConfig``.

    Imported lazily so ``--help`` and ``list`` stay fast and never require the
    numeric stack to be present.
    """
    from decimal import Decimal  # noqa: PLC0415

    from .backtest.exits import ExitPolicy  # noqa: PLC0415
    from .backtest.momentum import EngineConfig  # noqa: PLC0415

    overrides: dict = {
        "cadence": spec.get("cadence") or "monthly",
        "top_n": spec.get("top_n") or 20,
        "direction": spec.get("direction") or "top",
        "lookback_sessions": spec.get("lookback_sessions") or 63,
        "exit_policy": ExitPolicy(
            stop_loss_pct=_percent_to_fraction(spec.get("stop_loss_pct")),
            take_profit_pct=_percent_to_fraction(spec.get("take_profit_pct")),
        ),
    }
    if (spec.get("cost_model") or "full_charges") == "zero_cost":
        overrides.update({key: Decimal(value)
                          for key, value in ZERO_COST_OVERRIDES.items()})
    return EngineConfig(**overrides)


# ---------------------------------------------------------------------------
# backtest execution
# ---------------------------------------------------------------------------

BARS_REQUIRED_COLUMNS = ("date", "key", "open", "close", "volume")


def _bars_path(pack_dir: Path | None) -> Path | None:
    """Locate the adjusted bars parquet for a backtest."""
    if pack_dir is not None:
        explicit = pack_dir / "adjusted_bars.parquet"
        if explicit.is_file():
            return explicit
    fallback = data_root() / "processed" / "adjusted" / "adjusted_bars.parquet"
    return fallback if fallback.is_file() else None


def _load_bars(path: Path):
    """Load the bars parquet as a pandas frame, one attempt, clear failure."""
    try:
        import pandas as pd  # noqa: PLC0415
    except ImportError as exc:
        raise CliError(
            "backtest needs pandas and pyarrow.\n"
            "Install them with:  pip install pandas pyarrow") from exc
    try:
        frame = pd.read_parquet(path)
    except ImportError as exc:
        raise CliError(
            f"cannot read {path}: the parquet engine is missing.\n"
            "Install it with:  pip install pyarrow") from exc
    except OSError as exc:
        raise CliError(f"cannot read {path}: {exc}") from exc
    absent = [column for column in BARS_REQUIRED_COLUMNS
              if column not in frame.columns]
    if absent:
        raise CliError(
            f"{path}: bars file is missing required column(s): "
            f"{', '.join(absent)}")
    return frame


def _filter_window(frame, start: str | None, end: str | None):
    """Restrict bars to ``[start, end]`` without assuming the date dtype.

    A parquet file may carry ``date`` as an object column of ``datetime.date``
    or as a datetime column, so comparing directly against an ISO string can
    raise instead of filtering. Parsing both sides keeps the window exact.
    """
    if start is None and end is None:
        return frame
    import pandas as pd  # noqa: PLC0415

    dates = pd.to_datetime(frame["date"])
    if start is not None:
        frame = frame[dates >= pd.Timestamp(start)]
        dates = pd.to_datetime(frame["date"])
    if end is not None:
        frame = frame[dates <= pd.Timestamp(end)]
    return frame


def spec_label(spec: dict, config) -> str:
    """A label that is unique per result-affecting spec.

    The engine's own ``config.label`` covers cadence, size, lookback, exit
    overlay and direction, but not the cost model. Two rows that differ only in
    charges would therefore share a label and look like duplicates, so the cost
    assumption and the spec name are appended here.
    """
    parts = [config.label]
    if (spec.get("cost_model") or "full_charges") != "full_charges":
        parts.append(spec["cost_model"])
    if spec.get("start") or spec.get("end"):
        parts.append(f"{spec.get('start') or 'start'}_{spec.get('end') or 'end'}")
    if spec.get("name"):
        parts.append(str(spec["name"]))
    return "-".join(parts)


def run_backtest(spec: dict, pack_dir: Path | None, output: Path | None) -> dict:
    """Run one spec through ``MomentumRotationEngine`` and report the summary."""
    # Imported here so the module can be imported without numpy/pandas present.
    from .backtest.momentum import BarsFrame, MomentumRotationEngine  # noqa: PLC0415

    config = build_engine_config(spec)
    bars_path = _bars_path(pack_dir)
    if bars_path is None:
        raise CliError(
            "no adjusted bars file found.\n"
            "Looked in: " + (f"{pack_dir} / adjusted_bars.parquet" if pack_dir
                             else "<no --pack given>") +
            f"\n          {data_root() / 'processed' / 'adjusted' / 'adjusted_bars.parquet'}"
            "\nFetch the data pack first ('nse-alpha fetch --help'), or pass --pack.")
    frame = _load_bars(bars_path)

    start, end = spec.get("start"), spec.get("end")
    frame = _filter_window(frame, start, end)

    if frame.empty:
        raise CliError(
            f"no bars remain between start={start!r} and end={end!r} in {bars_path}")

    result = MomentumRotationEngine(config).run(BarsFrame(frame))

    equity_rows = [row for row in result["equity"] if row["equity"] is not None]
    final_equity = equity_rows[-1]["equity"] if equity_rows else None
    start_equity = config.initial_cash
    total_return_pct = None
    if final_equity is not None and start_equity > 0:
        from decimal import Decimal  # noqa: PLC0415
        total_return_pct = float(
            (Decimal(final_equity) - start_equity) / start_equity * 100)

    summary = {
        "label": spec_label(spec, config),
        "engine_label": result["label"],
        "spec": spec,
        "bars_source": str(bars_path),
        "sessions": len(result["equity"]),
        "rebalances": len(result["cadence"]["dates"]),
        "cadence_complete": result["cadence"]["complete"],
        "trades": len(result["trades"]),
        "exits": len(result["exits"]),
        "skips": len(result["skips"]),
        "final_cash": result["final_cash"],
        "final_equity": final_equity,
        "unpriceable_sessions": len(result["equity"]) - len(equity_rows),
        "total_return_pct": (round(total_return_pct, 4)
                             if total_return_pct is not None else None),
    }

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = {"summary": summary, "result": result}
        output.write_text(json.dumps(payload, indent=2, default=str),
                          encoding="utf-8")
        summary["output"] = str(output)
    return summary


# ---------------------------------------------------------------------------
# subcommand handlers
# ---------------------------------------------------------------------------

VERIFY_EPILOG = """\
manifest format
  A data pack is a directory plus a JSON manifest listing every file:

    {"generated_at": "2026-01-01T00:00:00Z",
     "files": [
       {"path": "adjusted_bars.parquet", "sha256": "<64 hex chars>", "size": 402313412},
       {"path": "processed/universe/universe_observations.parquet",
        "sha256": "...", "size": 12345}
     ]}

  * path   -- file location relative to the pack directory, using '/' separators
  * sha256 -- lowercase hex SHA256 of the file's exact bytes
  * size   -- exact byte size, checked before the hash so truncation is cheap to spot

  'entries' and 'checksums' are accepted as aliases for 'files', and a bare
  JSON list of entries works too.

what is checked
  For every entry: the file exists, its byte size matches, and its SHA256
  matches. A file is intact only when all three hold.

what is NOT used
  Git is never invoked and .git is never read: verification is a pure content
  check, so a pack that arrived as a tarball or a CI cache verifies identically.

exit status
  0  every listed file matched
  1  at least one file was missing or mismatched (all failures are listed)
  2  the manifest itself was missing, unreadable or malformed
"""


def cmd_verify(args: argparse.Namespace) -> int:
    pack_dir = (args.pack or REPO_ROOT).expanduser()
    manifest_path = resolve_manifest(pack_dir, args.manifest)
    _out(f"Verifying data pack: {pack_dir}")
    _out(f"Manifest:            {manifest_path}")
    _out("")

    result = verify_pack(pack_dir, manifest_path)

    if result["empty_manifest"]:
        _err("FAILED: the manifest lists no files, so nothing can be verified. "
             "An empty manifest is treated as a failure, not a pass.")
        return EXIT_FAIL

    for entry in result["ok"]:
        _out(f"  OK       {entry['path']}  ({entry['size']} bytes)")
    for entry in result["missing"]:
        _out(f"  MISSING  {entry['path']}")
    for entry in result["mismatched"]:
        _out(f"  MISMATCH {entry['path']}  -- {entry['reason']}")

    _out("")
    if result["passed"]:
        _out(f"PASSED: {result['checked']} file(s) verified against the manifest.")
        return EXIT_OK
    _err(f"FAILED: {len(result['missing'])} missing, "
         f"{len(result['mismatched'])} mismatched, "
         f"{len(result['ok'])} ok out of {result['checked']} listed. "
         "Re-fetch the pack with 'nse-alpha fetch'.")
    return EXIT_FAIL


FETCH_EPILOG = """\
sources
  local  Copy a directory or file from the filesystem. Requires --from PATH.
  url    Download one archive (or file) over https/http. Requires --from URL.
  kaggle Download a Kaggle dataset. Requires --from OWNER/DATASET and the
         optional 'kaggle' package:  pip install kaggle
  hf     Download a Hugging Face dataset. Requires --from REPO_ID and the
         optional 'huggingface_hub' package:  pip install huggingface_hub

hash verification
  When --manifest is given, every file in it is checked after the download.
  A single mismatched byte refuses the pack (exit 1) and, unless
  --keep-on-mismatch is passed, the downloaded payload is deleted so a partial
  pack can never be mistaken for a good one. Without --manifest nothing can be
  verified and the command says so explicitly.
"""


def _require_module(module: str, backend: str, install: str,
                    extra: str = "") -> None:
    """Refuse early when an optional fetch backend is not installed."""
    import importlib.util  # noqa: PLC0415

    if importlib.util.find_spec(module) is not None:
        return
    raise CliError(
        f"the {backend} backend needs the optional '{module}' package, which is "
        f"not installed.\nInstall it with:  {install}"
        + (f"\n{extra}" if extra else ""))


def _preflight_fetch(source: str, reference: str, destination: Path,
                     manifest: Path | None) -> None:
    """Check everything that can be checked before any bytes are written."""
    if source == "local":
        candidate = Path(reference).expanduser().resolve()
        if not candidate.exists():
            raise CliError(f"local source does not exist: {candidate}")
    elif source == "url":
        if not reference.lower().startswith(("http://", "https://")):
            raise CliError(
                f"--from for --source url must be an http:// or https:// URL, "
                f"got {reference!r}", EXIT_USAGE)
    elif source == "kaggle":
        if "/" not in reference:
            raise CliError(
                f"--from for --source kaggle must be OWNER/DATASET, "
                f"got {reference!r}", EXIT_USAGE)
        _require_module("kaggle", "Kaggle",
                        "pip install kaggle",
                        "Then authenticate with a ~/.kaggle/kaggle.json API "
                        "token, or set KAGGLE_USERNAME and KAGGLE_KEY.")
    elif source == "hf":
        _require_module("huggingface_hub", "Hugging Face",
                        "pip install huggingface_hub",
                        "For a gated dataset, log in with: huggingface-cli login")
    else:  # unreachable: argparse restricts choices
        raise CliError(f"unsupported source {source!r}", EXIT_USAGE)

    # A manifest that cannot be read would only be discovered after a long
    # download, so load it up front.
    if manifest is not None:
        load_manifest(manifest.expanduser().resolve())


def cmd_fetch(args: argparse.Namespace) -> int:
    destination = args.dest.expanduser().resolve()
    source = args.source
    reference = args.source_ref

    _headline(f"Fetching data pack via '{source}'")
    _out(f"Destination: {destination}")
    _out("")

    if destination.exists() and any(destination.iterdir()) and not args.force:
        raise CliError(
            f"{destination} already exists and is not empty. "
            "Pass --force to overwrite it, or choose another --dest.", EXIT_USAGE)

    # Preflight before touching the filesystem: a missing optional backend or an
    # absent local source must fail without leaving a half-built destination
    # directory behind for a later run to mistake for a real pack.
    _preflight_fetch(source, reference, destination, args.manifest)

    destination.mkdir(parents=True, exist_ok=True)
    payload_dir = destination

    if source == "local":
        _local_copy(Path(reference).expanduser().resolve(), payload_dir)
    elif source == "url":
        name = Path(urllib.parse.urlparse(reference).path).name or "pack.download"
        target = payload_dir / name
        _out(f"Downloading {reference} -> {target}")
        _download_url(reference, target)
        _out(f"Downloaded {target.stat().st_size} bytes.")
    elif source == "kaggle":
        _out(f"Resolving Kaggle dataset {reference!r} ...")
        _kaggle_download(reference, payload_dir)
    elif source == "hf":
        _out(f"Resolving Hugging Face dataset {reference!r} ...")
        _hf_download(reference, payload_dir)
    else:  # unreachable: argparse restricts choices
        raise CliError(f"unsupported source {source!r}", EXIT_USAGE)

    if args.manifest is None:
        _out("")
        _out("No --manifest given, so the download was NOT hash-verified.")
        _out("Pass --manifest to verify the pack you just fetched.")
        return EXIT_OK

    manifest_path = args.manifest.expanduser().resolve()
    _out("")
    _out(f"Verifying against manifest: {manifest_path}")
    result = verify_pack(payload_dir, manifest_path)
    for entry in result["ok"]:
        _out(f"  OK       {entry['path']}")
    for entry in result["missing"]:
        _out(f"  MISSING  {entry['path']}")
    for entry in result["mismatched"]:
        _out(f"  MISMATCH {entry['path']}  -- {entry['reason']}")

    if result["passed"] and not result["empty_manifest"]:
        _out("")
        _out(f"PASSED: {result['checked']} file(s) verified after download.")
        return EXIT_OK

    _err("")
    _err("REFUSING the pack: it does not match its manifest.")
    if not args.keep_on_mismatch:
        _remove_payload(destination, keep={args.manifest.expanduser().resolve()})
        _err(f"Deleted the downloaded payload under {destination} "
             "(pass --keep-on-mismatch to inspect it instead).")
    return EXIT_FAIL


def _remove_payload(destination: Path, keep: set[Path]) -> None:
    """Delete everything under ``destination`` except explicitly kept files.

    The destination itself is removed when nothing is left in it, so a refused
    fetch leaves no empty directory that a later run could mistake for a pack.
    """
    for child in sorted(destination.iterdir(), key=lambda p: p.name):
        if child.resolve() in keep:
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError:
            pass
    try:
        if not any(destination.iterdir()):
            destination.rmdir()
    except OSError:
        pass


BACKTEST_EPILOG = """\
strategy spec fields (JSON object)
  universe           required  string, the point-in-time universe name
  benchmark          optional  one of: """ + ", ".join(VALID_BENCHMARKS) + """
  top_n              optional  integer >= 1 (default 20)
  direction          optional  one of: """ + ", ".join(VALID_DIRECTIONS) + """
  lookback_sessions  optional  integer >= 1, trailing return window (default 63)
  cadence            optional  one of: """ + ", ".join(VALID_CADENCES) + """
  stop_loss_pct      optional  number in percent units, 0 < x < 100, or null
  take_profit_pct    optional  number in percent units, 0 < x < 100, or null
  cost_model         optional  one of: """ + ", ".join(VALID_COST_MODELS) + """
  start              optional  ISO date 'YYYY-MM-DD' (inclusive)
  end                optional  ISO date 'YYYY-MM-DD' (inclusive)
  name               optional  string label for the run
  description        optional  string, free text for humans

  An unknown field is an error, never ignored: a typo that silently drops a
  stop-loss would change the result without changing the file.

exports
  --out PATH  writes {"summary": {...}, "result": {...}} as JSON, including
              the full equity curve, trades, exits and skips.
"""


def cmd_backtest(args: argparse.Namespace) -> int:
    spec = load_spec(args.spec)
    label = spec.get("name") or args.spec.stem
    _headline(f"Backtest: {label}")
    for key in ("universe", "benchmark", "top_n", "direction",
                "lookback_sessions", "cadence", "stop_loss_pct",
                "take_profit_pct", "cost_model", "start", "end"):
        if key in spec and spec[key] is not None:
            _out(f"  {key:18s} {spec[key]}")
    _out("")

    summary = run_backtest(spec, args.pack.expanduser() if args.pack else None,
                           args.out)
    _out(f"Ran {summary['sessions']} sessions, "
         f"{summary['rebalances']} rebalances"
         f"{'' if summary['cadence_complete'] else ' (cadence incomplete)'}.")
    _out(f"  trades            {summary['trades']}")
    _out(f"  exits             {summary['exits']}")
    _out(f"  skips             {summary['skips']}")
    _out(f"  final cash        {summary['final_cash']}")
    _out(f"  final equity      {summary['final_equity']}")
    if summary["total_return_pct"] is not None:
        _out(f"  total return      {summary['total_return_pct']}%")
    if summary["unpriceable_sessions"]:
        _out(f"  NOTE: equity unknown on {summary['unpriceable_sessions']} "
             "session(s) because a holding was delisted and unpriceable.")
    if summary.get("output"):
        _out(f"  written to        {summary['output']}")
    _out("")
    _out("This is a research preview. Split/bonus-adjusted prices only, "
         "research universe is not official Nifty membership.")
    return EXIT_OK


SWEEP_EPILOG = """\
scripts used
  research/run_sweep.py    run one shard of the parameter grid -> shard .jsonl
  research/merge_sweep.py  merge all shards -> FINAL_RESULTS.csv, SWEEP_REPORT.md

typical two-step run
  nse-alpha sweep run   --shard 0 --shards 4
  nse-alpha sweep run   --shard 1 --shards 4
  nse-alpha sweep merge

  The merge step reads every shard written by the run step, so run all shards
  before merging.

exit status
  0  the script finished successfully
  1  the script was not found, or it exited non-zero
"""


def _research_script(name: str) -> Path:
    script = RESEARCH_DIR / name
    if not script.is_file():
        raise CliError(
            f"research script not found: {script}\n"
            "The sweep harness lives in research/ and ships with the research\n"
            "bundle. If you are on a source checkout, restore research/ from\n"
            "the research branch or the data-pack release, then re-run.")
    return script


def _child_env() -> dict:
    """Environment for a research child process, with ``src`` importable.

    The sweep scripts ``import nse_pipeline`` and use a relative ``PYTHONPATH``
    in the documented invocation. A relative entry stops resolving once the
    child's working directory changes to ``research/``, so the repository's
    ``src`` directory is prepended as an absolute path and any relative entries
    are re-anchored against the caller's directory.
    """
    env = dict(os.environ)
    caller_cwd = Path.cwd()
    entries: list[str] = [str((REPO_ROOT / "src").resolve())]
    for entry in env.get("PYTHONPATH", "").split(os.pathsep):
        if not entry.strip():
            continue
        path = Path(entry).expanduser()
        resolved = (path if path.is_absolute() else (caller_cwd / path))
        text = str(resolved.resolve())
        if text not in entries:
            entries.append(text)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    return env


def cmd_sweep(args: argparse.Namespace) -> int:
    script_name = "run_sweep.py" if args.action == "run" else "merge_sweep.py"
    script = _research_script(script_name)

    command = [sys.executable, str(script)]
    if args.action == "run":
        if args.shard is not None:
            command += ["--shard", str(args.shard)]
        if args.shards is not None:
            command += ["--shards", str(args.shards)]
        for flag, value in (("--limit", args.limit), ("--match", args.match),
                            ("--family", args.family),
                            ("--cadence", args.cadence),
                            ("--universe", args.universe),
                            ("--tag", args.tag), ("--out", args.out)):
            if value is not None:
                command += [flag, str(value)]
    else:
        if args.shard is not None or args.shards is not None:
            raise CliError(
                "'merge' does not take --shard/--shards; every shard written by "
                "'run' is merged. Drop those flags.", EXIT_USAGE)

    _headline("Sweep" if args.action == "run" else "Sweep merge")
    _out(f"Running: {' '.join(command)}")
    _out("")
    if args.action == "run":
        _out("A shard writes rows as it goes; a long run is expected to take")
        _out("minutes to hours depending on the grid and the data pack size.")
        _out("")

    completed = subprocess.run(command, cwd=str(RESEARCH_DIR), check=False,
                               env=_child_env())
    if completed.returncode != 0:
        _err("")
        _err(f"FAILED: {script_name} exited with status {completed.returncode}.")
        return EXIT_FAIL
    _out("")
    _out(f"Done: {script_name} exited 0.")
    if args.action == "run":
        _out("Next: run the remaining shards, then 'nse-alpha sweep merge'.")
    else:
        _out(f"Results written under {RESEARCH_DIR}.")
    return EXIT_OK


def _display_path(path: Path) -> str:
    """Shorten a path to repository-relative form when it lives in the repo.

    Absolute paths leak the author's home directory into copy-pasted output and
    release notes, so inside the repository the relative form is preferred.
    """
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def list_universes() -> list[str]:
    """Universe names this build knows about, from the sweep's own vocabulary."""
    return ["nifty50", "nifty500", "msci400", "midcap150", "smallcap250"]


def find_data_packs(search_root: Path) -> list[Path]:
    """Directories under ``search_root`` that carry a manifest we recognise."""
    found: list[Path] = []
    if not search_root.is_dir():
        return found
    for name in DEFAULT_MANIFEST_NAMES:
        for candidate in sorted(search_root.rglob(name)):
            pack = candidate.parent
            if pack not in found:
                found.append(pack)
    return found


def find_specs() -> list[Path]:
    """Bundled example specs, sorted by file name."""
    if not EXAMPLES_DIR.is_dir():
        return []
    return sorted(EXAMPLES_DIR.glob("*.json"))


def cmd_list(args: argparse.Namespace) -> int:
    _headline("Universes")
    root = data_root()
    for universe in list_universes():
        _out(f"  {universe}")

    _out("")
    _headline("Data on disk")
    _out(f"Data root: {_display_path(root)}")
    bars = root / "processed" / "adjusted" / "adjusted_bars.parquet"
    if bars.is_file():
        _out(f"  adjusted bars   {_display_path(bars)}  "
             f"({bars.stat().st_size} bytes)")
    else:
        _out(f"  adjusted bars   NOT FOUND ({_display_path(bars)})")
        _out("                  fetch it with: nse-alpha fetch --help")

    packs = find_data_packs(root)
    if packs:
        _out("  data packs      " + ", ".join(_display_path(p) for p in packs))
    else:
        _out("  data packs      none found (looking for "
             f"{', '.join(DEFAULT_MANIFEST_NAMES)})")

    _out("")
    _headline("Example specs")
    _out(f"Directory: {_display_path(EXAMPLES_DIR)}")
    specs = find_specs()
    if not specs:
        _out("  no example specs found")
        return EXIT_OK if not args.strict else EXIT_FAIL
    width = max(len(path.stem) for path in specs)
    failures = 0
    for path in specs:
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
            problems = validate_spec(spec)
        except (OSError, json.JSONDecodeError) as exc:
            problems = [f"  unreadable: {exc}"]
        if problems:
            failures += 1
            _out(f"  {path.stem:<{width}}  INVALID")
            for problem in problems:
                _out(f"    {problem}")
            continue
        summary = ", ".join(
            f"{key}={spec[key]}" for key in
            ("universe", "top_n", "direction", "cadence", "stop_loss_pct")
            if key in spec and spec[key] is not None)
        _out(f"  {path.stem:<{width}}  {summary}")
        _out(f"  {'':<{width}}  run: nse-alpha backtest {_display_path(path)}")
    if args.strict and failures:
        _err("")
        _err(f"FAILED: {failures} bundled spec(s) are invalid.")
        return EXIT_FAIL
    return EXIT_OK


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """The whole CLI surface, built with stdlib argparse only."""
    parser = argparse.ArgumentParser(
        prog="nse-alpha",
        description=(
            "NSE momentum rotation research lab: verify a data pack, fetch it, "
            "run a single strategy spec, drive the parameter sweep, and list "
            "what is available."),
        epilog=(
            "Every subcommand has its own --help explaining its inputs and exit "
            "status. Examples:\n"
            "  nse-alpha list\n"
            "  nse-alpha verify --pack data\n"
            "  nse-alpha backtest examples/specs/primary.json\n"
            "  nse-alpha sweep run --shard 0 --shards 4\n"
            "  nse-alpha sweep merge\n"
            "\nResults are research previews, not investment advice: prices are\n"
            "split/bonus-adjusted only (not total return) and the research\n"
            "universe is not official Nifty membership."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version",
                        version="nse-alpha 0.1.0")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND",
                                       title="commands")

    # -- verify ---------------------------------------------------------
    verify = subparsers.add_parser(
        "verify",
        help="verify a data pack against its manifest (SHA256 + size)",
        description=(
            "Verify every file listed in a data pack manifest: the file exists, "
            "its byte size matches and its SHA256 matches. Git is never used."),
        epilog=VERIFY_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    verify.add_argument("--pack", type=Path, default=None, metavar="DIR",
                        help="data pack directory (default: the repository root)")
    verify.add_argument("--manifest", type=Path, default=None, metavar="PATH",
                        help="manifest JSON file (default: "
                             f"{', '.join(DEFAULT_MANIFEST_NAMES)} inside --pack)")
    verify.set_defaults(handler=cmd_verify)

    # -- fetch ----------------------------------------------------------
    fetch = subparsers.add_parser(
        "fetch",
        help="download the data pack, optionally verifying hashes",
        description=(
            "Download or copy the data pack into --dest. With --manifest the "
            "downloaded files are hash-verified and a mismatch refuses the pack."),
        epilog=FETCH_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    fetch.add_argument("--source", choices=FETCH_SOURCES, default="local",
                       help="where to fetch from (default: local)")
    fetch.add_argument("--from", dest="source_ref", default=None, metavar="REF",
                       help="local path (local), URL (url), OWNER/DATASET "
                            "(kaggle) or REPO_ID (hf). Required for every source.")
    fetch.add_argument("--dest", type=Path, default=None, metavar="DIR",
                       help="destination directory (default: <data root>/pack)")
    fetch.add_argument("--manifest", type=Path, default=None, metavar="PATH",
                       help="manifest to verify the download against")
    fetch.add_argument("--force", action="store_true",
                       help="overwrite a non-empty destination")
    fetch.add_argument("--keep-on-mismatch", action="store_true",
                       help="keep the payload when verification fails "
                            "(default: delete it so a bad pack cannot be used)")
    fetch.set_defaults(handler=cmd_fetch)

    # -- backtest -------------------------------------------------------
    backtest = subparsers.add_parser(
        "backtest",
        help="run one strategy spec from a JSON file",
        description=(
            "Load one strategy spec, validate it, and run it through the "
            "momentum rotation engine. Invalid specs are refused with every "
            "problem listed and the valid values named."),
        epilog=BACKTEST_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    backtest.add_argument("spec", type=Path, metavar="SPEC_JSON",
                          help="path to a strategy spec JSON file "
                               f"(examples in {EXAMPLES_DIR})")
    backtest.add_argument("--pack", type=Path, default=None, metavar="DIR",
                          help="data pack directory holding adjusted_bars.parquet "
                               "(default: <data root>/processed/adjusted)")
    backtest.add_argument("--out", type=Path, default=None, metavar="PATH",
                          help="write the full result (equity, trades, exits) "
                               "as JSON to this file")
    backtest.set_defaults(handler=cmd_backtest)

    # -- sweep ----------------------------------------------------------
    sweep = subparsers.add_parser(
        "sweep",
        help="drive the parameter sweep (run shards, then merge)",
        description=(
            "Thin wrapper around research/run_sweep.py and "
            "research/merge_sweep.py that prints what it runs and reports the "
            "child process's exit status."),
        epilog=SWEEP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sweep.add_argument("action", choices=("run", "merge"),
                       help="run a sweep shard, or merge all shards")
    sweep.add_argument("--shard", type=int, default=None,
                       help="shard index for 'run' (0-based)")
    sweep.add_argument("--shards", type=int, default=None,
                       help="total shard count for 'run'")
    sweep.add_argument("--limit", type=int, default=None,
                       help="run at most N configurations (for a smoke test)")
    sweep.add_argument("--match", default=None,
                       help="substring filter on the configuration label")
    sweep.add_argument("--family", default=None,
                       help="restrict to one sweep family")
    sweep.add_argument("--cadence", default=None,
                       help="restrict to one rebalance cadence")
    sweep.add_argument("--universe", default=None,
                       help="restrict to one universe")
    sweep.add_argument("--tag", default=None,
                       help="tag written into the shard file name")
    sweep.add_argument("--out", default=None,
                       help="explicit shard output path for 'run'")
    sweep.set_defaults(handler=cmd_sweep)

    # -- list -----------------------------------------------------------
    list_parser = subparsers.add_parser(
        "list",
        help="list universes, data packs on disk and bundled example specs",
        description=(
            "Show the universes this build knows, whether the adjusted bars and "
            "any data pack are present under the data root, and every bundled "
            "example spec with a validation verdict."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    list_parser.add_argument("--strict", action="store_true",
                             help="exit 1 if any bundled spec is invalid or "
                                  "none are present (useful in CI)")
    list_parser.set_defaults(handler=cmd_list)

    return parser


def _apply_fetch_defaults(args: argparse.Namespace) -> None:
    """Fill the fetch defaults that depend on the parsed destination."""
    if args.source_ref is None:
        raise CliError(
            "--from is required: pass a filesystem path for --source local, a "
            "URL for --source url, OWNER/DATASET for --source kaggle, or "
            "REPO_ID for --source hf.", EXIT_USAGE)
    if args.source == "kaggle" and "/" not in args.source_ref:
        raise CliError(
            f"--from for --source kaggle must be OWNER/DATASET, "
            f"got {args.source_ref!r}", EXIT_USAGE)
    if args.source == "url" and not args.source_ref.lower().startswith(
            ("http://", "https://")):
        raise CliError(
            f"--from for --source url must be an http:// or https:// URL, "
            f"got {args.source_ref!r}", EXIT_USAGE)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m nse_pipeline`` and the ``nse-alpha`` script.

    Returns an exit code rather than raising: ``argparse`` reports usage errors
    and ``--help`` by raising ``SystemExit``, which is converted here so every
    failure path has one consistent contract and no traceback reaches a user.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # --help exits 0; a usage error exits 2 with its own message already
        # printed by argparse.
        return int(exc.code or EXIT_OK)

    if getattr(args, "command", None) is None:
        parser.print_help()
        return EXIT_USAGE

    try:
        if args.command == "fetch":
            if args.dest is None:
                args.dest = data_root() / "pack"
            _apply_fetch_defaults(args)
        return int(args.handler(args))
    except CliError as exc:
        _err("")
        _err(f"error: {exc}")
        return exc.exit_code
    except KeyboardInterrupt:
        _err("")
        _err("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
