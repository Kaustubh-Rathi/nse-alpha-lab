"""CANONICAL BACKTEST ENGINE. Momentum rotation with configurable cadence, entry timing and exit overlays.

Status
------
This is **the** backtest engine of the research pipeline. Every published result
in ``research/`` is produced by this module:

* ``research/run_sweep.py`` imports ``EngineConfig``, ``BarsFrame`` and
  ``MomentumRotationEngine`` from here (via ``backtest.momentum_grid``).
* ``research/run_alpha_backtest.py`` does the same.

``nse_pipeline.backtest.engine`` is a **separate legacy engine** and is NOT part
of this result path; see the note at the bottom of this docstring.

Why this module is canonical
----------------------------
``engine.py`` marks a holding with no bar at its last observed close forever and
carries no explicit refuse-to-report state, so its headline numbers are a
marking artefact rather than a measurement. This engine instead refuses to
invent a price (rule 2 below), applies an explicit ``DELISTING_POLICIES``
choice, and publishes its cost model as data (``COST_COMPONENTS``). Those three
properties are why the research sweep uses this module.

See ``docs/ENGINE.md`` for the full comparison, the test map, and the migration
status of the legacy engine.

Design rules, all enforced in code rather than documented and hoped for:

1. **No look-ahead.** A signal computed from session T's close may only be
   executed at session T+1's open (``entry_timing="next_open"``). The
   ``same_close`` mode exists so the size of that assumption can be measured,
   and it is labelled optimistic because it fills at a price the decision
   already knew.
2. **No invented prices.** A holding with no bar on a session is flagged, never
   marked at a stale price, never marked at zero and never silently dropped.
   The equity curve records how many positions were unpriceable.
3. **No invented corporate actions.** This engine consumes an already-adjusted
   price series. It does not apply splits, bonuses or dividends itself; that is
   the caller's normalisation step and its provenance must be recorded.
4. **Deterministic ordering.** Ranking ties break on the security key, so two
   runs over the same inputs produce byte-identical output.
5. **Integer shares.** Fractional shares are never created.

The engine is pure: it takes a prepared frame and returns results. It performs
no I/O and reads no clock.

Relationship to ``engine.py`` (legacy)
--------------------------------------
``backtest/engine.py`` is a predecessor implementation, not a component of this
one: neither module imports the other (verified by grep over ``src/``, ``tests/``,
``research/`` and ``scripts/``). It remains in the tree because its loaders
(``load_adjusted_bars``, ``load_bars_from_duckdb``), its identity-continuity
helpers (``apply_identity_continuity``, ``build_key_stabilization``) and its
``RotationBacktester`` are still imported by ``history.py``, ``sensitivity.py``,
``total_return.py``, ``successor.py``, ``series_fallback.py`` and by the legacy
tests. Deleting it would break that subtree, so it is documented as legacy
rather than removed. Do not add new callers: new work belongs here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd

from .cadence import CadenceSelection, select_rebalance_dates
from .delisting import terms_for
from .exits import ExitPolicy, describe as describe_exit, evaluate_exit

ZERO = Decimal(0)

ENTRY_TIMINGS = ("next_open", "same_close")

#: ``top`` buys the strongest trailing return; ``bottom`` buys the weakest.
#: The latter is a reversal counterfactual, not a recommendation.
SELECTION_DIRECTIONS = ("top", "bottom")

#: Every charge the engine applies, in the order reported.
COST_COMPONENTS = ("brokerage", "gst", "stt", "stamp", "exchange", "slippage")

#: How a holding that stops trading is treated.
#:
#: ``strict``
#:     The position stays held and unpriceable. Equity becomes unknown from that
#:     session on and no return is computed. This is the most conservative
#:     choice and it is the default.
#: ``last_close_writeoff``
#:     The position is exited at its last observed close, once, on the first
#:     session where it has no bar. The exit is recorded with
#:     ``reason="delisting_last_close_writeoff"`` so every affected trade is
#:     auditable. This is an assumption, not a fact: the true terminal proceeds
#:     of a delisted security are unknown. Results produced under this policy
#:     must be labelled as carrying that assumption.
#: ``reason_haircut``
#:     The position is exited at its last observed close **multiplied by a
#:     reason-conditioned haircut** from :mod:`nse_pipeline.backtest.delisting`,
#:     following Shumway (1997). A security with no recognised delisting reason
#:     is refused rather than haircut, so the assumption is never invented.
DELISTING_POLICIES = ("strict", "last_close_writeoff", "reason_haircut")


@dataclass(frozen=True)
class EngineConfig:
    """Everything that changes a result, in one hashable place."""

    cadence: str = "monthly"
    #: Optional explicit rebalance signal dates. When set, they replace the
    #: cadence-derived calendar. This is for evidence-dated rebalance chains
    #: (for example official membership anchors) where a calendar cadence would
    #: invent dates that no source proves. Dates with no observed session are
    #: ignored by the run loop and reported through the cadence block.
    rebalance_dates: tuple[date, ...] | None = None
    top_n: int = 20
    #: ``top`` holds the highest trailing return; ``bottom`` holds the lowest.
    #: The bottom variant exists to test the sign of the momentum effect.
    direction: str = "top"
    lookback_sessions: int = 63
    entry_timing: str = "next_open"
    exit_policy: ExitPolicy = field(default_factory=ExitPolicy)
    delisting_policy: str = "strict"
    #: ``{symbol: record}`` from NSE's delisted register, required by
    #: ``delisting_policy="reason_haircut"``. A symbol absent from the register
    #: is refused, never haircut by default.
    delisting_register: dict | None = None
    initial_cash: Decimal = Decimal("10000000")
    min_price: Decimal = Decimal("5")
    min_volume: int = 5000
    min_days_observed: int = 130
    brokerage_bps: Decimal = Decimal("2")
    brokerage_cap_per_order: Decimal = Decimal("2000")
    stt_bps_buy: Decimal = Decimal("10")
    stt_bps_sell: Decimal = Decimal("10")
    stamp_bps: Decimal = Decimal("1.5")
    exchange_bps: Decimal = Decimal("0.3")
    gst_on_brokerage_pct: Decimal = Decimal("18")
    slippage_bps: Decimal = Decimal("5")

    def __post_init__(self) -> None:
        if self.entry_timing not in ENTRY_TIMINGS:
            raise ValueError(
                f"entry_timing must be one of {ENTRY_TIMINGS}, got {self.entry_timing!r}")
        if self.delisting_policy not in DELISTING_POLICIES:
            raise ValueError(
                f"delisting_policy must be one of {DELISTING_POLICIES}, "
                f"got {self.delisting_policy!r}")
        if self.direction not in SELECTION_DIRECTIONS:
            raise ValueError(
                f"direction must be one of {SELECTION_DIRECTIONS}, got {self.direction!r}")
        if self.top_n < 1:
            raise ValueError("top_n must be at least 1")
        if self.lookback_sessions < 1:
            raise ValueError("lookback_sessions must be at least 1")
        if self.initial_cash <= ZERO:
            raise ValueError("initial_cash must be positive")
        if self.rebalance_dates is not None:
            if not self.rebalance_dates:
                raise ValueError("rebalance_dates must be non-empty when provided")
            if len(set(self.rebalance_dates)) != len(self.rebalance_dates):
                raise ValueError("rebalance_dates contains duplicates")
            if any(type(value) is not date for value in self.rebalance_dates):
                raise ValueError("rebalance_dates must contain datetime.date values")

    def cost_components(self, notional: Decimal, side: str) -> dict[str, Decimal]:
        """Itemised cash cost of a trade, charged separately from notional.

        Slippage is charged as an explicit cash amount rather than moved into
        the fill price. For a fixed order size the two are equivalent in cash
        terms; keeping it separate makes the drag auditable.
        """
        if notional <= ZERO:
            return {name: ZERO for name in COST_COMPONENTS}
        is_buy = side.lower().startswith("buy")
        stt = self.stt_bps_buy if is_buy else self.stt_bps_sell
        brokerage = min(notional * self.brokerage_bps / Decimal(10_000),
                        self.brokerage_cap_per_order)
        gst = brokerage * self.gst_on_brokerage_pct / Decimal(100)
        per_bps = notional / Decimal(10_000)
        return {
            "brokerage": brokerage,
            "gst": gst,
            "stt": per_bps * stt,
            "stamp": per_bps * self.stamp_bps if is_buy else ZERO,
            "exchange": per_bps * self.exchange_bps,
            "slippage": per_bps * self.slippage_bps,
        }

    def cost_for(self, notional: Decimal, side: str) -> Decimal:
        """Cash cost of a trade, charged separately from notional."""
        return sum(self.cost_components(notional, side).values(), ZERO)

    @property
    def label(self) -> str:
        """Stable identifier for a result row."""
        suffix = "" if self.delisting_policy == "strict" else f"-{self.delisting_policy}"
        direction = "-bottom" if self.direction == "bottom" else ""
        return (f"{self.cadence}-N{self.top_n}-L{self.lookback_sessions}"
                f"-{self.entry_timing}-{describe_exit(self.exit_policy)}"
                f"{direction}{suffix}")

    def manifest(self) -> dict:
        """JSON-safe description of every setting that affects the result."""
        return {
            "cadence": self.cadence,
            "rebalance_calendar": ("explicit_dates" if self.rebalance_dates
                                   else self.cadence),
            "rebalance_dates_count": (len(self.rebalance_dates)
                                      if self.rebalance_dates else None),
            "top_n": self.top_n,
            "selection_direction": self.direction,
            "lookback_sessions": self.lookback_sessions,
            "entry_timing": self.entry_timing,
            "execution_price": ("signal_session_close"
                                if self.entry_timing == "same_close"
                                else "next_session_open"),
            # ``same_close`` is an optimistic counterfactual: the fill uses the
            # close that formed the ranking. It is not an executable strategy.
            "lookahead": self.entry_timing == "same_close",
            "delisting_policy": self.delisting_policy,
            "exit_policy": {
                "stop_loss_pct": (str(self.exit_policy.stop_loss_pct)
                                  if self.exit_policy.stop_loss_pct is not None else None),
                "take_profit_pct": (str(self.exit_policy.take_profit_pct)
                                    if self.exit_policy.take_profit_pct is not None else None),
                "mode": self.exit_policy.mode,
            },
            "initial_cash": str(self.initial_cash),
            "min_price": str(self.min_price),
            "min_volume": self.min_volume,
            "min_days_observed": self.min_days_observed,
            "costs_bps": {
                "brokerage": str(self.brokerage_bps),
                "brokerage_cap_per_order": str(self.brokerage_cap_per_order),
                "stt_buy": str(self.stt_bps_buy),
                "stt_sell": str(self.stt_bps_sell),
                "stamp_buy_only": str(self.stamp_bps),
                "exchange": str(self.exchange_bps),
                "gst_on_brokerage_pct": str(self.gst_on_brokerage_pct),
                "slippage": str(self.slippage_bps),
            },
        }


class BarsFrame:
    """A validated, indexed view over prepared daily bars.

    Required columns: ``date``, ``key``, ``open``, ``close``, ``volume``.
    Optional: ``low``, ``high``, ``symbol``, ``valid_ohlcv``.

    ``key`` is the stable security identity. The caller is responsible for
    having resolved renames, mergers and ISIN changes into one key per
    economic security before constructing this frame.
    """

    REQUIRED = ("date", "key", "open", "close", "volume")

    def __init__(self, frame: pd.DataFrame):
        missing = [column for column in self.REQUIRED if column not in frame.columns]
        if missing:
            raise ValueError(f"bars frame is missing required columns: {missing}")
        prepared = frame.copy()
        prepared["date"] = pd.to_datetime(prepared["date"]).dt.date
        if prepared.duplicated(["date", "key"]).any():
            raise ValueError(
                "duplicate (date, key) bars: resolve identity or series ambiguity "
                "before running the engine")
        prepared = prepared.sort_values(["date", "key"]).reset_index(drop=True)
        self._frame = prepared
        self._by_date = {d: g for d, g in prepared.groupby("date")}
        self._sessions = sorted(self._by_date)
        self._session_index = {s: i for i, s in enumerate(self._sessions)}
        # Cumulative observed-session counts are needed only at signal dates,
        # but the first implementation materialised one dict per session (about
        # 18 million entries for a 14-year, 5.8m-row run). Per-key sorted
        # session-number arrays carry the same information in ~23 MB and let
        # ``counts_as_of`` answer with a binary search.
        ordered = prepared.sort_values(["key", "date"])
        session_numbers = ordered["date"].map(self._session_index)
        self._key_sessions = {
            key: group.to_numpy(dtype=np.int32, copy=True)
            for key, group in session_numbers.groupby(ordered["key"], sort=False)
        }
        self._last_session = dict(prepared.groupby("key")["date"].max())

    @property
    def sessions(self) -> list[date]:
        return self._sessions

    @property
    def frame(self) -> pd.DataFrame:
        return self._frame

    def day(self, session: date) -> pd.DataFrame:
        return self._by_date.get(session, self._frame.iloc[0:0])

    def session_at(self, index: int) -> date | None:
        if index < 0 or index >= len(self._sessions):
            return None
        return self._sessions[index]

    def index_of(self, session: date) -> int | None:
        return self._session_index.get(session)

    def counts_as_of(self, session: date) -> dict[str, int]:
        """Observed-session count per key up to and including ``session``."""
        position = self._session_index.get(session)
        if position is None:
            return {}
        counts: dict[str, int] = {}
        for key, sessions in self._key_sessions.items():
            count = int(np.searchsorted(sessions, position, side="right"))
            if count:
                counts[key] = count
        return counts

    def has_bar_after(self, key: str, session: date) -> bool:
        """True when ``key`` has any bar strictly after ``session``.

        This is what distinguishes a suspension (the security resumes) from a
        delisting (it never trades again in this run).
        """
        return key in self._last_session and self._last_session[key] > session


@dataclass
class _Position:
    shares: int
    avg_entry: Decimal
    last_seen: date
    flag: str = "active"
    symbol: str = ""


def _decimal(value) -> Decimal | None:
    if value is None or pd.isna(value):
        return None
    result = Decimal(str(float(value)))
    return result if result > ZERO else None


class MomentumRotationEngine:
    """Rank by trailing return, hold the top N, rebalance on a cadence."""

    def __init__(self, config: EngineConfig):
        self.config = config

    # -- signal ---------------------------------------------------------

    def eligible(self, bars: "BarsFrame", session: date) -> pd.DataFrame:
        """Point-in-time eligible set: only symbols observed on this session."""
        day = bars.day(session)
        if day.empty:
            return day
        mask = (day["volume"] >= self.config.min_volume) & (day["volume"] > 0)
        if "valid_ohlcv" in day.columns:
            mask &= day["valid_ohlcv"].fillna(False).astype(bool)
        closes = day["close"].astype(float)
        mask &= closes >= float(self.config.min_price)
        return day[mask]

    def rank(self, bars: "BarsFrame", session: date) -> pd.DataFrame:
        """Trailing-return rank at ``session``; empty when history is insufficient."""
        index = bars.index_of(session)
        if index is None or index < self.config.lookback_sessions:
            return bars.frame.iloc[0:0]
        anchor = bars.session_at(index - self.config.lookback_sessions)
        current = self.eligible(bars, session)
        if current.empty:
            return current
        prior = bars.day(anchor)[["key", "close"]].rename(
            columns={"close": "anchor_close"})
        merged = current.merge(prior, on="key", how="inner", validate="one_to_one")
        merged = merged[(merged["anchor_close"] > 0) & (merged["close"] > 0)]
        if self.config.min_days_observed > 1:
            counts = bars.counts_as_of(session)
            merged = merged[merged["key"].map(counts).fillna(0)
                            >= self.config.min_days_observed]
        merged = merged.assign(
            momentum=merged["close"].astype(float) / merged["anchor_close"].astype(float) - 1.0)
        return merged.sort_values(["momentum", "key"], ascending=[False, True],
                                  kind="stable")

    def targets(self, bars: "BarsFrame", session: date,
                universe: dict | None = None) -> list[str]:
        """Selected keys at ``session``, restricted to ``universe[session]`` when given.

        ``top`` returns the strongest trailing returns, ``bottom`` the weakest.
        ``universe`` maps a signal date to the set of keys that were members on
        that date. A key absent from the mapping is not eligible, so a missing
        membership date can never silently widen the universe.
        """
        ranked = self.rank(bars, session)
        if ranked.empty:
            return []
        if universe is not None:
            members = universe.get(session)
            if not members:
                return []
            ranked = ranked[ranked["key"].isin(members)]
            if ranked.empty:
                return []
        if self.config.direction == "bottom":
            # ``rank`` is strongest-first; reverse it with the same deterministic
            # key tie-break so two runs can never disagree on the selection.
            ranked = ranked.sort_values(["momentum", "key"], ascending=[True, True],
                                        kind="stable")
        return ranked["key"].tolist()[: self.config.top_n]

    # -- execution ------------------------------------------------------

    def run(self, bars: BarsFrame, universe: dict | None = None) -> dict:
        config = self.config
        if config.rebalance_dates:
            selection = CadenceSelection(
                tuple(sorted(set(config.rebalance_dates))), ())
        else:
            selection = select_rebalance_dates(bars.sessions, config.cadence)

        cash = config.initial_cash
        positions: dict[str, _Position] = {}
        trades: list[dict] = []
        skips: list[dict] = []
        exits: list[dict] = []
        equity_rows: list[dict] = []
        last_close: dict[str, Decimal] = {}
        pending_exit: dict[str, str] = {}

        # Map each rebalance signal date to its execution session.
        exec_map: dict[date, date | None] = {}
        for signal_date in selection.dates:
            index = bars.index_of(signal_date)
            if index is None:
                continue
            if config.entry_timing == "next_open":
                exec_map[signal_date] = bars.session_at(index + 1)
            else:
                exec_map[signal_date] = signal_date

        planned: dict[date, list[tuple[str, date]]] = {}
        for signal_date in selection.dates:
            execution = exec_map.get(signal_date)
            if execution is None:
                continue
            planned.setdefault(execution, []).extend(
                (key, signal_date)
                for key in self.targets(bars, signal_date, universe))

        for session in bars.sessions:
            day = bars.day(session)
            # Only the current holdings, today's pending exits and today's
            # target names are ever read from this session. Building a
            # name->row dict for every symbol on every session dominated run
            # time (about 5.8m namedtuples per config) without changing any
            # decision the loop can make.
            needed = set(positions)
            needed.update(pending_exit)
            needed.update(key for key, _ in planned.get(session, ()))
            lookup = ({row.key: row
                       for row in day[day["key"].isin(needed)].itertuples(index=False)}
                      if needed else {})

            def open_of(key: str) -> Decimal | None:
                row = lookup.get(key)
                return _decimal(getattr(row, "open", None)) if row is not None else None

            def close_of(key: str) -> Decimal | None:
                row = lookup.get(key)
                return _decimal(getattr(row, "close", None)) if row is not None else None

            def rebalance_fill(key: str) -> Decimal | None:
                # ``same_close`` deliberately fills at the signal session's
                # close, which is the price the ranking already knew. It is an
                # optimistic assumption, not an executable price. ``next_open``
                # fills at the following session's open. The two modes must
                # never be mixed: using today's open after ranking on today's
                # close would trade on a price that predates the signal.
                if config.entry_timing == "same_close":
                    return close_of(key)
                return open_of(key)

            # 1. Exit overlays act on the previous session's completed close and
            #    fill at this session's open. A breach detected today cannot fill
            #    today in next_open mode.
            if config.exit_policy.active:
                for key, reason in list(pending_exit.items()):
                    if key not in positions:
                        del pending_exit[key]
                        continue
                    fill = open_of(key)
                    if fill is None:
                        skips.append({"date": session, "key": key, "side": "sell",
                                      "reason": "no_bar_for_exit"})
                        continue
                    cash, trade = self._sell(key, fill, session, cash, positions,
                                             reason=reason)
                    trades.append(trade)
                    exits.append({"date": session, "key": key, "reason": reason,
                                  "fill_price": str(fill)})
                    del pending_exit[key]

            # 2. Scheduled rebalance.
            if session in planned:
                items = list(dict.fromkeys(planned[session]))
                target_keys = [key for key, _ in items]
                signal_of = {key: signal_date for key, signal_date in items}

                # An empty target list means the universe was unproven at this
                # rebalance, not that the strategy wanted zero holdings. Selling
                # everything here would liquidate the book on a data gap and
                # book the result as a strategy outcome, so the book is held
                # unchanged instead and the gap is recorded.
                if not target_keys:
                    skips.append({"date": session, "key": None, "side": "rebalance",
                                  "reason": "no_proven_universe_hold_unchanged"})
                    items = []

                for key in (list(positions) if target_keys else []):
                    if key in target_keys:
                        continue
                    fill = rebalance_fill(key)
                    if fill is None:
                        skips.append({"date": session, "key": key, "side": "sell",
                                      "reason": "no_bar_delisted_or_suspended"})
                        positions[key].flag = "delisted_or_suspended"
                        continue
                    cash, trade = self._sell(key, fill, session, cash, positions,
                                             reason="rebalance")
                    trades.append(trade)

                equity = cash + sum(
                    (position.shares * (rebalance_fill(key) or last_close.get(key, ZERO))
                     for key, position in positions.items()), ZERO)
                if target_keys:
                    per_target = equity / Decimal(len(target_keys))
                    for key in target_keys:
                        fill = rebalance_fill(key)
                        if fill is None:
                            skips.append({"date": session, "key": key, "side": "buy",
                                          "reason": "no_bar_at_execution"})
                            continue
                        held = (positions[key].shares * fill
                                if key in positions else ZERO)
                        need = per_target - held
                        if need <= ZERO:
                            continue
                        shares = int(min(need, cash) / fill)
                        while shares > 0:
                            notional = Decimal(shares) * fill
                            if notional + config.cost_for(notional, "buy") <= cash:
                                break
                            shares -= 1
                        if shares <= 0:
                            skips.append({"date": session, "key": key, "side": "buy",
                                          "reason": "insufficient_cash"})
                            continue
                        notional = Decimal(shares) * fill
                        cost = config.cost_for(notional, "buy")
                        cash -= notional + cost
                        trades.append({
                            "date": session, "signal_date": signal_of.get(key),
                            "key": key, "side": "buy", "fill_price": str(fill),
                            "shares": shares, "notional": str(notional),
                            "cost": str(cost), "reason": "rebalance"})
                        if key in positions:
                            existing = positions[key]
                            total = existing.shares + shares
                            existing.avg_entry = (
                                (Decimal(existing.shares) * existing.avg_entry
                                 + Decimal(shares) * fill) / Decimal(total))
                            existing.shares = total
                            existing.last_seen = session
                            existing.flag = "active"
                        else:
                            symbol = ""
                            row = lookup.get(key)
                            if row is not None:
                                symbol = str(getattr(row, "symbol", "") or "")
                            positions[key] = _Position(shares, fill, session,
                                                       symbol=symbol)

            # 3. Mark to close and evaluate the exit overlay for tomorrow.
            for key in positions:
                close = close_of(key)
                if close is not None:
                    last_close[key] = close
                    positions[key].last_seen = session
                    positions[key].flag = "active"

            if config.exit_policy.active:
                for key, position in positions.items():
                    if key in pending_exit:
                        continue
                    close = close_of(key)
                    if close is None:
                        continue
                    row = lookup.get(key)
                    signal = evaluate_exit(
                        config.exit_policy,
                        entry_price=position.avg_entry,
                        close=close,
                        low=_decimal(getattr(row, "low", None)) if row is not None else None,
                        high=_decimal(getattr(row, "high", None)) if row is not None else None,
                    )
                    if signal is None:
                        continue
                    if signal.executes_next_open:
                        pending_exit[key] = signal.reason
                    else:
                        # intraday_stop: fill at the trigger, this session.
                        trigger = signal.trigger_price
                        cash, trade = self._sell(key, trigger, session, cash, positions,
                                                 reason=signal.reason)
                        trades.append(trade)
                        exits.append({"date": session, "key": key,
                                      "reason": signal.reason,
                                      "fill_price": str(trigger)})

            priced = sum(1 for key in positions if close_of(key) is not None)
            unpriced = len(positions) - priced

            # A suspended holding (no bar today, but it trades again) is marked at
            # its last observed close. That is a stale mark, not an unknown one:
            # the security exists and has a price, it simply did not trade. It is
            # counted separately so the staleness stays visible.
            stale = 0
            if unpriced:
                for key in positions:
                    if close_of(key) is not None:
                        continue
                    if bars.has_bar_after(key, session) and key in last_close:
                        stale += 1

            # 4. Delisting policy. A holding with no bar this session may be
            #    suspended for a day, not delisted. Only a security that has no
            #    further bar anywhere in the run is treated as delisted; a
            #    temporary gap keeps the position and its last known close.
            if unpriced and config.delisting_policy in (
                    "last_close_writeoff", "reason_haircut"):
                for key in list(positions):
                    if close_of(key) is not None:
                        continue
                    if bars.has_bar_after(key, session):
                        # Suspended, not delisted: hold and keep the last close.
                        continue
                    position = positions[key]
                    mark = last_close.get(key)
                    if mark is None:
                        skips.append({"date": session, "key": key, "side": "sell",
                                      "reason": "delisted_without_any_price"})
                        continue

                    reason = "delisting_last_close_writeoff"
                    assumption = ("exited at last observed close; the true terminal "
                                  "proceeds are unknown")
                    fill = mark

                    if config.delisting_policy == "reason_haircut":
                        record = (config.delisting_register or {}).get(
                            position.symbol.upper() if position.symbol else key)
                        terms = terms_for(record["reason_raw"] if record else None)
                        if terms is None:
                            skips.append({
                                "date": session, "key": key, "side": "sell",
                                "reason": "delisting_reason_unknown_no_haircut_applied",
                                "symbol": position.symbol})
                            continue
                        fill = terms.exit_price(mark)
                        reason = f"delisting_{terms.reason}"
                        assumption = terms.citation

                    if fill <= ZERO:
                        # A total loss: the position is written off, not sold.
                        del positions[key]
                        exits.append({"date": session, "key": key, "reason": reason,
                                      "fill_price": "0", "assumption": assumption,
                                      "last_close": str(mark)})
                        continue

                    cash, trade = self._sell(key, fill, session, cash, positions,
                                             reason=reason)
                    trades.append(trade)
                    exits.append({"date": session, "key": key, "reason": reason,
                                  "fill_price": str(fill), "assumption": assumption,
                                  "last_close": str(mark)})
                priced = sum(1 for key in positions if close_of(key) is not None)
                unpriced = len(positions) - priced
                stale = 0
                if unpriced:
                    for key in positions:
                        if close_of(key) is not None:
                            continue
                        if bars.has_bar_after(key, session) and key in last_close:
                            stale += 1

            value = sum((position.shares * last_close[key]
                         for key, position in positions.items()
                         if key in last_close), ZERO)
            # Equity is known when every holding has a price. A *suspended*
            # holding (trades again later) may be marked at its last observed
            # close. A *delisted* holding (never trades again) may not: marking it
            # at a stale close forever is exactly the optimistic bias Shumway
            # (1997) documents, so under 'strict' it makes equity unknown.
            unpriceable = 0
            for key in positions:
                if close_of(key) is not None:
                    continue
                if bars.has_bar_after(key, session) and key in last_close:
                    continue  # suspended: stale mark is admissible
                if config.delisting_policy != "strict" and key in last_close:
                    continue  # the delisting policy will resolve it below
                unpriceable += 1
            equity_rows.append({
                "date": session,
                "cash": str(cash),
                "positions_value": str(value),
                "equity": str(cash + value) if unpriceable == 0 else None,
                "n_positions": len(positions),
                "unpriced_positions": unpriceable,
                "stale_marked_positions": stale,
            })

        return {
            "config": config.manifest(),
            "label": config.label,
            "cadence": {
                "dates": [d.isoformat() for d in selection.dates],
                "skipped_periods": [list(key) for key in selection.skipped_periods],
                "complete": selection.complete,
            },
            "equity": equity_rows,
            "trades": trades,
            "skips": skips,
            "exits": exits,
            "final_positions": [
                {"key": key, "shares": position.shares,
                 "avg_entry": str(position.avg_entry),
                 "last_seen": position.last_seen.isoformat(),
                 "flag": position.flag}
                for key, position in positions.items()],
            "final_cash": str(cash),
        }

    def _sell(self, key: str, fill: Decimal, session: date, cash: Decimal,
              positions: dict[str, _Position], *, reason: str):
        position = positions[key]
        notional = Decimal(position.shares) * fill
        cost = self.config.cost_for(notional, "sell")
        cash += notional - cost
        trade = {"date": session, "signal_date": None, "key": key, "side": "sell",
                 "fill_price": str(fill), "shares": position.shares,
                 "notional": str(notional), "cost": str(cost), "reason": reason}
        del positions[key]
        return cash, trade
