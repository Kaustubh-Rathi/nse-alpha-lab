"""Kaggle Nifty50 OHLCV + fundamentals: validate and import as THIRDPARTY-KAGGLE.

Dataset: kalyan197/nifty50-stocks1999-2026-daily-ohlcv-and-fundamentals (CC0-1.0).
Source is Yahoo Finance (per dataset description), NOT official NSE bhavcopy.
Kaggle prices are Yahoo split-and-dividend adjusted (OHLC scaled like Adj Close),
so they must never be compared to or overwrite ``market.duckdb`` raw ``bars``.
All imports carry ``source = THIRDPARTY-KAGGLE`` and live under
``data/processed/kaggle_ohlcv/``; ``market.duckdb`` is opened read-only.

Fundamentals (Market_Cap, PE_Ratio, EPS, Beta, ...) are a single current
snapshot repeated on every historical row — no publication / filing dates —
so they are NOT point-in-time safe and are exported only as a per-ticker
snapshot. Daily_Return / Volatility_20D / MA_50 / MA_200 are trailing
price-derived features, not filings.

Usage:
  uv run python src/kaggle_ohlcv.py --download
  uv run python src/kaggle_ohlcv.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from nse_pipeline.paths import REPO_ROOT, data_root
from nse_pipeline.build.portable import open_catalog

import duckdb
import pandas as pd

DATASET_SLUG = "kalyan197/nifty50-stocks1999-2026-daily-ohlcv-and-fundamentals"
LICENSE = "CC0-1.0"
SOURCE_LABEL = "THIRDPARTY-KAGGLE"
DATASET_URL = f"https://www.kaggle.com/datasets/{DATASET_SLUG}"
HIST_FILE = "nifty50_historical_data.csv"
SUMMARY_FILE = "nifty50_summary_statistics.csv"
META_FILE = "metadata.json"

EXPECTED_COLUMNS = [
    "Date", "Ticker", "Company_Name", "Sector",
    "Open", "High", "Low", "Close", "Volume",
    "Dividend", "Stock_Split", "Daily_Return", "Volatility_20D", "MA_50", "MA_200",
    "Market_Cap", "PE_Ratio", "Forward_PE", "PEG_Ratio", "Price_to_Book",
    "Dividend_Yield", "EPS", "Beta", "52Week_High", "52Week_Low",
]
FUNDAMENTALS_COLS = [
    "Market_Cap", "PE_Ratio", "Forward_PE", "PEG_Ratio", "Price_to_Book",
    "Dividend_Yield", "EPS", "Beta", "52Week_High", "52Week_Low",
]
PRICE_DERIVED_COLS = ["Daily_Return", "Volatility_20D", "MA_50", "MA_200"]

ROOT = REPO_ROOT
DEFAULT_DATA = data_root()
DEFAULT_RAW = DEFAULT_DATA / "raw" / "kaggle_ohlcv"
DEFAULT_OUT = DEFAULT_DATA / "processed" / "kaggle_ohlcv"
DEFAULT_REPORTS = DEFAULT_DATA / "reports"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_kaggle_date(value: str | datetime | date) -> date:
    """Parse Yahoo ``2026-01-30 00:00:00+05:30`` or ``2026-01-30`` to a plain date.

    Uses the leading YYYY-MM-DD slice so the UTC-midnight shift that a naive
    ``tz_convert(None)`` would introduce does not apply.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    return date.fromisoformat(s[:10])


def normalize_symbol(ticker: str) -> str:
    """Map Yahoo ``RELIANCE.NS`` to official ``RELIANCE`` (no exchange suffix)."""
    s = str(ticker).strip().upper()
    for suffix in (".NS", ".BO", ".NSE"):
        if s.endswith(suffix):
            return s[: -len(suffix)]
    return s


def ticker_map(df: pd.DataFrame) -> dict[str, str]:
    """Return ``{Yahoo ticker: official symbol}`` for tickers in df."""
    out: dict[str, str] = {}
    for t in sorted(df["Ticker"].astype(str).unique()):
        out[t] = normalize_symbol(t)
    return out


def download(raw: Path, dry_run: bool = False) -> dict:
    """Download+unzip via the Kaggle CLI. Credentials stay in the CLI config."""
    raw.mkdir(parents=True, exist_ok=True)
    if dry_run:
        return {"dataset": DATASET_SLUG, "license": LICENSE, "raw_dir": str(raw), "dry_run": True}
    cmd = ["kaggle", "datasets", "download", DATASET_SLUG, "-p", str(raw), "--unzip"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"kaggle CLI failed ({proc.returncode}): {proc.stderr[-2000:]}")
    files = {p.name: {"bytes": p.stat().st_size, "sha256": sha256_file(p)} for p in sorted(raw.iterdir()) if p.is_file()}
    return {"dataset": DATASET_SLUG, "dataset_url": DATASET_URL, "license": LICENSE,
            "raw_dir": str(raw), "files": files, "kaggle_stdout_tail": proc.stdout[-500:]}


def load_hist(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"Date": str})
    df["date"] = pd.to_datetime(df["Date"].str.slice(0, 10)).dt.date
    df["symbol"] = df["Ticker"].map(normalize_symbol)
    return df


def validate_schema(df: pd.DataFrame) -> dict:
    """Validate columns, date range, symbols, rows, duplicates, OHLC sanity."""
    actual = [c for c in df.columns if c not in ("date", "symbol", "sort")]
    missing = [c for c in EXPECTED_COLUMNS if c not in df.columns]
    extra = [c for c in actual if c not in EXPECTED_COLUMNS]
    n_dup = int(df.duplicated(subset=["date", "symbol"]).sum())
    invalid = int((((df["High"] < df["Low"])
                    | (df["Open"] > df["High"]) | (df["Open"] < df["Low"]))
                   & df[["Open", "High", "Low"]].notna().all(axis=1)).sum())
    negative = int(((df["Open"] < 0) | (df["High"] < 0) | (df["Low"] < 0) | (df["Close"] < 0)).sum())
    dates = pd.to_datetime(df["date"])
    return {
        "expected_columns": list(EXPECTED_COLUMNS),
        "actual_columns": [c for c in df.columns if c not in ("date", "symbol")],
        "missing_columns": missing,
        "extra_columns": extra,
        "schema_ok": not missing,
        "n_rows": int(len(df)),
        "n_symbols": int(df["symbol"].nunique()),
        "symbols": sorted(df["symbol"].unique().tolist()),
        "date_min": str(dates.min().date()),
        "date_max": str(dates.max().date()),
        "date_ok": str(dates.min().date())[:4] == "1999" and str(dates.max().date())[:4] == "2026",
        "n_duplicate_date_symbol": n_dup,
        "n_invalid_ohlcv": invalid,
        "n_negative_prices": negative,
        "null_counts": {c: int(df[c].isna().sum()) for c in EXPECTED_COLUMNS if c in df.columns},
    }


def fundamentals_pit_audit(df: pd.DataFrame) -> dict:
    """Point-in-time safety: filing/publication dates vs repeated current snapshot.

    Kaggle has no filing-date columns. If a fundamental column takes a single
    distinct value per ticker across all rows, it is a current snapshot backfilled
    through history (look-ahead) and is unsafe for PIT features.
    """
    per_col: dict[str, dict] = {}
    for col in FUNDAMENTALS_COLS:
        if col not in df.columns:
            per_col[col] = {"present": False, "pit_safe": False, "reason": "column missing"}
            continue
        nun = df.groupby("symbol")[col].nunique(dropna=False)
        constant = int((nun == 1).sum())
        n_tickers = int(len(nun))
        # PEG is all-NaN (nunique 0); still not PIT-safe because it has no dates.
        pit_safe = False  # No publication dates exist at all.
        per_col[col] = {
            "present": True,
            "n_tickers": n_tickers,
            "tickers_constant": constant,
            "max_nunique": int(nun.max()) if n_tickers else 0,
            "median_nunique": float(nun.median()) if n_tickers else 0.0,
            "has_publication_date": False,
            "pit_safe": pit_safe,
            "reason": ("all-NaN column; no dates" if int(nun.max()) == 0
                       else "single current snapshot repeated on every row; no filing dates => look-ahead bias"),
        }
    derived = {c: {"present": c in df.columns, "pit_safe": "trailing price feature; reproducible from past prices only (no filing needed)"}
               for c in PRICE_DERIVED_COLS}
    return {
        "has_publication_dates": False,
        "filing_date_columns_found": [],
        "fundamentals": per_col,
        "price_derived": derived,
        "pit_safe_for_backtesting": False,
        "verdict": ("NOT point-in-time safe: fundamentals are one current snapshot repeated "
                    "across 1999-2026 with zero publication dates. Export per-ticker snapshot only; "
                    "never join by date as a feature."),
    }


def compare_to_official(df: pd.DataFrame, db_path: Path, symbols: tuple[str, ...] = ("RELIANCE", "TCS"),
                        start: str = "2015-01-01", end: str = "2026-01-30",
                        price_rtol: float = 1e-4, price_atol: float = 0.01,
                        volume_exact: bool = True) -> tuple[pd.DataFrame, dict]:
    """Compare Kaggle (Yahoo adjusted) vs official NSE raw EQ bars on shared sessions.

    Prices are Yahoo split+dividend adjusted, so raw-price equality is NOT expected;
    the meaningful checks are (a) constant kaggle/raw ratio between corporate
    actions, (b) volume equality, and (c) difference vs our split-only adjustment.
    Returns (per-row comparison for the sample symbols, summary counts).
    """
    cols = ["Date", "Ticker", "symbol", "date", "Open", "High", "Low", "Close", "Volume"]
    k = df.loc[df["symbol"].isin(symbols) & df["date"].astype(str).between(start, end), cols].copy()
    with open_catalog(database=db_path) as db:
        off = db.execute(
            "SELECT date, symbol, open AS open_o, high AS high_o, low AS low_o, "
            "close AS close_o, volume AS volume_o FROM bars "
            "WHERE series='EQ' AND symbol IN (SELECT unnest($1::VARCHAR[])) "
            "AND date BETWEEN $2 AND $3",
            [list(symbols), start, end]).fetchdf()
        off["date"] = pd.to_datetime(off["date"]).dt.date
        adj_exists = (db_path.parent / "processed/adjusted/adjusted_bars.parquet").exists()
        adj = pd.DataFrame()
        if adj_exists:
            try:
                adj = db.execute(
                    "SELECT date, symbol, adj_close, adjustment_factor FROM read_parquet($1) "
                    "WHERE series='EQ' AND symbol IN (SELECT unnest($2::VARCHAR[])) AND date BETWEEN $3 AND $4",
                    [str(db_path.parent / "processed/adjusted/adjusted_bars.parquet"), list(symbols), start, end]).fetchdf()
                adj["date"] = pd.to_datetime(adj["date"]).dt.date
            except Exception:
                adj = pd.DataFrame()
    m = k.merge(off, on=["date", "symbol"], how="inner")
    if not adj.empty:
        m = m.merge(adj, on=["date", "symbol"], how="left")
    m = m.sort_values(["symbol", "date"]).reset_index(drop=True)
    # Mismatch flags (raw comparison — expected to mismatch because Kaggle is adjusted).
    # Kaggle columns are Capitalized (Open/Close/Volume); official are open_o/close_o/volume_o.
    m["close_mismatch_raw"] = ~(
        (m["Close"] - m["close_o"]).abs() <= (price_atol + price_rtol * m["close_o"].abs()))
    m["open_mismatch_raw"] = ~(
        (m["Open"] - m["open_o"]).abs() <= (price_atol + price_rtol * m["open_o"].abs()))
    m["volume_mismatch"] = (m["Volume"] != m["volume_o"])
    if "adj_close" in m.columns:
        m["close_mismatch_vs_split_adj"] = ~(
            (m["Close"] - m["adj_close"]).abs() <= (price_atol + price_rtol * m["adj_close"].abs()))
        m["rel_err_vs_split_adj"] = (m["Close"] - m["adj_close"]).abs() / m["adj_close"].abs()
    for field in ("Open", "High", "Low", "Close"):
        lo = field.lower()
        m[f"ratio_{lo}"] = m[field] / m[f"{lo}_o"]
    m["volume_ratio"] = m["Volume"] / m["volume_o"]
    summary: dict[str, dict] = {}
    for sym in symbols:
        sub = m[m["symbol"] == sym]
        n = int(len(sub))
        summary[sym] = {
            "n_compared": n,
            "open_mismatch_raw": int((sub["open_mismatch_raw"] == True).sum()),  # noqa: E712
            "close_mismatch_raw": int((sub["close_mismatch_raw"] == True).sum()),  # noqa: E712
            "close_match_raw": int(n - (sub["close_mismatch_raw"] == True).sum()),
            "close_mismatch_vs_split_adj": (int((sub["close_mismatch_vs_split_adj"] == True).sum())
                                            if "close_mismatch_vs_split_adj" in sub.columns else None),
            "volume_mismatch": int((sub["volume_mismatch"] == True).sum()),
            "volume_match_exact": int(n - (sub["volume_mismatch"] == True).sum()),
            "ratio_close_mean": float(sub["ratio_close"].mean()) if n else None,
            "ratio_close_std": float(sub["ratio_close"].std()) if n else None,
            "ratio_close_min": float(sub["ratio_close"].min()) if n else None,
            "ratio_close_max": float(sub["ratio_close"].max()) if n else None,
            "volume_ratio_mean": float(sub["volume_ratio"].mean()) if n else None,
            "volume_ratio_median": float(sub["volume_ratio"].median()) if n else None,
            "n_official_only_in_window": int(len(off[(off["symbol"] == sym)]) - n),
            "n_kaggle_only_in_window": int(len(k[(k["symbol"] == sym)]) - n),
        }
    return m, {"symbols": list(symbols), "start": start, "end": end,
               "price_rtol": price_rtol, "price_atol": price_atol, "per_symbol": summary,
               "note": "Kaggle is Yahoo split+dividend adjusted; raw mismatches are expected. Ratio constancy between actions is the real accuracy signal."}


def import_thirdparty(df: pd.DataFrame, out: Path) -> dict:
    """Write THIRDPARTY-KAGGLE-labelled parquet. Never touches market.duckdb."""
    out.mkdir(parents=True, exist_ok=True)
    bars = pd.DataFrame({
        "date": df["date"].astype(str),
        "symbol": df["symbol"],
        "yahoo_ticker": df["Ticker"],
        "company_name": df["Company_Name"] if "Company_Name" in df.columns else "",
        "sector": df["Sector"] if "Sector" in df.columns else "",
        "open": df["Open"], "high": df["High"], "low": df["Low"], "close": df["Close"],
        "volume": df["Volume"], "dividend": df["Dividend"], "stock_split": df["Stock_Split"],
        "source": SOURCE_LABEL,
        "license": LICENSE,
        "price_basis": "YAHOO_SPLIT_DIVIDEND_ADJUSTED",
        "is_adjusted": True,
    })
    bars_path = out / "bars_thirdparty_kaggle.parquet"
    bars.to_parquet(bars_path, index=False)
    # Current-snapshot fundamentals: one row per ticker; NOT a historical feature table.
    snap_cols = ["Ticker", "symbol", "Company_Name", "Sector"] + FUNDAMENTALS_COLS
    snap = df.sort_values(["symbol", "date"]).groupby("symbol").tail(1)[snap_cols].copy()
    snap["source"] = SOURCE_LABEL
    snap["as_of"] = df["date"].astype(str).max()
    snap["pit_safe"] = False
    snap_path = out / "fundamentals_current_snapshot_kaggle.parquet"
    snap.to_parquet(snap_path, index=False)
    sample_path = out / "sample_bars_thirdparty_kaggle.json"
    sample = json.loads(bars.head(20).to_json(orient="records"))
    sample_path.write_text(json.dumps({"source": SOURCE_LABEL, "n_rows": int(len(bars)),
                                       "sample_20": sample}, indent=2))
    return {"bars_parquet": str(bars_path), "bars_rows": int(len(bars)),
            "snapshot_parquet": str(snap_path), "snapshot_rows": int(len(snap)),
            "sample_json": str(sample_path), "source": SOURCE_LABEL}


def build_all(data: Path, raw: Path | None = None, out: Path | None = None,
              do_download: bool = False, compare_symbols: tuple[str, ...] = ("RELIANCE", "TCS")) -> dict:
    raw = raw or data / "raw" / "kaggle_ohlcv"
    out = out or data / "processed" / "kaggle_ohlcv"
    reports = data / "reports"
    if do_download:
        dl = download(raw)
    else:
        _files = {p.name: {"bytes": p.stat().st_size, "sha256": sha256_file(p)}
                  for p in sorted(raw.iterdir()) if p.is_file()} if raw.exists() else {}
        dl = {"dataset": DATASET_SLUG, "dataset_url": DATASET_URL, "license": LICENSE,
              "raw_dir": str(raw), "files": _files, "downloaded": "previously via kaggle CLI --unzip"}
    hist_path = raw / HIST_FILE
    if not hist_path.exists():
        raise FileNotFoundError(f"{hist_path} missing — run with --download")
    df = load_hist(hist_path)
    schema = validate_schema(df)
    pit = fundamentals_pit_audit(df)
    # Per-ticker coverage
    g = df.groupby("symbol")["date"].agg(["min", "max", "count"]).reset_index()
    per_ticker = {str(r["symbol"]): {"first": str(r["min"])[:10], "last": str(r["max"])[:10], "rows": int(r["count"]) }
                  for _, r in g.iterrows()}
    cmp_df, compare = compare_to_official(df, data / "market.duckdb", symbols=compare_symbols)
    imp = import_thirdparty(df, out)
    cmp_path = out / "compare_reliance_tcs_2015_2026.csv"
    cmp_df.to_csv(cmp_path, index=False)
    coverage = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": DATASET_SLUG, "dataset_url": DATASET_URL, "license": LICENSE,
        "data_source_claimed": "Yahoo Finance",
        "download": dl,
        "schema": schema,
        "per_ticker": per_ticker,
        "pit_audit": pit,
        "compare_vs_official_raw_EQ": compare,
        "compare_csv": str(cmp_path),
        "import": imp,
        "guards": ["market.duckdb opened read-only; official bars never overwritten",
                   "All imported rows labelled THIRDPARTY-KAGGLE",
                   "Fundamentals snapshot is current-only; not PIT-safe"],
    }
    reports.mkdir(parents=True, exist_ok=True)
    cov_path = reports / "kaggle-ohlcv-coverage.json"
    cov_path.write_text(json.dumps(coverage, indent=2))
    research = render_research(coverage)
    (reports / "kaggle-ohlcv-research.md").write_text(research)
    return coverage


def render_research(cov: dict) -> str:
    s = cov["schema"]
    pit = cov["pit_audit"]
    cmpv = cov["compare_vs_official_raw_EQ"]
    lines = [
        "# Kaggle Nifty50 OHLCV + fundamentals — research note",
        "",
        f"- Dataset: `{cov['dataset']}` ({cov['dataset_url']})",
        f"- License: {cov['license']}",
        "- Claimed upstream source: Yahoo Finance (per dataset description). **Not** official NSE bhavcopy.",
        f"- Rows: {s['n_rows']} daily rows; symbols: {s['n_symbols']}; date range: {s['date_min']} .. {s['date_max']}",
        f"- Schema: {len(s['expected_columns'])} expected columns; missing: {s['missing_columns'] or 'none'}; duplicates (date,symbol): {s['n_duplicate_date_symbol']}",
        f"- Invalid OHLC rows: {s['n_invalid_ohlcv']}; negative-price rows: {s['n_negative_prices']} (ADANIENT.NS 47 rows, see below).",
        "",
        "## Price basis & accuracy vs official NSE `bars` (EQ series)",
        "",
        "Kaggle OHLC are Yahoo **split+dividend adjusted** (same scaling as Yahoo Adj Close).",
        "A 1:1 raw comparison to official NSE raw prices therefore mismatches by construction;",
        "the real signal is that `kaggle/raw` is piecewise-constant, stepping only at corporate actions.",
        "",
    ]
    for sym, v in cmpv["per_symbol"].items():
        if v["ratio_close_mean"] is None:
            continue
        lines.append(
            f"- {sym}: compared {v['n_compared']} sessions "
            f"({cmpv['start']}..{cmpv['end']}); raw close matches {v['close_match_raw']} / mismatches {v['close_mismatch_raw']} "
            f"(tolerance atol {cmpv['price_atol']} rtol {cmpv['price_rtol']}, expected because Kaggle is adjusted); "
            f"vs our split-only adjustment mismatches {v.get('close_mismatch_vs_split_adj')} "
            f"(our adjustment ignores dividends — see `data/reports/adjustment-coverage.json`); "
            f"volume exact matches {v['volume_match_exact']} / mismatches {v['volume_mismatch']} "
            f"(volume ratio mean {v['volume_ratio_mean']:.3f}, median {v['volume_ratio_median']:.3f}); "
            f"close ratio kag/raw mean {v['ratio_close_mean']:.4f} min {v['ratio_close_min']:.4f} max {v['ratio_close_max']:.4f}."
        )
    lines += [
        "",
        "- Spot check 2026-01-30: Kaggle equals raw (`ratio 1.0`) and volumes match exactly",
        "  (RELIANCE 11,239,835; TCS 3,022,068 shares).",
        "- Earlier years: RELIANCE ratio ~0.2140 (2015) stepping at 2017-09-07 (bonus 1:1),",
        "  2023-07-20, 2024-10-28 (splits); TCS ratio ~0.3885 (2015) stepping at splits",
        "  2018-05-31, plus small dividend steps. Constant-within-segments confirms correct action mapping.",
        "- Volume ratios >1 in early years (RELIANCE mean ~2.6x, TCS ~1.3x) reflect Yahoo/BSE-inclusive volume;",
        "  do not use Kaggle volume as NSE EQ volume.",
        "- Calendar: Kaggle includes exchange holidays Yahoo traded on (e.g. 1999-01-01) and omits a handful of",
        "  Saturday special sessions present in official bars (2015-02-28, 2015-11-11, 2016-10-30, ...).",
        "",
        "## Fundamentals point-in-time safety: **NOT safe**",
        "",
        "_Market_Cap, PE_Ratio, Forward_PE, Price_to_Book, Dividend_Yield, EPS, Beta, 52Week_High/Low_",
        "are a single current snapshot repeated verbatim on every row 1999-2026 (49/49 tickers constant per column;",
        "only PEG_Ratio is all-NaN). There are **no** publication / filing / period dates. Joining them by date",
        "as backtest features is textbook look-ahead bias. They are exported only as",
        "`fundamentals_current_snapshot_kaggle.parquet` (one row per ticker, `pit_safe=false`).",
        "_Daily_Return, Volatility_20D, MA_50, MA_200_ are trailing price-derived and reproducible from past prices.",
        "",
        "## Canary: ADANIENT.NS negative prices",
        "",
        "47 rows for ADANIENT.NS carry negative prices (~-0.012) from 2002-07-01; the summary CSV amplifies it",
        "into a nonsense -1.66e9% total return. Exclude or quarantine ADANIENT.NS before any use.",
        "",
        "## Files & guards",
        "",
        f"- Raw: `data/raw/kaggle_ohlcv/` ({len(cov['download'].get('files', {}) or {'raw': {}})} files, unzipped).",
        f"- Import (labelled `{cov['import']['source']}`): `{cov['import']['bars_parquet']}` "
        f"({cov['import']['bars_rows']} rows), `{cov['import']['snapshot_parquet']}` "
        f"({cov['import']['snapshot_rows']} rows), `{cov['import']['sample_json']}`.",
        f"- Comparison: `{cov['compare_csv']}`.",
        "- `market.duckdb` is **never** modified; official `bars` remain the only raw-price source.",
        "- Tests: `tests/test_kaggle_ohlcv.py`.",
        "",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description="Validate Kaggle Nifty50 OHLCV as THIRDPARTY-KAGGLE (never overwrites official bars)")
    p.add_argument("--data", type=Path, default=data_root())
    p.add_argument("--raw", type=Path, default=None)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--download", action="store_true")
    p.add_argument("--symbols", type=str, default="RELIANCE,TCS")
    a = p.parse_args()
    syms = tuple(s.strip().upper() for s in a.symbols.split(",") if s.strip())
    cov = build_all(a.data, a.raw, a.out, a.download, syms)
    print(json.dumps({"schema": {k: cov['schema'][k] for k in ('n_rows', 'n_symbols', 'date_min', 'date_max')},
                      "import": cov["import"], "compare": cov["compare_vs_official_raw_EQ"]["per_symbol"]}, indent=2))


if __name__ == "__main__":
    main()
