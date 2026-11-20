"""Cross-check Yahoo Finance for NSE index/prices; never invent PIT data.

Stdlib-only collector/validator (mirrors ``src/fundamentals.py``).  Probes
Yahoo's public chart endpoint (``query2.finance.yahoo.com/v8/finance/chart``,
no login, no API key) for a *bounded* universe only: ``^NSEI`` plus
``RELIANCE.NS`` / ``TCS.NS`` / ``INFY.NS``.  Fundamentals were assessed
separately with ``yfinance`` (a quoteSummary client) for the same three
tickers; those probe tables live in ``data/raw/yahoo/*_financials*.csv`` etc.
and are summarised by :func:`summarize_fundamentals_tables` -- they are NOT
fetched by this module because quoteSummary needs a crumb and, crucially,
carries no filing/publication date (see report).

Semantics that matter for the host backtest
--------------------------------------------
* Yahoo ``Close`` (``auto_adjust=False``) is **split/bonus-adjusted**
  retroactively: on 2000-01-03 Yahoo RELIANCE ``Close`` is 17.97 while the
  host raw ``close`` is 251.7 (ratio 0.0714).  Only after the last split do
  the two coincide (2026-11-06: both 1320.0).
* Yahoo ``Adj Close`` is **split/bonus + dividend** adjusted (total-return
  style).  The host ``adjusted_bars`` engine applies verified
  split/bonus only and never dividends, so
  ``Yahoo Adj / host adj_close`` is < 1 and drifts down with time
  (e.g. TCS 2005 median 0.08).  See :func:`compare_adjustment`.
* Yahoo index history for ``^NSEI`` starts **2007-09-17** (daily), not 1999;
  ``range=max&interval=1d`` silently downgrades to monthly.  This module
  therefore uses ``period1=0&period2=now&interval=1d`` and rejects any
  response whose ``dataGranularity`` is not ``1d``.
* Yahoo exposes **no index constituents** for ``^NSEI`` (no fund-data /
  holdings endpoint; ``FundsData`` raises "No Fund data found.").  Current
  components live on Yahoo's website pages, not via this API, and no
  historical membership series was observable.

Licensing note: Yahoo Terms of Use require express prior permission for
automated collection and bar commercial reuse/redistribution.  This collector
makes 4 bounded requests max, stops on any error (401/403/429 included),
never retries with a different identity, and is for personal research only.
Yahoo data must not be redistributed or used in a commercial product.
"""

from __future__ import annotations

import argparse
import collections
import datetime as _dt
import hashlib
import json
import math
import urllib.parse
import urllib.request
from pathlib import Path
from nse_pipeline.paths import data_root

CHART_API = "https://query2.finance.yahoo.com/v8/finance/chart"
# Bounded universe only -- never expand this to a full-NSE download.
TICKERS: tuple[str, ...] = ("^NSEI", "RELIANCE.NS", "TCS.NS", "INFY.NS")
USER_AGENT = "NSEHistoricalResearch/1.0 (personal historical-data research)"


def chart_url(ticker: str, period1: int = 0, period2: int | None = None) -> str:
    """Full daily history URL (period-based; range=max downgrades to monthly)."""
    if period2 is None:
        period2 = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    return (
        f"{CHART_API}/{urllib.parse.quote(ticker, safe='')}"
        f"?interval=1d&period1={period1}&period2={period2}"
        "&events=div%2Csplit&includePrePost=false"
    )


def raw_name(ticker: str) -> str:
    return f"yahoo-{ticker.replace('^', 'idx_').replace('.', '_')}-1d.json"


def validate_chart(data: object, ticker: str) -> dict:
    """Reject error payloads, missing results, and non-daily granularity."""
    if not isinstance(data, dict) or "chart" not in data:
        raise ValueError("Expected a Yahoo chart response")
    chart = data["chart"]
    if chart.get("error"):
        raise ValueError(f"Yahoo chart error for {ticker}: {chart['error']!r}")
    results = chart.get("result")
    if not results or not isinstance(results, list):
        raise ValueError(f"No chart result for {ticker}")
    result = results[0]
    meta = result.get("meta") or {}
    if meta.get("symbol") != ticker:
        raise ValueError(f"Symbol mismatch: {meta.get('symbol')!r} != {ticker!r}")
    if meta.get("dataGranularity") != "1d":
        raise ValueError(
            f"Unexpected granularity {meta.get('dataGranularity')!r} for {ticker}; "
            "range=max silently returns monthly -- refusing to store it as daily"
        )
    timestamps = result.get("timestamp") or []
    if not timestamps:
        raise ValueError(f"Empty chart timestamps for {ticker}")
    quotes = (result.get("indicators") or {}).get("quote") or []
    if not quotes or "close" not in quotes[0]:
        raise ValueError(f"Missing quote indicators for {ticker}")
    return result


def _epoch_to_date(epoch: int) -> str:
    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).date().isoformat()


def normalize_bars(result: dict) -> list[dict]:
    """Flatten one chart result into dated rows, preserving Yahoo's split.

    Keeps ``close`` (split/bonus-adjusted) and ``adjclose`` (dividend
    adjusted) as separate columns -- collapsing them would reproduce the
    host's raw-vs-adjusted confusion.  Dates use the exchange timezone
    offset carried in ``meta.gmtoffset`` when available (fallback UTC);
    the Yahoo timestamp is a UTC epoch, shifted to Asia/Kolkata (IST,
    UTC+5:30, no DST) for NSE symbols.  Rows with ``close is None`` (Yahoo
    emits these on 31 ^NSEI and ~10 equity dates) are dropped: they are not
    trading bars, and ``yfinance`` drops them identically.
    """
    meta = result.get("meta") or {}
    symbol = meta.get("symbol")
    gmtoffset = meta.get("gmtoffset", 19800)
    tz = _dt.timezone(_dt.timedelta(seconds=gmtoffset))
    timestamps = result["timestamp"]
    quotes = result["indicators"]["quote"][0]
    adj_rows = (result["indicators"].get("adjclose") or [{}])[0].get("adjclose") or []
    events = result.get("events") or {}
    splits = events.get("splits") or {}
    dividends = events.get("dividends") or {}

    def _event(epoch: int, table: dict) -> dict | None:
        entry = table.get(str(epoch)) or table.get(epoch)
        return entry if isinstance(entry, dict) else None

    rows: list[dict] = []
    for i, epoch in enumerate(timestamps):
        close = quotes["close"][i] if i < len(quotes["close"]) else None
        if close is None:
            continue  # Yahoo null placeholders are not trading bars.
        adj = adj_rows[i] if i < len(adj_rows) else None
        day = _dt.datetime.fromtimestamp(epoch, tz=tz).date().isoformat()
        split = _event(epoch, splits)
        div = _event(epoch, dividends)
        rows.append(
            {
                "date": day,
                "symbol": symbol,
                "open": quotes["open"][i],
                "high": quotes["high"][i],
                "low": quotes["low"][i],
                "close": close,
                "adjclose": adj,
                "volume": quotes["volume"][i],
                "split_ratio": (split or {}).get("splitRatio"),
                "dividend": (div or {}).get("amount"),
                # Yahoo adjustment factors made explicit for testing.
                "split_bonus_factor_close_over_adj": (
                    close / adj if close is not None and adj else None
                ),
            }
        )
    return rows


def compare_adjustment(yahoo_rows: list[dict], host_rows: list[dict]) -> dict:
    """Quantify Yahoo-vs-host adjustment on overlapping dates.

    ``host_rows`` are host ``adjusted_bars``-style dicts with ``date`` and
    ``adj_close`` (split/bonus only) plus ``close`` (raw nominal).
    Returns per-year median ratios and overlap counts; pure function.
    """
    yahoo = {r["date"]: r for r in yahoo_rows if r.get("date")}
    host = {r["date"]: r for r in host_rows if r.get("date")}
    common = sorted(set(yahoo) & set(host))
    by_year: dict[str, dict] = {}
    for year in sorted({d[:4] for d in common}):
        days = [d for d in common if d.startswith(year)]
        close_ratios = [
            yahoo[d]["close"] / host[d]["close"]
            for d in days
            if yahoo[d].get("close") and host[d].get("close")
        ]
        adj_ratios = [
            yahoo[d]["adjclose"] / host[d]["adj_close"]
            for d in days
            if yahoo[d].get("adjclose") and host[d].get("adj_close")
        ]

        def _med(values: list[float]) -> float | None:
            values = sorted(values)
            if not values:
                return None
            mid = len(values) // 2
            return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2

        by_year[year] = {
            "n_common": len(days),
            "median_yahoo_close_over_host_raw": _med(close_ratios),
            "median_yahoo_adj_over_host_adj": _med(adj_ratios),
        }
    return {
        "yahoo_rows": len(yahoo_rows),
        "host_rows": len(host_rows),
        "common_dates": len(common),
        "first_common": common[0] if common else None,
        "last_common": common[-1] if common else None,
        "by_year": by_year,
    }


def summarize_fundamentals_tables(tables: dict[str, list[str]]) -> dict:
    """Summarise yfinance fundamentals probe tables (pure assessment).

    ``tables`` maps a table label (e.g. ``financials_annual``) to its
    column dates (period ends, as ISO strings).  Yahoo returns only ~4-5
    annual and ~5-7 quarterly period-ends, earliest FY2022 or FY2023, and
    **no publication/filing date column** -- so any use with ``as_of``
    filtering would leak (a value dated 2026-03-31 would look known on
    that day).  This helper encodes that assessment so a backtest cannot
    mistake period-end for point-in-time.
    """
    per_table: dict[str, dict] = {}
    for label, dates in tables.items():
        parsed = sorted(d for d in dates if d)
        per_table[label] = {
            "n_period_ends": len(parsed),
            "earliest_period_end": parsed[0] if parsed else None,
            "latest_period_end": parsed[-1] if parsed else None,
            "has_publication_date": False,
            "point_in_time_safe": False,
        }
    publication_dates_present = any(t["has_publication_date"] for t in per_table.values())
    return {
        "tables": per_table,
        "publication_dates_present": publication_dates_present,
        "point_in_time_safe": publication_dates_present,
        "earliest_year": (
            min(
                (t["earliest_period_end"][:4] for t in per_table.values() if t["earliest_period_end"]),
                default=None,
            )
        ),
    }


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def acquire(raw: Path, tickers: tuple[str, ...] = TICKERS) -> None:
    """At most one request per ticker. Stop on any error; never retry."""
    raw.mkdir(parents=True, exist_ok=True)
    manifest_path = raw / "acquisition.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for ticker in tickers:
        name = raw_name(ticker)
        path = raw / name
        url = chart_url(ticker)
        cached = path.exists()
        record: dict = {"url": url, "cached_at_run": cached}
        try:
            if cached:
                content = path.read_bytes()
                expected = manifest.get(name, {}).get("sha256")
                if expected and hashlib.sha256(content).hexdigest() != expected:
                    raise ValueError(f"Cached source hash changed: {name}")
            else:
                req = urllib.request.Request(
                    url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=60) as response:
                    content = response.read()
                    record.update(
                        http_status=response.status,
                        retrieved_at=_dt.datetime.now().astimezone().isoformat(),
                    )
            result = validate_chart(json.loads(content), ticker)
            bars = normalize_bars(result)
            record.update(
                rows=len(bars),
                granularity=(result.get("meta") or {}).get("dataGranularity"),
                first_date=bars[0]["date"] if bars else None,
                last_date=bars[-1]["date"] if bars else None,
                first_trade_date=_epoch_to_date(
                    int((result.get("meta") or {}).get("firstTradeDate", 0))
                )
                if (result.get("meta") or {}).get("firstTradeDate")
                else None,
            )
            if not cached:
                path.write_bytes(content)
            record.update(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
            manifest[name] = {**manifest.get(name, {}), **record}
            manifest[name].pop("error", None)
            write_json(manifest_path, manifest)
            print(f"Ready {name}: {len(bars)} bars", flush=True)
        except Exception as error:
            manifest[name] = {**manifest.get(name, {}), **record, "error": repr(error)}
            write_json(manifest_path, manifest)
            raise  # Respect 401/403/429; never retry with a different identity.


def _fundamentals_summary_from_raw(raw: Path) -> dict:
    tables: dict[str, list[str]] = {}
    for path in sorted(raw.glob("*_financials_*.csv")) + sorted(raw.glob("*_balance_sheet*.csv")) + sorted(raw.glob("*_cashflow*.csv")):
        header = path.read_text(encoding="utf-8").splitlines()[0] if path.read_text(encoding="utf-8") else ""
        cols = [c.strip() for c in header.split(",")][1:]
        tables[path.name] = cols
    return summarize_fundamentals_tables(tables)


def build(raw: Path, processed: Path, report: Path, data_root: Path | None = None) -> dict:
    rows: list[dict] = []
    per_ticker: dict[str, dict] = {}
    missing: list[str] = []
    for ticker in TICKERS:
        path = raw / raw_name(ticker)
        if not path.exists():
            missing.append(path.name)
            continue
        result = validate_chart(json.loads(path.read_bytes()), ticker)
        bars = normalize_bars(result)
        rows.extend(bars)
        per_ticker[ticker] = {
            "rows": len(bars),
            "api_timestamps": len(result.get("timestamp") or []),
            "null_close_rows_dropped": len(result.get("timestamp") or []) - len(bars),
            "first_date": bars[0]["date"] if bars else None,
            "last_date": bars[-1]["date"] if bars else None,
            "first_trade_date": (
                _epoch_to_date(int((result.get("meta") or {})["firstTradeDate"]))
                if (result.get("meta") or {}).get("firstTradeDate")
                else None
            ),
            "splits": sum(1 for b in bars if b["split_ratio"] not in (None, 0)),
            "dividends": sum(1 for b in bars if b["dividend"] not in (None, 0)),
            "adj_equals_close_at_latest": (
                bool(bars) and bars[-1]["close"] == bars[-1]["adjclose"]
            ),
        }
    rows.sort(key=lambda r: (r["symbol"] or "", r["date"] or ""))
    # Bounded processed artefact (JSONL, not a full parquet): 4 tickers only.
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "yahoo_bars.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    coverage = {
        "tickers": list(TICKERS),
        "per_ticker": per_ticker,
        "missing_files": missing,
        "total_rows": len(rows),
        "complete": not missing and len(rows) > 0,
        "complete_note": "Complete only for the 4 bounded probe tickers; no full-universe Yahoo download was attempted.",
        "nsei_daily_start": (per_ticker.get("^NSEI") or {}).get("first_date"),
        "nsei_daily_start_note": "Yahoo ^NSEI daily starts 2007-09-17, not 1999 (firstTradeDate). Monthly max extends no further.",
        "constituents": {
            "available": False,
            "evidence": "Ticker(^NSEI).funds_data raises 'No Fund data found.'; info has no constituent/holding keys.",
            "historical_membership": False,
        },
        "fundamentals": _fundamentals_summary_from_raw(raw),
        "adjustment_note": (
            "Yahoo Close = split/bonus-adjusted; Yahoo Adj Close = split/bonus+dividend. "
            "Host adjusted_bars = split/bonus only (no dividends). Compare via compare_adjustment()."
        ),
        "limitations": [
            "Yahoo Close is retroactively split/bonus-adjusted and cannot be used as nominal host-like OHLC.",
            "Yahoo Adj Close adds dividend adjustment and is not comparable to the host split/bonus-only series.",
            "Fundamentals tables carry period-end dates only, no publication dates: not point-in-time safe.",
            "No Yahoo ^NSEI constituents or historical membership: current-only even where a website list exists.",
            "Yahoo terms require express permission for automated collection and bar commercial redistribution; personal research only.",
        ],
    }
    write_json(report, coverage)
    return coverage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=data_root())
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args()
    raw = args.data / "raw/yahoo"
    if args.fetch:
        acquire(raw)
    result = build(
        raw,
        args.data / "processed/yahoo",
        args.data / "processed/yahoo/coverage.json",
    )
    write_json(args.data / "reports/yahoo-coverage.json", result)
    print(json.dumps({k: result[k] for k in ("total_rows", "missing_files", "nsei_daily_start")}))


if __name__ == "__main__":
    main()
