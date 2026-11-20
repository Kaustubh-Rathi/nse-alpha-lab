"""Special-dividend layer for NSE multi-amount rows (FIN / INT / SPL).

Reads read-only (never modifies):

* ``data/processed/actions_triage/triage.parquet`` -- the
  ``bucket='dividend_cash' AND dividend_amount_count>1`` rows only
  (546 rows: ``FIN+INT+SPL`` components the dividend layer excluded).
* ``data/processed/actions_dividends/dividend_events.jsonl`` -- the
  dividend layer's events (for same-day overlap disclosure and the TCS
  Yahoo validation baseline).
* ``data/processed/adjusted/adjusted_bars.parquet`` -- split-only
  ``adj_close`` / ``adjustment_factor`` (for ``P_prev`` and the 2% test).
* ``data/raw/yahoo/<SYMBOL>_NS_max_1d.csv`` -- Yahoo validation only.

Parsing (no fabrication)
------------------------
Each ``RS/RE/INR`` amount in a ``purpose`` is classified by the label
in its immediate context (checked before and after the amount):

* ``SPL`` -- ``SPL`` / ``SPLDIV`` / ``SPLDV`` / ``SPDV`` / ``SPDIV`` /
  ``SPCL`` (special dividend leg);
* ``INT`` -- ``INTDIV`` / ``INT DIV`` / ``INT`` (interim dividend leg);
* ``FIN`` -- ``FIN`` (final dividend leg);
* ``ORDINARY`` -- a bare ``DIV`` / ``DIVID`` / ``DISTRIBUTION`` label
  with none of the above (an ordinary dividend leg);
* ``NON_DIVIDEND`` -- ``REDM``/``REDEMPTION``, ``FVSPL``/face-value and
  ``SPLIT``-ratio legs (e.g. ``PART REDM RS2/DIV-RE0.11``: the Rs 2
  redemption is not dividend cash; ``DIV-RS6/SPLIT RS 10 TO RE 1``:
  the split leg is not dividend cash).  These legs are recorded and
  never counted in cash.

``SPL`` wins over ``DIV`` when both appear (``SPL DIV RS 2.5`` is a
special leg).  An amount whose context carries *two* dividend classes
(``DIV/SPDV RS 80 & RS 50`` -- labels together, amounts together) is
**ambiguous** and the whole row is excluded, as is any row with an
amount carrying *no* dividend label or with a duplicated component
carrying differing amounts.  Identical duplicates of one component
collapse to one value.  Ambiguous rows are kept in
``specials_events.jsonl`` with ``parse_status='ambiguous_excluded'``
but contribute nothing to any factor or bridge.

NSE 2% rule (NSE TR methodology)
--------------------------------
NSE treats an amount of at least 2% of the market price as a *special*
dividend: it is divisor-adjusted in the price-return (PR) index on the
ex-date and excluded from the total-return (TR) computation (return of
capital, not reinvestable income).  Amounts below 2% are ordinary
dividends handled like any other cash dividend (gross, ex-date
reinvestment, Yahoo/CRSP factor ``(P_prev - D_adj) / P_prev``).

For every parsed split leg::

    spl_yield = SPL_adj / P_prev,   SPL_adj = SPL / F_after

(the yield is split-invariant: numerator and denominator are both on
the split-adjusted basis).  Then:

* ``SPL < 2%`` -- the SPL leg is an ordinary TR dividend
  (``special_labelled_below_2pct``); it is included in the NSE TR
  bridge together with FIN/INT/ordinary legs.
* ``SPL >= 2%`` -- the SPL leg is excluded from the NSE TR bridge
  (``special_ge_2pct_excluded_from_TR``) and recorded as a PR
  divisor adjustment ``(P_prev - SPL_adj)/P_prev`` in
  ``pr_divisor_adjustments.jsonl`` (magnitude only; ``adj_close`` is
  owned by the split engine and is never rewritten here).
* No pre-ex bar -- the 2% test is untestable
  (``untestable_no_bars``); the row is excluded from both bridges
  with disclosure.

FIN/INT/ordinary legs are always ordinary (subject to the same bar
validity checks as the dividend layer).  The INT leg therefore
becomes a dividend event -- see ``specials_dividend_bridge.jsonl``,
which is the ``dividend_events`` update: one row per ex-date carrying
``nse_tr_per_share`` (FIN+INT+ordinary [+ SPL if below 2%]) and
``yahoo_tr_per_share`` (all cash legs including >=2% specials).
Yahoo Adj Close includes specials of every size, so the Yahoo bridge
is the comparable basis for the TCS isolation check; the NSE bridge
is the methodology basis.  For TCS every SPL leg is below 2%, so the
two bridges coincide there.

``market.duckdb`` is never opened -- all inputs are host artefacts.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from nse_pipeline.paths import data_root

import numpy as np
import pandas as pd

# Allow `python src/nse_pipeline/actions/specials.py` as well as
# `python -m nse_pipeline.actions.specials` (package-relative import
# of the dividend layer below).
_src_root = str(Path(__file__).resolve().parents[2])
if _src_root not in sys.path:
    sys.path.insert(0, _src_root)

SPECIAL_THRESHOLD = 0.02

DIV_AMOUNT = re.compile(r"(?:RS\.?|RE\.?|INR|RSE)\s*[-]?\s*(\d+(?:\.\d+)?|\.\d+)", re.I)
# Currency-less labelled amounts such as ``INT DIV 3`` (no RS prefix, no
# trailing per-share unit) or ``DIV-FIN 0.20`` -- the label classifies the
# number as rupees-per-share.  Percentage numbers (``6%``) never match.
_BARE_LEG_AMOUNT = re.compile(
    r"\b(?:(?:INTDIV|INTDIV|INT|INTRM|FINDIV|FIN|SPLDIV|SPLDV|SPDIV|SPDV|SPCL|SPL|SP|DIVIDEND|DIVID|DIV)\b[^+\-/\d]{0,24}?)(\d+(?:\.\d+)?|\.\d+)(?!\s*%)",
    re.I,
)
# Local context: ~30 chars before / ~18 chars after an amount match.
CTX_BEFORE = 34
CTX_AFTER = 20

_SPL_RE = re.compile(r"\b(?:SPLDIV|SPLDV|SPDIV|SPDV|SPCL|SPL|SP)\b", re.I)
_SPLIT_RATIO_RE = re.compile(r"\b(?:SPLIT|FVSPL|FACE VALUE|FACEVAL)", re.I)
_REDEM_RE = re.compile(r"\b(?:REDM|REDEMPTION|REDEMPTN)\b", re.I)
_INT_RE = re.compile(r"\b(?:INTDIV|INTRM|ITM|INT)\b", re.I)
_FIN_RE = re.compile(r"\b(?:FINDIV|FDIV|FIN)\b", re.I)
_DIV_RE = re.compile(r"\b(?:DIVIDEND|DIVDND|DIVID|DIVD|DIVI|DIV|DISTRIBUTION)\b", re.I)


def _classify_context(context: str) -> str | None:
    """Dividend class of one amount's context, or a non-dividend tag.

    Returns one of ``SPL`` / ``INT`` / ``FIN`` / ``ORDINARY`` /
    ``NON_DIVIDEND`` / ``MULTI`` (two dividend classes in one
    context, not separable without inventing an assignment) /
    ``None`` (no dividend label at all).
    """
    text = str(context or "")
    if _REDEM_RE.search(text) or _SPLIT_RATIO_RE.search(text):
        return "NON_DIVIDEND"
    classes: list[str] = []
    if _SPL_RE.search(text):
        classes.append("SPL")
    if _INT_RE.search(text):
        classes.append("INT")
    if _FIN_RE.search(text):
        classes.append("FIN")
    if _DIV_RE.search(text):
        # SPL/INT/FIN labels already capture the dividend class; a
        # bare DIV label alongside one of them (SPL DIV, INT DIV)
        # does not add a second class.
        if not classes:
            classes.append("ORDINARY")
    if not classes:
        return None
    if len(classes) > 1:
        return "MULTI"
    return classes[0]


def parse_components(purpose: str) -> dict:
    """Split a multi-amount ``purpose`` into FIN/INT/ordinary/SPL legs.

    Pure.  Returns per-leg Rs/share amounts (``None`` when a leg is
    absent), ``total_cash_per_share`` (sum of dividend legs only;
    ``None`` when ambiguous), ``non_dividend_legs`` (redemption /
    face-value / split-ratio legs, recorded but never counted as
    cash), ``parse_status`` (``parsed`` / ``ambiguous_excluded`` /
    ``no_components``), ``ambiguous_reason``, ``labels_found`` and
    ``amount_count`` (all RS/RE/INR amounts seen, dividend or not).
    """
    text = str(purpose or "")
    # Combined label with amounts pooled after it (``DIV/SPDV RS 80 & RS 50``):
    # the label order cannot be trusted as the amount assignment, so the
    # whole row is ambiguous rather than guessed.
    if re.search(r"\bDIV\s*/\s*(?:SPDV|SPDIV|SPLDIV|SPLDV)\s*(?:RS|RE|INR)", text, re.I):
        return {
            "fin_per_share": None, "int_per_share": None, "ordinary_per_share": None,
            "spl_per_share": None, "total_cash_per_share": None, "non_dividend_legs": [],
            "parse_status": "ambiguous_excluded",
            "ambiguous_reason": "combined_label_amounts_not_separable",
            "labels_found": [], "amount_count": len(DIV_AMOUNT.findall(text)),
            "amounts": [float(x) for x in DIV_AMOUNT.findall(text)],
        }
    matches = list(DIV_AMOUNT.finditer(text))
    # No RS/RE/INR amount: fall back to labelled currency-less numbers
    # (``DIV-FIN 0.20+SPL 0.15``).  Unlabelled or %-suffixed numbers are
    # never treated as cash (strict).
    bare_matches = False
    if not matches and "%" not in text:
        matches = list(_BARE_LEG_AMOUNT.finditer(text))
        bare_matches = True
    amounts = [float(m.group(1)) for m in matches]
    result: dict = {
        "fin_per_share": None,
        "int_per_share": None,
        "ordinary_per_share": None,
        "spl_per_share": None,
        "total_cash_per_share": None,
        "non_dividend_legs": [],
        "parse_status": "no_components",
        "ambiguous_reason": "",
        "labels_found": [],
        "amount_count": len(amounts),
        "amounts": amounts,
    }
    if not amounts:
        result["ambiguous_reason"] = "no_per_share_amount"
        return result

    legs: dict[str, list[float]] = {"FIN": [], "INT": [], "ORDINARY": [], "SPL": []}
    labels: list[str] = []
    # Face-value pairs ("RS 10 TO RE 1") are one non-dividend leg with
    # two amounts; mark both spans up front.
    face_value_spans = [
        m.span() for m in re.finditer(r"(?:RS|RE|INR)\s*\d+(?:\.\d+)?\s*TO\s*(?:RS|RE|INR)\s*\d+(?:\.\d+)?", text, re.I)
    ]
    for pos, match in enumerate(matches):
        amount = float(match.group(1))
        # Context is this amount's own segment only: bounded by the
        # neighbouring amounts and by + / & separators, so a label
        # belonging to the next leg never leaks into this amount.
        lo = matches[pos - 1].end() if pos > 0 else 0
        hi = matches[pos + 1].start() if pos < len(matches) - 1 else len(text)
        before = text[lo: match.start()]
        if bare_matches:
            # The bare match itself *starts* with its label
            # (``FIN 0.20``, ``SPL 0.15``), so ``before`` alone holds
            # only the separator.  Fold the match's own pre-number
            # label back into the classification context.
            before = before + match.group(0)[: match.start(1) - match.start()]
        after = text[match.end(): hi]
        for sep in ("+", "/", "&"):
            if sep in before:
                before = before.rsplit(sep, 1)[-1]
            if sep in after:
                after = after.split(sep, 1)[0]
        if any(s <= match.start() and match.end() <= e for s, e in face_value_spans):
            cls = "NON_DIVIDEND"
        else:
            # A label before the amount classifies it (``INTDIV RS 8``
            # is INT even though the next leg's ``SPLDV`` follows the
            # amount); only unlabeled amounts use a trailing label
            # (``RE 0.50 SPL``).
            cls = _classify_context(before[-CTX_BEFORE:])
            if cls is None or cls == "MULTI":
                trailing = _classify_context(after[:CTX_AFTER])
                cls = cls if cls == "MULTI" else trailing
        if cls == "MULTI":
            result["parse_status"] = "ambiguous_excluded"
            result["ambiguous_reason"] = "combined_label_amounts_not_separable"
            result["labels_found"] = sorted(set(labels))
            return result
        if cls == "NON_DIVIDEND":
            result["non_dividend_legs"].append(amount)
            continue
        if cls is None:
            result["parse_status"] = "ambiguous_excluded"
            result["ambiguous_reason"] = "amount_without_dividend_label"
            result["labels_found"] = sorted(set(labels))
            return result
        legs[cls].append(amount)
        labels.append(cls)

    if not any(legs.values()):
        result["parse_status"] = "no_components"
        result["ambiguous_reason"] = "no_dividend_component"
        result["labels_found"] = sorted(set(labels))
        return result

    total = 0.0
    mapping = {
        "FIN": "fin_per_share",
        "INT": "int_per_share",
        "ORDINARY": "ordinary_per_share",
        "SPL": "spl_per_share",
    }
    for cls, values in legs.items():
        if not values:
            continue
        if len(values) > 1:
            if len(set(values)) > 1:
                result["parse_status"] = "ambiguous_excluded"
                result["ambiguous_reason"] = f"duplicate_{cls.lower()}_component_differing_amounts"
                result["labels_found"] = sorted(set(labels))
                return result
            values = values[:1]  # identical duplicate announcement
        result[mapping[cls]] = float(values[0])
        total += float(values[0])

    result["total_cash_per_share"] = round(total, 10)
    result["parse_status"] = "parsed"
    result["ambiguous_reason"] = ""
    result["labels_found"] = sorted(set(labels))
    return result


def classify_special(spl_nominal: float | None, prev_close: float | None, future_split: float) -> dict:
    """Apply the NSE >=2% rule to one SPL leg (pure).

    Returns ``is_special_2pct`` (``None`` when there is no SPL leg or
    the test is untestable), ``spl_adj``, ``spl_yield``,
    ``nse_treatment`` and ``pr_divisor_factor`` (``None`` unless the
    leg is a >=2% special excluded from TR).
    """
    empty = {
        "is_special_2pct": None,
        "spl_adj": None,
        "spl_yield": None,
        "nse_treatment": "no_special_leg",
        "pr_divisor_factor": None,
    }
    if spl_nominal is None:
        return empty
    if prev_close is None or not (math.isfinite(prev_close) and prev_close > 0):
        return {**empty, "spl_adj": None, "nse_treatment": "untestable_no_bars"}
    if not (math.isfinite(future_split) and future_split > 0):
        return {**empty, "spl_adj": None, "nse_treatment": "untestable_no_bars"}
    spl_adj = float(spl_nominal) / float(future_split)
    yld = spl_adj / float(prev_close)
    if yld >= SPECIAL_THRESHOLD:
        factor = (float(prev_close) - spl_adj) / float(prev_close) if spl_adj < prev_close else None
        return {
            "is_special_2pct": True,
            "spl_adj": spl_adj,
            "spl_yield": yld,
            "nse_treatment": "special_ge_2pct_excluded_from_TR",
            "pr_divisor_factor": factor,
        }
    return {
        "is_special_2pct": False,
        "spl_adj": spl_adj,
        "spl_yield": yld,
        "nse_treatment": "special_labelled_below_2pct",
        "pr_divisor_factor": None,
    }


def _lookup_prev_price(bars_sorted_lines: dict, symbol: str, series: str, ex_date) -> dict:
    """Last-bar-before / first-bar-on-or-after lookup (pure, per line)."""
    line = bars_sorted_lines.get((symbol, series))
    if line is None or not len(line):
        return {"prev_bar_date": None, "prev_close": None, "future_split": None, "reason": "no_bars_for_line"}
    dates = line["date"].to_numpy(dtype="datetime64[ns]")
    ex = np.datetime64(pd.Timestamp(ex_date).normalize())
    pre_idx = int(np.searchsorted(dates, ex, side="left")) - 1
    if pre_idx < 0:
        return {"prev_bar_date": None, "prev_close": None, "future_split": None, "reason": "no_pre_ex_bar"}
    prev_row = line.iloc[pre_idx]
    post_idx = int(np.searchsorted(dates, ex, side="left"))
    future_split = float(line.iloc[post_idx]["adjustment_factor"]) if post_idx < len(line) else 1.0
    prev_close = float(prev_row["adj_close"])
    return {
        "prev_bar_date": prev_row["date"].date().isoformat(),
        "prev_close": prev_close,
        "future_split": future_split,
        "reason": "",
    }


def build_specials_events(multi: pd.DataFrame) -> pd.DataFrame:
    """Parse every multi-amount row into one row per distinct event.

    Pure.  Identical (symbol, series, ex_date, component) rows
    collapse with ``duplicate_count``; same-day differing totals are
    kept and flagged ``ambiguous_same_day`` (never summed, never
    picked -- mirroring the dividend layer).
    """
    parsed_rows = []
    for _, r in multi.iterrows():
        comp = parse_components(str(r.get("purpose") or ""))
        parsed_rows.append({
            "symbol": r["symbol"],
            "series": r["series"],
            "ex_date": pd.Timestamp(r["ex_date"]).normalize().date(),
            "purpose": str(r.get("purpose") or ""),
            "fin_per_share": comp["fin_per_share"],
            "int_per_share": comp["int_per_share"],
            "ordinary_per_share": comp["ordinary_per_share"],
            "spl_per_share": comp["spl_per_share"],
            "total_cash_per_share": comp["total_cash_per_share"],
            "non_dividend_legs": json.dumps(comp["non_dividend_legs"]),
            "parse_status": comp["parse_status"],
            "ambiguous_reason": comp["ambiguous_reason"],
            "labels_found": "+".join(comp["labels_found"]),
            "amount_count": comp["amount_count"],
        })
    if not parsed_rows:
        return pd.DataFrame(columns=[
            "symbol", "series", "ex_date", "purpose", "fin_per_share", "int_per_share",
            "ordinary_per_share", "spl_per_share", "total_cash_per_share",
            "non_dividend_legs", "parse_status", "ambiguous_reason", "labels_found",
            "amount_count", "duplicate_count", "ambiguous_same_day",
        ])
    raw = pd.DataFrame(parsed_rows)
    raw["ex_date"] = pd.to_datetime(raw["ex_date"]).dt.date

    key_cols = ["symbol", "series", "ex_date", "fin_per_share", "int_per_share",
                "ordinary_per_share", "spl_per_share", "total_cash_per_share",
                "parse_status", "ambiguous_reason"]
    grouped = raw.groupby(key_cols, sort=True, dropna=False)
    collapsed = grouped.agg(
        duplicate_count=("purpose", "size"),
        purpose=("purpose", lambda s: " | ".join(sorted(set(s)))),
        non_dividend_legs=("non_dividend_legs", "first"),
        labels_found=("labels_found", "first"),
        amount_count=("amount_count", "first"),
    ).reset_index()

    # Same (symbol, series, ex_date) with differing totals: ambiguous.
    day_counts = collapsed.groupby(["symbol", "series", "ex_date"])["total_cash_per_share"].transform(
        lambda s: s.nunique(dropna=True)
    )
    collapsed["ambiguous_same_day"] = (
        (day_counts > 1) & (collapsed["parse_status"] == "parsed")
    )
    collapsed = collapsed.sort_values(
        ["symbol", "series", "ex_date", "total_cash_per_share"], kind="stable"
    ).reset_index(drop=True)
    return collapsed


def attach_prices_and_classify(events: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    """Attach ``P_prev`` / ``F_after`` and apply the NSE 2% rule.

    Pure.  Also derives the NSE-TR bridge amount
    (FIN+INT+ordinary [+ SPL if below 2%]), the Yahoo-comparable
    amount (all cash legs) and the PR divisor adjustment for >=2%
    specials.  Rows without a usable pre-ex bar stay excluded from
    both bridges with ``reason``.
    """
    frame = events.copy()
    frame["prev_bar_date"] = ""
    frame["prev_split_adj_close"] = np.nan
    frame["future_split_factor"] = np.nan
    frame["spl_adj"] = np.nan
    frame["spl_yield"] = np.nan
    frame["is_special_2pct"] = None
    frame["nse_treatment"] = ""
    frame["pr_divisor_factor"] = np.nan
    frame["nse_tr_per_share"] = np.nan
    frame["yahoo_tr_per_share"] = np.nan
    frame["special_excluded_from_TR_per_share"] = np.nan
    frame["included_in_nse_TR"] = False
    frame["included_in_yahoo_bridge"] = False
    frame["reason"] = ""

    bars_sorted = bars.copy()
    bars_sorted["date"] = pd.to_datetime(bars_sorted["date"]).dt.normalize()
    bars_sorted = bars_sorted.sort_values(["symbol", "series", "date"], kind="stable")
    lines = {k: g for k, g in bars_sorted.groupby(["symbol", "series"], sort=False)}

    for idx, ev in frame.iterrows():
        if ev["parse_status"] != "parsed":
            frame.at[idx, "reason"] = ev["ambiguous_reason"] or ev["parse_status"]
            frame.at[idx, "nse_treatment"] = "excluded_ambiguous"
            continue
        if bool(ev.get("ambiguous_same_day", False)):
            frame.at[idx, "reason"] = "ambiguous_same_day_amounts"
            frame.at[idx, "nse_treatment"] = "excluded_ambiguous"
            continue
        look = _lookup_prev_price(lines, ev["symbol"], ev["series"], ev["ex_date"])
        if look["reason"]:
            frame.at[idx, "reason"] = look["reason"]
            frame.at[idx, "nse_treatment"] = "untestable_no_bars"
            continue
        p_prev = look["prev_close"]
        f_after = look["future_split"]
        if not (math.isfinite(p_prev) and p_prev > 0 and math.isfinite(f_after) and f_after > 0):
            frame.at[idx, "reason"] = "invalid_prev_close_or_factor"
            frame.at[idx, "nse_treatment"] = "untestable_no_bars"
            continue
        frame.at[idx, "prev_bar_date"] = look["prev_bar_date"]
        frame.at[idx, "prev_split_adj_close"] = p_prev
        frame.at[idx, "future_split_factor"] = f_after

        cls = classify_special(ev["spl_per_share"], p_prev, f_after)
        frame.at[idx, "is_special_2pct"] = cls["is_special_2pct"]
        frame.at[idx, "nse_treatment"] = cls["nse_treatment"]
        if cls["spl_adj"] is not None:
            frame.at[idx, "spl_adj"] = cls["spl_adj"]
            frame.at[idx, "spl_yield"] = cls["spl_yield"]
        if cls["pr_divisor_factor"] is not None:
            frame.at[idx, "pr_divisor_factor"] = cls["pr_divisor_factor"]

        fin = float(ev["fin_per_share"]) if pd.notna(ev["fin_per_share"]) else 0.0
        intr = float(ev["int_per_share"]) if pd.notna(ev["int_per_share"]) else 0.0
        ordinary = float(ev["ordinary_per_share"]) if pd.notna(ev["ordinary_per_share"]) else 0.0
        spl = float(ev["spl_per_share"]) if pd.notna(ev["spl_per_share"]) else 0.0
        nse_tr = fin + intr + ordinary
        yahoo_tr = fin + intr + ordinary + spl
        excluded = 0.0
        if cls["is_special_2pct"]:
            excluded = spl
        elif spl:
            nse_tr += spl  # below 2%: ordinary treatment
        frame.at[idx, "nse_tr_per_share"] = round(nse_tr, 10)
        frame.at[idx, "yahoo_tr_per_share"] = round(yahoo_tr, 10)
        frame.at[idx, "special_excluded_from_TR_per_share"] = round(excluded, 10)

        # Bridge inclusion: same bar-validity checks as the dividend
        # layer (dividend at least as large as the pre-ex price is
        # not a valid multiplier).  The combined amount is tested
        # once per ex-date; individual legs are not re-tested.
        from nse_pipeline.actions.dividends import dividend_factor, restate_to_split_adj

        def _ok(amount_nominal: float) -> bool:
            d_adj = restate_to_split_adj(amount_nominal, f_after)
            return dividend_factor(p_prev, d_adj) is not None

        if nse_tr > 0 and _ok(nse_tr):
            frame.at[idx, "included_in_nse_TR"] = True
            frame.at[idx, "reason"] = "included_nse_TR_ordinary_legs" + (
                "+special_below_2pct" if spl and not cls["is_special_2pct"] else ""
            )
        else:
            frame.at[idx, "reason"] = "dividend_ge_prev_close" if nse_tr > 0 else "no_ordinary_legs_for_nse_TR"
        if yahoo_tr > 0 and _ok(yahoo_tr):
            frame.at[idx, "included_in_yahoo_bridge"] = True
    return frame


def build_bridge(events: pd.DataFrame, dividend_events: pd.DataFrame | None = None) -> pd.DataFrame:
    """``dividend_events`` update rows (one per ex-date line).

    The ``specials_dividend_bridge.jsonl`` format: ordinary legs
    (FIN+INT+ordinary [+ SPL below 2%]) are new dividend events;
    the Yahoo column carries all cash legs for comparison.
    Same-day overlap with ``dividend_events`` is flagged per row:
    ``identical_amount`` (likely a duplicate announcement already
    carried), ``differing_amount`` (ambiguous -- the two amounts
    must not both be applied without an announcement-level check)
    or ``none``.
    """
    bridge = events[events["parse_status"] == "parsed"].copy()
    if not len(bridge):
        return pd.DataFrame()
    bridge = bridge.sort_values(["symbol", "series", "ex_date"], kind="stable")
    existing: dict[tuple, list[float]] = {}
    if dividend_events is not None and len(dividend_events):
        for _, r in dividend_events.iterrows():
            key = (r["symbol"], r["series"], str(pd.to_datetime(r["ex_date"]).date()))
            try:
                existing.setdefault(key, []).append(float(r["dividend_per_share"]))
            except (TypeError, ValueError):
                pass

    def _overlap(r) -> tuple[str, str]:
        amounts = existing.get((r["symbol"], r["series"], str(pd.to_datetime(r["ex_date"]).date())), [])
        if not amounts:
            return "none", ""
        nse = r["nse_tr_per_share"]
        same = pd.notna(nse) and any(abs(a - float(nse)) < 1e-9 for a in amounts)
        return ("identical_amount" if same else "differing_amount", ",".join(str(a) for a in sorted(amounts)))

    overlap = bridge.apply(_overlap, axis=1)
    bridge["overlap_type"] = [o[0] for o in overlap]
    bridge["same_day_dividend_amount"] = [o[1] for o in overlap]
    return pd.DataFrame({
        "symbol": bridge["symbol"],
        "series": bridge["series"],
        "ex_date": pd.to_datetime(bridge["ex_date"]).dt.date.astype(str),
        "dividend_per_share": bridge["nse_tr_per_share"],
        "yahoo_tr_per_share": bridge["yahoo_tr_per_share"],
        "fin_per_share": bridge["fin_per_share"],
        "int_per_share": bridge["int_per_share"],
        "ordinary_per_share": bridge["ordinary_per_share"],
        "spl_per_share": bridge["spl_per_share"],
        "special_excluded_from_TR_per_share": bridge["special_excluded_from_TR_per_share"],
        "is_special_2pct": bridge["is_special_2pct"],
        "included_in_nse_TR": bridge["included_in_nse_TR"],
        "included_in_yahoo_bridge": bridge["included_in_yahoo_bridge"],
        "reason": bridge["reason"],
        "prev_bar_date": bridge["prev_bar_date"],
        "prev_split_adj_close": bridge["prev_split_adj_close"],
        "purpose": bridge["purpose"],
        "source": "specials_multi_amount",
        "overlap_type": bridge["overlap_type"],
        "same_day_dividend_amount": bridge["same_day_dividend_amount"],
    })


def validate_tcs(data_root: Path, bars: pd.DataFrame, events_with_factors: pd.DataFrame,
                 dividend_events: pd.DataFrame) -> dict:
    """TCS EQ Yahoo isolation: baseline vs NSE bridge vs Yahoo bridge.

    Pure given inputs.  New ex-date cash is combined with any
    existing same-day dividend event (never applied as two separate
    per-day multipliers against the same ``P_prev``).
    """
    from nse_pipeline.actions.dividends import (
        apply_total_return, compare_to_yahoo, compute_factors,
    )

    symbol, series = "TCS", "EQ"
    tcs_bars = bars[(bars["symbol"] == symbol) & (bars["series"] == series)].copy()
    tcs_bars = tcs_bars.sort_values("date", kind="stable")
    yahoo_path = Path(data_root) / "raw" / "yahoo" / f"{symbol}_NS_max_1d.csv"
    if not yahoo_path.exists() or not len(tcs_bars):
        return {"note": "TCS bars or Yahoo csv not found", "symbol": symbol, "series": series}

    existing = dividend_events[
        (dividend_events["symbol"] == symbol)
        & (dividend_events["series"] == series)
        & (dividend_events["included_in_factor"].astype(bool))
    ].copy()

    new_events = events_with_factors[
        (events_with_factors["symbol"] == symbol)
        & (events_with_factors["series"] == series)
        & events_with_factors["included_in_nse_TR"].astype(bool)
    ].copy()

    def _augmented(amount_col: str, include_col: str) -> dict:
        included_new = events_with_factors[
            (events_with_factors["symbol"] == symbol)
            & (events_with_factors["series"] == series)
            & events_with_factors[include_col].astype(bool)
        ]
        combined: dict[str, float] = {}
        purposes: dict[str, list[str]] = {}
        for _, r in existing.iterrows():
            ex = pd.to_datetime(r["ex_date"]).normalize()
            combined[ex] = combined.get(ex, 0.0) + float(r["dividend_per_share"])
            purposes.setdefault(ex, []).append(str(r.get("purpose", "")))
        added = 0
        for _, r in included_new.iterrows():
            ex = pd.to_datetime(r["ex_date"]).normalize()
            amount = float(r[amount_col]) if pd.notna(r[amount_col]) else 0.0
            if amount <= 0:
                continue
            combined[ex] = combined.get(ex, 0.0) + amount
            purposes.setdefault(ex, []).append(str(r.get("purpose", "")))
            added += 1
        frame = pd.DataFrame([
            {
                "symbol": symbol, "series": series, "ex_date": ex, "dividend_per_share": amt,
                "sources": "specials_validation", "duplicate_count": 1,
                "purpose": " | ".join(purposes.get(ex, [])), "action_type": "dividend",
                "ambiguous_same_day": False,
            }
            for ex, amt in sorted(combined.items())
        ])
        wf = compute_factors(tcs_bars, frame)
        series_out = apply_total_return(tcs_bars, wf)
        yahoo = pd.read_csv(yahoo_path)
        yahoo["date"] = pd.to_datetime(yahoo["Date"]).dt.tz_localize(None).dt.normalize()
        comp = compare_to_yahoo(series_out, yahoo)
        return {
            "new_bridge_events_applied": added,
            "isolation": comp["dividend_factor_isolation"],
            "n_common": comp["n_common"],
        }

    def _baseline() -> dict:
        wf = compute_factors(tcs_bars, existing.copy()) if len(existing) else existing.copy()
        if len(wf):
            series_out = apply_total_return(tcs_bars, wf)
            yahoo = pd.read_csv(yahoo_path)
            yahoo["date"] = pd.to_datetime(yahoo["Date"]).dt.tz_localize(None).dt.normalize()
            comp = compare_to_yahoo(series_out, yahoo)
            return {"isolation": comp["dividend_factor_isolation"], "n_common": comp["n_common"]}
        return {"isolation": {"note": "no existing included events"}, "n_common": 0}

    baseline = _baseline()
    nse = _augmented("nse_tr_per_share", "included_in_nse_TR")
    yahoo = _augmented("yahoo_tr_per_share", "included_in_yahoo_bridge")
    return {
        "symbol": symbol,
        "series": series,
        "baseline_no_specials": baseline,
        "nse_TR_bridge": nse,
        "yahoo_bridge": yahoo,
        "note": (
            "dividend_factor_isolation post_2011 ratio = ours growth / Yahoo growth "
            "of (Adj Close / Close). 0.887 is the dividends-layer baseline; the "
            "NSE bridge adds ordinary legs (FIN/INT [+SPL<2%]); the Yahoo bridge "
            "adds every cash leg including >=2% specials."
        ),
    }


def _reason_counts(frame: pd.DataFrame, column: str = "reason") -> dict:
    if not len(frame):
        return {}
    return {str(k): int(v) for k, v in frame[column].value_counts().items()}


def run_specials(data_root: Path, out_root: Path | None = None, report_root: Path | None = None) -> dict:
    """Run the specials layer read-only against host artefacts only."""
    data_root = Path(data_root)
    out_root = Path(out_root) if out_root is not None else data_root / "processed" / "actions_dividends" / "specials"
    report_root = Path(report_root) if report_root is not None else data_root / "reports"
    out_root.mkdir(parents=True, exist_ok=True)
    report_root.mkdir(parents=True, exist_ok=True)

    triage_parquet = data_root / "processed" / "actions_triage" / "triage.parquet"
    if not triage_parquet.exists():
        raise FileNotFoundError(f"Missing {triage_parquet}; run the triage module first")
    triaged = pd.read_parquet(triage_parquet)
    cash = triaged[triaged["bucket"] == "dividend_cash"] if "bucket" in triaged else pd.DataFrame()
    multi = cash[cash["dividend_amount_count"] > 1].copy() if len(cash) else pd.DataFrame()

    dividend_events_path = data_root / "processed" / "actions_dividends" / "dividend_events.jsonl"
    if not dividend_events_path.exists():
        raise FileNotFoundError(f"Missing {dividend_events_path}; run the dividends module first")
    dividend_events = pd.DataFrame(
        [json.loads(line) for line in dividend_events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    )

    adjusted_parquet = data_root / "processed" / "adjusted" / "adjusted_bars.parquet"
    if not adjusted_parquet.exists():
        raise FileNotFoundError(f"Missing {adjusted_parquet}; run the adjustment engine first")
    bars = pd.read_parquet(
        adjusted_parquet, columns=["date", "symbol", "series", "close", "adj_close", "adjustment_factor"]
    )

    events = build_specials_events(multi)
    with_factors = attach_prices_and_classify(events, bars)
    bridge = build_bridge(with_factors, dividend_events)
    special_pr = with_factors[
        (with_factors["is_special_2pct"].astype(object) == True)  # noqa: E712 - nullable bool
    ].copy() if len(with_factors) else with_factors.copy()

    overlap_counts = (
        {str(k): int(v) for k, v in bridge["overlap_type"].value_counts().items()}
        if len(bridge) else {}
    )
    overlap_count = int(len(bridge) - overlap_counts.get("none", 0)) if len(bridge) else 0

    validation = validate_tcs(data_root, bars, with_factors, dividend_events)

    # --- write specials_events.jsonl ---
    events_out = with_factors.copy()
    events_out["ex_date"] = pd.to_datetime(events_out["ex_date"]).dt.date.astype(str)
    events_out["prev_bar_date"] = events_out["prev_bar_date"].fillna("")
    events_out = events_out.sort_values(["symbol", "series", "ex_date", "total_cash_per_share"], kind="stable")
    (out_root / "specials_events.jsonl").write_text(
        "\n".join(json.dumps(rec, default=str, ensure_ascii=False) for rec in events_out.to_dict(orient="records")) + "\n",
        encoding="utf-8",
    )
    if len(bridge):
        (out_root / "specials_dividend_bridge.jsonl").write_text(
            "\n".join(json.dumps(rec, default=str, ensure_ascii=False) for rec in bridge.to_dict(orient="records")) + "\n",
            encoding="utf-8",
        )
    if len(special_pr):
        pr_out = special_pr[[
            "symbol", "series", "ex_date", "purpose", "spl_per_share", "spl_adj",
            "spl_yield", "prev_split_adj_close", "future_split_factor",
            "pr_divisor_factor", "nse_tr_per_share", "yahoo_tr_per_share", "reason",
        ]].copy()
        pr_out["ex_date"] = pd.to_datetime(pr_out["ex_date"]).dt.date.astype(str)
        (out_root / "pr_divisor_adjustments.jsonl").write_text(
            "\n".join(json.dumps(rec, default=str, ensure_ascii=False) for rec in pr_out.to_dict(orient="records")) + "\n",
            encoding="utf-8",
        )

    parsed = with_factors[with_factors["parse_status"] == "parsed"] if len(with_factors) else with_factors
    coverage = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "triage_parquet_multi_rows": int(len(multi)),
            "triage_parquet": "data/processed/actions_triage/triage.parquet",
            "dividend_events": "data/processed/actions_dividends/dividend_events.jsonl",
            "adjusted_bars": "data/processed/adjusted/adjusted_bars.parquet (split/bonus only)",
        },
        "method": {
            "component_parse": "label-in-immediate-context classification (SPL/INT/FIN/ordinary vs redemption/face-value/split legs); ambiguous contexts excluded, never summed or guessed",
            "nse_2pct_rule": "SPL_adj/P_prev >= 0.02 -> special: divisor-adjusted in PR, excluded from TR. Below 2% -> ordinary TR dividend.",
            "special_threshold": SPECIAL_THRESHOLD,
            "spl_adj": "SPL_nominal / future_split_factor; P_prev = split-adj close of last bar strictly before ex_date",
            "rounding": "none applied in factors; jsonl serialisation only",
        },
        "events": {
            "distinct_events": int(len(with_factors)),
            "parsed": int((with_factors["parse_status"] == "parsed").sum()) if len(with_factors) else 0,
            "ambiguous_excluded": int((with_factors["parse_status"] == "ambiguous_excluded").sum()) if len(with_factors) else 0,
            "no_components": int((with_factors["parse_status"] == "no_components").sum()) if len(with_factors) else 0,
            "ambiguous_same_day_rows": int(with_factors["ambiguous_same_day"].sum()) if len(with_factors) else 0,
            "included_in_nse_TR": int(with_factors["included_in_nse_TR"].sum()) if len(with_factors) else 0,
            "included_in_yahoo_bridge": int(with_factors["included_in_yahoo_bridge"].sum()) if len(with_factors) else 0,
            "special_ge_2pct_events": int((with_factors["is_special_2pct"].astype(object) == True).sum()) if len(with_factors) else 0,
            "special_below_2pct_events": int((with_factors["is_special_2pct"].astype(object) == False).sum()) if len(with_factors) else 0,
            "special_untestable_events": int((with_factors["nse_treatment"] == "untestable_no_bars").sum()) if len(with_factors) else 0,
            "excluded_by_reason": _reason_counts(with_factors),
            "sum_fin_rs_per_share": float(parsed["fin_per_share"].sum()) if len(parsed) else 0.0,
            "sum_int_rs_per_share": float(parsed["int_per_share"].sum()) if len(parsed) else 0.0,
            "sum_ordinary_rs_per_share": float(parsed["ordinary_per_share"].sum()) if len(parsed) else 0.0,
            "sum_spl_rs_per_share": float(parsed["spl_per_share"].sum()) if len(parsed) else 0.0,
            "sum_nse_tr_rs_per_share": float(parsed["nse_tr_per_share"].sum()) if len(parsed) else 0.0,
            "sum_excluded_special_rs_per_share": float(parsed["special_excluded_from_TR_per_share"].sum()) if len(parsed) else 0.0,
            "note": "Sums are sums of per-share amounts across event rows (Rs/share), NOT rupees payable.",
        },
        "bridge_overlap": {
            "bridge_events": int(len(bridge)),
            "bridge_ex_dates_already_in_dividend_events": overlap_count,
            "overlap_type_counts": overlap_counts,
            "note": "Same-day overlap is disclosed, not merged away. When both exist, the combined amount is the sum; two separate per-day multipliers against one P_prev would double-count the price. identical_amount rows are likely duplicate announcements already carried by dividend_events; differing_amount rows are ambiguous and need an announcement-level check before application (mirrors the dividend layer's ambiguous_same_day rule).",
        },
        "validation_tcs_yahoo_isolation": validation,
        "limitations": [
            "Single-amount parsed SPL/INTDIV rows already sit in dividend_events as ordinary cash (out of scope here); only the 546 multi-amount triage rows are re-parsed by this layer.",
            "Ambiguous contexts (DIV/SPDV combined labels, unlabelled amounts) are excluded with disclosure; no amount is invented to complete a split.",
            "The 2% test uses the split-adjusted pre-ex close (host adj_close). The ratio is split-invariant, but it is not the announcement-date market price NSE uses operationally; borderline cases near 2% may differ from the official index treatment.",
            "PR divisor adjustments are recorded as magnitudes only (pr_divisor_adjustments.jsonl); adj_close is owned by the split engine and is never rewritten here.",
            "Bars validity (no_bars_for_line / no_pre_ex_bar) excludes events from both bridges exactly as the dividend layer does; the full event stays in specials_events.jsonl.",
        ],
        "files": {
            "specials_events": "data/processed/actions_dividends/specials/specials_events.jsonl",
            "dividend_bridge": "data/processed/actions_dividends/specials/specials_dividend_bridge.jsonl",
            "pr_divisor_adjustments": "data/processed/actions_dividends/specials/pr_divisor_adjustments.jsonl",
            "coverage_json": "data/reports/specials-coverage.json",
            "research_md": "data/reports/specials-research.md",
        },
    }
    (report_root / "specials-coverage.json").write_text(
        json.dumps(coverage, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return coverage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=data_root())
    args = parser.parse_args()
    result = run_specials(args.data)
    iso = result["validation_tcs_yahoo_isolation"]
    print(json.dumps({
        "distinct_events": result["events"]["distinct_events"],
        "parsed": result["events"]["parsed"],
        "included_in_nse_TR": result["events"]["included_in_nse_TR"],
        "special_ge_2pct_events": result["events"]["special_ge_2pct_events"],
        "tcs_baseline_post2011": iso.get("baseline_no_specials", {}).get("isolation", {}).get("post_2011", {}),
        "tcs_nse_post2011": iso.get("nse_TR_bridge", {}).get("isolation", {}).get("post_2011", {}),
        "tcs_yahoo_post2011": iso.get("yahoo_bridge", {}).get("isolation", {}).get("post_2011", {}),
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
