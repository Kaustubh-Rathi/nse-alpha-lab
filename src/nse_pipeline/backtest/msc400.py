"""Build the NIFTY MidSmallcap 400 (MSC400) universe from raw NSE index ZIPs.

Raju (2023) defines MSC400 as market-cap ranks 101-500. NSE publishes the
equivalent as the **NIFTY MidSmallcap 400** index, whose constituent PDFs sit
inside the monthly market-capitalisation ZIPs already on disk. Each PDF row is:

    SYMBOL
    Company Name
    SECTOR
    Price
    Weight %
    Market Cap (Rs crore)

This module extracts that table for every available month, so the MSC400
universe can be built from an official dated document rather than inferred from
a market-cap rank we would have to compute ourselves.

Policy: a snapshot proves membership on its own document date only. Nothing is
carried forward, nothing is interpolated, and a month with no ZIP produces no
snapshot rather than a guessed one.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RAW = ROOT / "data" / "raw" / "index_membership"

#: ``indices_dataOct2017.zip`` -> (2017, 10)
_ZIP_RE = re.compile(r"indices_data([A-Za-z]{3})(\d{4})\.zip$", re.IGNORECASE)
_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}

#: A row is six consecutive lines: symbol, company, sector, price, weight, mcap.
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&.\-]{0,19}$")
_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_SECTORS = {
    "AUTOMOBILE", "FINANCIAL SERVICES", "INDUSTRIAL MANUFACTURING",
    "HEALTHCARE SERVICES", "TEXTILES", "CONSTRUCTION", "PHARMA",
    "CONSUMER GOODS", "IT", "TELECOM", "METALS", "CHEMICALS", "ENERGY",
    "POWER", "REALTY", "MEDIA", "CEMENT", "FERTILISERS", "SERVICES",
    "HEALTHCARE", "CONSUMER SERVICES", "UTILITIES", "DIVERSIFIED",
    "CAPITAL GOODS", "OIL & GAS", "BANKING", "FINANCE", "INSURANCE",
    "MEDIA & ENTERTAINMENT", "CEMENT & CEMENT PRODUCTS",
    "FERTILISERS & PESTICIDES", "CONSUMER DURABLES", "RETAILING",
    "TELECOMMUNICATIONS", "COMMERCIAL SERVICES",
    "CONSUMER GOODS & SERVICES", "AUTOMOBILE & AUTO COMPONENTS",
    "CHEMICALS & PETROCHEMICALS", "OIL GAS & CONSUMABLE FUELS",
    "POWER & UTILITIES", "INFORMATION TECHNOLOGY", "METALS & MINING",
    "PAPER", "PLASTIC", "RUBBER", "LEATHER", "GLASS", "SUGAR",
    "TRADING", "HOTELS", "SHIPPING", "LOGISTICS", "AVIATION",
    "MEDIA, ENTERTAINMENT & PUBLICATION",
}


@dataclass(frozen=True)
class Constituent:
    """One row of an official constituent table."""

    symbol: str
    company: str
    sector: str
    price: float | None
    weight_pct: float | None
    market_cap_crore: float | None


@dataclass(frozen=True)
class Snapshot:
    """One dated official constituent table."""

    as_of: date
    index_name: str
    source_zip: str
    source_member: str
    constituents: tuple[Constituent, ...]

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(row.symbol for row in self.constituents)


def _parse_month(zip_path: Path) -> tuple[int, int] | None:
    match = _ZIP_RE.search(zip_path.name)
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    return int(match.group(2)), month


def _month_end(year: int, month: int) -> date:
    """Last calendar day of the month, used only as the snapshot's label."""
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1).fromordinal(
        date(year, month + 1, 1).toordinal() - 1)


def _members_for(zip_path: Path, index_token: str) -> list[str]:
    with zipfile.ZipFile(zip_path) as archive:
        return [name for name in archive.namelist()
                if index_token.lower() in name.lower() and name.lower().endswith(".pdf")]


def parse_constituent_text(text: str) -> tuple[Constituent, ...]:
    """Parse the constituent table out of a PDF's text.

    The table is positional, not delimited, and a long company name wraps onto a
    second line, so a fixed six-line stride drops rows. Instead this anchors on
    the **sector** line: a sector is followed by exactly three numbers (price,
    weight, market cap), and the symbol is the nearest preceding line that looks
    like a ticker. Everything between the symbol and the sector is the company
    name, joined back together.

    A row is emitted only when the anchor, the three numbers and the symbol are
    all well formed. Anything else is skipped, never guessed.
    """
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    # A long sector label can wrap onto a second line. Rejoin it before parsing
    # so the anchor is a single line followed by exactly three numbers.
    merged: list[str] = []
    index = 0
    while index < len(lines):
        current = lines[index]
        if (index + 1 < len(lines)
                and current.isupper()
                and current.upper() not in _SECTORS
                and lines[index + 1].isupper()
                and not _NUMBER_RE.match(lines[index + 1])):
            joined = f"{current} {lines[index + 1]}"
            if joined in _SECTORS:
                merged.append(joined)
                index += 2
                continue
        merged.append(current)
        index += 1
    lines = merged

    rows: list[Constituent] = []
    for index, line in enumerate(lines):
        sector = line.upper()
        if sector not in _SECTORS:
            continue
        if index + 3 >= len(lines):
            continue
        price, weight, market_cap = lines[index + 1:index + 4]
        if not (_NUMBER_RE.match(price) and _NUMBER_RE.match(weight)
                and _NUMBER_RE.match(market_cap)):
            continue

        # Walk backwards for the ticker: the nearest all-caps short token.
        symbol = None
        symbol_at = None
        for back in range(index - 1, max(index - 6, -1), -1):
            candidate = lines[back]
            # A price, weight or market-cap line is all caps as far as
            # ``str.upper`` is concerned, but it is never a ticker. Without
            # this guard a row whose symbol line is absent would silently
            # acquire the previous row's market cap as its symbol.
            if _NUMBER_RE.match(candidate):
                continue
            if _SYMBOL_RE.match(candidate) and candidate.upper() == candidate:
                symbol, symbol_at = candidate, back
                break
        if symbol is None or symbol_at is None:
            continue

        company = " ".join(lines[symbol_at + 1:index]).strip()
        if not company:
            continue

        rows.append(Constituent(
            symbol=symbol.upper(),
            company=company,
            sector=sector,
            price=float(price),
            weight_pct=float(weight),
            market_cap_crore=float(market_cap),
        ))

    # A symbol appearing twice would double-count; keep the first occurrence.
    seen: set[str] = set()
    unique: list[Constituent] = []
    for row in rows:
        if row.symbol in seen:
            continue
        seen.add(row.symbol)
        unique.append(row)
    return tuple(unique)


def load_snapshots(raw_dir: Path = DEFAULT_RAW,
                   index_token: str = "MidSmallcap_400") -> list[Snapshot]:
    """Every dated constituent table available in the raw ZIP archive."""
    import pymupdf

    raw_dir = Path(raw_dir)
    if not raw_dir.is_dir():
        return []

    snapshots: list[Snapshot] = []
    for zip_path in sorted(raw_dir.glob("indices_data*.zip")):
        parsed = _parse_month(zip_path)
        if parsed is None:
            continue
        year, month = parsed
        members = _members_for(zip_path, index_token)
        if not members:
            continue
        member = members[0]
        with zipfile.ZipFile(zip_path) as archive:
            payload = archive.read(member)
        document = pymupdf.open(stream=payload, filetype="pdf")
        try:
            text = "\n".join(page.get_text() for page in document)
        finally:
            document.close()
        rows = parse_constituent_text(text)
        if not rows:
            continue
        snapshots.append(Snapshot(
            as_of=_month_end(year, month),
            index_name=index_token,
            source_zip=zip_path.name,
            source_member=member,
            constituents=rows,
        ))
    snapshots.sort(key=lambda snap: snap.as_of)
    return snapshots


def membership_by_date(snapshots) -> dict[date, set[str]]:
    """``{snapshot date: symbol set}`` for the engine's universe argument."""
    return {snap.as_of: set(snap.symbols) for snap in snapshots}
