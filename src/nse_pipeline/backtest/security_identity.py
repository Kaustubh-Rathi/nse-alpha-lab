"""CANONICAL BACKTEST IDENTITY. Stable security identity for backtests: one key per economic security.

Status
------
This is the identity system used by the published research results:

* ``research/run_sweep.py`` imports ``resolve_identity`` from here.
* ``research/run_alpha_backtest.py`` imports ``resolve_identity`` from here.
* ``backtest/momentum_grid.py`` imports ``DEFAULT_EDGES`` and ``resolve_identity``.

``backtest/identity.py`` is a **different, active** identity system serving the
Stage-1 accounting ledger (``IdentityResolver``, ``EventTime``-gated). It is not
a duplicate of this module and cannot be replaced by it; see
``docs/IDENTITY.md`` for why the two are not interchangeable.

The problem this solves, measured on the real data:

* 551 symbols carry more than one ISIN over their history (ISIN changes).
* 442 ISINs are carried by more than one symbol (renames and reuse).
* Neither the symbol nor the ISIN is a stable key on its own.

The rule, applied in this order:

1. **Official symbol-change edges win.** ``data/processed/identity/identity_edges.jsonl``
   records 1,071 dated NSE symbol changes. An edge states that on its effective
   date, symbol OLD becomes symbol NEW: the two are one security. Therefore *all*
   of OLD's bars belong to NEW's security, including bars before the effective
   date. The chain is followed transitively so A->B->C resolves to C.
   A join is refused when the two sides' bar spans overlap, because overlapping
   spans mean the symbol was reused for a different company and the edge cannot
   be trusted to describe one continuous security.
2. **ISIN change within a symbol is followed.** When one symbol carries several
   ISINs in non-overlapping date spans, the latest ISIN is the security's current
   identity and earlier spans are joined to it. This is a *within-symbol* join:
   it never merges two different symbols.
3. **Nothing else is merged.** A symbol with no edge and one ISIN keeps its ISIN.
   A symbol whose spans overlap ambiguously is left unresolved and reported, not
   guessed.

Every resolution is recorded so a reviewer can audit which bars were re-keyed and
why. The function is pure: it reads the edges file the caller names and nothing
else.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EDGES = ROOT / "data" / "processed" / "identity" / "identity_edges.jsonl"


@dataclass
class IdentityResolution:
    """The result of resolving identity over a bar frame."""

    frame: pd.DataFrame
    key_column: str = "key"
    edges_applied: int = 0
    isin_changes_applied: int = 0
    unresolved: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def load_symbol_edges(path: Path = DEFAULT_EDGES) -> list[dict]:
    """Read the official symbol-change edges, oldest first."""
    edges: list[dict] = []
    path = Path(path)
    if not path.exists():
        return edges
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            old = str(row.get("old_symbol") or "").strip().upper()
            new = str(row.get("new_symbol") or "").strip().upper()
            effective = row.get("effective_date")
            if not old or not new or not effective:
                continue
            try:
                effective_date = date.fromisoformat(str(effective)[:10])
            except ValueError:
                continue
            edges.append({"old": old, "new": new, "effective": effective_date})
    edges.sort(key=lambda row: row["effective"])
    return edges


def _resolve_symbol_chain(symbol: str, edges: list[dict]) -> tuple[str, list[dict]]:
    """Follow symbol changes forward from ``symbol`` to its terminal symbol.

    Returns the terminal symbol and the edges traversed. The date is deliberately
    not used to gate the walk: an edge proves the old and new symbols are one
    security, so the old symbol's earlier bars belong to the successor too.
    """
    current = symbol
    seen = {current}
    traversed: list[dict] = []
    changed = True
    while changed:
        changed = False
        for edge in edges:
            if edge["old"] != current:
                continue
            if edge["new"] in seen:
                # A cycle is a data defect; stop rather than loop.
                break
            traversed.append(edge)
            current = edge["new"]
            seen.add(current)
            changed = True
            break
    return current, traversed


def resolve_identity(frame: pd.DataFrame,
                     edges_path: Path = DEFAULT_EDGES,
                     *,
                     symbol_column: str = "symbol",
                     isin_column: str = "isin",
                     date_column: str = "date") -> IdentityResolution:
    """Attach a stable ``key`` to every bar.

    The key is ``ISIN:<isin>`` where an ISIN is known after identity resolution,
    otherwise ``SYMBOL:<symbol>``. Two bars share a key only when they belong to
    the same economic security under the documented rules.
    """
    working = frame.copy()
    working[date_column] = pd.to_datetime(working[date_column]).dt.date
    working[symbol_column] = working[symbol_column].astype(str).str.upper()
    if isin_column in working.columns:
        working[isin_column] = working[isin_column].where(
            working[isin_column].notna(), None)
        working[isin_column] = working[isin_column].apply(
            lambda value: str(value).strip() if value else None)

    edges = load_symbol_edges(edges_path)
    unresolved: list[dict] = []

    # Bar span per symbol, used to detect symbol reuse before joining.
    spans = (working.groupby(symbol_column)[date_column]
             .agg(["min", "max"]).to_dict("index"))

    # 1. Symbol-change resolution: map each symbol to its terminal successor,
    #    refusing any join whose two sides overlap in time.
    terminal: dict[str, str] = {}
    for symbol in working[symbol_column].unique():
        target, traversed = _resolve_symbol_chain(symbol, edges)
        if target == symbol:
            continue
        blocked = None
        for edge in traversed:
            old_span = spans.get(edge["old"])
            new_span = spans.get(edge["new"])
            if old_span is None or new_span is None:
                continue
            if old_span["max"] >= new_span["min"]:
                blocked = {
                    "symbol": symbol,
                    "reason": "symbol_edge_spans_overlap",
                    "edge": f"{edge['old']}->{edge['new']}",
                    "effective": edge["effective"].isoformat(),
                    "old_last_bar": old_span["max"].isoformat(),
                    "new_first_bar": new_span["min"].isoformat(),
                }
                break
        if blocked is not None:
            unresolved.append(blocked)
            continue
        terminal[symbol] = target

    working["resolved_symbol"] = working[symbol_column].map(
        lambda symbol: terminal.get(symbol, symbol))
    edges_applied = int((working["resolved_symbol"] != working[symbol_column]).sum())

    # 2. Within-symbol ISIN changes: join earlier ISIN spans to the latest ISIN
    #    of the same resolved symbol, but only when the spans do not overlap.
    isin_changes = 0
    canonical_isin: dict[str, str] = {}
    if isin_column in working.columns:
        for symbol, group in working.groupby("resolved_symbol"):
            spans = (group.dropna(subset=[isin_column])
                     .groupby(isin_column)[date_column]
                     .agg(["min", "max"])
                     .sort_values("min"))
            if len(spans) <= 1:
                continue
            ordered = list(spans.iterrows())
            overlaps = any(
                ordered[index][1]["min"] <= ordered[index - 1][1]["max"]
                for index in range(1, len(ordered)))
            if overlaps:
                unresolved.append({
                    "symbol": symbol,
                    "reason": "isin_spans_overlap",
                    "isins": [str(name) for name, _ in ordered],
                })
                continue
            latest_isin = str(ordered[-1][0])
            for isin_name, _ in ordered[:-1]:
                canonical_isin[str(isin_name)] = latest_isin
                isin_changes += 1

    if isin_column in working.columns:
        working["canonical_isin"] = working[isin_column].map(
            lambda value: canonical_isin.get(value, value) if value else value)
    else:
        working["canonical_isin"] = None

    working["key"] = [
        f"ISIN:{isin}" if isin else f"SYMBOL:{symbol}"
        for isin, symbol in zip(working["canonical_isin"], working["resolved_symbol"])
    ]

    # 3. Report any key that still spans more than one symbol, which would mean
    #    two securities were merged and the caller must know.
    collisions = (working.groupby("key")["resolved_symbol"].nunique()
                  .loc[lambda series: series > 1])
    for key, count in collisions.items():
        unresolved.append({"key": str(key), "reason": "key_spans_multiple_symbols",
                           "symbols": int(count)})

    return IdentityResolution(
        frame=working,
        edges_applied=edges_applied,
        isin_changes_applied=isin_changes,
        unresolved=unresolved,
        stats={
            "bars": int(len(working)),
            "distinct_keys": int(working["key"].nunique()),
            "distinct_symbols": int(working["resolved_symbol"].nunique()),
            "symbol_edges_loaded": len(edges),
            "bars_rekeyed_by_symbol_edge": edges_applied,
            "isin_changes_joined": isin_changes,
            "unresolved": len(unresolved),
        },
    )
