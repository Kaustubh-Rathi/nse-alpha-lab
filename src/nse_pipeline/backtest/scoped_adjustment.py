"""Versioned, security-scoped historical price adjustment.

Raw prices are immutable. An adjustment is a separate view over a snapshot; it
never rewrites recorded prices, cash, units, marks or NAV. A factor is admissible
only inside its declared security scope and dated window, and a point-in-time
query excludes any factor whose evidence was not yet available. Ex-post research
may deliberately use later-known factors over the historical window.

Factors are exact rationals. Adjusted values are exact Fractions; no rounding is
performed or hidden here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
import hashlib

from .accounting_types import EventTime, decimal, identifier, sha256_digest


def _fraction(value) -> Fraction:
    if isinstance(value, Fraction):
        result = value
    elif isinstance(value, Decimal):
        result = Fraction(value)
    elif type(value) is int:
        result = Fraction(value)
    elif isinstance(value, str):
        try:
            result = Fraction(value)
        except (ValueError, ZeroDivisionError) as error:
            raise ValueError("Adjustment factor must be an exact rational") from error
    else:
        raise TypeError("Use Fraction, Decimal, an integer or an exact rational string; not float/bool")
    if result <= 0:
        raise ValueError("Adjustment factor must be strictly positive")
    return result


@dataclass(frozen=True)
class RawPriceSeries:
    security_key: str
    prices: tuple[tuple[date, Decimal], ...]

    def __post_init__(self):
        identifier(self.security_key)
        rows = tuple(self.prices)
        if not rows:
            raise ValueError("A raw price series needs at least one observation")
        checked = []
        previous = None
        for row in rows:
            if not isinstance(row, tuple) or len(row) != 2:
                raise ValueError("Raw prices must be (date, Decimal) pairs")
            on, price = row
            if type(on) is not date:
                raise ValueError("Raw price observations need session dates")
            if previous is not None and on <= previous:
                raise ValueError("Raw price dates must be strictly increasing and unique")
            checked.append((on, decimal(price, positive=True)))
            previous = on
        object.__setattr__(self, "prices", tuple(checked))

    @property
    def raw_series_sha256(self) -> str:
        payload = self.security_key + "\n" + "".join(
            f"{on.isoformat()},{price}\n" for on, price in self.prices)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def price_on(self, on: date) -> Decimal | None:
        if type(on) is not date:
            raise ValueError("A price query needs a date")
        return next((price for session, price in self.prices if session == on), None)


@dataclass(frozen=True)
class AdjustmentFactor:
    security_key: str
    action_id: str
    effective_date: date
    available_at: EventTime
    factor: Fraction
    scope_from: date
    scope_to: date | None
    version: int
    citation: str
    raw_series_sha256: str | None = None

    def __post_init__(self):
        identifier(self.security_key)
        identifier(self.action_id)
        identifier(self.citation)
        if type(self.effective_date) is not date:
            raise ValueError("A factor needs an explicit effective date")
        if not isinstance(self.available_at, EventTime):
            raise ValueError("A factor needs an explicit availability event time")
        object.__setattr__(self, "factor", _fraction(self.factor))
        if type(self.scope_from) is not date:
            raise ValueError("scope_from must be a date")
        if self.scope_to is not None:
            if type(self.scope_to) is not date or self.scope_to < self.scope_from:
                raise ValueError("scope_to must be a date on or after scope_from")
        if self.effective_date < self.scope_from:
            raise ValueError("The effective date cannot precede the adjustment scope")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("An adjustment factor needs a positive integer version")
        if self.raw_series_sha256 is not None:
            sha256_digest(self.raw_series_sha256, "raw_series_sha256")

    def covers(self, on: date) -> bool:
        return (self.scope_from <= on
                and (self.scope_to is None or on <= self.scope_to))


@dataclass(frozen=True)
class AdjustedPoint:
    on: date
    raw_price: Decimal
    factor: Fraction
    adjusted: Fraction

    def __post_init__(self):
        if type(self.on) is not date or self.factor <= 0:
            raise ValueError("An adjusted point needs a date and positive exact factor")
        object.__setattr__(self, "raw_price", decimal(self.raw_price, positive=True))
        object.__setattr__(self, "factor", _fraction(self.factor))
        if self.adjusted != Fraction(self.raw_price) * self.factor:
            raise ValueError("Adjusted value must equal raw price times the exact factor")


@dataclass(frozen=True)
class AdjustedSeries:
    security_key: str
    mode: str
    decision_at: EventTime | None
    raw_series_sha256: str
    points: tuple[AdjustedPoint, ...]
    applied: tuple[tuple[str, int], ...] = ()

    def value_on(self, on: date) -> Fraction | None:
        if type(on) is not date:
            raise ValueError("A price query needs a date")
        return next((point.adjusted for point in self.points if point.on == on), None)


def _selected(raw: RawPriceSeries, factors: tuple[AdjustmentFactor, ...]):
    grouped = {}
    for factor in factors:
        grouped.setdefault(factor.action_id, []).append(factor)
    selected = []
    for action_id, group in grouped.items():
        top = max(factor.version for factor in group)
        versions = [factor for factor in group if factor.version == top]
        signatures = {
            (factor.effective_date, factor.available_at, factor.factor,
             factor.scope_from, factor.scope_to, factor.citation,
             factor.raw_series_sha256)
            for factor in versions
        }
        if len(signatures) > 1:
            raise ValueError("conflicting_adjustment_versions")
        selected.append(versions[0])
    return tuple(sorted(selected, key=lambda factor: (
        factor.effective_date, factor.action_id, factor.version)))


def adjusted_series(raw_series: RawPriceSeries, factors, *, mode="point_in_time",
                    decision_at: EventTime | None = None) -> AdjustedSeries:
    """Return a new exact adjusted view; the supplied raw series is never modified."""
    if not isinstance(raw_series, RawPriceSeries):
        raise ValueError("Adjustment requires a typed RawPriceSeries")
    if mode not in ("point_in_time", "ex_post"):
        raise ValueError("Unknown adjustment mode")
    if mode == "point_in_time" and not isinstance(decision_at, EventTime):
        raise ValueError("point_in_time adjustment requires an explicit decision_at event time")
    if decision_at is not None and not isinstance(decision_at, EventTime):
        raise ValueError("decision_at must be a typed EventTime")
    factors = tuple(factors)
    if not all(isinstance(factor, AdjustmentFactor) for factor in factors):
        raise ValueError("Adjustment evidence must use typed factors")

    available = []
    for factor in factors:
        if factor.security_key != raw_series.security_key:
            raise ValueError("factor_security_scope_mismatch")
        if mode == "point_in_time" and factor.available_at > decision_at:
            continue
        if (factor.raw_series_sha256 is not None
                and factor.raw_series_sha256 != raw_series.raw_series_sha256):
            raise ValueError("factor_raw_series_digest_mismatch")
        available.append(factor)

    chosen = _selected(raw_series, tuple(available))
    points = []
    for on, price in raw_series.prices:
        product = Fraction(1)
        for factor in chosen:
            if factor.covers(on):
                product *= factor.factor
        points.append(AdjustedPoint(on, price, product, Fraction(price) * product))
    return AdjustedSeries(
        raw_series.security_key, mode, decision_at, raw_series.raw_series_sha256,
        tuple(points), tuple((factor.action_id, factor.version) for factor in chosen))
