"""Independent, frozen Stage 1 contracts. Not wired to legacy runners."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from typing import ClassVar

# Long names are the canonical vocabulary of PortfolioSpec, goldens and
# SizingResult.policy. Short P1/P2/P3 aliases exist only at the size_orders
# boundary (D-13); they are never serialized.
P1 = "P1_freeze_on_unresolved"
P2 = "P2_size_excluding_unresolved"
P3 = "P3_size_including_unresolved_at_screen"
CANONICAL_POLICIES = (P1, P2, P3)
LONG_POLICIES = CANONICAL_POLICIES
ALIASES = {"P1": P1, "P2": P2, "P3": P3}


class Specification:
    def to_json(self) -> str:
        return json.dumps({"schema_version": 1, "type": type(self).__name__,
                           "values": asdict(self)}, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_json(cls, payload: str):
        record = json.loads(payload)
        if (not isinstance(record, dict) or set(record) != {"schema_version", "type", "values"}
                or type(record["schema_version"]) is not int or record["schema_version"] != 1
                or record["type"] != cls.__name__ or not isinstance(record["values"], dict)):
            raise ValueError("Wrong specification type or schema version")
        return cls(**record["values"])


def _number(name, value, minimum=0, maximum=None):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value < minimum or (maximum is not None and value > maximum)):
        raise ValueError(f"{name} must be finite and in range [{minimum}, {maximum}]")


def _integer(name, value, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _choice(name, value, choices):
    if value not in choices:
        raise ValueError(f"{name} must be one of {choices}")


def _boolean(name, value):
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True)
class UniverseSpec(Specification):
    series: tuple[str, ...] = ("EQ",)
    isin_policy: str = "ine_or_null"
    min_price: float = 5.0
    min_volume: int = 5000
    min_adv_traded_value: float = 0.0
    min_adv_window_sessions: int = 20
    min_avg_trades: float = 0.0
    min_listed_sessions: int = 63
    exclude_series: tuple[str, ...] = ("BE", "BZ", "SM", "ST", "GB", "GS", "N1", "N2", "N4", "N5", "N6")
    label: str = "research-defined NSE equity (not official Nifty)"

    def __post_init__(self):
        for name in ("series", "exclude_series"):
            values = getattr(self, name)
            if (not isinstance(values, (tuple, list)) or any(not isinstance(x, str) or not x for x in values)
                    or len(set(values)) != len(values)):
                raise ValueError(f"{name} must contain unique nonempty series names")
            object.__setattr__(self, name, tuple(values))
        if not self.series or set(self.series).intersection(self.exclude_series):
            raise ValueError("Active series must be nonempty and not excluded")
        _choice("isin_policy", self.isin_policy, ("ine_or_null", "ine_only", "any"))
        for name in ("min_price", "min_adv_traded_value", "min_avg_trades"):
            _number(name, getattr(self, name))
        _integer("min_volume", self.min_volume, 0)
        _integer("min_listed_sessions", self.min_listed_sessions)
        _integer("min_adv_window_sessions", self.min_adv_window_sessions)
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("label must disclose the universe's research definition")


@dataclass(frozen=True)
class MomentumSpec(Specification):
    schema_version: ClassVar[int] = 2
    lookback_sessions: int = 63
    price_basis: str = "close"
    anchor_rule: str = "exact_session_offset"
    observation_rule: str = "valid_observations"
    min_observations: int | None = None
    tie_break: str = "key_asc"

    def __post_init__(self):
        _integer("lookback_sessions", self.lookback_sessions)
        if self.min_observations is not None:
            upper = self.lookback_sessions + 1
            if type(self.min_observations) is not int or not 1 <= self.min_observations <= upper:
                raise ValueError(
                    f"min_observations must be an integer in [1, {upper}] or None "
                    f"for lookback_sessions={self.lookback_sessions}")
        _choice("price_basis", self.price_basis, ("adj_close", "close", "total_close"))
        _choice("anchor_rule", self.anchor_rule, ("exact_session_offset",))
        _choice("observation_rule", self.observation_rule, ("valid_observations",))
        _choice("tie_break", self.tie_break, ("key_asc",))

    def to_json(self) -> str:
        return json.dumps({"schema_version": self.schema_version, "type": type(self).__name__,
                           "values": asdict(self)}, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_json(cls, payload: str):
        record = json.loads(payload)
        if (not isinstance(record, dict) or record.get("type") != cls.__name__
                or not isinstance(record.get("values"), dict)):
            raise ValueError("Wrong specification type or schema version")
        version = record.get("schema_version")
        if type(version) is int and version == 1:
            raise ValueError(
                "unsupported_spec_schema: MomentumSpec schema 1 is not supported; migrate "
                "to schema 2 (min_observations is window-scoped over lookback_sessions + 1, "
                "None requires the full window, and price_basis defaults to 'close')")
        if type(version) is not int or version != cls.schema_version:
            raise ValueError(
                f"unsupported_spec_schema: MomentumSpec supports schema {cls.schema_version} "
                f"only, not {version!r}")
        return cls(**record["values"])


@dataclass(frozen=True)
class ExecutionSpec(Specification):
    trade_settlement_lag_sessions: int  # explicit research assumption, not an NSE schedule
    fill_timing: str = "next_session_open"
    adverse_slippage_bps_buy: float = 5.0
    adverse_slippage_bps_sell: float = 5.0
    sizing_participation_rate: float = 0.05
    max_participation_rate: float = 0.05
    adv_window_sessions: int = 20
    max_order_notional: float | None = None
    min_order_notional: float = 0.0
    partial_fill_policy: str = "partial"
    settlement_funding: str = "settled_cash_only"
    unfilled_remainder: str = "expire"
    circuit_limit_pct: float = 0.20
    hold_suspended_positions: bool = True

    def __post_init__(self):
        _integer("trade_settlement_lag_sessions", self.trade_settlement_lag_sessions)
        _integer("adv_window_sessions", self.adv_window_sessions)
        _choice("fill_timing", self.fill_timing, ("next_session_open",))
        _choice("partial_fill_policy", self.partial_fill_policy, ("partial", "skip"))
        _choice("settlement_funding", self.settlement_funding, ("settled_cash_only",))
        _choice("unfilled_remainder", self.unfilled_remainder, ("expire",))
        _number("circuit_limit_pct", self.circuit_limit_pct, 0, 1)
        _boolean("hold_suspended_positions", self.hold_suspended_positions)
        for name in ("adverse_slippage_bps_buy", "adverse_slippage_bps_sell"):
            _number(name, getattr(self, name), 0, 9999)
        for name in ("sizing_participation_rate", "max_participation_rate"):
            _number(name, getattr(self, name), 0, 1)
        if self.sizing_participation_rate > self.max_participation_rate:
            raise ValueError("Sizing participation must not exceed the reporting threshold")
        _number("min_order_notional", self.min_order_notional)
        if self.max_order_notional is not None:
            _number("max_order_notional", self.max_order_notional, self.min_order_notional)


@dataclass(frozen=True)
class ValuationPolicy(Specification):
    fallback_lag: int | None
    lag_unit: str
    dividend_settlement_mode: str = "verified_else_lag"
    dividend_cash_phase: str = "post_open_settlement"
    series_precedence: tuple[str, ...] = ("EQ", "BE", "BZ", "SM", "ST")
    carried_position_is: str = "unresolved"
    absence_review_sessions: int = 20
    sizing_uses_screen_value: bool = False
    allow_scenario_statistics: bool = False

    def __post_init__(self):
        _choice("dividend_settlement_mode", self.dividend_settlement_mode,
                ("verified_else_lag", "hold_receivable"))
        _choice("lag_unit", self.lag_unit, ("calendar_days", "exchange_sessions"))
        if self.dividend_settlement_mode == "verified_else_lag":
            _integer("fallback_lag", self.fallback_lag)
        elif self.fallback_lag is not None:
            raise ValueError("hold_receivable must not carry an unused lag")
        _choice("dividend_cash_phase", self.dividend_cash_phase, ("post_open_settlement",))
        _choice("carried_position_is", self.carried_position_is, ("unresolved",))
        _integer("absence_review_sessions", self.absence_review_sessions)
        _boolean("sizing_uses_screen_value", self.sizing_uses_screen_value)
        _boolean("allow_scenario_statistics", self.allow_scenario_statistics)
        if self.allow_scenario_statistics:
            raise ValueError("Scenario-valued series cannot produce performance statistics")
        values = self.series_precedence
        if (not isinstance(values, (tuple, list)) or not values or len(set(values)) != len(values)
                or any(x not in ("EQ", "BE", "BZ", "SM", "ST") for x in values)):
            raise ValueError("Series precedence must contain unique permitted equity series")
        object.__setattr__(self, "series_precedence", tuple(values))


@dataclass(frozen=True)
class PortfolioSpec(Specification):
    max_positions: int = 20
    initial_cash: float = 10_000_000.0
    weighting: str = "equal_weight"
    sizing_policy: str = P2
    allocation_order: str = "rank_then_key"
    disposal_policy: str = "hold_unresolved"
    scenario_only: bool = False
    rebalance_frequency: str = "monthly"
    rebalance_anchor: str = "known_calendar_else_observed_month_change"
    trim_overweight_targets: bool = True

    def __post_init__(self):
        _integer("max_positions", self.max_positions)
        _number("initial_cash", self.initial_cash, 0.01)
        _choice("weighting", self.weighting, ("equal_weight",))
        if self.sizing_policy not in CANONICAL_POLICIES:
            raise ValueError(
                f"unknown_sizing_policy: {self.sizing_policy!r}; expected a canonical long "
                f"name from {CANONICAL_POLICIES}; short P1/P2/P3 aliases are accepted only "
                "at the size_orders boundary")
        _choice("allocation_order", self.allocation_order, ("rank_then_key",))
        _choice("disposal_policy", self.disposal_policy, ("hold_unresolved", "retire_zero"))
        _boolean("scenario_only", self.scenario_only)
        _boolean("trim_overweight_targets", self.trim_overweight_targets)
        if self.disposal_policy == "retire_zero" and not self.scenario_only:
            raise ValueError("Zero-recovery retirement requires a separate scenario portfolio")
        _choice("rebalance_frequency", self.rebalance_frequency, ("monthly",))
        _choice("rebalance_anchor", self.rebalance_anchor, ("known_calendar_else_observed_month_change",))


@dataclass(frozen=True)
class CostSpec(Specification):
    brokerage_bps: float = 2.0
    brokerage_cap_per_order: float = 2000.0
    stt_bps_buy: float = 10.0
    stt_bps_sell: float = 10.0
    stamp_bps_buy: float = 1.5
    exchange_bps: float = 0.3
    gst_on_brokerage_pct: float = 18.0
    schedule_basis: str = "flat_research_assumption"
    monetary_rounding: str = "paise_half_up_per_component"

    def __post_init__(self):
        for name in ("brokerage_bps", "stt_bps_buy", "stt_bps_sell", "stamp_bps_buy", "exchange_bps"):
            _number(name, getattr(self, name), 0, 10000)
        _number("brokerage_cap_per_order", self.brokerage_cap_per_order)
        _number("gst_on_brokerage_pct", self.gst_on_brokerage_pct, 0, 100)
        _choice("schedule_basis", self.schedule_basis, ("flat_research_assumption",))
        _choice("monetary_rounding", self.monetary_rounding, ("paise_half_up_per_component",))


def validate_policy_combination(portfolio: PortfolioSpec, valuation: ValuationPolicy) -> None:
    """Independent specs may not silently combine contradictory sizing policies."""
    if not isinstance(portfolio, PortfolioSpec) or not isinstance(valuation, ValuationPolicy):
        raise ValueError("Policy validation requires a PortfolioSpec and a ValuationPolicy")
    if ((portfolio.sizing_policy == P3) != valuation.sizing_uses_screen_value):
        raise ValueError(
            "screen-value sizing must be explicitly enabled if and only if policy is P3")


def resolve_sizing_policy(portfolio: PortfolioSpec, valuation: ValuationPolicy) -> str:
    """The single cross-spec mapper from PortfolioSpec to a canonical long policy name.

    ``size_orders`` alone never performs this mapping; short P1/P2/P3 aliases are
    deliberately not accepted here because PortfolioSpec rejects them at
    construction and this resolver never coerces an unknown value.
    """
    validate_policy_combination(portfolio, valuation)
    policy = portfolio.sizing_policy
    if policy not in CANONICAL_POLICIES:
        raise ValueError(
            f"unknown_sizing_policy: {policy!r}; expected one of {CANONICAL_POLICIES}")
    return policy


# Imported after the public contracts above so specs.validation can import them.
from .validation import (  # noqa: E402  (deliberate re-export)
    RebalanceScheduleDescriptor,
    resolve_rebalance_schedule,
    validate_run_specs,
)
