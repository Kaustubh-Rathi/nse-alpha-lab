"""Causal trailing ADV/average-trades over observed sessions and conservative sizing capacity.

Absence is not zero trading (D-09). A never-seen key is unknown, an explicit
zero bar is known zero, and a partially observed window reports its partial
estimate with ``partial_coverage`` while capacity stays withheld unless the
caller explicitly opts into the documented verified-listed/no-trade assumption.
No future observation changes a past result.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from datetime import date
from decimal import Decimal
from fractions import Fraction
import math

from nse_pipeline.specs import ExecutionSpec

from .accounting_types import identifier
from .evidence import ObservationLedger

FIELDS = ("traded_value", "trades")
SECURITY_STATUSES = (None, "unknown", "listed", "suspended", "delisted")
MIN_CAPACITY_COVERAGE = Fraction(1, 1)


def exact(value) -> Fraction:
    """Spec thresholds are JSON floats; read them as their shortest decimal literal."""
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Specification numbers must be finite int/float")
    return Fraction(Decimal(repr(value)))


@dataclass(frozen=True)
class TrailingMean:
    key: str
    as_of: date
    field: str
    window_sessions: int
    total: Decimal | None
    traded_sessions: int = 0
    reason: str = ""
    # Diagnostics are informative only; equality keeps the historical value fields.
    observed_sessions: int = dataclass_field(default=0, compare=False)
    missing_sessions: int = dataclass_field(default=0, compare=False)
    zero_volume_sessions: int = dataclass_field(default=0, compare=False)
    invalid_sessions: int = dataclass_field(default=0, compare=False)
    unknown_sessions: int = dataclass_field(default=0, compare=False)
    security_status: str | None = dataclass_field(default=None, compare=False)

    @property
    def exact(self) -> Fraction | None:
        return None if self.total is None else Fraction(self.total) / self.window_sessions

    @property
    def value(self) -> Decimal | None:
        """Mean rounded down to paise (traded value) or to 0.01 trades."""
        mean = self.exact
        return None if mean is None else floor_cents(mean)

    @property
    def coverage(self) -> Fraction:
        """Observed sessions in the window divided by the requested window."""
        return Fraction(self.observed_sessions, self.window_sessions)

    @property
    def partial(self) -> bool:
        """True when a numeric estimate exists but the window was not fully observed."""
        return self.total is not None and self.observed_sessions < self.window_sessions


def floor_cents(value: Fraction) -> Decimal:
    return Decimal(f"{math.floor(value * 100)}E-2")


def _validate_flags(allow_partial_adv: bool, security_status: str | None) -> None:
    if type(allow_partial_adv) is not bool:
        raise ValueError("allow_partial_adv must be a boolean")
    if security_status not in SECURITY_STATUSES:
        raise ValueError(f"security_status must be one of {SECURITY_STATUSES}")


def trailing_mean(ledger: ObservationLedger, key: str, as_of: date, window_sessions: int,
                  field: str = "traded_value", *, allow_partial_adv: bool = False,
                  security_status: str | None = None) -> TrailingMean:
    """Mean over the last N observed sessions ending at as_of inclusive.

    Missing bars are not zero trading. A never-seen key is unknown. A fully
    observed window includes explicit zero values and is a known zero. A partial
    window exposes its estimate but is labelled ``partial_coverage``; missing
    bars are treated as no trade only when the caller explicitly opts into that
    assumption. Invalid bars, unknown fields and non-tradable/unknown security
    status fail closed.
    """
    if not isinstance(ledger, ObservationLedger) or field not in FIELDS:
        raise ValueError(f"Need an ObservationLedger and a field in {FIELDS}")
    _validate_flags(allow_partial_adv, security_status)
    identifier(key)
    window = ledger.window(as_of, window_sessions)
    if window is None:
        return TrailingMean(key, as_of, field, window_sessions, None, 0,
                            "insufficient_sessions", security_status=security_status)
    if ledger.history(key, as_of).first_session is None:
        return TrailingMean(key, as_of, field, window_sessions, None, 0, "no_observations",
                            missing_sessions=len(window), security_status=security_status)

    total = Decimal(0)
    traded = observed = missing = zero_volume = invalid = unknown = 0
    failure = None
    for session in window:
        bar = ledger.bar(key, session)
        if bar is None:
            missing += 1
            continue
        observed += 1
        if not bar.valid_ohlcv:
            invalid += 1
            if failure is None:
                failure = f"invalid_bar:{session}"
            continue
        value = getattr(bar, field)
        if value is None:
            unknown += 1
            if failure is None:
                failure = f"{field}_unknown:{session}"
            continue
        total += value
        traded += 1
        if value == 0:
            zero_volume += 1

    diagnostics = dict(observed_sessions=observed, missing_sessions=missing,
                       zero_volume_sessions=zero_volume, invalid_sessions=invalid,
                       unknown_sessions=unknown, security_status=security_status)
    if failure is not None:
        return TrailingMean(key, as_of, field, window_sessions, None, 0, failure, **diagnostics)
    if security_status == "unknown":
        return TrailingMean(key, as_of, field, window_sessions, None, 0,
                            "security_status_unknown", **diagnostics)
    if security_status in ("suspended", "delisted"):
        return TrailingMean(key, as_of, field, window_sessions, None, 0,
                            f"security_status:{security_status}", **diagnostics)
    if missing == 0:
        return TrailingMean(key, as_of, field, window_sessions, total, traded, "", **diagnostics)
    if traded == 0:
        if security_status == "listed" and allow_partial_adv:
            return TrailingMean(key, as_of, field, window_sessions, Decimal(0), 0,
                                "verified_listed_no_trade", **diagnostics)
        return TrailingMean(key, as_of, field, window_sessions, None, 0,
                            "no_observations_in_window", **diagnostics)
    return TrailingMean(key, as_of, field, window_sessions, total, traded,
                        "partial_coverage", **diagnostics)


def trailing_adv(ledger: ObservationLedger, key: str, as_of: date, window_sessions: int,
                 *, allow_partial_adv: bool = False,
                 security_status: str | None = None) -> TrailingMean:
    return trailing_mean(ledger, key, as_of, window_sessions, "traded_value",
                         allow_partial_adv=allow_partial_adv, security_status=security_status)


def capacity_notional(ledger: ObservationLedger, key: str, as_of: date,
                      spec: ExecutionSpec, *, allow_partial_adv: bool = False,
                      min_coverage: Fraction = MIN_CAPACITY_COVERAGE,
                      security_status: str | None = None) -> Decimal | None:
    """ADV(spec.adv_window_sessions) x sizing_participation_rate, rounded DOWN to paise.

    Capacity is None below the minimum observed coverage (full window by
    default) or when the mean is unknown. ``allow_partial_adv=True`` is the
    explicit verified-listed/no-trade opt-in; it permits a partial estimate but
    never changes what the trailing record reports.
    """
    if not isinstance(spec, ExecutionSpec):
        raise ValueError("Capacity needs an ExecutionSpec")
    _validate_flags(allow_partial_adv, security_status)
    if not isinstance(min_coverage, Fraction):
        if type(min_coverage) is int:
            min_coverage = Fraction(min_coverage)
        else:
            raise ValueError("min_coverage must be a Fraction between 0 and 1")
    if not 0 <= min_coverage <= 1:
        raise ValueError("min_coverage must be a Fraction between 0 and 1")
    adv = trailing_adv(ledger, key, as_of, spec.adv_window_sessions,
                       allow_partial_adv=allow_partial_adv, security_status=security_status)
    if adv.exact is None:
        return None
    if not allow_partial_adv and adv.coverage < min_coverage:
        return None
    return floor_cents(adv.exact * exact(spec.sizing_participation_rate))
