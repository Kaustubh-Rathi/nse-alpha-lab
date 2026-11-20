"""Normalize original NSE reports while keeping raw prices and historical names.

No current constituent filter, stale-stock removal, forward filling, or implicit
corporate-action adjustment is performed. A presence observation is not proof of
index membership, listing status, asset class, or an executable fill.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from nse_pipeline.download.client import report_date, write_json
from nse_pipeline.build.portable import MANAGED_VIEWS, open_catalog, sql_path, view_definitions

# The readiness verdict belongs to the full-history gate. The legacy reports in
# this module only point at it, so a stale copy can never read as fresh proof.
AUTHORITATIVE_READINESS_REPORT = "data/reports/full-history-readiness.json"
VERIFIED_ACTIONS_REPORT = "data/reports/verified-actions.json"
ADJUSTMENT_COVERAGE_REPORT = "data/reports/adjustment-coverage.json"
PRE2011_COVERAGE_REPORT = "data/reports/pre2011-actions-coverage.json"

PRICE_SCHEMA = pa.schema([
    ("date", pa.date32()), ("symbol", pa.string()), ("series", pa.string()),
    ("isin", pa.string()), ("open", pa.float64()), ("high", pa.float64()),
    ("low", pa.float64()), ("close", pa.float64()), ("previous_close", pa.float64()),
    ("volume", pa.int64()), ("traded_value", pa.float64()), ("trades", pa.int64()),
    ("valid_ohlcv", pa.bool_()), ("source", pa.string()),
])
PRICE_ALIASES = {
    "symbol": ("SYMBOL", "TCKRSYMB"), "series": ("SERIES", "SCTYSRS"),
    "isin": ("ISIN",), "open": ("OPEN", "OPNPRIC"),
    "high": ("HIGH", "HGHPRIC"), "low": ("LOW", "LWPRIC"),
    "close": ("CLOSE", "CLSPRIC"), "previous_close": ("PREVCLOSE", "PRVSCLSGPRIC"),
    "volume": ("TOTTRDQTY", "TTLTRADGVOL"),
    "traded_value": ("TOTTRDVAL", "TTLTRFVAL", "TTLTRDVAL"),
    "trades": ("TOTALTRADES", "TTLNBOFTXSEXCTD"),
}
ACTION_SCHEMA = pa.schema([
    ("symbol", pa.string()), ("series", pa.string()), ("ex_date", pa.date32()),
    ("record_date", pa.date32()), ("purpose", pa.string()), ("action_type", pa.string()),
    ("share_multiplier", pa.float64()), ("dividend_per_share", pa.float64()),
    ("parse_status", pa.string()), ("first_observed", pa.date32()),
    ("last_observed", pa.date32()), ("source", pa.string()),
])


def read_csv_bytes(content: bytes, repair_overflow_into: str | None = None) -> pd.DataFrame:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252")
    if repair_overflow_into is None:
        frame = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
        frame.columns = frame.columns.str.strip().str.upper()
        return frame
    # A few original PR reports contain an unquoted comma inside PURPOSE. Pandas
    # rejects the whole report; merge only the overflow into that named field so
    # no record is dropped or silently truncated.
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return pd.DataFrame()
    header = [name.strip().upper() for name in rows[0]]
    if repair_overflow_into not in header:
        raise ValueError(f"CSV repair field {repair_overflow_into} absent from {header}")
    field_index = header.index(repair_overflow_into)
    repaired_indices = set()
    records = []
    for line_number, row in enumerate(rows[1:], 2):
        if not row:
            continue
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        elif len(row) > len(header):
            overflow = len(row) - len(header)
            end = field_index + overflow + 1
            row = row[:field_index] + [",".join(row[field_index:end])] + row[end:]
            repaired_indices.add(len(records))
        if len(row) != len(header):
            raise ValueError(f"Malformed CSV row {line_number}: expected {len(header)} fields, got {len(row)}")
        records.append(row)
    frame = pd.DataFrame(records, columns=header, dtype=str)
    frame.attrs["overflow_repaired_rows"] = repaired_indices
    return frame


@lru_cache(maxsize=65536)
def parse_exchange_date(value: str) -> date:
    """ISO dates are year-first; legacy NSE dates are day-first, never ambiguous."""
    value = value.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return pd.to_datetime(value, format="ISO8601", errors="raise").date()
    return pd.to_datetime(value, format="mixed", dayfirst=True, errors="raise").date()


def normalize_prices(content: bytes, day: date, source: str) -> pd.DataFrame:
    original = read_csv_bytes(content)
    result = pd.DataFrame(index=original.index)
    required = {"symbol", "series", "open", "high", "low", "close", "volume"}
    for output, aliases in PRICE_ALIASES.items():
        column = next((name for name in aliases if name in original.columns), None)
        if column is None and output in required:
            raise ValueError(f"{source}: missing required column {output}; got {list(original.columns)}")
        values = original[column].str.strip() if column else pd.Series("", index=original.index)
        if output in ("symbol", "series", "isin"):
            # NA is an actual NSE debt series, not a generic missing-value marker.
            values = values.str.upper().replace({"": None, "-": None})
            if output == "isin":
                values = values.replace({"NA": None})
            result[output] = values
        else:
            values = pd.to_numeric(values.replace({"": None, "-": None}), errors="raise")
            result[output] = values.astype("Int64") if output in ("volume", "trades") else values.astype(float)
    if result["symbol"].isna().any():
        raise ValueError(f"{source}: empty symbol")
    # Some original reports carry debt securities with blank series. Retain the
    # missing classification; SQL's equity-series filter will exclude them.
    # Never infer a date from the caller if the report itself disagrees.
    for date_column in ("TIMESTAMP", "TRADDT"):
        if date_column in original:
            parsed = {parse_exchange_date(value) for value in original[date_column].unique()}
            if parsed != {day}:
                raise ValueError(f"{source}: report date differs from filename {day}")
    if result.duplicated(["symbol", "series", "isin"]).any():
        raise ValueError(f"{source}: duplicate security rows; manual review required")
    result["date"] = day
    result["source"] = source
    prices = result[["open", "high", "low", "close"]]
    result["valid_ohlcv"] = (
        prices.notna().all(axis=1) & prices.gt(0).all(axis=1)
        & result["volume"].ge(0)
        & result["high"].ge(prices[["open", "close", "low"]].max(axis=1) - 1e-8)
        & result["low"].le(prices[["open", "close", "high"]].min(axis=1) + 1e-8)
    ).fillna(False)
    return result[PRICE_SCHEMA.names]


@dataclass
class PriceReport:
    archive: Path
    member: str | None
    day: date
    crc: int
    size: int

    @property
    def source(self) -> str:
        return f"{self.archive.name}!{self.member}" if self.member else self.archive.name


def discover_prices(data: Path, start: date, end: date) -> list[PriceReport]:
    reports = []
    archive_path = data / "raw" / "nseBhav.zip"
    if not archive_path.exists():
        raise FileNotFoundError(f"Missing {archive_path}; run the fetch command first")
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            if member.is_dir() or "__MACOSX" in member.filename:
                continue
            day = report_date(member.filename)
            if day and start <= day <= end and member.filename.lower().endswith(".csv"):
                reports.append(PriceReport(archive_path, member.filename, day, member.CRC, member.file_size))
    for archive_path in sorted((data / "raw" / "recent" / "prices").glob("*.zip")):
        day = report_date(archive_path.name)
        if day and start <= day <= end:
            with zipfile.ZipFile(archive_path) as archive:
                members = [m for m in archive.infolist() if m.filename.lower().endswith(".csv")]
                if len(members) != 1:
                    raise ValueError(f"Expected one price CSV in {archive_path}")
                member = members[0]
                reports.append(PriceReport(archive_path, member.filename, day, member.CRC, member.file_size))
    reports.sort(key=lambda report: (report.day, report.source))
    dates = [r.day for r in reports]
    if len(set(dates)) != len(dates):
        raise ValueError("Multiple price reports for a session; remove the redundant downloaded report explicitly")
    if not reports:
        raise ValueError("No recognized price reports in the requested date range")
    return reports


def parquet_atomic(frame: pd.DataFrame, path: Path, schema: pa.Schema | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".parquet.tmp")
    table = pa.Table.from_pandas(frame, schema=schema, preserve_index=False)
    pq.write_table(table, temporary, compression="zstd")
    temporary.replace(path)


def build_price_partition(data: Path, year: int, entries: list[PriceReport], signature: str) -> dict:
    """A worker owns one year/output file; raw archives are opened read-only."""
    frames = []
    with ExitStack() as stack:
        archives = {}
        for report in entries:
            if report.archive not in archives:
                archives[report.archive] = stack.enter_context(zipfile.ZipFile(report.archive))
            content = archives[report.archive].read(report.member)
            frames.append(normalize_prices(content, report.day, report.source))
    frame = pd.concat(frames, ignore_index=True)
    target = data / "processed" / "prices" / f"year={year}" / "bars.parquet"
    parquet_atomic(frame, target, PRICE_SCHEMA)
    result = {
        "source_signature": signature, "rows": len(frame), "sessions": len(entries),
        "first_date": entries[0].day.isoformat(), "last_date": entries[-1].day.isoformat(),
        "invalid_ohlcv_rows": int((~frame["valid_ohlcv"]).sum()),
    }
    print(f"Prices {year}: {len(frame):,} rows, {len(entries)} sessions", flush=True)
    return result


def build_prices(data: Path, start: date, end: date, force: bool, workers: int = 1) -> dict:
    if not 1 <= workers <= 4:
        raise ValueError("Build workers must be between 1 and 4")
    reports = discover_prices(data, start, end)
    write_json(data / "manifests" / "price-report-inventory.json", {
        "requested_start": start, "requested_end": end, "reports": [
            {"date": report.day, "archive": report.archive.name, "member": report.member,
             "crc32": report.crc, "uncompressed_bytes": report.size}
            for report in reports
        ],
        "note": "A source inventory, not proof of completeness against an official exchange calendar.",
    })
    groups = defaultdict(list)
    for report in reports:
        groups[report.day.year].append(report)
    manifest_path = data / "manifests" / "prices-build.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest = {}
    jobs = []
    for year, entries in sorted(groups.items()):
        signature = hashlib.sha256(json.dumps(
            {"format_version": 1, "reports": [(r.day.isoformat(), r.source, r.crc, r.size) for r in entries]}
        ).encode()).hexdigest()
        target = data / "processed" / "prices" / f"year={year}" / "bars.parquet"
        old = previous.get(str(year), {})
        if not force and target.exists() and old.get("source_signature") == signature:
            print(f"Prices {year}: cached ({old['rows']:,} rows)", flush=True)
            manifest[str(year)] = old
            continue
        jobs.append((year, entries, signature))
    if workers == 1:
        for year, entries, signature in jobs:
            manifest[str(year)] = build_price_partition(data, year, entries, signature)
            write_json(manifest_path, {**previous, **manifest})
    elif jobs:
        errors = []
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(build_price_partition, data, year, entries, signature): year
                       for year, entries, signature in jobs}
            for future in as_completed(futures):
                year = futures[future]
                try:
                    manifest[str(year)] = future.result()
                except Exception as error:
                    errors.append((year, error))
                    print(f"Prices {year} failed: {type(error).__name__}", flush=True)
                    continue
                write_json(manifest_path, {**previous, **manifest})
        if errors:
            raise RuntimeError(f"Price partitions failed: {[year for year, _ in errors]}") from errors[0][1]
    # Do not silently mix partitions from a previous, wider request into SQL views.
    manifest_path = data / "manifests" / "prices-build.json"
    write_json(manifest_path, manifest)
    return dict(sorted(manifest.items()))


def parse_action(purpose: str) -> dict:
    """Conservative extraction only: ambiguous/composite events are review items."""
    text = re.sub(r"\s+", " ", purpose.upper()).strip()
    text = re.sub(r"\b(?:FVSPLT|FVSPLIT|FVSPL)\b", "FV SPLIT", text)
    text = re.sub(r"\bSPLT\b", "SPLIT", text)
    text = re.sub(r"\bFRM\b", "FROM", text)
    types = []
    if "SPLIT" in text:
        types.append("split")
    if "BONUS" in text:
        types.append("bonus")
    if re.search(r"\b(?:DIVIDEND|DIVD|DIVI|DIVID|DIV|INTDIV|SPLDIV)\b", text):
        types.append("dividend")
    if re.search(r"RIGHT|RHTS|RGHT", text):
        types.append("rights")
    if re.search(r"MERGER|AMALG|ARRANG|CAPITAL RED|CONSOLID", text):
        types.append("reorganization")
    result = {"action_type": "+".join(types) or "other", "share_multiplier": None,
              "dividend_per_share": None, "parse_status": "needs_review"}
    if len(types) != 1 or re.search(r"PREF|DEBENTURE|NCRPS|DVR", text):
        return result
    if types == ["bonus"]:
        ratios = re.findall(r"(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)", text)
        if len(ratios) == 1 and float(ratios[0][1]) > 0:
            result.update(share_multiplier=1 + float(ratios[0][0]) / float(ratios[0][1]),
                          parse_status="parsed_not_applied")
    if types == ["split"]:
        match = re.search(
            r"FROM\s+(?:(?:RS|RE)\.?\s*)?(\d+(?:\.\d+)?)\s*(?:/-)?\s*(?:PER SHARE\s*)?"
            r"TO\s+(?:(?:RS|RE)\.?\s*)?(\d+(?:\.\d+)?)", text,
        )
        if match and float(match[2]) > 0:
            result.update(share_multiplier=float(match[1]) / float(match[2]),
                          parse_status="parsed_not_applied")
    if types == ["dividend"] and "%" not in text:
        amounts = re.findall(r"(?:RS\.?|RE\.?|INR)\s*(\d+(?:\.\d+)?)", text)
        if len(amounts) == 1:
            result.update(dividend_per_share=float(amounts[0]), parse_status="parsed_not_applied")
    return result


def action_rows(content: bytes, observed: date, source: str) -> list[dict]:
    frame = read_csv_bytes(content, repair_overflow_into="PURPOSE")
    required = {"SYMBOL", "SERIES", "PURPOSE", "EX_DT", "RECORD_DT"}
    if not required.issubset(frame.columns):
        raise ValueError(f"{source}: unexpected corporate-action schema: {list(frame.columns)}")
    repaired = frame.attrs.get("overflow_repaired_rows", set())
    rows = []
    for index, item in enumerate(frame.to_dict("records")):
        ex_text = item["EX_DT"].strip()
        if ex_text in ("", "-", "NA"):
            continue
        ex_date = parse_exchange_date(ex_text)
        record_text = item["RECORD_DT"].strip()
        record_date = None if record_text in ("", "-", "NA") else parse_exchange_date(record_text)
        purpose = item["PURPOSE"].strip()
        rows.append({"symbol": item["SYMBOL"].strip().upper(),
                     "series": item["SERIES"].strip().upper(), "ex_date": ex_date,
                     "record_date": record_date, "purpose": purpose, **parse_action(purpose),
                     "first_observed": observed, "last_observed": observed, "source": source,
                     "csv_overflow_repaired": index in repaired})
    return rows


def read_pr_zip(content: bytes, observed: date, source: str) -> list[dict]:
    rows = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for member in archive.infolist():
            if re.fullmatch(r"bc\d+\.csv", Path(member.filename).name, re.I) and member.file_size:
                rows.extend(action_rows(archive.read(member), observed, f"{source}!{member.filename}"))
    return rows


def merge_action_rows(deduplicated: dict, rows: list[dict], end: date) -> None:
    for row in rows:
        if row["ex_date"] > end:
            continue
        key = tuple(row[k] for k in ("symbol", "series", "ex_date", "record_date", "purpose"))
        if key in deduplicated:
            old = deduplicated[key]
            if row["first_observed"] < old["first_observed"]:
                old["source"] = row["source"]
            old["first_observed"] = min(old["first_observed"], row["first_observed"])
            old["last_observed"] = max(old["last_observed"], row["last_observed"])
            old["csv_overflow_repaired"] = (
                old.get("csv_overflow_repaired", False) or row.get("csv_overflow_repaired", False)
            )
        else:
            deduplicated[key] = row


def collect_action_year(master: Path, year: int, members: list[str], end: date) -> list[dict]:
    deduplicated = {}
    with zipfile.ZipFile(master) as archive:
        for member in members:
            day = report_date(member)
            merge_action_rows(deduplicated, read_pr_zip(archive.read(member), day,
                              f"{master.name}!{member}"), end)
    print(f"Corporate-action source year {year}: {len(members)} reports", flush=True)
    return list(deduplicated.values())


def build_actions(data: Path, end: date, workers: int = 1) -> None:
    if not 1 <= workers <= 4:
        raise ValueError("Action workers must be between 1 and 4")
    master = data / "raw" / "PR_bhav-master.zip"
    if not master.exists():
        raise FileNotFoundError("Corporate-action archive missing; fetch actions or explicitly use --skip-actions")
    deduplicated = {}
    groups = defaultdict(list)
    with zipfile.ZipFile(master) as archive:
        for member in archive.infolist():
            day = report_date(member.filename)
            if day and day <= end and member.filename.lower().endswith(".zip"):
                groups[day.year].append(member.filename)
    jobs = [(year, sorted(members, key=report_date)) for year, members in sorted(groups.items())]
    if workers == 1:
        for year, members in jobs:
            merge_action_rows(deduplicated, collect_action_year(master, year, members, end), end)
    elif jobs:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(collect_action_year, master, year, members, end)
                       for year, members in jobs]
            for future in as_completed(futures):
                merge_action_rows(deduplicated, future.result(), end)
    for path in sorted((data / "raw" / "recent" / "actions").glob("*.zip")):
        day = report_date(path.name)
        if day and day <= end:
            merge_action_rows(deduplicated, read_pr_zip(path.read_bytes(), day, path.name), end)
    if not deduplicated:
        raise ValueError("No corporate-action records found; inspect the archive layout")
    repaired_rows = sum(bool(row.pop("csv_overflow_repaired", False))
                        for row in deduplicated.values())
    frame = pd.DataFrame(deduplicated.values()).sort_values(["ex_date", "symbol", "series", "purpose"])
    parquet_atomic(frame, data / "processed" / "actions" / "actions.parquet", ACTION_SCHEMA)
    review = frame[frame["parse_status"] == "needs_review"]
    reports_dir = data / "reports"
    reports_dir.mkdir(exist_ok=True)
    review.to_csv(reports_dir / "actions-needing-review.csv", index=False)
    write_json(data / "manifests" / "actions-build.json", {
        "rows": len(frame), "first_ex_date": frame["ex_date"].min(),
        "last_ex_date": frame["ex_date"].max(), "needs_review": len(review),
        "unquoted_comma_rows_repaired": repaired_rows,
        "adjustments_applied": False,
        "note": "First observed is earliest available PR report, not a verified original announcement timestamp.",
    })
    print(f"Corporate actions: {len(frame):,} records; {len(review):,} need review", flush=True)


def create_catalog(data: Path, partitions: dict, include_actions: bool) -> None:
    definitions = view_definitions(data, partitions=partitions, include_actions=include_actions)
    with duckdb.connect(str(data / "market.duckdb")) as connection:
        for name in reversed(MANAGED_VIEWS):
            if name not in definitions:
                connection.execute(f'DROP VIEW IF EXISTS "{name}"')
        for name, sql in definitions.items():
            connection.execute(f'CREATE OR REPLACE VIEW "{name}" AS {sql}')
        connection.execute(f"COPY (SELECT * FROM security_history ORDER BY security_key) TO '{sql_path(data / 'processed' / 'security_history.parquet')}' (FORMAT PARQUET, COMPRESSION ZSTD)")


def read_json_report(path: Path) -> dict:
    """Read a JSON report if it is present and readable.

    An absent, unreadable or non-object file is an empty mapping: a missing
    artifact must never become a number.
    """
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def normalized_actions_summary(reports: Path) -> dict:
    """Summarise the normalized, terms-verified and applied pre-2011 layer.

    Every count is read from the current artifacts at write time; a missing
    artifact yields ``None`` instead of a stale or invented number. The
    tags-only inventory is deliberately not repeated here: it is not evidence
    that any term was verified or any factor applied.
    """
    verified = read_json_report(reports / "verified-actions.json")
    adjustment = read_json_report(reports / "adjustment-coverage.json")
    applied_block = adjustment.get("verified_actions")
    applied_block = applied_block if isinstance(applied_block, dict) else {}
    pre2011 = adjustment.get("pre2011")
    pre2011 = pre2011 if isinstance(pre2011, dict) else {}
    applied = pre2011.get("applied")
    if applied is None:
        applied = applied_block.get("actually_applied_actions")
    status = applied_block.get("application_status") or verified.get("application_status")
    return {
        "normalized_action_rows": verified.get("normalized_action_rows"),
        "terms_verified_rows": verified.get("verified_terms_rows"),
        "execution_ready_actions": verified.get("execution_ready_actions"),
        "actually_applied_actions": applied,
        "verified_multipliers": pre2011.get("verified_multipliers"),
        "split_or_bonus_tagged_rows": pre2011.get("split_or_bonus_tagged_rows"),
        "factor_application": {
            "applied_pre2011_actions": applied,
            "application_status": status,
            "scope": "split/bonus share multipliers only",
            "note": ("Split/bonus factors are applied for the pre-2011 layer. Dividends, "
                     "rights and schemes stay unadjusted audit records; readiness is not "
                     "completeness."),
        },
        "reports": {
            "normalized_actions": VERIFIED_ACTIONS_REPORT,
            "adjustment_coverage": ADJUSTMENT_COVERAGE_REPORT,
            "full_history_readiness": AUTHORITATIVE_READINESS_REPORT,
        },
    }


def legacy_readiness(reports: Path, coverage: dict | None, pre2011: dict | None) -> dict:
    """Build the legacy readiness report as a pointer to the authoritative gate.

    ``data/reports/full-history-readiness.json`` owns the verdict. This report
    states that it is superseded, names the authoritative path and restates only
    numbers that are current at write time.
    """
    authoritative = read_json_report(reports / "full-history-readiness.json")
    normalized = normalized_actions_summary(reports)
    blockers = [
        {"code": item.get("code"), "reason": item.get("reason"),
         "next_action": item.get("next_action")}
        for item in authoritative.get("blockers", []) if isinstance(item, dict)
    ]
    if not blockers:
        blockers = [{
            "code": "authoritative_readiness_report_missing",
            "reason": (f"{AUTHORITATIVE_READINESS_REPORT} was not readable at write time, so "
                       "no current readiness verdict or blocker list can be restated."),
            "next_action": "Run the readiness audit, then regenerate this pointer report.",
        }]
    pre2011_block = {
        "normalized_action_rows": normalized["normalized_action_rows"],
        "terms_verified_rows": normalized["terms_verified_rows"],
        "execution_ready_actions": normalized["execution_ready_actions"],
        "actually_applied_actions": normalized["actually_applied_actions"],
        "factor_application": normalized["factor_application"],
        "report": PRE2011_COVERAGE_REPORT,
        "normalized_report": VERIFIED_ACTIONS_REPORT,
        "adjustment_report": ADJUSTMENT_COVERAGE_REPORT,
    }
    if pre2011 is not None:
        pre2011_block.update({
            "raw_inventory_rows": pre2011.get("official_raw_rows"),
            "raw_inventory_dated_rows": pre2011.get("official_dated_rows"),
            "raw_inventory_label": "tags_only_nse_api_inventory",
            "raw_inventory_note": ("Official NSE API rows with search labels only: the row count "
                                   "is not an economic event count and not a verified term."),
            "raw_inventory_earliest": pre2011.get("earliest"),
            "raw_inventory_latest": pre2011.get("latest"),
            "raw_inventory_complete": pre2011.get("complete"),
        })
    calendar = (coverage or {}).get("sessions", {}).get("missing_date_counts")
    snapshot = (coverage or {}).get("snapshot", {})
    return {
        "ready_for_backtesting": False,
        "status": "superseded_pointer",
        "superseded_by": AUTHORITATIVE_READINESS_REPORT,
        "note": ("Legacy pointer report. The authoritative readiness verdict, blockers and "
                 "evidence live in " + AUTHORITATIVE_READINESS_REPORT + ", which reads "
                 + VERIFIED_ACTIONS_REPORT + " and " + ADJUSTMENT_COVERAGE_REPORT + ". "
                 "Do not read this file as a standalone readiness verdict."),
        "authoritative_report": AUTHORITATIVE_READINESS_REPORT,
        "authoritative_ready_for_backtesting": authoritative.get("ready_for_backtesting"),
        "authoritative_generated_at": authoritative.get("generated_at"),
        "authoritative_requested_window": {
            "start": authoritative.get("requested_start"),
            "end": authoritative.get("requested_end"),
        },
        "blocker_codes": [blocker["code"] for blocker in blockers],
        "blocking_work": blockers,
        "completed": ["Raw historical report acquisition for observed coverage",
                      "Schema, filename/report-date and duplicate-row checks",
                      "OHLCV checks; invalid rows retained separately for review",
                      "Historical report-presence observations without today's stock filter"],
        "conditional_requirements": {
            "historical_nifty_or_sector_membership": "Required only if the strategy uses those historical universes",
            "point_in_time_fundamentals": "Not needed for a purely price/volume-ranked strategy",
            "intraday_or_options_data": "Not needed for an end-of-day cash-equity rotation strategy",
        },
        "cost_and_fill_assumptions": "Configure brokerage/taxes/slippage, circuit/illiquidity limits and executable entry timing in the backtester.",
        "verified_coverage": {
            "calendar_audit": None if coverage is None else {
                "requested_cutoff": snapshot.get("requested_cutoff"),
                "last_observed": snapshot.get("last_observed"),
                "missing_date_counts": calendar,
                "report": "data/reports/coverage-audit.json",
            },
            "pre2011_actions": pre2011_block,
        },
        "reports": {
            "full_history_readiness": AUTHORITATIVE_READINESS_REPORT,
            "verified_actions": VERIFIED_ACTIONS_REPORT,
            "adjustment_coverage": ADJUSTMENT_COVERAGE_REPORT,
            "pre2011_actions_coverage": PRE2011_COVERAGE_REPORT,
        },
    }


def validate_database(data: Path) -> dict:
    with open_catalog(data) as connection:
        result = connection.execute("""SELECT count(*) AS rows, count(DISTINCT date) AS sessions,
            count(DISTINCT symbol) AS historical_symbols, min(date) AS first_date, max(date) AS last_date,
            count(*) FILTER (WHERE NOT valid_ohlcv) AS invalid_ohlcv_rows,
            count(*) FILTER (WHERE series IS NULL) AS rows_without_series,
            count(*) FILTER (WHERE isin IS NULL) AS rows_without_isin FROM bars""").fetchdf().iloc[0].to_dict()
        duplicates = connection.execute("""SELECT count(*) FROM
            (SELECT date, symbol, series, isin FROM bars GROUP BY ALL HAVING count(*) > 1)""").fetchone()[0]
        result["duplicate_security_sessions"] = duplicates
        result["series_counts"] = dict(connection.execute(
            "SELECT series, count(*) FROM bars GROUP BY series ORDER BY count(*) DESC"
        ).fetchall())
        connection.execute(f"""COPY (SELECT * FROM bars WHERE NOT valid_ohlcv)
            TO '{sql_path(data / 'reports' / 'invalid-ohlcv-rows.csv')}' (HEADER, DELIMITER ',')""")
        identity_conflicts = connection.execute("""SELECT count(*) FROM (
            SELECT date, isin FROM equity_candidates WHERE isin IS NOT NULL
            GROUP BY date, isin HAVING count(DISTINCT symbol) > 1)""").fetchone()[0]
        result["same_day_isin_symbol_conflicts"] = identity_conflicts
        for view in ("corporate_actions", "pre2011_actions", "corporate_actions_all",
                     "equity_corporate_actions", "equity_corporate_actions_all"):
            try:
                result[f"{view}_rows"] = connection.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
            except duckdb.CatalogException:
                result[f"{view}_rows"] = None
        if result["corporate_actions_rows"] is not None:
            result["corporate_actions_needs_review"] = connection.execute(
                "SELECT count(*) FROM corporate_actions WHERE parse_status = 'needs_review'"
            ).fetchone()[0]
        result["cash_equity_rows"] = connection.execute(
            "SELECT count(*) FROM cash_equity_candidates").fetchone()[0]
        result["cash_equity_symbols"] = connection.execute(
            "SELECT count(DISTINCT symbol) FROM cash_equity_candidates").fetchone()[0]
        sessions = {row[0] for row in connection.execute("SELECT DISTINCT date FROM bars").fetchall()}
        if not sessions:
            raise ValueError("Database contains no sessions")
        missing = []
        day = min(sessions)
        while day <= max(sessions):
            if day.weekday() < 5 and day not in sessions:
                missing.append(day.isoformat())
            day += timedelta(days=1)
        result["unverified_weekdays_without_report"] = missing
        result["missing_date_note"] = "Includes exchange holidays; not a validated missing-session list."
        # This is a diagnostic, not an assertion: raw corporate-action gaps are expected.
        connection.execute(f"""COPY (
            WITH returns AS (SELECT date, symbol, series, isin, close,
                lag(close) OVER (PARTITION BY security_key ORDER BY date, series) AS prior_close
                FROM equity_candidates WHERE valid_ohlcv AND series = 'EQ')
            SELECT *, close / nullif(prior_close, 0) - 1 AS raw_return
            FROM returns WHERE abs(close / nullif(prior_close, 0) - 1) > 0.5
            ORDER BY date, symbol
        ) TO '{sql_path(data / 'reports' / 'large-raw-price-moves.csv')}' (HEADER, DELIMITER ',')""")
    result.update(
        validated_at=datetime.now(timezone.utc).isoformat(), price_adjustment="none (raw exchange prices)",
        ready_for_unbiased_backtesting=False,
        limitations=[
            "Corporate actions have NOT been applied; parsing requires review and ordinary-equity pre-2011 coverage is partial.",
            "Historical symbols are retained, but delisting proceeds, mergers and security-identity continuity are not reconstructed.",
            "Observed trading rows are not historical Nifty/sector membership or proof of listing status.",
            "Series filters can include ETFs and other instruments; equity asset-class classification is not complete.",
            "No forward-filled prices, invented sessions, or assumption that stale prices were executable.",
        ],
    )
    inventory_path = data / "manifests" / "price-report-inventory.json"
    if inventory_path.exists():
        inventory = json.loads(inventory_path.read_text())
        result["requested_start"] = inventory["requested_start"]
        result["requested_end"] = inventory["requested_end"]
        result["source_report_sessions"] = len(inventory["reports"])
        source_dates = {date.fromisoformat(report["date"]) for report in inventory["reports"]}
        result["source_sessions_match_processed"] = source_dates == sessions
    (data / "reports").mkdir(exist_ok=True)
    coverage_path = data / "reports" / "coverage-audit.json"
    pre2011_path = data / "reports" / "pre2011-actions-coverage.json"
    coverage = json.loads(coverage_path.read_text()) if coverage_path.exists() else None
    pre2011 = json.loads(pre2011_path.read_text()) if pre2011_path.exists() else None
    if coverage is not None:
        result["calendar_audit"] = {
            "missing_date_counts": coverage["sessions"]["missing_date_counts"],
            "report": "data/reports/coverage-audit.json",
        }
    if pre2011 is not None:
        result["pre2011_action_coverage"] = {
            "official_rows": pre2011["official_raw_rows"],
            "earliest": pre2011["earliest"],
            "latest": pre2011["latest"],
            "complete": pre2011["complete"],
        }
    write_json(data / "reports" / "validation.json", result)
    write_json(data / "reports" / "readiness.json",
               legacy_readiness(data / "reports", coverage, pre2011))
    summary = {key: result[key] for key in (
        "rows", "sessions", "historical_symbols", "first_date", "last_date",
        "invalid_ohlcv_rows", "duplicate_security_sessions", "ready_for_unbiased_backtesting",
    )}
    print(json.dumps(summary, indent=2, default=str), flush=True)
    print(f"Full audit: {data / 'reports' / 'validation.json'}", flush=True)
    if duplicates:
        raise ValueError("Duplicate security/session rows detected")
    return result


def build_database(data: Path, start: date, end: date, force: bool = False,
                   include_actions: bool = True, workers: int = 1) -> None:
    if start > end:
        raise ValueError("Start date must be no later than end date")
    (data / "reports").mkdir(parents=True, exist_ok=True)
    partitions = build_prices(data, start, end, force, workers)
    if include_actions:
        build_actions(data, end, workers)
    create_catalog(data, partitions, include_actions)
    validate_database(data)
