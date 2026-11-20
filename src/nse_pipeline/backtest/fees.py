"""Opening-price proxy, per-order fees, and fee-inclusive integer affordability."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

from nse_pipeline.specs import CostSpec, ExecutionSpec
from .accounting_types import ZERO, decimal, integer, money


@dataclass(frozen=True)
class Fees:
    brokerage: Decimal = ZERO
    gst: Decimal = ZERO
    stt: Decimal = ZERO
    stamp: Decimal = ZERO
    exchange: Decimal = ZERO

    @property
    def components(self):
        return tuple((name, getattr(self, name)) for name in ("brokerage", "gst", "stt", "stamp", "exchange"))

    @property
    def total(self):
        return sum((value for _, value in self.components), ZERO)


@dataclass(frozen=True)
class FillQuote:
    quantity: int
    reference_price: Decimal
    fill_price: Decimal
    notional: Decimal
    fees: Fees
    reason: str = "filled"
    applied_principal_cap: Decimal | None = None


def principal_cap(execution: ExecutionSpec) -> Decimal | None:
    """The declared hard principal cap floored to paise; None means unbounded.

    A hard maximum must never be rounded upward, so this deliberately does not
    use money() (ROUND_HALF_UP).
    """
    if execution.max_order_notional is None:
        return None
    return Decimal(str(execution.max_order_notional)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)


def within_notional_cap(principal, cap) -> bool:
    """Whether a paise-quantized principal respects a floored declared cap."""
    return cap is None or money(principal) <= cap


# affordable_buy's fourth parameter is the as-of capacity budget and retains
# its historical name; the alias reaches the D-01 helper without shadowing.
_floored_principal_cap = principal_cap


def fee_breakdown(notional, side: str, spec: CostSpec) -> Fees:
    if side not in ("buy", "sell"):
        raise ValueError("Unknown trade side")
    amount = money(notional)
    def rate(bps):
        return money(amount * Decimal(str(bps)) / 10000)
    brokerage = money(min(amount * Decimal(str(spec.brokerage_bps)) / 10000,
                          Decimal(str(spec.brokerage_cap_per_order))))
    return Fees(brokerage, money(brokerage * Decimal(str(spec.gst_on_brokerage_pct)) / 100),
                rate(spec.stt_bps_buy if side == "buy" else spec.stt_bps_sell),
                rate(spec.stamp_bps_buy) if side == "buy" else ZERO, rate(spec.exchange_bps))


def opening_quote(side: str, quantity: int, reference_price, costs: CostSpec, execution: ExecutionSpec) -> FillQuote:
    integer(quantity)
    reference = decimal(reference_price, positive=True)
    if side not in ("buy", "sell"):
        raise ValueError("Unknown trade side")
    slippage = execution.adverse_slippage_bps_buy if side == "buy" else -execution.adverse_slippage_bps_sell
    price = reference * (1 + Decimal(str(slippage)) / 10000)
    notional = money(price * quantity)
    return FillQuote(quantity, reference, price, notional, fee_breakdown(notional, side, costs))


def affordable_buy(quantity, reference_price, cash_budget, principal_cap,
                   costs: CostSpec, execution: ExecutionSpec) -> FillQuote:
    integer(quantity, minimum=1)
    cash_budget, capacity = money(cash_budget), money(principal_cap)
    declared_cap = _floored_principal_cap(execution)
    applied_cap = capacity if declared_cap is None else min(capacity, declared_cap)
    low, high = 0, quantity
    while low < high:
        mid = (low + high + 1) // 2
        quoted = opening_quote("buy", mid, reference_price, costs, execution)
        if within_notional_cap(quoted.notional, applied_cap) and quoted.notional + quoted.fees.total <= cash_budget:
            low = mid
        else:
            high = mid - 1
    result = opening_quote("buy", low, reference_price, costs, execution)
    if (result.notional == 0 or result.notional < Decimal(str(execution.min_order_notional))
            or (execution.partial_fill_policy == "skip" and low != quantity)):
        return FillQuote(0, result.reference_price, result.fill_price, ZERO, Fees(),
                         "unaffordable_or_partial_disallowed", applied_cap)
    return FillQuote(result.quantity, result.reference_price, result.fill_price, result.notional,
                     result.fees, result.reason, applied_cap)


def floored_capacity(capacity) -> Decimal:
    """A supplied frozen capacity as exact paise, never rounded upward."""
    return decimal(capacity).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)


def bounded_sell(quantity, reference_price, costs: CostSpec, execution: ExecutionSpec,
                 capacity_notional=None) -> FillQuote:
    integer(quantity, minimum=1)
    # The effective principal cap is the tighter of the declared global hard cap
    # and any caller-supplied frozen per-order cap. Both are floored to paise so a
    # cap can never be rounded upward into an extra share.
    declared_cap = _floored_principal_cap(execution)
    supplied_cap = None if capacity_notional is None else floored_capacity(capacity_notional)
    caps = tuple(cap for cap in (declared_cap, supplied_cap) if cap is not None)
    applied_cap = min(caps) if caps else None
    low, high = 0, quantity
    if applied_cap is not None:
        while low < high:
            mid = (low + high + 1) // 2
            if within_notional_cap(opening_quote("sell", mid, reference_price, costs, execution).notional,
                                   applied_cap):
                low = mid
            else:
                high = mid - 1
    else:
        low = quantity
    result = opening_quote("sell", low, reference_price, costs, execution)
    if (result.notional == 0 or result.notional < Decimal(str(execution.min_order_notional))
            or (execution.partial_fill_policy == "skip" and low != quantity)):
        return FillQuote(0, result.reference_price, result.fill_price, ZERO, Fees(),
                         "notional_limit_or_partial_disallowed", applied_cap)
    return FillQuote(result.quantity, result.reference_price, result.fill_price, result.notional,
                     result.fees, result.reason, applied_cap)
