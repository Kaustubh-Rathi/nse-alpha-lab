"""Third-party Nifty 50 weights from Kaggle — exact-date observations only.

Source: ``megachonk/historical-nifty-50-constituent-weights-20y`` downloaded
via the Kaggle CLI into ``data/raw/kaggle/``. The file claims monthly Nifty 50
constituent weights from 2008-01-31, in wide layout (``DATE`` + one column per
ticker, ``0`` = not a constituent). Every observation is labelled
``THIRDPARTY-KAGGLE`` and is *never* official membership.

Trust rules (mirror ``nifty_alt`` / ``nifty_proxy``):

- Membership kind is ``thirdparty_kaggle_exact_date_observation``. An input
  claiming ``dated_official_snapshot`` is rejected: Kaggle re-publishes and
  re-derives weights (the README documents log-linear extrapolation for
  incoming/outgoing stocks, corporate-action adjustments and a 2013
  recalculation), so nothing here is a dated official snapshot.
- Lookups are exact-date only. Gaps are unknown, never forward/backward
  filled — including the 18 month-windows with no Kaggle row at all and any
  official October date Kaggle does not contain exactly (Kaggle dates are
  calendar month-ends and rebalance effective dates, while official reports
  use the last trading day, e.g. 2009-10-30 vs Kaggle 2009-10-31).
- Stored symbols are the raw Kaggle tickers. The README-documented historical
  aliases (``INFOSYSTCH`` -> ``INFY``, ``HEROHONDA`` -> ``HEROMOTOCO``,
  ``BAJAJ-AUTO`` -> ``BAJAJAUTO``, ``SSLT`` -> ``VEDL``, plus ``REL`` ->
  ``RELINFRA`` and ``ZOMATO`` -> ``ETERNAL``) are applied *only* for the
  comparison against official snapshots (official symbols harmonised to the
  modern Kaggle ticker), never by rewriting stored data.

Run: ``uv run python src/kaggle_nifty.py``
Tests: ``uv run python -m unittest tests.test_kaggle_nifty -v``
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from nse_pipeline.paths import data_root

THIRDPARTY_LABEL = "THIRDPARTY-KAGGLE"
MEMBERSHIP_KIND = "thirdparty_kaggle_exact_date_observation"
DATASET_ID = "megachonk/historical-nifty-50-constituent-weights-20y"
DATASET_URL = "https://www.kaggle.com/datasets/megachonk/historical-nifty-50-constituent-weights-20y"
LICENSE_ID = "CC-BY-NC-SA-4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/"
DEFAULT_CUTOFF = date(2026, 11, 7)
SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9&.\-]*\Z")

# Official historical symbol -> Kaggle modern ticker, per the dataset README
# "TICKER & NAMING CONVENTIONS" mapping. Official snapshots predate the
# modern tickers, so for comparison the *official* symbols are harmonised to
# the Kaggle ticker. Used for comparison only; stored symbols stay raw.
HISTORICAL_TO_KAGGLE = {
    "INFOSYSTCH": "INFY",
    "HEROHONDA": "HEROMOTOCO",
    "BAJAJ-AUTO": "BAJAJAUTO",
    "SSLT": "VEDL",
}
# README also maps REL -> RELINFRA (Kaggle column is already RELINFRA) and
# ZOMATO -> ETERNAL (Kaggle column is already ETERNAL); no official
# historical symbols for those appear in our 26 snapshots, so no alias entry.

RAW_DIR = data_root() / "raw/kaggle"
PROCESSED_DIR = data_root() / "processed/kaggle_nifty"
OFFICIAL_DIR = data_root() / "processed/index_membership"
COVERAGE_PATH = data_root() / "reports/kaggle-nifty-coverage.json"
RESEARCH_PATH = data_root() / "reports/kaggle-nifty-research.md"

WEIGHT_SUM_TOLERANCE = 0.01  # percentage points; Kaggle claims validation to 1e-6
CUTOFF = DEFAULT_CUTOFF


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_symbol(symbol: str) -> str:
    """Modern Kaggle ticker for an official historical symbol (comparison only)."""
    return HISTORICAL_TO_KAGGLE.get(symbol, symbol)


@dataclass(frozen=True)
class KaggleObservation:
    """One exact-date weight observation: non-zero constituents only."""

    as_of: date
    weights: tuple  # tuple[tuple[symbol, weight_pct], ...] sorted by symbol
    metadata: dict


def _parse_weights_csv(content: bytes):
    """Parse wide weights.csv bytes -> (tickers, [(date, [weights])])."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("weights.csv is not UTF-8") from exc
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError("weights.csv is empty")
    header = [c.strip() for c in rows[0]]
    if not header or header[0] != "DATE":
        raise ValueError("weights.csv must start with a DATE column")
    tickers = header[1:]
    if not tickers:
        raise ValueError("weights.csv has no ticker columns")
    if len(set(tickers)) != len(tickers):
        raise ValueError("duplicate ticker column")
    for ticker in tickers:
        if not SYMBOL.fullmatch(ticker):
            raise ValueError(f"invalid ticker: {ticker!r}")
    parsed = []
    seen_dates = set()
    for row in rows[1:]:
        if not row or all(not c.strip() for c in row):
            continue
        if len(row) != len(header):
            raise ValueError(f"weights row has {len(row)} fields, expected {len(header)}")
        try:
            day = date.fromisoformat(row[0].strip())
        except ValueError:
            raise ValueError(f"invalid DATE value: {row[0]!r}") from None
        if day in seen_dates:
            raise ValueError(f"duplicate DATE row: {day}")
        seen_dates.add(day)
        weights = []
        for raw in row[1:]:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"non-numeric weight {raw!r} on {day}") from None
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"non-finite weight {raw!r} on {day}")
            if value < 0:
                raise ValueError(f"negative weight {value} on {day}")
            weights.append(value)
        parsed.append((day, weights))
    if not parsed:
        raise ValueError("weights.csv has no data rows")
    dates = [day for day, _ in parsed]
    if dates != sorted(dates):
        raise ValueError("weights.csv dates must be ascending")
    return tickers, parsed


def validate_observation(obs: KaggleObservation, cutoff: date = CUTOFF) -> None:
    """Fail closed on label confusion, cutoff, counts, duplicates, sums."""
    meta = obs.metadata
    if meta.get("membership_kind") == "dated_official_snapshot":
        raise ValueError("Kaggle data must never claim dated_official_snapshot")
    if meta.get("membership_kind") != MEMBERSHIP_KIND:
        raise ValueError(f"unsupported membership_kind: {meta.get('membership_kind')!r}")
    if meta.get("source_label") != THIRDPARTY_LABEL:
        raise ValueError("observations must carry the THIRDPARTY-KAGGLE label")
    if meta.get("is_official") is not False:
        raise ValueError("Kaggle observations are never official")
    if obs.as_of > cutoff:
        raise ValueError("observation is after cutoff")
    if meta.get("as_of") != obs.as_of.isoformat():
        raise ValueError("as_of differs from metadata")
    symbols = [symbol for symbol, _ in obs.weights]
    if not symbols:
        raise ValueError("observation has no constituents")
    if len(symbols) != len(set(symbols)):
        raise ValueError("duplicate symbol in observation")
    for symbol, weight in obs.weights:
        if not SYMBOL.fullmatch(symbol):
            raise ValueError(f"invalid symbol: {symbol!r}")
        if not (0 < weight <= 100):
            raise ValueError(f"weight out of range (0, 100]: {symbol}={weight}")
    weight_sum = sum(w for _, w in obs.weights)
    if abs(weight_sum - 100.0) > WEIGHT_SUM_TOLERANCE:
        raise ValueError(f"weights sum to {weight_sum:.6f}, expected ~100")
    count = meta.get("expected_security_count")
    if type(count) is not int or count != len(obs.weights):
        raise ValueError("security count differs from declared count")


def import_weights_zip(zip_path: Path, provenance: dict,
                       cutoff: date = CUTOFF) -> list[KaggleObservation]:
    """Import ``weights.csv`` from the downloaded Kaggle ZIP.

    ``provenance`` must carry ``sha256`` of the ZIP bytes and
    ``retrieved_at_utc``. Zero weights (not constituents) are dropped; each
    kept row is an exact-date observation.
    """
    content = zip_path.read_bytes()
    if provenance.get("sha256") != sha256(content):
        raise ValueError("source digest mismatch")
    if provenance.get("membership_kind") == "dated_official_snapshot":
        raise ValueError("Kaggle data must never claim dated_official_snapshot")
    try:
        retrieved = datetime.fromisoformat(
            str(provenance["retrieved_at_utc"]).replace("Z", "+00:00"))
        if retrieved.utcoffset() is None:
            raise ValueError("retrieved_at_utc needs a timezone")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid retrieved_at_utc") from exc
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            member = archive.read("weights.csv")
    except KeyError:
        raise ValueError("weights.csv missing from Kaggle ZIP") from None
    tickers, parsed = _parse_weights_csv(member)
    observations = []
    for day, weights in parsed:
        constituents = tuple(sorted(
            (symbol, weight) for symbol, weight in zip(tickers, weights) if weight > 0))
        meta = {
            "index_code": "NIFTY50",
            "as_of": day.isoformat(),
            "membership_kind": MEMBERSHIP_KIND,
            "source_label": THIRDPARTY_LABEL,
            "is_official": False,
            "dataset_id": DATASET_ID,
            "dataset_url": DATASET_URL,
            "license": LICENSE_ID,
            "source_zip": zip_path.name,
            "source_zip_sha256": sha256(content),
            "source_member": "weights.csv",
            "source_member_sha256": sha256(member),
            "retrieved_at_utc": provenance["retrieved_at_utc"],
            "expected_security_count": len(constituents),
            "as_of_basis": "kaggle_date_column_exact_date",
            "extraction": ("Non-zero weights only from weights.csv wide layout; "
                           "0 = not a constituent. Stored symbols are raw "
                           "Kaggle tickers; historical aliases applied only "
                           "in official comparison, never rewritten."),
        }
        obs = KaggleObservation(day, constituents, meta)
        validate_observation(obs, cutoff)
        observations.append(obs)
    return observations


def lookup_observation(observations: list[KaggleObservation], day: date) -> KaggleObservation:
    """Exact-date lookup only: gaps are unknown, never filled."""
    matches = [obs for obs in observations if obs.as_of == day]
    if not matches:
        raise LookupError(f"unknown THIRDPARTY-KAGGLE observation for {day}")
    if len(matches) > 1:
        raise ValueError("ambiguous duplicate observations")
    return matches[0]


def precision_recall(predicted: set, truth: set) -> dict:
    tp = len(predicted & truth)
    fp = len(predicted - truth)
    fn = len(truth - predicted)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4)}


def load_official_symbols(membership_dir: Path):
    """Official symbol sets by (index_code, as_of) from the 26 snapshots."""
    mapping = {}
    catalog_path = membership_dir / "catalog.json"
    rows = []
    if catalog_path.exists():
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        rows = [(e["index_code"], e["as_of"], membership_dir / Path(e["snapshot_csv"]).name)
                for e in catalog]
    else:
        for csv_path in sorted(membership_dir.glob("NIFTY*.csv")):
            index_code, _, stem = csv_path.stem.partition("_")
            rows.append((index_code, stem, csv_path))
    for index_code, as_of, csv_path in rows:
        if index_code != "NIFTY50":
            continue
        with open(csv_path, newline="", encoding="utf-8-sig") as stream:
            symbols = {r["symbol"] for r in csv.DictReader(stream) if r.get("symbol")}
        key = (index_code, as_of)
        if key in mapping:
            raise ValueError(f"ambiguous duplicate snapshot {key}")
        mapping[key] = symbols
    return mapping


def compare_to_official(observations: list[KaggleObservation],
                        official: dict) -> dict:
    """Precision/recall per *exact-date* overlap with official Nifty 50 dates.

    Official dates with no exact Kaggle row are reported as non-overlapping —
    never nearest-date scored, never filled. Raw-ticker and
    alias-normalised metrics are both reported.
    """
    kaggle_by_date = {}
    for obs in observations:
        if obs.as_of in kaggle_by_date:
            raise ValueError(f"ambiguous duplicate observation {obs.as_of}")
        kaggle_by_date[obs.as_of] = {symbol for symbol, _ in obs.weights}
    overlap = []
    non_overlapping = []
    for (_code, as_of), truth in sorted(official.items()):
        day = date.fromisoformat(as_of)
        predicted_raw = kaggle_by_date.get(day)
        if predicted_raw is None:
            nearest = None
            if kaggle_by_date:
                nearest = min(kaggle_by_date,
                              key=lambda d: abs((d - day).days)).isoformat()
            non_overlapping.append({
                "official_as_of": as_of,
                "official_symbols": len(truth),
                "exact_kaggle_match": False,
                "nearest_kaggle_date_unscored": nearest,
                "reason": ("official last-trading-day vs Kaggle calendar "
                           "month-end/event date; no exact row, not scored"),
            })
            continue
        truth_norm = {canonical_symbol(s) for s in truth}
        raw = precision_recall(predicted_raw, truth)
        norm = precision_recall(predicted_raw, truth_norm)
        overlap.append({
            "as_of": as_of,
            "official_symbols": len(truth),
            "kaggle_symbols": len(predicted_raw),
            "raw": raw,
            "alias_normalised": norm,
            "kaggle_only_raw": sorted(predicted_raw - truth),
            "official_only_raw": sorted(truth - predicted_raw),
            "is_official": False,
            "source_label": THIRDPARTY_LABEL,
        })
    def _aggregate(rows, key_name):
        if not rows:
            return {"snapshots": 0}
        tp = sum(r[key_name]["tp"] for r in rows)
        fp = sum(r[key_name]["fp"] for r in rows)
        fn = sum(r[key_name]["fn"] for r in rows)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        return {"snapshots": len(rows), "tp": tp, "fp": fp, "fn": fn,
                "micro_precision": round(precision, 4),
                "micro_recall": round(recall, 4),
                "mean_precision": round(sum(r[key_name]["precision"] for r in rows) / len(rows), 4),
                "mean_recall": round(sum(r[key_name]["recall"] for r in rows) / len(rows), 4)}
    return {
        "exact_date_overlaps": overlap,
        "non_overlapping_official_dates": non_overlapping,
        "aggregate_raw": _aggregate(overlap, "raw"),
        "aggregate_alias_normalised": _aggregate(overlap, "alias_normalised"),
        "policy": ("Exact-date only: official dates without an identical "
                   "Kaggle DATE are listed as non-overlapping and never "
                   "scored against a nearby Kaggle row."),
    }


def build_provenance(zip_path: Path, retrieved_at_utc: str) -> dict:
    """Provenance record for the raw ZIP (no credentials stored)."""
    content = zip_path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        member_hashes = {name: sha256(archive.read(name)) for name in names}
    return {
        "dataset_id": DATASET_ID,
        "dataset_url": DATASET_URL,
        "license": LICENSE_ID,
        "license_url": LICENSE_URL,
        "creator": "Sukrit Bera",
        "citation_doi": "https://doi.org/10.6084/m9.figshare.30217915",
        "download_command": ("kaggle datasets download "
                             f"-d {DATASET_ID} -p data/raw/kaggle/"),
        "downloaded_at_utc": retrieved_at_utc,
        "zip_file": zip_path.name,
        "zip_size_bytes": len(content),
        "zip_sha256": sha256(content),
        "archive_members": names,
        "member_sha256": member_hashes,
        "membership_kind": MEMBERSHIP_KIND,
        "source_label": THIRDPARTY_LABEL,
        "is_official": False,
        "note": ("Downloaded with the locally configured Kaggle CLI; no "
                 "tokens or credentials copied into this repository. "
                 "Underlying NSE Indices data rights do not follow from "
                 "the Kaggle/CC grant — see coverage JSON licence block."),
    }


def write_outputs(observations: list[KaggleObservation], processed_dir: Path):
    """Write canonical long-form CSVs. Returns (obs_path, summary_path)."""
    processed_dir.mkdir(parents=True, exist_ok=True)
    obs_path = processed_dir / "observations.csv"
    summary_path = processed_dir / "snapshot_summary.csv"
    with obs_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("date", "symbol", "weight_pct",
                         "source_label", "is_official", "membership_kind"))
        for obs in sorted(observations, key=lambda o: (o.as_of,)):
            for symbol, weight in obs.weights:
                writer.writerow((obs.as_of.isoformat(), symbol, repr(float(weight)),
                                 THIRDPARTY_LABEL, "false", MEMBERSHIP_KIND))
    with summary_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("date", "n_constituents", "weight_sum", "weight_sum_deviation",
                         "source_label", "is_official", "membership_kind"))
        for obs in sorted(observations, key=lambda o: (o.as_of,)):
            total = sum(w for _, w in obs.weights)
            writer.writerow((obs.as_of.isoformat(), len(obs.weights),
                             repr(float(total)), repr(float(abs(total - 100.0))),
                             THIRDPARTY_LABEL, "false", MEMBERSHIP_KIND))
    return obs_path, summary_path


def build_all(zip_path: Path = RAW_DIR / "historical-nifty-50-constituent-weights-20y.zip",
              raw_dir: Path = RAW_DIR,
              processed_dir: Path = PROCESSED_DIR,
              official_dir: Path = OFFICIAL_DIR,
              coverage_path: Path = COVERAGE_PATH,
              research_path: Path = RESEARCH_PATH) -> dict:
    retrieved_at = datetime.now(timezone.utc).isoformat()
    provenance = build_provenance(zip_path, retrieved_at)
    (raw_dir / "provenance.json").parent.mkdir(parents=True, exist_ok=True)
    (raw_dir / "provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8")

    observations = import_weights_zip(
        zip_path, {"sha256": provenance["zip_sha256"],
                   "retrieved_at_utc": retrieved_at})
    obs_path, summary_path = write_outputs(observations, processed_dir)

    tickers = sorted({symbol for obs in observations for symbol, _ in obs.weights})
    weight_sums = [sum(w for _, w in obs.weights) for obs in observations]
    counts = [len(obs.weights) for obs in observations]
    date_range = [min(obs.as_of for obs in observations).isoformat(),
                  max(obs.as_of for obs in observations).isoformat()]
    official = load_official_symbols(official_dir)
    comparison = compare_to_official(observations, official)

    coverage = {
        "label": THIRDPARTY_LABEL,
        "is_official_nifty": False,
        "membership_kind": MEMBERSHIP_KIND,
        "generated_at": retrieved_at,
        "source": provenance,
        "validation": {
            "date_range": date_range,
            "observation_dates": len(observations),
            "distinct_dates": len({obs.as_of for obs in observations}),
            "distinct_tickers": len(tickers),
            "tickers": tickers,
            "observation_rows_long_form": sum(counts),
            "constitutents_per_date": {"min": min(counts), "max": max(counts),
                                       "mean": round(sum(counts) / len(counts), 4)},
            "weight_sum": {"min": min(weight_sums), "max": max(weight_sums),
                           "max_abs_deviation_from_100": max(abs(s - 100.0) for s in weight_sums)},
            "alias_map_historical_to_kaggle": dict(HISTORICAL_TO_KAGGLE),
        },
        "official_comparison": comparison,
        "license": {
            "dataset_license": LICENSE_ID,
            "license_url": LICENSE_URL,
            "attribution": ("Sukrit Bera, Historical Nifty 50 Constituent "
                            "Weights (Rolling 20-Year Window), Kaggle / figshare "
                            "10.6084/m9.figshare.30217915"),
            "implications": [
                "CC BY-NC-SA 4.0: non-commercial use only; commercial use "
                "needs a separate licence from the dataset creator.",
                "Attribution required in any redistribution/adaptation.",
                "Share-alike: derivatives must be under the same "
                "non-commercial terms.",
                "The Kaggle/CC grant does not override NSE Indices data "
                "terms for the underlying official reports; this dataset "
                "re-derives weights (extrapolation + corporate-action "
                "adjustments), so it is third-party data, not NSE-published "
                "history.",
                "Do not merge into official snapshots or present as "
                "dated_official_snapshot.",
                "Rolling 20-year window: oldest months are dropped as the "
                "dataset is maintained.",
                "KEEP the original LICENSE.txt inside the retained ZIP and "
                "attribute whenever these rows are used or republished.",
            ],
        },
        "limitations": [
            "Membership (non-zero pattern) matches official snapshots at all "
            "exact-overlap dates after documented alias normalisation; the "
            "weights themselves are creator-derived (extrapolation for "
            "incoming/outgoing names, corporate-action and 2013 "
            "recalculations) and not directly validated against official "
            "weight tables.",
            "18 calendar months have no Kaggle row (gaps unknown, never filled).",
            "Only exact-date overlaps are scored; official last-trading-day "
            "dates that differ from Kaggle calendar dates are unscored.",
            "Nifty 50 only; no Nifty 500 coverage.",
        ],
        "outputs": {
            "observations_csv": obs_path.as_posix(),
            "snapshot_summary_csv": summary_path.as_posix(),
            "provenance_json": (raw_dir / "provenance.json").as_posix(),
            "raw_zip": zip_path.as_posix(),
        },
    }
    coverage_path.parent.mkdir(parents=True, exist_ok=True)
    coverage_path.write_text(json.dumps(coverage, indent=2) + "\n", encoding="utf-8")
    research_path.write_text(_render_research(coverage), encoding="utf-8")
    return coverage


def _render_research(coverage: dict) -> str:
    v = coverage["validation"]
    agg_raw = coverage["official_comparison"]["aggregate_raw"]
    agg_norm = coverage["official_comparison"]["aggregate_alias_normalised"]
    overlaps = coverage["official_comparison"]["exact_date_overlaps"]
    non_over = coverage["official_comparison"]["non_overlapping_official_dates"]
    lines = [
        "# Kaggle third-party Nifty 50 weights: validation",
        "",
        "Research date: **2026-11-08**. Label: **THIRDPARTY-KAGGLE** — exact-date",
        "observations only, never official membership. Source:",
        "`megachonk/historical-nifty-50-constituent-weights-20y` (CC-BY-NC-SA-4.0).",
        "",
        "## What was downloaded",
        "",
        "- Command: `kaggle datasets download -d "
        f"{DATASET_ID} -p data/raw/kaggle/` (Kaggle CLI, no credentials stored)",
        f"- ZIP: `{coverage['source']['zip_file']}` "
        f"({coverage['source']['zip_size_bytes']} bytes, "
        f"sha256 `{coverage['source']['zip_sha256'][:16]}...`)",
        f"- `weights.csv` member sha256 "
        f"`{coverage['source']['member_sha256']['weights.csv'][:16]}...`; "
        f"{len(coverage['source']['archive_members'])} archive members retained "
        "(README, LICENSE, sectors/summary, validate.py).",
        "",
        "## Coverage and integrity checks",
        "",
        f"- Dates: `{v['date_range'][0]}` → `{v['date_range'][1]}`; "
        f"{v['observation_dates']} distinct observation dates "
        f"({v['observation_rows_long_form']} long-form rows).",
        f"- Distinct tickers (union across history): {v['distinct_tickers']}.",
        f"- Constituents per date: min {v['constitutents_per_date']['min']}, "
        f"max {v['constitutents_per_date']['max']}, "
        f"mean {v['constitutents_per_date']['mean']} (expected ~50).",
        f"- Weight sums per date: min {v['weight_sum']['min']!r}, "
        f"max {v['weight_sum']['max']!r}, max |sum − 100| = "
        f"{v['weight_sum']['max_abs_deviation_from_100']!r}.",
        "- Wide layout: `DATE` + ticker weights, `0` = not a constituent.",
        "- Raw Kaggle tickers are preserved verbatim in the canonical files.",
        "",
        "## Comparison vs the 26 official snapshots",
        "",
        "Only Nifty 50 official dates are comparable (Kaggle has no Nifty 500).",
        f"Exact-date overlaps scored: {agg_raw.get('snapshots', 0)} of 19 "
        "official Nifty 50 dates. Official dates with no identical Kaggle "
        "DATE (last-trading-day vs calendar month-end) are listed as "
        "non-overlapping and never scored against a nearby row.",
        "",
        f"- **Raw tickers (micro):** TP {agg_raw.get('tp')}, FP {agg_raw.get('fp')}, "
        f"FN {agg_raw.get('fn')}; precision {agg_raw.get('micro_precision')}, "
        f"recall {agg_raw.get('micro_recall')} "
        f"(mean precision {agg_raw.get('mean_precision')}, "
        f"mean recall {agg_raw.get('mean_recall')}).",
        f"- **After documented alias normalisation "
        f"({coverage['validation']['alias_map_historical_to_kaggle']}):** "
        f"TP {agg_norm.get('tp')}, FP {agg_norm.get('fp')}, FN {agg_norm.get('fn')}; "
        f"precision {agg_norm.get('micro_precision')}, recall {agg_norm.get('micro_recall')}.",
        "",
        "Raw-ticker misses are exclusively the README-documented historical "
        "aliases (`INFOSYSTCH`/`INFY`, `HEROHONDA`/`HEROMOTOCO`, "
        "`BAJAJ-AUTO`/`BAJAJAUTO`, `SSLT`/`VEDL`); no other symbol disagrees "
        "at any overlapping date. Weights themselves are creator-derived "
        "(extrapolation for incoming/outgoing names) and are not validated "
        "against official weight tables.",
        "",
        "### Exact-date overlap rows (raw precision / recall)",
        "",
        "| as_of | official | kaggle | precision | recall |",
        "|---|---|---|---|---|",
    ]
    for row in overlaps:
        lines.append(
            f"| {row['as_of']} | {row['official_symbols']} | {row['kaggle_symbols']} "
            f"| {row['raw']['precision']} | {row['raw']['recall']} |")
    lines += [
        "",
        f"Non-overlapping official dates (no exact Kaggle row, unscored): "
        + ", ".join(r["official_as_of"] for r in non_over) + ".",
        "",
        "## License (CC BY-NC-SA 4.0)",
        "",
        "Non-commercial research use with attribution; commercial use needs a",
        "separate licence from the creator (Sukrit Bera; figshare",
        "10.6084/m9.figshare.30217915). Share-alike applies to derivatives.",
        "The Kaggle/CC grant does not override NSE Indices data terms for the",
        "underlying official reports. These rows must stay labelled",
        "THIRDPARTY-KAGGLE and must not be merged into the official",
        "snapshot series or presented as `dated_official_snapshot`. Keep the",
        "original `LICENSE.txt` with the retained ZIP.",
        "",
        "## Outputs",
        "",
        f"- `{coverage['outputs']['observations_csv']}` — "
        f"{v['observation_rows_long_form']} rows (date, symbol, weight_pct).",
        f"- `{coverage['outputs']['snapshot_summary_csv']}` — {v['observation_dates']} rows.",
        f"- `{coverage['outputs']['provenance_json']}` — download provenance.",
        "- `data/reports/kaggle-nifty-coverage.json` — machine-readable summary.",
        "- `src/kaggle_nifty.py`, `tests/test_kaggle_nifty.py`.",
        "",
        "```powershell",
        "uv run python -m unittest tests.test_kaggle_nifty -v",
        "```",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path,
                        default=RAW_DIR / "historical-nifty-50-constituent-weights-20y.zip")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--processed", type=Path, default=PROCESSED_DIR)
    parser.add_argument("--official", type=Path, default=OFFICIAL_DIR)
    parser.add_argument("--coverage", type=Path, default=COVERAGE_PATH)
    parser.add_argument("--research", type=Path, default=RESEARCH_PATH)
    args = parser.parse_args()
    summary = build_all(args.zip, args.raw_dir, args.processed, args.official,
                        args.coverage, args.research)
    print(json.dumps({
        "label": summary["label"],
        "is_official_nifty": summary["is_official_nifty"],
        "date_range": summary["validation"]["date_range"],
        "distinct_tickers": summary["validation"]["distinct_tickers"],
        "observation_dates": summary["validation"]["observation_dates"],
        "exact_overlaps": summary["official_comparison"]["aggregate_raw"],
        "alias_normalised": summary["official_comparison"]["aggregate_alias_normalised"],
    }, indent=2))


if __name__ == "__main__":
    main()
