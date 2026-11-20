"""Read-only parser for NIFTY index levels already on disk; it never writes under data/.

Sources discovered from nse_pipeline.paths.data_root():
raw/nseIndices.zip members and raw/calendar/ind_close_all_*.csv files, the
same report family admitted by nse_pipeline.audit.calendar. The filename
date is authoritative: three archive members carry day/month-swapped
content dates (audit section 6.7.1), so content dates are not consulted.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator

from nse_pipeline.paths import data_root

NIFTY_INDEX_NAMES = frozenset({"NIFTY 50", "CNX NIFTY", "S&P CNX NIFTY"})
REPORT_NAME = re.compile(r"ind_close_all_(\d{8})\.csv", re.I)
ZIP_RELATIVE = Path("raw") / "nseIndices.zip"
CALENDAR_DIR_RELATIVE = Path("raw") / "calendar"
REQUIRED_COLUMNS = frozenset({"Index Name", "Closing Index Value"})


@dataclass(frozen=True, order=True)
class IndexLevelRecord:
    session_date: date
    level: Decimal


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1252")


def _report_date(origin: str) -> date:
    match = REPORT_NAME.search(Path(origin).name)
    if match is None:
        raise ValueError(f"Report filename carries no authoritative session date: {origin}")
    return datetime.strptime(match[1], "%d%m%Y").date()


def _parse_report(content: bytes, origin: str) -> dict[date, Decimal]:
    reader = csv.DictReader(io.StringIO(_decode(content)))
    if reader.fieldnames is None or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
        raise ValueError(f"Malformed index report, missing required columns: {origin}")
    session = _report_date(origin)
    chosen: Decimal | None = None
    for row in reader:
        try:
            name = row["Index Name"].strip().upper()
            raw = row["Closing Index Value"]
        except (KeyError, AttributeError) as error:
            raise ValueError(f"Malformed row in index report {origin}") from error
        if name not in NIFTY_INDEX_NAMES or raw is None:
            continue
        try:
            level = Decimal(raw.strip())
        except InvalidOperation as error:
            raise ValueError(f"Malformed closing index value {raw!r} in {origin}") from error
        if not level.is_finite() or level <= 0:
            raise ValueError(f"Non-positive or non-finite index level in {origin}: {raw!r}")
        if chosen is not None and chosen != level:
            raise ValueError(f"Conflicting NIFTY rows within one report: {origin}")
        chosen = level
    return {} if chosen is None else {session: chosen}


def _report_sources(root: Path) -> Iterator[tuple[str, bytes]]:
    archive_path = root / ZIP_RELATIVE
    if archive_path.exists():
        with zipfile.ZipFile(archive_path) as archive:
            for name in sorted(archive.namelist()):
                if REPORT_NAME.fullmatch(Path(name).name):
                    yield name, archive.read(name)
    calendar_dir = root / CALENDAR_DIR_RELATIVE
    if calendar_dir.is_dir():
        for path in sorted(calendar_dir.iterdir()):
            if REPORT_NAME.fullmatch(path.name):
                yield path.name, path.read_bytes()


def load_nifty_levels(data: Path | None = None) -> tuple[IndexLevelRecord, ...]:
    """Sorted, unique (session_date, level) records; fails closed on conflicts or malformed rows.

    Identical duplicates across sources (same date, same level) collapse to one
    record; a same-date level disagreement raises.
    """
    root = data_root() if data is None else Path(data)
    merged: dict[date, tuple[Decimal, str]] = {}
    for origin, content in _report_sources(root):
        for session, level in _parse_report(content, origin).items():
            previous = merged.get(session)
            if previous is not None:
                if previous[0] != level:
                    raise ValueError(
                        f"Conflicting NIFTY levels for {session}: {previous[1]} vs {origin}")
                continue
            merged[session] = (level, origin)
    if not merged:
        raise ValueError(f"No NIFTY index levels found under {root}")
    return tuple(IndexLevelRecord(session, level) for session, (level, _) in sorted(merged.items()))
