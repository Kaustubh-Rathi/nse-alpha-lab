"""Triage ``needs_review`` corporate actions into actionable buckets.

Reads ``market.duckdb`` read-only (``corporate_actions``) and classifies every
``parse_status='needs_review'`` row into exactly one bucket:

    dividend_cash, rights, buyback, agm_other, debt_interest,
    scheme_reorg, unparseable

No price adjustment and no total-return application is performed here.
``dividend_cash`` only means the ``purpose`` text carries a per-share
``RS/RE/INR`` amount that could later be added as a total-return cash flow;
the amount is triaged, never applied.  ``scheme_reorg`` rows are flagged for
linking to the scheme ledger (``data/processed/schemes/scheme-index.jsonl``);
no ratio/cash term is extracted.  ``debt_interest`` and ``agm_other`` are
non-equity / ignore-for-price buckets.  ``unparseable`` keeps everything else
-- notably bare dividends without an amount (``INTERIM DIVIDEND``) and
split/bonus rows the conservative parser left for review -- so the
price-relevant remainder is never silently dropped.

Outputs (owned by this module only)
-----------------------------------
* ``data/processed/actions_triage/triage.parquet`` -- one row per
  ``needs_review`` action with bucket, price-relevance flag and parsed
  dividend/rights helpers.
* ``data/processed/actions_triage/dividend_sample.jsonl`` -- bounded sample
  of ``dividend_cash`` parses (equity first).
* ``data/reports/actions-triage.json`` and ``data/reports/actions-triage.md``.

``market.duckdb`` is opened with ``read_only=True`` and never written.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from nse_pipeline.paths import data_root
from nse_pipeline.build.portable import open_catalog

import pandas as pd

BUCKETS = (
    "dividend_cash",
    "rights",
    "buyback",
    "agm_other",
    "debt_interest",
    "scheme_reorg",
    "unparseable",
)

EQUITY_SERIES = ("EQ", "BE", "SM", "ST")
EQUITY_VIEW_SERIES = ("EQ", "BE", "BZ", "SM", "ST")

# Price-relevant = affects equity price level or total return if its terms
# were known.  debt_interest / agm_other are the ignore-for-equity-price
# buckets; unparseable is split below (price-relevant split/bonus leftovers
# vs. the rest) via the ``price_relevant`` column.
PRICE_RELEVANT_BUCKETS = ("dividend_cash", "rights", "buyback", "scheme_reorg")

DIV_SIGNAL = re.compile(r"\b(?:DIVIDEND|DIVD|DIVI|DIVID|DIV|INTDIV|SPLDIV|DIVDND|DISTRIBUTION)\b", re.I)
RIGHT_SIGNAL = re.compile(r"\b(?:RIGHTS?|RHTS|RGHTS|RGHT)\b", re.I)
BUYBACK_SIGNAL = re.compile(r"BUY[-\s]?BACK", re.I)
BUYBACK_PAYMENT = re.compile(r"PAYMENT OF AMT OF BUYBACK", re.I)
SCHEME_SIGNAL = re.compile(
    r"\b(?:SCHEME|SCHM|SCH|AGMT|MERGER|DEMERGER|AMALG|ARRANG|ARRNG|"
    r"CAPITAL RED|CONSOLID|DELIST)\b", re.I
)
# Interest payment and redemption language on debt/MF/N-series.  DIV is
# excluded upstream so ``INTDIV`` (an equity dividend) never lands here.
INTEREST_SIGNAL = re.compile(
    r"(?:INTEREST|INT[-\s]?PAY(?:MENT|MNT|MENT)|INTPAY(?:MENT|MNT)|INTPYMNT|"
    r"INTPAYMENT|INTERST|INT PAYMT|INT PAYMENT|ANNUAL INTEREST)",
    re.I,
)
REDEMPTION_SIGNAL = re.compile(r"REDEMPTION|REDEMPTN|PART REDMPTN|PART REDM|PARTREDEMPTION", re.I)
AGM_SIGNAL = re.compile(
    r"\b(?:AGM|EGM|EOGM|MEETING|BOOK CLOS|ANNUAL CLOS|E-VOTING|GENERAL MEETING|"
    r"EXTRA GENERAL|ANNUAL GENERAL|NOVATION DEED|REISSUE-BOND|CONVERSION INTO EQUITY)\b",
    re.I,
)
DIV_AMOUNT = re.compile(r"(?:RS\.?|RE\.?|INR)\s*(\d+(?:\.\d+)?)", re.I)
RIGHTS_RATIO = re.compile(r"(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)")
RIGHTS_PREMIUM = re.compile(r"(?:@|PRM|PREM(?:IUM|UM)?)\s*(?:RS\.?|RE\.?)\s*(\d+(?:\.\d+)?)", re.I)


def dividend_amounts(purpose: str) -> list[float]:
    """All RS/RE/INR per-share amounts named in a purpose string."""
    return [float(x) for x in DIV_AMOUNT.findall(str(purpose or ""))]


def parse_dividend(purpose: str) -> dict:
    """Parse a dividend-like purpose conservatively.

    Returns ``is_dividend_like``, ``amounts``, ``dividend_per_share`` (only
    for a single amount), ``total_if_summed`` (multi-amount rows only) and
    ``parseable``.  A ``%``-only dividend or a bare ``INTERIM DIVIDEND``
    has no amount and is not parseable.  Multi-amount rows (e.g.
    ``FIN+SPL``) are parseable but flagged -- the host parser left them for
    review precisely because summing needs a documented rule.
    """
    text = str(purpose or "")
    is_like = bool(DIV_SIGNAL.search(text)) or action_looks_like_distribution(text)
    amounts = dividend_amounts(text)
    parsed: dict = {
        "is_dividend_like": is_like,
        "amounts": amounts,
        "amount_count": len(amounts),
        "dividend_per_share": None,
        "total_if_summed": None,
        "parseable": False,
        "pct_only": "%" in text and not amounts,
    }
    if not is_like or not amounts:
        return parsed
    if len(amounts) == 1:
        parsed["dividend_per_share"] = amounts[0]
        parsed["parseable"] = True
    else:
        parsed["total_if_summed"] = round(sum(amounts), 10)
        parsed["parseable"] = True
    return parsed


def action_looks_like_distribution(text: str) -> bool:
    return bool(re.search(r"\bDISTRIBUTION\b", str(text or ""), re.I))


def parse_rights(purpose: str) -> dict:
    """Extract the ``a:b`` rights ratio and ``@ PRM`` premium if stated."""
    text = str(purpose or "")
    has_signal = bool(RIGHT_SIGNAL.search(text))
    ratio = RIGHTS_RATIO.search(text) if has_signal else None
    premium = RIGHTS_PREMIUM.search(text) if has_signal else None
    return {
        "has_rights_signal": has_signal,
        "ratio": f"{ratio.group(1)}:{ratio.group(2)}" if ratio else None,
        "premium_per_share": float(premium.group(1)) if premium else None,
        "parseable": bool(ratio),
    }


def is_price_relevant(bucket: str, action_type: str) -> bool:
    """True when the row could change an equity price/total return."""
    if bucket in PRICE_RELEVANT_BUCKETS:
        return True
    if bucket == "unparseable" and action_type and (
        "split" in str(action_type) or "bonus" in str(action_type)
    ):
        return True
    return False


def triage_row(purpose: str, action_type: str = "") -> dict:
    """Assign one bucket to a needs_review row (first match wins).

    Priority is deliberate: scheme language outranks debt so a
    ``SCH AGMT-BONUS`` is not lost as interest; debt outranks a bare
    ``DIV/REDEMPTION`` MF row without an amount so it stays out of the
    equity queue; buyback/rights outrank dividend so compound rows are
    triaged by their price-adjusting leg first.  The result is a triage
    label only -- no factor or cash flow is applied.
    """
    text = str(purpose or "")
    upper = text.upper()
    dividend = parse_dividend(text)
    rights = parse_rights(text)
    atype = str(action_type or "").strip().lower()

    def result(bucket: str, reason: str) -> dict:
        return {
            "bucket": bucket,
            "reason": reason,
            "dividend_per_share_triaged": dividend["dividend_per_share"],
            "dividend_amounts": dividend["amounts"],
            "dividend_amount_count": dividend["amount_count"],
            "dividend_total_if_summed": dividend["total_if_summed"],
            "dividend_parseable": dividend["parseable"],
            "dividend_pct_only": dividend["pct_only"],
            "is_dividend_like": dividend["is_dividend_like"],
            "rights_ratio": rights["ratio"],
            "rights_premium_per_share": rights["premium_per_share"],
            "rights_parseable": rights["parseable"],
            "price_relevant": is_price_relevant(bucket, atype),
        }

    # 1. scheme / reorganization -- includes delisting (needs ledger).
    if SCHEME_SIGNAL.search(text) or "reorganization" in atype:
        return result("scheme_reorg", "scheme_or_reorganization_language")
    # 2. debt interest / redemption -- excludes INTDIV dividends.
    has_interest = bool(INTEREST_SIGNAL.search(text)) and not dividend["is_dividend_like"]
    has_redemption = bool(REDEMPTION_SIGNAL.search(text)) and not dividend["parseable"]
    if has_interest or has_redemption:
        if not dividend["is_dividend_like"]:
            return result("debt_interest", "interest_or_redemption_language")
        # Dividend-bearing redemption rows without a parseable amount stay
        # out of the equity queue as debt-like; parseable ones fall through
        # to dividend_cash below.
        if not dividend["parseable"]:
            return result("debt_interest", "redemption_with_dividend_language_no_amount")
    # 3. buyback
    if BUYBACK_SIGNAL.search(text) or BUYBACK_PAYMENT.search(text):
        return result("buyback", "buyback_language")
    # 4. rights (explicit signal)
    if rights["has_rights_signal"]:
        return result("rights", "rights_language")
    # 5. dividend cash -- per-share parseable only.
    if dividend["parseable"]:
        if "%" in text and dividend["amount_count"] == 0:
            return result("unparseable", "dividend_pct_only_no_per_share")
        number_of_amounts = dividend["amount_count"]
        reason = "single_amount_per_share" if number_of_amounts == 1 else "multi_amount_compound_needs_review"
        return result("dividend_cash", reason)
    # 6. AGM / otherignore.
    if AGM_SIGNAL.search(text):
        return result("agm_other", "meeting_or_book_closure_language")
    # 7. residuals.  Split 'SPL' inside DIV-INT+SPL is special dividend
    # without amount -> unparseable; bare INTERIM DIVIDEND likewise.
    if dividend["is_dividend_like"]:
        return result("unparseable", "dividend_without_per_share_amount")
    return result("unparseable", "no_price_signal_or_compound_review")


def classify_actions(actions: pd.DataFrame) -> pd.DataFrame:
    """Add triage columns to a needs_review frame (pure, no DB)."""
    frame = actions.copy()
    rows = [triage_row(str(r.get("purpose") or ""), str(r.get("action_type") or "")) for _, r in frame.iterrows()]
    frame["bucket"] = [r["bucket"] for r in rows]
    frame["triage_reason"] = [r["reason"] for r in rows]
    frame["price_relevant"] = [r["price_relevant"] for r in rows]
    frame["is_equity_subset"] = frame["series"].astype(str).isin(EQUITY_SERIES)
    frame["is_equity_view"] = frame["series"].astype(str).isin(EQUITY_VIEW_SERIES)
    frame["ex_year"] = pd.to_datetime(frame["ex_date"]).dt.normalize().dt.year
    frame["dividend_per_share_triaged"] = [r["dividend_per_share_triaged"] for r in rows]
    frame["dividend_amounts"] = [r["dividend_amounts"] for r in rows]
    frame["dividend_amount_count"] = [r["dividend_amount_count"] for r in rows]
    frame["dividend_total_if_summed"] = [r["dividend_total_if_summed"] for r in rows]
    frame["dividend_parseable"] = [r["dividend_parseable"] for r in rows]
    frame["rights_ratio"] = [r["rights_ratio"] for r in rows]
    frame["rights_premium_per_share"] = [r["rights_premium_per_share"] for r in rows]
    frame["rights_parseable"] = [r["rights_parseable"] for r in rows]
    return frame


def summarize(triaged: pd.DataFrame, parsed_dividend: pd.DataFrame | None = None) -> dict:
    """Build the JSON-serializable triage summary (pure, no DB)."""
    total = int(len(triaged))
    bucket_counts = collections.Counter(triaged["bucket"])
    equity = triaged[triaged["is_equity_subset"]]
    equity_view = triaged[triaged["is_equity_view"]]
    by_bucket_year: dict[str, dict[str, int]] = {}
    for bucket in sorted(bucket_counts):
        sub = triaged[triaged["bucket"] == bucket]
        by_bucket_year[bucket] = {str(k): int(v) for k, v in sub.groupby("ex_year").size().items()}
    by_bucket_series: dict[str, dict[str, int]] = {}
    for bucket in sorted(bucket_counts):
        sub = triaged[triaged["bucket"] == bucket]
        top = sub["series"].astype(str).value_counts().head(10)
        by_bucket_series[bucket] = {str(k): int(v) for k, v in top.items()}
    cash = triaged[triaged["bucket"] == "dividend_cash"]
    single = cash[cash["dividend_amount_count"] == 1]
    multi = cash[cash["dividend_amount_count"] > 1]
    eq_cash = cash[cash["is_equity_subset"]]
    bare_div = triaged[triaged["triage_reason"] == "dividend_without_per_share_amount"]
    split_bonus = triaged[triaged["bucket"] == "unparseable"][
        triaged[triaged["bucket"] == "unparseable"]["action_type"].astype(str).str.contains("split|bonus", case=False)
    ]
    equity_price_relevant = equity[equity["price_relevant"]]
    dividend_by_year: dict[str, dict] = {}
    for year, group in cash.groupby("ex_year"):
        single_y = group[group["dividend_amount_count"] == 1]
        multi_y = group[group["dividend_amount_count"] > 1]
        dividend_by_year[str(int(year))] = {
            "dividend_cash_rows": int(len(group)),
            "single_amount_rows": int(len(single_y)),
            "single_sum_rs_per_share": float(single_y["dividend_per_share_triaged"].sum()) if len(single_y) else 0.0,
            "multi_amount_rows": int(len(multi_y)),
            "multi_sum_if_summed": float(multi_y["dividend_total_if_summed"].sum()) if len(multi_y) else 0.0,
        }
    parsed_context: dict = {}
    if parsed_dividend is not None and len(parsed_dividend):
        parsed_context = {
            "parsed_dividend_rows": int(len(parsed_dividend)),
            "parsed_dividend_sum_rs_per_share": float(parsed_dividend["dividend_per_share"].sum()),
            "parsed_dividend_median_rs_per_share": float(parsed_dividend["dividend_per_share"].median()),
            "parsed_dividend_max_rs_per_share": float(parsed_dividend["dividend_per_share"].max()),
        }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_database": "data/market.duckdb (read-only)",
        "scope": "corporate_actions WHERE parse_status='needs_review'; no adjustment applied",
        "total_needs_review": total,
        "bucket_counts": {bucket: int(bucket_counts.get(bucket, 0)) for bucket in BUCKETS},
        "bucket_by_year": by_bucket_year,
        "bucket_top_series": by_bucket_series,
        "price_relevant_rows": int(triaged["price_relevant"].sum()),
        "price_relevant_by_bucket": {
            bucket: int(triaged[(triaged["bucket"] == bucket) & triaged["price_relevant"]].shape[0])
            for bucket in BUCKETS
        },
        "ignore_for_equity_price_rows": int(total - triaged["price_relevant"].sum()),
        "equity_subset": {
            "definition": f"series IN {list(EQUITY_SERIES)}",
            "rows": int(len(equity)),
            "bucket_counts": {bucket: int(equity[equity["bucket"] == bucket].shape[0]) for bucket in BUCKETS},
            "price_relevant_rows": int(equity["price_relevant"].sum()),
            "price_relevant_share": float(equity["price_relevant"].sum() / len(equity)) if len(equity) else 0.0,
            "ignore_for_price_rows": int(len(equity) - equity["price_relevant"].sum()),
        },
        "equity_view_subset": {
            "definition": f"series IN {list(EQUITY_VIEW_SERIES)} (equity_corporate_actions view)",
            "rows": int(len(equity_view)),
            "bucket_counts": {bucket: int(equity_view[equity_view["bucket"] == bucket].shape[0]) for bucket in BUCKETS},
            "price_relevant_rows": int(equity_view["price_relevant"].sum()),
        },
        "equity_price_relevant_bucket_counts": {
            bucket: int(equity_price_relevant[equity_price_relevant["bucket"] == bucket].shape[0])
            for bucket in BUCKETS
        },
        "dividend_cash": {
            "rows": int(len(cash)),
            "single_amount_rows": int(len(single)),
            "single_amount_sum_rs_per_share": float(single["dividend_per_share_triaged"].sum()) if len(single) else 0.0,
            "single_amount_median_rs_per_share": float(single["dividend_per_share_triaged"].median()) if len(single) else None,
            "single_amount_max_rs_per_share": float(single["dividend_per_share_triaged"].max()) if len(single) else None,
            "multi_amount_rows": int(len(multi)),
            "multi_amount_sum_if_summed_rs_per_share": float(multi["dividend_total_if_summed"].sum()) if len(multi) else 0.0,
            "equity_rows": int(len(eq_cash)),
            "equity_single_rows": int(len(eq_cash[eq_cash["dividend_amount_count"] == 1])),
            "equity_single_sum_rs_per_share": float(
                eq_cash[eq_cash["dividend_amount_count"] == 1]["dividend_per_share_triaged"].sum()
            ) if len(eq_cash[eq_cash["dividend_amount_count"] == 1]) else 0.0,
            "equity_multi_rows": int(len(eq_cash[eq_cash["dividend_amount_count"] > 1])),
            "equity_multi_sum_if_summed_rs_per_share": float(
                eq_cash[eq_cash["dividend_amount_count"] > 1]["dividend_total_if_summed"].sum()
            ) if len(eq_cash[eq_cash["dividend_amount_count"] > 1]) else 0.0,
            "by_year": dividend_by_year,
            "note": (
                "Sums are sums of per-share amounts across action rows (Rs/share), "
                "NOT rupees payable.  Rupees payable need shares held per symbol/series; "
                "nothing is applied to prices."
            ),
        },
        "bare_dividend_without_amount_rows": int(len(bare_div)),
        "bare_dividend_equity_rows": int(bare_div["is_equity_subset"].sum()) if len(bare_div) else 0,
        "parsed_dividend_context": parsed_context,
        "split_bonus_unparseable_rows": int(len(split_bonus)),
        "split_bonus_unparseable_equity_rows": int(split_bonus["is_equity_subset"].sum()) if len(split_bonus) else 0,
        "rights_parseable_rows": int(triaged["rights_parseable"].sum()),
        "rights_equity_parseable_rows": int(triaged[triaged["is_equity_subset"]]["rights_parseable"].sum()),
        "recommendation": {
            "total_return_action": (
                "Do not price-adjust for dividends.  For a total-return variant, "
                "add dividend_cash single-amount rows as ex-date cash per share "
                "(dividend_per_share_triaged) to the holder's cash, and exclude "
                "multi-amount rows until FIN/INT/SPL components are verified; "
                "bare dividends (INTERIM DIVIDEND without amount) and split/bonus "
                "unparseable rows stay excluded with a disclosed sensitivity."
            ),
            "equity_backtest_filter": (
                "Exclude debt_interest and agm_other entirely; carry rights, "
                "buyback and scheme_reorg as known-unadjusted price breaks with "
                "flagged sensitivity rather than invented factors."
            ),
        },
        "limitations": [
            "Bucketing is purpose-text based; purpose can combine legs (e.g. "
            "BONUS+DIV) and the priority rule triages by the price-adjusting leg first.",
            "Equity is series-based (EQ/BE/SM/ST); ETF/debt classification is not complete.",
            "Dividend sums are per-share sums across rows, not rupee cash; share counts are needed.",
            "Multi-amount dividend rows are parseable but not summed for application.",
            "Scheme rows link to the ledger by symbol only; no ratio is inferred.",
            "Pre-2011 needs_review is outside this table (pre2011_actions has tags only).",
        ],
        "files": {
            "triage_parquet": "data/processed/actions_triage/triage.parquet",
            "dividend_sample": "data/processed/actions_triage/dividend_sample.jsonl",
            "triage_json": "data/reports/actions-triage.json",
            "triage_md": "data/reports/actions-triage.md",
        },
    }


def build_sample(triaged: pd.DataFrame, limit: int = 2000) -> pd.DataFrame:
    """Bounded dividend_cash sample: equity rows first, then the rest."""
    cash = triaged[triaged["bucket"] == "dividend_cash"].copy()
    cash["_equity_first"] = (~cash["is_equity_subset"]).astype(int)
    sample = cash.sort_values(["_equity_first", "ex_date", "symbol", "series"], kind="stable").drop(
        columns=["_equity_first"]
    ).head(limit)
    return sample.drop(columns=["dividend_amounts"], errors="ignore")


def run_triage(data_root: Path, out_root: Path | None = None, report_root: Path | None = None) -> dict:
    """Run the triage read-only against ``data_root/market.duckdb``."""
    import duckdb

    data_root = Path(data_root)
    out_root = Path(out_root) if out_root is not None else data_root / "processed" / "actions_triage"
    report_root = Path(report_root) if report_root is not None else data_root / "reports"
    out_root.mkdir(parents=True, exist_ok=True)
    report_root.mkdir(parents=True, exist_ok=True)
    db_path = data_root / "market.duckdb"
    if not db_path.exists():
        raise FileNotFoundError(f"Missing {db_path}; build the database first")
    con = open_catalog(database=db_path)
    try:
        actions = con.execute(
            "SELECT symbol, series, ex_date, record_date, purpose, action_type, "
            "share_multiplier, dividend_per_share, parse_status, first_observed, "
            "last_observed, source FROM corporate_actions "
            "WHERE parse_status='needs_review' ORDER BY ex_date, symbol, series, purpose"
        ).fetchdf()
        parsed_dividend = con.execute(
            "SELECT dividend_per_share FROM corporate_actions "
            "WHERE parse_status='parsed_not_applied' AND dividend_per_share IS NOT NULL"
        ).fetchdf()
    finally:
        con.close()
    triaged = classify_actions(actions)
    # Triage table: keep provenance + bucket columns; drop the raw list column
    # for parquet friendliness (amounts stay in the sample JSONL as arrays).
    triage_out = triaged.drop(columns=["dividend_amounts"], errors="ignore")
    triage_out.to_parquet(out_root / "triage.parquet", index=False, compression="zstd")
    sample = build_sample(triaged)
    sample_records = sample.to_dict(orient="records")
    (out_root / "dividend_sample.jsonl").write_text(
        "\n".join(json.dumps(record, default=str, ensure_ascii=False) for record in sample_records) + "\n",
        encoding="utf-8",
    )
    summary = summarize(triaged, parsed_dividend)
    # Scheme-ledger link diagnostics (symbol overlap only, never a ratio).
    ledger = data_root / "processed" / "schemes" / "scheme-index.jsonl"
    if ledger.exists():
        ledger_symbols = {
            json.loads(line).get("symbol")
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        ledger_symbols.discard(None)
        triage_scheme_symbols = set(triaged.loc[triaged["bucket"] == "scheme_reorg", "symbol"].astype(str))
        summary["scheme_ledger_link"] = {
            "ledger_path": "data/processed/schemes/scheme-index.jsonl",
            "ledger_symbols": len(ledger_symbols),
            "triage_scheme_rows": int((triaged["bucket"] == "scheme_reorg").sum()),
            "triage_scheme_symbols": len(triage_scheme_symbols),
            "triage_scheme_symbols_in_ledger": len(triage_scheme_symbols & ledger_symbols),
            "note": "Symbol overlap only; appearance in the ledger is not a verified ratio or match on this ex-date.",
        }
    (report_root / "actions-triage.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (report_root / "actions-triage.md").write_text(_render_markdown(summary), encoding="utf-8")
    return summary


def _render_markdown(summary: dict) -> str:
    lines = [
        "# Corporate-action needs_review triage",
        "",
        f"Generated {summary['generated_at']} from `data/market.duckdb` (read-only).",
        f"Scope: `corporate_actions` with `parse_status='needs_review'` ({summary['total_needs_review']:,} rows).",
        "No price adjustment and no total-return application is performed.",
        "",
        "## Bucket counts",
        "",
        "| bucket | rows | price-relevant | note |",
        "|---|---:|---:|---|",
    ]
    notes = {
        "dividend_cash": "per-share parseable; candidate total-return cash",
        "rights": "price-adjusting issuance; terms not applied",
        "buyback": "price-adjusting; terms not applied",
        "agm_other": "ignore for equity price",
        "debt_interest": "exclude from equity backtest",
        "scheme_reorg": "link to scheme ledger; no ratio applied",
        "unparseable": "bare dividends without amount + split/bonus leftovers",
    }
    for bucket in BUCKETS:
        lines.append(
            f"| {bucket} | {summary['bucket_counts'][bucket]:,} | "
            f"{summary['price_relevant_by_bucket'][bucket]:,} | {notes[bucket]} |"
        )
    lines += [
        "",
        f"- Price-relevant rows (all series): {summary['price_relevant_rows']:,} "
        f"({summary['price_relevant_rows'] / summary['total_needs_review']:.1%} of needs_review).",
        f"- Ignore-for-price rows (debt_interest + agm_other): {summary['ignore_for_equity_price_rows']:,}.",
        "",
        "## Equity subset",
        "",
        f"- Series EQ/BE/SM/ST: {summary['equity_subset']['rows']:,} needs_review rows; "
        f"price-relevant: {summary['equity_subset']['price_relevant_rows']:,} "
        f"({summary['equity_subset']['price_relevant_share']:.1%}), "
        f"ignore-for-price: {summary['equity_subset']['ignore_for_price_rows']:,}.",
        "",
        "| bucket | equity rows |",
        "|---|---:|",
    ]
    for bucket in BUCKETS:
        lines.append(f"| {bucket} | {summary['equity_subset']['bucket_counts'][bucket]:,} |")
    lines += [
        "",
        "## Dividend cash (per-share triaged, not applied)",
        "",
        f"- dividend_cash rows: {summary['dividend_cash']['rows']:,} "
        f"(single-amount {summary['dividend_cash']['single_amount_rows']:,}, "
        f"multi-amount {summary['dividend_cash']['multi_amount_rows']:,}).",
        f"- Single-amount sum (all series): {summary['dividend_cash']['single_amount_sum_rs_per_share']:.2f} Rs/share "
        f"across rows (median {summary['dividend_cash']['single_amount_median_rs_per_share']}, "
        f"max {summary['dividend_cash']['single_amount_max_rs_per_share']}).",
        f"- Equity single-amount: {summary['dividend_cash']['equity_single_rows']:,} rows, "
        f"sum {summary['dividend_cash']['equity_single_sum_rs_per_share']:.2f} Rs/share; "
        f"equity multi-amount: {summary['dividend_cash']['equity_multi_rows']:,} rows, "
        f"sum-if-summed {summary['dividend_cash']['equity_multi_sum_if_summed_rs_per_share']:.2f} Rs/share.",
        f"- Bare dividends without amount (unparseable): {summary['bare_dividend_without_amount_rows']:,} rows "
        f"({summary['bare_dividend_equity_rows']:,} equity) -- ex-div price drops with no amount in purpose.",
        f"- Split/bonus in unparseable: {summary['split_bonus_unparseable_rows']:,} rows "
        f"({summary['split_bonus_unparseable_equity_rows']:,} equity) -- price-relevant, ambiguous.",
        f"- Rights parseable ratio rows: {summary['rights_parseable_rows']:,} "
        f"({summary['rights_equity_parseable_rows']:,} equity).",
        "",
        "Sums are sums of per-share amounts across action rows, not rupees payable. "
        "Rupees payable require shares held per symbol/series; nothing is added to prices.",
        "",
        "## Bucket by year",
        "",
        "| year | " + " | ".join(BUCKETS) + " |",
        "|---:|" + "|---:|" * len(BUCKETS),
    ]
    years = sorted({y for b in summary["bucket_by_year"].values() for y in b})
    for year in years:
        lines.append("| " + year + " | " + " | ".join(str(summary["bucket_by_year"].get(b, {}).get(year, 0)) for b in BUCKETS) + " |")
    lines += [
        "",
        "## Recommendation",
        "",
        summary["recommendation"]["total_return_action"],
        "",
        summary["recommendation"]["equity_backtest_filter"],
        "",
        "## Limitations",
        "",
    ]
    lines += ["- " + item for item in summary["limitations"]]
    if "scheme_ledger_link" in summary:
        link = summary["scheme_ledger_link"]
        lines += [
            "",
            f"Scheme ledger link: {link['triage_scheme_symbols_in_ledger']:,}/{link['triage_scheme_symbols']:,} "
            f"triage scheme symbols appear in the scheme index; {link['triage_scheme_rows']:,} scheme rows triaged.",
        ]
    lines += [
        "",
        "## Files",
        "",
        "- `data/processed/actions_triage/triage.parquet` -- one row per needs_review action.",
        "- `data/processed/actions_triage/dividend_sample.jsonl` -- dividend_cash sample parse.",
        "- `data/reports/actions-triage.json` -- machine-readable summary.",
        "- `data/reports/actions-triage.md` -- this report.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=data_root(),
                        help="Directory holding market.duckdb and processed outputs")
    args = parser.parse_args()
    result = run_triage(args.data)
    print(json.dumps({
        "total_needs_review": result["total_needs_review"],
        "bucket_counts": result["bucket_counts"],
        "equity_rows": result["equity_subset"]["rows"],
        "equity_price_relevant_rows": result["equity_subset"]["price_relevant_rows"],
        "dividend_cash_rows": result["dividend_cash"]["rows"],
        "bare_dividend_without_amount_rows": result["bare_dividend_without_amount_rows"],
    }, indent=2))


if __name__ == "__main__":
    main()
