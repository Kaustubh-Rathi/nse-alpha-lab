"""Leakage-proof causality suite for the momentum rotation engine.

Every test here protects one invariant that a backtest must satisfy before its
numbers can be believed. Together they answer a single question: *could any
decision the engine made have been changed by information that did not exist
at the moment the decision was made?*

The suite is deliberately written against the real engine API in
:mod:`nse_pipeline.backtest.momentum` -- ``MomentumRotationEngine``,
``EngineConfig`` and ``BarsFrame`` -- rather than against a description of it.
Synthetic bars are used so that every causal property can be isolated from the
data's noise.

The five invariants:

1. ``test_future_perturbation_invariance``  -- perturbing anything after the
   decision horizon T cannot change what was decided up to T.
2. ``test_exact_anchor_momentum``            -- the momentum score is exactly
   ``close(T)/close(T-63)-1`` and reads no future close.
3. ``test_point_in_time_universe``           -- membership is enforced per
   signal date, and a missing membership date holds the book instead of
   liquidating it.
4. ``test_exit_fill_uses_next_session_open`` -- a stop breach on T fills at the
   open of T+1 and is insensitive to T+1's close.
5. ``test_delisting_writeoff_is_not_future_data_dependent`` -- documented
   specification assertion exposing known risk R4 (currently expected to fail).
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from nse_pipeline.backtest.exits import ExitPolicy
from nse_pipeline.backtest.momentum import (
    BarsFrame,
    EngineConfig,
    MomentumRotationEngine,
)

D = Decimal

#: The engine's default lookback. Tests that assert the anchor formula use the
#: default so that the assertion fails if the default ever silently changes.
LOOKBACK = 63


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def session_series(start: date, count: int) -> list[date]:
    """Consecutive calendar days used as exchange sessions.

    The engine treats whatever dates it is given as sessions, so a synthetic
    calendar is enough and keeps the tests fast and readable.
    """
    return [start + timedelta(days=index) for index in range(count)]


def price_row(session: date, key: str, open_: float, close: float,
              volume: int = 100_000) -> dict:
    """One synthetic OHLCV bar with a degenerate but self-consistent range."""
    return {"date": session, "key": key, "open": open_, "close": close,
            "volume": volume, "low": min(open_, close),
            "high": max(open_, close)}


def linear_bars(sessions, slopes=None, base: float = 100.0) -> list[dict]:
    """Bars whose price rises linearly, so momentum ordering is unambiguous.

    ``slopes`` maps a security key to its per-session price increment. A key
    with a larger slope has strictly stronger momentum at every rankable date.
    """
    slopes = slopes or {"A": 1.0, "B": 0.6, "C": 0.2}
    rows = []
    for index, session in enumerate(sessions):
        for key, slope in slopes.items():
            price = base + slope * index
            rows.append(price_row(session, key, price, price))
    return rows


def truncated(rows: list[dict], cutoff: date) -> list[dict]:
    """Every bar on or before ``cutoff``: the data a run at T could have seen."""
    return [row for row in rows if row["date"] <= cutoff]


def scale_prices(row: dict, factor: float) -> dict:
    """Multiply a bar's whole price column set by ``factor``."""
    scaled = dict(row)
    for column in ("open", "close", "low", "high"):
        scaled[column] = scaled[column] * factor
    return scaled


@pytest.fixture()
def trailing_exit_bars():
    """A rises, then crashes; B and C keep rising.

    Returns the bars and the session whose close first breaches a 10% stop on
    A's average entry. The crash is far steeper than any other move in the
    series, so the breach session is unambiguous. Each bar after the peak opens
    at the prior close and then moves intraday, which makes the open and the
    close of the fill session genuinely different: a fill at the open cannot be
    mistaken for a fill at the close.
    """
    sessions = session_series(date(2020, 1, 1), 240)
    rows = []
    previous_a_open = None
    for index, session in enumerate(sessions):
        rising = 100.0 + index
        crashing = max(180.0 - (index - 80) * 2.0, 5.0)
        a_price = rising if index <= 80 else crashing
        # The open is the previous session's close; the close is the level.
        a_open = previous_a_open if previous_a_open is not None else a_price
        rows.append(price_row(session, "A", a_open, a_price))
        previous_a_open = a_price
        for key, price in (("B", 100.0 + 0.6 * index),
                           ("C", 100.0 + 0.2 * index)):
            rows.append(price_row(session, key, price, price))
    return rows, sessions


# ---------------------------------------------------------------------------
# 1. Future-perturbation invariance
# ---------------------------------------------------------------------------

def test_future_perturbation_invariance():
    """INVARIANT: no decision dated on or before T may depend on data after T.

    Two runs are compared. The first sees only bars up to and including T --
    everything a real portfolio could have known on the evening of T. The
    second sees the same history but with every later bar multiplied by a
    random factor (fixed seed) and T's own close scrambled while T's open is
    preserved.

    Because the last observation the first run can make is T's close, and the
    second run's T close differs, the invariant is stated precisely:

    * every trade and exit dated strictly before T is identical, including
      side, key, fill price, share count and charges;
    * every trade and exit dated on T is identical *up to the close-driven
      exit overlay*: the engine may legitimately flag a stop on T's close, but
      it must not fill it on T, because a breach detected on the close can
      only execute at the next session's open;
    * the equity prefix up to and including T is identical, since on T only
      the open and (for marking) the close of held names are read;
    * no trade is ever dated after T in the truncated run, which proves the
      engine cannot reach forward.
    """
    cutoff = date(2020, 6, 30)
    # The fixture is built so all three decision paths are exercised before the
    # cutoff, which is what makes this comparison sensitive instead of cosmetic:
    #
    # * A and B rank within a hair of each other on the April signal date, so a
    #   one-session peek at their next opens flips which one gets bought;
    # * B (originally the leader) crashes hard after the entry, so a stop on B
    #   fires before the cutoff and the exit path is compared too;
    # * C is flat enough to never be selected, giving a stable control name.
    sessions = session_series(date(2020, 1, 1), 240)
    all_rows = []
    for index, session in enumerate(sessions):
        a_price = 101.4 + index
        b_price = 100.0 + index
        if index > 100:
            b_price = max(200.0 - (index - 100) * 3.0, 5.0)
        c_price = 100.0 + 0.2 * index
        for key, price in (("A", a_price), ("B", b_price), ("C", c_price)):
            all_rows.append(price_row(session, key, price, price))
    config = EngineConfig(cadence="monthly", lookback_sessions=LOOKBACK,
                          min_days_observed=1, top_n=1,
                          # A stop makes the comparison cover exits as well as
                          # entries, so the invariant is tested on both paths.
                          exit_policy=ExitPolicy(stop_loss_pct=D("0.10")))

    truncated_run = MomentumRotationEngine(config).run(
        BarsFrame(pd.DataFrame(truncated(all_rows, cutoff))))

    rng = random.Random(20240101)
    perturbed_rows = []
    for row in all_rows:
        if row["date"] > cutoff:
            perturbed_rows.append(scale_prices(row, rng.uniform(0.4, 2.5)))
        elif row["date"] == cutoff:
            # Keep the open, which any run already knew before T, so that every
            # open-driven fill on T is unchanged. Wreck the close, which is the
            # newest fact the truncated run can possibly use, by scaling it down
            # so far that a close-driven stop fires. The two reference runs then
            # legitimately disagree about whether T's close breached a stop,
            # which is exactly the same-session lookahead the assertions exclude
            # (never fill a T-close breach on T).
            scrambled = dict(row)
            scrambled["close"] = row["close"] * 0.5
            scrambled["low"] = min(row["open"], scrambled["close"])
            scrambled["high"] = max(row["open"], scrambled["close"])
            perturbed_rows.append(scrambled)
        else:
            perturbed_rows.append(dict(row))
    perturbed_run = MomentumRotationEngine(config).run(
        BarsFrame(pd.DataFrame(perturbed_rows)))

    # Invariance can be proven trivially if the fixture never decides anything,
    # so the fixture itself is asserted to exercise the paths it claims to.
    assert truncated_run["trades"], "fixture produced no trades; nothing was tested"
    assert any(trade["date"] < cutoff for trade in truncated_run["trades"])
    assert any(trade["side"] == "sell" for trade in truncated_run["trades"]), (
        "fixture produced no exit before the cutoff; the exit path is untested")
    assert any(trade["side"] == "buy" and trade["date"] > cutoff - timedelta(days=70)
               for trade in truncated_run["trades"]), (
        "fixture produced no entry in the final period before the cutoff; the "
        "decision nearest the horizon is untested")

    def normalise(trade: dict) -> tuple:
        return (trade["date"], trade["key"], trade["side"],
                trade["fill_price"], trade["shares"], trade["notional"],
                trade["cost"], trade["signal_date"], trade["reason"])

    # Every trade and exit dated strictly before T, including the final
    # rebalance before the horizon, must be byte-identical across the two runs.
    before = [normalise(t) for t in truncated_run["trades"] if t["date"] < cutoff]
    later = [normalise(t) for t in perturbed_run["trades"] if t["date"] < cutoff]
    assert before == later

    # Exits before T are equally frozen.
    exits_before = [tuple(sorted(e.items())) for e in truncated_run["exits"]
                    if e["date"] < cutoff]
    exits_later = [tuple(sorted(e.items())) for e in perturbed_run["exits"]
                   if e["date"] < cutoff]
    assert exits_before == exits_later

    # The truncated run cannot trade into the future at all.
    assert max(trade["date"] for trade in truncated_run["trades"]) <= cutoff

    # Equity up to T: same cash, same position count, same known/unknown state.
    def equity_prefix(run: dict) -> list[tuple]:
        # ``positions_value`` on T itself is a mark at T's close, and T's close
        # is exactly the field this test scrambles, so it is compared only up
        # to (excluding) T. Cash, position count and the known/unknown state
        # are facts about decisions and must match through T inclusive.
        return [(row["date"], row["cash"], row["n_positions"],
                 row["unpriced_positions"], row["stale_marked_positions"],
                 row["positions_value"] if row["date"] < cutoff else None)
                for row in run["equity"] if row["date"] <= cutoff]

    assert equity_prefix(truncated_run) == equity_prefix(perturbed_run)

    # On T itself the open-driven fills must match exactly. Any T-dated trade
    # must be an entry filled on T's open, never a same-day stop fill.
    def on_cutoff(run: dict) -> list[tuple]:
        return [normalise(t) for t in run["trades"] if t["date"] == cutoff]

    assert on_cutoff(truncated_run) == on_cutoff(perturbed_run)


# ---------------------------------------------------------------------------
# 2. Exact anchor
# ---------------------------------------------------------------------------

def test_exact_anchor_momentum():
    """INVARIANT: momentum at T is exactly close(T)/close(T-63)-1.

    The lookback anchor is an index offset into the observed session list, not
    a calendar subtraction: session T-63 must mean 63 *sessions* earlier. The
    test also proves the rank at T is a function of history only, by scaling
    every bar after T and re-ranking.
    """
    sessions = session_series(date(2020, 1, 1), 240)
    bars = BarsFrame(pd.DataFrame(linear_bars(sessions)))
    engine = MomentumRotationEngine(
        EngineConfig(lookback_sessions=LOOKBACK, min_days_observed=1, top_n=3))

    position = 150
    signal_date = sessions[position]
    anchor_date = bars.session_at(position - LOOKBACK)
    assert anchor_date == sessions[position - LOOKBACK]
    # The anchor is 63 sessions back even though the calendar gap is longer or
    # shorter than 63 days; the engine must use sessions, not calendar days.
    assert (signal_date - anchor_date).days == LOOKBACK

    ranked = engine.rank(bars, signal_date)
    assert not ranked.empty
    assert ranked["key"].tolist() == ["A", "B", "C"]

    closes_today = bars.day(signal_date).set_index("key")["close"]
    closes_anchor = bars.day(anchor_date).set_index("key")["close"]
    for key in ("A", "B", "C"):
        expected = float(closes_today[key]) / float(closes_anchor[key]) - 1.0
        actual = ranked.loc[ranked["key"] == key, "momentum"].iloc[0]
        assert actual == pytest.approx(expected, rel=0, abs=1e-12), key

    # Changing every close after T must not move the rank at T by even a bit.
    future_scaled = [scale_prices(row, 3.7) if row["date"] > signal_date
                     else dict(row) for row in linear_bars(sessions)]
    rescored = MomentumRotationEngine(
        EngineConfig(lookback_sessions=LOOKBACK, min_days_observed=1,
                     top_n=3)).rank(BarsFrame(pd.DataFrame(future_scaled)),
                                    signal_date)
    assert rescored["key"].tolist() == ranked["key"].tolist()
    assert rescored["momentum"].tolist() == ranked["momentum"].tolist()

    # One session short of a full lookback: no rank is possible at all, so the
    # engine cannot quietly shorten the window to look at less data.
    assert engine.rank(bars, sessions[LOOKBACK - 1]).empty


# ---------------------------------------------------------------------------
# 3. Point-in-time universe
# ---------------------------------------------------------------------------

def test_point_in_time_universe():
    """INVARIANT: selection is restricted to members known on the signal date.

    B outranks A on d1, but only A is a member on d1. The engine must therefore
    buy A and must never buy B on d1's execution session. On a second signal
    date with no membership evidence the book must be *held unchanged*, not
    liquidated: an unproven universe is a data gap, not a sell signal.
    """
    sessions = session_series(date(2020, 1, 1), 240)
    # B overtakes A so that the ranking and the membership disagree: the
    # point-in-time filter, not the momentum score, must decide this entry.
    bars = BarsFrame(pd.DataFrame(
        linear_bars(sessions, slopes={"A": 0.6, "B": 1.0, "C": 0.2})))
    d1, d2, d3 = date(2020, 6, 30), date(2020, 7, 31), date(2020, 8, 31)
    engine = MomentumRotationEngine(EngineConfig(
        cadence="monthly", lookback_sessions=LOOKBACK, min_days_observed=1,
        top_n=1, rebalance_dates=(d1, d2, d3)))

    # The premise of the test: unfiltered, B beats A on d1.
    unfiltered = engine.rank(bars, d1)["key"].tolist()
    assert unfiltered[0] == "B"
    assert engine.targets(bars, d1) == ["B"]
    # ... and the membership filter overrides the ranking.
    assert engine.targets(bars, d1, universe={d1: {"A"}}) == ["A"]
    # A member set never admits a non-member, even when the non-member is the
    # strongest name on the date.
    for members in ({"A"}, {"C"}, {"A", "C"}):
        selected = engine.targets(bars, d1, universe={d1: members})
        assert set(selected) <= members
        assert "B" not in selected
    # An empty membership set selects nothing at all.
    assert engine.targets(bars, d1, universe={d1: set()}) == []
    # A signal date absent from the mapping selects nothing rather than
    # silently widening to the full universe.
    assert engine.targets(bars, d1, universe={d2: {"B"}}) == []

    buy_execution = bars.session_at(bars.index_of(d1) + 1)
    hold_execution = bars.session_at(bars.index_of(d2) + 1)
    result = engine.run(bars, universe={d1: {"A"}, d2: set()})

    on_buy_day = [t for t in result["trades"] if t["date"] == buy_execution]
    assert on_buy_day, "expected an entry on d1's execution session"
    assert [t["key"] for t in on_buy_day] == ["A"]
    assert all(t["side"] == "buy" for t in on_buy_day)
    assert all(t["signal_date"] == d1 for t in on_buy_day)

    # The stronger-but-non-member B is never bought on d1's execution session,
    # at any price and for any reason.
    assert not any(t["key"] == "B" and t["date"] == buy_execution
                   for t in result["trades"])

    # With no membership for d2 the book is held: no sell, and an explicit,
    # machine-readable skip instead of a silent liquidation.
    on_hold_day = [t for t in result["trades"] if t["date"] == hold_execution]
    assert on_hold_day == []
    hold_skips = [s for s in result["skips"]
                  if s["date"] == hold_execution and s["side"] == "rebalance"]
    assert [s["reason"] for s in hold_skips] == [
        "no_proven_universe_hold_unchanged"]
    assert hold_skips[0]["key"] is None
    assert [p["key"] for p in result["final_positions"]] == ["A"]

    # Even when a future date's membership is supplied, it may not retroactively
    # admit B on d1: the universe is keyed by signal date, not merged.
    future_aware = engine.run(bars, universe={d1: {"A"}, d2: {"B"}, d3: {"B"}})
    assert not any(t["key"] == "B" and t["date"] == buy_execution
                   for t in future_aware["trades"])


# ---------------------------------------------------------------------------
# 4. Exit fill price
# ---------------------------------------------------------------------------

def test_exit_fill_uses_next_session_open(trailing_exit_bars):
    """INVARIANT: a stop breached on T's close fills at T+1's open.

    The breach is detected on a *completed* session's close, so the earliest
    executable price is the next session's open. The fill must equal that open
    exactly, must be dated T+1 (never T), and must be unchanged when T+1's own
    close is perturbed, because the exit price cannot depend on a close that
    printed after the order was filled.
    """
    rows, sessions = trailing_exit_bars
    config = EngineConfig(cadence="monthly", lookback_sessions=LOOKBACK,
                          min_days_observed=1, top_n=2,
                          exit_policy=ExitPolicy(stop_loss_pct=D("0.10")))
    engine = MomentumRotationEngine(config)
    result = engine.run(BarsFrame(pd.DataFrame(rows)))

    stops = [e for e in result["exits"] if e["reason"] == "stop_loss"]
    assert stops, "fixture did not breach the stop; nothing was tested"
    stop = stops[0]
    exit_session = stop["date"]
    exit_index = [i for i, s in enumerate(sessions) if s == exit_session][0]
    breach_session = sessions[exit_index - 1]

    bars = BarsFrame(pd.DataFrame(rows))
    breach_close = bars.day(breach_session).set_index("key").loc["A", "close"]
    entry = next(t for t in result["trades"]
                 if t["key"] == "A" and t["side"] == "buy")
    entry_price = D(entry["fill_price"])
    # The breach really happened on the breach session's close, not earlier.
    assert D(str(breach_close)) <= entry_price * (D(1) - D("0.10"))

    open_after_breach = bars.day(exit_session).set_index("key").loc["A", "open"]
    sell = next(t for t in result["trades"]
                if t["key"] == "A" and t["side"] == "sell")
    assert sell["date"] == exit_session
    assert sell["date"] == exit_session > breach_session
    assert D(sell["fill_price"]) == D(str(open_after_breach))
    # The fill is the open, not the close: the two differ in this fixture, so
    # the assertion above cannot pass by coincidence.
    assert D(sell["fill_price"]) != D(str(
        bars.day(exit_session).set_index("key").loc["A", "close"]))

    # Perturbing the fill session's close must not move the fill, the shares or
    # the date. A same-day-close fill would change here.
    perturbed = []
    for row in rows:
        if row["date"] == exit_session and row["key"] == "A":
            moved = dict(row)
            moved["close"] = row["close"] * 0.25
            moved["low"] = min(row["open"], moved["close"])
            moved["high"] = max(row["open"], moved["close"])
            perturbed.append(moved)
        else:
            perturbed.append(dict(row))
    rerun = engine.run(BarsFrame(pd.DataFrame(perturbed)))
    rerun_sell = next(t for t in rerun["trades"]
                      if t["key"] == "A" and t["side"] == "sell")
    assert rerun_sell["date"] == sell["date"]
    assert rerun_sell["fill_price"] == sell["fill_price"]
    assert rerun_sell["shares"] == sell["shares"]


# ---------------------------------------------------------------------------
# 5. Delisting stability (known risk R4)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=False,
    reason=(
        "Exposes known risk R4: `last_close_writeoff` decides that a security "
        "is delisted by asking whether a *later* bar exists anywhere in the "
        "supplied frame (BarsFrame.has_bar_after). Truncating the frame at T "
        "removes that future bar, so the truncated run writes the position "
        "off at T while the full run holds it. The writeoff is therefore "
        "future-data dependent and prefix runs are not comparable. Marked "
        "non-strict so it flips to a pass once the engine refuses or flags "
        "this case."
    ),
)
def test_delisting_writeoff_is_not_future_data_dependent():
    """INVARIANT (specification): truncated and full runs agree, or refuse.

    ``last_close_writeoff`` must decide delisting from information available
    at the decision date. Today the engine asks ``has_bar_after(key, session)``
    over the whole supplied frame, which is a lookahead by construction: the
    answer changes when bars after T are appended.

    The test truncates the frame at T with a holding that has no bar on T, then
    appends a later bar and re-runs. A compliant engine either produces
    identical decisions up to T, or explicitly records that the result is
    future-data dependent (a flag, a refusal, or a skip reason). Producing a
    *different* decision with no flag is the failure this test documents.
    """
    sessions = session_series(date(2020, 1, 1), 240)
    cutoff = date(2020, 7, 15)
    # A trades on every session except T; B trades throughout.
    rows = []
    for index, session in enumerate(sessions):
        if session != cutoff:
            price = 100.0 + index
            rows.append(price_row(session, "A", price, price))
        rows.append(price_row(session, "B", 100.0, 100.0))

    config = EngineConfig(cadence="monthly", lookback_sessions=20,
                          min_days_observed=1, top_n=2,
                          delisting_policy="last_close_writeoff")

    truncated_run = MomentumRotationEngine(config).run(
        BarsFrame(pd.DataFrame(truncated(rows, cutoff))))
    full_run = MomentumRotationEngine(config).run(BarsFrame(pd.DataFrame(rows)))

    def decisions(run: dict) -> list[tuple]:
        return [(t["date"], t["key"], t["side"], t["fill_price"], t["reason"])
                for t in run["trades"] if t["date"] <= cutoff]

    def dated_exits(run: dict) -> list[tuple]:
        return [(e["date"], e["key"], e["reason"]) for e in run["exits"]
                if e["date"] <= cutoff]

    # Compile the engine's own machine-readable evidence that the run is
    # future-data dependent, so the test can accept a documented refusal.
    flagged = False
    for key in ("future_data_dependent", "future_data_dependent_decisions"):
        flagged = flagged or bool(full_run.get(key)) or bool(
            truncated_run.get(key))
    for skip in truncated_run.get("skips", []) + full_run.get("skips", []):
        if "future" in str(skip.get("reason", "")).lower():
            flagged = True
    for exit_row in truncated_run.get("exits", []) + full_run.get("exits", []):
        if "future" in str(exit_row.get("assumption", "")).lower():
            flagged = True

    same = (decisions(truncated_run) == decisions(full_run)
            and dated_exits(truncated_run) == dated_exits(full_run))

    # The run must at minimum have made a decision worth comparing.
    assert decisions(full_run) or decisions(truncated_run)

    assert same or flagged, (
        "truncating the frame at T changed the decisions dated on or before T, "
        "and the engine did not flag the run as future-data dependent.\n"
        f"truncated trades: {decisions(truncated_run)}\n"
        f"full trades:      {decisions(full_run)}\n"
        f"truncated exits:  {dated_exits(truncated_run)}\n"
        f"full exits:       {dated_exits(full_run)}\n"
        "This is known risk R4 in the last_close_writeoff delisting policy.")
