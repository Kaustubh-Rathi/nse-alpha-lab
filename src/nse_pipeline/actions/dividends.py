"""Total-return dividend layer (gross, ex-date reinvestment; no tax).

Reads ``market.duckdb`` read-only plus two host artefacts it does not own:

* ``data/processed/actions_triage/triage.parquet`` -- triaged
  ``needs_review`` rows (``dividend_cash`` single-amount rows are the
  triaged candidates; multi-amount and bare dividends stay excluded).
* ``data/processed/adjusted/adjusted_bars.parquet`` -- verified
  split/bonus-only ``adj_close`` (``adj = raw / cumulative split factor``).
  It is never modified; the total-return series is a *new* column built
  on top of it.

Dividend events
---------------
An event is one distinct ``(symbol, series, ex_date, dividend_per_share)``
cash amount per share, honouring the ex-date:

* **Parsed source** -- ``corporate_actions`` rows with
  ``parse_status='parsed_not_applied' AND dividend_per_share IS NOT NULL``
  (46,122 rows).  Same-(symbol, series, ex-date) groups with an
  *identical* amount are duplicate announcements of one dividend and
  collapse to a single event (``duplicate_count`` records how many raw
  rows merged).  Groups with *differing* amounts on the same ex-date
  (130 groups) are ambiguous -- they may be two components or a
  correction -- and are excluded from the factor with
  ``reason='ambiguous_same_day_amounts'`` rather than summed or picked.
* **Triaged single-amount source** -- ``triage.parquet`` rows with
  ``bucket='dividend_cash' AND dividend_amount_count=1`` (221 rows,
  median Rs 2.45/share, max Rs 30).  They enter as
  ``dividend_per_share_triaged`` with ``source='triaged_single_amount'``.
  The 546 multi-amount ``dividend_cash`` rows (``FIN+INT+SPL`` components)
  and the 7,070 bare dividends without an amount (bucket ``unparseable``,
  reason ``dividend_without_per_share_amount``) are **excluded** and only
  counted in coverage -- summing FIN/INT/SPL or inventing a bare amount
  would fabricate cash.

Total-return factor (Yahoo/CRSP convention, gross)
---------------------------------------------------
For an event with ex-date ``E`` let ``P_prev`` be the split-adjusted
close (``adj_close``) of the last bar *strictly before* ``E`` in the
same (symbol, series) line, and ``F_after`` the ``adjustment_factor``
of the first bar on/after ``E`` (1.0 if there is no such bar).  The
nominal cash ``D`` is restated to the split-adjusted basis by dividing
by the splits that occur *after* ``E``::

    D_adj = D / F_after
    f     = (P_prev - D_adj) / P_prev        # dividend multiplier

This is Yahoo's documented rule ``1 - dividend / prior_close`` (Yahoo
Help, CRSP standard) applied in split-adjusted space so a later bonus
is not double-counted: e.g. a Rs 8 RELIANCE dividend (2011) is Rs 2
after the 2017 and 2024 1:1 bonuses (2 x 2).  A bar on date ``d``
carries the cumulative product of all ``f`` with ``ex_date > d``
(strictly before the ex-date, mirroring ``adjust/factors.py``), and::

    adj_close_total(d) = adj_close(d) * prod_{ex > d} f_ex

Events without a pre-ex bar, with non-positive/non-finite amounts, or
with ``D_adj >= P_prev`` (factor <= 0) are excluded from the factor
with a reason; they stay in ``dividend_events.jsonl`` for audit.

That factor assumes the cash is reinvested **gross at the ex-date**
(NSE Total Return index / CRSP daily convention).  It is *not* a
brokerage cash account: real holders receive cash on the pay date,
often net of TDS (India: dividends taxable in hand since 2020-04-01,
10% TDS above Rs 10,000/company/year for residents), and reinvest at
the then price.  Tax is therefore a separate sensitivity
(``net_factor`` helper is provided but the default series is gross;
see ``dividends-research.md``).  Special dividends (>= 2% of price for
NSE PR adjustment) are *not* special-cased here -- they are treated
like ordinary dividends in this gross layer and flagged in coverage.

Outputs (owned by this module only)
-----------------------------------
* ``data/processed/actions_dividends/dividend_events.jsonl`` -- one row
  per distinct event with amount, provenance, factor inputs and the
  inclusion decision.
* ``data/processed/actions_dividends/total_return_sample.jsonl`` --
  RELIANCE/TCS EQ full total-return series (the full 400MB bar set is
  not duplicated; ``apply_total_return`` rebuilds any line).
* ``data/reports/dividends-coverage.json`` -- machine-readable coverage
  incl. Yahoo comparison.
* ``data/reports/dividends-research.md`` is written by hand from the
  web research (see module docstring for sources); this module only
  guarantees the JSON numbers the report cites.

``market.duckdb`` is opened ``read_only=True`` and never written.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path
from nse_pipeline.paths import data_root
from nse_pipeline.build.portable import open_catalog

import numpy as np
import pandas as pd

EQUITY_SERIES = ("EQ", "BE", "SM", "ST")
VALIDATION_SYMBOLS = ("RELIANCE", "TCS")
VALIDATION_SERIES = "EQ"

# ---------------------------------------------------------------------------
# Explicit-amount parser / registry (bare-dividend recovery).
#
# Triage only recognises ``(RS|RE|INR) <number>`` with no separator between
# currency and number.  The bare universe (7,070 rows) therefore misses
# explicit per-share amounts written as ``RS - 8.50`` (dash separator),
# ``RE .75`` (leading dot), ``RSE``/``R`` (currency abbreviations),
# ``INT DIV 3 PER SHARE`` (no currency, per-share unit) and bare multi-leg
# strings (``DIV-FIN 0.20+SPL 0.15``).  The parser below recovers *those*
# rows only -- a row with no amount, a percentage rate, a fractional-share
# leg or an ambiguous multi-leg split stays excluded (strict).
# Currency normalisation: RS / RE / INR / RSE / R are all rupees (no
# conversion); paise / per-unit distributions are never silently coerced to
# rupees-per-share.
# ---------------------------------------------------------------------------
_CURRENCY_RE = re.compile(
    r"(?:\bINR\b|\bRS\.?|\bRE\.?|\bRSE\b|\bR\b)\s*[-]?\s*(\d+(?:\.\d+)?|\.\d+)",
    re.I,
)
_PER_SHARE_SUFFIX_RE = re.compile(
    r"(\d+(?:\.\d+)?|\.\d+)\s*(?:/\s*[-]?\s*|\bPER\s+SHARE\b|\bPR\s*SH(?:R)?\b|\bPER\s+SH(?:R)?\b|\bPERSH|\bPS\b)",
    re.I,
)
_ORDINAL_RE = re.compile(
    r"\b(?:\d+(?:ST|ND|RD|TH)|FIRST|SECOND|THIRD|FOURTH|II|I)\b", re.I
)
_COMPONENT_LABEL_RE = re.compile(
    r"\b(?:FIN|INT|SPL|DIV|SPCL|INTRM|ITM)\b", re.I
)


def normalize_ex_date(value) -> date | None:
    """Floor any timestamp / datetime / date to the entitlement date.

    The ex-date (not the record-date) is what prices the dividend: the
    last bar strictly before it supplies ``P_prev``.  Timestamps carry no
    time-of-day information here (the DB stores DATE), so any time part
    is dropped -- never rounded to the next day.
    """
    return _to_date(value)


def record_date_status(record_date, ex_date) -> str:
    """Classify ``record_date`` vs ``ex_date`` (both floored to dates)."""
    rec = _to_date(record_date)
    ex = _to_date(ex_date)
    if rec is None or ex is None:
        return "missing"
    if rec < ex:
        return "record_before_ex anomaly"
    if rec == ex:
        return "same_day"
    return "after_ex normal"


def _strip_ordinal_numbers(text: str) -> str:
    return _ORDINAL_RE.sub(" ", str(text or ""))


def parse_explicit_amounts(purpose: str) -> list[dict]:
    """Find explicit per-share rupee amounts in a ``purpose`` string.

    Pure.  Returns ``[{amount, token, span}]`` for currency-prefixed
    amounts (``RS/RE/INR/RSE/R``, optional dash separator, leading-dot
    decimals included) plus currency-less amounts with an explicit
    per-share unit (``PER SHARE`` / ``PR SH`` / ``PER SH`` / ``PS``).
    Percentage rates (``6%``), fractional-share legs (``RE PER 0.125
    SH``) and ordinal numbers (``2ND``) never produce an entry.  Empty
    list means no explicit amount was found (strict exclusion).
    """
    text = str(purpose or "")
    if not text.strip():
        return []
    # Percentage rates are rates, not cash per share.
    if "%" in text:
        stripped = _strip_ordinal_numbers(text)
        if re.search(r"\d+(?:\.\d+)?\s*%", stripped):
            return []
    # Fractional-share legs: "RE PER 0.125 SH" -- the number is a share
    # fraction, not rupees.
    if re.search(r"\bPER\s+\d+(?:\.\d+)?\s*SH\b", text, re.I):
        return []
    found: list[tuple[int, int, float, str]] = []
    for m in _CURRENCY_RE.finditer(text):
        try:
            amount = float(m.group(1))
        except ValueError:
            continue
        if math.isfinite(amount) and amount > 0:
            found.append((m.start(), m.end(), amount, m.group(0)))
    if not found:
        for m in _PER_SHARE_SUFFIX_RE.finditer(text):
            # Skip numbers that are part of a currency amount already hit
            # (avoid double counting ``RS 8 PER SHARE``).
            if any(m.start() >= s and m.end() <= e for s, e, _, _ in found):
                continue
            prefix = text[max(0, m.start() - 8):m.start()]
            if re.search(r"%\s*$", prefix):
                continue
            try:
                amount = float(m.group(1))
            except ValueError:
                continue
            if math.isfinite(amount) and amount > 0:
                found.append((m.start(), m.end(), amount, m.group(0)))
    found.sort()
    return [
        {"amount": amount, "token": token, "span": (s, e)}
        for s, e, amount, token in found
    ]


def _classify_bare_segment(segment: str) -> str | None:
    """Dividend class of one ``+/-`` separated bare segment (or None)."""
    s = str(segment or "").upper()
    if re.search(r"\bSPL(?:DIV|D)?\b|\bSPCL\b|\bSP\b", s):
        return "SPL"
    if re.search(r"\bINT(?:DIV|R)?\b|\bINTRM\b|\bFIN\b", s):
        # FIN wins when both FIN and INT words appear in one segment.
        if "FIN" in s:
            return "FIN"
        return "INT"
    if "FIN" in s:
        return "FIN"
    if re.search(r"\bDIV\b|\bDIVIDEND\b", s):
        return "ORDINARY"
    return None


def parse_bare_dividend(purpose: str) -> dict:
    """Parse one bare-dividend ``purpose`` into a recovery decision.

    Pure.  Returns ``status`` in ``recovered_single`` /
    ``recovered_multi_sum`` / ``excluded_*`` plus ``amount`` (Rs/share,
    ``None`` when excluded) and ``reason`` / ``components`` for audit.
    Multi-leg rows are summed only when every leg classifies to a
    distinct dividend component; otherwise the row is ambiguous and
    excluded -- amounts are never invented to complete a split.
    """
    text = str(purpose or "")
    if not text.strip():
        return {"status": "excluded_no_amount", "amount": None,
                "reason": "empty_purpose", "components": []}
    if "%" in text and re.search(r"\d+(?:\.\d+)?\s*%", text):
        return {"status": "excluded_percentage_rate", "amount": None,
                "reason": "percentage_rate_not_amount", "components": []}
    if re.search(r"\bPER\s+\d+(?:\.\d+)?\s*SH\b", text, re.I):
        return {"status": "excluded_fractional_share", "amount": None,
                "reason": "fractional_share_not_amount", "components": []}
    if re.search(r"\bCONV\b|\bREDM\b", text, re.I):
        return {"status": "excluded_non_dividend", "amount": None,
                "reason": "conversion_or_redemption_not_cash", "components": []}
    explicit = parse_explicit_amounts(text)
    if len(explicit) == 1:
        return {"status": "recovered_single", "amount": float(explicit[0]["amount"]),
                "reason": "", "components": [{"class": "ORDINARY", "amount": float(explicit[0]["amount"])}]}
    # Multi-leg bare strings: split on +/-/& separators, classify each leg.
    # Currency-less labelled legs (``DIV-FIN 0.20+SPL 0.15``) have no
    # currency/per-share explicit amount, so they need the same separable
    # multi-leg path (every leg labelled, duplicate differing amounts
    # ambiguous) rather than the strict no-amount exclusion.
    _numbered_segments = [
        seg for seg in re.split(r"[+&/]", text)
        if re.findall(r"\d+(?:\.\d+)?|\.\d+", _strip_ordinal_numbers(seg))
    ]
    if len(explicit) > 1 or (len(explicit) == 0 and len(_numbered_segments) >= 2):
        segments = re.split(r"[+&/]", text)
        legs: list[dict] = []
        ok = True
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            nums = re.findall(r"\d+(?:\.\d+)?|\.\d+", _strip_ordinal_numbers(seg))
            if not nums:
                continue
            cls = _classify_bare_segment(seg)
            if cls is None:
                ok = False
                break
            try:
                amount = float(nums[-1])
            except ValueError:
                ok = False
                break
            if not (math.isfinite(amount) and amount > 0):
                ok = False
                break
            legs.append({"class": cls, "amount": amount})
        # Duplicate component, differing amounts -> ambiguous.
        by_class: dict[str, list[float]] = {}
        for leg in legs:
            by_class.setdefault(leg["class"], []).append(leg["amount"])
        for amounts in by_class.values():
            if len(set(amounts)) > 1:
                return {"status": "excluded_ambiguous_multi", "amount": None,
                        "reason": "duplicate_component_differing_amounts",
                        "components": legs}
        if ok and legs and all(_COMPONENT_LABEL_RE.search(text) for _ in [0]):
            # Every '+'-separated number was labelled: sum the legs.
            total = sum(leg["amount"] for leg in legs)
            return {"status": "recovered_multi_sum", "amount": float(total),
                    "reason": "", "components": legs}
        return {"status": "excluded_ambiguous_multi", "amount": None,
                "reason": "unlabelled_component_amount", "components": legs}
    # No currency / per-share amount found: strict exclusion, no invention.
    return {"status": "excluded_no_amount", "amount": None,
            "reason": "dividend_without_per_share_amount", "components": []}


def _to_date(value) -> date | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text.lower() in ("nat", "none"):
        return None
    return pd.to_datetime(text).date()


def _valid_amount(value) -> bool:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(amount) and amount > 0


def dividend_factor(prev_split_adj_close: float, dividend_adj: float) -> float | None:
    """Yahoo/CRSP dividend multiplier ``(P_prev - D) / P_prev``.

    Returns ``None`` when the inputs cannot define a valid factor
    (non-positive price, non-positive dividend, or dividend at least
    as large as the price -- factor <= 0 would invert history).
    """
    try:
        price = float(prev_split_adj_close)
        div = float(dividend_adj)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(price) and math.isfinite(div)):
        return None
    if price <= 0 or div <= 0 or div >= price:
        return None
    return (price - div) / price


def net_dividend_factor(prev_split_adj_close: float, dividend_adj: float, tax_rate: float) -> float | None:
    """Dividend multiplier after a flat withholding ``tax_rate``.

    ``tax_rate`` is a sensitivity input (e.g. 0.10 Indian resident TDS,
    0.20 FPI/NRI before treaty), never a claim about a holder's actual
    slab.  Gross series use :func:`dividend_factor` (``tax_rate=0``).
    """
    try:
        rate = float(tax_rate)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(rate) and 0.0 <= rate < 1.0):
        return None
    return dividend_factor(prev_split_adj_close, dividend_adj * (1.0 - rate))


def restate_to_split_adj(dividend_nominal: float, future_split_factor: float) -> float:
    """Restate a nominal per-share cash amount to the split-adjusted basis.

    ``future_split_factor`` is the product of split/bonus multipliers
    with ex-date strictly after the dividend ex-date -- the
    ``adjustment_factor`` of the first bar on/after the ex-date.
    """
    return float(dividend_nominal) / float(future_split_factor)


def _bare_registry(bare: pd.DataFrame | None, event_keys) -> tuple[pd.DataFrame, dict]:
    """Partition bare-dividend rows into recovered events + registry counts.

    Pure.  ``event_keys`` are the ``(symbol, series, ex_date)`` already
    covered by a parsed amount, either as a set or as a dict mapping
    each key to its (single) covered amount.  Bare rows on a covered
    key are duplicate announcements (amount exists elsewhere); when
    the covered amount is known they are returned as duplicate rows
    carrying that amount so :func:`build_events` collapses them into
    the existing event (``duplicate_count`` + joined ``purpose``, so
    both source purposes are preserved honestly -- never canonicalised
    to one of them).  Non-overlapping rows go through
    :func:`parse_bare_dividend` and only explicit amounts become events.
    """
    counts: dict[str, int] = {
        "bare_total": 0,
        "bare_duplicate_announcement_covered": 0,
        "bare_recovered_single": 0,
        "bare_recovered_multi_sum": 0,
        "bare_excluded_no_amount": 0,
        "bare_excluded_percentage_rate": 0,
        "bare_excluded_fractional_share": 0,
        "bare_excluded_non_dividend": 0,
        "bare_excluded_ambiguous_multi": 0,
    }
    if bare is None or not len(bare):
        return pd.DataFrame(columns=["symbol", "series", "ex_date", "record_date",
                                     "dividend_per_share_event", "source",
                                     "purpose_event", "action_type_event"]), counts
    amounts_by_key: dict = dict(event_keys) if isinstance(event_keys, dict) else {}
    key_set = set(amounts_by_key) if amounts_by_key else set(event_keys or set())
    recovered = []
    for _, r in bare.iterrows():
        counts["bare_total"] += 1
        symbol, series = r["symbol"], r["series"]
        ex = _to_date(r["ex_date"])
        rec = _to_date(r.get("record_date"))
        purpose = str(r.get("purpose") or "")
        if (symbol, series, ex) in key_set:
            counts["bare_duplicate_announcement_covered"] += 1
            if (symbol, series, ex) in amounts_by_key:
                recovered.append({
                    "symbol": symbol, "series": series, "ex_date": ex,
                    "record_date": rec,
                    "dividend_per_share_event": float(amounts_by_key[(symbol, series, ex)]),
                    "source": "bare_duplicate_announcement",
                    "purpose_event": purpose,
                    "action_type_event": str(r.get("action_type") or "dividend"),
                })
            continue
        dec = parse_bare_dividend(purpose)
        status = dec["status"]
        if status == "recovered_single":
            counts["bare_recovered_single"] += 1
            source = "bare_recovered_single"
        elif status == "recovered_multi_sum":
            counts["bare_recovered_multi_sum"] += 1
            source = "bare_recovered_multi_sum"
        elif status == "excluded_no_amount":
            counts["bare_excluded_no_amount"] += 1
            continue
        elif status == "excluded_percentage_rate":
            counts["bare_excluded_percentage_rate"] += 1
            continue
        elif status == "excluded_fractional_share":
            counts["bare_excluded_fractional_share"] += 1
            continue
        elif status == "excluded_non_dividend":
            counts["bare_excluded_non_dividend"] += 1
            continue
        else:
            counts["bare_excluded_ambiguous_multi"] += 1
            continue
        if dec["amount"] is None or not _valid_amount(dec["amount"]):
            counts["bare_excluded_no_amount"] += 1
            continue
        recovered.append({
            "symbol": symbol, "series": series, "ex_date": ex,
            "record_date": rec,
            "dividend_per_share_event": float(dec["amount"]),
            "source": source, "purpose_event": purpose,
            "action_type_event": str(r.get("action_type") or "dividend"),
        })
    return pd.DataFrame(recovered), counts


def build_events(parsed: pd.DataFrame, triaged_single: pd.DataFrame | None = None,
                 bare: pd.DataFrame | None = None) -> pd.DataFrame:
    """Merge parsed + triaged + bare-recovered rows into distinct events.

    Pure (no DB).  ``parsed`` needs symbol/series/ex_date/
    dividend_per_share (+purpose/action_type/record_date when present);
    ``triaged_single`` needs symbol/series/ex_date/
    dividend_per_share_triaged.  ``bare`` needs symbol/series/ex_date/
    purpose (+record_date/action_type); only non-overlapping rows with
    an explicit parsed amount become events (see
    :func:`parse_bare_dividend`).  Identical same-day amounts collapse
    (``duplicate_count``); differing same-day amounts are kept but
    flagged ambiguous (never summed, never picked).
    """
    frames: list[pd.DataFrame] = []
    if parsed is not None and len(parsed):
        p = parsed.copy()
        p["dividend_per_share_event"] = p["dividend_per_share"].astype(float)
        p["source"] = "parsed_not_applied"
        p["purpose_event"] = p.get("purpose", "")
        p["action_type_event"] = p.get("action_type", "dividend")
        p["record_date_event"] = p.get("record_date", None)
        frames.append(
            p[["symbol", "series", "ex_date", "record_date_event", "dividend_per_share_event", "source", "purpose_event", "action_type_event"]]
        )
    if triaged_single is not None and len(triaged_single):
        t = triaged_single.copy()
        t["dividend_per_share_event"] = t["dividend_per_share_triaged"].astype(float)
        t["source"] = "triaged_single_amount"
        t["purpose_event"] = t.get("purpose", "")
        t["action_type_event"] = t.get("action_type", "dividend")
        t["record_date_event"] = t.get("record_date", None)
        frames.append(
            t[["symbol", "series", "ex_date", "record_date_event", "dividend_per_share_event", "source", "purpose_event", "action_type_event"]]
        )
    if bare is not None and len(bare):
        # Bare registry needs the parsed/triaged keys first so overlapping
        # announcements aggregate instead of double-counting or excluding.
        # Map each covered key to its amount only when that key carries
        # a single distinct amount, so an overlapping bare duplicate
        # can collapse into that event (amount + both purposes) without
        # guessing which amount it duplicates on a genuinely ambiguous
        # same-day group.
        amounts_by_key_amounts: dict[tuple, set] = {}
        for frame in frames:
            for s, se, e, amt in zip(frame["symbol"], frame["series"], frame["ex_date"], frame["dividend_per_share_event"]):
                d = _to_date(e)
                amounts_by_key_amounts.setdefault((s, se, d), set()).add(float(amt))
        keys = {
            key: next(iter(amounts))
            for key, amounts in amounts_by_key_amounts.items()
            if len(amounts) == 1
        }
        bare_frame, _ = _bare_registry(bare, keys)
        if len(bare_frame):
            frames.append(bare_frame)
    if not frames:
        return pd.DataFrame(
            columns=["symbol", "series", "ex_date", "record_date", "record_date_status",
                     "ex_record_delta_days", "dividend_per_share", "sources",
                     "duplicate_count", "purpose", "action_type", "ambiguous_same_day"]
        )
    raw = pd.concat(frames, ignore_index=True)
    raw["ex_date"] = raw["ex_date"].map(_to_date)
    raw["record_date_event"] = raw["record_date_event"].map(_to_date)
    raw = raw[raw["dividend_per_share_event"].map(_valid_amount)].copy()

    # Group by same (symbol, series, ex_date): identical amounts merge,
    # differing amounts stay ambiguous (never summed, never picked).
    # Bare duplicates on a covered key do not create a second event:
    # they collapse into the existing event via duplicate_count (the
    # amount-bearing announcement supplies the cash).
    rows = []
    for (symbol, series, ex), group in raw.groupby(["symbol", "series", "ex_date"], sort=True):
        amounts = sorted(group["dividend_per_share_event"].unique().tolist())
        sources = sorted(set(group["source"].tolist()))
        purposes = sorted(set(str(x) for x in group["purpose_event"].tolist() if str(x)))
        action_types = sorted(set(str(x) for x in group["action_type_event"].tolist() if str(x)))
        rec_dates = sorted({d.isoformat() for d in group["record_date_event"].tolist() if d is not None})
        record_date = rec_dates[0] if rec_dates else None
        ex_d = _to_date(ex)
        rec_d = _to_date(record_date)
        delta = (rec_d - ex_d).days if (rec_d is not None and ex_d is not None) else None
        if len(amounts) == 1:
            rows.append({
                "symbol": symbol, "series": series, "ex_date": ex,
                "record_date": record_date,
                "record_date_status": record_date_status(rec_d, ex_d),
                "ex_record_delta_days": delta,
                "dividend_per_share": float(amounts[0]),
                "sources": "+".join(sources),
                "duplicate_count": int(len(group)),
                "purpose": " | ".join(purposes),
                "action_type": "+".join(action_types),
                "ambiguous_same_day": False,
            })
        else:
            for amount in amounts:
                sub = group[group["dividend_per_share_event"] == amount]
                sub_recs = sorted({d.isoformat() for d in sub["record_date_event"].tolist() if d is not None})
                sub_rec = sub_recs[0] if sub_recs else None
                sub_rec_d = _to_date(sub_rec)
                sub_delta = (sub_rec_d - ex_d).days if (sub_rec_d is not None and ex_d is not None) else None
                rows.append({
                    "symbol": symbol, "series": series, "ex_date": ex,
                    "record_date": sub_rec,
                    "record_date_status": record_date_status(sub_rec_d, ex_d),
                    "ex_record_delta_days": sub_delta,
                    "dividend_per_share": float(amount),
                    "sources": "+".join(sorted(set(sub["source"].tolist()))),
                    "duplicate_count": int(len(sub)),
                    "purpose": " | ".join(sorted(set(str(x) for x in sub["purpose_event"].tolist() if str(x)))),
                    "action_type": "+".join(sorted(set(str(x) for x in sub["action_type_event"].tolist() if str(x)))),
                    "ambiguous_same_day": True,
                })
    events = pd.DataFrame(rows)
    if len(events):
        events = events.sort_values(["symbol", "series", "ex_date", "dividend_per_share"], kind="stable").reset_index(drop=True)
    return events


def compute_factors(bars: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Attach factor inputs and the inclusion decision to every event.

    Pure.  ``bars`` needs date/symbol/series/adj_close/adjustment_factor
    (the host split-only adjusted bars).  Per (symbol, series) line the
    last bar strictly before the ex-date supplies ``P_prev`` and the
    first bar on/after supplies ``F_after``.
    """
    frame = events.copy()
    frame["ex_date"] = pd.to_datetime(frame["ex_date"]).dt.normalize()
    frame["included_in_factor"] = False
    frame["reason"] = ""
    frame["prev_bar_date"] = None
    frame["prev_split_adj_close"] = np.nan
    frame["future_split_factor"] = np.nan
    frame["dividend_adj"] = np.nan
    frame["dividend_factor"] = np.nan
    if not len(frame) or bars is None or not len(bars):
        frame["reason"] = "no_bars"
        return frame

    bars_sorted = bars.copy()
    bars_sorted["date"] = pd.to_datetime(bars_sorted["date"]).dt.normalize()
    bars_sorted = bars_sorted.sort_values(["symbol", "series", "date"], kind="stable")
    lines: dict[tuple, pd.DataFrame] = {
        key: group for key, group in bars_sorted.groupby(["symbol", "series"], sort=False)
    }
    for idx, event in frame.iterrows():
        key = (event["symbol"], event["series"])
        line = lines.get(key)
        if line is None or not len(line):
            frame.at[idx, "reason"] = "no_bars_for_line"
            continue
        if bool(event.get("ambiguous_same_day", False)):
            frame.at[idx, "reason"] = "ambiguous_same_day_amounts"
            continue
        if not _valid_amount(event["dividend_per_share"]):
            frame.at[idx, "reason"] = "invalid_amount"
            continue
        dates = line["date"].to_numpy(dtype="datetime64[ns]")
        ex = np.datetime64(event["ex_date"])
        pre_idx = int(np.searchsorted(dates, ex, side="left")) - 1  # last date < ex
        post_idx = int(np.searchsorted(dates, ex, side="left"))  # first date >= ex
        if pre_idx < 0:
            frame.at[idx, "reason"] = "no_pre_ex_bar"
            continue
        prev_row = line.iloc[pre_idx]
        p_prev = float(prev_row["adj_close"])
        future_split = float(line.iloc[post_idx]["adjustment_factor"]) if post_idx < len(line) else 1.0
        if not (math.isfinite(p_prev) and p_prev > 0 and math.isfinite(future_split) and future_split > 0):
            frame.at[idx, "reason"] = "invalid_prev_close_or_factor"
            continue
        d_adj = restate_to_split_adj(float(event["dividend_per_share"]), future_split)
        factor = dividend_factor(p_prev, d_adj)
        frame.at[idx, "prev_bar_date"] = prev_row["date"].date().isoformat()
        frame.at[idx, "prev_split_adj_close"] = p_prev
        frame.at[idx, "future_split_factor"] = future_split
        frame.at[idx, "dividend_adj"] = d_adj
        if factor is None:
            frame.at[idx, "reason"] = "dividend_ge_prev_close"
            continue
        frame.at[idx, "dividend_factor"] = factor
        frame.at[idx, "included_in_factor"] = True
        frame.at[idx, "reason"] = "included_gross_ex_date_reinvestment"
    return frame


def cumulative_dividend_factors(dates: np.ndarray, ex_dates: np.ndarray, factors: np.ndarray):
    """Per-bar cumulative dividend factor: prod of ``f`` where ``ex > date``.

    Same strictly-before semantics as the split engine
    (:func:`nse_pipeline.adjust.factors.bar_factors`): a bar dated
    exactly on the ex-date does not carry that dividend's factor.
    """
    if len(ex_dates) == 0:
        return np.ones(len(dates), dtype=np.float64)
    order = np.argsort(ex_dates)
    ordered_ex = ex_dates[order]
    ordered_f = factors[order]
    n = len(ordered_f)
    suffix = np.ones(n + 1, dtype=np.float64)
    for i in range(n - 1, -1, -1):
        suffix[i] = ordered_f[i] * suffix[i + 1]
    idx = np.searchsorted(ordered_ex, dates, side="right")
    return suffix[idx]


def apply_total_return(bars: pd.DataFrame, included_events: pd.DataFrame) -> pd.DataFrame:
    """Return bars plus ``adj_close_total`` (split-only adj x dividend factor).

    Pure.  ``bars`` needs date/symbol/series/adj_close; ``included_events``
    needs symbol/series/ex_date/dividend_factor (only
    ``included_in_factor`` rows are used when that column exists).
    Raw price columns are never touched; no rounding.
    """
    frame = bars.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame["dividend_factor_cum"] = 1.0
    frame["n_future_dividends"] = 0
    if included_events is None or not len(included_events):
        frame["adj_close_total"] = frame["adj_close"].astype(np.float64)
        return frame
    events = included_events.copy()
    if "included_in_factor" in events.columns:
        events = events[events["included_in_factor"].astype(bool)]
    if not len(events):
        frame["adj_close_total"] = frame["adj_close"].astype(np.float64)
        return frame
    events["ex_date"] = pd.to_datetime(events["ex_date"]).dt.normalize()
    by_line: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}
    for (symbol, series), group in events.groupby(["symbol", "series"], sort=False):
        ex = group["ex_date"].to_numpy(dtype="datetime64[ns]")
        fac = group["dividend_factor"].to_numpy(dtype=np.float64)
        by_line[(symbol, series)] = (ex, fac)
    cum = np.ones(len(frame), dtype=np.float64)
    n_future = np.zeros(len(frame), dtype=np.int64)
    line_key = list(zip(frame["symbol"].tolist(), frame["series"].tolist()))
    for line, (ex, fac) in by_line.items():
        mask = np.array([k == line for k in line_key])
        if not mask.any():
            continue
        dates = frame.loc[mask, "date"].to_numpy(dtype="datetime64[ns]")
        order = np.argsort(ex)
        ordered_ex = ex[order]
        cum[mask] = cumulative_dividend_factors(dates, ordered_ex, fac[order])
        idx = np.searchsorted(ordered_ex, dates, side="right")
        n_future[mask] = (len(ex) - idx).astype(np.int64)
    frame["dividend_factor_cum"] = cum
    frame["n_future_dividends"] = n_future
    frame["adj_close_total"] = frame["adj_close"].astype(np.float64) * cum
    return frame


def compare_to_yahoo(our: pd.DataFrame, yahoo: pd.DataFrame) -> dict:
    """Compare our (split-only vs total-return) series with Yahoo Adj Close.

    Pure.  ``our`` needs date/adj_close/adj_close_total; ``yahoo`` needs
    date/Adj Close (and Close when available).  Both series are
    normalised to 1.0 at the first common date so the (large) level
    gap from differing split coverage does not dominate; return
    correlations and CAGR gaps are the primary metrics.
    """
    ours = our.copy()
    theirs = yahoo.copy()
    ours["date"] = pd.to_datetime(ours["date"]).dt.normalize()
    theirs["date"] = pd.to_datetime(theirs["date"]).dt.normalize()
    yahoo_cols = ["date", "Adj Close"] + (["Close"] if "Close" in theirs.columns else [])
    merged = pd.merge(
        ours[["date", "adj_close", "adj_close_total"]],
        theirs[yahoo_cols],
        on="date", how="inner",
    ).sort_values("date", kind="stable").reset_index(drop=True)
    if len(merged) < 3:
        return {"n_common": int(len(merged)), "note": "too few common dates"}
    merged["yahoo_adj"] = merged["Adj Close"].astype(float)

    def _metrics(column: str) -> dict:
        level = merged[column].astype(float)
        norm = level / level.iloc[0] if level.iloc[0] else level
        yahoo_norm = merged["yahoo_adj"] / merged["yahoo_adj"].iloc[0] if merged["yahoo_adj"].iloc[0] else merged["yahoo_adj"]
        ratio = norm / yahoo_norm
        rets = level.pct_change()
        yahoo_rets = merged["yahoo_adj"].pct_change()
        both = pd.concat([rets, yahoo_rets], axis=1).dropna()
        corr = float(both.iloc[:, 0].corr(both.iloc[:, 1])) if len(both) > 2 else None
        days = max((merged["date"].iloc[-1] - merged["date"].iloc[0]).days, 1)
        years = days / 365.25
        cagr = float((level.iloc[-1] / level.iloc[0]) ** (1.0 / years) - 1.0) if level.iloc[0] > 0 else None
        return {
            "final_normalised": float(norm.iloc[-1]),
            "yahoo_final_normalised": float(yahoo_norm.iloc[-1]),
            "final_ratio_ours_over_yahoo": float(ratio.iloc[-1]),
            "median_ratio_ours_over_yahoo": float(ratio.median()),
            "min_ratio_ours_over_yahoo": float(ratio.min()),
            "max_ratio_ours_over_yahoo": float(ratio.max()),
            "daily_return_correlation": corr,
            "cagr": cagr,
        }

    total = _metrics("adj_close_total")
    split_only = _metrics("adj_close")
    # Dividend-factor isolation: (total / split-only) must track Yahoo's
    # (Adj Close / Close) if the dividend layer alone is correct, even
    # when split coverage differs (host has no verified TCS split at
    # all, so level comparison is dominated by that gap, not dividends).
    div_isolation: dict = {"note": "requires Yahoo Close column"}
    if "Close" in merged.columns:
        ours_div = (merged["adj_close_total"] / merged["adj_close"]).astype(float)
        yahoo_div = (merged["yahoo_adj"] / merged["Close"].astype(float))
        ours_div_norm = ours_div / ours_div.iloc[0]
        yahoo_div_norm = yahoo_div / yahoo_div.iloc[0]
        post2011 = merged["date"] >= pd.Timestamp("2011-01-01")
        def _growth(mask) -> dict:
            if mask.sum() < 2:
                return {}
            o = ours_div_norm[mask]
            y = yahoo_div_norm[mask]
            return {
                "ours_growth": float(o.iloc[-1] / o.iloc[0]),
                "yahoo_growth": float(y.iloc[-1] / y.iloc[0]),
                "ratio_ours_over_yahoo": float((o.iloc[-1] / o.iloc[0]) / (y.iloc[-1] / y.iloc[0])),
            }
        div_isolation = {
            "ours_div_factor_first": float(ours_div.iloc[0]),
            "yahoo_div_factor_first": float(yahoo_div.iloc[0]),
            "full_window": _growth(pd.Series(True, index=merged.index)),
            "post_2011": _growth(post2011),
            "correlation_div_factor_norm": float(ours_div_norm.corr(yahoo_div_norm)),
            "note": "Normalised dividend-factor growth; post_2011 isolates the host dividend era (host dividends start 2011-01-03).",
        }
    result = {
        "n_common": int(len(merged)),
        "first_common": merged["date"].iloc[0].date().isoformat(),
        "last_common": merged["date"].iloc[-1].date().isoformat(),
        "total_return": total,
        "split_only": split_only,
        "dividend_factor_isolation": div_isolation,
        "cagr_gap_total_minus_yahoo": (
            total["cagr"] - float((merged["yahoo_adj"].iloc[-1] / merged["yahoo_adj"].iloc[0]) ** (365.25 / max((merged["date"].iloc[-1] - merged["date"].iloc[0]).days, 1)) - 1.0)
            if total["cagr"] is not None else None
        ),
    }
    return result


def _reason_counts(events: pd.DataFrame) -> dict:
    if not len(events):
        return {}
    return {str(k): int(v) for k, v in events["reason"].value_counts().items()}


def run_dividends(data_root: Path, out_root: Path | None = None, report_root: Path | None = None) -> dict:
    """Run the dividend layer read-only against ``data_root/market.duckdb``."""
    import duckdb

    data_root = Path(data_root)
    out_root = Path(out_root) if out_root is not None else data_root / "processed" / "actions_dividends"
    report_root = Path(report_root) if report_root is not None else data_root / "reports"
    out_root.mkdir(parents=True, exist_ok=True)
    report_root.mkdir(parents=True, exist_ok=True)
    db_path = data_root / "market.duckdb"
    if not db_path.exists():
        raise FileNotFoundError(f"Missing {db_path}; build the database first")

    con = open_catalog(database=db_path)
    try:
        parsed = con.execute(
            "SELECT symbol, series, ex_date, record_date, purpose, action_type, "
            "dividend_per_share, parse_status FROM corporate_actions "
            "WHERE parse_status='parsed_not_applied' AND dividend_per_share IS NOT NULL "
            "ORDER BY ex_date, symbol, series, purpose"
        ).fetchdf()
        triage_counts = con.execute(
            "SELECT count(*) FROM corporate_actions WHERE parse_status='needs_review'"
        ).fetchone()[0]
    finally:
        con.close()

    triage_parquet = data_root / "processed" / "actions_triage" / "triage.parquet"
    if not triage_parquet.exists():
        raise FileNotFoundError(f"Missing {triage_parquet}; run the triage module first")
    triaged = pd.read_parquet(triage_parquet)
    cash = triaged[triaged["bucket"] == "dividend_cash"] if "bucket" in triaged else pd.DataFrame()
    single = cash[cash["dividend_amount_count"] == 1] if len(cash) else pd.DataFrame()
    multi = cash[cash["dividend_amount_count"] > 1] if len(cash) else pd.DataFrame()
    bare = triaged[triaged["triage_reason"] == "dividend_without_per_share_amount"] if "triage_reason" in triaged else pd.DataFrame()

    raw_parsed_rows = int(len(parsed))
    raw_single_rows = int(len(single))
    events = build_events(parsed, single, bare)
    events["ex_date"] = pd.to_datetime(events["ex_date"]).dt.date
    # Bare registry counts for honest coverage (duplicates vs recovered
    # vs strict exclusions).  Computed against the pre-bare keys so the
    # duplicate/coverage split is exact, not estimated.
    _keys: set = set()
    for _f in ([parsed, single] if len(single) else [parsed]):
        if _f is not None and len(_f):
            for _s, _se, _e in zip(_f["symbol"], _f["series"], _f["ex_date"]):
                _keys.add((_s, _se, _to_date(_e)))
    _, bare_registry = _bare_registry(bare, _keys) if len(bare) else ({}, {})

    adjusted_parquet = data_root / "processed" / "adjusted" / "adjusted_bars.parquet"
    if not adjusted_parquet.exists():
        raise FileNotFoundError(f"Missing {adjusted_parquet}; run the adjustment engine first")
    adj_bars = pd.read_parquet(
        adjusted_parquet, columns=["date", "symbol", "series", "close", "adj_close", "adjustment_factor"]
    )
    with_factors = compute_factors(adj_bars, events)
    included = with_factors[with_factors["included_in_factor"].astype(bool)].copy()

    # Total-return series: materialise only the validation lines in full
    # plus a bounded sample elsewhere (the full 400MB bar set is not
    # duplicated; apply_total_return() rebuilds any line on demand).
    validation: dict = {}
    validation_frames: list[pd.DataFrame] = []
    for symbol in VALIDATION_SYMBOLS:
        bars = adj_bars[(adj_bars["symbol"] == symbol) & (adj_bars["series"] == VALIDATION_SERIES)].copy()
        bars = bars.sort_values("date", kind="stable")
        series = apply_total_return(bars, included)
        validation_frames.append(
            series[["date", "symbol", "series", "close", "adj_close", "adj_close_total", "dividend_factor_cum", "n_future_dividends"]]
        )
        yahoo_path = data_root / "raw" / "yahoo" / f"{symbol}_NS_max_1d.csv"
        symbol_result: dict = {
            "bars_rows": int(len(bars)),
            "events_total": int((with_factors["symbol"] == symbol).sum()),
            "events_included": int(((with_factors["symbol"] == symbol) & with_factors["included_in_factor"]).sum()),
            "events_excluded_by_reason": _reason_counts(with_factors[with_factors["symbol"] == symbol]),
            "dividend_sum_per_share_included": float(
                included[included["symbol"] == symbol]["dividend_per_share"].sum()
            ) if len(included[included["symbol"] == symbol]) else 0.0,
        }
        if yahoo_path.exists():
            yahoo = pd.read_csv(yahoo_path)
            yahoo["date"] = pd.to_datetime(yahoo["Date"]).dt.tz_localize(None).dt.normalize()
            symbol_result["yahoo"] = compare_to_yahoo(series, yahoo)
            symbol_result["yahoo_source"] = f"data/raw/yahoo/{symbol}_NS_max_1d.csv"
        else:
            symbol_result["yahoo"] = {"note": "yahoo csv not found"}
        validation[symbol] = symbol_result

    # Materialised sample = validation lines in full only.  The full
    # 400MB bar set is not duplicated; apply_total_return() rebuilds
    # any (symbol, series) line on demand from adjusted_bars + events.
    sample = pd.concat(validation_frames, ignore_index=True) if validation_frames else pd.DataFrame()
    sample = sample.sort_values(["symbol", "series", "date"], kind="stable") if len(sample) else sample

    events_out = with_factors.copy()
    events_out["ex_date"] = pd.to_datetime(events_out["ex_date"]).dt.date.astype(str)
    events_out["prev_bar_date"] = events_out["prev_bar_date"].fillna("")
    events_out = events_out.sort_values(["symbol", "series", "ex_date", "dividend_per_share"], kind="stable")
    (out_root / "dividend_events.jsonl").write_text(
        "\n".join(json.dumps(record, default=str, ensure_ascii=False) for record in events_out.to_dict(orient="records")) + "\n",
        encoding="utf-8",
    )
    if len(sample):
        sample_out = sample.copy()
        sample_out["date"] = pd.to_datetime(sample_out["date"]).dt.date.astype(str)
        (out_root / "total_return_sample.jsonl").write_text(
            "\n".join(json.dumps(record, default=str, ensure_ascii=False) for record in sample_out.to_dict(orient="records")) + "\n",
            encoding="utf-8",
        )

    included_by_reason = _reason_counts(with_factors)
    coverage = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_database": "data/market.duckdb (read-only)",
        "inputs": {
            "triage_parquet": "data/processed/actions_triage/triage.parquet",
            "adjusted_bars": "data/processed/adjusted/adjusted_bars.parquet (split/bonus only)",
            "needs_review_rows": int(triage_counts),
        },
        "method": {
            "formula": "adj_close_total = adj_close * prod_{ex_date > bar_date} (P_prev - D_adj)/P_prev; "
                       "D_adj = D_nominal / future_split_factor; P_prev = split-adj close of last bar < ex_date",
            "convention": "gross, ex-date reinvestment (Yahoo/CRSP, NSE TR); pay-date cash and tax are sensitivities, not in the default series",
            "strictly_before_ex_date": True,
            "rounding": "none (float64 end-to-end)",
            "ex_date_semantics": "entitlement cutoff; timestamps floored to date; record_date is post-ex book-closure evidence only (record>=ex normal, record<ex anomaly)",
            "currency_normalisation": "RS/RE/INR/RSE/R are rupees (no conversion); paise/per-unit/% rates never coerced to rupees-per-share",
            "duplicate_aggregation": "same (symbol,series,ex_date) identical amounts collapse into one event with duplicate_count (bare duplicates on a covered key aggregate the same way)",
            "strict_exclusions": "missing/ambiguous/percentage/fractional-share/non-dividend amounts are excluded with a reason; never invented",
        },
        "events": {
            "raw_parsed_rows": raw_parsed_rows,
            "raw_triaged_single_rows": raw_single_rows,
            "raw_triaged_single_median_rs_per_share": float(single["dividend_per_share_triaged"].median()) if raw_single_rows else None,
            "raw_triaged_single_max_rs_per_share": float(single["dividend_per_share_triaged"].max()) if raw_single_rows else None,
            "distinct_events": int(len(with_factors)),
            "included_in_factor": int(with_factors["included_in_factor"].sum()),
            "excluded_from_factor": int((~with_factors["included_in_factor"]).sum()),
            "excluded_by_reason": included_by_reason,
            "included_sum_rs_per_share": float(included["dividend_per_share"].sum()) if len(included) else 0.0,
            "included_by_source": (
                {str(k): int(v) for k, v in included["sources"].value_counts().items()} if len(included) else {}
            ),
            "symbols_included": int(included["symbol"].nunique()) if len(included) else 0,
            "ex_date_range_included": [
                str(included["ex_date"].min().date()) if len(included) else None,
                str(included["ex_date"].max().date()) if len(included) else None,
            ],
            "note": "Sums are sums of per-share amounts across event rows (Rs/share), NOT rupees payable.",
        },
        "excluded_universes": {
            "triaged_multi_amount_rows": int(len(multi)),
            "triaged_multi_note": "FIN/INT/SPL components not summed here; parsed separately by specials.py with the NSE 2% rule (non-overlapping rows only).",
            "bare_dividends_without_amount_rows": int(len(bare)),
            "bare_registry": bare_registry,
            "bare_note": "Bare rows are triaged into duplicate announcements already covered by a parsed amount (aggregated, not excluded), bare-recovered explicit amounts (single + separable multi-leg sums, non-overlapping only) and strict exclusions (no amount, percentage rates, fractional-share legs, conversion/redemption, ambiguous multi-leg). True no-amount rows stay in the split-only series with disclosed sensitivity; no amount is invented.",
            "bare_true_excluded_rows": (
                int(bare_registry.get("bare_excluded_no_amount", 0))
                + int(bare_registry.get("bare_excluded_percentage_rate", 0))
                + int(bare_registry.get("bare_excluded_fractional_share", 0))
                + int(bare_registry.get("bare_excluded_non_dividend", 0))
                + int(bare_registry.get("bare_excluded_ambiguous_multi", 0))
            ) if len(bare) else 0,
            "ambiguous_same_day_groups_rows": int(with_factors["ambiguous_same_day"].sum()) if len(with_factors) else 0,
        },
        "validation_vs_yahoo_adj_close": validation,
        "yahoo_caveats": [
            "Yahoo Adj Close restates dividends per *current* share (post all splits) and uses Yahoo's own split calendar (incl. pre-2011 splits host has no verified multiplier for), so levels differ; normalised returns are compared.",
            "Host dividends start 2011-01-03; Yahoo RELIANCE dividends start 1996 and TCS 2004, so pre-2011 Yahoo dividend contribution is missing here by construction.",
            "Yahoo bulk dividends file for RELIANCE shows restated amounts (e.g. 2011: 1.98 vs nominal Rs 8) -- compare factors, not raw Yahoo dividend amounts.",
            "Host has ZERO verified split/bonus rows for TCS (only a needs_review BONUS+DIV 2018-05-31), so host TCS split-only levels are raw (2004 adj=987.95) vs Yahoo split-adjusted (~123); level comparison for TCS is dominated by that split gap. The dividend_factor_isolation block is the valid dividend-layer test.",
            "TCS post-2011 dividend growth understates Yahoo because TCS multi-amount special dividends (INT+SPL, e.g. 2014 Rs45, 2023 Rs75, 2025 Rs76) are excluded with the multi-amount universe.",
        ],
        "limitations": [
            "Gross only: no TDS/slab tax, no pay-date lag, no reinvestment price slippage, no fees.",
            "Special dividends are treated as ordinary gross dividends (NSE PR would adjust >=2% specials via divisor; here they remain in TR).",
            "Bonus+dividend and distribution-per-unit single-amount triaged rows are included as cash legs; their compound leg (bonus/per-unit terms) is not applied here.",
            "Matching is (symbol, series) only; symbol reuse/ISIN continuity unresolved (inherited from adjusted bars).",
            "Multi-amount triage rows are excluded here (specials.py parses them separately with the NSE 2% rule, non-overlapping only); true no-amount bare dividends remain excluded with disclosed sensitivity: total return understates true TR for affected lines.",
        ],
        "files": {
            "dividend_events": "data/processed/actions_dividends/dividend_events.jsonl",
            "total_return_sample": "data/processed/actions_dividends/total_return_sample.jsonl",
            "coverage_json": "data/reports/dividends-coverage.json",
            "research_md": "data/reports/dividends-research.md",
        },
    }
    (report_root / "dividends-coverage.json").write_text(
        json.dumps(coverage, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return coverage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=data_root())
    args = parser.parse_args()
    result = run_dividends(args.data)
    print(json.dumps({
        "distinct_events": result["events"]["distinct_events"],
        "included_in_factor": result["events"]["included_in_factor"],
        "excluded_from_factor": result["events"]["excluded_from_factor"],
        "validation": {
            symbol: {
                "yahoo_total_corr": info.get("yahoo", {}).get("total_return", {}).get("daily_return_correlation"),
                "yahoo_split_corr": info.get("yahoo", {}).get("split_only", {}).get("daily_return_correlation"),
                "final_ratio_total": info.get("yahoo", {}).get("total_return", {}).get("final_ratio_ours_over_yahoo"),
            }
            for symbol, info in result["validation_vs_yahoo_adj_close"].items()
        },
    }, indent=2))


if __name__ == "__main__":
    main()
