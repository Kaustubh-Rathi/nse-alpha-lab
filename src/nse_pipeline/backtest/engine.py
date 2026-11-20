"""LEGACY ENGINE. Shared-cash equal-weight rotation backtester (skeleton, honest limits).

Status
------
**This is NOT the engine that produces the published research results.** The
canonical engine is :mod:`nse_pipeline.backtest.momentum` (``momentum.py``),
which ``research/run_sweep.py`` and ``research/run_alpha_backtest.py`` import.
Neither module imports the other.

This module is retained, deliberately, for three reasons:

1. **Shared utilities still in use.** ``load_adjusted_bars``,
   ``load_bars_from_duckdb``, ``apply_identity_continuity``,
   ``build_key_stabilization``, ``apply_key_stabilization``, ``KeyStabilization``,
   ``CostConfig``, ``CircuitConfig``, ``UniverseConfig``, ``executable_reason``,
   ``month_end_dates``, ``annual_stats`` and ``momentum_signal`` are imported by
   ``history.py``, ``sensitivity.py``, ``total_return.py``, ``successor.py``,
   ``series_fallback.py`` and ``momentum_grid.py``.
2. **Legacy tests.** ``tests/test_backtester.py`` and
   ``tests/test_backtest_causality.py`` assert directly against
   ``RotationBacktester``; ``test_history.py``, ``test_total_return.py``,
   ``test_successor.py`` and ``test_loader_causality.py`` cover its loaders.
3. **Audit traceability.** ``docs/QUANT_REPOSITORY_AUDIT.md`` cites line numbers
   in this file. Removing it would orphan that evidence.

Known defects that motivated the move to ``momentum.py`` (do not "fix" these by
rewriting them here -- fix them in ``momentum.py``):

* **Stale marks forever.** A holding with no bar on a session is marked at its
  last observed close indefinitely (``RotationBacktester.equity_on``), so
  terminal wealth is inflated and drawdown is understated for any holding that
  stops trading. ``momentum.py`` instead refuses to invent a price and records
  the number of unpriceable positions.
* **Raw prices by default.** ``run_demo(adjusted=False)`` ranks momentum on raw
  (split-distorted) closes.
* **Slippage charged to cash, not applied to the fill price**, despite the
  inline comment claiming otherwise.
* **Overweight targets are never trimmed**; only names absent from the new
  target list are sold.

Do not add new callers of :class:`RotationBacktester`. See ``docs/ENGINE.md``.

Signal at close T, fill at open T+1. Costs, circuit/illiquidity skips,
survivorship-aware point-in-time universe, cash ledger, delisting flags.

Price mode (explicit, never silent):

* ``--adjusted`` (default off): bars come from
  ``data/processed/adjusted/adjusted_bars.parquet`` (see ``src/adjust.py``)
  and the engine runs on ``adj_open/adj_close/adj_previous_close`` --
  verified split/bonus-only adjustment (``adj = raw / cumulative_factor``,
  strictly before each ex-date). Dividends, rights, mergers, pre-2011
  actions and volume are NOT adjusted, so even adjusted mode is not a
  total-return series and no unbiased claim is made.
* Raw fallback (default): bars come from ``cash_equity_candidates`` in
  ``market.duckdb`` and the engine runs on raw exchange prices, as
  before. Raw results around corporate actions remain distorted.

Identity continuity (``--identity``, default on when the edges file
exists): official symbol-change edges
(``data/processed/identity/identity_edges.jsonl``, from ``src/identity.py``)
are applied date-boundedly to repair ``security_key`` splits that only
occur because ISIN is missing (pre-2011 / no ISIN, where the key falls
back to ``SYMBOL:<symbol>``). The corporate-actions API layer
(``data/processed/identity/api_identity_edges.jsonl``) is loaded with it and
gated fail-closed: an API edge may only remap a key when it is a dated step
with one unambiguous predecessor and successor, crosses no symbol reuse,
creates no branch or merge in the merged graph, and its share count is
provably continuous across the handover (the same ISIN under both symbols
proves the same instrument; two different ISINs prove the same company only,
and the handover's exchange ratio is then undocumented, so no 1:1 carry-over
may be made). Only SYMBOL-fallback keys are remapped,
only along a single unambiguous predecessor step whose effective date is
on/before the bar date, and never where both ISINs are present and
differ. No blind transitive chaining: each step must be an official edge
effective on/before the bar, ambiguous (branch/merge) steps stop the
walk, and ISIN evidence always wins over symbol lineage.
"""
from __future__ import annotations

import csv
import json
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from nse_pipeline.build.portable import open_catalog
from nse_pipeline.paths import data_root

import numpy as np
import pandas as pd

EPS = 1e-9

ADJUSTED_PARQUET_DEFAULT = str(data_root() / "processed/adjusted/adjusted_bars.parquet")
IDENTITY_EDGES_DEFAULT = str(data_root() / "processed/identity/identity_edges.jsonl")
API_IDENTITY_EDGES_DEFAULT = str(data_root() / "processed/identity/api_identity_edges.jsonl")

#: Maximum number of exchange sessions allowed between the last observed
#: null-ISIN bar and the first observed ISIN bar of the same symbol.
KEY_STABILIZATION_GAP_SESSIONS = 10


@dataclass
class CostConfig:
    brokerage_bps: float = 2.0         # per side, capped at brokerage_cap_per_order
    brokerage_cap_per_order: float = 2000.0
    stt_bps_buy: float = 10.0          # Indian delivery equity STT ~0.1%
    stt_bps_sell: float = 10.0
    stamp_bps: float = 1.5             # ~0.015% buy only; not a historical schedule
    exchange_bps: float = 0.3          # exchange txn charge approx
    gst_on_brokerage_pct: float = 18.0
    slippage_bps: float = 5.0          # applied adverse to fill price

    def cost_for(self, notional: float, side: str) -> float:
        """Cost charged against cash, separate from notional.

        Buy cost = notional*(brokerage+STT_buy+stamp+exchange+slippage);
        Sell cost = notional*(brokerage+STT_sell+exchange+slippage).
        Brokerage is bps of notional capped per order; GST applies to brokerage.
        """
        if notional <= 0:
            return 0.0
        is_buy = side.lower().startswith("buy")
        stt = self.stt_bps_buy if is_buy else self.stt_bps_sell
        brokerage = min(notional * self.brokerage_bps / 10_000.0,
                        self.brokerage_cap_per_order)
        gst = brokerage * self.gst_on_brokerage_pct / 100.0
        other = notional * (stt + (self.stamp_bps if is_buy else 0) + self.exchange_bps
                            + self.slippage_bps) / 10_000.0
        return brokerage + gst + other


@dataclass
class UniverseConfig:
    """Research-defined price/liquidity universe. NOT official Nifty."""
    label: str = "research-defined price/liquidity (not official Nifty)"
    min_price: float = 5.0
    min_volume: float = 5000.0         # volume on signal date T
    min_days_observed: int = 130       # history required before a symbol can rank
    min_history_sessions: int = 63     # for 3M momentum


@dataclass
class CircuitConfig:
    limit_pct: float = 0.20            # daily circuit limit
    series_limits: dict | None = None  # e.g. {"EQ": 0.20, "SM": 0.05}


def _limit_for(series: str | None, cfg: CircuitConfig) -> float:
    if cfg.series_limits and series in cfg.series_limits:
        return float(cfg.series_limits[series])
    return float(cfg.limit_pct)


def executable_reason(bar: dict, cfg: CircuitConfig) -> tuple[bool, str | None]:
    """Return (ok, reason). Skip if invalid, zero volume, or open-limit."""
    valid = bar.get("valid_ohlcv")
    if valid is None or pd.isna(valid) or not bool(valid):
        return False, "invalid_ohlcv"
    vol = bar.get("volume")
    if vol is None or (isinstance(vol, float) and pd.isna(vol)) or vol <= 0:
        return False, "zero_volume"
    opn = bar.get("open")
    prev = bar.get("previous_close")
    if opn is None or pd.isna(opn) or float(opn) <= 0:
        return False, "invalid_open"
    if prev is not None and not pd.isna(prev) and float(prev) > 0:
        lim = _limit_for(bar.get("series"), cfg)
        if float(opn) >= float(prev) * (1 + lim) - 1e-8:
            return False, "open_upper_limit"
        if float(opn) <= float(prev) * (1 - lim) + 1e-8:
            return False, "open_lower_limit"
    return True, None


def month_end_dates(session_dates: list[date]) -> list[date]:
    """Last observed session of each calendar month."""
    if not session_dates:
        return []
    by_month: dict[tuple[int, int], date] = {}
    for d in sorted(session_dates):
        by_month[(d.year, d.month)] = d
    return sorted(by_month.values())


def annual_stats(equity: pd.Series, sessions_per_year: int = 252) -> dict:
    """Compute total return, CAGR, vol, max drawdown from an equity curve."""
    eq = equity.dropna()
    if len(eq) < 2 or eq.iloc[0] <= 0:
        return {"total_return": 0.0, "cagr": 0.0, "ann_vol": 0.0,
                "max_drawdown": 0.0, "n_sessions": int(len(eq))}
    rets = eq.pct_change().dropna()
    total = float(eq.iloc[-1] / eq.iloc[0] - 1)
    years = max(len(eq) / sessions_per_year, 1 / sessions_per_year)
    cagr = float((eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1)
    vol = float(rets.std() * (sessions_per_year ** 0.5)) if len(rets) > 1 else 0.0
    roll_max = eq.cummax()
    dd = (eq / roll_max - 1).min()
    return {"total_return": total, "cagr": cagr, "ann_vol": vol,
            "max_drawdown": float(dd), "n_sessions": int(len(eq))}


def _to_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    return pd.to_datetime(value).date()


def load_identity_edges(path: str | Path) -> list[dict]:
    """Load official identity edges (one JSON object per line).

    Returns a list of dicts with at least ``old_symbol``, ``new_symbol``
    and ``effective_date`` (ISO string). Excludes nothing -- callers
    decide how to handle ambiguous or ISIN-contradicting edges.
    """
    edges: list[dict] = []
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if line:
                edges.append(json.loads(line))
    return edges


def load_merged_identity_edges(official_path: str | Path = IDENTITY_EDGES_DEFAULT,
                               api_path: str | Path = API_IDENTITY_EDGES_DEFAULT) -> dict:
    """Both official identity layers, merged with distinguishable provenance.

    The official symbol-change rows and the corporate-actions API edges are
    loaded together and every edge carries an ``edge_source``. The API edges
    are gated fail-closed by the identity layer
    (:func:`nse_pipeline.identity.lineage.merge_identity_edges`): only the
    edges in ``remap_edges`` may remap a key, and every refused edge is
    reported in ``refused`` with its reason.
    """
    from nse_pipeline.identity.lineage import merge_identity_edges, read_jsonl

    official = read_jsonl(Path(official_path))
    api = read_jsonl(Path(api_path))
    return merge_identity_edges(official, api)


def _root_for_symbol(symbol: str, on: date, pred: dict, succ: dict | None = None) -> str:
    """Earliest symbol reachable by unambiguous predecessor steps <= ``on``.

    Each step must be an official edge ``old -> cur`` with
    ``effective_date <= on`` (date-bounded). The walk stops at the first
    step with no predecessor, with >1 distinct predecessor (merge
    ambiguity), with >1 distinct successor of the predecessor effective
    on/before ``on`` (branch ambiguity), or on a cycle -- so no blind
    chaining occurs.
    """
    cur = symbol
    seen = {symbol}
    while True:
        cands = [(eff, old) for eff, old in pred.get(cur, []) if eff <= on]
        if not cands:
            return cur
        olds = {old for _, old in cands}
        if len(olds) != 1:
            return cur  # ambiguous merge: do not guess
        old = next(iter(olds))
        if succ is not None:
            news = {new for eff, new in succ.get(old, []) if eff <= on}
            if len(news) != 1:
                return cur  # branch ambiguity: do not guess
        if old in seen:
            return cur  # cycle guard
        seen.add(old)
        cur = old


def apply_identity_continuity(bars: pd.DataFrame, edges: list[dict]) -> tuple[pd.DataFrame, dict]:
    """Remap SYMBOL-fallback keys along date-bounded identity edges.

    Only rows whose ``key``/``security_key`` is a ``SYMBOL:`` fallback
    (no ISIN, so ``security_key`` splits at a rename) are candidates for
    remapping; ISIN-keyed rows already carry continuity and are never
    overridden. An edge whose two ISINs are both present and differ is
    excluded (a rename that changed ISIN is not assumed to be price
    continuity here). An edge the identity layer refused (``remap_admissible``
    is ``False``) is excluded as well and counted separately: a refused edge
    never remaps a key, so no holding is ever carried across a handover whose
    share exchange ratio is undocumented. Returns ``(bars, stats)``;
    ``bars`` gains ``identity_canonical_symbol`` and ``identity_remapped``
    columns.
    """
    frame = bars.copy().reset_index(drop=True)
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    stats = {"edges_total": len(edges), "edges_excluded_isin_change": 0,
             "edges_excluded_not_remappable": 0,
             "edges_used": 0, "bars_remapped": 0, "bars_candidates": 0}
    if not edges or frame.empty:
        frame["identity_canonical_symbol"] = frame["symbol"]
        frame["identity_remapped"] = False
        return frame, stats
    pred: dict[str, list[tuple[date, str]]] = {}
    succ: dict[str, list[tuple[date, str]]] = {}
    used_edges: set[tuple] = set()
    for edge in edges:
        if edge.get("remap_admissible") is False:
            stats["edges_excluded_not_remappable"] += 1
            continue
        if edge.get("share_count_multiplier") is not None:
            # The handover's share-count change is carried by the applied
            # action audit, which this loader does not read. The key is not
            # remapped, so no holding is carried across such a handover.
            stats["edges_excluded_not_remappable"] += 1
            continue
        old = str(edge["old_symbol"])
        new = str(edge["new_symbol"])
        # The remap boundary is the edge's own dated step: the identity layer
        # publishes it as ``remap_effective_date`` (for an API edge the
        # observed trading handover, which is the only date on which the two
        # price series can be joined) and falls back to ``effective_date``.
        eff = _to_date(edge.get("remap_effective_date") or edge.get("effective_date"))
        if eff is None:
            continue
        isin_old, isin_new = edge.get("isin_old"), edge.get("isin_new")
        if isin_old and isin_new and str(isin_old) != str(isin_new):
            stats["edges_excluded_isin_change"] += 1
            continue
        pred.setdefault(new, []).append((eff, old))
        succ.setdefault(old, []).append((eff, new))
        used_edges.add((old, new, eff.isoformat()))
    for symbol_edges in pred.values():
        symbol_edges.sort()
    for symbol_edges in succ.values():
        symbol_edges.sort()
    stats["edges_used"] = len(used_edges)

    # Per-symbol breakpoints: root is constant between effective dates of
    # edges reachable from the symbol, so evaluate the walk only there.
    canonical = frame["symbol"].astype(str).to_numpy().copy()
    remapped = np.zeros(len(frame), dtype=bool)
    if "key" in frame.columns:
        keys = frame["key"].astype(str).to_numpy()
    elif "security_key" in frame.columns:
        keys = frame["security_key"].astype(str).to_numpy()
    else:  # synthetic bars keyed by symbol directly
        keys = frame["symbol"].astype(str).to_numpy()
    is_fallback = np.array([k.startswith("SYMBOL:") or ":" not in k for k in keys])
    # Vectorised per symbol group.
    dates_all = frame["date"].to_numpy(dtype="datetime64[D]")
    for symbol, idx in frame.groupby(frame["symbol"].astype(str), sort=False).groups.items():
        pos = np.asarray(idx)
        # Collect candidate breakpoint dates from reachable edges.
        breakpoints: set = set()
        frontier = [symbol]
        visited = set()
        while frontier:
            cur = frontier.pop()
            if cur in visited:
                continue
            visited.add(cur)
            for eff, old in pred.get(cur, []):
                breakpoints.add(np.datetime64(eff))
                frontier.append(old)
        if not breakpoints:
            continue
        bp = np.array(sorted(breakpoints))
        group_dates = dates_all[pos]
        # For each group date, find the representative breakpoint: the
        # largest bp <= date (root evaluated at that date), or the date
        # itself when before every breakpoint (root == symbol there, since
        # no predecessor step is effective yet for the first step --
        # evaluate directly to stay exact in corner cases).
        seg = np.searchsorted(bp, group_dates, side="right") - 1
        roots: dict[int, str] = {}
        group_roots = np.empty(len(pos), dtype=object)
        for i, s in enumerate(seg):
            rep = group_dates[i] if s < 0 else bp[s]
            rep_date = pd.Timestamp(rep).date()
            key_seg = int(s)
            if key_seg not in roots:
                roots[key_seg] = _root_for_symbol(str(symbol), rep_date, pred, succ)
            group_roots[i] = roots[key_seg]
        for j, p in enumerate(pos):
            root = group_roots[j]
            canonical[p] = root
            if root != symbol and is_fallback[p]:
                remapped[p] = True
    stats["bars_candidates"] = int(is_fallback.sum())
    stats["bars_remapped"] = int(remapped.sum())
    frame["identity_canonical_symbol"] = canonical
    frame["identity_remapped"] = remapped
    if "key" in frame.columns:
        new_keys = keys.copy()
        new_keys[remapped] = ["SYMBOL:" + s for s in canonical[remapped]]
        frame["key"] = new_keys
    elif "security_key" in frame.columns:
        new_keys = keys.copy()
        new_keys[remapped] = ["SYMBOL:" + s for s in canonical[remapped]]
        frame["security_key"] = new_keys
        frame["key"] = new_keys
    else:
        new_keys = keys.copy()
        new_keys[remapped] = canonical[remapped]
        frame["key"] = new_keys
    return frame, stats


# ---------------------------------------------------------------------------
# Key stabilization: one proven security key per symbol, from the bars only
# ---------------------------------------------------------------------------


@dataclass
class KeyStabilization:
    """One proven security key per symbol, built from the bars themselves.

    A symbol whose own bars prove a single security -- null-ISIN bars first,
    exactly one non-blank ISIN afterwards, a contiguous handover, and no
    evidence of a reused symbol or of a rename boundary -- carries that ISIN
    on every one of its bars, including the bars that predate the ISIN.

    ``canonical`` holds only the proven symbols. Everything else stays in
    ``unresolved`` with the reason it was refused: an ambiguous symbol is
    never merged, only reported.
    """

    canonical: dict[str, str] = field(default_factory=dict)
    evidence: list[dict] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def key_for(self, symbol: str) -> str | None:
        """The proven canonical key of ``symbol`` (``None`` when unproven)."""
        return self.canonical.get(str(symbol))

    def as_dict(self) -> dict:
        return {"canonical": dict(self.canonical),
                "evidence": list(self.evidence),
                "unresolved": list(self.unresolved),
                "stats": dict(self.stats)}


def _normalise_company_name(name: str) -> str:
    """Whitespace-collapsed, case-folded company name."""
    return " ".join(str(name).split()).casefold()


def _read_jsonl_rows(path: Path):
    """Yield the dict rows of a JSON-lines file (missing file: nothing)."""
    if not path.exists():
        return
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _official_company_names(data_path: str | Path) -> dict[str, set[str]]:
    """Official company names per symbol, from the two official name sources.

    The corporate-actions API rows (``comp``) and the delisted list
    (``Company``) are the only name evidence used. A symbol that appears
    under two different names is a *reused* symbol: the name change proves
    neither that the two lives are one security nor that they are two, so
    such a symbol is never stabilized.
    """
    root = Path(data_path)
    names: dict[str, set[str]] = defaultdict(set)
    for row in _read_jsonl_rows(root / "processed/older_actions/nse-api-actions.jsonl"):
        symbol, company = row.get("symbol"), row.get("comp")
        if symbol and company:
            names[str(symbol).strip()].add(_normalise_company_name(company))
    delisted = root / "raw/security_master/delisted.csv"
    if delisted.exists():
        raw = delisted.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
        for row in csv.DictReader(text.splitlines()):
            symbol = str(row.get("Symbol") or row.get("symbol") or "").strip()
            company = str(row.get("Company") or row.get("company") or "")
            if symbol and company.strip():
                names[symbol].add(_normalise_company_name(company))
    return names


def _identity_edge_symbols(data_path: str | Path) -> dict[str, str]:
    """Symbols that sit on either side of an official identity edge.

    A symbol on a rename boundary is never stabilized: the boundary is
    exactly where "the same ticker" stops proving "the same security", and
    the edge may carry a share-count change that the bars do not prove.
    """
    root = Path(data_path)
    blocked: dict[str, str] = {}
    for name in ("identity_edges.jsonl", "api_identity_edges.jsonl"):
        for row in _read_jsonl_rows(root / "processed/identity" / name):
            for side in ("old_symbol", "new_symbol"):
                symbol = row.get(side)
                if symbol:
                    blocked.setdefault(str(symbol).strip(),
                                       f"official_identity_edge:{name}:{side}")
    return blocked


def build_key_stabilization(
        db_path: str | Path,
        data_path: str | Path | None = None,
        max_gap_sessions: int = KEY_STABILIZATION_GAP_SESSIONS,
        connection=None) -> KeyStabilization:
    """Build the per-symbol key stabilization map from the bars themselves.

    Only the identity fields of the bars (``symbol``, ``series``, ``isin``)
    and their observed dates are read. No price is read, so no price can
    influence the result, and no symbol list is consulted.

    A symbol is stabilized only when all of the following hold:

    * it has null-ISIN bars and non-blank ISIN bars (it is a candidate);
    * exactly one distinct non-blank ISIN is observed for it, anywhere;
    * that ISIN is an INE-family ISIN;
    * the symbol is not the old or the new side of an official identity edge
      (a rename boundary);
    * the symbol is not observed under two official company names (reuse);
    * the null-ISIN period and the ISIN period do not interleave;
    * the handover gap -- exchange sessions between the last observed
      null-ISIN bar and the first observed ISIN bar -- is at most
      ``max_gap_sessions``;
    * no other symbol claims that ISIN.

    Every refusal is reported in ``unresolved`` with its reason. A symbol
    that fails any rule keeps the key it has today: the map never merges
    two securities.

    ``connection`` reuses an already open read-only DuckDB connection (the
    database file may not be opened twice with a different configuration).
    """
    import duckdb

    root = Path(data_path) if data_path is not None else Path(db_path).parent
    stabilization = KeyStabilization()
    if connection is None and not Path(db_path).exists():
        stabilization.stats = {"candidates": 0, "stabilized": 0, "unresolved": 0,
                               "reason": "bars_database_not_available"}
        return stabilization
    if connection is not None:
        con, owned = connection, False
    else:
        con, owned = open_catalog(database=db_path), True
    try:
        spans = con.execute("""
            SELECT symbol,
                   min(date) FILTER (WHERE isin IS NULL OR trim(isin) = '') AS null_first,
                   max(date) FILTER (WHERE isin IS NULL OR trim(isin) = '') AS null_last,
                   count(*) FILTER (WHERE isin IS NULL OR trim(isin) = '') AS null_rows,
                   min(date) FILTER (WHERE isin IS NOT NULL AND trim(isin) <> '') AS isin_first,
                   max(date) FILTER (WHERE isin IS NOT NULL AND trim(isin) <> '') AS isin_last,
                   count(*) FILTER (WHERE isin IS NOT NULL AND trim(isin) <> '') AS isin_rows,
                   count(DISTINCT trim(isin))
                       FILTER (WHERE isin IS NOT NULL AND trim(isin) <> '') AS distinct_isins,
                   min(trim(isin))
                       FILTER (WHERE isin IS NOT NULL AND trim(isin) <> '') AS one_isin
            FROM bars
            GROUP BY symbol
        """).fetchall()
        pairs = con.execute("""
            SELECT DISTINCT symbol, trim(isin) FROM bars
            WHERE isin IS NOT NULL AND trim(isin) <> ''
        """).fetchall()
        sessions = [row[0] for row in con.execute(
            "SELECT DISTINCT date FROM bars ORDER BY date").fetchall()]
    finally:
        if owned:
            con.close()

    isin_owners: dict[str, set[str]] = defaultdict(set)
    for symbol, isin in pairs:
        isin_owners[isin].add(str(symbol))
    company_names = _official_company_names(root)
    edge_symbols = _identity_edge_symbols(root)

    def refuse(symbol: str, reason: str, detail: dict) -> None:
        stabilization.unresolved.append(dict(symbol=symbol, reason=reason, **detail))

    for (symbol, null_first, null_last, null_rows, isin_first, isin_last,
         isin_rows, distinct_isins, one_isin) in spans:
        symbol = str(symbol)
        if not null_rows or not isin_rows:
            continue  # not a candidate: nothing to stabilize
        stabilization.stats["candidates"] = stabilization.stats.get("candidates", 0) + 1
        if distinct_isins > 1:
            refuse(symbol, "multiple_distinct_isins",
                   {"distinct_isins": int(distinct_isins)})
            continue
        isin = str(one_isin)
        if not isin.startswith("INE"):
            refuse(symbol, "canonical_isin_not_ine_family", {"isin": isin})
            continue
        edge = edge_symbols.get(symbol)
        if edge:
            refuse(symbol, "symbol_on_official_identity_edge",
                   {"isin": isin, "edge": edge})
            continue
        names = sorted(company_names.get(symbol, ()))
        if len(names) > 1:
            refuse(symbol, "symbol_reuse_official_company_names",
                   {"isin": isin, "company_names": names})
            continue
        if not (null_last < isin_first):
            refuse(symbol, "null_isin_periods_interleave",
                   {"isin": isin, "null_isin_first": str(null_first),
                    "null_isin_last": str(null_last), "isin_first": str(isin_first),
                    "isin_last": str(isin_last)})
            continue
        gap = bisect_right(sessions, isin_first) - bisect_right(sessions, null_last)
        if gap > max_gap_sessions:
            refuse(symbol, "handover_gap_exceeds_limit",
                   {"isin": isin, "handover_gap_sessions": int(gap),
                    "max_gap_sessions": int(max_gap_sessions),
                    "null_isin_last": str(null_last), "isin_first": str(isin_first)})
            continue
        owners = sorted(isin_owners.get(isin, ()))
        if len(owners) > 1:
            refuse(symbol, "canonical_isin_claimed_by_another_symbol",
                   {"isin": isin, "other_symbols": [o for o in owners if o != symbol]})
            continue
        stabilization.canonical[symbol] = "ISIN:" + isin
        stabilization.evidence.append({
            "symbol": symbol,
            "canonical_key": "ISIN:" + isin,
            "isin": isin,
            "null_isin_first": str(null_first),
            "null_isin_last": str(null_last),
            "null_isin_rows": int(null_rows),
            "isin_first": str(isin_first),
            "isin_last": str(isin_last),
            "isin_rows": int(isin_rows),
            "handover_gap_sessions": int(gap),
        })
    stabilization.stats.update({
        "candidates": stabilization.stats.get("candidates", 0),
        "stabilized": len(stabilization.canonical),
        "unresolved": len(stabilization.unresolved),
        "max_gap_sessions": int(max_gap_sessions),
        "unresolved_by_reason": dict(Counter(
            row["reason"] for row in stabilization.unresolved)),
    })
    return stabilization


def apply_key_stabilization(frame: pd.DataFrame,
                            stabilization: KeyStabilization | None
                            ) -> tuple[pd.DataFrame, dict]:
    """Give a proven symbol one key for its whole life.

    Only a symbol the stabilization map proved is rewritten, and only to the
    canonical key the map published. A symbol the map refused keeps the key
    it has today. The frame gains a ``key_stabilized`` audit column.
    """
    stats = {"symbols_stabilized": 0, "rows_rewritten": 0,
             "symbols_refused": len(stabilization.unresolved) if stabilization else 0}
    if frame is None or frame.empty:
        return frame, stats
    frame = frame.copy()
    if stabilization is None or not stabilization.canonical:
        frame["key_stabilized"] = False
        return frame, stats
    symbols = frame["symbol"].astype(str)
    target = symbols.map(stabilization.canonical)
    hit = target.notna()
    if hit.any():
        canonical = target.astype(object)
        if "security_key" in frame.columns:
            frame["security_key"] = frame["security_key"].astype(object).where(~hit, canonical)
        if "key" in frame.columns:
            frame["key"] = frame["key"].astype(object).where(~hit, canonical)
        else:
            frame["key"] = canonical
        stats["rows_rewritten"] = int(hit.sum())
        stats["symbols_stabilized"] = int(symbols[hit].nunique())
    frame["key_stabilized"] = hit.to_numpy()
    return frame, stats


def map_adjusted_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Map adjusted parquet columns onto the engine's price columns.

    ``adj_open/adj_close/adj_previous_close`` become
    ``open/close/previous_close`` (raw values kept as ``raw_*``);
    ``adjustment_factor``/``is_adjusted`` are carried through for audit.
    Pure function -- the engine itself never inspects factors.
    """
    frame = df.copy()
    for col in ("open", "high", "low", "close", "previous_close"):
        adj = f"adj_{col}"
        if adj in frame.columns:
            frame[f"raw_{col}"] = frame[col]
            frame[col] = frame[adj]
    if "adj_previous_close" in frame.columns:
        frame["previous_close"] = frame["adj_previous_close"]
    return frame


def load_adjusted_bars(parquet_path: str = ADJUSTED_PARQUET_DEFAULT,
                       start: str = "2021-01-01", end: str = "2022-12-31",
                       series_filter: tuple[str, ...] = ("EQ",),
                       identity_edges: list[dict] | None = None,
                       key_stabilization: KeyStabilization | None = None) -> pd.DataFrame:
    """Load adjusted bars using contemporaneous research-universe evidence.

    The SQL below reproduces the exact view definition in
    ``build_data.create_catalog`` (usable equity candidates: series in
    EQ/BE/BZ/SM/ST, valid OHLCV, volume > 0; keep ``INE%`` or NULL ISIN;
    never use a future ``INF%``/``IN9%`` observation to erase prior rows), then
    applies ``series_filter`` and maps ``adj_*`` onto the engine columns
    via :func:`map_adjusted_columns`. Raw values remain in ``raw_*``.

    ``key_stabilization`` gives a proven symbol one key for its whole life
    (see :func:`build_key_stabilization`); every other symbol keeps the key
    it has today.
    """
    import duckdb

    quoted = str(Path(parquet_path).resolve()).replace("\\", "/").replace("'", "''")
    con = duckdb.connect()
    try:
        con.execute(f"CREATE OR REPLACE VIEW _adjusted_bars AS SELECT * FROM read_parquet('{quoted}')")
        series_sql = ""
        if series_filter:
            series_sql = " AND series IN (" + ",".join("?" for s in series_filter) + ")"
        q = f"""
        WITH usable AS (
            SELECT * FROM _adjusted_bars
            WHERE series IN ('EQ','BE','BZ','SM','ST') AND valid_ohlcv AND volume > 0
        )
        SELECT date, symbol, series, isin, open, high, low, close, previous_close,
               volume, valid_ohlcv,
               adj_open, adj_high, adj_low, adj_close, adj_previous_close,
               adjustment_factor, n_future_actions, is_adjusted,
               CASE WHEN isin IS NOT NULL THEN 'ISIN:' || isin
                    ELSE 'SYMBOL:' || symbol END AS security_key
        FROM usable
        WHERE date BETWEEN ? AND ?{series_sql}
          AND (isin LIKE 'INE%' OR isin IS NULL)
        ORDER BY date, symbol
        """
        df = con.execute(q, [start, end, *(series_filter or ())]).fetchdf()
    finally:
        con.close()
    df = map_adjusted_columns(df)
    df["key"] = df["security_key"]
    if identity_edges:
        df, _ = apply_identity_continuity(df, identity_edges)
    if key_stabilization is not None:
        df, _ = apply_key_stabilization(df, key_stabilization)
    return df.sort_values(["date", "key"]).reset_index(drop=True)


class RotationBacktester:
    """Equal-weight rotation with next-session execution and shared cash."""

    def __init__(self, costs: CostConfig | None = None,
                 universe: UniverseConfig | None = None,
                 circuit: CircuitConfig | None = None):
        self.costs = costs or CostConfig()
        self.universe = universe or UniverseConfig()
        self.circuit = circuit or CircuitConfig()

    def build_eligible(self, bars: pd.DataFrame, signal_date: date) -> pd.DataFrame:
        """Point-in-time eligible set: only symbols observed at signal_date.

        Survivorship-aware: never uses a symbol list from the future.
        Returns rows for signal_date meeting liquidity filters.
        """
        day = bars[bars["date"] == signal_date]
        if day.empty:
            return day
        price = day["raw_close"] if "raw_close" in day else day["close"]
        ok = day[
            (day["valid_ohlcv"] == True)  # noqa: E712
            & (day["volume"] >= self.universe.min_volume)
            & (day["volume"] > 0)
            & (price >= self.universe.min_price)
        ]
        return ok

    def rank_momentum(self, bars: pd.DataFrame, signal_date: date,
                      lookback_sessions: int = 63, _cache: dict | None = None) -> pd.DataFrame:
        """3M (63-session) return rank among eligible symbols at signal_date."""
        day_map = _cache["day_map"] if _cache else None
        sessions = _cache["sessions"] if _cache else sorted(bars["date"].unique())
        idx = sessions.index(signal_date) if signal_date in sessions else None
        if idx is None or idx < lookback_sessions:
            return pd.DataFrame()
        anchor = sessions[idx - lookback_sessions]
        if day_map is not None:
            cur = day_map[signal_date].copy()
            old = day_map[anchor][["key", "close"]].rename(columns={"close": "anchor_close"})
        else:
            cur = bars[bars["date"] == signal_date].copy()
            old = bars[bars["date"] == anchor][["key", "close"]].rename(columns={"close": "anchor_close"})
        eligible = self.build_eligible(cur, signal_date)
        if _cache is not None and "history_counts" in _cache:
            counts = _cache["history_counts"].loc[eligible.index]
            eligible = eligible[counts >= self.universe.min_days_observed]
        else:
            counts = bars[bars["date"] <= signal_date].groupby("key")["date"].nunique()
            eligible = eligible[eligible["key"].map(counts) >= self.universe.min_days_observed]
        m = eligible.merge(old, on="key", how="inner", validate="one_to_one")
        m = m[(m["anchor_close"] > 0) & (m["close"] > 0) & (m["valid_ohlcv"] == True)]
        m["mom_return"] = m["close"] / m["anchor_close"] - 1
        m = m.sort_values(["mom_return", "key"], ascending=[False, True], kind="stable")
        return m

    def run(self, bars: pd.DataFrame, rebalance_dates: list[date],
            signal_fn, initial_cash: float = 1_000_000.0,
            max_positions: int = 20, debug: bool = False) -> dict:
        """Execute rotation. signal_fn(self, bars, signal_date) -> list of keys."""
        bars = bars.copy()
        bars["date"] = pd.to_datetime(bars["date"]).dt.date
        if bars.duplicated(["date", "key"]).any():
            raise ValueError("Duplicate security/date bars: resolve identity or series ambiguity before execution")
        day_map: dict[date, pd.DataFrame] = {d: g for d, g in bars.groupby("date")}
        sessions = sorted(day_map.keys())
        # Construct row dictionaries for one execution session at a time,
        # rather than retaining millions of full-history dictionaries.
        closes: dict[date, dict[str, float]] = {
            d: dict(zip(g["key"].tolist(), g["close"].tolist())) for d, g in day_map.items()
        }
        counts = bars.sort_values(["key", "date"]).groupby("key").cumcount() + 1
        cache = {"day_map": day_map, "sessions": sessions, "history_counts": counts}

        cash = float(initial_cash)
        positions: dict[str, dict] = {}  # key -> {shares, avg_entry, last_price, last_seen, flag}
        trades: list[dict] = []
        skips: list[dict] = []
        # last known close per key
        last_price: dict[str, float] = {}
        last_seen: dict[str, date] = {}

        def equity_on(d: date) -> float:
            eq = cash
            for k, p in positions.items():
                px = closes.get(d, {}).get(k, last_price.get(k, 0.0))
                eq += p["shares"] * px
            return eq

        sess_idx = {s: i for i, s in enumerate(sessions)}
        exec_map: dict[date, date | None] = {}
        for sd in rebalance_dates:
            i = sess_idx.get(sd)
            if i is None:
                continue
            exec_map[sd] = sessions[i + 1] if i + 1 < len(sessions) else None

        # Equity curve: mark every session from first execution onward
        equity_rows: list[dict] = []

        # Iterate sessions in order; executions happen at open of exec date
        pending: dict[date, list[tuple[str, date]]] = {}  # exec_date -> [(key, signal_date)]
        for sd in rebalance_dates:
            ed = exec_map.get(sd)
            if ed is None:
                continue
            try:
                targets = list(signal_fn(self, bars, sd, cache))[:max_positions]
            except TypeError:
                targets = list(signal_fn(self, bars, sd))[:max_positions]
            pending.setdefault(ed, []).extend([(k, sd) for k in targets])

        for d in sessions:
            # executions scheduled at open of d
            if d in pending:
                lookup = {row.key: row._asdict() for row in day_map[d].itertuples(index=False)}
                # At the opening auction only current opens and preceding
                # closes are available. Today's closes must not size orders.
                def opening_value(k):
                    row = lookup.get(k)
                    opn = row.get("open") if row else None
                    return float(opn) if opn is not None and pd.notna(opn) and float(opn) > 0 else last_price.get(k, 0.0)
                items = pending[d]
                items = list(dict.fromkeys(items))  # dedupe preserving order
                targets = [k for k, _ in items]
                sig_of = {k: sd for k, sd in items}
                # Sells first: holdings not in targets
                for k in list(positions.keys()):
                    if k in targets:
                        continue
                    bar = lookup.get(k)
                    if bar is None:
                        skips.append({"date": d, "key": k, "side": "sell",
                                      "reason": "no_bar_delisted_or_suspended"})
                        positions[k]["flag"] = "delisted_or_suspended"
                        continue
                    ok, reason = executable_reason(bar, self.circuit)
                    if not ok:
                        skips.append({"date": d, "key": k, "side": "sell", "reason": reason})
                        positions[k]["flag"] = f"suspended_{reason}"
                        continue
                    fill = float(bar["open"])
                    shares = positions[k]["shares"]
                    notional = shares * fill
                    cost = self.costs.cost_for(notional, "sell")
                    cash += notional - cost
                    trades.append({"date": d, "signal_date": None, "key": k,
                                   "symbol": bar.get("symbol", k), "side": "sell",
                                   "fill_price": fill, "shares": shares,
                                   "notional": notional, "cost": cost})
                    del positions[k]
                # Buys: equal weight allocation of current equity
                eq = cash + sum(p["shares"] * opening_value(k)
                                for k, p in positions.items())
                n = len(targets)
                if n:
                    per = eq / n
                    for k in targets:
                        held = positions[k]["shares"] * opening_value(k) \
                            if k in positions else 0.0
                        need = max(per - held, 0.0)
                        if need <= 0:
                            continue
                        bar = lookup.get(k)
                        if bar is None:
                            skips.append({"date": d, "key": k, "side": "buy",
                                          "reason": "no_bar_at_execution"})
                            continue
                        ok, reason = executable_reason(bar, self.circuit)
                        if not ok:
                            skips.append({"date": d, "key": k, "side": "buy", "reason": reason})
                            continue
                        fill = float(bar["open"])
                        # affordable shares after cost (iterate once)
                        shares = int(min(need, cash) // fill)
                        while shares > 0:
                            notional = shares * fill
                            cost = self.costs.cost_for(notional, "buy")
                            if notional + cost <= cash:
                                break
                            shares -= 1
                        if shares <= 0:
                            skips.append({"date": d, "key": k, "side": "buy",
                                          "reason": "insufficient_cash"})
                            continue
                        notional = shares * fill
                        cost = self.costs.cost_for(notional, "buy")
                        cash -= notional + cost
                        trades.append({"date": d, "signal_date": sig_of.get(k), "key": k,
                                       "symbol": bar.get("symbol", k), "side": "buy",
                                       "fill_price": fill, "shares": shares,
                                       "notional": notional, "cost": cost})
                        if k in positions:
                            old = positions[k]
                            tot = old["shares"] + shares
                            old["avg_entry"] = (old["shares"] * old["avg_entry"]
                                                + shares * fill) / tot
                            old["shares"] = tot
                            old["last_seen"] = d
                            old["flag"] = "active"
                        else:
                            positions[k] = {"shares": shares, "avg_entry": fill,
                                            "last_price": fill, "last_seen": d,
                                            "flag": "active"}
            # EOD data become visible only after opening orders are complete.
            for k, px in closes.get(d, {}).items():
                last_price[k] = px
                last_seen[k] = d
            # EOD marking: flag stale holdings (no bar today)
            day_closes = closes.get(d, {})
            for k, p in positions.items():
                if k not in day_closes:
                    p["flag"] = "delisted_or_suspended"
                else:
                    if p["flag"].startswith("delisted"):
                        p["flag"] = "active"
                    p["last_seen"] = d
            equity_rows.append({"date": d, "cash": cash,
                                "positions_value": equity_on(d) - cash,
                                "equity": equity_on(d),
                                "n_positions": len(positions)})

        equity_df = pd.DataFrame(equity_rows)
        trades_df = pd.DataFrame(trades)
        skips_df = pd.DataFrame(skips)
        # position ledger
        ledger = [{"key": k, "shares": p["shares"], "avg_entry": p["avg_entry"],
                   "last_price": last_price.get(k, p["avg_entry"]),
                   "last_seen": p.get("last_seen"), "flag": p.get("flag", "active")}
                  for k, p in positions.items()]
        stats = annual_stats(equity_df.set_index("date")["equity"]) if not equity_df.empty \
            else {"total_return": 0, "cagr": 0, "ann_vol": 0, "max_drawdown": 0, "n_sessions": 0}
        stats["n_trades"] = int(len(trades_df))
        stats["n_skips"] = int(len(skips_df))
        stats["final_cash"] = float(cash)
        stats["final_equity"] = float(equity_df["equity"].iloc[-1]) if not equity_df.empty else cash
        return {"equity": equity_df, "trades": trades_df, "skips": skips_df,
                "positions": ledger, "stats": stats,
                "config": {"costs": self.costs.__dict__, "universe": self.universe.__dict__,
                           "circuit": self.circuit.__dict__}}


def momentum_signal(top_n: int = 20, lookback_sessions: int = 63):
    def fn(engine: RotationBacktester, bars: pd.DataFrame, signal_date: date, cache=None) -> list[str]:
        ranked = engine.rank_momentum(bars, signal_date, lookback_sessions, cache)
        if ranked.empty:
            return []
        return ranked["key"].tolist()[:top_n]
    return fn


def load_bars_from_duckdb(db_path: str, start: str, end: str,
                          series_filter: tuple[str, ...] = ("EQ",),
                          identity_edges: list[dict] | None = None,
                          key_stabilization: KeyStabilization | None = None) -> pd.DataFrame:
    """Raw-price loader (fallback).

    ``identity_edges`` optionally remaps keys and ``key_stabilization``
    optionally gives a proven symbol one key for its whole life (see
    :func:`build_key_stabilization`).
    """
    import duckdb
    db = open_catalog(database=db_path)
    try:
        q = """SELECT date, symbol, series, isin, open, high, low, close,
               previous_close, volume, valid_ohlcv,
               CASE WHEN isin IS NOT NULL THEN 'ISIN:' || isin
                    ELSE 'SYMBOL:' || symbol END AS security_key
               FROM bars
               WHERE date BETWEEN ? AND ?
                 AND series IN ('EQ','BE','BZ','SM','ST')
                 AND valid_ohlcv AND volume > 0
                 AND (isin LIKE 'INE%' OR isin IS NULL)"""
        df = db.execute(q, [start, end]).fetchdf()
    finally:
        db.close()
    df["key"] = df["security_key"].fillna(df["symbol"])
    if series_filter:
        df = df[df["series"].isin(series_filter)]
    df = df.sort_values(["date", "key"]).reset_index(drop=True)
    if identity_edges:
        df, _ = apply_identity_continuity(df, identity_edges)
    if key_stabilization is not None:
        df, _ = apply_key_stabilization(df, key_stabilization)
    return df


def load_bars(db_path: str = str(data_root() / "market.duckdb"), start: str = "2021-01-01",
              end: str = "2022-12-31", adjusted: bool = False,
              parquet_path: str = ADJUSTED_PARQUET_DEFAULT,
              series_filter: tuple[str, ...] = ("EQ",),
              identity_edges: list[dict] | None = None,
              key_stabilization: KeyStabilization | None = None) -> pd.DataFrame:
    """Dispatching loader: adjusted parquet when ``adjusted`` else raw DB."""
    if adjusted:
        return load_adjusted_bars(parquet_path, start, end,
                                  series_filter, identity_edges, key_stabilization)
    return load_bars_from_duckdb(db_path, start, end,
                                 series_filter, identity_edges, key_stabilization)


def run_demo(db_path: str = str(data_root() / "market.duckdb"), start: str = "2018-01-01",
              end: str = "2022-12-31", out_dir: str = str(data_root() / "processed/backtest"),
             initial_cash: float = 10_000_000.0, top_n: int = 20,
             adjusted: bool = False, use_identity: bool = True,
             parquet_path: str = ADJUSTED_PARQUET_DEFAULT,
             identity_path: str = IDENTITY_EDGES_DEFAULT,
             api_identity_path: str = API_IDENTITY_EDGES_DEFAULT) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    edges = None
    merged = None
    identity_stats: dict | None = None
    if use_identity and Path(identity_path).exists():
        merged = load_merged_identity_edges(identity_path, api_identity_path)
        # Only the edges that cleared the fail-closed gate may remap a key.
        edges = merged["remap_edges"]
    # A symbol whose own bars prove one security keeps that ISIN on every
    # one of its bars, including the bars that predate the ISIN.
    stabilization = build_key_stabilization(db_path, Path(db_path).parent)
    if adjusted:
        bars = load_adjusted_bars(parquet_path, start, end, ("EQ",), edges, stabilization)
    else:
        bars = load_bars_from_duckdb(db_path, start, end, ("EQ",), edges, stabilization)
    if edges is not None and len(bars):
        # Re-derive counts from the loaded frame (loader already applied them).
        identity_stats = {
            "edges_total": len(merged["edges"]),
            "edges_by_source": merged["summary"]["edges_by_source"],
            "edges_remap_admissible": len(merged["remap_edges"]),
            "edges_refused": len(merged["refused"]),
            "edges_refused_by_code": merged["summary"]["refused_by_code"],
            "edges_file": str(identity_path),
            "api_edges_file": str(api_identity_path),
            "bars_remapped": int(bars["identity_remapped"].sum())
            if "identity_remapped" in bars.columns else 0,
            "key_stabilized_symbols": len(stabilization.canonical),
        }
    sessions = sorted(pd.to_datetime(bars["date"]).dt.date.unique().tolist())
    rebalances = month_end_dates(sessions)
    # drop early dates without 63-session lookback
    rebalances = [d for d in rebalances if sessions.index(d) >= 63]
    engine = RotationBacktester()
    result = engine.run(bars, rebalances, momentum_signal(top_n=top_n), initial_cash,
                        max_positions=top_n)
    result["equity"].to_csv(out / "equity_curve.csv", index=False)
    result["trades"].to_csv(out / "trades.csv", index=False)
    result["skips"].to_csv(out / "skipped_orders.csv", index=False)
    pd.DataFrame(result["positions"]).to_csv(out / "positions.csv", index=False)
    mode = "adjusted_split_bonus_only" if adjusted else "raw"
    price_limit = (
        "Adjusted prices: verified split/bonus only (adj = raw / cumulative factor, strictly before ex-date). Dividends, rights and mergers/demergers are not included; pre-2011 actions remain incomplete. Volume is raw and min_price uses raw contemporaneous close. Not a total-return series; results must not be claimed unbiased."
        if adjusted else
        "Raw unadjusted prices: splits/bonuses/dividends/rights/mergers not applied; returns around corporate actions are distorted and results must not be claimed unbiased.")
    summary = {
        "demo": "monthly momentum 3M top-20 equal-weight",
        "implementation_version": "causal_open_sizing_v2",
        "ready_for_backtesting": False,
        "ready_for_alpha_claim": False,
        "price_mode": mode,
        "identity_continuity": bool(edges is not None),
        "identity_stats": identity_stats,
        "key_stabilization": stabilization.stats,
        "universe_label": engine.universe.label,
        "start": start, "end": end, "initial_cash": initial_cash,
        "top_n": top_n, "series": ["EQ"],
        "bars_rows": int(len(bars)), "sessions": len(sessions),
        "rebalances": [d.isoformat() for d in rebalances],
        "stats": result["stats"], "config": result["config"],
        "limitations": [price_limit,
                        "Research-defined price/liquidity universe, not official Nifty membership.",
                         "Circuit limit approximated as flat 20%; no tick-level order-book data.",
                         "Costs are stylized constants, not the changing 2000-2026 tax/fee schedule; stamp duty is buy-only.",
                         "This legacy adjusted-price engine trades integer normalized units, not historical raw shares; use the full-history raw-share runner for split accounting.",
                        "Delisted/suspended holdings exit only when a tradable bar reappears; otherwise marked at last price and flagged.",
                        "Identity continuity only remaps SYMBOL-fallback (no-ISIN) keys along single unambiguous date-bounded official edges; ISIN evidence wins and ambiguous branches are not chained."],
        "outputs": {"equity": str(out / "equity_curve.csv"),
                    "trades": str(out / "trades.csv")},
    }
    (out / "demo-summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Rotation backtester demo")
    p.add_argument("--db", default=str(data_root() / "market.duckdb"))
    p.add_argument("--start", default="2018-01-01")
    p.add_argument("--end", default="2022-12-31")
    p.add_argument("--out", default=str(data_root() / "processed/backtest"))
    p.add_argument("--cash", type=float, default=10_000_000.0)
    p.add_argument("--adjusted", action="store_true",
                   help="Run on split/bonus-adjusted bars (adj_*) instead of raw prices")
    p.add_argument("--parquet", default=ADJUSTED_PARQUET_DEFAULT)
    p.add_argument("--no-identity", action="store_true",
                   help="Disable identity_edges continuity remapping")
    p.add_argument("--identity", default=IDENTITY_EDGES_DEFAULT)
    p.add_argument("--api-identity", default=API_IDENTITY_EDGES_DEFAULT)
    a = p.parse_args()
    print(json.dumps(run_demo(a.db, a.start, a.end, a.out, a.cash,
                              adjusted=a.adjusted, use_identity=not a.no_identity,
                              parquet_path=a.parquet, identity_path=a.identity,
                              api_identity_path=a.api_identity),
                     indent=2, default=str))
