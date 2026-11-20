"""Successor transitions with an explicit fail-closed production boundary.

``SuccessorBacktester.run(..., production=True, price_basis="raw")`` uses
only verified NSE ex-dates and event-specific party/key bindings. It supports
whole raw-share merger/demerger entitlements into a verified listed security.
Raw scheme ratios are never applied to split-adjusted share quantities.
Pending unlisted children, fractional settlements, cash payouts, unresolved
held schemes and dividend/scheme composition stop the run *before marking*,
via ``SchemeSafetyError``. Pending entitlements are separate from holdings,
have no fabricated valuation, and never create spendable cash.

The optional ledger ``execution`` envelope is local to this module (no global
schema change): ``ex_date``, ``ex_date_verified=True``, ``exchange="NSE"``,
``source_url`` (NSE), ``page_ref``, ``verified_by``,
``party_binding_verified=True``, ``source_symbol``, ``source_key``, and for
share swaps ``successor_symbol``, ``successor_key``,
``successor_tradable_date``. Party roles must identify exactly one source and
the named successor; list order is not a binding. Missing listing information
means pending, not tradable. The envelope attests bindings/listing information
for this event, never all past/future uses of a ticker.

Production signal quarantine is causal: it starts at the known event and
covers the complete momentum anchor interval. Held-exposure assessment is
independent of quarantine, including unresolved/undated events. Coverage and
quarantined targets are disclosed in ``quality``; unresolved coverage is not
labelled production-safe merely because a symbol was excluded.

Default ``production=False``, ``classify_entry``, ``event_date_for``, and
``apply_swap`` retain legacy synthetic-fixture semantics, NOT production
readiness. Legacy fractional valuations use the current open/prior close in
the runner, never the future execution-day close. Synthetic SANOFI/RAYMOND
ratios are test overlays, not verified real-world terms. No database/network
access is performed here.
"""
from __future__ import annotations

import bisect
import json
import math
from datetime import date
from pathlib import Path
from nse_pipeline.paths import data_root
from urllib.parse import urlparse

import pandas as pd

from nse_pipeline.backtest.engine import (
    RotationBacktester,
    annual_stats,
    executable_reason,
)

# ---------------------------------------------------------------------------
# Provenance / actionability
# ---------------------------------------------------------------------------

BANNED_PRICE_FIELDS = {
    "price_factor", "share_multiplier", "inferred_ratio",
    "inferred_from_price", "price_discontinuity_ratio",
    "implied_ratio_from_return", "raw_return_used",
}
DATE_PRIORITY = ("ex_date", "record_date", "effective_date",
                 "nclt_order_date", "delisting_date", "board_date")
MERGER_TYPES = {"merger", "scheme_of_arrangement", "restructuring"}
CASH_TYPES = {"cash_out", "liquidation"}


class SchemeSafetyError(ValueError):
    """A production run stopped before unsupported scheme exposure was marked.

    ``assessment`` preserves the held exposure and non-tradable entitlements;
    it must not be converted to a successful return/equity observation.
    """

    def __init__(self, assessment: dict):
        self.assessment = assessment
        self.pending_entitlements = assessment.get("pending_entitlements", [])
        super().__init__("Unsafe scheme exposure: " + "; ".join(
            issue["reason"] for issue in assessment["issues"]))


def validate_execution_entry(entry: dict) -> dict:
    """Validate the *production* execution envelope, returning it unchanged.

    Ledger provenance alone is insufficient. ``execution`` is a local,
    explicitly verified NSE ex-date/party-key binding for this event, not
    a global symbol alias. Board/record/effective dates never execute swaps.
    The legacy ``classify_entry`` remains a synthetic-plumbing helper.
    """
    if not isinstance(entry, dict):
        raise ValueError("execution entry must be an object")
    if not isinstance(entry.get("scheme_id"), str) or not entry["scheme_id"].strip():
        raise ValueError("production scheme_id must be a non-empty string")
    dates = entry.get("dates") or {}
    execution = entry.get("execution") or {}
    if not isinstance(dates, dict) or not isinstance(execution, dict):
        raise ValueError("execution requires explicit verified NSE ex-date")
    ex = _parse_iso(dates.get("ex_date"))
    if (ex is None or execution.get("ex_date_verified") is not True
            or execution.get("exchange") != "NSE"
            or _parse_iso(execution.get("ex_date")) != ex):
        raise ValueError("execution requires explicit verified NSE ex-date")
    host = urlparse(str(execution.get("source_url") or "")).hostname or ""
    if (not (host == "nseindia.com" or host.endswith(".nseindia.com"))
            or not execution.get("page_ref") or not execution.get("verified_by")):
        raise ValueError("NSE ex-date requires exchange citation, page_ref and verified_by")
    disposition, reason = classify_entry(entry)
    if disposition != "actionable":
        raise ValueError(reason)
    if entry.get("terms_verified") is not True or entry.get("status") != "effective":
        raise ValueError("production terms require boolean terms_verified and effective status")
    for field in ("ratio_num", "ratio_den", "cash_per_share"):
        value = entry.get(field)
        if value is not None and (isinstance(value, bool)
                                  or not isinstance(value, (int, float))
                                  or not math.isfinite(value)):
            raise ValueError(f"{field} must be a finite number")
    source_roles = ({"demerged_company"} if entry["scheme_type"] == "demerger"
                    else {"target", "transferor", "dissolved"})
    parties = entry.get("parties") or []
    if not isinstance(parties, list):
        raise ValueError("production parties must be a list")
    sources = {str(p["symbol"]).upper() for p in parties if isinstance(p, dict)
               and p.get("symbol") and str(p.get("role", "")) in source_roles}
    source = str(execution.get("source_symbol") or "").upper()
    if (execution.get("party_binding_verified") is not True
            or sources != {source} or not isinstance(execution.get("source_key"), str)
            or not execution["source_key"].strip()):
        raise ValueError("execution requires a verified unambiguous source party/key binding")
    if entry["scheme_type"] not in CASH_TYPES:
        if execution.get("successor_key") is not None and (
                not isinstance(execution["successor_key"], str) or not execution["successor_key"].strip()):
            raise ValueError("successor_key must be a non-empty string or null for an unlisted entitlement")
        successor = str(entry.get("successor_symbol") or "").upper()
        successor_roles = ({"resulting_company"} if entry["scheme_type"] == "demerger"
                           else {"survivor", "transferee"})
        bound = {str(p["symbol"]).upper() for p in parties if isinstance(p, dict)
                 and p.get("symbol") and str(p.get("role", "")) in successor_roles}
        if (successor == source or successor not in bound
                or str(execution.get("successor_symbol") or "").upper() != successor
                or execution.get("successor_key") == execution.get("source_key")):
            raise ValueError("execution requires a verified distinct successor party binding")
    return execution


def assess_exposure(entries: list[dict], holdings: list[dict] | dict, on: date,
                    *, lookback_start: date | None = None,
                    price_basis: str = "raw", return_mode: str = "price",
                    available_keys: set[str] | None = None,
                    market_symbols: dict[str, str] | None = None,
                    executed_scheme_ids: set[str] | None = None) -> dict:
    """Point-in-time safety assessment, independent of universe exclusions.

    ``holdings`` are real quantities (list of key/symbol/shares records or
    key -> position). No quotes or future prices are used. Invalid entries
    stop *held* exposure at their first known risk date (or immediately if
    undated); this does not execute them on a board/record date. Signal
    quarantine begins at the event, never retroactively for all history.
    A demerger contaminates momentum until the anchor is on/after ex-date.

    Unlisted and fractional entitlements are returned separately, without
    a cash/market value. A caller must stop rather than mark these at zero.
    Dividend/scheme composition and adjusted-share conversion are unsupported.
    """
    on = pd.Timestamp(on).date()
    if lookback_start is not None:
        lookback_start = pd.Timestamp(lookback_start).date()
    held = ([dict(p, key=k) for k, p in holdings.items()]
            if isinstance(holdings, dict) else list(holdings))
    issues, pending, exclusions = [], [], set()
    executed = executed_scheme_ids or set()
    for entry in entries:
        if not isinstance(entry, dict):
            if held:
                issues.append({"scheme_id": "?", "reason": "unbound malformed scheme with held exposure"})
            continue
        if entry.get("status") == "withdrawn":
            continue
        scheme_id = str(entry.get("scheme_id", "?"))
        try:
            execution = validate_execution_entry(entry)
            verified, validation_reason = True, ""
        except (ValueError, TypeError, AttributeError) as exc:
            execution = entry.get("execution") if isinstance(entry.get("execution"), dict) else {}
            verified, validation_reason = False, str(exc)
        raw_parties = entry.get("parties")
        parties = [p for p in raw_parties if isinstance(p, dict)] if isinstance(raw_parties, list) else []
        roles = ({"demerged_company"} if entry.get("scheme_type") == "demerger"
                 else {"target", "transferor", "dissolved"})
        sources = {str(p["symbol"]).upper() for p in parties
                   if p.get("symbol") and str(p.get("role", "")) in roles}
        if not sources:
            sources = {str(p["symbol"]).upper() for p in parties if p.get("symbol")}
        source_key = execution.get("source_key")
        if not isinstance(source_key, str):
            source_key = None
        dates = entry.get("dates") if isinstance(entry.get("dates"), dict) else {}
        event = _parse_iso(dates.get("ex_date"))
        if event is None:
            known = [d for v in dates.values() if (d := _parse_iso(v)) is not None]
            event = min(known) if known else None
        if event is not None and on < event:
            continue
        # A verified binding applies to this security, not every future use
        # of the ticker. Unverified bindings cannot hide a held party.
        affected = [p for p in held if float(p.get("shares", 0)) > 0 and (
            (source_key and p.get("key") == source_key)
            or (not verified and str(p.get("symbol", p.get("key", ""))).upper() in sources)
            or (verified and event == on and available_keys is not None
                and str(p.get("symbol", p.get("key", ""))).upper() in sources)
            or (return_mode == "total_return"
                and str(p.get("symbol", p.get("key", ""))).upper() in sources)
            or (not sources and not source_key))]
        contaminated = event is not None and (
            not verified or entry.get("scheme_type") != "demerger"
            or lookback_start is None or lookback_start < event)
        if contaminated:
            if verified and source_key:
                exclusions.add(source_key)
            else:
                exclusions.update(sources)
                if source_key:
                    exclusions.add(source_key)
        for position in affected:
            # Execution acknowledges only this verified source key, not an
            # unrelated same-ticker holding acquired later at the open.
            if scheme_id in executed and verified and position.get("key") == source_key:
                continue
            reason = validation_reason if not verified else ""
            if verified and position.get("key") != source_key:
                reason = "held source key disagrees with verified event party binding"
            if verified and market_symbols is not None:
                source_symbol = str(execution["source_symbol"]).upper()
                if source_key in market_symbols and str(market_symbols[source_key]).upper() != source_symbol:
                    reason = "source market symbol disagrees with verified event party binding"
                successor_key = execution.get("successor_key")
                if (successor_key in market_symbols and str(market_symbols[successor_key]).upper()
                        != str(execution.get("successor_symbol", "")).upper()):
                    reason = "successor market symbol disagrees with verified event party binding"
            if verified and price_basis != "raw":
                reason = "raw scheme ratio cannot be applied to split-adjusted share counts"
            if verified and return_mode == "total_return":
                reason = "total_return dividend/scheme holding composition is unsupported"
            if (verified and price_basis == "raw" and position.get("key") == source_key
                    and entry["scheme_type"] not in CASH_TYPES):
                n = float(position["shares"]) * entry["ratio_num"] / entry["ratio_den"]
                whole = math.floor(n)
                fraction = n - whole
                tradable = _parse_iso(execution.get("successor_tradable_date"))
                successor_key = execution.get("successor_key")
                if (not successor_key or tradable is None or on < tradable
                        or (available_keys is not None and successor_key not in available_keys)):
                    pending.append({"scheme_id": scheme_id, "source_key": source_key,
                                    "successor_symbol": entry["successor_symbol"],
                                    "successor_key": successor_key, "shares": whole,
                                    "fractional_shares": fraction, "tradable": False,
                                    "cash_credited": 0.0, "market_value": None})
                    reason = reason or "pending unlisted/unavailable successor entitlement has no verified valuation"
                elif fraction > 1e-12:
                    pending.append({"scheme_id": scheme_id, "source_key": source_key,
                                    "successor_symbol": entry["successor_symbol"],
                                    "shares": 0, "fractional_shares": fraction,
                                    "tradable": False, "cash_credited": 0.0, "market_value": None})
                    reason = reason or "fractional entitlement settlement is unsupported; no spendable cash inferred"
                if entry.get("cash_per_share") not in (None, 0):
                    reason = reason or "mixed share/cash scheme settlement is unsupported"
            elif verified and entry["scheme_type"] in CASH_TYPES:
                reason = reason or "cash-out settlement date/payment is unsupported; no immediate cash credit"
            if reason:
                issues.append({"scheme_id": scheme_id, "key": position.get("key"),
                               "shares": position.get("shares"), "reason": reason})
    return {"safe": not issues, "issues": issues,
            "signal_exclusions": sorted(exclusions), "pending_entitlements": pending}


def _parse_iso(value) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def event_date_for(entry: dict) -> date | None:
    """Legacy fixture date fallback; never an execution verification gate."""
    dates = entry.get("dates") or {}
    for key in DATE_PRIORITY:
        parsed = _parse_iso(dates.get(key))
        if parsed is not None:
            return parsed
    return None


def classify_entry(entry: dict) -> tuple[str, str]:
    """Return legacy synthetic ``(disposition, reason)`` for a ledger entry.

    Production callers must use ``validate_execution_entry`` instead.

    Dispositions: ``actionable`` (verified terms present), ``unresolved``
    (excluded from universe around the event), ``ignored_withdrawn``.
    Never raises: malformed entries are unresolved.
    """
    if not isinstance(entry, dict):
        return "unresolved", "entry is not an object"
    status = str(entry.get("status") or "")
    if status == "withdrawn":
        return "ignored_withdrawn", "withdrawn schemes carry no terms"
    for banned in BANNED_PRICE_FIELDS:
        if banned in entry:
            return "unresolved", f"banned price-derived field present: {banned}"
    source = str(entry.get("extraction_source") or "").lower()
    if "price" in source or "raw_return" in source or "discontinuity" in source:
        return "unresolved", "terms must never be inferred from a price move"
    if not entry.get("terms_verified"):
        return "unresolved", "terms_verified is false (PDF not read)"
    if not entry.get("pdf_url") or not entry.get("page_ref") or not entry.get("verified_by"):
        return "unresolved", "verified terms require pdf_url + page_ref + verified_by"
    ratio_num, ratio_den = entry.get("ratio_num"), entry.get("ratio_den")
    if (ratio_num is None) != (ratio_den is None):
        return "unresolved", "ratio_num and ratio_den must be set together"
    if ratio_num is not None and (not isinstance(ratio_num, (int, float))
                                  or not isinstance(ratio_den, (int, float))
                                  or ratio_num <= 0 or ratio_den <= 0):
        return "unresolved", "ratio_num/ratio_den must be positive numbers"
    cash = entry.get("cash_per_share")
    if cash is not None and (not isinstance(cash, (int, float)) or cash < 0):
        return "unresolved", "cash_per_share must be a non-negative number"
    scheme_type = str(entry.get("scheme_type") or "unknown")
    successor = entry.get("successor_symbol")
    if scheme_type == "demerger":
        if ratio_num is None or not successor:
            return "unresolved", "demerger needs verified ratio + successor_symbol"
    elif scheme_type in MERGER_TYPES:
        if ratio_num is None or not successor:
            return "unresolved", "merger needs verified ratio + successor_symbol"
    elif scheme_type in CASH_TYPES:
        if cash is None:
            return "unresolved", "cash-out/liquidation needs verified cash_per_share"
    else:
        return "unresolved", f"unknown scheme_type: {scheme_type!r}"
    if event_date_for(entry) is None:
        return "unresolved", "no usable event date"
    return "actionable", f"verified {scheme_type} terms with citation"


def classify_entries(entries: list[dict]) -> dict:
    """Partition ledger entries into actionable / unresolved / withdrawn."""
    out = {"actionable": [], "unresolved": [], "withdrawn": [], "reasons": {}}
    for entry in entries:
        disposition, reason = classify_entry(entry)
        scheme_id = str(entry.get("scheme_id", "?")) if isinstance(entry, dict) else "?"
        out["reasons"][scheme_id] = reason
        if disposition == "actionable":
            out["actionable"].append(entry)
        elif disposition == "ignored_withdrawn":
            out["withdrawn"].append(entry)
        else:
            out["unresolved"].append(entry)
    return out


def primary_symbol(entry: dict) -> str | None:
    """Source symbol: the first party carrying a symbol (fallbacks: none)."""
    for party in entry.get("parties") or []:
        if isinstance(party, dict) and party.get("symbol"):
            return str(party["symbol"]).upper()
    return None


# ---------------------------------------------------------------------------
# Universe exclusion for unresolved entries
# ---------------------------------------------------------------------------

def exclusion_windows(entries: list[dict], sessions: list[date],
                      buffer_sessions: int = 5) -> dict[date, set[str]]:
    """Legacy fixture exclusion windows; not a held-exposure safety mechanism.

    The window is ``buffer_sessions`` sessions on either side of the event
    date (inclusive), clipped to the session calendar.  Entries with no
    event date contribute nothing (reported as unresolved globally).
    """
    if buffer_sessions < 0:
        raise ValueError("buffer_sessions must be >= 0")
    windows: dict[date, set[str]] = {}
    for entry in entries:
        disposition, _ = classify_entry(entry)
        if disposition != "unresolved":
            continue
        symbol = primary_symbol(entry)
        event = event_date_for(entry)
        if not symbol or event is None:
            continue
        pos = bisect.bisect_left(sessions, event)
        lo = max(0, pos - buffer_sessions)
        hi = min(len(sessions) - 1, pos + buffer_sessions)
        for i in range(lo, hi + 1):
            windows.setdefault(sessions[i], set()).add(symbol)
    return windows


# ---------------------------------------------------------------------------
# Pure swap arithmetic (tested directly; the engine reuses the same rules)
# ---------------------------------------------------------------------------

def apply_swap(n: float, ratio_num: float, ratio_den: float,
               cash_per_share: float | None,
               successor_close: float | None) -> tuple[float, float]:
    """Legacy synthetic arithmetic, NOT production fractional settlement.

    Returns ``(whole_successor_shares, fractional_cash)``.  Fractional
    cash is ``fraction * cash_per_share`` when the scheme states one;
    otherwise ``fraction * successor_close`` (requires a close, else
    ValueError -- cash is never invented without either term).
    """
    if n < 0:
        raise ValueError("share count must be >= 0")
    if ratio_num <= 0 or ratio_den <= 0:
        raise ValueError("ratio must be positive")
    total = n * float(ratio_num) / float(ratio_den)
    whole = float(int(total // 1))
    fraction = total - whole
    if fraction <= 1e-12:
        return whole, 0.0
    price = float(cash_per_share) if cash_per_share is not None else successor_close
    if price is None:
        raise ValueError("fractional cash needs cash_per_share or a successor close")
    if price < 0:
        raise ValueError("fractional cash price must be >= 0")
    return whole, fraction * float(price)


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

class SuccessorBacktester(RotationBacktester):
    """Rotation engine with successor corporate-action transitions.

    Control flow mirrors :class:`RotationBacktester` (and
    ``TotalReturnBacktester``); the additions are (1) pre-open successor
    transitions on actionable events, (2) exclusion of unresolved symbols
    from *filtered* signals and buys, and (3) a corporate-action ledger in
    the result.  With no actionable/unresolved entries the result is
    exactly the base engine (tested).
    """

    def run(self, bars: pd.DataFrame, rebalance_dates: list[date],
            signal_fn, initial_cash: float = 1_000_000.0,
            max_positions: int = 20, debug: bool = False,
             successor_entries: list[dict] | None = None,
            exclusion_buffer_sessions: int = 5, *, production: bool = False,
            price_basis: str | None = None, return_mode: str = "price",
            signal_lookback_sessions: int = 63) -> dict:
        """Run legacy fixtures, or opt into fail-closed production execution.

        ``production=True`` requires an explicit price basis. Only raw-share,
        whole-share, listed-successor transitions are supported; other held
        exposure raises ``SchemeSafetyError`` before EOD marking. Synthetic
        defaults are retained for old fixtures and labelled in the result.
        """
        if production and price_basis not in {"raw", "split_adjusted"}:
            raise ValueError("production execution requires explicit price_basis: raw or split_adjusted")
        if production and return_mode != "price":
            raise ValueError("SuccessorBacktester cannot claim total_return: dividend/scheme composition is unsupported")
        if signal_lookback_sessions < 1:
            raise ValueError("signal_lookback_sessions must be positive")
        entries = list(successor_entries or [])
        if production:
            ids = [e.get("scheme_id") for e in entries if isinstance(e, dict)]
            if len(ids) != len(set(str(scheme_id) for scheme_id in ids)):
                raise ValueError("Duplicate scheme IDs: canonicalize the execution ledger first")
        bars = bars.copy()
        if production and price_basis == "raw" and "raw_open" in bars.columns:
            comparable = bars["open"].notna() & bars["raw_open"].notna()
            delta = (bars.loc[comparable, "open"] - bars.loc[comparable, "raw_open"]).abs()
            if (delta > 1e-10 * bars.loc[comparable, "raw_open"].abs().clip(lower=1)).any():
                raise ValueError("raw price basis disagrees with preserved raw_open")
        bars["date"] = pd.to_datetime(bars["date"]).dt.date
        if bars.duplicated(["date", "key"]).any():
            raise ValueError("Duplicate security/date bars: resolve identity or series ambiguity before execution")
        day_map: dict[date, pd.DataFrame] = {d: g for d, g in bars.groupby("date")}
        sessions = sorted(day_map.keys())
        closes: dict[date, dict[str, float]] = {
            d: dict(zip(g["key"].tolist(), g["close"].tolist())) for d, g in day_map.items()
        }
        # Symbol -> key (ledger speaks symbols; bars trade keys).  First
        # key observed per symbol; synthetic bars use key == symbol.
        symbol_to_key: dict[str, str] = {}
        if not production:
            for r in bars[["symbol", "key"]].drop_duplicates().itertuples(index=False):
                symbol_to_key.setdefault(str(r.symbol).upper(), str(r.key))
        classified = classify_entries(entries) if not production else {}
        if production:
            classified = {"actionable": [], "unresolved": [], "withdrawn": [], "reasons": {}}
            for entry in entries:
                scheme_id = str(entry.get("scheme_id", "?")) if isinstance(entry, dict) else "?"
                if isinstance(entry, dict) and entry.get("status") == "withdrawn":
                    classified["withdrawn"].append(entry)
                    continue
                try:
                    validate_execution_entry(entry)
                except (ValueError, TypeError, AttributeError) as exc:
                    classified["unresolved"].append(entry)
                    classified["reasons"][scheme_id] = str(exc)
                else:
                    classified["actionable"].append(entry)
                    classified["reasons"][scheme_id] = "verified NSE execution binding"
        unresolved_windows = exclusion_windows(
            entries, sessions, exclusion_buffer_sessions) if not production else {}
        signal_quarantines: list[dict] = []

        def is_excluded(symbol: str, on: date) -> bool:
            return symbol.upper() in unresolved_windows.get(on, set())

        def filtered_targets(targets: list, on: date) -> list:
            out = []
            symbols_on = dict(zip(day_map[on]["key"], day_map[on]["symbol"]))
            if production:
                anchor = sessions[max(0, bisect.bisect_left(sessions, on) - signal_lookback_sessions)]
                assessment = assess_exposure(entries, [], on, lookback_start=anchor)
                excluded = set(assessment["signal_exclusions"])
                for k in targets:
                    if k in excluded or str(symbols_on.get(k, k)).upper() in excluded:
                        signal_quarantines.append({"date": on, "key": k,
                                                   "reason": "scheme-contaminated lookback or unresolved/retired source"})
                    else:
                        out.append(k)
                return out
            for k in targets:
                sym = str(symbol_to_key.get(k.upper(), k)).upper()
                # Keys are often ISIN:...; resolve back through the bar
                # for the signal date and fall back to the raw key.
                if k in symbols_on:
                    sym = str(symbols_on[k]).upper()
                if not is_excluded(sym, on):
                    out.append(k)
            return out

        # Actionable buckets: first session on/after the event date.
        actions_on: dict[date, list[dict]] = {}
        for entry in classified["actionable"]:
            event = _parse_iso(entry["dates"]["ex_date"]) if production else event_date_for(entry)
            if production and sessions and sessions[0] <= event <= sessions[-1] and event not in day_map:
                raise ValueError("verified NSE ex-date is absent from the supplied session calendar")
            pos = bisect.bisect_left(sessions, event)
            if pos < len(sessions):
                actions_on.setdefault(sessions[pos], []).append(entry)
        if production:
            for events in actions_on.values():
                sources = [e["execution"]["source_key"] for e in events]
                if (len(sources) != len(set(sources)) or any(
                        e["execution"].get("successor_key") in sources for e in events)):
                    raise ValueError("Same-session overlapping/chained scheme execution is unsupported")

        cash = float(initial_cash)
        positions: dict[str, dict] = {}
        trades: list[dict] = []
        skips: list[dict] = []
        corp_ledger: list[dict] = []
        last_price: dict[str, float] = {}
        executed_scheme_ids: set[str] = set()

        def check_exposure(d, available, symbols):
            assessment = assess_exposure(
                entries, positions, d, price_basis=price_basis or "raw",
                return_mode=return_mode, available_keys=available,
                market_symbols=symbols,
                executed_scheme_ids=executed_scheme_ids)
            if not assessment["safe"]:
                assessment.update({"on": d, "cash": cash,
                                   "holdings": [dict(p, key=k) for k, p in positions.items()]})
                raise SchemeSafetyError(assessment)

        def equity_on(d: date) -> float:
            eq = cash
            for k, p in positions.items():
                eq += p["shares"] * closes.get(d, {}).get(k, last_price.get(k, 0.0))
            return eq

        sess_idx = {s: i for i, s in enumerate(sessions)}
        exec_map: dict[date, date | None] = {}
        for sd in rebalance_dates:
            i = sess_idx.get(sd)
            if i is not None:
                exec_map[sd] = sessions[i + 1] if i + 1 < len(sessions) else None
        pending: dict[date, list[tuple[str, date]]] = {}
        for sd in rebalance_dates:
            ed = exec_map.get(sd)
            if ed is None:
                continue
            try:
                targets = list(signal_fn(self, bars, sd, {"day_map": day_map, "sessions": sessions}))[:max_positions]
            except TypeError:
                targets = list(signal_fn(self, bars, sd))[:max_positions]
            # Unresolved symbols leave the universe at signal time.
            targets = filtered_targets(targets, sd)
            if targets:
                pending.setdefault(ed, []).extend([(k, sd) for k in targets])

        equity_rows: list[dict] = []
        for d in sessions:
            # Retain row dictionaries for only the current execution session.
            lookup = ({row.key: row._asdict() for row in day_map[d].itertuples(index=False)}
                      if d in pending or d in actions_on else {})

            def opening_value(k):
                row = lookup.get(k)
                opn = row.get("open") if row else None
                return float(opn) if opn is not None and pd.notna(opn) and float(opn) > 0 else last_price.get(k, 0.0)

            available = {k for k, row in lookup.items() if pd.notna(row.get("open"))
                         and float(row["open"]) > 0}
            symbols = {k: row.get("symbol", k) for k, row in lookup.items()}
            if production:
                check_exposure(d, available, symbols)
            # 1) Successor transitions on pre-open holdings.
            for entry in actions_on.get(d, []):
                binding = entry["execution"] if production else {}
                src_sym = binding.get("source_symbol") if production else primary_symbol(entry)
                src_key = binding.get("source_key") if production else symbol_to_key.get(src_sym or "", "")
                executed_scheme_ids.add(str(entry.get("scheme_id", "?")))
                scheme_type = str(entry["scheme_type"])
                event = _parse_iso(entry["dates"]["ex_date"]) if production else event_date_for(entry)
                ratio_num = entry.get("ratio_num")
                ratio_den = entry.get("ratio_den")
                cash_ps = entry.get("cash_per_share")
                pos = positions.get(src_key) if src_key else None
                if pos is None or pos.get("shares", 0) <= 0:
                    corp_ledger.append({
                        "date": d, "event_date": event, "scheme_id": entry.get("scheme_id"),
                        "symbol": src_sym, "type": scheme_type, "action": "no_position",
                        "shares_before": 0.0, "successor_shares": 0.0,
                        "cash_credited": 0.0,
                    })
                    continue
                n = float(pos["shares"])
                if scheme_type == "demerger":
                    succ_sym = str(entry["successor_symbol"]).upper()
                    succ_key = binding["successor_key"] if production else symbol_to_key.get(succ_sym, succ_sym)
                    succ_close = opening_value(succ_key) or None
                    if succ_close is None and cash_ps is None:
                        # Fractional terms cannot be settled: hold parent,
                        # record unresolved cash, never invent a price.
                        corp_ledger.append({
                            "date": d, "event_date": event, "scheme_id": entry.get("scheme_id"),
                            "symbol": src_sym, "type": scheme_type, "action": "held_parent_no_successor_price",
                            "shares_before": n, "successor_shares": 0.0, "cash_credited": 0.0,
                        })
                        continue
                    whole, frac_cash = (
                        apply_swap(n, ratio_num, ratio_den, cash_ps, succ_close)
                        if (ratio_num is not None) else (0.0, 0.0))
                    cash += frac_cash
                    if whole > 0:
                        if succ_key in positions:
                            old = positions[succ_key]
                            tot = old["shares"] + whole
                            old["avg_entry"] = (old["shares"] * old["avg_entry"] + whole * (succ_close or old["avg_entry"])) / tot
                            old["shares"] = tot
                            old["last_seen"] = d
                        else:
                            positions[succ_key] = {"shares": whole, "avg_entry": float(succ_close or 0.0),
                                                   "last_price": float(succ_close or 0.0), "last_seen": d,
                                                   "flag": "active", "symbol": succ_sym}
                    corp_ledger.append({
                        "date": d, "event_date": event, "scheme_id": entry.get("scheme_id"),
                        "symbol": src_sym, "type": scheme_type, "action": "demerged_child",
                        "shares_before": n, "successor_symbol": succ_sym,
                        "successor_shares": whole, "cash_credited": frac_cash,
                    })
                elif scheme_type in MERGER_TYPES:
                    succ_sym = str(entry["successor_symbol"]).upper()
                    succ_key = binding["successor_key"] if production else symbol_to_key.get(succ_sym, succ_sym)
                    succ_close = opening_value(succ_key) or None
                    whole, frac_cash = apply_swap(n, ratio_num, ratio_den, cash_ps, succ_close)
                    cash += frac_cash
                    del positions[src_key]
                    if whole > 0:
                        if succ_key in positions:
                            old = positions[succ_key]
                            tot = old["shares"] + whole
                            old["avg_entry"] = (old["shares"] * old["avg_entry"] + whole * (succ_close or old["avg_entry"])) / tot
                            old["shares"] = tot
                            old["last_seen"] = d
                        else:
                            positions[succ_key] = {"shares": whole, "avg_entry": float(succ_close or 0.0),
                                                   "last_price": float(succ_close or 0.0), "last_seen": d,
                                                   "flag": "active", "symbol": succ_sym}
                    corp_ledger.append({
                        "date": d, "event_date": event, "scheme_id": entry.get("scheme_id"),
                        "symbol": src_sym, "type": scheme_type, "action": "merged_into_survivor",
                        "shares_before": n, "successor_symbol": succ_sym,
                        "successor_shares": whole, "cash_credited": frac_cash,
                    })
                elif scheme_type in CASH_TYPES:
                    proceeds = n * float(cash_ps)
                    cash += proceeds
                    del positions[src_key]
                    corp_ledger.append({
                        "date": d, "event_date": event, "scheme_id": entry.get("scheme_id"),
                        "symbol": src_sym, "type": scheme_type, "action": "cashed_out",
                        "shares_before": n, "successor_shares": 0.0,
                        "cash_credited": proceeds,
                    })
            # 2) Executions at open (engine logic).
            if d in pending:
                items = list(dict.fromkeys(pending[d]))
                targets = [k for k, _ in items]
                sig_of = {k: sd for k, sd in items}
                # Belt and braces: a target newly excluded on the
                # execution date is not bought (window may straddle).
                targets = filtered_targets(targets, d)
                log_targets = set(targets)
                for k in list(positions.keys()):
                    if k in log_targets:
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
                            skips.append({"date": d, "key": k, "side": "buy", "reason": "no_bar_at_execution"})
                            continue
                        ok, reason = executable_reason(bar, self.circuit)
                        if not ok:
                            skips.append({"date": d, "key": k, "side": "buy", "reason": reason})
                            continue
                        fill = float(bar["open"])
                        shares = int(min(need, cash) // fill)
                        while shares > 0:
                            notional = shares * fill
                            cost = self.costs.cost_for(notional, "buy")
                            if notional + cost <= cash:
                                break
                            shares -= 1
                        if shares <= 0:
                            skips.append({"date": d, "key": k, "side": "buy", "reason": "insufficient_cash"})
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
                            old["avg_entry"] = (old["shares"] * old["avg_entry"] + shares * fill) / tot
                            old["shares"] = tot
                            old["last_seen"] = d
                            old["flag"] = "active"
                        else:
                            positions[k] = {"shares": shares, "avg_entry": fill,
                                            "last_price": fill, "last_seen": d,
                                            "flag": "active", "symbol": bar.get("symbol", k)}
            if production:
                check_exposure(d, available, symbols)
            # 3) EOD marking.
            for k, px in closes.get(d, {}).items():
                last_price[k] = px
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
        ledger = [{"key": k, "shares": p["shares"], "avg_entry": p["avg_entry"],
                   "last_price": last_price.get(k, p["avg_entry"]),
                   "last_seen": p.get("last_seen"), "flag": p.get("flag", "active")}
                  for k, p in positions.items()]
        stats = annual_stats(equity_df.set_index("date")["equity"]) if not equity_df.empty \
            else {"total_return": 0, "cagr": 0, "ann_vol": 0, "max_drawdown": 0, "n_sessions": 0}
        stats["n_trades"] = int(len(trades_df))
        stats["n_skips"] = int(len(skips_df))
        stats["n_successor_actions"] = len([c for c in corp_ledger if c["action"] != "no_position"])
        stats["final_cash"] = float(cash)
        stats["final_equity"] = float(equity_df["equity"].iloc[-1]) if not equity_df.empty else cash
        return {"equity": equity_df, "trades": trades_df, "skips": skips_df,
                "positions": ledger, "corporate_actions": pd.DataFrame(corp_ledger),
                "classified": classified, "stats": stats,
                "quality": {"execution_mode": "production" if production else "synthetic_legacy",
                            "production_safe": production and not classified["unresolved"],
                            "unresolved_entries": len(classified["unresolved"]),
                            "scheme_coverage_complete": not classified["unresolved"],
                            "signal_quarantines": signal_quarantines,
                            "dividend_scheme_composition_supported": False},
                "config": {"costs": self.costs.__dict__, "universe": self.universe.__dict__,
                           "circuit": self.circuit.__dict__, "production": production,
                           "price_basis": price_basis, "return_mode": "price_only"}}


# ---------------------------------------------------------------------------
# Sample backtests: synthetic + SANOFI/RAYMOND 1:1 overlay
# ---------------------------------------------------------------------------

def _synthetic_bars(dates, prices: dict[str, list[float]]) -> pd.DataFrame:
    rows = []
    for i, d in enumerate(dates):
        for sym, series in prices.items():
            px = float(series[i])
            rows.append({"date": d, "key": sym, "symbol": sym, "series": "EQ",
                         "open": px, "high": px + 1, "low": px - 1, "close": px,
                         "previous_close": px, "volume": 100000,
                         "valid_ohlcv": True})
    return pd.DataFrame(rows)


def _verified(scheme_id, scheme_type, symbol, successor, ratio_num, ratio_den,
              cash, event_date, status="effective") -> dict:
    return {
        "scheme_id": scheme_id, "scheme_type": scheme_type,
        "parties": [{"symbol": symbol, "company_name": symbol, "role": "target"}],
        "dates": {"effective_date": event_date.isoformat()},
        "ratio_num": ratio_num, "ratio_den": ratio_den, "cash_per_share": cash,
        "successor_symbol": successor,
        "pdf_url": "https://nsearchives.nseindia.com/synthetic-verified.pdf",
        "page_ref": "p.42", "status": status,
        "verified_by": "synthetic-manual-reader",
        "extraction_source": "manual PDF reading (synthetic verified row)",
        "terms_verified": True,
    }


def run_synthetic(out_dir: str | Path = data_root() / "processed/backtest_successor") -> dict:
    """Synthetic merger (A->B 1:2 + fractional cash) and demerger proof."""
    from datetime import timedelta
    d0 = date(2024, 1, 2)
    dates = [d0 + timedelta(days=i) for i in range(6)]
    # A trades until the merger session then disappears; B continues.
    prices = {
        "A": [10.0, 10.0, 10.0, 10.0, 10.0, 10.0],
        "B": [20.0, 20.0, 20.0, 20.0, 20.0, 20.0],
        "C": [10.0] * 6,
    }
    bars = _synthetic_bars(dates, prices)
    # Drop A bars on/after the event session (delisting).
    bars = bars[~((bars["symbol"] == "A") & (bars["date"] >= dates[3]))]
    entries = [
        _verified("SYN-A-B", "merger", "A", "B", 1, 2, 4.0, dates[3]),
        # Unverified stub: must be excluded, never swapped.
        {**_verified("SYN-UNV", "merger", "C", "B", 1, 1, None, dates[3]),
         "terms_verified": False, "verified_by": None},
    ]
    engine = SuccessorBacktester()
    sig = lambda e, b, d: ["A", "C"] if d == dates[0] else ["B"]
    res = engine.run(bars, [dates[0]], sig, initial_cash=1_000_000.0,
                     successor_entries=entries, exclusion_buffer_sessions=1)
    out = Path(out_dir) / "synthetic"
    out.mkdir(parents=True, exist_ok=True)
    res["equity"].to_csv(out / "equity_curve.csv", index=False)
    res["trades"].to_csv(out / "trades.csv", index=False)
    res["corporate_actions"].to_csv(out / "corporate_actions.csv", index=False)
    summary = {"sample": "synthetic", "stats": {k: (float(v) if isinstance(v, (int, float)) else v) for k, v in res["stats"].items() if k != "config"},
               "corporate_actions": res["corporate_actions"].to_dict("records"),
               "classified": {k: len(res["classified"][k]) for k in ("actionable", "unresolved", "withdrawn")}}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    return summary


def run_sanofi_raymond_1to1(out_dir: str | Path = data_root() / "processed/backtest_successor") -> dict:
    """SANOFI demerger + RAYMOND merger at explicit 1:1 (synthetic overlay).

    Ledger terms for these filings are unverified (0 rows), so the 1:1
    ratio here is a *test overlay* citing synthetic manual verification --
    not a claim about the filings.  Bars are synthetic flat prices so the
    sample runs without the host DB.
    """
    from datetime import timedelta
    d0 = date(2024, 6, 3)
    dates = [d0 + timedelta(days=i) for i in range(8)]
    prices = {
        # Parent drops 500 -> 400 on the demerger session: 1000 shares
        # x 500 = 1000 x 400 (parent) + 1000 x 100 (child) is continuous.
        "SANOFI": [500.0, 500.0, 500.0, 500.0, 400.0, 400.0, 400.0, 400.0],
        "SANOFI_CO": [100.0] * 8,   # resulting company (synthetic symbol)
        "RAYMOND": [2000.0] * 8,
        "RAYMOND_SURV": [2000.0] * 8,  # survivor (synthetic symbol)
    }
    bars = _synthetic_bars(dates, prices)
    # Delisting legs disappear on/after their events.
    bars = bars[~((bars["symbol"] == "RAYMOND") & (bars["date"] >= dates[5]))]
    entries = [
        _verified("SYN-SANOFI-DEMERGE-1TO1", "demerger", "SANOFI", "SANOFI_CO",
                  1, 1, None, dates[4]),
        _verified("SYN-RAYMOND-MERGE-1TO1", "merger", "RAYMOND", "RAYMOND_SURV",
                  1, 1, None, dates[5]),
    ]
    engine = SuccessorBacktester()
    sig = lambda e, b, d: ["SANOFI", "RAYMOND"] if d in (dates[0], dates[1]) else (
        ["SANOFI", "RAYMOND", "SANOFI_CO", "RAYMOND_SURV"])
    res = engine.run(bars, [dates[0], dates[1], dates[2]], sig,
                     initial_cash=1_000_000.0,
                     successor_entries=entries, exclusion_buffer_sessions=2)
    out = Path(out_dir) / "sanofi_raymond_1to1"
    out.mkdir(parents=True, exist_ok=True)
    res["equity"].to_csv(out / "equity_curve.csv", index=False)
    res["trades"].to_csv(out / "trades.csv", index=False)
    res["corporate_actions"].to_csv(out / "corporate_actions.csv", index=False)
    summary = {"sample": "sanofi_raymond_1to1",
               "overlay_note": "1:1 ratios are synthetic verified test rows; ledger filings remain unverified",
               "stats": {k: (float(v) if isinstance(v, (int, float)) else v) for k, v in res["stats"].items() if k != "config"},
               "corporate_actions": res["corporate_actions"].to_dict("records"),
               "classified": {k: len(res["classified"][k]) for k in ("actionable", "unresolved", "withdrawn")}}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    return summary


def build_coverage(ledger_entries: list[dict]) -> dict:
    """Coverage of the strict execution-entry gate (not synthetic readiness)."""
    actionable, unresolved, withdrawn, reasons = 0, 0, 0, {}
    for entry in ledger_entries:
        scheme_id = str(entry.get("scheme_id", "?")) if isinstance(entry, dict) else "?"
        if isinstance(entry, dict) and entry.get("status") == "withdrawn":
            withdrawn += 1
            reasons[scheme_id] = "withdrawn"
            continue
        try:
            validate_execution_entry(entry)
        except (ValueError, TypeError, AttributeError) as exc:
            unresolved += 1
            reasons[scheme_id] = str(exc)
        else:
            actionable += 1
            reasons[scheme_id] = "verified NSE execution entry; held quantity/listing/basis checked at runtime"
    return {
        "ledger_rows": len(ledger_entries),
        "terms_verified": sum(1 for e in ledger_entries if isinstance(e, dict) and e.get("terms_verified")),
        "actionable": actionable,
        "unresolved": unresolved,
        "withdrawn": withdrawn,
        "actionability_gate": "strict_nse_ex_date_and_event_party_binding",
        "ratios_inferred_from_price": False,
        "by_reason": dict(sorted(reasons.items())),
        "limitations": [
            "SANOFI/RAYMOND 1:1 figures in the report are synthetic verified overlays for plumbing tests, not filing readings.",
            "Verified entry counts do not guarantee executable held exposure: raw share basis, whole shares and a verified available listed successor are required.",
            "Production stops for pending unlisted/fractional entitlements, cash settlement, unresolved held schemes and dividend/scheme composition.",
            "Legacy five-session exclusions and synthetic fractional valuations are not production safety controls.",
        ],
    }


def run_all(out_dir: str | Path = data_root() / "processed/backtest_successor",
            reports_dir: str | Path = data_root() / "reports") -> dict:
    synthetic = run_synthetic(out_dir)
    sample = run_sanofi_raymond_1to1(out_dir)
    ledger_path = data_root() / "processed/ledger/scheme-ledger.jsonl"
    entries: list[dict] = []
    if ledger_path.exists():
        entries = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    coverage = build_coverage(entries)
    coverage["synthetic"] = synthetic
    coverage["sanofi_raymond_1to1"] = sample
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "successor-coverage.json").write_text(
        json.dumps(coverage, indent=2, default=str) + "\n", encoding="utf-8")
    return coverage


if __name__ == "__main__":
    print(json.dumps(run_all(), indent=2, default=str))
