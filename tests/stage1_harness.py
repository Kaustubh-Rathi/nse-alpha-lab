"""Batch 6 test-only composition harness (no legacy imports).

This module composes the public, in-memory Stage-1 components into a single
causal session flow so the end-to-end, frozen-order and prefix-invariance
contracts can be proven. It is test-only: it imports only
``nse_pipeline.backtest.*`` and ``nse_pipeline.specs`` public boundaries and
never touches ``engine.py``, ``total_return.py``, ``history.py``, ``freeze.py``
or any data-acquisition / DuckDB / pandas path.

The harness owns only *composition*; every economic rule it relies on lives in
the public components (identity keys, observation gating, signal ranking,
universe eligibility, sizing, ordering, settlement and valuation). Desired
notionals, as-of capacity, prices and calendars are caller-supplied inputs from
the hand-derived golden, never invented here. One documented composition step
exists: because ``size_orders`` returns allocated principal notional rather than
a share count, the harness derives the integer quantity as
``floor(allocated_notional / decision_close)`` before reserving, which mirrors
the sizer's own floor and is asserted equal to the golden's hand-derived shares.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction

from nse_pipeline.backtest.accounting_types import EventTime, OpeningPrint, Phase
from nse_pipeline.backtest.core import Portfolio
from nse_pipeline.backtest.evidence import ObservationLedger
from nse_pipeline.backtest.identity import IdentityResolver, Observation
from nse_pipeline.backtest.invariants import reconcile
from nse_pipeline.backtest.observations import Bar, SessionStream
from nse_pipeline.backtest.signals import MomentumRanks, rank_momentum
from nse_pipeline.backtest.sizing import SizingResult, size_orders
from nse_pipeline.backtest.universe import Membership, build_universe
from nse_pipeline.backtest.valuation import Mark, Valuation, value
from nse_pipeline.specs import CostSpec, ExecutionSpec, MomentumSpec, UniverseSpec

ZERO_COSTS = CostSpec(
    brokerage_bps=0, brokerage_cap_per_order=0, stt_bps_buy=0, stt_bps_sell=0,
    stamp_bps_buy=0, exchange_bps=0, gst_on_brokerage_pct=0,
    schedule_basis="flat_research_assumption", monetary_rounding="paise_half_up_per_component")


def execution(*, lag: int = 1, slip_buy: int = 0, slip_sell: int = 0,
              rate: float = 0.05, adv: int = 20) -> ExecutionSpec:
    return ExecutionSpec(
        trade_settlement_lag_sessions=lag,
        adverse_slippage_bps_buy=slip_buy, adverse_slippage_bps_sell=slip_sell,
        sizing_participation_rate=rate, max_participation_rate=rate,
        adv_window_sessions=adv)


def momentum(*, lookback: int = 2, min_obs: int = 2) -> MomentumSpec:
    return MomentumSpec(lookback_sessions=lookback, min_observations=min_obs,
                        price_basis="close")


def universe() -> UniverseSpec:
    return UniverseSpec(
        series=("EQ",), isin_policy="any", min_price=0.0, min_volume=0,
        min_adv_traded_value=0.0, min_avg_trades=0.0, min_adv_window_sessions=2,
        min_listed_sessions=1, label="synthetic research universe (Batch 6 e2e)")


def money(value) -> Decimal:
    """Exact decimal string/number boundary mirror (no floats/bools)."""
    from nse_pipeline.backtest.accounting_types import decimal
    return decimal(value)


@dataclass(frozen=True)
class FixtureResult:
    """Everything a Batch 6 test may inspect after ``run_plan``."""
    book: Portfolio
    ledger: ObservationLedger
    stream: SessionStream
    resolver: IdentityResolver
    membership: Membership | None
    ranks: MomentumRanks | None
    sizing: SizingResult | None
    valuations: dict[str, Valuation]
    reconciliation_ok: bool
    reconciliation_checks: tuple[str, ...]


def _keys(resolver: IdentityResolver, symbols: tuple[str, ...], session: date,
           series_name: str, isin_by_symbol: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for symbol in symbols:
        resolution = resolver.observe(
            Observation(symbol, series_name, isin_by_symbol[symbol], session))
        out[symbol] = resolution.key
    return out


def run_plan(golden: dict, *, stop_at: str | None = None) -> FixtureResult:
    """Execute a declarative fixture and return the composed artifacts.

    ``golden`` is the hand-derived ``tests/golden/e2e_cases.json`` structure.
    ``stop_at`` (an ISO session date) truncates the run to produce a prefix
    artifact set for prefix-invariance comparison; sessions after ``stop_at``
    are not processed.
    """
    spec_momentum = golden["momentum"]
    spec_universe = golden["universe"]
    spec_execution = golden["execution"]
    capacity = money(golden["capacity_notional"])
    initial_cash = money(golden["initial_cash"])
    series = golden["series"]
    series_name = series[0]
    bind_first_isin = golden["bind_first_isin"]
    max_positions = int(golden["max_positions"])

    isin_by_symbol = {row["symbol"]: row["isin"] for row in golden["symbols"]}
    symbols = tuple(isin_by_symbol)

    momentum_spec = MomentumSpec(**spec_momentum)
    universe_spec = UniverseSpec(**spec_universe)
    execution_spec = ExecutionSpec(**spec_execution)

    sessions = golden["sessions"]
    first_date = date.fromisoformat(sessions[0]["date"])
    if stop_at is not None and date.fromisoformat(stop_at) < first_date:
        raise ValueError("stop_at precedes the first session")

    stream = SessionStream()
    ledger = ObservationLedger(stream)
    resolver = IdentityResolver(series=series, bind_first_isin=bind_first_isin)
    book = Portfolio(initial_cash, on=first_date, max_positions=max_positions)

    membership: Membership | None = None
    ranks: MomentumRanks | None = None
    sizing: SizingResult | None = None
    valuations: dict[str, Valuation] = {}
    orders: list[dict] = []  # {order_id, key, rank} reserved at the last decision

    for row in sessions:
        session = date.fromisoformat(row["date"])
        if stop_at is not None and session > date.fromisoformat(stop_at):
            break
        bars = row.get("bars", [])
        key_by_symbol = _keys(resolver, symbols, session, series_name, isin_by_symbol)

        if session != first_date:
            book.observe_session(session)

        stream.observe(session)
        bar_records = tuple(
            Bar(key_by_symbol[bar["symbol"]], bar["symbol"], series_name, session,
                money(bar["open"]), money(bar["high"]), money(bar["low"]),
                money(bar["close"]), int(bar["volume"]), True, isin_by_symbol[bar["symbol"]],
                money(bar["traded_value"]), int(bar["trades"]))
            for bar in bars)
        ledger.ingest(session, bar_records)
        close_by_key = {bar.key: bar.close for bar in bar_records}
        open_by_key = {bar.key: bar.open for bar in bar_records}

        if row.get("decision"):
            decision = row["decision"]
            at_close = EventTime(session, Phase.CLOSE_ORDERS)
            membership = build_universe(ledger, session, universe_spec)
            ranks = rank_momentum(ledger, session, tuple(membership.eligible),
                                  momentum_spec)
            desired = [money(value) for value in decision["desired_notionals"]]
            prices = [close_by_key[key] for key in ranks.order]
            sizing = size_orders("P2", book.balances.cash_available, desired,
                                 price=prices, slots=max_positions,
                                 costs=ZERO_COSTS, execution=execution_spec)
            orders = []
            for index, key in enumerate(ranks.order):
                filled = sizing.filled[index]
                if filled <= 0:
                    continue
                fill_price = close_by_key[key]
                quantity = int(Fraction(filled) / Fraction(fill_price))
                if quantity < 1:
                    continue
                order_id = f"o{index}"
                book.reserve(order_id, key, quantity, filled, capacity,
                             at=EventTime(session, Phase.CLOSE_ORDERS, index),
                             rank=index)
                orders.append({"order_id": order_id, "key": key, "rank": index})

        if row.get("fills"):
            at_open = EventTime(session, Phase.OPEN_FILLS)
            for index, order in enumerate(sorted(orders, key=lambda o: (o["rank"], o["key"], o["order_id"]))):
                key = order["key"]
                book.buy(order["order_id"], OpeningPrint(key, session, open_by_key[key]),
                         ZERO_COSTS, execution_spec, at=EventTime(session, Phase.OPEN_FILLS, index))

        if row.get("settle"):
            at_settle = EventTime(session, Phase.PRE_OPEN_SETTLEMENT)
            sequence = 0
            for claim in tuple(book.snapshot.claims):
                if claim.due is not None and claim.due.reached(at_settle, book.session_index):
                    book.settle(claim.claim_id, at=EventTime(session, Phase.PRE_OPEN_SETTLEMENT, sequence))
                    sequence += 1

        if row.get("valuation"):
            at_value = EventTime(session, Phase.CLOSE_VALUATION)
            marks = tuple(
                Mark(position.position_id, close_by_key[position.key], session, at_value)
                for position in book.snapshot.positions if position.shares > 0)
            valuations[session.isoformat()] = value(book.snapshot, at_value, marks=marks)

    reconciliation = reconcile(book.initial, book.events, book.snapshot,
                               sessions=book.observed_sessions,
                               max_positions=max_positions)
    return FixtureResult(
        book=book, ledger=ledger, stream=stream, resolver=resolver,
        membership=membership, ranks=ranks, sizing=sizing, valuations=valuations,
        reconciliation_ok=reconciliation.passed,
        reconciliation_checks=tuple(check.name for check in reconciliation.checks))
