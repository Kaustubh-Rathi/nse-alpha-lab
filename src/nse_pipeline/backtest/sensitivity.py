"""Sensitivity / robustness diagnostic for the 2000-2026 price/volume momentum.

**Incomplete-data diagnostic only.**  Nothing in this module certifies alpha,
an unbiased estimate, a total-return series or a production-ready strategy.
The whole point of the diagnostic is to show how much the *measured* behaviour
of the strategy depends on analyst choices (momentum lookback, portfolio
breadth, and the treatment of unresolved scheme/dividend/identity events)
given only the data that actually exists on disk.

Reuse, not duplication
----------------------
The bounded-memory streaming reader, the scheme/identity/dividend risk auditing
and the readiness gate are imported from :mod:`nse_pipeline.backtest.history`
(private helpers, imported and never modified) so the repository keeps exactly
one implementation of "what counts as an unresolved event".  ``engine.py``
supplies :class:`CostConfig` (buy-only stamp duty) and the symbol-root helper.
No other module is edited by this file.

Bounded memory
--------------
One SQL pass over year-bounded raw bars joined to the split-adjusted parquet,
one session of rows at a time, one shared rolling window of adjusted closes
and one small account per grid variant.  Full-history frames are never built.

Grid and cap
------------
momentum lookback x top-N x unresolved-event policy.  The grid is capped by
``--max-variants`` (default 18, the full 3 x 3 x 2 grid) and by
``--time-budget-seconds`` (default 540, i.e. ~9 minutes) so that a run stays
inside a predictable wall-clock budget.  When a cap bites, the summary records
it under ``grid.cap`` and the affected variants are labelled ``truncated``.

Unknown prices are unknown
--------------------------
A holding that cannot be priced on a session leaves the strict equity series
*unknown* for that session.  It is never turned into cash, never valued at
zero and never silently forward-filled, and it never contributes to the
strict equity figure.

An unpriceable holding contributes **nothing** to any equity figure.  The
diagnostic publishes no marked total for such a session at all - not the
strict equity and not the "last observed close" marking - because publishing
one would mean putting a stale price on a security that cannot be priced.
The marking is published only on sessions where every holding already
carried a current valid close, where it is by construction identical to the
strict equity.  Where it is published it is an assumption; it is reported in
its own column next to - never instead of - the strict equity, and it is
never called a return and never an alpha.

Unpriceable exposure is disclosed, never valued
------------------------------------------------
The equity of a session with an unpriceable holding is unknown, and it
stays unknown.  The *size* of that unknown is nevertheless a fact about
the data, so it is disclosed instead of being valued: for every
unpriceable position the runner reads the position's own last observed
valid close and the session on which it was observed, and publishes the
notional size of the unpriceable book together with how stale that last
observed mark is.

Those exposure figures are deliberately **not** totals.  They are not
equity, not a portfolio value, not a marked total and not a return: they
say how much of the book could not be priced at all and how old the last
observed marks are, so a reader sees the size of the hole instead of a
number that pretends to fill it.  The invariant above is unchanged: an
unpriceable holding contributes nothing to any equity figure, and the
sum of the exposure figures is not the value of the portfolio.
"""
from __future__ import annotations

import argparse
import csv
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict, deque
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
from nse_pipeline.paths import data_root
from nse_pipeline.build.portable import open_catalog
import sys
import time

if __package__ in (None, ""):  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nse_pipeline.audit.readiness import build_readiness
from nse_pipeline.backtest.engine import CostConfig, KeyStabilization

# Private helpers are imported (and never edited) on purpose: the scheme,
# identity and dividend risk auditing must have a single source of truth.
from nse_pipeline.backtest.history import (  # noqa: F401
    _days,
    _date,
    _events,
    _fingerprint,
    _Identity,
    _jsonl,
    _positive,
    _risk_inputs,
)

DEFAULT_DATA = data_root()
DEFAULT_START = "2000-01-03"
DEFAULT_END = "2026-11-06"

DEFAULT_LOOKBACKS = (21, 63, 126)
DEFAULT_TOP_NS = (10, 20, 40)
DEFAULT_POLICIES = ("audit_only", "quarantine_around_events")
DEFAULT_MAX_VARIANTS = 18
DEFAULT_TIME_BUDGET_SECONDS = 540.0

# Signal eligibility is held constant across the grid so that the grid varies
# one thing at a time.  These are the full-history runner's values.
MIN_VALID_OBSERVATIONS = 130
MIN_RAW_CLOSE = 5.0
MIN_VOLUME = 5000.0

# Unresolved-event kinds.  Structural events can change the *number of shares
# or the identity* of a holding; dividend events change *cash only* (the share
# count is unaffected, but the ex-date drop is in the price series while the
# cash is not credited).
STRUCTURAL_KINDS = ("scheme", "identity", "corporate_action")
DIVIDEND_KINDS = ("dividend",)
DEFAULT_STRUCTURAL_QUARANTINE_SESSIONS = 21
DEFAULT_DIVIDEND_BUY_QUARANTINE_SESSIONS = 5

MAX_IMPACT_ROWS_PER_BUCKET = 25

MARKING_ASSUMPTION = (
    "ASSUMPTION ONLY, AND DELIBERATELY NOT PUBLISHED WHILE A HOLDING IS UNPRICED: "
    "a marked total would value a holding that cannot be priced on a session at "
    "the last close actually observed for it (split-consistent). That price may be "
    "stale, may pre-date an uncredited dividend or an unmodelled corporate action, "
    "and may belong to a delisted, suspended or unlisted security. An unpriceable "
    "holding therefore contributes NOTHING to any equity figure: that session's "
    "equity is unknown - never cash, never zero, never a stale price - and no "
    "marked total is published for it either, because publishing one would mean "
    "pricing a security that cannot be priced. A marked total is published only "
    "for a session in which every holding carried a current valid close, where it "
    "is by construction identical to the strict equity. It is not a return, not an "
    "alpha and not an unbiased estimate."
)

EXPOSURE_ASSUMPTION = (
    "ASSUMPTION-DRIVEN EXPOSURE DISCLOSURE ONLY - NOT EQUITY, NOT A PORTFOLIO VALUE, "
    "NOT A MARKED TOTAL, NOT A RETURN AND NOT ALPHA: for every holding that cannot be "
    "priced on a session the runner reads the position's own last observed valid close "
    "and the session on which that close was observed, and publishes the notional size "
    "of the unpriceable book (shares x last observed close), the staleness of that last "
    "observed mark (session date - last observed date, in calendar days) and the count "
    "of unpriceable positions that had no last observed close at all. The last "
    "observed close is the raw close last observed for that position; it is not "
    "restated for corporate actions, so the figure is an order-of-magnitude "
    "disclosure and never a valuation. These figures never enter the strict equity "
    "and never enter the published marked total: an unpriceable holding still "
    "contributes nothing to any equity figure, and the sum of the exposure figures "
    "is not the value of the portfolio."
)

POLICY_DEFINITIONS = {
    "audit_only": (
        "Unresolved scheme/dividend/identity events are recorded in the impact "
        "audit only. They never exclude a symbol from ranking, never block a "
        "buy and never make an equity mark unknown. This is the full-history "
        "runner's behaviour and the baseline of the grid."
    ),
    "quarantine_around_events": (
        "Fail-closed sensitivity around unresolved events. Structural events "
        "(scheme, identity, unapplied/ambiguous corporate action) quarantine the "
        "symbol: it is excluded from ranking while the event falls inside the "
        "lookback window, new buys are refused for "
        f"{DEFAULT_STRUCTURAL_QUARANTINE_SESSIONS} sessions around the event, and an "
        "existing holding is quarantined (not marked, not sold) for the same "
        "window, so the equity of that session is unknown rather than guessed. "
        "Unverified dividends contaminate the momentum window (the ex-date drop "
        "is in the price series but the cash is not credited), so the symbol is "
        "excluded from ranking for the whole lookback window and from new buys "
        f"for {DEFAULT_DIVIDEND_BUY_QUARANTINE_SESSIONS} sessions after the ex-date; "
        "a dividend does not change the share count, so the mark stays known. "
        "Pre-2011 price discontinuities are review-pool items, not proven events, "
        "and stay audit-only in both policies."
    ),
}

LIMITATIONS = [
    "Incomplete-data diagnostic. Never alpha, never unbiased, never a total-return series, never a certification of completeness.",
    "readiness_for_backtesting is evaluated before any trading input is read; this run was allowed to proceed only because the operator passed --allow-incomplete.",
    "The equity series is unknown on every session where a holding cannot be priced. Unknown equity is reported as unknown, never as cash, zero or a stale price.",
    "An unpriceable holding contributes nothing to any equity figure. The 'last observed close' marking is published only on a session where every holding already carried a current valid close, where it equals the strict equity; while any holding is unpriceable no marked total is published at all, so a stale price is never put on a security that cannot be priced.",
    "An unknown held opening mark also freezes new buying on that session (it never releases cash), so a data gap in a holding reduces executed trades as well as measured equity.",
    "The unpriceable-exposure figures (shares x the position's own last observed close, the staleness of that mark and the count of unpriceable positions with no last observed close) are a disclosure of the size and the age of the unpriceable book. They are not equity, not a portfolio value, not a return and not alpha, they never enter any equity figure, and they are never summed across sessions: the published aggregate is the final session's magnitude with the worst-session magnitude alongside it, because a session sum would re-count one stuck holding on every session it stayed unpriceable and would describe nothing.",
    "Unresolved scheme, identity and dividend coverage is inherited from the repository's current artefacts; counts of events are diagnostics, never proof of coverage.",
    "Grid results are sensitivity to analyst choices, not evidence that any variant is better.",
    "Dividend cash is credited only where the event row carries a verified raw per-share amount, verified ex-date, official source and party binding; no such dividend was credited in the current artefacts.",
    "Pre-2011 corporate actions and the 2000-2004 price discontinuities remain unresolved; no factor is inferred from a price move.",
    "Costs are the same stylized constants as the full-history runner (buy-only stamp duty, flat brokerage cap, flat slippage), not an authenticated 2000-2026 tax schedule.",
    "The opening screen is a stylized flat 20% band around the raw previous close, not a historical circuit or auction simulation.",
    "Exclusion windows (21 structural sessions, 5 dividend sessions, full-lookback contamination) are analyst choices chosen for this diagnostic; they are not exchange rules and are not claimed to be correct.",
    "The research universe is date-local EQ with an INE ISIN or no ISIN on that date; it is not official Nifty membership and no membership history is used.",
    "No forced terminal liquidation and no fictitious exit for delisted or suspended holdings.",
    "A single pass over the data serves every variant; a truncated run (time budget) stops all variants at the same session so they stay comparable, but the window is then partial.",
]


class SensitivityReadinessError(RuntimeError):
    """Strict runs refuse incomplete evidence before reading trading inputs."""

    def __init__(self, readiness: dict):
        self.readiness = readiness
        self.blocker_codes = list(readiness.get("blocker_codes", []))
        super().__init__("Sensitivity diagnostic blocked: " + ", ".join(self.blocker_codes))


@dataclass(frozen=True, order=True)
class Variant:
    """One point of the sensitivity grid."""

    lookback: int
    top_n: int
    policy: str

    @property
    def id(self) -> str:
        return f"L{self.lookback}-N{self.top_n}-{self.policy}"


@dataclass
class Account:
    """A single portfolio under one grid variant."""

    variant: Variant
    initial_cash: float
    cash: float = 0.0
    positions: dict = field(default_factory=dict)
    trades: int = 0
    buys: int = 0
    sells: int = 0
    skip_counts: Counter = field(default_factory=Counter)
    sessions: int = 0
    sessions_all_positions_marked: int = 0
    sessions_with_unknown_positions: int = 0
    unknown_position_instances: int = 0
    position_days_marked: int = 0
    position_days_unknown: int = 0
    sessions_with_unknown_opening_mark: int = 0
    unpriced_reasons: Counter = field(default_factory=Counter)
    unpriced_shares_total: int = 0
    # Exposure disclosure for the part of the book that cannot be priced.
    # These are EXPOSURE figures: not equity, not a portfolio value, not a
    # marked total and not a return.  They quantify the size and the age of
    # the unpriceable book and never enter any equity figure.
    # Exposure is a per-session magnitude.  It is NEVER summed across
    # sessions: the same stuck holding is marked unpriceable on every
    # session it stays unpriceable, so a session-sum would count one
    # position thousands of times and would be larger than the initial
    # cash by construction.  The final-session figure is the magnitude of
    # the hole at the end of the window and the maximum is its worst
    # point; both are exposure, not equity and not a portfolio value.
    final_unpriced_exposure_value: float = 0.0
    max_unpriced_exposure_value: float = 0.0
    unpriced_positions_without_last_close: int = 0
    max_unpriced_mark_staleness_days: int = 0
    # Sessions on which no equity total could be published at all, because a
    # holding could not be priced.
    sessions_with_no_total_published: int = 0
    max_unpriced_positions: int = 0
    total_costs: float = 0.0
    final_equity_strict: float | None = None
    # The marked total is published only where every holding was priced at a
    # current valid close; it is never a number that includes an unpriceable
    # holding, so it is None whenever a holding could not be priced.
    final_equity_last_observed_marking: float | None = None
    first_trade: date | None = None
    last_trade: date | None = None
    rebalances: int = 0
    signals_scheduled: int = 0
    year_coverage: dict = field(default_factory=lambda: defaultdict(
        lambda: {"sessions": 0, "sessions_with_unknown_positions": 0,
                 "unknown_position_instances": 0, "unknown_reasons": Counter(),
                 # Per-year exposure disclosure (never an equity figure).
                 # Per-year figures are maxima and final-session values, never
                 # session sums: summing would re-count one stuck holding on
                 # every session it stays unpriceable.
                 "max_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION": 0.0,
                 "final_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION": 0.0,
                 "unpriced_positions_without_last_close": 0,
                 "max_unpriced_mark_staleness_days": 0}))


def build_grid(lookbacks=DEFAULT_LOOKBACKS, top_ns=DEFAULT_TOP_NS,
               policies=DEFAULT_POLICIES, max_variants=DEFAULT_MAX_VARIANTS) -> tuple[list[Variant], list[Variant]]:
    """Deterministic grid plus the variants dropped by the cap.

    The grid is sorted by (lookback, top_n, policy) so that the same request
    always produces the same variant list in the same order, and the cap
    truncates that deterministic order.
    """
    grid = [Variant(int(lb), int(n), str(p))
            for lb in lookbacks for n in top_ns for p in policies]
    grid.sort(key=lambda v: (v.lookback, v.top_n, v.policy))
    if max_variants is not None and max_variants >= 0:
        kept, dropped = grid[:max_variants], grid[max_variants:]
    else:
        kept, dropped = grid, []
    return kept, dropped


class _Simulation:
    """One streaming pass over the data serving every variant at once."""

    def __init__(self, data: Path, first: date, last: date, grid: list[Variant],
                 initial_cash: float, costs: CostConfig, out: Path,
                 structural_quarantine: int, dividend_buy_quarantine: int,
                 time_budget_seconds: float, use_key_stabilization: bool = True):
        self.data = data
        self.first = first
        self.last = last
        self.grid = grid
        self.costs = costs
        self.out = out
        self.structural_quarantine = structural_quarantine
        self.dividend_buy_quarantine = dividend_buy_quarantine
        self.time_budget_seconds = time_budget_seconds
        self.use_key_stabilization = use_key_stabilization
        self.accounts = [Account(variant=v, initial_cash=initial_cash,
                                 cash=float(initial_cash)) for v in grid]
        self.inputs: set[Path] = set()
        self.audit_counts: Counter = Counter()
        self.data_audit_counts: Counter = Counter()
        self.risk_counts: Counter = Counter()
        self.rows_written: Counter = Counter()
        self.truncated = False
        self.stopped_at: date | None = None
        self.sessions_seen = 0
        self.streamed_rows = 0
        self.rebalances = 0
        self.risk_by_symbol: dict[str, list] = defaultdict(list)
        self.contamination: dict[str, list[int]] = defaultdict(list)
        self._struct_starts: dict[str, list[int]] = defaultdict(list)
        self._struct_ends: dict[str, list[int]] = defaultdict(list)
        self._div_starts: dict[str, list[int]] = defaultdict(list)
        self._div_ends: dict[str, list[int]] = defaultdict(list)
        self._seen_impacts: dict[str, set] = defaultdict(set)
        self._impact_rows: Counter = Counter()
        # Audited impact counts per variant, so the two policies can be
        # compared on the audit trail itself and not only on the outcome.
        self.variant_impacts: dict[str, Counter] = defaultdict(Counter)

    # -- helpers ----------------------------------------------------------
    def _impact(self, account: Account | None, on: date, scope: str, kind: str,
                reason: str, key: str = "", symbol: str = "", event_id: str = "") -> None:
        """Record one audit row; identical events are counted once per bucket."""
        bucket = f"{account.variant.id if account else 'data'}:{scope}:{kind}"
        self.audit_counts[bucket] += 1
        if account is not None:
            self.variant_impacts[account.variant.id][f"{scope}:{kind}"] += 1
            token = (event_id or reason, key, scope)
            if token in self._seen_impacts[bucket]:
                return
            self._seen_impacts[bucket].add(token)
        else:
            self.data_audit_counts[f"{scope}:{kind}"] += 1
        if self._impact_rows[bucket] >= MAX_IMPACT_ROWS_PER_BUCKET:
            return
        self._impact_rows[bucket] += 1
        self._impact_writer.writerow({
            "variant_id": account.variant.id if account else "",
            "date": on.isoformat(), "scope": scope, "kind": kind, "key": key,
            "symbol": symbol, "event_id": event_id, "reason": reason})

    def _in_window(self, symbol: str, starts: dict, ends: dict, index: int) -> bool:
        """Is ``index`` inside any window recorded for ``symbol``?"""
        lo = starts.get(symbol)
        if not lo:
            return False
        pos = bisect_right(lo, index) - 1
        if pos < 0:
            return False
        return index < ends[symbol][pos]

    def _contaminated(self, symbol: str, anchor_index: int, signal_index: int) -> bool:
        """Is an unresolved event inside the lookback window ``(anchor, signal]``?"""
        idxs = self.contamination.get(symbol)
        if not idxs:
            return False
        pos = bisect_right(idxs, anchor_index)
        return pos < len(idxs) and idxs[pos] <= signal_index

    def _struct_quarantined(self, symbol: str, index: int) -> bool:
        return self._in_window(symbol, self._struct_starts, self._struct_ends, index)

    def _div_quarantined(self, symbol: str, index: int) -> bool:
        return self._in_window(symbol, self._div_starts, self._div_ends, index)

    def _buy_quarantined(self, symbol: str, index: int) -> bool:
        return self._struct_quarantined(symbol, index) or self._div_quarantined(symbol, index)

    # -- main loop --------------------------------------------------------
    def run(self, db) -> None:
        data = self.data
        inputs = self.inputs
        inputs.add(data / "market.duckdb")
        adjusted = data / "processed/adjusted/adjusted_bars.parquet"
        inputs.add(adjusted)
        inputs.update(p for name in (
            "coverage-audit.json", "pre2011-actions-coverage.json", "adjustment-coverage.json",
            "identity-coverage.json", "dividends-coverage.json", "canonical-schemes.json",
        ) if (p := data / "reports" / name).exists())
        inputs.update(p for p in (data / "processed/prices").glob("year=*/bars.parquet")
                      if self.first.year <= int(p.parent.name.split("=")[-1]) <= self.last.year)

        risks = _risk_inputs(data, inputs)
        identity = _Identity(data, inputs, risks, db=db,
                             stabilization=None if self.use_key_stabilization
                             else KeyStabilization())
        splits, dividends = _events(db, data, inputs, risks, self.first, self.last)

        sessions = [r[0] for r in db.execute(
            "SELECT DISTINCT date FROM bars WHERE date BETWEEN ? AND ? ORDER BY date",
            [self.first, self.last]).fetchall()]
        self.session_index = {on: i for i, on in enumerate(sessions)}
        by_month: dict[tuple[int, int], date] = {}
        for on in sessions:
            by_month[(on.year, on.month)] = on
        month_ends = set(by_month.values())
        next_session = dict(zip(sessions, sessions[1:]))

        for risk in risks:
            self.risk_by_symbol[risk["symbol"]].append(risk)
            self.risk_counts[risk["kind"]] += 1
        self._build_quarantine_index(risks, sessions)

        action_days, dividend_days = defaultdict(list), defaultdict(list)
        for records, buckets in ((splits, action_days), (dividends, dividend_days)):
            for record in records:
                i = bisect_left(sessions, record["date"])
                if i < len(sessions):
                    buckets[sessions[i]].append(record)

        max_lookback = max((v.lookback for v in self.grid), default=63)
        history: deque = deque(maxlen=max_lookback + 1)
        counts: Counter = Counter()
        last_symbol_key: dict[str, str] = {}
        reported_stabilized: set[str] = set()
        pending: dict[str, tuple] = {}
        ranks: dict[tuple, list[str]] = {}

        days = iter(_days(db, adjusted, self.first, self.last))
        item = next(days, None)
        began = time.monotonic()
        for on in sessions:
            rows: list = []
            if item is not None and item[0] == on:
                rows = item[1]
                item = next(days, None)
            self.streamed_rows += len(rows)
            self.sessions_seen += 1

            grouped: dict[str, list] = defaultdict(list)
            for row in rows:
                grouped[identity.key(row[1], row[3], on)].append(row)
            day: dict[str, tuple] = {}
            for key, candidates in grouped.items():
                if len(candidates) != 1:
                    self._impact(None, on, "data", "identity",
                                 "duplicate_security_session_quarantined", key,
                                 candidates[0][1], f"duplicate:{on}:{key}")
                    continue
                row = candidates[0]
                day[key] = row
                previous = last_symbol_key.get(row[1])
                if previous and previous != key:
                    self._impact(None, on, "data", "identity",
                                 "observed_key_change_no_automatic_holding_transfer", key,
                                 row[1], f"keychange:{row[1]}:{previous}:{key}")
                    for account in self.accounts:
                        position = account.positions.get(previous)
                        if position is not None:
                            position["blocked"] = True
                            self._impact(account, on, "holding", "identity",
                                         "held_identity_change_requires_verified_binding",
                                         previous, row[1], f"keychange:{row[1]}:{previous}:{key}")
                last_symbol_key[row[1]] = key
                if (row[1] not in reported_stabilized
                        and identity.stabilization.key_for(row[1])):
                    # Audited once per symbol: this key is the ISIN that the
                    # symbol's own bars prove, not a guess.
                    reported_stabilized.add(row[1])
                    self._impact(None, on, "data", "identity",
                                 "symbol_key_stabilized_to_the_single_isin_proved_by_its_own_bars",
                                 key, row[1], f"stabilized:{row[1]}")
                for account in self.accounts:
                    position = account.positions.get(key)
                    if position is not None:
                        position["symbol"] = row[1]

            index = self.session_index[on]
            for account in self.accounts:
                self._apply_actions(account, on, day, action_days, dividend_days, index)
                self._audit_holdings(account, on, index)
                if account.variant.id in pending:
                    signal_date, targets = pending.pop(account.variant.id)
                    self._execute(account, on, day, signal_date, targets, index)
                account.sessions += 1

            closes: dict[str, float] = {}
            for key, row in day.items():
                if row[8] and _positive(row[5]) and _positive(row[7]):
                    counts[key] += 1
                    if _positive(row[9]):
                        closes[key] = row[9]
                    else:
                        self._impact(None, on, "data", "signal_price",
                                     "missing_split_adjusted_close_no_raw_fallback", key,
                                     row[1], f"adjusted:{on}:{key}")
            history.append((on, closes))

            for account in self.accounts:
                self._mark(account, on, day, index)

            if on in month_ends:
                self.rebalances += 1
                # The ranking cache is per signal date: lookback x policy only.
                ranks.clear()
                for account in self.accounts:
                    variant = account.variant
                    cache_key = (variant.lookback, variant.policy)
                    if cache_key not in ranks:
                        ranks[cache_key] = self._rank(on, history, day, closes, counts,
                                                      variant.lookback, variant.policy)
                    targets = ranks[cache_key][:variant.top_n]
                    account.rebalances += 1
                    if targets:
                        account.signals_scheduled += 1
                    if on in next_session:
                        pending[variant.id] = (on, targets)
                if time.monotonic() - began > self.time_budget_seconds:
                    self.truncated = True
                    self.stopped_at = on
                    break

    # -- per-variant steps ------------------------------------------------
    def _apply_actions(self, account: Account, on: date, day: dict,
                       action_days: dict, dividend_days: dict, index: int) -> None:
        for event in action_days.get(on, []):
            for key, position in account.positions.items():
                row = day.get(key)
                symbol = row[1] if row else position["symbol"]
                if symbol != event["symbol"] or position["series"] != event["series"]:
                    continue
                before, multiplier = position["shares"], event["multiplier"]
                after = before * multiplier
                if not math.isclose(after, round(after), abs_tol=1e-8):
                    position["blocked"] = True
                    self._impact(account, on, "holding", "corporate_action",
                                 "fractional_split_entitlement_unsupported", key, symbol,
                                 f"fraction:{event['symbol']}:{event['date']}")
                    continue
                position["shares"] = round(after)
                if _positive(position.get("reference_close")):
                    position["reference_close"] /= multiplier
        for event in dividend_days.get(on, []):
            for key, position in account.positions.items():
                if position["symbol"] != event["symbol"] or position["series"] != event["series"]:
                    continue
                compound = any(e["symbol"] == event["symbol"] and e["series"] == event["series"]
                               for e in action_days.get(on, []))
                bound = not event["isin"] or key == "ISIN:" + str(event["isin"])
                if not event["safe"] or not bound or compound or position.get("blocked"):
                    self._impact(account, on, "holding", "dividend",
                                 "dividend_not_credited_unsafe_or_compound_binding", key,
                                 event["symbol"], f"dividend_cash:{event['symbol']}:{event['date']}")
                    continue
                amount = position["shares"] * event["amount"]
                account.cash += amount
                self._impact(account, on, "holding", "dividend",
                             "gross_dividend_cash_credited", key, event["symbol"],
                             f"dividend_cash:{event['symbol']}:{event['date']}")

    def _audit_holdings(self, account: Account, on: date, index: int) -> None:
        for key, position in account.positions.items():
            symbol = position["symbol"]
            for risk in self.risk_by_symbol.get(symbol, []):
                event_date = risk["date"]
                if event_date is not None and event_date > on:
                    continue
                self._impact(account, on, "holding", risk["kind"], risk["reason"], key,
                             symbol, risk["id"])

    def _execute(self, account: Account, on: date, day: dict, signal_date: date,
                 targets: list[str], index: int) -> None:
        positions = account.positions
        # Only today's raw opens are used.  An unknown held opening mark
        # prevents buys; it never releases cash.
        opening_known = all(_positive(day.get(k, (None,) * 5)[4]) and not p.get("blocked")
                            for k, p in positions.items())
        opening_equity = (account.cash + sum(p["shares"] * day[k][4] for k, p in positions.items())
                          if opening_known else None)
        if not opening_known:
            # An unknown held opening mark freezes new buying for the session.
            account.sessions_with_unknown_opening_mark += 1
        per = opening_equity / len(targets) if opening_equity is not None and targets else 0.0
        for key, position in list(positions.items()):
            row = day.get(key)
            wanted = (math.floor(per / row[4])
                      if key in targets and per and row and _positive(row[4])
                      else (position["shares"] if key in targets else 0))
            sell = position["shares"] - wanted
            if sell <= 0:
                continue
            if position.get("blocked"):
                account.skip_counts["sell_position_blocked"] += 1
                self._impact(account, on, "order", "execution",
                             "sell_not_executable_no_fictitious_exit", key,
                             position["symbol"], f"sell:{on}:{key}")
                continue
            if (account.variant.policy == "quarantine_around_events"
                    and self._struct_quarantined(position["symbol"], index)):
                account.skip_counts["sell_quarantined_unresolved_event"] += 1
                self._impact(account, on, "order", "execution",
                             "sell_quarantined_unresolved_event_no_fictitious_exit", key,
                             position["symbol"], f"sell:{on}:{key}")
                continue
            if not self._tradable(row):
                account.skip_counts["sell_not_executable"] += 1
                self._impact(account, on, "order", "execution",
                             "sell_not_executable_no_fictitious_exit", key,
                             position["symbol"], f"sell:{on}:{key}")
                continue
            self._trade(account, on, signal_date, key, row, "sell", sell)
            position["shares"] -= sell
            if not position["shares"]:
                del positions[key]
        for key in targets:
            row = day.get(key)
            if not opening_known:
                account.skip_counts["buy_unknown_held_opening_value"] += 1
                self._impact(account, on, "order", "execution",
                             "buy_unknown_held_opening_value", key,
                             row[1] if row else key, f"buy:{on}:{key}")
                continue
            if not self._tradable(row):
                account.skip_counts["buy_not_executable"] += 1
                self._impact(account, on, "order", "execution",
                             "buy_missing_open_or_not_executable", key,
                             row[1] if row else key, f"buy:{on}:{key}")
                continue
            if positions.get(key, {}).get("blocked"):
                account.skip_counts["buy_position_blocked"] += 1
                self._impact(account, on, "order", "execution",
                             "buy_position_blocked", key, row[1], f"buy:{on}:{key}")
                continue
            if (account.variant.policy == "quarantine_around_events"
                    and self._buy_quarantined(row[1], index)):
                account.skip_counts["buy_quarantined_unresolved_event"] += 1
                self._impact(account, on, "order", "execution",
                             "buy_quarantined_unresolved_event", key, row[1],
                             f"buy:{on}:{key}")
                continue
            held = positions.get(key, {}).get("shares", 0)
            budget = min(account.cash, max(0.0, per - held * row[4]))
            lo, hi = 0, max(0, math.floor(budget / row[4]))
            while lo < hi:
                mid = (lo + hi + 1) // 2
                notional = mid * row[4]
                if notional + self.costs.cost_for(notional, "buy") <= budget:
                    lo = mid
                else:
                    hi = mid - 1
            if lo:
                self._trade(account, on, signal_date, key, row, "buy", lo)
                if key in positions:
                    positions[key]["shares"] += lo
                else:
                    positions[key] = {"symbol": row[1], "series": row[2], "shares": lo,
                                      "reference_close": row[4], "last_close": None,
                                      "last_date": None, "blocked": False}
        if account.cash < -1e-6:
            raise ArithmeticError("Cash conservation failure: negative cash")

    def _trade(self, account: Account, on: date, signal_date: date, key: str,
               row: tuple, side: str, shares: int) -> None:
        notional = shares * row[4]
        cost = self.costs.cost_for(notional, side)
        if side == "sell":
            account.cash += notional - cost
        else:
            account.cash -= notional + cost
        account.total_costs += cost
        account.trades += 1
        if side == "sell":
            account.sells += 1
        else:
            account.buys += 1
        if account.first_trade is None:
            account.first_trade = on
        account.last_trade = on
        self._trades_writer.writerow({
            "variant_id": account.variant.id, "date": on.isoformat(),
            "signal_date": signal_date.isoformat(), "key": key, "symbol": row[1],
            "side": side, "fill_price": row[4], "shares": shares, "notional": notional,
            "cost": cost, "cash_after": account.cash})

    def _mark(self, account: Account, on: date, day: dict, index: int) -> None:
        known_value = 0.0
        missing = 0
        unpriced_shares = 0
        unpriced_symbols: list[str] = []
        session_reasons: Counter = Counter()
        # Exposure disclosure for the part of the book that cannot be priced.
        # These are EXPOSURE figures, never a price and never an equity
        # figure: they say how big the unpriceable book is and how old its
        # last observed marks are.
        unpriced_exposure_value = 0.0
        unpriced_without_last_close = 0
        staleness_days_max = 0
        for key, position in account.positions.items():
            row = day.get(key)
            quarantined = (account.variant.policy == "quarantine_around_events"
                           and self._struct_quarantined(position["symbol"], index))
            valid = (row is not None and row[8] and _positive(row[5])
                     and not position.get("blocked") and not quarantined)
            if valid:
                position["last_close"] = row[5]
                position["last_date"] = on
                position["reference_close"] = row[5]
                known_value += position["shares"] * row[5]
                account.position_days_marked += 1
            else:
                missing += 1
                account.position_days_unknown += 1
                unpriced_shares += position["shares"]
                unpriced_symbols.append(position["symbol"])
                # Why the mark is missing is part of the diagnostic: a holding
                # that leaves the date-local EQ universe, a blocked identity, a
                # quarantined event and an invalid close are different gaps.
                if position.get("blocked"):
                    reason = "position_blocked_unverified_identity_change"
                elif quarantined:
                    reason = "quarantined_unresolved_event"
                elif row is None:
                    reason = "no_bar_in_date_local_eq_research_universe"
                else:
                    reason = "no_valid_close_for_session"
                account.unpriced_reasons[reason] += 1
                session_reasons[reason] += 1
                # Exposure disclosure (never a price, never a value): the
                # position's own last observed valid close and the session it
                # was observed on.  The holding still contributes NOTHING to
                # any equity figure; this only quantifies the size of the
                # hole and the age of the last mark that exists.
                last_close = position.get("last_close")
                last_date = position.get("last_date")
                if _positive(last_close):
                    unpriced_exposure_value += position["shares"] * last_close
                else:
                    unpriced_without_last_close += 1
                if last_date is not None:
                    staleness_days_max = max(staleness_days_max, (on - last_date).days)
                # The holding cannot be valued, so it contributes NOTHING to
                # any equity figure.  Its last observed close stays a fact
                # about the data; it is never used as a price, never as cash
                # and never as a zero.
                self._impact(account, on, "holding", "terminal_price",
                             "no_current_valid_close_equity_unknown_not_forward_filled",
                             key, position["symbol"], f"unpriced:{key}")
        # Strict equity: unknown whenever a holding cannot be priced.
        strict = None if missing else account.cash + known_value
        account.final_equity_strict = strict
        # The marked total is published only where every holding already
        # carried a current valid close; there it is by construction identical
        # to the strict equity.  While any holding is unpriceable no total is
        # published at all: publishing one would require pricing a security
        # that cannot be priced, which is exactly the dishonesty this
        # diagnostic must not commit.
        marked = strict
        account.final_equity_last_observed_marking = marked
        if missing:
            account.sessions_with_unknown_positions += 1
            account.sessions_with_no_total_published += 1
            account.unknown_position_instances += missing
            account.max_unpriced_positions = max(account.max_unpriced_positions, missing)
            account.unpriced_shares_total += unpriced_shares
            # Exposure disclosure, accumulated per variant.  It never enters
            # an equity figure: it is the size of the unpriceable book, not
            # a valuation of it.
            # The exposure magnitude is tracked per session, never summed
            # across sessions (the same stuck holding would be counted on
            # every session it stays unpriceable).
            account.final_unpriced_exposure_value = unpriced_exposure_value
            account.max_unpriced_exposure_value = max(
                account.max_unpriced_exposure_value, unpriced_exposure_value)
            account.unpriced_positions_without_last_close += unpriced_without_last_close
            account.max_unpriced_mark_staleness_days = max(
                account.max_unpriced_mark_staleness_days, staleness_days_max)
        else:
            account.sessions_all_positions_marked += 1
        bucket = account.year_coverage[on.year]
        bucket["sessions"] += 1
        if missing:
            bucket["sessions_with_unknown_positions"] += 1
            bucket["unknown_position_instances"] += missing
            # Per-year exposure: maxima and final-session value only.  A
            # session-sum would re-count the same stuck holding on every
            # session it stays unpriceable.
            bucket["max_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION"] = max(
                bucket["max_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION"],
                unpriced_exposure_value)
            bucket["final_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION"] = (
                unpriced_exposure_value)
            bucket["unpriced_positions_without_last_close"] += unpriced_without_last_close
            bucket["max_unpriced_mark_staleness_days"] = max(
                bucket["max_unpriced_mark_staleness_days"], staleness_days_max)
            for reason, count in session_reasons.items():
                bucket["unknown_reasons"][reason] += count
        self._equity_writer.writerow({
            "variant_id": account.variant.id, "date": on.isoformat(), "cash": account.cash,
            "positions_value_known": known_value, "equity_strict": strict if strict is not None else "",
            # Blank on every session with an unpriceable holding: no total is
            # published while a holding cannot be priced.
            "equity_last_observed_close_marking_ASSUMPTION": marked if marked is not None else "",
            "equity_status": "unknown_unpriced_holding" if missing else "known",
            "n_positions": len(account.positions), "unpriced_positions": missing,
            "unpriced_shares": unpriced_shares,
            # Exposure disclosure, NOT an equity figure and NOT a portfolio
            # value: the notional size of the part of the book that could not
            # be priced, at each position's own last observed close, and how
            # old that last observed mark is.
            "unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION":
                unpriced_exposure_value,
            "unpriced_positions_without_last_close": unpriced_without_last_close,
            "unpriced_mark_staleness_days_max": staleness_days_max,
            "unpriced_symbols": ";".join(sorted(unpriced_symbols))})

    def _rank(self, on: date, history: deque, day: dict, closes: dict, counts: Counter,
              lookback: int, policy: str) -> list[str]:
        if len(history) < lookback + 1:
            return []
        anchor_date, anchor = history[-(lookback + 1)]
        if self.session_index[on] - self.session_index[anchor_date] != lookback:
            return []
        anchor_index = self.session_index[anchor_date]
        signal_index = self.session_index[on]
        ranked = []
        for key, row in day.items():
            if counts.get(key, 0) < MIN_VALID_OBSERVATIONS:
                continue
            if not (row[8] and _positive(row[5]) and row[5] >= MIN_RAW_CLOSE
                    and _positive(row[7]) and row[7] >= MIN_VOLUME):
                continue
            if key not in closes or key not in anchor:
                continue
            if policy == "quarantine_around_events" and self._contaminated(row[1], anchor_index, signal_index):
                continue
            ranked.append((key, closes[key] / anchor[key] - 1))
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return [key for key, _ in ranked]

    def _build_quarantine_index(self, risks: list, sessions: list) -> None:
        ordered = sorted(risks, key=lambda r: (str(r.get("date") or ""),
                                                str(r.get("kind") or ""),
                                                str(r.get("symbol") or ""),
                                                str(r.get("id") or "")))
        for risk in ordered:
            symbol = risk.get("symbol")
            on = risk.get("date")
            if not symbol or on is None:
                continue
            index = bisect_left(sessions, on)
            if index >= len(sessions):
                continue
            kind = risk.get("kind")
            if kind in STRUCTURAL_KINDS:
                self.contamination[symbol].append(index)
                self._struct_starts[symbol].append(index)
                self._struct_ends[symbol].append(index + self.structural_quarantine)
            elif kind in DIVIDEND_KINDS:
                self.contamination[symbol].append(index)
                self._div_starts[symbol].append(index)
                self._div_ends[symbol].append(index + self.dividend_buy_quarantine)
        for table in (self.contamination, self._struct_starts, self._struct_ends,
                      self._div_starts, self._div_ends):
            for key in table:
                table[key] = sorted(set(table[key]))

    @staticmethod
    def _tradable(row) -> bool:
        # Do not inspect today's close or full-day volume/validity at the open.
        if row is None or not _positive(row[4]) or not _positive(row[6]):
            return False
        return 0.8 < row[4] / row[6] < 1.2


def _variant_report(account: Account, impact_counts: dict | None = None) -> dict:
    coverage = {str(year): dict(values) for year, values in sorted(account.year_coverage.items())}
    return {
        "variant_id": account.variant.id,
        "lookback_sessions": account.variant.lookback,
        "top_n": account.variant.top_n,
        "policy": account.variant.policy,
        "sessions": account.sessions,
        "rebalances": account.rebalances,
        "signals_scheduled": account.signals_scheduled,
        "trades": account.trades,
        "buys": account.buys,
        "sells": account.sells,
        "skips_total": int(sum(account.skip_counts.values())),
        "skip_counts": dict(sorted(account.skip_counts.items())),
        "sessions_with_all_positions_marked": account.sessions_all_positions_marked,
        "sessions_with_unknown_positions": account.sessions_with_unknown_positions,
        "unpriced_equity_sessions": account.sessions_with_unknown_positions,
        "unknown_position_instances": account.unknown_position_instances,
        "position_days_marked_known": account.position_days_marked,
        "position_days_unknown": account.position_days_unknown,
        "sessions_with_unknown_opening_mark": account.sessions_with_unknown_opening_mark,
        "sessions_where_no_total_could_be_published":
            account.sessions_with_no_total_published,
        "unpriced_reason_counts": dict(sorted(account.unpriced_reasons.items())),
        "unpriced_shares_total": account.unpriced_shares_total,
        # Exposure disclosure, aggregated per variant.  These are EXPOSURE
        # figures: not equity, not a portfolio value, not a marked total and
        # not a return.  They never enter any equity figure.
        "final_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION":
            account.final_unpriced_exposure_value,
        "max_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION":
            account.max_unpriced_exposure_value,
        "max_unpriced_mark_staleness_days": account.max_unpriced_mark_staleness_days,
        "unpriced_positions_without_last_close": account.unpriced_positions_without_last_close,
        "max_unpriced_positions_in_session": account.max_unpriced_positions,
        "final_cash": account.cash,
        "total_costs": account.total_costs,
        "final_equity_strict": account.final_equity_strict,
        "terminal_positions": len(account.positions),
        "first_trade": account.first_trade.isoformat() if account.first_trade else None,
        "last_trade": account.last_trade.isoformat() if account.last_trade else None,
        # No marked total is published while a holding cannot be priced.  The
        # unpriceable holding contributes nothing to any equity figure, so a
        # marked total would be a fiction; the equity stays unknown.
        "equity_last_observed_close_marking": {
            "label": "NOT PUBLISHED while a holding is unpriced - not a return, not alpha, "
                     "not unbiased",
            "assumption": MARKING_ASSUMPTION,
            "published": account.final_equity_last_observed_marking is not None,
            "final_equity": account.final_equity_last_observed_marking,
            "final_equity_strict": account.final_equity_strict,
            "sessions": account.sessions,
            "sessions_with_unpriced_positions": account.sessions_with_unknown_positions,
            "sessions_where_no_total_could_be_published":
                account.sessions_with_no_total_published,
            "unpriced_position_instances": account.unknown_position_instances,
            "unpriced_shares_total": account.unpriced_shares_total,
            "note": ("A holding that cannot be priced contributes nothing to any equity "
                     "figure. The equity of such a session is unknown, and no marked total "
                     "is published for it either, because a marked total would have to put "
                     "a stale price on a security that cannot be priced. A marked total is "
                     "published only for a session in which every holding carried a "
                     "current valid close, where it is by construction identical to the "
                     "strict equity. It is not a return and not alpha."),
        },
        # The size of the unpriceable book is disclosed separately, and is
        # deliberately not a total of any kind.
        "unpriced_exposure_disclosure": {
            "label": "EXPOSURE disclosure - not equity, not a portfolio value, "
                     "not a marked total, not a return, not alpha",
            "assumption": EXPOSURE_ASSUMPTION,
            "final_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION":
                account.final_unpriced_exposure_value,
            "max_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION":
                account.max_unpriced_exposure_value,
            "max_unpriced_mark_staleness_days": account.max_unpriced_mark_staleness_days,
            "unpriced_positions_without_last_close": account.unpriced_positions_without_last_close,
            "unpriced_exposure_shares_total": account.unpriced_shares_total,
            "sessions_with_unpriced_positions": account.sessions_with_unknown_positions,
            "sessions_where_no_total_could_be_published":
                account.sessions_with_no_total_published,
            "note": ("These figures quantify how much of the book could not be priced at "
                     "all and how stale the last observed marks are. They are not a "
                     "value of the portfolio, not an equity total, not a return and not "
                     "alpha: the total is the same unpriceable book counted again on "
                     "every session it stayed unpriceable, so it is not additive to "
                     "anything and must not be read as a portfolio value. An unpriceable "
                     "holding still contributes nothing to any equity figure."),
        },
        "impact_counts": dict(sorted((impact_counts or {}).items())),
        "unknown_coverage_by_year": coverage,
    }


UNPRICED_REASONS = (
    "no_bar_in_date_local_eq_research_universe",
    "no_valid_close_for_session",
    "position_blocked_unverified_identity_change",
    "quarantined_unresolved_event",
)

COMPARISON_COLUMNS = [
    "variant_id", "lookback_sessions", "top_n", "policy", "sessions", "rebalances",
    "trades", "buys", "sells", "skips_total", "skip_sell_not_executable",
    "skip_sell_position_blocked", "skip_sell_quarantined_unresolved_event",
    "skip_buy_not_executable", "skip_buy_position_blocked",
    "skip_buy_quarantined_unresolved_event", "skip_buy_unknown_held_opening_value",
    "sessions_with_all_positions_marked", "sessions_with_unknown_positions",
    "unpriced_equity_sessions", "unknown_position_instances", "max_unpriced_positions",
    "position_days_marked_known", "position_days_unknown",
    "sessions_with_unknown_opening_mark",
    "unpriced_no_bar_in_date_local_eq_research_universe",
    "unpriced_no_valid_close_for_session",
    "unpriced_position_blocked_unverified_identity_change",
    "unpriced_quarantined_unresolved_event",
    "sessions_where_no_total_could_be_published",
    # Exposure disclosure per variant: not equity, not a portfolio value.
    "final_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION",
    "max_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION",
    "max_unpriced_mark_staleness_days",
    "unpriced_positions_without_last_close",
    "final_cash", "total_costs",
    "final_equity_strict", "final_equity_last_observed_close_marking_ASSUMPTION",
    "terminal_positions", "first_trade", "last_trade",
]


def _comparison_row(report: dict) -> dict:
    skips = report["skip_counts"]
    reasons = report["unpriced_reason_counts"]
    row = {key: report.get(key) for key in COMPARISON_COLUMNS if key in report}
    for column in COMPARISON_COLUMNS:
        if column.startswith("skip_"):
            row[column] = skips.get(column[len("skip_"):], 0)
        elif column.startswith("unpriced_") and column[len("unpriced_"):] in UNPRICED_REASONS:
            # Only the four reason columns come from the reason counters; the
            # other "unpriced_*" columns are plain report fields.
            row[column] = reasons.get(column[len("unpriced_"):], 0)
    row["max_unpriced_positions"] = report["max_unpriced_positions_in_session"]
    marked = report["equity_last_observed_close_marking"]["final_equity"]
    row["final_equity_last_observed_close_marking_ASSUMPTION"] = "" if marked is None else marked
    row["final_equity_strict"] = "" if report["final_equity_strict"] is None else report["final_equity_strict"]
    return row


def _impact_counts_by_policy(reports_list: list[dict]) -> dict:
    """Audited impact counts per policy, summed over that policy's variants.

    The quarantine policy has to differ from audit-only in the audited impact
    counts, otherwise it is not a policy at all.  This makes the difference
    inspectable instead of implied.
    """
    totals: dict[str, Counter] = defaultdict(Counter)
    for report in reports_list:
        for bucket, count in report.get("impact_counts", {}).items():
            totals[report["policy"]][bucket] += count
    return {policy: dict(sorted(counts.items())) for policy, counts in sorted(totals.items())}


def _policy_comparison(reports_list: list[dict]) -> list[dict]:
    """Per (lookback, top-N) comparison of the two unresolved-event policies."""
    by_key: dict[tuple, dict] = {}
    for report in reports_list:
        key = (report["lookback_sessions"], report["top_n"])
        by_key.setdefault(key, {})[report["policy"]] = report
    comparisons = []
    for (lookback, top_n), policies in sorted(by_key.items()):
        baseline = policies.get("audit_only")
        other = policies.get("quarantine_around_events")
        if baseline is None or other is None:
            continue
        fields = {}
        differs = False
        for name in ("trades", "buys", "sells", "skips_total",
                     "sessions_with_unknown_positions", "unknown_position_instances",
                     "position_days_unknown", "sessions_with_unknown_opening_mark",
                     "terminal_positions"):
            left, right = baseline[name], other[name]
            fields[name] = {"audit_only": left, "quarantine_around_events": right,
                            "delta": right - left}
            if right != left:
                differs = True
        impact_delta = {}
        for bucket in sorted(set(baseline["impact_counts"]) | set(other["impact_counts"])):
            left = baseline["impact_counts"].get(bucket, 0)
            right = other["impact_counts"].get(bucket, 0)
            impact_delta[bucket] = {"audit_only": left, "quarantine_around_events": right,
                                    "delta": right - left}
            if right != left:
                differs = True
        comparisons.append({
            "lookback_sessions": lookback, "top_n": top_n,
            "audit_only_variant_id": baseline["variant_id"],
            "quarantine_variant_id": other["variant_id"],
            "outcomes": fields,
            "impact_counts": impact_delta,
            "policies_differ": differs,
        })
    return comparisons


def _analysis_digest(summary: dict) -> str:
    """SHA-256 over the deterministic part of a summary.

    The wall-clock stamp, the generation stamp and the input mtimes are the
    only volatile fields; everything else - every count, every table and every
    figure - must be identical between two runs over the same inputs.
    """
    import hashlib

    payload = json.loads(json.dumps(summary, default=str))
    payload.pop("generated_at", None)
    payload.pop("determinism", None)
    # Output locations are run configuration, not analysis: two runs of the
    # same analysis from different directories must agree on the digest.
    payload.pop("outputs", None)
    provenance = payload.get("provenance", {})
    provenance.pop("elapsed_seconds", None)
    for item in provenance.get("inputs", []):
        item.pop("mtime_ns", None)
    readiness = payload.get("readiness", {})
    readiness.pop("generated_at", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_sensitivity(data: Path | str = DEFAULT_DATA, start: str = DEFAULT_START,
                    end: str = DEFAULT_END, out: Path | str | None = None,
                    reports: Path | str | None = None, allow_incomplete: bool = False,
                    initial_cash: float = 1e7, lookbacks=DEFAULT_LOOKBACKS,
                    top_ns=DEFAULT_TOP_NS, policies=DEFAULT_POLICIES,
                    max_variants: int = DEFAULT_MAX_VARIANTS,
                    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
                    structural_quarantine: int = DEFAULT_STRUCTURAL_QUARANTINE_SESSIONS,
                    dividend_buy_quarantine: int = DEFAULT_DIVIDEND_BUY_QUARANTINE_SESSIONS,
                    write_outputs: bool = True,
                    use_key_stabilization: bool = True) -> dict:
    """Run the sensitivity grid, or raise :class:`SensitivityReadinessError`.

    Strict by default: the readiness gate runs first and blockers stop the
    run unless ``allow_incomplete=True`` is passed.  ``allow_incomplete`` is
    diagnostic consent, never a completeness waiver, and no result of this
    function ever claims alpha.  ``use_key_stabilization=False`` reproduces
    the pre-stabilization keys (the diagnostic before/after switch); it
    never relaxes a safety rule.
    """
    import duckdb

    began = time.monotonic()
    data = Path(data)
    first, last = _date(start), _date(end)
    if first is None or last is None or last < first:
        raise ValueError("Expected an ordered ISO start/end date window")
    if not _positive(initial_cash):
        raise ValueError("initial_cash must be positive")
    out = Path(out) if out is not None else data / "processed/backtest/sensitivity"
    reports = Path(reports) if reports is not None else data / "reports"
    out.mkdir(parents=True, exist_ok=True)

    # The gate runs before any trading input is read, and its artefact is
    # written into this runner's own output directory.
    readiness = build_readiness(data, str(first), str(last), universe="research",
                                out=out / "readiness.json")
    if not readiness.get("ready_for_backtesting") and not allow_incomplete:
        raise SensitivityReadinessError(readiness)

    adjusted = data / "processed/adjusted/adjusted_bars.parquet"
    if not adjusted.exists():
        raise FileNotFoundError("Split-adjusted signal input is required; no raw fallback: " + str(adjusted))
    action_audit = data / "processed/adjusted/action_audit.parquet"
    if not action_audit.exists():
        raise FileNotFoundError("Raw-share accounting requires the applied split audit: " + str(action_audit))

    grid, dropped = build_grid(lookbacks, top_ns, policies, max_variants)
    if not grid:
        raise ValueError("The sensitivity grid is empty")
    costs = CostConfig()
    simulation = _Simulation(data, first, last, grid, initial_cash, costs, out,
                             structural_quarantine, dividend_buy_quarantine,
                             float(time_budget_seconds), use_key_stabilization)

    with ExitStack() as stack:
        db = stack.enter_context(open_catalog(data,
                                                config={"memory_limit": "512MB", "threads": 2}))
        if write_outputs:
            trades_stream = stack.enter_context(
                (out / "trades.csv").open("w", newline="", encoding="utf-8", buffering=65536))
            simulation._trades_writer = csv.DictWriter(trades_stream, fieldnames=[
                "variant_id", "date", "signal_date", "key", "symbol", "side", "fill_price",
                "shares", "notional", "cost", "cash_after"])
            simulation._trades_writer.writeheader()
            equity_stream = stack.enter_context(
                (out / "equity_marks.csv").open("w", newline="", encoding="utf-8", buffering=65536))
            simulation._equity_writer = csv.DictWriter(equity_stream, fieldnames=[
                "variant_id", "date", "cash", "positions_value_known", "equity_strict",
                "equity_last_observed_close_marking_ASSUMPTION",
                "equity_status",
                "n_positions", "unpriced_positions", "unpriced_shares",
                "unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION",
                "unpriced_positions_without_last_close",
                "unpriced_mark_staleness_days_max",
                "unpriced_symbols"])
            simulation._equity_writer.writeheader()
            impact_stream = stack.enter_context(
                (out / "impact_audit.csv").open("w", newline="", encoding="utf-8", buffering=65536))
            simulation._impact_writer = csv.DictWriter(impact_stream, fieldnames=[
                "variant_id", "date", "scope", "kind", "key", "symbol", "event_id", "reason"])
            simulation._impact_writer.writeheader()
        else:
            class _Null:
                def writerow(self, row):
                    return None
            simulation._trades_writer = _Null()
            simulation._equity_writer = _Null()
            simulation._impact_writer = _Null()
        simulation.run(db)

    reports_list = [_variant_report(account, simulation.variant_impacts.get(account.variant.id))
                    for account in simulation.accounts]
    provenance = [_fingerprint(path) for path in sorted(simulation.inputs) if path.exists()]
    cap_reasons = []
    if dropped:
        cap_reasons.append(f"max_variants={max_variants} dropped {len(dropped)} variant(s)")
    if simulation.truncated:
        cap_reasons.append(f"time_budget_seconds={time_budget_seconds} stopped the window at "
                           f"{simulation.stopped_at}")

    impact_by_policy = _impact_counts_by_policy(reports_list)
    policy_comparison = _policy_comparison(reports_list)
    window_coverage = {
        "requested_start": str(first), "requested_end": str(last),
        "sessions_simulated": simulation.sessions_seen,
        "truncated": simulation.truncated,
        "stopped_at": simulation.stopped_at.isoformat() if simulation.stopped_at else None,
        "note": ("Every variant shares one streaming pass, so a truncated window stops all "
                 "variants at the same session and they stay comparable."),
    }
    for report in reports_list:
        report["window_coverage"] = dict(window_coverage)

    summary = {
        "schema_version": 1,
        "mode": "sensitivity_robustness_diagnostic_incomplete_data",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ready_for_backtesting": False,
        "ready_for_alpha_claim": False,
        "total_return": False,
        "alpha_claim": False,
        "allow_incomplete": bool(allow_incomplete),
        "start": str(first),
        "end": str(last),
        "initial_cash": initial_cash,
        "readiness": readiness,
        "readiness_note": ("Blockers remain; this run was allowed to proceed only because the "
                           "operator passed --allow-incomplete. That is diagnostic consent, not a "
                           "completeness waiver."),
        "grid": {
            "lookback_sessions": [int(v) for v in lookbacks],
            "top_n": [int(v) for v in top_ns],
            "policies": [str(p) for p in policies],
            "requested_variants": len(grid) + len(dropped),
            "variants_run": len(grid),
            "variant_ids": [v.id for v in grid],
            "dropped_variants": [v.id for v in dropped],
            "max_variants": max_variants,
            "time_budget_seconds": float(time_budget_seconds),
            "cap_reasons": cap_reasons,
            "cap_documented": ("The grid is capped so a full run stays inside a predictable "
                               "wall-clock budget: at most max_variants portfolios share one "
                               "streaming pass and the pass stops at the first month end after "
                               "time_budget_seconds is exceeded. Truncation is recorded here and "
                               "in the per-variant window_coverage field."),
            "window_truncated": simulation.truncated,
            "stopped_at": simulation.stopped_at.isoformat() if simulation.stopped_at else None,
        },
        "method": {
            "rebalance": "monthly, on the last observed session of each calendar month",
            "signal": ("momentum over the lookback window on split-adjusted closes; the anchor "
                       "must be an observed session exactly lookback sessions earlier; no "
                       "forward-filled anchors"),
            "execution": ("next observed session, raw open only; today's close, full-day volume "
                          "and validity are never used to size an opening order"),
            "costs": dict(costs.__dict__),
            "cost_note": ("Same CostConfig as the full-history runner: stamp duty is charged on "
                          "buys only, brokerage is bps of notional capped per order, GST applies "
                          "to brokerage, slippage is charged as a cost."),
            "universe": ("date-local EQ with an INE ISIN or no ISIN on that date; research "
                         "defined, not official Nifty membership"),
            "signal_eligibility": {"raw_min_close": MIN_RAW_CLOSE, "min_volume": MIN_VOLUME,
                                    "valid_observations": MIN_VALID_OBSERVATIONS},
            "price_mode": "split_adjusted_signal_raw_open_execution_raw_share_accounting",
            "dividends": ("credited only when the event row carries a verified raw per-share "
                          "amount, a verified ex-date, an official source and party binding; "
                          "otherwise the event is audited and no cash is invented"),
            "unknown_prices": ("a holding that cannot be priced leaves the equity unknown "
                               "for that session; it is never cash, never zero and "
                               "never forward-filled. An unpriceable holding "
                               "contributes nothing to any equity figure: the "
                               "labelled 'last observed close' marking is published "
                               "only on a session where every holding already "
                               "carried a current valid close, and is never a "
                               "number that contains an unpriceable holding."),
            "unpriced_exposure_disclosure": EXPOSURE_ASSUMPTION,
        },
        "policy_definitions": POLICY_DEFINITIONS,
        "policy_parameters": {
            "structural_quarantine_sessions": structural_quarantine,
            "dividend_buy_quarantine_sessions": dividend_buy_quarantine,
            "signal_contamination_window": "lookback window (anchor, signal session]",
            "note": ("These windows are analyst choices for this diagnostic. They are not exchange "
                     "rules and are not claimed to be correct."),
        },
        "variants": reports_list,
        "impact_counts": dict(sorted(simulation.audit_counts.items())),
        "impact_counts_by_policy": impact_by_policy,
        "policy_comparison": policy_comparison,
        "data_impact_counts": dict(sorted(simulation.data_audit_counts.items())),
        "input_risk_counts": dict(sorted(simulation.risk_counts.items())),
        "coverage": {
            "sessions_simulated": simulation.sessions_seen,
            "rebalances": simulation.rebalances,
            "streamed_join_rows": simulation.streamed_rows,
            "window": window_coverage,
        },
        "provenance": {
            "inputs": provenance,
            "database_read_only": True,
            "duckdb_memory_limit": "512MB",
            "duckdb_threads": 2,
            "fetch_batch_rows": 8192,
            "signal_history_sessions": max((v.lookback for v in grid), default=63) + 1,
            "elapsed_seconds": time.monotonic() - began,
        },
        "limitations": LIMITATIONS,
        "determinism": {
            "volatile_fields": ["generated_at", "provenance.elapsed_seconds",
                                "provenance.inputs[].mtime_ns", "readiness.generated_at",
                                "outputs.* (run locations, not analysis)"],
            "note": ("Every count, table and figure in this summary is a deterministic "
                     "function of the inputs: two runs over the same inputs produce the "
                     "same analysis_digest and byte-identical CSV/Markdown outputs. Only "
                     "the fields listed in volatile_fields differ between runs."),
            "analysis_digest": "",
        },
        "outputs": {
            "summary": str(out / "summary.json"),
            "comparison_csv": str(out / "comparison.csv"),
            "unknown_coverage_by_year_csv": str(out / "unknown_coverage_by_year.csv"),
            "equity_marks_csv": str(out / "equity_marks.csv"),
            "trades_csv": str(out / "trades.csv"),
            "impact_audit_csv": str(out / "impact_audit.csv"),
            "readiness": str(out / "readiness.json"),
            "report_json": str(reports / "backtest-sensitivity.json"),
            "report_md": str(reports / "backtest-sensitivity.md"),
        },
    }

    summary["determinism"]["analysis_digest"] = _analysis_digest(summary)

    if write_outputs:
        _write_outputs(out, reports, summary, reports_list, simulation)
    return summary


def _write_outputs(out: Path, reports: Path, summary: dict,
                   reports_list: list[dict], simulation: _Simulation) -> None:
    out.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False,
                                                  default=str) + "\n", encoding="utf-8")
    with (out / "comparison.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=COMPARISON_COLUMNS)
        writer.writeheader()
        for report in reports_list:
            writer.writerow(_comparison_row(report))
    with (out / "unknown_coverage_by_year.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=[
            "variant_id", "lookback_sessions", "top_n", "policy", "year", "sessions",
            "sessions_with_unknown_positions", "sessions_all_positions_marked",
            "unknown_position_instances",
            "max_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION",
            "final_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION",
            "unpriced_positions_without_last_close",
            "max_unpriced_mark_staleness_days"]
            + [f"unknown_reason_{reason}" for reason in UNPRICED_REASONS])
        writer.writeheader()
        for report in reports_list:
            for year, values in sorted(report["unknown_coverage_by_year"].items()):
                reasons = values.get("unknown_reasons", {})
                row = {
                    "variant_id": report["variant_id"],
                    "lookback_sessions": report["lookback_sessions"],
                    "top_n": report["top_n"], "policy": report["policy"], "year": year,
                    "sessions": values["sessions"],
                    "sessions_with_unknown_positions": values["sessions_with_unknown_positions"],
                    "sessions_all_positions_marked": values["sessions"]
                    - values["sessions_with_unknown_positions"],
                    "unknown_position_instances": values["unknown_position_instances"],
                    # Per-year exposure disclosure: the size of the unpriceable
                    # book in that year (worst session and final session), not
                    # an equity figure and not a portfolio value.
                    "max_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION":
                        values.get("max_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION", 0.0),
                    "final_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION":
                        values.get("final_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION", 0.0),
                    "unpriced_positions_without_last_close":
                        values.get("unpriced_positions_without_last_close", 0),
                    "max_unpriced_mark_staleness_days":
                        values.get("max_unpriced_mark_staleness_days", 0)}
                for reason in UNPRICED_REASONS:
                    row[f"unknown_reason_{reason}"] = reasons.get(reason, 0)
                writer.writerow(row)
    (reports / "backtest-sensitivity.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False, default=str) + "\n", encoding="utf-8")
    (reports / "backtest-sensitivity.md").write_text(
        _render_markdown(summary, reports_list), encoding="utf-8")


def _fmt(value, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return str(value)


def _render_policy_comparison(summary: dict) -> list[str]:
    """Markdown block proving the two policies differ in the audited impact counts."""
    comparisons = summary.get("policy_comparison", [])
    by_policy = summary.get("impact_counts_by_policy", {})
    lines = [
        "## Policy comparison (audit-only vs quarantine-around-events)",
        "",
        "The two unresolved-event policies must differ in the **audited impact counts**, not "
        "only in wording: the quarantine policy quarantines a symbol around an unresolved "
        "structural event, which changes what is audited, what is executed and which sessions "
        "have an unknown equity.",
        "",
    ]
    if not comparisons:
        lines += ["No comparable policy pair was run in this grid.", ""]
        return lines
    differing = [c for c in comparisons if c["policies_differ"]]
    lines += [
        f"- Grid points compared: {len(comparisons)}; points where the two policies differ in "
        f"an outcome or an audited impact count: **{len(differing)}**.",
        "",
        "| lookback | top-N | trades (audit -> quarantine) | skips (audit -> quarantine) | "
        "unknown-equity sessions (audit -> quarantine) | audited impact counts differ |",
        "|---|---|---|---|---|---|",
    ]
    for item in comparisons:
        outcomes = item["outcomes"]
        lines.append(
            f"| {item['lookback_sessions']} | {item['top_n']} | "
            f"{outcomes['trades']['audit_only']} -> {outcomes['trades']['quarantine_around_events']} | "
            f"{outcomes['skips_total']['audit_only']} -> {outcomes['skips_total']['quarantine_around_events']} | "
            f"{outcomes['sessions_with_unknown_positions']['audit_only']} -> "
            f"{outcomes['sessions_with_unknown_positions']['quarantine_around_events']} | "
            f"{'yes' if item['policies_differ'] else 'no'} |")
    lines += [
        "",
        "### Audited impact counts by policy (summed over that policy's variants)",
        "",
    ]
    buckets = sorted({bucket for counts in by_policy.values() for bucket in counts})
    lines += ["| impact bucket | " + " | ".join(sorted(by_policy)) + " |",
              "|---" * (1 + len(by_policy)) + "|"]
    for bucket in buckets:
        cells = [str(by_policy[policy].get(bucket, 0)) for policy in sorted(by_policy)]
        lines.append(f"| `{bucket}` | " + " | ".join(cells) + " |")
    lines += [
        "",
        "The full per-variant impact counts are in each variant record of `summary.json` "
        "(`variants[].impact_counts`) and in `data/processed/backtest/sensitivity/"
        "impact_audit.csv`.",
        "",
    ]
    return lines


def _render_exposure_disclosure(summary: dict, reports_list: list[dict]) -> list[str]:
    """Markdown block for the unpriceable-exposure disclosure.

    The exposure figures are never a total: they say how much of the book
    could not be priced at all and how stale the last observed marks are.
    They are not presented as a portfolio value, a return or alpha.
    """
    if not reports_list:
        return []
    rates = [(r["sessions_with_unknown_positions"] / r["sessions"])
             for r in reports_list if r["sessions"]]
    low = min(rates) * 100 if rates else 0.0
    high = max(rates) * 100 if rates else 0.0
    universe_reason = "no_bar_in_date_local_eq_research_universe"
    universe_days = sum(r["unpriced_reason_counts"].get(universe_reason, 0)
                         for r in reports_list)
    total_days = sum(sum(r["unpriced_reason_counts"].values()) for r in reports_list)
    trades = [r["last_trade"] for r in reports_list if r["last_trade"]]
    rebalances = sum(r["rebalances"] for r in reports_list)
    executed = sum(r["trades"] for r in reports_list)
    lines = [
        "## Why the equity is unknown on most sessions, and what the exposure figures are",
        "",
        f"- The strict equity of this strategy is unknown on **{low:.1f}% to {high:.1f}%** of "
        f"the sessions it was run over, across the {len(reports_list)} variants of the grid.",
        f"- The dominant reason is `{universe_reason}`: the held security has no row in the "
        f"date-local EQ/INE research universe on that session - a series migration (EQ to BE), "
        f"an ISIN outside the INE set, or a missing bar. That is {universe_days:,} of the "
        f"{total_days:,} unpriced position-days of this run.",
        "- The runner never invents an exit for such a holding, so the holding stays open, the "
        "opening mark of the session is unknown, no buy is executed and the unpriceable "
        "holding itself can never be sold: **the strategy stops trading**. The last executed "
        "trade is "
        + (f"between {min(trades)} and {max(trades)}" if trades else "never")
        + f" across the variants, while {rebalances:,} rebalances were scheduled and only "
        f"{executed:,} trades were executed.",
        "- The strategy consequently **cannot be rebalanced into a priced book** after that "
        "point. The measured behaviour of the strategy is the behaviour of the period up to "
        "that freeze; the rest of the window is a frozen book that cannot be priced.",
        "- **The exposure figures below are a disclosure, not a valuation.** They quantify how "
        "much of the book could not be priced at all (at each position's own last observed "
        "close) and how stale those last observed marks are. They are **not returns, not "
        "alpha, not an equity total and not a portfolio value**: the figure below is the "
        "unpriceable book's size at the **final session** (the worst-session size is in the "
        "per-variant JSON), and it is deliberately not summed across sessions, because the "
        "same stuck holding is unpriceable on every session it stays unpriceable and a session "
        "sum would re-count it thousands of times while describing nothing. An "
        "unpriceable holding still contributes nothing to any equity figure, and no marked "
        "total is published while any holding is unpriceable.",
        "",
        "| variant | sessions with an unpriced holding | unpriced exposure at the final "
        "session (ASSUMPTION - not a value) | max staleness of the last observed mark (calendar "
        "days) | unpriceable positions with no last observed close | last trade |",
        "|---|---|---|---|---|---|",
    ]
    for report in reports_list:
        lines.append(
            f"| `{report['variant_id']}` | {report['sessions_with_unknown_positions']} | "
            f"{_fmt(report['final_unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION'])} "
            f"| {report['max_unpriced_mark_staleness_days']} | "
            f"{report['unpriced_positions_without_last_close']} | "
            f"{report['last_trade'] or 'never'} |")
    lines += [
        "",
        f"> **Exposure assumption:** {EXPOSURE_ASSUMPTION}",
        "",
        f"- The exposure column is the magnitude of the unpriceable book at the **final "
        f"session**, with the worst-session magnitude in the per-variant JSON. It is a "
        f"per-session magnitude and is deliberately **not summed across sessions**: the same "
        f"stuck holding is unpriceable on every session it stays unpriceable, so a session "
        f"sum would count one position thousands of times and could exceed the initial cash "
        f"of {summary['initial_cash']:,.0f} while describing nothing. It is not additive to "
        f"anything, not a portfolio value and not what the portfolio was worth.",
        "- The per-session exposure figures are in "
        "`data/processed/backtest/sensitivity/equity_marks.csv` (columns "
        "`unpriced_exposure_value_AT_LAST_OBSERVED_CLOSE_ASSUMPTION`, "
        "`unpriced_positions_without_last_close`, `unpriced_mark_staleness_days_max`) and the "
        "per-year table is in "
        "`data/processed/backtest/sensitivity/unknown_coverage_by_year.csv`.",
        "",
    ]
    return lines


def _render_markdown(summary: dict, reports_list: list[dict]) -> str:
    grid = summary["grid"]
    readiness = summary.get("readiness", {})
    lines = [
        "# Backtest sensitivity / robustness diagnostic (INCOMPLETE DATA)",
        "",
        "> **This is an incomplete-data diagnostic. It is not a return, not alpha, not an "
        "unbiased estimate and not a total-return series.** It shows how much the measured "
        "behaviour of the 2000-2026 price/volume momentum strategy depends on analyst choices, "
        "using only the data that exists on disk. No variant here is a claim of performance.",
        "",
        "## What was run",
        "",
        f"- Window: **{summary['start']} to {summary['end']}** "
        f"(the retained snapshot ends {summary['end']}; \"2026\" means the available part of the year).",
        f"- Initial cash: {summary['initial_cash']:,.0f}; rebalance: {summary['method']['rebalance']}.",
        f"- Signal: {summary['method']['signal']}",
        f"- Execution: {summary['method']['execution']}",
        f"- Costs: {summary['method']['cost_note']}",
        f"- Universe: {summary['method']['universe']}",
        f"- Unknown prices: {summary['method']['unknown_prices']}",
        "",
        "## Readiness gate (strict by default)",
        "",
        f"- `ready_for_backtesting`: **{readiness.get('ready_for_backtesting')}**; "
        f"`ready_for_alpha_claim`: **{readiness.get('ready_for_alpha_claim')}**.",
        f"- Blockers still open: `{', '.join(readiness.get('blocker_codes', [])) or 'none'}`.",
        f"- This run proceeded because `--allow-incomplete` was passed: "
        f"**{summary['allow_incomplete']}**. {summary['readiness_note']}",
        "",
        "## Grid and cap",
        "",
        f"- Lookbacks: {grid['lookback_sessions']}; top-N: {grid['top_n']}; policies: {grid['policies']}.",
        f"- Variants requested: {grid['requested_variants']}; variants run: {grid['variants_run']} "
        f"(cap `max_variants={grid['max_variants']}`, time budget "
        f"{grid['time_budget_seconds']:.0f}s).",
        f"- Dropped variants: {grid['dropped_variants'] or 'none'}.",
        f"- Cap reasons: {'; '.join(grid['cap_reasons']) or 'none'}.",
        f"- {grid['cap_documented']}",
        f"- Window truncated: **{grid['window_truncated']}**"
        + (f" (stopped at {grid['stopped_at']})" if grid["stopped_at"] else ""),
        "",
        "## Variant comparison",
        "",
        "`final equity (strict)` is unknown when any holding could not be priced; it is never "
        "replaced by cash, never zero and never a stale price. `final equity "
        "(last-observed-close marking)` is **not published** for a variant whose last session "
        "held an unpriceable position: an unpriceable holding contributes nothing to any "
        "equity figure, not even to a labelled assumption total, so the figure stays unknown. "
        "Where every holding was priced at a current valid close the marked total is identical "
        "to the strict equity. The unpriceable exposure (share count and symbols, plus the "
        "notional size at each position's own last observed close and the staleness of that "
        "mark) is published per session in `equity_marks.csv` as an EXPOSURE disclosure, not "
        "as a value.",
        "",
        "| variant | lookback | top-N | policy | sessions | trades | skips | sessions w/ unknown "
        "positions | unpriced-equity sessions | final equity (strict) | final equity "
        "(last-observed-close marking) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for report in reports_list:
        marking = report["equity_last_observed_close_marking"]
        lines.append(
            f"| `{report['variant_id']}` | {report['lookback_sessions']} | {report['top_n']} | "
            f"{report['policy']} | {report['sessions']} | {report['trades']} | "
            f"{report['skips_total']} | {report['sessions_with_unknown_positions']} | "
            f"{report['unpriced_equity_sessions']} | "
            f"{_fmt(report['final_equity_strict'])} | "
            f"{_fmt(marking['final_equity'])} |")
    lines += [
        "",
        f"> **Marking assumption:** {MARKING_ASSUMPTION}",
        "",
        "## What this run shows",
        "",
    ]
    total_sessions = max((r["sessions"] for r in reports_list), default=0)
    unknown_rates = [(r["sessions_with_unknown_positions"] / r["sessions"])
                     for r in reports_list if r["sessions"]]
    known_final = [r["variant_id"] for r in reports_list if r["final_equity_strict"] is not None]
    frozen = [(r["variant_id"], r["sessions_with_unknown_opening_mark"])
              for r in reports_list]
    lines += [
        f"- Sessions simulated per variant: {total_sessions}.",
        f"- Share of sessions whose equity is unknown under strict marking: "
        f"{min(unknown_rates) * 100:.1f}% to {max(unknown_rates) * 100:.1f}% across the "
        f"{len(reports_list)} variants.",
        f"- Variants whose final equity is known under strict marking: "
        f"{len(known_final)} of {len(reports_list)}"
        + (f" ({', '.join(known_final)})" if known_final else
           " - the strict equity series is unknown at the end for every variant, so no "
           "variant has a strict final equity level to report."),
        "- Sessions on which no equity total could be published at all, because a holding "
        "could not be priced (variant: sessions): "
        + ", ".join(f"`{r['variant_id']}` {r['sessions_where_no_total_could_be_published']}"
                    for r in reports_list) + ".",
        "- Sessions on which an unknown held opening mark froze new buying (variant: sessions): "
        + ", ".join(f"`{name}` {count}" for name, count in frozen) + ".",
        "- Dominant reason for an unpriced holding (position-days, summed over all variants): "
        + ", ".join(
            f"`{reason}` {sum(r['unpriced_reason_counts'].get(reason, 0) for r in reports_list)}"
            for reason in UNPRICED_REASONS
            if sum(r["unpriced_reason_counts"].get(reason, 0) for r in reports_list)) + ".",
        "- The quarantine policy changes selection, not just the audit trail: compare the "
        "`trades` and `sessions_with_unknown_positions` columns of the two policy blocks "
        "above and the per-year tables below.",
        "",
    ]
    lines += _render_exposure_disclosure(summary, reports_list)
    lines += _render_policy_comparison(summary)
    lines += [
        "## Per-year coverage of unknown-position sessions",
        "",
        "Cells are `sessions with at least one unpriced holding / sessions`. A year with "
        "`0/0` was never simulated. Full detail for every variant is in "
        "`data/processed/backtest/sensitivity/unknown_coverage_by_year.csv`.",
        "",
    ]
    years = sorted({int(year) for report in reports_list
                    for year in report["unknown_coverage_by_year"]})
    for policy in summary["grid"]["policies"]:
        subset = [r for r in reports_list if r["policy"] == policy]
        if not subset:
            continue
        lines += [f"### policy `{policy}`", "",
                  "| variant | " + " | ".join(str(y) for y in years) + " |",
                  "|---" * (len(years) + 1) + "|"]
        for report in subset:
            cells = []
            for year in years:
                values = report["unknown_coverage_by_year"].get(str(year))
                if not values:
                    cells.append("-")
                    continue
                cells.append(f"{values['sessions_with_unknown_positions']}/{values['sessions']}")
            lines.append(f"| `{report['variant_id']}` | " + " | ".join(cells) + " |")
        lines.append("")
    lines += [
        "## Why holdings could not be priced (position-days)",
        "",
        "Counts of position-days with no mark. `no_bar_in_date_local_eq_research_universe` "
        "means the security had no row in the date-local EQ/INE research universe that "
        "session - a series migration (EQ to BE), an ISIN outside the INE set or a missing "
        "bar - and the runner never invents an exit for it.",
        "",
        "| variant | " + " | ".join(UNPRICED_REASONS) + " | total unknown position-days |",
        "|---" * (len(UNPRICED_REASONS) + 2) + "|",
    ]
    for report in reports_list:
        reasons = report["unpriced_reason_counts"]
        cells = [str(reasons.get(reason, 0)) for reason in UNPRICED_REASONS]
        lines.append(f"| `{report['variant_id']}` | " + " | ".join(cells)
                     + f" | {report['unknown_position_instances']} |")
    lines += [
        "",
        "## Skip counts by variant",
        "",
        "| variant | " + " | ".join(c.strip() for c in COMPARISON_COLUMNS
                                   if c.startswith("skip_")) + " |",
        "|---" * (1 + len([c for c in COMPARISON_COLUMNS if c.startswith("skip_")])) + "|",
    ]
    for report in reports_list:
        cells = [str(report["skip_counts"].get(c[len("skip_"):], 0)) for c in COMPARISON_COLUMNS
                 if c.startswith("skip_")]
        lines.append(f"| `{report['variant_id']}` | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Unresolved input counts (diagnostics, never proof of coverage)",
        "",
        "```json",
        json.dumps(summary["input_risk_counts"], indent=2),
        "```",
        "",
        "## Caveats",
        "",
    ]
    for limitation in summary["limitations"]:
        lines.append(f"- {limitation}")
    lines += [
        "",
        "## Provenance (input hashes)",
        "",
        "| input | bytes | sha256 |",
        "|---|---|---|",
    ]
    for item in summary["provenance"]["inputs"]:
        lines.append(f"| `{item['path']}` | {item['bytes']:,} | `{item['sha256']}` |")
    lines += [
        "",
        f"- Database opened read-only: {summary['provenance']['database_read_only']}; "
        f"DuckDB memory limit {summary['provenance']['duckdb_memory_limit']}; "
        f"threads {summary['provenance']['duckdb_threads']}.",
        f"- Sessions simulated: {summary['coverage']['sessions_simulated']}; streamed join rows: "
        f"{summary['coverage']['streamed_join_rows']:,}.",
        f"- Analysis digest (sha256 over the deterministic part of the summary): "
        f"`{summary['determinism']['analysis_digest']}`. Two runs over the same inputs produce "
        f"the same digest and byte-identical CSV and Markdown outputs; only "
        f"{', '.join('`' + field + '`' for field in summary['determinism']['volatile_fields'])} "
        f"differ between runs. The wall clock of this run is in `summary.json` under "
        f"`provenance.elapsed_seconds`.",
        "",
        "## Reproduce",
        "",
        "```powershell",
        "$env:PYTHONPATH = \"src\"",
        ".\\.venv\\Scripts\\python.exe -m nse_pipeline.backtest.sensitivity "
        f"--end {summary['end']} --allow-incomplete",
        "```",
        "",
        "Without `--allow-incomplete` the same command exits **2** while readiness blockers "
        "remain and writes no trading output.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sensitivity / robustness diagnostic for the 2000-2026 momentum strategy "
                    "(incomplete-data diagnostic; never alpha).")
    parser.add_argument("--data", type=Path, default=data_root())
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--reports", type=Path, default=None)
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="Explicit diagnostic consent while readiness blockers remain")
    parser.add_argument("--cash", type=float, default=10_000_000.0)
    parser.add_argument("--lookback", type=int, action="append", default=None,
                        help="momentum lookback in sessions (repeatable; default 21/63/126)")
    parser.add_argument("--top-n", type=int, action="append", default=None,
                        help="portfolio breadth (repeatable; default 10/20/40)")
    parser.add_argument("--policy", action="append", default=None,
                        choices=list(DEFAULT_POLICIES),
                        help="unresolved-event policy (repeatable; default both)")
    parser.add_argument("--max-variants", type=int, default=DEFAULT_MAX_VARIANTS,
                        help="cap on the number of portfolios in the grid")
    parser.add_argument("--time-budget-seconds", type=float, default=DEFAULT_TIME_BUDGET_SECONDS,
                        help="wall-clock cap for the single streaming pass")
    parser.add_argument("--no-key-stabilization", action="store_true",
                        help="Reproduce the pre-stabilization keys (diagnostic "
                             "before/after switch; never a completeness waiver)")
    args = parser.parse_args(argv)

    try:
        summary = run_sensitivity(
            args.data, args.start, args.end, out=args.out, reports=args.reports,
            allow_incomplete=args.allow_incomplete, initial_cash=args.cash,
            lookbacks=args.lookback or DEFAULT_LOOKBACKS,
            top_ns=args.top_n or DEFAULT_TOP_NS,
            policies=args.policy or DEFAULT_POLICIES,
            max_variants=args.max_variants,
            time_budget_seconds=args.time_budget_seconds,
            use_key_stabilization=not args.no_key_stabilization)
    except SensitivityReadinessError as exc:
        print(json.dumps({
            "status": "refused",
            "reason": "readiness_blockers_remain",
            "blocker_codes": exc.blocker_codes,
            "ready_for_backtesting": False,
            "ready_for_alpha_claim": False,
            "note": ("Strict by default. Re-run with --allow-incomplete to produce an "
                     "incomplete-data diagnostic, which still never claims alpha."),
        }, indent=2))
        return 2
    print(json.dumps({
        "status": "ok",
        "mode": summary["mode"],
        "ready_for_backtesting": summary["ready_for_backtesting"],
        "ready_for_alpha_claim": summary["ready_for_alpha_claim"],
        "total_return": summary["total_return"],
        "allow_incomplete": summary["allow_incomplete"],
        "variants_run": summary["grid"]["variants_run"],
        "window_truncated": summary["grid"]["window_truncated"],
        "analysis_digest": summary["determinism"]["analysis_digest"],
        "elapsed_seconds": round(summary["provenance"]["elapsed_seconds"], 1),
        "outputs": summary["outputs"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
