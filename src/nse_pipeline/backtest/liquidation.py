"""Estimated, hypothetical liquidation of supplied sell orders.

The estimate never touches authoritative accounting. Prices, sell-side cost
schedules and capacity must be dated and available at the decision. Slippage is
adverse. Fees are charged once per order. Unknown price or capacity yields
``None`` rather than a zero recovery, and no fill is fabricated when capacity is
unknown or zero.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from nse_pipeline.specs import CostSpec
from .accounting_types import EventTime, ZERO, decimal, identifier, integer, money
from .fees import fee_breakdown


@dataclass(frozen=True)
class LiquidationCostSchedule:
    schedule_id: str
    effective_from: date
    effective_to: date | None
    available_at: EventTime
    citation: str
    costs: CostSpec
    adverse_slippage_bps: Decimal

    def __post_init__(self):
        identifier(self.schedule_id)
        identifier(self.citation)
        if type(self.effective_from) is not date:
            raise ValueError("A cost schedule needs an effective date")
        if self.effective_to is not None:
            if (type(self.effective_to) is not date
                    or self.effective_to < self.effective_from):
                raise ValueError("effective_to must be on or after effective_from")
        if not isinstance(self.available_at, EventTime):
            raise ValueError("A cost schedule needs an availability event time")
        if not isinstance(self.costs, CostSpec):
            raise ValueError("A cost schedule needs a typed CostSpec")
        object.__setattr__(self, "adverse_slippage_bps",
                           decimal(self.adverse_slippage_bps))
        if self.adverse_slippage_bps >= 10000:
            raise ValueError("Adverse slippage must leave a positive sell price")

    def covers(self, on: date) -> bool:
        return (self.effective_from <= on
                and (self.effective_to is None or on <= self.effective_to))


@dataclass(frozen=True)
class SaleOrder:
    order_id: str
    security_key: str
    quantity: int
    position_id: str = ""

    def __post_init__(self):
        identifier(self.order_id)
        identifier(self.security_key)
        integer(self.quantity, minimum=1)


@dataclass(frozen=True)
class LiquidationPrice:
    security_key: str
    price: Decimal | None
    on: date
    available_at: EventTime
    citation: str

    def __post_init__(self):
        identifier(self.security_key)
        identifier(self.citation)
        if type(self.on) is not date or not isinstance(self.available_at, EventTime):
            raise ValueError("A liquidation price needs a session and availability time")
        if self.available_at.session < self.on:
            raise ValueError("A liquidation price cannot be available before its session")
        if self.price is not None:
            object.__setattr__(self, "price", decimal(self.price, positive=True))


@dataclass(frozen=True)
class LiquidationCapacity:
    security_key: str
    as_of: date
    available_at: EventTime
    capacity_shares: int | None
    citation: str

    def __post_init__(self):
        identifier(self.security_key)
        identifier(self.citation)
        if type(self.as_of) is not date or not isinstance(self.available_at, EventTime):
            raise ValueError("Capacity needs an as-of date and availability time")
        if self.capacity_shares is not None:
            integer(self.capacity_shares)


@dataclass(frozen=True)
class LiquidationLine:
    order_id: str
    requested_quantity: int
    filled_quantity: int
    reference_price: Decimal | None
    slipped_price: Decimal | None
    gross: Decimal | None
    fees: Decimal | None
    net: Decimal | None
    status: str

    def __post_init__(self):
        identifier(self.order_id)
        integer(self.requested_quantity, minimum=1)
        integer(self.filled_quantity)
        if self.filled_quantity > self.requested_quantity:
            raise ValueError("Liquidation fill exceeds the supplied order")
        if self.status not in ("filled", "capacity_limited", "zero_capacity",
                               "capacity_unknown", "unpriced"):
            raise ValueError("Unknown liquidation line status")


@dataclass(frozen=True)
class LiquidationEstimate:
    lines: tuple[LiquidationLine, ...]
    status: str
    gross_proceeds: Decimal | None
    fees: Decimal | None
    net_proceeds: Decimal | None
    zero_friction_subtotal: Decimal | None
    unknown_reasons: tuple[str, ...]
    decision_on: date
    schedule_id: str
    hypothetical: bool = True
    authoritative: bool = False
    claims_created: int = 0

    def __post_init__(self):
        object.__setattr__(self, "lines", tuple(self.lines))
        object.__setattr__(self, "unknown_reasons", tuple(sorted(set(self.unknown_reasons))))
        if self.status not in ("estimated", "unknown"):
            raise ValueError("Unknown liquidation estimate status")
        if self.hypothetical is not True or self.authoritative is not False:
            raise ValueError("A liquidation estimate is hypothetical and not authoritative")
        if self.claims_created != 0:
            raise ValueError("A liquidation estimate never creates accounting claims")

    @property
    def authoritative_nav(self):
        return None

    def require_authoritative(self):
        raise ValueError("liquidation_estimate_is_not_authoritative")

    def authoritative_statistics(self):
        raise ValueError("liquidation_estimate_is_not_authoritative")


def select_cost_schedule(schedules, *, on: date, decision_at: EventTime) -> LiquidationCostSchedule:
    if type(on) is not date or not isinstance(decision_at, EventTime):
        raise ValueError("Cost-schedule selection needs an explicit date and decision event")
    schedules = tuple(schedules)
    if not all(isinstance(schedule, LiquidationCostSchedule) for schedule in schedules):
        raise ValueError("Cost schedules must be typed records")
    available = tuple(schedule for schedule in schedules
                      if schedule.covers(on) and schedule.available_at <= decision_at)
    if not available:
        raise ValueError("cost_schedule_unavailable")
    latest = max(schedule.effective_from for schedule in available)
    winners = tuple(schedule for schedule in available if schedule.effective_from == latest)
    if len({(schedule.schedule_id, schedule.costs, schedule.adverse_slippage_bps)
            for schedule in winners}) > 1:
        raise ValueError("ambiguous_cost_schedule")
    return winners[0]


def _index(records, name):
    indexed = {}
    for record in records:
        if record.security_key in indexed:
            raise ValueError(f"Ambiguous multiple {name} records for one security")
        indexed[record.security_key] = record
    return indexed


def estimate_liquidation(orders, *, prices, capacities, schedules, on: date,
                         decision_at: EventTime) -> LiquidationEstimate:
    """Per-order, fee-once, capacity-limited hypothetical sell-side estimate."""
    if type(on) is not date or not isinstance(decision_at, EventTime):
        raise ValueError("A liquidation estimate needs an explicit date and decision event")
    orders = tuple(orders)
    if not all(isinstance(order, SaleOrder) for order in orders):
        raise ValueError("Liquidation inputs must be typed SaleOrder records")
    if len({order.order_id for order in orders}) != len(orders):
        raise ValueError("Liquidation order IDs must be unique")
    schedule = select_cost_schedule(schedules, on=on, decision_at=decision_at)
    prices = _index(tuple(prices), "price")
    capacities = _index(tuple(capacities), "capacity")

    lines = []
    unknown = []
    for order in orders:
        price = prices.get(order.security_key)
        priced = (price is not None and price.price is not None
                  and price.on == on and price.available_at <= decision_at)
        if not priced:
            unknown.append(f"{order.security_key}:unpriced")
            lines.append(LiquidationLine(order.order_id, order.quantity, 0, None, None,
                                         None, None, None, "unpriced"))
            continue
        capacity = capacities.get(order.security_key)
        usable = (capacity is not None and capacity.capacity_shares is not None
                  and capacity.as_of == on and capacity.available_at <= decision_at)
        if not usable:
            unknown.append(f"{order.security_key}:capacity_unknown")
            lines.append(LiquidationLine(order.order_id, order.quantity, 0,
                                         price.price, None, None, None, None,
                                         "capacity_unknown"))
            continue
        capacity_shares = capacity.capacity_shares
        filled = min(order.quantity, capacity_shares)
        if filled == 0:
            lines.append(LiquidationLine(order.order_id, order.quantity, 0,
                                         price.price, None, ZERO, ZERO, ZERO,
                                         "zero_capacity"))
            continue
        slipped = price.price * (1 - schedule.adverse_slippage_bps / Decimal(10000))
        gross = money(slipped * filled)
        fees = fee_breakdown(gross, "sell", schedule.costs)
        lines.append(LiquidationLine(
            order.order_id, order.quantity, filled, price.price, slipped,
            gross, fees.total, gross - fees.total,
            "filled" if filled == order.quantity else "capacity_limited"))

    lines = tuple(lines)
    if unknown:
        return LiquidationEstimate(
            lines, "unknown", None, None, None, None, tuple(unknown), on,
            schedule.schedule_id)
    gross = sum((line.gross for line in lines), ZERO)
    fees = sum((line.fees for line in lines), ZERO)
    net = sum((line.net for line in lines), ZERO)
    zero_friction = sum((money(line.reference_price * line.filled_quantity)
                         for line in lines if line.filled_quantity), ZERO)
    return LiquidationEstimate(lines, "estimated", gross, fees, net, zero_friction, (),
                               on, schedule.schedule_id)
