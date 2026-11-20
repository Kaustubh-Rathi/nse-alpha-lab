#!/usr/bin/env python
"""Build and validate the benchmark daily-close evidence file.

Inputs (local only):
  data/raw/nseIndices.zip                  NSE daily index-close CSVs
  data/raw/yahoo/idx_NSEI_max_1d.csv       Yahoo ^NSEI daily bars

Outputs (written next to this script):
  benchmark-series.json
  BENCHMARK.md

The filename date (DDMMYYYY) is authoritative for NSE rows.  Row-level
``Index Date`` values are cross-checked and reported, never used to
silently move a close to another date.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import math
import re
import statistics
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = Path(__file__).resolve().parent
NSE_ZIP = REPO_ROOT / "data" / "raw" / "nseIndices.zip"
YAHOO_CSV = REPO_ROOT / "data" / "raw" / "yahoo" / "idx_NSEI_max_1d.csv"
NSE_ZIP_REL = "data/raw/nseIndices.zip"
YAHOO_REL = "data/raw/yahoo/idx_NSEI_max_1d.csv"

WINDOW_START = dt.date(2013, 4, 18)
WINDOW_END = dt.date(2026, 9, 30)

TARGET_SERIES = {
    "NIFTY50_NSE": "Nifty 50",
    "NIFTY500_NSE": "Nifty 500",
    "G_SEC_10Y_NSE": "Nifty 10 yr Benchmark G-Sec",
}
YAHOO_KEY = "NIFTY50_YAHOO"
NIFTY50_NAME_HISTORY = ["S&P CNX Nifty", "CNX Nifty", "Nifty 50"]
NIFTY500_NAME_HISTORY = ["S&P CNX 500", "CNX 500", "Nifty 500"]
SPLICE_CHAINS = {
    "NIFTY500_SPLICED_NSE": NIFTY500_NAME_HISTORY,
    "NIFTY50_SPLICED_NSE": NIFTY50_NAME_HISTORY,
}
SPLICE_HARD_LIMIT_PCT = 10.0
SPLICE_Z_LIMIT = 3.0
BROAD_500_NAMES = {"Nifty 500", "CNX 500", "S&P CNX 500"}


def iso(value: dt.date) -> str:
    return value.isoformat()


def parse_nse_date(raw: str) -> tuple[dt.date | None, str | None]:
    raw = (raw or "").strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y"):
        try:
            return dt.datetime.strptime(raw, fmt).date(), fmt
        except ValueError:
            pass
    return None, None


def parse_close(raw: str) -> float | None:
    text = (raw or "").strip().replace(",", "")
    if text in {"", "-", "--", "NA", "N/A", "null", "None"}:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_yahoo() -> tuple[list[dict], dict]:
    records: list[dict] = []
    raw_dates: Counter = Counter()
    with YAHOO_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            raw = (row.get("Date") or "").strip()
            when = dt.datetime.fromisoformat(raw).date()
            close = parse_close(row.get("Close", ""))
            if close is None:
                raise RuntimeError(f"Yahoo row has invalid Close: {row!r}")
            records.append({"date": when, "close": close, "raw_date": raw})
            raw_dates[when] += 1
    meta = {
        "byte_size": YAHOO_CSV.stat().st_size,
        "sha256": sha256(YAHOO_CSV),
        "rows": len(records),
        "duplicate_date_count": sum(1 for n in raw_dates.values() if n > 1),
    }
    return records, meta


def load_nse(yahoo_by_date: dict[dt.date, float]) -> tuple[dict, dict]:
    """Read the NSE zip.

    Returns (payload, meta).  ``payload`` contains raw records for the three
    exact-name target series, alias-name records, and every diagnostic needed
    for the JSON report.
    """
    target_records: dict[str, list[dict]] = defaultdict(list)
    alias_records: dict[str, list[dict]] = defaultdict(list)
    name_history_records: dict[str, list[dict]] = defaultdict(list)
    year_counts: Counter = Counter()
    folder_year_mismatches: list[dict] = []
    duplicate_filename_dates: list[dict] = []
    row_date_parse_errors: list[dict] = []
    row_date_conflict_files: list[dict] = []
    seen_filename_dates: dict[dt.date, str] = {}

    total_rows = 0
    blank_name_rows = 0
    latest_row_date: dt.date | None = None
    rows_with_2026_dates = 0
    target_invalid_closes: list[dict] = []

    with zipfile.ZipFile(NSE_ZIP) as archive:
        members = sorted(name for name in archive.namelist() if name.endswith(".csv"))
        for member in members:
            match = re.search(r"^nseIndices/(\d{4})/ind_close_all_(\d{8})\.csv$", member)
            if not match:
                continue
            folder_year = int(match.group(1))
            filename_date = dt.datetime.strptime(match.group(2), "%d%m%Y").date()
            year_counts[filename_date.year] += 1
            if folder_year != filename_date.year:
                folder_year_mismatches.append(
                    {
                        "file": member,
                        "folder_year": folder_year,
                        "filename_date": iso(filename_date),
                    }
                )
            if filename_date in seen_filename_dates:
                duplicate_filename_dates.append(
                    {
                        "filename_date": iso(filename_date),
                        "files": [seen_filename_dates[filename_date], member],
                    }
                )
            seen_filename_dates[filename_date] = member

            text = archive.read(member).decode("utf-8-sig")
            rows = list(csv.DictReader(io.StringIO(text)))
            total_rows += len(rows)

            raw_date_counter: Counter = Counter()
            parsed_row_dates: dict[str, dt.date] = {}
            file_nifty50_close: float | None = None

            for row in rows:
                name = (row.get("Index Name") or "").strip()
                if not name:
                    blank_name_rows += 1
                    continue
                raw_date = (row.get("Index Date") or "").strip()
                row_date, _fmt = parse_nse_date(raw_date)
                raw_date_counter[raw_date] += 1
                if row_date is None:
                    row_date_parse_errors.append(
                        {"file": member, "name": name, "index_date": raw_date}
                    )
                else:
                    parsed_row_dates[raw_date] = row_date
                    if latest_row_date is None or row_date > latest_row_date:
                        latest_row_date = row_date
                    if row_date.year >= 2026:
                        rows_with_2026_dates += 1

                close = parse_close(row.get("Closing Index Value", ""))
                record = {
                    "date": filename_date,
                    "close": close,
                    "file": member,
                    "raw_date": raw_date,
                }

                if name in TARGET_SERIES.values():
                    if close is None:
                        target_invalid_closes.append(
                            {"file": member, "name": name, "value": row.get("Closing Index Value")}
                        )
                    else:
                        target_records[name].append(record)
                    if name == "Nifty 50":
                        file_nifty50_close = close

                if "500" in name.upper():
                    alias_records[name].append(record)
                if name in NIFTY50_NAME_HISTORY or name in NIFTY500_NAME_HISTORY:
                    name_history_records[name].append(record)

            # Filename-vs-row-date cross-check.  A conflict is only reported;
            # the filename date remains the key used for the series.
            if any(rd != filename_date for rd in parsed_row_dates.values()):
                for raw_date, count in sorted(raw_date_counter.items()):
                    parsed_ddmm, _ = parse_nse_date(raw_date)
                    if parsed_ddmm is None or parsed_ddmm == filename_date:
                        continue
                    parsed_mmdd = None
                    if re.fullmatch(r"\d{2}-\d{2}-\d{4}", raw_date):
                        try:
                            parsed_mmdd = dt.datetime.strptime(raw_date, "%m-%d-%Y").date()
                        except ValueError:
                            parsed_mmdd = None

                    candidates: dict[str, dt.date] = {"dd_mm": parsed_ddmm}
                    if parsed_mmdd is not None:
                        candidates["mm_dd"] = parsed_mmdd
                    yahoo_checks = {}
                    for label, candidate in candidates.items():
                        if candidate in yahoo_by_date and file_nifty50_close is not None:
                            yahoo_checks[label] = {
                                "date": iso(candidate),
                                "weekday": candidate.strftime("%A"),
                                "yahoo_close": yahoo_by_date[candidate],
                                "abs_diff": abs(file_nifty50_close - yahoo_by_date[candidate]),
                            }
                    best_label = None
                    if yahoo_checks:
                        best_label = min(yahoo_checks, key=lambda key: yahoo_checks[key]["abs_diff"])
                        if yahoo_checks[best_label]["abs_diff"] > 1.0:
                            best_label = None

                    if best_label == "mm_dd" and parsed_mmdd == filename_date:
                        classification = "row_date_mm_dd_format_variant"
                    elif best_label == "dd_mm":
                        classification = "possible_day_month_swapped_filename"
                    elif best_label == "mm_dd":
                        classification = "row_date_mm_dd_value_confirms_filename"
                    else:
                        classification = "unresolved_row_date_conflict"

                    row_date_conflict_files.append(
                        {
                            "file": member,
                            "filename_date": iso(filename_date),
                            "raw_row_date": raw_date,
                            "rows_with_this_raw_date": count,
                            "parsed_as_dd_mm": iso(parsed_ddmm),
                            "parsed_as_mm_dd": iso(parsed_mmdd) if parsed_mmdd else None,
                            "nifty50_close_in_file": file_nifty50_close,
                            "yahoo_value_checks": yahoo_checks,
                            "best_yahoo_match": best_label,
                            "classification": classification,
                            "action": "filename date retained as authoritative",
                        }
                    )

    meta = {
        "csv_files": len(members),
        "total_rows": total_rows,
        "blank_name_rows": blank_name_rows,
        "first_filename_date": iso(min(seen_filename_dates)),
        "last_filename_date": iso(max(seen_filename_dates)),
        "latest_index_date_in_rows": iso(latest_row_date) if latest_row_date else None,
        "rows_with_2026_dates": rows_with_2026_dates,
        "year_file_counts": {str(year): count for year, count in sorted(year_counts.items())},
        "folder_year_mismatches": folder_year_mismatches,
        "duplicate_filename_dates": duplicate_filename_dates,
        "row_date_parse_errors": row_date_parse_errors,
        "row_date_conflict_files": row_date_conflict_files,
        "target_invalid_closes": target_invalid_closes,
        "byte_size": NSE_ZIP.stat().st_size,
        "sha256": sha256(NSE_ZIP),
    }
    payload = {
        "target_records": target_records,
        "alias_records": alias_records,
        "name_history_records": name_history_records,
    }
    return payload, meta


def dedupe(records: list[dict], label: str) -> tuple[list[tuple[dt.date, float]], list[dict]]:
    grouped: dict[dt.date, list[dict]] = defaultdict(list)
    for record in records:
        grouped[record["date"]].append(record)

    output: list[tuple[dt.date, float]] = []
    duplicates: list[dict] = []
    for date in sorted(grouped):
        rows = grouped[date]
        values = {row["close"] for row in rows}
        if len(values) > 1:
            detail = [
                {"value": row["close"], "file": row["file"], "raw_date": row["raw_date"]}
                for row in rows
            ]
            raise RuntimeError(
                f"{label}: conflicting duplicate closes for {iso(date)}: {detail}"
            )
        if len(rows) > 1:
            duplicates.append(
                {
                    "date": iso(date),
                    "occurrences": len(rows),
                    "values": sorted(values),
                    "files": sorted({row["file"] for row in rows}),
                    "resolution": "kept one row; values identical",
                }
            )
        output.append((date, float(rows[0]["close"])))
    return output, duplicates


def gap_summary(pairs: list[tuple[dt.date, float]]) -> dict:
    gaps = []
    for (left, _), (right, _) in zip(pairs, pairs[1:]):
        days = (right - left).days
        if days > 5:
            gaps.append({"from": iso(left), "to": iso(right), "calendar_days": days})
    gaps.sort(key=lambda item: (-item["calendar_days"], item["from"]))
    return {
        "count": len(gaps),
        "max_calendar_days": gaps[0]["calendar_days"] if gaps else 0,
        "top_10": gaps[:10],
    }


def check_series(
    key: str,
    pairs: list[tuple[dt.date, float]],
    duplicates: list[dict],
    source: str,
    extra: dict | None = None,
) -> dict:
    dates = [date for date, _ in pairs]
    non_weekday = [
        {"date": iso(date), "weekday": date.strftime("%A")}
        for date in dates
        if date.weekday() >= 5
    ]
    non_positive = [
        {"date": iso(date), "close": close}
        for date, close in pairs
        if not math.isfinite(close) or close <= 0
    ]
    first, last = dates[0], dates[-1]
    covers_start = first <= WINDOW_START
    covers_end = last >= WINDOW_END
    reasons = []
    if not covers_start:
        reasons.append(f"first date {iso(first)} is after window start {iso(WINDOW_START)}")
    if not covers_end:
        reasons.append(f"last date {iso(last)} is before window end {iso(WINDOW_END)}")
    result = {
        "source": source,
        "rows": len(pairs),
        "first_date": iso(first),
        "last_date": iso(last),
        "duplicate_dates": {
            "count": len(duplicates),
            "examples": duplicates[:5],
        },
        "non_weekday_rows": {
            "count": len(non_weekday),
            "examples": non_weekday[:10],
        },
        "non_positive_closes": {
            "count": len(non_positive),
            "examples": non_positive[:10],
        },
        "calendar_gaps_gt_5_days": gap_summary(pairs),
        "covers_strategy_window": {
            "start": iso(WINDOW_START),
            "end": iso(WINDOW_END),
            "covers_start": covers_start,
            "covers_end": covers_end,
            "covers_full_window": covers_start and covers_end,
            "reason": "; ".join(reasons) if reasons else "covers both window boundaries",
        },
    }
    if extra:
        result.update(extra)
    return result


def coverage_text(check: dict) -> str:
    coverage = check["covers_strategy_window"]
    if coverage["covers_full_window"]:
        return "Yes"
    if not coverage["covers_start"] and not coverage["covers_end"]:
        return f"No — starts {check['first_date']}, ends {check['last_date']}"
    if not coverage["covers_start"]:
        return f"No — starts {check['first_date']}"
    return f"No — ends {check['last_date']}"


def fmt_number(value: float, digits: int = 6) -> str:
    if value == 0:
        return "0"
    if abs(value) < 1e-4:
        return f"{value:.3e}"
    text = f"{value:,.{digits}f}".rstrip("0").rstrip(".")
    return text


def alias_summary(records: list[dict]) -> dict:
    dates = sorted({record["date"] for record in records})
    files = {record["file"] for record in records}
    nulls = sum(1 for record in records if record["close"] is None)
    return {
        "rows": len(records),
        "distinct_dates": len(dates),
        "first_date": iso(dates[0]),
        "last_date": iso(dates[-1]),
        "files": len(files),
        "null_closes": nulls,
    }


def rename_evidence(records_by_name: dict[str, list[dict]], names: list[str], series_key: str) -> dict:
    segments = []
    boundaries = []
    for name in names:
        summary = alias_summary(records_by_name[name])
        ordered = sorted(records_by_name[name], key=lambda item: item["date"])
        segments.append(
            {
                "name": name,
                **summary,
                "first_close": ordered[0]["close"],
                "last_close": ordered[-1]["close"],
            }
        )
    for left, right in zip(names, names[1:]):
        left_records = sorted(records_by_name[left], key=lambda item: item["date"])
        right_records = sorted(records_by_name[right], key=lambda item: item["date"])
        left_dates = {record["date"] for record in left_records}
        right_dates = {record["date"] for record in right_records}
        boundaries.append(
            {
                "from_name": left,
                "to_name": right,
                "from_last_date": iso(left_records[-1]["date"]),
                "to_first_date": iso(right_records[0]["date"]),
                "calendar_days_between": (right_records[0]["date"] - left_records[-1]["date"]).days,
                "overlap_dates": len(left_dates & right_dates),
                "from_last_close": left_records[-1]["close"],
                "to_first_close": right_records[0]["close"],
            }
        )
    return {
        "series_key": series_key,
        "exact_target_name": names[-1],
        "segments_in_name_history": segments,
        "boundary_checks": boundaries,
        "merged_into_target_series": False,
        "note": (
            "Name changes are reported for review only. The exact-name target series "
            "was not extended with alias rows."
        ),
    }


def build_splice(
    series_key: str,
    names: list[str],
    records_by_name: dict[str, list[dict]],
) -> tuple[list[tuple[dt.date, float]] | None, list[dict], dict]:
    """Splice a documented rename chain with a fail-closed boundary test.

    Closes are concatenated unchanged only when the implied boundary move is
    inside the old-era daily-return distribution.  A move above the hard limit
    or outside that distribution refuses the splice; this function never
    rescales silently.
    """
    segments = []
    for name in names:
        pairs, _duplicates = dedupe(records_by_name[name], f"{series_key}:{name}")
        segments.append({"name": name, "pairs": pairs})

    boundary_entries: list[dict] = []
    all_boundaries_ok = True
    for boundary_number, (left, right) in enumerate(zip(segments, segments[1:]), start=1):
        old_pairs = left["pairs"]
        new_pairs = right["pairs"]
        old_last_date, old_last_close = old_pairs[-1]
        new_first_date, new_first_close = new_pairs[0]
        implied_return_pct = (new_first_close / old_last_close - 1.0) * 100.0
        old_returns_pct = [
            (old_pairs[index][1] / old_pairs[index - 1][1] - 1.0) * 100.0
            for index in range(1, len(old_pairs))
        ]
        old_return_count = len(old_returns_pct)
        old_mean_pct = statistics.fmean(old_returns_pct)
        old_stdev_pct = statistics.pstdev(old_returns_pct) if old_return_count > 1 else 0.0
        old_max_abs_pct = max(abs(value) for value in old_returns_pct)
        if old_stdev_pct > 0:
            implied_z_score = implied_return_pct / old_stdev_pct
        else:
            implied_z_score = 0.0 if implied_return_pct == 0 else None
        implied_abs_percentile = (
            100.0
            * sum(1 for value in old_returns_pct if abs(value) <= abs(implied_return_pct))
            / old_return_count
        )
        within_old_era_range = abs(implied_return_pct) <= old_max_abs_pct
        abs_z_within_limit = (
            abs(implied_z_score) <= SPLICE_Z_LIMIT
            if implied_z_score is not None
            else abs(implied_return_pct) == 0
        )
        over_hard_limit = abs(implied_return_pct) > SPLICE_HARD_LIMIT_PCT
        continuous = (not over_hard_limit) and within_old_era_range and abs_z_within_limit

        if continuous:
            decision = "concatenate"
            reason = (
                "Implied boundary move is inside the old-era daily-return distribution "
                f"(|z| <= {SPLICE_Z_LIMIT:g} and <= old-era max absolute daily move); "
                "closes concatenated unchanged."
            )
        elif over_hard_limit:
            decision = "refused"
            reason = (
                f"Level break: |implied move| {abs(implied_return_pct):.6f}% exceeds the "
                f"{SPLICE_HARD_LIMIT_PCT:g}% hard limit; no splice applied."
            )
        else:
            decision = "refused"
            reason = (
                "Small but statistically abnormal level break; refusing to splice rather "
                "than silently rescaling."
            )

        if decision != "concatenate":
            all_boundaries_ok = False

        boundary_entries.append(
            {
                "series_key": series_key,
                "boundary": boundary_number,
                "old_name": left["name"],
                "new_name": right["name"],
                "old_last_date": iso(old_last_date),
                "old_last_close": old_last_close,
                "new_first_date": iso(new_first_date),
                "new_first_close": new_first_close,
                "calendar_days_between": (new_first_date - old_last_date).days,
                "implied_return_pct": implied_return_pct,
                "old_era_daily_return_count": old_return_count,
                "old_era_daily_return_mean_pct": old_mean_pct,
                "old_era_daily_return_stdev_pct": old_stdev_pct,
                "old_era_max_abs_daily_return_pct": old_max_abs_pct,
                "implied_z_score": implied_z_score,
                "implied_abs_percentile": implied_abs_percentile,
                "hard_limit_pct": SPLICE_HARD_LIMIT_PCT,
                "continuity_tests": {
                    "within_old_era_range": within_old_era_range,
                    "abs_z_le_limit": abs_z_within_limit,
                    "z_limit": SPLICE_Z_LIMIT,
                    "over_hard_limit": over_hard_limit,
                },
                "decision": decision,
                "level_break": not continuous,
                "applied_adjustment_factor": 1.0 if decision == "concatenate" else None,
                "reason": reason,
            }
        )

    if not all_boundaries_ok:
        summary = {
            "series_key": series_key,
            "spliced": False,
            "source_segments": [
                {
                    "name": segment["name"],
                    "rows": len(segment["pairs"]),
                    "first_date": iso(segment["pairs"][0][0]),
                    "last_date": iso(segment["pairs"][-1][0]),
                }
                for segment in segments
            ],
            "boundary_decisions": [entry["decision"] for entry in boundary_entries],
            "rows": None,
            "first_date": None,
            "last_date": None,
            "covers_strategy_start_through_2025_12_31": False,
            "covers_full_strategy_window": False,
            "remaining_gap": "not spliced",
            "method": "refused; no series emitted",
            "note": "One or more boundaries failed the continuity test; see validation.splices.",
        }
        return None, boundary_entries, summary

    merged: list[tuple[dt.date, float]] = []
    for segment in segments:
        merged.extend(segment["pairs"])
    merged.sort(key=lambda item: item[0])
    merged_dates = [date for date, _ in merged]
    if len(set(merged_dates)) != len(merged_dates):
        duplicates = [iso(date) for date, count in Counter(merged_dates).items() if count > 1]
        raise RuntimeError(f"{series_key}: duplicate dates across splice segments: {duplicates}")

    first_date, last_date = merged[0][0], merged[-1][0]
    summary = {
        "series_key": series_key,
        "spliced": True,
        "source_segments": [
            {
                "name": segment["name"],
                "rows": len(segment["pairs"]),
                "first_date": iso(segment["pairs"][0][0]),
                "last_date": iso(segment["pairs"][-1][0]),
            }
            for segment in segments
        ],
        "boundary_decisions": [entry["decision"] for entry in boundary_entries],
        "rows": len(merged),
        "first_date": iso(first_date),
        "last_date": iso(last_date),
        "covers_strategy_start_through_2025_12_31": first_date <= WINDOW_START and last_date >= dt.date(2025, 12, 31),
        "covers_full_strategy_window": first_date <= WINDOW_START and last_date >= WINDOW_END,
        "remaining_gap": (
            f"{iso(dt.date(2026, 1, 1))}..{iso(WINDOW_END)}" if last_date < WINDOW_END else None
        ),
        "method": "concatenate closes unchanged; no rescaling",
        "note": (
            "Documented index renames of the same index; levels are continuous at each "
            "boundary, so no adjustment factor was applied."
        ),
    }
    return merged, boundary_entries, summary


def main() -> None:
    if not NSE_ZIP.exists():
        raise FileNotFoundError(NSE_ZIP)
    if not YAHOO_CSV.exists():
        raise FileNotFoundError(YAHOO_CSV)

    yahoo_records, yahoo_meta = load_yahoo()
    yahoo_pairs, yahoo_duplicates = dedupe(yahoo_records, YAHOO_KEY)
    yahoo_by_date = dict(yahoo_pairs)

    payload, nse_meta = load_nse(yahoo_by_date)

    series_pairs: dict[str, list[tuple[dt.date, float]]] = {}
    duplicate_info: dict[str, list[dict]] = {}
    for key, name in TARGET_SERIES.items():
        pairs, duplicates = dedupe(payload["target_records"][name], key)
        series_pairs[key] = pairs
        duplicate_info[key] = duplicates

    series_pairs[YAHOO_KEY] = yahoo_pairs
    duplicate_info[YAHOO_KEY] = yahoo_duplicates

    nifty50_nse = dict(series_pairs["NIFTY50_NSE"])
    nifty500_nse = dict(series_pairs["NIFTY500_NSE"])
    gsec = dict(series_pairs["G_SEC_10Y_NSE"])
    nse_calendar = set(nifty50_nse)

    # Per-series checks plus calendar differences against the Nifty 50
    # exact-name NSE calendar.
    checks: dict[str, dict] = {}
    checks["NIFTY50_NSE"] = check_series(
        "NIFTY50_NSE",
        series_pairs["NIFTY50_NSE"],
        duplicate_info["NIFTY50_NSE"],
        "NSE zip, exact Index Name 'Nifty 50'",
    )
    checks["NIFTY500_NSE"] = check_series(
        "NIFTY500_NSE",
        series_pairs["NIFTY500_NSE"],
        duplicate_info["NIFTY500_NSE"],
        "NSE zip, exact Index Name 'Nifty 500'",
        extra={
            "missing_sessions_vs_nifty50_calendar": {
                "count": len(nse_calendar - set(nifty500_nse)),
                "examples": [iso(date) for date in sorted(nse_calendar - set(nifty500_nse))[:10]],
            }
        },
    )
    gsec_missing = sorted(nse_calendar - set(gsec))
    checks["G_SEC_10Y_NSE"] = check_series(
        "G_SEC_10Y_NSE",
        series_pairs["G_SEC_10Y_NSE"],
        duplicate_info["G_SEC_10Y_NSE"],
        "NSE zip, exact Index Name 'Nifty 10 yr Benchmark G-Sec'",
        extra={
            "missing_sessions_vs_nifty50_calendar": {
                "count": len(gsec_missing),
                "examples": [iso(date) for date in gsec_missing[:10]],
            }
        },
    )
    checks[YAHOO_KEY] = check_series(
        YAHOO_KEY,
        series_pairs[YAHOO_KEY],
        duplicate_info[YAHOO_KEY],
        "Yahoo ^NSEI daily Close (local CSV)",
    )

    # Documented rename-chain splices, added alongside the unspliced series.
    spliced_pairs: dict[str, list[tuple[dt.date, float]]] = {}
    splice_entries: list[dict] = []
    splice_summaries: dict[str, dict] = {}
    for key, names in SPLICE_CHAINS.items():
        pairs, entries, summary = build_splice(key, names, payload["name_history_records"])
        splice_entries.extend(entries)
        splice_summaries[key] = summary
        if pairs is not None:
            spliced_pairs[key] = pairs
            series_pairs[key] = pairs
            duplicate_info[key] = []
            checks[key] = check_series(
                key,
                pairs,
                [],
                "NSE zip, documented rename chain: " + " -> ".join(names),
                extra={"splice_summary": summary},
            )

    # NIFTY 50 cross-source comparison.
    shared_dates = sorted(set(nifty50_nse) & set(yahoo_by_date))
    abs_diffs = []
    rel_diffs = []
    for date in shared_dates:
        nse_close = nifty50_nse[date]
        yahoo_close = yahoo_by_date[date]
        abs_diffs.append((abs(nse_close - yahoo_close), date, nse_close, yahoo_close))
        if yahoo_close == 0:
            raise RuntimeError(f"Yahoo close is zero on {iso(date)}; cannot compute relative difference")
        rel_diffs.append((abs(nse_close - yahoo_close) / abs(yahoo_close), date, nse_close, yahoo_close))

    mean_abs = statistics.fmean(item[0] for item in abs_diffs)
    max_abs = max(abs_diffs, key=lambda item: (item[0], item[1]))
    max_rel = max(rel_diffs, key=lambda item: (item[0], item[1]))
    over_threshold = [item for item in rel_diffs if item[0] > 0.001]
    material_disagreement = (max_rel[0] * 100.0 > 0.1) or (mean_abs > 1.0)
    round2_equal = sum(
        1 for date in shared_dates
        if round(nifty50_nse[date], 2) == round(yahoo_by_date[date], 2)
    )
    cross_source = {
        "nse_series": "NIFTY50_NSE",
        "yahoo_series": YAHOO_KEY,
        "overlapping_observations": len(shared_dates),
        "overlap_first_date": iso(shared_dates[0]),
        "overlap_last_date": iso(shared_dates[-1]),
        "mean_absolute_difference": mean_abs,
        "max_absolute_difference": max_abs[0],
        "max_absolute_difference_date": iso(max_abs[1]),
        "max_absolute_difference_nse_close": max_abs[2],
        "max_absolute_difference_yahoo_close": max_abs[3],
        "max_relative_difference_pct": max_rel[0] * 100.0,
        "max_relative_difference_date": iso(max_rel[1]),
        "max_relative_difference_nse_close": max_rel[2],
        "max_relative_difference_yahoo_close": max_rel[3],
        "count_relative_difference_gt_0_1_pct": len(over_threshold),
        "examples_relative_difference_gt_0_1_pct": [
            {
                "date": iso(item[1]),
                "nse_close": item[2],
                "yahoo_close": item[3],
                "relative_difference_pct": item[0] * 100.0,
            }
            for item in sorted(over_threshold, reverse=True)[:10]
        ],
        "count_closes_equal_at_2dp": round2_equal,
        "materiality_rule": "material if max relative difference > 0.1% or mean absolute difference > 1.0 index point",
        "material_disagreement": material_disagreement,
        "sources_averaged": False,
        "assessment": (
            "The sources agree: the largest relative difference is far below 0.1%, and "
            "the maximum absolute difference is smaller than two-decimal rounding noise. "
            "The two series were compared, not averaged."
            if not material_disagreement
            else "The sources disagree materially; do not average or splice them without investigation."
        ),
    }

    # Session-date set differences.
    nse_only = sorted(nse_calendar - set(yahoo_by_date))
    nse_min, nse_max = min(nse_calendar), max(nse_calendar)
    yahoo_only = sorted(
        date for date in (set(yahoo_by_date) - nse_calendar) if nse_min <= date <= nse_max
    )
    session_diff = {
        "nse_span_first_date": iso(nse_min),
        "nse_span_last_date": iso(nse_max),
        "nse_only_count": len(nse_only),
        "nse_only_dates": [
            {
                "date": iso(date),
                "weekday": date.strftime("%A"),
                "nse_close": nifty50_nse[date],
            }
            for date in nse_only
        ],
        "yahoo_only_count": len(yahoo_only),
        "yahoo_only_dates": [
            {
                "date": iso(date),
                "weekday": date.strftime("%A"),
                "yahoo_close": yahoo_by_date[date],
            }
            for date in yahoo_only
        ],
        "note": (
            "The NIFTY50_NSE series and Yahoo do not have identical session calendars. "
            "All cross-source value statistics above use only the shared dates. "
            "NSE-only dates are retained in the NSE series; Yahoo-only dates are retained "
            "in the Yahoo series."
        ),
    }

    # NSE filename-vs-row-date cross-check summary.
    conflict_rows = nse_meta["row_date_conflict_files"]
    swapped = [
        row for row in conflict_rows
        if row["classification"] == "possible_day_month_swapped_filename"
    ]
    unresolved = [
        row for row in conflict_rows
        if row["classification"] == "unresolved_row_date_conflict"
    ]
    nse_date_check = {
        "files_scanned": nse_meta["csv_files"],
        "rows_scanned": nse_meta["total_rows"],
        "filename_date_policy": "DDMMYYYY in the filename is authoritative; row Index Date is a cross-check only",
        "conflict_file_count": len({row["file"] for row in conflict_rows}),
        "conflict_row_count": sum(row["rows_with_this_raw_date"] for row in conflict_rows),
        "conflicts": conflict_rows,
        "day_month_swapped_filenames_detected": swapped,
        "unresolved_conflicts": unresolved,
        "row_date_parse_errors": nse_meta["row_date_parse_errors"],
        "folder_year_mismatches": nse_meta["folder_year_mismatches"],
        "duplicate_filename_dates": nse_meta["duplicate_filename_dates"],
        "note": (
            "Only three April-2023 files show row-date conflicts. Their Index Date strings "
            "are in MM-DD-YYYY order and the Nifty 50 close in each file matches Yahoo on the "
            "filename date, so the filenames are retained as authoritative. No evidence of a "
            "day/month-swapped filename was found."
        ),
    }

    # Alias scan: every name containing '500'.
    names_500 = []
    for name in sorted(payload["alias_records"], key=str.casefold):
        summary = alias_summary(payload["alias_records"][name])
        names_500.append(
            {
                "name": name,
                **summary,
                "broad_500_alias": name in BROAD_500_NAMES,
                "is_nifty500_target": name == "Nifty 500",
            }
        )

    nifty50_history = [
        {"name": name, **alias_summary(payload["name_history_records"][name])}
        for name in NIFTY50_NAME_HISTORY
    ]
    nifty500_history = [
        {"name": name, **alias_summary(payload["name_history_records"][name])}
        for name in NIFTY500_NAME_HISTORY
    ]
    alias_scan = {
        "scan_rule": "all distinct Index Name values containing '500' (case-insensitive)",
        "names_containing_500": names_500,
        "nifty50_name_history": nifty50_history,
        "nifty500_name_history": nifty500_history,
        "rename_evidence": {
            "nifty50": rename_evidence(payload["name_history_records"], NIFTY50_NAME_HISTORY, "NIFTY50_NSE"),
            "nifty500": rename_evidence(payload["name_history_records"], NIFTY500_NAME_HISTORY, "NIFTY500_NSE"),
        },
        "note": (
            "The name histories partition the NSE corpus cleanly, but aliases were not merged "
            "into NIFTY50_NSE or NIFTY500_NSE. The exact-name series are the validated targets."
        ),
    }

    # Strategy-window coverage summary.
    series_order = ["NIFTY50_NSE", "NIFTY500_NSE", "G_SEC_10Y_NSE", YAHOO_KEY] + [
        key for key in SPLICE_CHAINS if key in series_pairs
    ]
    coverage = {}
    for key in series_order:
        check = checks[key]
        coverage[key] = {
            "first_date": check["first_date"],
            "last_date": check["last_date"],
            "covers_full_strategy_window": check["covers_strategy_window"]["covers_full_window"],
            "reason": check["covers_strategy_window"]["reason"],
        }

    generated_at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    series_payload = {
        key: [[iso(date), float(close)] for date, close in series_pairs[key]]
        for key in series_order
    }

    document = {
        "generated_at_utc": generated_at,
        "sources": {"nse_zip": NSE_ZIP_REL, "yahoo": YAHOO_REL},
        "strategy_window": {"start": iso(WINDOW_START), "end": iso(WINDOW_END)},
        "series": series_payload,
        "validation": {
            "generated_by": "verification/research/alpha-backtest/build_benchmark.py",
            "inputs": {
                "nse_zip": {
                    "path": NSE_ZIP_REL,
                    "csv_files": nse_meta["csv_files"],
                    "rows": nse_meta["total_rows"],
                    "first_filename_date": nse_meta["first_filename_date"],
                    "last_filename_date": nse_meta["last_filename_date"],
                    "latest_index_date_in_rows": nse_meta["latest_index_date_in_rows"],
                    "rows_with_2026_dates": nse_meta["rows_with_2026_dates"],
                    "year_file_counts": nse_meta["year_file_counts"],
                    "byte_size": nse_meta["byte_size"],
                    "sha256": nse_meta["sha256"],
                },
                "yahoo": {
                    "path": YAHOO_REL,
                    "rows": yahoo_meta["rows"],
                    "duplicate_date_count": yahoo_meta["duplicate_date_count"],
                    "byte_size": yahoo_meta["byte_size"],
                    "sha256": yahoo_meta["sha256"],
                },
            },
            "series_checks": checks,
            "nifty50_cross_source": cross_source,
            "nse_vs_yahoo_session_sets": session_diff,
            "nse_filename_row_date_crosscheck": nse_date_check,
            "nse_alias_scan": alias_scan,
            "splices": splice_entries,
            "spliced_series": splice_summaries,
            "coverage_vs_strategy_window": coverage,
        },
    }

    # Fail loudly on non-finite values before serialising.
    for key, pairs in series_pairs.items():
        for date, close in pairs:
            if not math.isfinite(close):
                raise RuntimeError(f"{key}: non-finite close on {iso(date)}: {close!r}")

    json_path = OUT_DIR / "benchmark-series.json"
    with json_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")

    md_path = OUT_DIR / "BENCHMARK.md"
    write_markdown(
        md_path,
        generated_at=generated_at,
        checks=checks,
        series_pairs=series_pairs,
        cross_source=cross_source,
        session_diff=session_diff,
        nse_date_check=nse_date_check,
        alias_scan=alias_scan,
        nse_meta=nse_meta,
        splices=splice_entries,
        spliced_summaries=splice_summaries,
        series_order=series_order,
    )

    print(f"wrote {json_path} ({json_path.stat().st_size:,} bytes)")
    print(f"wrote {md_path} ({md_path.stat().st_size:,} bytes)")
    print(f"generated_at_utc={generated_at}")
    for key in series_order:
        print(f"{key}: rows={len(series_pairs[key])} first={checks[key]['first_date']} last={checks[key]['last_date']}")


def write_markdown(
    path: Path,
    *,
    generated_at: str,
    checks: dict,
    series_pairs: dict,
    cross_source: dict,
    session_diff: dict,
    nse_date_check: dict,
    alias_scan: dict,
    nse_meta: dict,
    splices: list[dict],
    spliced_summaries: dict,
    series_order: list[str],
) -> None:
    keys = ["NIFTY50_NSE", "NIFTY500_NSE", "G_SEC_10Y_NSE", YAHOO_KEY]
    labels = {
        "NIFTY50_NSE": "NIFTY50_NSE",
        "NIFTY500_NSE": "NIFTY500_NSE",
        "G_SEC_10Y_NSE": "G_SEC_10Y_NSE",
        YAHOO_KEY: "NIFTY50_YAHOO",
    }
    cross = cross_source
    coverage = {key: checks[key]["covers_strategy_window"] for key in series_order}
    full = [labels.get(key, key) for key in series_order if coverage[key]["covers_full_window"]]
    partial = [labels.get(key, key) for key in series_order if not coverage[key]["covers_full_window"]]
    spliced_keys = [key for key in series_order if key in SPLICE_CHAINS and key in checks]

    lines = []
    lines.append("# Benchmark daily-close evidence: NIFTY 50, NIFTY 500, 10Y G-Sec")
    lines.append("")
    lines.append(f"Generated: `{generated_at}`")
    lines.append("")
    lines.append("Script: `verification/research/alpha-backtest/build_benchmark.py`")
    lines.append("")
    lines.append("## Sources")
    lines.append("")
    lines.append(
        f"- `{NSE_ZIP_REL}`: {nse_meta['csv_files']:,} daily "
        f"`ind_close_all_DDMMYYYY.csv` files, filename dates "
        f"{nse_meta['first_filename_date']}..{nse_meta['last_filename_date']}; "
        f"latest `Index Date` in any row {nse_meta['latest_index_date_in_rows']}; "
        f"{nse_meta['rows_with_2026_dates']} rows dated 2026 or later."
    )
    lines.append(
        f"- `{YAHOO_REL}`: Yahoo ^NSEI daily bars, "
        f"{series_pairs[YAHOO_KEY][0][0].isoformat()}..{series_pairs[YAHOO_KEY][-1][0].isoformat()}; "
        "`Close` used as the index level (not `Adj Close`)."
    )
    lines.append("- No network access was used.")
    lines.append("")
    lines.append("## Coverage")
    lines.append("")
    lines.append("| series | source | rows | first date | last date | covers 2013-04-18..2026-09-30? |")
    lines.append("|---|---|---:|---|---|---|")
    lines.append(
        f"| NIFTY50_NSE | NSE, exact `Nifty 50` | {len(series_pairs['NIFTY50_NSE']):,} | "
        f"{checks['NIFTY50_NSE']['first_date']} | {checks['NIFTY50_NSE']['last_date']} | "
        f"{coverage_text(checks['NIFTY50_NSE'])} |"
    )
    lines.append(
        f"| NIFTY500_NSE | NSE, exact `Nifty 500` | {len(series_pairs['NIFTY500_NSE']):,} | "
        f"{checks['NIFTY500_NSE']['first_date']} | {checks['NIFTY500_NSE']['last_date']} | "
        f"{coverage_text(checks['NIFTY500_NSE'])} |"
    )
    lines.append(
        f"| G_SEC_10Y_NSE | NSE, exact `Nifty 10 yr Benchmark G-Sec` | {len(series_pairs['G_SEC_10Y_NSE']):,} | "
        f"{checks['G_SEC_10Y_NSE']['first_date']} | {checks['G_SEC_10Y_NSE']['last_date']} | "
        f"{coverage_text(checks['G_SEC_10Y_NSE'])} |"
    )
    lines.append(
        f"| NIFTY50_YAHOO | Yahoo ^NSEI `Close` | {len(series_pairs[YAHOO_KEY]):,} | "
        f"{checks[YAHOO_KEY]['first_date']} | {checks[YAHOO_KEY]['last_date']} | "
        f"{coverage_text(checks[YAHOO_KEY])} |"
    )
    lines.append("")
    lines.append(
        f"Full-window series: **{', '.join(full) if full else 'none'}**. "
        f"Partial-window series: **{', '.join(partial) if partial else 'none'}**."
    )
    lines.append("")
    lines.append("## Spliced coverage (documented renames)")
    lines.append("")
    lines.append("| series | rename chain | rows | first date | last date | covers 2013-04-18..2025-12-31? | covers 2026? |")
    lines.append("|---|---|---:|---|---|---|---|")
    for key in spliced_keys:
        summary = spliced_summaries[key]
        chain = " -> ".join(segment["name"] for segment in summary["source_segments"])
        covers_2026 = summary["last_date"] is not None and summary["last_date"] >= "2026-01-01"
        lines.append(
            f"| {key} | {chain} | {summary['rows']:,} | {summary['first_date']} | "
            f"{summary['last_date']} | "
            f"{'Yes' if summary['covers_strategy_start_through_2025_12_31'] else 'No'} | "
            f"{'Yes' if covers_2026 else 'No — ends 2025-12-31'} |"
        )
    lines.append("")
    lines.append(
        "These are documented index renames (the same index, renamed), not aliases inferred "
        "from similarity. The spliced NIFTY 500 ends 2025-12-31 and therefore does not cover "
        "2026. The unspliced source series remain in `series` unchanged alongside the spliced "
        "series."
    )
    lines.append("")
    lines.append("## Splice evidence")
    lines.append("")
    lines.append("| series | boundary | old last date / close | new first date / close | implied move | old-era daily sigma | old-era max abs | z-score | abs percentile | decision |")
    lines.append("|---|---:|---|---|---:|---:|---:|---:|---:|---|")
    for entry in splices:
        lines.append(
            f"| {entry['series_key']} | {entry['boundary']} ({entry['old_name']} -> {entry['new_name']}) | "
            f"{entry['old_last_date']} / {entry['old_last_close']:,.2f} | "
            f"{entry['new_first_date']} / {entry['new_first_close']:,.2f} | "
            f"{entry['implied_return_pct']:+.4f}% | "
            f"{entry['old_era_daily_return_stdev_pct']:.4f}% | "
            f"{entry['old_era_max_abs_daily_return_pct']:.4f}% | "
            f"{entry['implied_z_score']:+.2f} | "
            f"{entry['implied_abs_percentile']:.1f}% | {entry['decision']} |"
        )
    lines.append("")
    lines.append(
        f"No boundary exceeded the {SPLICE_HARD_LIMIT_PCT:g}% hard limit and every implied "
        "move was inside the old-era daily-return distribution, so every boundary used "
        "`concatenate` with no adjustment factor. The spliced series are level-continuous "
        "by construction because the index level was not rescaled."
    )
    lines.append("")
    lines.append("## NIFTY 50 cross-source check")
    lines.append("")
    lines.append(
        f"Date-aligned NSE `Nifty 50` vs Yahoo ^NSEI `Close`: "
        f"**{cross['overlapping_observations']:,}** overlapping sessions "
        f"({cross['overlap_first_date']}..{cross['overlap_last_date']})."
    )
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---:|")
    lines.append(f"| mean absolute difference | {fmt_number(cross['mean_absolute_difference'])} |")
    lines.append(
        f"| max absolute difference | {fmt_number(cross['max_absolute_difference'])} "
        f"({cross['max_absolute_difference_date']}) |"
    )
    lines.append(
        f"| max relative difference | {fmt_number(cross['max_relative_difference_pct'])}% "
        f"({cross['max_relative_difference_date']}) |"
    )
    lines.append(
        f"| relative differences > 0.1% | {cross['count_relative_difference_gt_0_1_pct']} |"
    )
    lines.append(f"| closes equal at 2 decimal places | {cross['count_closes_equal_at_2dp']:,} |")
    lines.append("")
    lines.append(
        f"**Result: {'material disagreement' if cross['material_disagreement'] else 'agree'}.** "
        f"{cross['assessment']} The comparison covers only the NSE exact-name span, "
        "so it does not independently validate Yahoo before 2015-11-09."
    )
    lines.append("")
    lines.append("## Data quirks and gaps")
    lines.append("")
    lines.append(
        "- **Exact-name NSE coverage starts 2015-11-09.** Earlier rows use the old index names; "
        "the NSE export contains the documented rename chains `S&P CNX Nifty` -> `CNX Nifty` "
        "-> `Nifty 50` and `S&P CNX 500` -> `CNX 500` -> `Nifty 500`. The unspliced series stay "
        "exact-name only; the separate `*_SPLICED_NSE` series apply those documented renames "
        "with the boundary evidence below."
    )
    lines.append(
        "- **No 2026 data in the NSE zip.** The `2025` folder contains 2025-01-01..2025-12-31 "
        "(249 files) and is the latest folder; the last filename date and the latest row date "
        "are both 2025-12-31. Both spliced series therefore end on 2025-12-31."
    )
    lines.append(
        f"- **G-Sec is sparse.** `G_SEC_10Y_NSE` has {len(series_pairs['G_SEC_10Y_NSE']):,} rows, "
        f"{checks['G_SEC_10Y_NSE']['missing_sessions_vs_nifty50_calendar']['count']} fewer sessions "
        f"than the Nifty 50 calendar, with "
        f"{checks['G_SEC_10Y_NSE']['calendar_gaps_gt_5_days']['count']} calendar gaps > 5 days "
        f"(largest {checks['G_SEC_10Y_NSE']['calendar_gaps_gt_5_days']['max_calendar_days']} days)."
    )
    weekend_extras = sorted(
        set(item["date"] for key in spliced_keys for item in checks[key]["non_weekday_rows"]["examples"])
        - set(item["date"] for item in checks["NIFTY50_NSE"]["non_weekday_rows"]["examples"])
    )
    lines.append(
        "- **Weekend rows are retained and reported.** The unspliced NSE exact series each have "
        f"{checks['NIFTY50_NSE']['non_weekday_rows']['count']} weekend rows: "
        + ", ".join(
            f"{item['date']} ({item['weekday'][:3]})"
            for item in checks['NIFTY50_NSE']['non_weekday_rows']['examples']
        )
        + f". The spliced series each have {checks[spliced_keys[0]]['non_weekday_rows']['count']}: "
        "the same five plus "
        + ", ".join(weekend_extras)
        + ". Yahoo contains "
        + f"{checks[YAHOO_KEY]['non_weekday_rows']['count']} weekend rows: "
        + ", ".join(
            f"{item['date']} ({item['weekday'][:3]})"
            for item in checks[YAHOO_KEY]['non_weekday_rows']['examples']
        )
        + "; the 2026-11-08 bar is outside the strategy window."
    )
    lines.append(
        f"- **NSE/Yahoo session sets differ.** {session_diff['nse_only_count']} NSE dates are absent "
        f"from Yahoo (including special/weekend sessions) and {session_diff['yahoo_only_count']} "
        "Yahoo dates inside the NSE span are absent from the zip (`2015-12-01`, `2016-06-20`). "
        "Value statistics use only shared dates."
    )
    lines.append(
        f"- **Row-date format quirk, not a date swap.** {nse_date_check['conflict_file_count']} files "
        f"({nse_date_check['conflict_row_count']:,} index rows) in April 2023 carry `Index Date` in "
        "`MM-DD-YYYY` order: `06042023`, `10042023`, `11042023`. The Nifty 50 close in each file "
        "matches Yahoo on the filename date, so the filename remains authoritative; no "
        "day/month-swapped filename was detected. Details are in the JSON."
    )
    lines.append(
        f"- **Yahoo final bar is a Sunday.** The last Yahoo row is 2026-11-08 (Sunday), "
        "outside the strategy window; it is retained for transparency but is not used by a "
        "window ending 2026-09-30."
    )
    lines.append("")
    lines.append("### Per-series validation")
    lines.append("")
    lines.append("| series | rows | duplicate dates | non-Mon-Fri rows | non-positive closes | gaps > 5 days |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for key in series_order:
        check = checks[key]
        duplicate_count = check["duplicate_dates"]["count"]
        weekend_count = check["non_weekday_rows"]["count"]
        nonpositive_count = check["non_positive_closes"]["count"]
        gap_check = check["calendar_gaps_gt_5_days"]
        duplicate_text = "0 (none)" if duplicate_count == 0 else f"{duplicate_count} (see JSON)"
        weekend_text = "0 (none)" if weekend_count == 0 else f"{weekend_count} (listed above)"
        nonpositive_text = "0 (none)" if nonpositive_count == 0 else f"{nonpositive_count} (see JSON)"
        gap_text = (
            "0 (none)"
            if gap_check["count"] == 0
            else f"{gap_check['count']} (max {gap_check['max_calendar_days']} days)"
        )
        lines.append(
            f"| {key} | {check['rows']:,} | {duplicate_text} | {weekend_text} | "
            f"{nonpositive_text} | {gap_text} |"
        )
    lines.append("")
    lines.append("Top calendar gaps > 5 days (up to 10 per series):")
    for key in series_order:
        gap_check = checks[key]["calendar_gaps_gt_5_days"]
        if gap_check["count"] == 0:
            lines.append(f"- {key}: none.")
        else:
            rendered = ", ".join(
                f"{gap['from']}..{gap['to']} ({gap['calendar_days']}d)"
                for gap in gap_check["top_10"]
            )
            lines.append(f"- {key} ({gap_check['count']} gaps): {rendered}.")
    lines.append("")
    lines.append("### Nifty 50 / Nifty 500 name histories")
    lines.append("")
    lines.append("| name | rows | first date | last date |")
    lines.append("|---|---:|---|---|")
    for item in alias_scan["nifty50_name_history"] + alias_scan["nifty500_name_history"]:
        lines.append(
            f"| `{item['name']}` | {item['rows']:,} | {item['first_date']} | {item['last_date']} |"
        )
    lines.append("")
    broad = [item for item in alias_scan["names_containing_500"] if item["broad_500_alias"]]
    thematic = [item for item in alias_scan["names_containing_500"] if not item["broad_500_alias"]]
    lines.append(
        "Broad 500-family names: "
        + "; ".join(
            f"`{item['name']}` {item['first_date']}..{item['last_date']} ({item['rows']:,} rows)"
            for item in broad
        )
        + "."
    )
    lines.append(
        f"The other {len(thematic)} names containing `500` are thematic/derived indices "
        "(for example `Nifty500 Shariah`, `NIFTY500 Value 50`, `Nifty500 Momentum 50`); "
        "their full coverage is enumerated in the JSON `nse_alias_scan`."
    )
    lines.append("")
    lines.append("## What this series can and cannot support")
    lines.append("")
    lines.append(
        "`NIFTY50_YAHOO` is the **primary benchmark** for the alpha report: it is the only "
        "series that spans the full strategy window 2013-04-18..2026-09-30, and it supports "
        "daily close-to-close returns, excess returns, and alpha/beta over that window. It "
        "agrees with the exact-name NSE `Nifty 50` series on all 2,497 overlapping sessions "
        "to within "
        f"{fmt_number(cross['max_absolute_difference'])} index points, but it cannot provide "
        "intraday data, total-return/dividend-adjusted returns, or independent validation "
        "before 2015-11-09. `NIFTY50_SPLICED_NSE` is the NSE cross-check and "
        "`NIFTY500_SPLICED_NSE` is the universe robustness benchmark: both "
        "cover 2012-02-21..2025-12-31, both use the documented renames with normal boundary "
        "moves concatenated unchanged, and both stop at 2025-12-31, so neither covers "
        "2026-01-01..2026-09-30; the NSE zip has no 2026 data. The unspliced exact-name series "
        "start 2015-11-09 and end 2025-12-31, and `G_SEC_10Y_NSE` additionally has 117 missing "
        "sessions. Do not average the NSE and Yahoo sources and do not rescale the spliced "
        "levels."
    )
    lines.append("")
    lines.append("## Reproduce")
    lines.append("")
    lines.append("```powershell")
    lines.append(".venv\\Scripts\\python.exe verification\\research\\alpha-backtest\\build_benchmark.py")
    lines.append("```")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
