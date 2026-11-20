"""Inferential statistics over paired Decimal signal/forward-return fixtures.

Policy: all functions fail closed (ValueError/TypeError) on short,
mismatched, degenerate or non-Decimal inputs; no sentinel values.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Sequence

ZERO = Decimal(0)
ONE = Decimal(1)
DEFAULT_CONFIDENCE = Decimal("0.95")
DEFAULT_RESAMPLES = 2000
_PRECISION = 34


def _to_decimal(value: Fraction) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return Decimal(value.numerator) / Decimal(value.denominator)


def _validated_pair(signals: Sequence[Decimal], forward_returns: Sequence[Decimal]) -> None:
    if len(signals) != len(forward_returns):
        raise ValueError("signal and forward-return series must have equal length")
    _validated_series(signals, name="signals")
    _validated_series(forward_returns, name="forward_returns")


def _validated_series(values: Sequence[Decimal], *, name: str) -> None:
    if len(values) < 2:
        raise ValueError(f"{name} needs at least 2 observations, got {len(values)}")
    for value in values:
        if not isinstance(value, Decimal):
            raise TypeError(f"{name} must be a sequence of Decimal")
        if not value.is_finite():
            raise ValueError(f"{name} contains a non-finite Decimal")


def _fractional(values: Sequence[Decimal]) -> tuple[Fraction, ...]:
    return tuple(Fraction(value) for value in values)


def _mean_fractions(values: Sequence[Fraction]) -> Fraction:
    return sum(values, Fraction(0)) / len(values)


def _correlation(left: Sequence[Fraction], right: Sequence[Fraction]) -> Decimal:
    mean_l = _mean_fractions(left)
    mean_r = _mean_fractions(right)
    dev_l = [v - mean_l for v in left]
    dev_r = [v - mean_r for v in right]
    sum_ll = sum((d * d for d in dev_l), Fraction(0))
    sum_rr = sum((d * d for d in dev_r), Fraction(0))
    if sum_ll == 0 or sum_rr == 0:
        raise ValueError("Correlation is undefined for a constant series")
    cross = sum((l * r for l, r in zip(dev_l, dev_r)), Fraction(0))
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return _to_decimal(cross) / (_to_decimal(sum_ll * sum_rr) ** Decimal("0.5"))


def _ranks(values: Sequence[Decimal]) -> tuple[Fraction, ...]:
    """1-based ranks with ties resolved to their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks: list[Fraction | None] = [None] * len(values)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        average = Fraction(start + 1 + end + 1, 2)
        for position in range(start, end + 1):
            ranks[order[position]] = average
        start = end + 1
    return tuple(ranks)


def information_coefficient(signals: Sequence[Decimal], forward_returns: Sequence[Decimal]) -> Decimal:
    """Pearson correlation between paired signal values and realised forward returns."""
    _validated_pair(signals, forward_returns)
    return _correlation(_fractional(signals), _fractional(forward_returns))


def rank_ic(signals: Sequence[Decimal], forward_returns: Sequence[Decimal]) -> Decimal:
    """Spearman rank correlation: Pearson correlation of average-tie ranks."""
    _validated_pair(signals, forward_returns)
    return _correlation(_ranks(signals), _ranks(forward_returns))


def t_statistic(values: Sequence[Decimal]) -> Decimal:
    """One-sample t against zero: mean * sqrt(n) / sample stdev (ddof=1)."""
    _validated_series(values, name="values")
    fractions = _fractional(values)
    mean = _mean_fractions(fractions)
    deviations = [v - mean for v in fractions]
    variance = sum((d * d for d in deviations), Fraction(0)) / (len(values) - 1)
    if variance == 0:
        raise ValueError("t statistic is undefined for a zero-variance series")
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        scale = (Decimal(len(values)) / _to_decimal(variance)) ** Decimal("0.5")
        return _to_decimal(mean) * scale


@dataclass(frozen=True)
class BootstrapInterval:
    confidence_level: Decimal
    lower: Decimal
    upper: Decimal
    resamples: int
    seed: int


def bootstrap_confidence_interval(
    values: Sequence[Decimal],
    *,
    seed: int,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: Decimal = DEFAULT_CONFIDENCE,
) -> BootstrapInterval:
    """Percentile bootstrap CI of the mean, drawn with random.Random(seed); deterministic."""
    _validated_series(values, name="values")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError("seed must be an int")
    if not isinstance(resamples, int) or isinstance(resamples, bool) or resamples < 1:
        raise ValueError("resamples must be a positive integer")
    if not isinstance(confidence, Decimal) or not (ZERO < confidence < ONE):
        raise ValueError("confidence must be a Decimal strictly between 0 and 1")
    size = len(values)
    observations = _fractional(values)
    means: list[Fraction] = []
    rng = random.Random(seed)
    for _ in range(resamples):
        total = Fraction(0)
        for _ in range(size):
            total += observations[rng.randrange(size)]
        means.append(total / size)
    means.sort()
    tail = (ONE - confidence) / 2
    lower = _quantile(means, Fraction(tail), resamples)
    upper = _quantile(means, Fraction(ONE - tail), resamples)
    return BootstrapInterval(confidence_level=confidence, lower=lower, upper=upper,
                             resamples=resamples, seed=seed)


def _quantile(sorted_means: Sequence[Fraction], level: Fraction, resamples: int) -> Decimal:
    position = level * (resamples - 1)
    index = position.numerator // position.denominator
    remainder = position - index
    if remainder == 0 or index + 1 >= resamples:
        return _to_decimal(sorted_means[index])
    span = sorted_means[index + 1] - sorted_means[index]
    return _to_decimal(sorted_means[index] + span * remainder)
