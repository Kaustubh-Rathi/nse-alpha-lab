"""Load the monthly official constituent sets from the universe-construction lane.

The lane at ``verification/research/universe-construction/local/parsed/parsed_sets.json``
holds monthly official NSE constituent tables with per-file SHA-256 provenance:

* ``NIFTY_500``            107 months, 2013-05 .. 2022-03, 500-501 symbols
* ``NIFTY_MidSmallcap_400``  57 months, 2017-07 .. 2022-03, exactly 400 symbols
* ``NIFTY_Midcap_150``       57 months, 2017-07 .. 2022-03, exactly 150
* ``NIFTY_Smallcap_250``     57 months, 2017-07 .. 2022-03, exactly 250

This is materially better than the annual market-cap ZIPs for backtesting: a
monthly membership series means every rebalance has a proven universe, instead
of falling back to a stale annual snapshot.

Policy: a month's set proves membership on that month's ``as_of`` date only.
The loader returns exactly what the lane records and refuses a month whose
``status`` is not ``ok`` or whose symbol count is implausible, rather than
trimming or padding it to the nominal size.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SETS = (ROOT / "verification" / "research" / "universe-construction"
                / "local" / "parsed" / "parsed_sets.json")

#: Nominal size and the tolerance a month must fall within to be accepted.
NOMINAL = {
    "NIFTY_500": 500,
    "NIFTY_MidSmallcap_400": 400,
    "NIFTY_Midcap_150": 150,
    "NIFTY_Smallcap_250": 250,
}

#: Friendly aliases for the CLI.
ALIASES = {
    "nifty500": "NIFTY_500",
    "msc400": "NIFTY_MidSmallcap_400",
    "midcap150": "NIFTY_Midcap_150",
    "smallcap250": "NIFTY_Smallcap_250",
}


def resolve_index(name: str) -> str:
    """Accept either the lane key or a short alias."""
    if name in NOMINAL:
        return name
    alias = ALIASES.get(name.lower())
    if alias is None:
        raise ValueError(
            f"unknown index {name!r}; expected one of {sorted(NOMINAL)} "
            f"or an alias in {sorted(ALIASES)}")
    return alias


def load_monthly_sets(index: str, path: Path = DEFAULT_SETS) -> dict[date, set[str]]:
    """``{as_of date: symbol set}`` for every accepted month of ``index``.

    A month is skipped, not repaired, when its status is not ``ok`` or its
    symbol count is outside +/-2% of the index's nominal size.
    """
    index = resolve_index(index)
    path = Path(path)
    if not path.exists():
        return {}

    payload = json.loads(path.read_text(encoding="utf-8"))
    months = payload.get("sets", {}).get(index, {})
    nominal = NOMINAL[index]
    low, high = int(nominal * 0.98), int(nominal * 1.02)

    resolved: dict[date, set[str]] = {}
    for month, record in months.items():
        if record.get("status") != "ok":
            continue
        symbols = {str(value).strip().upper()
                   for value in record.get("symbols", []) if str(value).strip()}
        if not symbols or not (low <= len(symbols) <= high):
            continue
        try:
            as_of = date.fromisoformat(str(record["as_of"]))
        except (KeyError, ValueError):
            continue
        resolved[as_of] = symbols
    return resolved


def coverage(index: str, path: Path = DEFAULT_SETS) -> dict:
    """Summary of what the lane holds for one index, for a run manifest."""
    sets = load_monthly_sets(index, path)
    if not sets:
        return {"index": resolve_index(index), "months": 0}
    dates = sorted(sets)
    sizes = [len(sets[day]) for day in dates]
    return {
        "index": resolve_index(index),
        "months": len(dates),
        "first": dates[0].isoformat(),
        "last": dates[-1].isoformat(),
        "size_min": min(sizes),
        "size_max": max(sizes),
    }
