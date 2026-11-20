"""NSE Alpha Lab -- a single-file Streamlit research front-end.

Purpose
-------
A thin, honest window onto the *existing* research engine. This file contains no
strategy logic of its own: it builds a
:class:`nse_pipeline.backtest.momentum.EngineConfig`, runs the real
:class:`MomentumRotationEngine`, and renders the result. Nothing here may quietly
change an accounting rule, invent a price, or sharpen a number.

Design rules for this file
--------------------------
1. **Graceful degradation.** Every optional import (streamlit, pandas, numpy,
   duckdb, and the ``nse_pipeline`` package itself) is guarded with
   ``try``/``except``. If something is missing the page says so in plain words
   and stops -- it never shows a fake number or a half-drawn chart.
2. **The numbers are the engine's.** Metrics are computed by the engine's own
   modules when they are importable; otherwise they are derived from the equity
   curve returned by ``run()`` with the formulas spelled out on screen.
3. **No silent assumptions.** A result the UI cannot fully price (an unpriced
   holding, an incomplete rebalance calendar, a non-causal fill mode) is
   labelled where the user is looking at it, not buried in a log.
4. **Honest about testing.** The pages have been rendered in Streamlit and
   captured for the README screenshots in ``docs/assets/ui/``. There is still
   no automated browser test suite, so treat layout and interaction changes as
   manually tested only.

How to read a result
--------------------
The headline table in ``research/FINAL_RESULTS.csv`` holds 968 configurations.
The best Sharpe in that file is a *maximum over 968 trials*, not a strategy
expectation. This app says so on every page where a user could mistake an
in-sample maximum for an edge.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths and optional imports.
#
# Everything below is best-effort. The module must import cleanly (and therefore
# compile and be inspectable) on a machine where streamlit and the pipeline's own
# dependencies are absent.
# ---------------------------------------------------------------------------

APP_FILE = Path(__file__).resolve()
APP_DIR = APP_FILE.parent
REPO_ROOT = APP_DIR.parents[1]  # apps/lab/app.py -> <repo>
SRC_DIR = REPO_ROOT / "src"
RESEARCH_DIR = REPO_ROOT / "research"
DATA_DIR = REPO_ROOT / "data"


def _repo_relative(path: Path) -> str:
    """Render a path relative to the repo when possible, else absolutely."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except (ValueError, OSError):
        return str(path)


# Make the src layout importable when the app is launched directly, so a user can
# run `streamlit run apps/lab/app.py` from the repository root without an
# editable install. Appending is deliberate: an already-installed nse_pipeline
# keeps priority over a sibling checkout.
if SRC_DIR.is_dir() and str(SRC_DIR) not in sys.path:
    sys.path.append(str(SRC_DIR))

try:  # pragma: no cover - environment dependent
    import streamlit as st
except Exception:  # noqa: BLE001 - any failure means "no UI runtime here"
    st = None

try:  # pragma: no cover - environment dependent
    import pandas as pd
except Exception:  # noqa: BLE001
    pd = None

try:  # pragma: no cover - environment dependent
    import numpy as np
except Exception:  # noqa: BLE001
    np = None

# The engine needs Decimal; it is stdlib, but guard anyway so a broken install
# reports "engine unavailable" instead of a traceback on every page.
ENGINE_IMPORT_ERROR: str | None = None
try:  # pragma: no cover - environment dependent
    from nse_pipeline.backtest.momentum import (
        DELISTING_POLICIES,
        ENTRY_TIMINGS,
        SELECTION_DIRECTIONS,
        BarsFrame,
        EngineConfig,
        MomentumRotationEngine,
    )
    from nse_pipeline.backtest.exits import ExitPolicy
except Exception as exc:  # noqa: BLE001
    ENGINE_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    EngineConfig = None  # type: ignore[assignment]
    MomentumRotationEngine = None  # type: ignore[assignment]
    BarsFrame = None  # type: ignore[assignment]
    ExitPolicy = None  # type: ignore[assignment]
    ENTRY_TIMINGS = ("next_open", "same_close")
    SELECTION_DIRECTIONS = ("top", "bottom")
    DELISTING_POLICIES = ("strict", "last_close_writeoff", "reason_haircut")

ENGINE_AVAILABLE = MomentumRotationEngine is not None and EngineConfig is not None

# Rebalance cadences. Mirrors ``nse_pipeline.backtest.cadence.VALID_CADENCES``;
# kept as a literal so the Create page still renders if the engine cannot import.
try:  # pragma: no cover - environment dependent
    from nse_pipeline.backtest.cadence import VALID_CADENCES as _VALID_CADENCES
    CADENCES = tuple(_VALID_CADENCES)
except Exception:  # noqa: BLE001
    CADENCES = ("monthly", "quarterly", "semiannual", "annual")

# Optional data layer. Pandas is required by the engine; duckdb only by the
# market.duckdb loader, so the two are tested separately.
PANDAS_AVAILABLE = pd is not None
DUCKDB_IMPORT_ERROR: str | None = None
try:  # pragma: no cover - environment dependent
    import duckdb  # noqa: F401
except Exception as exc:  # noqa: BLE001
    DUCKDB_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

# The CLI verify logic lives in a script, not a package module. Try the package
# path first (in case one is added later), then the script, then a subprocess.
VERIFY_IMPORT_ERROR: str | None = None
try:  # pragma: no cover - environment dependent
    from nse_pipeline.stage1_evidence import (  # noqa: F401
        inventory_integrity_ok,
        inventory_state,
    )
    VERIFY_IMPORTED = True
except Exception as exc:  # noqa: BLE001
    VERIFY_IMPORTED = False
    VERIFY_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_stage1_inventory.py"

# ---------------------------------------------------------------------------
# Constants lifted from the real pipeline so the UI speaks the same language.
# ---------------------------------------------------------------------------

#: Universe choices. The label is what the pipeline's own universe module says:
#: this is a price/liquidity screen, NOT an official index membership list.
UNIVERSES: dict[str, str] = {
    "nifty500": "NIFTY 500 -- evidence-dated official membership",
    "smallcap250": "NIFTY Smallcap 250 -- evidence-dated official membership",
    "all_cash": "All cash equity, research-defined price/liquidity screen "
                "(not official Nifty)",
}

BENCHMARKS: dict[str, str] = {
    "NIFTY 50 price index (Yahoo ^NSEI)": "Price index; dividends excluded on both sides.",
    "NIFTY 500 spliced NSE": "Spliced NSE series; coverage differs by year.",
    "no benchmark": "Skip relative statistics; absolute metrics only.",
}

#: Cost models exposed to the user. Each maps to explicit EngineConfig bps
#: fields; nothing is left implicit.
COST_MODELS: dict[str, dict] = {
    "all_charges": {
        "label": "All charges (brokerage + GST + STT + stamp + exchange + slippage)",
        "brokerage_bps": "2",
        "brokerage_cap_per_order": "2000",
        "stt_bps_buy": "10",
        "stt_bps_sell": "10",
        "stamp_bps": "1.5",
        "exchange_bps": "0.3",
        "gst_on_brokerage_pct": "18",
        "slippage_bps": "5",
    },
    "zero_cost": {
        "label": "Zero cost (counterfactual -- shows cost drag only)",
        "brokerage_bps": "0",
        "brokerage_cap_per_order": "0",
        "stt_bps_buy": "0",
        "stt_bps_sell": "0",
        "stamp_bps": "0",
        "exchange_bps": "0",
        "gst_on_brokerage_pct": "0",
        "slippage_bps": "0",
    },
}

#: The full 968-row table's filename.
FINAL_RESULTS = "FINAL_RESULTS_public.csv"
FINAL_RESULTS_CSV = "FINAL_RESULTS.csv"

#: Headline comparisons published in the plan; repeated here so the Compare page
#: can frame the table even before the CSV is loaded.
TRIALS_TOTAL = 968

#: Required acknowledgement before any backtest can run.
ACK_LABEL = "I understand this is an unvalidated research tool, not investment advice"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _dec(text, *, default=None):
    """Parse user text into a positive Decimal, or return ``default``."""
    if text is None or str(text).strip() == "":
        return default
    try:
        value = Decimal(str(text).strip())
    except InvalidOperation:
        return default
    if not value.is_finite():
        return default
    return value


def _pct(text):
    """Parse a user-entered percentage (``15`` -> ``Decimal('0.15')``).

    Empty or zero means "no overlay" and returns ``None``. ``ExitPolicy`` takes a
    positive magnitude below 1.0, so the input is divided by 100 here and
    validated by the policy itself, not by this function.
    """
    value = _dec(text)
    if value is None or value <= 0:
        return None
    return value / Decimal(100)


def _fmt_decimal(value) -> str:
    """Plain string for a Decimal that may lack a fixed exponent."""
    try:
        return format(Decimal(value), "f")
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


def _money(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.0f}"


def _safe_float(value, default=float("nan")) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Data presence probes
# ---------------------------------------------------------------------------


def find_data_packs() -> list[dict]:
    """Locate candidate data packs under ``data/``.

    A "pack" is a directory that carries at least one of the things the engine
    can read: ``market.duckdb``, a ``processed/prices`` tree, or a
    ``processed/dataset/dataset_manifest.json``. Returns a list of dicts so the
    UI can render exactly what was found rather than a yes/no.
    """
    packs: list[dict] = []
    if not DATA_DIR.is_dir():
        return packs
    candidates = [DATA_DIR] + [p for p in sorted(DATA_DIR.iterdir()) if p.is_dir()]
    for candidate in candidates:
        duckdb_path = candidate / "market.duckdb"
        prices = candidate / "processed" / "prices"
        manifest = candidate / "processed" / "dataset" / "dataset_manifest.json"
        partitions = sorted(prices.glob("year=*/bars.parquet")) if prices.is_dir() else []
        found = []
        if duckdb_path.is_file():
            found.append(f"market.duckdb ({duckdb_path.stat().st_size:,} bytes)")
        if partitions:
            found.append(f"{len(partitions)} price partition(s) under processed/prices")
        if manifest.is_file():
            found.append("processed/dataset/dataset_manifest.json")
        if found:
            packs.append({
                "path": candidate,
                "relative": _repo_relative(candidate),
                "contents": found,
                "duckdb": duckdb_path if duckdb_path.is_file() else None,
                "partitions": partitions,
                "manifest": manifest if manifest.is_file() else None,
            })
    return packs


def data_pack_absent_message() -> None:
    """The one message the user sees when there is nothing to backtest."""
    st.error(
        "**No data pack installed.** There is no readable NSE data under "
        f"`{_repo_relative(DATA_DIR)}/`, so a backtest cannot be run on this machine.\n\n"
        "The engine does not download data itself. Fetch a pack with the pipeline's "
        "acquisition stage, then place it under `data/`:"
    )
    st.code(
        "# 1. install the pipeline, data dependencies and optional fetchers\n"
        "uv sync --extra fetch\n"
        "$env:PYTHONPATH='src'   # PowerShell; use: export PYTHONPATH=src\n\n"
        "# 2. copy a pack you have access to into data/, or use the fetcher\n"
        "uv run python -m nse_pipeline fetch --help\n\n"
        "# 3. verify the checksummed pack before using it\n"
        "uv run python -m nse_pipeline verify --pack data",
        language="bash",
    )
    st.caption(
        "The commands above are the pipeline's documented stages. Exact flags depend on "
        "your checkout; run each `--help` before relying on a flag."
    )
    st.info(
        "**Pack-layout limit.** This detector looks for a `market.duckdb` catalog or the "
        "older partitioned layout under `data/`. It does not yet recognise the 1.0.0 "
        "pack's `processed/adjusted/` tree; use the CLI and `verify --pack data` for that "
        "pack."
    )


# ---------------------------------------------------------------------------
# Spec building
# ---------------------------------------------------------------------------


def build_spec(state: dict) -> dict:
    """Translate widget state into a JSON-safe, engine-shaped spec.

    This is the single place the UI's vocabulary meets the engine's. Every
    field name below matches an ``EngineConfig`` field so the preview doubles as
    the constructor arguments.
    """
    exit_overlay: dict = {"mode": "next_open"}
    stop = _pct(state.get("stop_loss_pct", ""))
    take = _pct(state.get("take_profit_pct", ""))
    exit_overlay["stop_loss_pct"] = _fmt_decimal(stop) if stop is not None else None
    exit_overlay["take_profit_pct"] = _fmt_decimal(take) if take is not None else None

    costs = {
        key: value for key, value in COST_MODELS[state["cost_model"]].items()
        if key != "label"
    }
    return {
        "universe": state["universe"],
        "universe_label": UNIVERSES.get(state["universe"], ""),
        "benchmark": state["benchmark"],
        "benchmark_note": BENCHMARKS.get(state["benchmark"], ""),
        "cadence": state["cadence"],
        "top_n": int(state["top_n"]),
        "direction": state["direction"],
        "lookback_sessions": int(state["lookback_sessions"]),
        "entry_timing": state["entry_timing"],
        "exit_policy": exit_overlay,
        "cost_model": state["cost_model"],
        "costs_bps": costs,
        "initial_cash": _fmt_decimal(state.get("initial_cash", "10000000")),
        "delisting_policy": "strict",
        "acknowledged": bool(state.get("acknowledged")),
        "engine": "nse_pipeline.backtest.momentum.MomentumRotationEngine",
    }


def make_engine_config(spec: dict):
    """Construct a real :class:`EngineConfig` from the spec; raise on bad input."""
    if not ENGINE_AVAILABLE:
        raise RuntimeError(f"engine unavailable ({ENGINE_IMPORT_ERROR})")
    stop = _dec(spec["exit_policy"].get("stop_loss_pct"), default=None)
    take = _dec(spec["exit_policy"].get("take_profit_pct"), default=None)
    policy = ExitPolicy(stop_loss_pct=stop, take_profit_pct=take,
                        mode=spec["exit_policy"]["mode"])
    costs = spec["costs_bps"]
    return EngineConfig(
        cadence=spec["cadence"],
        top_n=spec["top_n"],
        direction=spec["direction"],
        lookback_sessions=spec["lookback_sessions"],
        entry_timing=spec["entry_timing"],
        exit_policy=policy,
        delisting_policy=spec["delisting_policy"],
        initial_cash=Decimal(spec["initial_cash"]),
        brokerage_bps=Decimal(costs["brokerage_bps"]),
        brokerage_cap_per_order=Decimal(costs["brokerage_cap_per_order"]),
        stt_bps_buy=Decimal(costs["stt_bps_buy"]),
        stt_bps_sell=Decimal(costs["stt_bps_sell"]),
        stamp_bps=Decimal(costs["stamp_bps"]),
        exchange_bps=Decimal(costs["exchange_bps"]),
        gst_on_brokerage_pct=Decimal(costs["gst_on_brokerage_pct"]),
        slippage_bps=Decimal(costs["slippage_bps"]),
    )


# ---------------------------------------------------------------------------
# Bar loading (adapts the engine's loaders to BarsFrame's contract)
# ---------------------------------------------------------------------------

#: Columns BarsFrame requires. ``key`` is the stable security identity; the
#: engine is explicit that resolving renames/mergers into one key is the
#: caller's job, so this loader documents that it does *not* do it.
BARS_REQUIRED = ("date", "key", "open", "close", "volume")


#: Bar windows are large and a data pack on disk changes rarely, so a parsed
#: window is reused for an hour instead of being re-read on every rerun.
BARS_CACHE_TTL_SECONDS = 3600

#: A full-history bars frame is tens of MB in memory; cap the cache so this
#: cannot trade a disk read for unbounded process memory.
BARS_CACHE_MAX_ENTRIES = 2


def _cache_data(**kwargs):
    """Resolve ``st.cache_data`` at import time, tolerating a missing streamlit.

    The module's contract (see the docstring) is that it imports where the UI
    extras are absent, so ``@st.cache_data`` cannot be written literally -- ``st``
    may be ``None``. In that case the function is returned undecorated: correct,
    just not cached.
    """
    if st is None:
        def _passthrough(func):
            return func
        return _passthrough
    return st.cache_data(**kwargs)


@_cache_data(ttl=BARS_CACHE_TTL_SECONDS, max_entries=BARS_CACHE_MAX_ENTRIES,
             show_spinner=False)
def _read_bars_cached(duckdb_path: str | None,
                      partitions: tuple[str, ...],
                      start: str,
                      end: str,
                      series: tuple[str, ...]) -> tuple[pd.DataFrame, str | None, int]:
    """Read, filter and normalise bars for one window -- the cacheable I/O.

    This is a pure function of the pack's *files* and the requested window: the
    same arguments always produce the same frame, and it reads no widget or
    session state. It takes the source paths as plain strings and tuples rather
    than the ``pack`` dict so the cache key is derived from what actually
    selects files; a dict of ``Path`` objects and lists would be at best
    unhashable and at worst hashed by object identity, which would make every
    rerun a cache miss.

    Failures are not cached (Streamlit does not store exceptions): a malformed
    or unreadable pack raises the same readable error on every call rather than
    being remembered.
    """
    if pd is None:
        raise RuntimeError("pandas is not installed")

    frame = None
    source = None

    if duckdb_path is not None:
        try:
            from nse_pipeline.backtest.engine import load_bars
            frame = load_bars(db_path=duckdb_path, start=start, end=end,
                              adjusted=False, series_filter=series)
            source = f"market.duckdb via load_bars({start}..{end})"
        except Exception as exc:  # noqa: BLE001 - fall through to parquet
            frame = None
            source = f"market.duckdb failed ({type(exc).__name__}: {exc})"

    if frame is None and partitions:
        parts = []
        errors = []
        for path in partitions:
            try:
                parts.append(pd.read_parquet(path))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{_repo_relative(Path(path))}: {type(exc).__name__}: {exc}")
        if parts:
            frame = pd.concat(parts, ignore_index=True)
            source = f"{len(parts)} parquet partition(s)"
        elif errors:
            raise RuntimeError("no parquet partition could be read: " + "; ".join(errors[:3]))

    if frame is None or len(frame) == 0:
        raise RuntimeError(
            "the data pack contains nothing readable for this window "
            f"({start}..{end}, series={series})" +
            (f"; last attempt: {source}" if source else "")
        )

    frame = frame.copy()
    # Normalise to BarsFrame's contract without inventing anything.
    if "key" not in frame.columns:
        if "security_key" in frame.columns:
            frame["key"] = frame["security_key"]
        elif "symbol" in frame.columns:
            frame["key"] = frame["symbol"]
        else:
            raise RuntimeError("no 'key', 'security_key' or 'symbol' column to identify securities")
    missing = [column for column in BARS_REQUIRED if column not in frame.columns]
    if missing:
        raise RuntimeError(f"bars frame is missing required columns: {missing}")
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame[(frame["date"] >= pd.Timestamp(start)) & (frame["date"] <= pd.Timestamp(end))]
    numeric = [c for c in ("open", "close", "volume", "high", "low") if c in frame.columns]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    rows_before = len(frame)
    frame = frame.dropna(subset=["open", "close"])
    dropped = rows_before - len(frame)
    frame = frame[frame["open"] > 0]
    if frame.empty:
        raise RuntimeError("every bar in the window was rejected (missing or non-positive open/close)")
    return frame, source, dropped


def load_bars_frame(pack: dict, start: str, end: str, series: tuple[str, ...] = ("EQ",)):
    """Return a :class:`BarsFrame`, or raise with a readable reason.

    Two sources are tried in order:

    1. ``market.duckdb`` via ``nse_pipeline.backtest.engine.load_bars`` -- the
       pipeline's own loader, which already applies the eligibility filters.
    2. ``processed/prices/year=*/bars.parquet`` read directly.

    The returned frame is passed through ``BarsFrame``'s validation (required
    columns, duplicate ``(date, key)`` rejection), so an ambiguous or malformed
    pack fails loudly instead of producing a plausible-looking curve.

    The read itself is cached in :func:`_read_bars_cached`. The public signature
    is unchanged; the ``pack`` dict is first reduced to the values that select
    files (the duckdb path and the ordered partition paths) so the cache key is
    stable across reruns. ``BarsFrame`` receives its own copy of the cached
    frame, so validation or a caller mutating columns in place cannot corrupt
    what a later rerun reads back.
    """
    if pd is None:
        raise RuntimeError("pandas is not installed")
    if not ENGINE_AVAILABLE:
        raise RuntimeError(f"engine unavailable ({ENGINE_IMPORT_ERROR})")

    duckdb_path = pack.get("duckdb")
    frame, source, dropped = _read_bars_cached(
        str(duckdb_path) if duckdb_path is not None else None,
        tuple(str(path) for path in (pack.get("partitions") or ())),
        start,
        end,
        tuple(series),
    )
    return BarsFrame(frame.copy()), source, dropped


def run_backtest(config, pack: dict, start: str, end: str) -> dict:
    """Run the real engine and return its result dict plus loader provenance."""
    bars, source, dropped = load_bars_frame(pack, start, end)
    engine = MomentumRotationEngine(config)
    result = engine.run(bars)
    result["_ui"] = {
        "data_source": source,
        "sessions": len(bars.sessions),
        "rows_dropped_unusable": dropped,
        "bar_rows": int(len(bars.frame)),
        "start": start,
        "end": end,
    }
    return result


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def equity_series(result: dict):
    """Equity as a pandas Series indexed by date, ``None`` rows preserved.

    The engine writes ``equity: None`` for a session where a holding could not
    be priced (a delisted position under ``strict``). Those sessions are kept as
    gaps rather than interpolated: filling them in would invent a price.
    """
    if pd is None:
        raise RuntimeError("pandas is not installed")
    rows = result.get("equity") or []
    points = []
    for row in rows:
        value = row.get("equity")
        if value is None:
            points.append((row["date"], float("nan")))
        else:
            points.append((row["date"], float(value)))
    if not points:
        return pd.Series(dtype="float64")
    series = pd.Series([v for _, v in points],
                       index=pd.to_datetime([d for d, _ in points]), name="equity")
    return series.sort_index()


def drawdown_series(equity):
    """Drawdown as a fraction of the running peak, preserving ``NaN`` gaps."""
    if equity is None or len(equity) == 0:
        return equity
    running_peak = equity.cummax()
    return equity / running_peak - 1.0


def compute_metrics(result: dict) -> dict:
    """Absolute performance metrics derived from the engine's equity curve.

    Formulas are stated in the UI next to the numbers. If a metric cannot be
    computed from a priced-only curve (for example because sessions are
    missing), it is returned as ``None`` and displayed as ``n/a`` -- never
    silently patched with a substitute.
    """
    equity = equity_series(result)
    metrics: dict = {
        "start": None, "end": None, "sessions": int(len(equity)),
        "years": None, "total_return": None, "cagr": None,
        "max_drawdown": None, "final_equity": None, "initial_equity": None,
        "unpriced_sessions": 0, "note": "",
    }
    if len(equity) == 0:
        metrics["note"] = "the engine produced no equity rows"
        return metrics

    priced = equity.dropna()
    metrics["unpriced_sessions"] = int(equity.isna().sum())
    if len(priced) < 2:
        metrics["note"] = "fewer than two priced sessions; no return can be computed"
        return metrics

    first, last = float(priced.iloc[0]), float(priced.iloc[-1])
    metrics["initial_equity"] = first
    metrics["final_equity"] = last
    metrics["start"] = priced.index[0].date().isoformat()
    metrics["end"] = priced.index[-1].date().isoformat()
    if first > 0:
        metrics["total_return"] = last / first - 1.0
    years = (priced.index[-1] - priced.index[0]).days / 365.25
    metrics["years"] = years
    if metrics["total_return"] is not None and years > 0 and (1 + metrics["total_return"]) > 0:
        metrics["cagr"] = (1 + metrics["total_return"]) ** (1 / years) - 1.0
    dd = drawdown_series(priced)
    metrics["max_drawdown"] = float(dd.min()) if len(dd) else None
    if metrics["unpriced_sessions"]:
        metrics["note"] = (
            f"{metrics['unpriced_sessions']} session(s) had no price for a held security; "
            "they are excluded from this curve rather than interpolated."
        )
    return metrics


def charge_totals(result: dict) -> dict:
    """Sum the itemised per-trade charges the engine recorded.

    ``cost_components`` on ``EngineConfig`` defines the items; this sums the
    single ``cost`` field each trade row carries, so the total always ties back
    to the trades list rather than to a separate calculation.
    """
    trades = result.get("trades") or []
    buy = Decimal(0)
    sell = Decimal(0)
    notional = Decimal(0)
    for trade in trades:
        cost = _dec(trade.get("cost"), default=Decimal(0)) or Decimal(0)
        value = _dec(trade.get("notional"), default=Decimal(0)) or Decimal(0)
        notional += value
        if str(trade.get("side", "")).lower().startswith("buy"):
            buy += cost
        else:
            sell += cost
    total = buy + sell
    return {
        "trades": len(trades),
        "buy_charges": buy,
        "sell_charges": sell,
        "total_charges": total,
        "turnover": notional,
        "charges_bps_of_turnover": (total / notional * Decimal(10_000)) if notional > 0 else None,
        # Reconcile against the engine's own reported result where possible.
        "per_trade_cost_components": ["brokerage", "gst", "stt", "stamp", "exchange", "slippage"],
    }


def sharpe_from_equity(equity):
    """Annualised Sharpe with a zero risk-free rate, from daily returns.

    ``n/a`` when there are fewer than two returns or the standard deviation is
    zero. This is the same definition the published table's ``sharpe_rf0`` uses.
    """
    if equity is None or len(equity.dropna()) < 3:
        return None
    returns = equity.dropna().pct_change().dropna()
    if len(returns) < 2:
        return None
    sd = float(returns.std(ddof=1))
    if sd == 0 or sd != sd:  # zero or NaN
        return None
    return float(returns.mean()) / sd * (252 ** 0.5)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

DEFAULT_STATE: dict = {
    "universe": "nifty500",
    "benchmark": "NIFTY 50 price index (Yahoo ^NSEI)",
    "top_n": 10,
    "direction": "top",
    "lookback_sessions": 63,
    "cadence": "monthly",
    "stop_loss_pct": "",
    "take_profit_pct": "",
    "cost_model": "all_charges",
    "entry_timing": "next_open",
    "initial_cash": "10000000",
    "acknowledged": False,
    "result": None,
    "result_spec": None,
}


def init_state() -> None:
    for key, value in DEFAULT_STATE.items():
        st.session_state.setdefault(key, value)


def current_spec() -> dict:
    return build_spec({key: st.session_state.get(key, value)
                       for key, value in DEFAULT_STATE.items()})


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def page_home() -> None:
    st.title("NSE Alpha Lab")
    st.subheader("A research preview, not a product.")
    st.warning(
        "**Research preview.** This tool runs an unvalidated research backtest on "
        "historical NSE data. It is not investment advice, not a trading system, and "
        "it has no forward-test record. Numbers you see here are what a specific "
        "configuration produced in-sample on a specific data pack -- they are not a "
        "forecast and they are not evidence that the configuration works."
    )

    left, right = st.columns(2)
    with left:
        st.markdown("**What this app is**")
        st.markdown(
            "- A thin UI over `nse_pipeline.backtest.momentum.MomentumRotationEngine`.\n"
            "- A way to build a `EngineConfig` interactively and read the result honestly.\n"
            "- A window onto the published 968-configuration table in `research/`."
        )
        st.markdown("**What this app is not**")
        st.markdown(
            "- Not a strategy generator. It does not search for parameters.\n"
            "- Not a broker connection. It never places an order.\n"
            "- Not a source of data. The engine reads a pack you have installed."
        )
    with right:
        st.markdown("**Environment**")
        rows = [
            ("streamlit", "available" if st is not None else "MISSING"),
            ("pandas", "available" if PANDAS_AVAILABLE else "MISSING"),
            ("duckdb", "available" if DUCKDB_IMPORT_ERROR is None else f"MISSING ({DUCKDB_IMPORT_ERROR})"),
            ("engine (nse_pipeline.backtest.momentum)",
             "available" if ENGINE_AVAILABLE else f"MISSING ({ENGINE_IMPORT_ERROR})"),
            ("data pack", "found" if find_data_packs() else "not installed"),
            ("public results table",
             "found" if _public_results_path() else "not found"),
        ]
        st.table({"component": [r[0] for r in rows], "status": [r[1] for r in rows]})

    st.markdown("### Quickstart")
    st.code(
        "# 1. install the UI extras (streamlit is not in the core dependency set)\n"
        "uv pip install -r apps/lab/requirements.txt\n\n"
        "# 2. run the lab from the repository root\n"
        "streamlit run apps/lab/app.py\n\n"
        "# 3. run the engine's own test suite -- this is the authority, not the UI\n"
        "uv run pytest -q\n\n"
        "# 4. reproduce the published research table\n"
        "uv run python scripts/reproduce_stage1_findings.py",
        language="bash",
    )
    st.caption(
        "The pages have been rendered and captured for the README "
        "(`docs/assets/ui/`). There is no automated browser test suite yet, so "
        "layout and interaction changes are still manually tested."
    )

    st.markdown("### Before you trust a number")
    st.info(
        "The 968-configuration table is a parameter sweep. Its best Sharpe is the "
        "**maximum of 968 trials**, which is close to what pure noise produces at that "
        "trial count. Read the Compare page before drawing any conclusion."
    )


def page_data() -> None:
    st.title("Data")
    st.caption(f"Looking for data under `{_repo_relative(DATA_DIR)}/`.")
    packs = find_data_packs()
    if not packs:
        data_pack_absent_message()
    else:
        st.success(f"Found {len(packs)} data pack(s).")
        for pack in packs:
            with st.expander(pack["relative"], expanded=len(packs) == 1):
                for item in pack["contents"]:
                    st.markdown(f"- {item}")

    st.markdown("---")
    st.subheader("Verify pack integrity")
    st.caption(
        "Verification re-reads every inventoried byte and compares it with the recorded "
        "hash inventory. It never writes into the data tree. This calls the same logic "
        "as `scripts/verify_stage1_inventory.py`."
    )
    if st.button("Verify data integrity", type="secondary"):
        _run_verify()

    if not VERIFY_IMPORTED and VERIFY_IMPORT_ERROR:
        st.caption(f"Package verify import unavailable ({VERIFY_IMPORT_ERROR}); "
                   "the subprocess path will be used.")


def _run_verify() -> None:
    """Run verification: in-process if importable, else subprocess, else explain."""
    inventory = DATA_DIR / "manifests" / "dataset_hashes.json"
    if not inventory.is_file():
        st.warning(
            "No hash inventory found at "
            f"`{_repo_relative(inventory)}`. There is nothing to verify: integrity "
            "checking compares files against a recorded baseline, and no baseline "
            "is installed here."
        )
        st.code(
            "uv run python scripts/verify_stage1_inventory.py --out reports/verify.json",
            language="bash",
        )
        return

    if VERIFY_IMPORTED:
        try:
            entries = json.loads(inventory.read_text(encoding="utf-8"))["data"]
            result = inventory_state(DATA_DIR.resolve(), entries, [])
            ok = inventory_integrity_ok(result)
            (st.success if ok else st.error)(
                f"Integrity check: **{'PASS' if ok else 'FAIL'}** (state={result.get('state')})"
            )
            st.json(result)
            return
        except Exception as exc:  # noqa: BLE001 - fall back to subprocess
            st.caption(f"In-process verification failed ({type(exc).__name__}: {exc}); "
                       "trying the script.")

    if VERIFY_SCRIPT.is_file():
        out = REPO_ROOT / "reports" / "verify_ui.json"
        try:
            completed = subprocess.run(
                [sys.executable, str(VERIFY_SCRIPT), "--out", str(out)],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=1800,
            )
            (st.success if completed.returncode == 0 else st.error)(
                f"Verification script exit code: {completed.returncode}"
            )
            if completed.stdout:
                st.code(completed.stdout[-8000:], language="json")
            if completed.stderr:
                st.code(completed.stderr[-4000:], language="text")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not run the verification script: {type(exc).__name__}: {exc}")
        return

    st.error(
        "No verification implementation is available: neither "
        "`nse_pipeline.stage1_evidence` nor `scripts/verify_stage1_inventory.py` "
        "could be reached from this app."
    )


def page_create() -> None:
    st.title("Create a strategy spec")
    st.caption(
        "This builds a `EngineConfig`. It does not create a strategy: every field below "
        "is a configuration of the same momentum rotation rule."
    )
    if not ENGINE_AVAILABLE:
        st.error(f"The engine could not be imported, so no spec can be run: {ENGINE_IMPORT_ERROR}")

    with st.form("strategy_builder"):
        col1, col2 = st.columns(2)
        with col1:
            st.selectbox("Universe", list(UNIVERSES), key="universe",
                         format_func=lambda key: UNIVERSES[key],
                         help="Membership source. The 'all cash' option is a "
                              "price/liquidity screen, not an official index.")
            st.selectbox("Benchmark", list(BENCHMARKS), key="benchmark",
                         format_func=lambda key: key, help="Used for relative statistics.")
            st.number_input("Hold top N", min_value=1, max_value=100, step=1, key="top_n")
            st.radio("Selection direction", list(SELECTION_DIRECTIONS), key="direction",
                     horizontal=True,
                     help="'bottom' buys the weakest trailing returns -- a reversal "
                          "counterfactual, not a recommendation.")
            st.number_input("Lookback (sessions)", min_value=1, max_value=756, step=21,
                            key="lookback_sessions",
                            help="Trailing-return window in observed sessions. 63 ~ one quarter.")
        with col2:
            st.selectbox("Rebalance cadence", list(CADENCES), key="cadence")
            st.text_input("Stop loss % (blank = none)", key="stop_loss_pct",
                          help="Positive magnitude below 100. Exits when the close is "
                               "that far below average entry.")
            st.text_input("Take profit % (blank = none)", key="take_profit_pct",
                          help="Positive magnitude below 100.")
            st.selectbox("Cost model", list(COST_MODELS), key="cost_model",
                         format_func=lambda key: COST_MODELS[key]["label"])
            st.selectbox("Entry timing", list(ENTRY_TIMINGS), key="entry_timing",
                         format_func=lambda key: {
                             "next_open": "next_open -- causal (fill at next session's open)",
                             "same_close": "same_close -- OPTIMISTIC (fills at the close "
                                           "that formed the ranking)",
                         }.get(key, key))

        st.checkbox(ACK_LABEL, key="acknowledged")

        submitted = st.form_submit_button(
            "Run backtest", type="primary", disabled=not st.session_state.get("acknowledged", False)
        )

    if not st.session_state.get("acknowledged", False):
        st.info(
            "The acknowledgement above must be checked before a backtest can run. It is a "
            "gate, not a formality: an in-sample result from this tool is not a basis for "
            "putting money at risk."
        )

    spec = current_spec()
    st.subheader("Spec preview")
    st.json(spec)

    # Validate eagerly so the user sees the engine's own objection, not a crash.
    errors = []
    stop_loss = spec["exit_policy"]["stop_loss_pct"]
    take_profit = spec["exit_policy"]["take_profit_pct"]
    if stop_loss:
        raw = _dec(stop_loss)
        if raw is None or raw <= 0 or raw >= 100:
            errors.append("stop loss % must be between 0 and 100 (exclusive)")
    if take_profit:
        raw = _dec(take_profit)
        if raw is None or raw <= 0 or raw >= 100:
            errors.append("take profit % must be between 0 and 100 (exclusive)")
    if ENGINE_AVAILABLE:
        try:
            config = make_engine_config(spec)
            st.caption(f"Engine label for this spec: `{config.label}`")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
    for message in errors:
        st.error(message)

    if submitted:
        st.session_state["result_spec"] = spec
        st.session_state["result"] = None
        st.success("Spec saved. Open the **Backtest** page in the sidebar to run it.")

    if spec["entry_timing"] == "same_close":
        st.warning(
            "**`same_close` is an optimistic counterfactual.** It fills at the same close "
            "that formed the ranking, so it trades on a price the decision already knew. "
            "The engine flags this as look-ahead (`lookahead: true`). It exists to measure "
            "the size of the assumption, not to be used."
        )
    if spec["direction"] == "bottom":
        st.info(
            "Direction `bottom` holds the weakest trailing returns. It is a reversal "
            "counterfactual for testing the sign of the momentum effect -- not a "
            "recommended configuration."
        )


def page_backtest() -> None:
    st.title("Backtest")
    if not ENGINE_AVAILABLE:
        st.error(f"The engine could not be imported: {ENGINE_IMPORT_ERROR}")
        st.caption("Install the repository's dependencies (pandas, numpy) and retry.")
        return

    spec = st.session_state.get("result_spec") or current_spec()
    if not spec.get("acknowledged"):
        st.warning(
            "The acknowledgement checkbox on the **Create** page has not been ticked for "
            "this session. Tick it there to enable a run."
        )

    packs = find_data_packs()
    if not packs:
        data_pack_absent_message()
        return
    pack = packs[0]
    if len(packs) > 1:
        chosen = st.selectbox("Data pack", [p["relative"] for p in packs])
        pack = next(p for p in packs if p["relative"] == chosen)
    st.caption(f"Using data pack `{pack['relative']}`: " + "; ".join(pack["contents"]))

    # The window must be supplied by the user; the app never guesses a date range.
    col1, col2 = st.columns(2)
    start = col1.text_input("Start date (YYYY-MM-DD)", value="2013-01-01")
    end = col2.text_input("End date (YYYY-MM-DD)", value=date.today().isoformat())

    st.json(spec)
    if spec["entry_timing"] == "same_close":
        st.warning("This spec uses `same_close` fills. The result is optimistic "
                   "and includes look-ahead by construction.")

    disabled = not spec.get("acknowledged")
    if st.button("Run engine", type="primary", disabled=disabled):
        try:
            config = make_engine_config(spec)
        except Exception as exc:  # noqa: BLE001
            st.error(f"The spec is not a valid engine configuration: {type(exc).__name__}: {exc}")
            return
        try:
            for label in (start, end):
                date.fromisoformat(label)
        except ValueError:
            st.error("Start and end must both be ISO dates (YYYY-MM-DD).")
            return
        with st.spinner("Running the momentum rotation engine..."):
            try:
                result = run_backtest(config, pack, start, end)
            except Exception as exc:  # noqa: BLE001 - report, never pretend
                st.error(f"The engine could not run: {type(exc).__name__}: {exc}")
                st.caption("Nothing is displayed from a failed run: a partial result "
                           "would be a fabricated result.")
                return
        st.session_state["result"] = result
        st.session_state["result_spec"] = spec
        st.success("Run complete. See the **Results** page.")

    result = st.session_state.get("result")
    if result:
        ui = result.get("_ui", {})
        st.markdown("### Last run")
        st.caption(
            f"Labels: `{result.get('label')}`  |  sessions {ui.get('sessions')}  |  "
            f"bars {ui.get('bar_rows'):,}  |  source: {ui.get('data_source')}"
        )
        cadence = result.get("cadence") or {}
        if not cadence.get("complete", True):
            st.warning(
                "The rebalance calendar is **incomplete**: some periods had no observed "
                f"session. Skipped periods: {cadence.get('skipped_periods')}"
            )
        equity_rows = result.get("equity") or []
        unpriced = sum(1 for row in equity_rows if row.get("equity") is None)
        if unpriced:
            st.warning(
                f"{unpriced} session(s) have **unknown equity** because a holding could not "
                "be priced (a delisted security under the `strict` policy). Those sessions "
                "are left as gaps, never filled."
            )
        skips = result.get("skips") or []
        if skips:
            st.caption(f"{len(skips)} skip event(s) recorded by the engine; "
                       "see the Results page for the reason breakdown.")


def page_results() -> None:
    st.title("Results")
    result = st.session_state.get("result")
    if not result:
        st.info("No run in this session. Build a spec on **Create**, then run it on **Backtest**.")
        return

    metrics = compute_metrics(result)
    ui = result.get("_ui", {})

    st.caption(
        f"`{result.get('label')}`  |  {metrics['start']} to {metrics['end']}  |  "
        f"{ui.get('sessions', '?')} sessions"
    )

    cols = st.columns(4)
    cols[0].metric("Total return",
                   "n/a" if metrics["total_return"] is None else f"{metrics['total_return'] * 100:,.2f}%")
    cols[1].metric("CAGR",
                   "n/a" if metrics["cagr"] is None else f"{metrics['cagr'] * 100:,.2f}%")
    cols[2].metric("Sharpe (rf=0)",
                   "n/a" if sharpe_from_equity(equity_series(result)) is None
                   else f"{sharpe_from_equity(equity_series(result)):,.3f}")
    cols[3].metric("Max drawdown",
                   "n/a" if metrics["max_drawdown"] is None else f"{metrics['max_drawdown'] * 100:,.2f}%")

    if metrics["note"]:
        st.warning(metrics["note"])

    equity = equity_series(result)
    if len(equity):
        st.subheader("Equity curve")
        st.line_chart(equity, height=280)
        st.caption(f"Final equity {_money(Decimal(str(metrics['final_equity'])))} INR from "
                   f"{_money(Decimal(str(metrics['initial_equity'])))} INR initial. "
                   "Gaps are sessions where equity was genuinely unknown.")

        st.subheader("Drawdown")
        st.line_chart(drawdown_series(equity), height=220, color="#c0392b")
        st.caption("Fraction below the running peak. Same gaps apply.")

    st.subheader("Charges")
    charges = charge_totals(result)
    charge_cols = st.columns(4)
    charge_cols[0].metric("Trades", charges["trades"])
    charge_cols[1].metric("Buy charges", _money(charges["buy_charges"]))
    charge_cols[2].metric("Sell charges", _money(charges["sell_charges"]))
    charge_cols[3].metric("Total charges", _money(charges["total_charges"]))
    st.caption(
        "Summed from the `cost` field of every trade the engine recorded. Components "
        f"per trade: {', '.join(charges['per_trade_cost_components'])}."
        + (f" Charges are {charges['charges_bps_of_turnover']:.2f} bps of turnover."
           if charges["charges_bps_of_turnover"] is not None else "")
    )

    with st.expander("Metric definitions"):
        st.markdown(
            "- **Total return** = final priced equity / first priced equity - 1.\n"
            "- **CAGR** = (1 + total return) ** (1 / years) - 1, years from calendar days.\n"
            "- **Sharpe (rf=0)** = mean(daily returns) / stdev(daily returns) * sqrt(252), "
            "computed on the priced curve.\n"
            "- **Max drawdown** = min(equity / running peak - 1).\n"
            "- Sessions with unknown equity are excluded from every statistic above "
            "rather than interpolated."
        )

    trades = result.get("trades") or []
    if trades:
        st.subheader("Trades")
        if PANDAS_AVAILABLE:
            frame = pd.DataFrame(trades)
            st.dataframe(frame, height=320, use_container_width=True)
        else:
            st.write(trades[:200])

    skips = result.get("skips") or []
    if skips:
        st.subheader("Skips")
        st.caption("Every skip the engine recorded, with its reason. A skip is not an "
                   "error: it is the engine refusing to invent a fill.")
        if PANDAS_AVAILABLE:
            frame = pd.DataFrame(skips)
            st.dataframe(frame, use_container_width=True)
        else:
            st.write(skips[:200])

    with st.expander("Full engine output (JSON)"):
        st.json(result)


def _public_results_path() -> Path | None:
    """The comparison table: prefer the public export, fall back to the raw file."""
    for name in (FINAL_RESULTS, FINAL_RESULTS_CSV):
        candidate = RESEARCH_DIR / name
        if candidate.is_file():
            return candidate
    return None


def page_compare() -> None:
    st.title("Compare against the research grid")
    path = _public_results_path()
    st.warning(
        f"**Multiple testing.** The table holds {TRIALS_TOTAL} configurations. Its best "
        f"Sharpe is the **maximum over {TRIALS_TOTAL} trials**, not an achievable "
        "expectation. At that trial count a maximum of this size is close to what pure "
        "noise can produce. Only 26 of 968 configurations reach t > 1.96, when about 48 "
        "would be expected by chance alone, and the pre-specified primary configuration "
        "has a bootstrap p of 0.096 -- not significant. Reading a single good row as "
        "'the strategy' is the exact error this table exists to expose."
    )
    if path is None:
        st.info(
            f"No results table found at `research/{FINAL_RESULTS}` (or "
            f"`research/{FINAL_RESULTS_CSV}`). Generate it first:"
        )
        st.code("uv run python scripts/reproduce_stage1_findings.py", language="bash")
        return
    if not PANDAS_AVAILABLE:
        st.error("pandas is not installed, so the table cannot be loaded.")
        return

    try:
        frame = pd.read_csv(path)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read `{_repo_relative(path)}`: {type(exc).__name__}: {exc}")
        return

    st.caption(f"Loaded `{_repo_relative(path)}`: {len(frame):,} rows x {len(frame.columns)} columns.")
    if len(frame) != TRIALS_TOTAL:
        st.info(f"This file has {len(frame):,} rows, not the {TRIALS_TOTAL} in the published "
                "table. Treat the multiple-testing framing as approximate for this file.")

    summary = st.columns(4)
    if "sharpe_rf0" in frame.columns:
        best = frame["sharpe_rf0"].max()
        summary[0].metric("Best Sharpe (max of trials)", f"{best:,.3f}")
        summary[1].metric("Median Sharpe", f"{frame['sharpe_rf0'].median():,.3f}")
    if "excess_cagr_pct" in frame.columns:
        summary[2].metric("Configs beating NIFTY 50",
                          f"{int((frame['excess_cagr_pct'] > 0).sum())}/{len(frame)}")
        summary[3].metric("Median excess CAGR", f"{frame['excess_cagr_pct'].median():,.2f}%")

    # Candidate lead columns, filtered by presence: the public export deliberately
    # drops some internal fields (gate_status, publishable, raw t-statistics), so
    # the shortlist adapts to whichever file is actually on disk.
    shortlist = [c for c in (
        "config_id", "family", "universe", "universe_label",
        "direction", "top_n", "lookback_sessions", "cadence", "stop_loss_pct",
        "take_profit_pct", "is_flagship",
        "window_years", "sessions", "total_return_pct", "cagr_pct", "sharpe_rf0",
        "max_drawdown_pct", "benchmark_cagr_pct", "excess_cagr_pct",
        "t_newey_west_5", "publishable", "gate_status",
    ) if c in frame.columns]
    if "sharpe_rf0" in frame.columns:
        frame = frame.sort_values("sharpe_rf0", ascending=False)
    st.subheader("All rows")
    st.dataframe(frame[shortlist] if shortlist else frame,
                 height=460, use_container_width=True)

    with st.expander("Column reference"):
        st.write(sorted(frame.columns.tolist()))


def page_docs() -> None:
    st.title("Docs: limitations, disclaimer, licensing")
    st.subheader("Limitations")
    st.markdown(
        "1. **This is an in-sample research result.** A configuration that ranks well "
        "here has been selected on the same data it was measured on. No out-of-sample "
        "or forward test is presented.\n"
        "2. **The grid is 968 trials.** The best row is a maximum over trials; it will be "
        "an upward-biased estimate of any real effect. See the Compare page.\n"
        "3. **Window coverage is uneven.** Many of the best rows use 4.66-4.92 year "
        "windows; only 428 of 968 span 13 years. A window that starts after a crash is "
        "not the same strategy as one that lived through it.\n"
        "4. **Dividends are excluded** on both strategy and benchmark; the benchmark is a "
        "price index. Excess return therefore understates total-return performance on "
        "both sides, but not necessarily equally.\n"
        "5. **Delisted securities are an assumption, not a fact.** Under the default "
        "`strict` policy, a holding that stops trading makes equity *unknown* from that "
        "session rather than marked at a stale price. Rows that resolve delistings by "
        "other policies carry that assumption explicitly.\n"
        "6. **Costs are modelled, not observed.** Brokerage, GST, STT, stamp duty, "
        "exchange charges and slippage are parameterised in `EngineConfig`; real fills "
        "and real impact will differ, and impact scales with size in ways this model "
        "does not represent.\n"
        "7. **Liquidity and capacity are not bounded** by this UI. A backtest that trades "
        "a notional larger than the market can absorb is a fiction.\n"
        "8. **Identity is the caller's job.** The engine consumes one `key` per economic "
        "security; renames, mergers and ISIN changes must be resolved upstream.\n"
        "9. **Adjusted prices only.** The engine never applies corporate actions itself; "
        "the pack's adjustment provenance must be recorded.\n"
        "10. **No survivorship correction is claimed** beyond what the data pack's own "
        "universe construction provides.\n"
        "11. **The public export drops columns.** `FINAL_RESULTS_public.csv` omits some "
        "internal fields (gate status, raw t-statistics) that the full sweep carries; "
        "the Compare page shows whichever columns are present in the file it loaded."
    )

    st.subheader("Disclaimer")
    st.error(
        "This software is provided for research and education only. It is **not** "
        "investment advice, not a recommendation to buy or sell any security, and not a "
        "solicitation. Past performance -- especially in-sample, selected-on-the-same-data "
        "performance -- does not predict future results. Backtest results are subject to "
        "survivorship, look-ahead, overfitting and modelling error. Trading involves "
        "substantial risk of loss. You are responsible for your own decisions. The authors "
        "accept no liability for any loss arising from use of this software."
    )

    st.subheader("Data licensing")
    st.markdown(
        "**The data pack is not part of this repository and is not distributed with it.** "
        "Market data is sourced from official NSE archives and APIs, and NSE's terms of use "
        "govern redistribution of that data. Do not commit a data pack to a public "
        "repository, do not publish derived price series, and check the licence that "
        "applies to your copy before sharing anything built from it.\n\n"
        "The code in the repository and the published summary tables are separate from the "
        "underlying market data. See the licensing notes in the repository documentation "
        "for the current terms, and `docs/` for the data provenance policy."
    )

    st.subheader("Provenance")
    st.markdown(
        "- Engine: `nse_pipeline.backtest.momentum.MomentumRotationEngine`.\n"
        "- Exits: `nse_pipeline.backtest.exits.ExitPolicy` (causal `next_open` by default).\n"
        f"- Repo root: `{REPO_ROOT}`\n"
        "- This UI adds no calculation of its own beyond the metric definitions shown "
        "on the Results page, which are spelled out so they can be checked."
    )

    st.caption(
        "Testing status: the pages have been rendered and captured in "
        "`docs/assets/ui/`, but there is no automated browser test suite. "
        "Treat layout regressions as possible."
    )


PAGES = {
    "Home": page_home,
    "Data": page_data,
    "Create": page_create,
    "Backtest": page_backtest,
    "Results": page_results,
    "Compare": page_compare,
    "Docs": page_docs,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="NSE Alpha Lab", page_icon="", layout="wide")
    st.sidebar.title("NSE Alpha Lab")
    st.sidebar.caption("Research preview")
    choice = st.sidebar.radio("Page", list(PAGES))
    st.sidebar.markdown("---")
    st.sidebar.warning(
        "Unvalidated research tool.\n\nNot investment advice.\n\n"
        "The best result in a 968-trial sweep is a maximum, not an edge."
    )
    st.sidebar.caption(
        "engine: " + ("available" if ENGINE_AVAILABLE else "unavailable") + "\n\n"
        "packs: " + str(len(find_data_packs())) + "\n\n"
        "results table: " + ("present" if _public_results_path() else "absent")
    )
    pages = PAGES
    if not ENGINE_AVAILABLE:
        st.sidebar.info("Engine import failed; backtests are disabled.")
    init_state()
    pages[choice]()


if __name__ == "__main__":
    if st is None:
        # Imported without streamlit (for example during static analysis).
        # Say exactly what is missing instead of raising an opaque ImportError.
        print(
            "streamlit is not installed, so the UI cannot start.\n"
            "Install the app's extras first:\n"
            "  pip install -r apps/lab/requirements.txt\n"
            "then run:\n"
            "  streamlit run apps/lab/app.py",
            file=sys.stderr,
        )
        raise SystemExit(1)
    main()
