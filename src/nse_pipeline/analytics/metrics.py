"""Pure Decimal risk and benchmark metrics; every function fails closed on degenerate series.

Policy: metrics require at least two observations and positive compounding
equity; undefined results raise ValueError, no sentinels are returned.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

ZERO = Decimal(0)
ONE = Decimal(1)
TWO = Decimal(2)
HALF = Decimal("0.5")


def _sqrt(value: Decimal) -> Decimal:
    if value < 0:
        raise ValueError("Square root of a negative Decimal")
    return value ** HALF


def _series(returns: Sequence[Decimal], *, name: str) -> None:
    if len(returns) < 2:
        raise ValueError(f"{name} needs at least 2 returns, got {len(returns)}")
    for value in returns:
        if not isinstance(value, Decimal):
            raise TypeError(f"{name} must be a sequence of Decimal")
        if not value.is_finite():
            raise ValueError(f"{name} contains a non-finite Decimal")


def _paired(strategy: Sequence[Decimal], benchmark: Sequence[Decimal]) -> None:
    _series(strategy, name="strategy")
    _series(benchmark, name="benchmark")
    if len(strategy) != len(benchmark):
        raise ValueError("strategy and benchmark series must have equal length")


def _periods(periods_per_year: int) -> int:
    if not isinstance(periods_per_year, int) or isinstance(periods_per_year, bool) or periods_per_year < 1:
        raise ValueError("periods_per_year must be a positive integer")
    return periods_per_year


def _growth_factor(returns: Sequence[Decimal]) -> Decimal:
    growth = ONE
    for value in returns:
        leg = ONE + value
        if leg <= 0:
            raise ValueError("A return at or below -100% destroys compounding; series is unusable")
        growth *= leg
    return growth


def _mean(returns: Sequence[Decimal]) -> Decimal:
    return sum(returns, ZERO) / Decimal(len(returns))


def _stdev(returns: Sequence[Decimal]) -> Decimal:
    mean = _mean(returns)
    variance = sum(((r - mean) ** 2 for r in returns), ZERO) / Decimal(len(returns) - 1)
    return _sqrt(variance)


def _deviations(returns: Sequence[Decimal]) -> tuple[Decimal, ...]:
    mean = _mean(returns)
    return tuple(r - mean for r in returns)


def annualised_return(returns: Sequence[Decimal], *, periods_per_year: int) -> Decimal:
    """Geometric annual return: (prod(1+r)) ** (periods_per_year / n) - 1."""
    _series(returns, name="returns")
    periods = _periods(periods_per_year)
    growth = _growth_factor(returns)
    exponent = Decimal(periods) / Decimal(len(returns))
    return growth ** exponent - ONE


def annualised_volatility(returns: Sequence[Decimal], *, periods_per_year: int) -> Decimal:
    """Sample standard deviation (ddof=1) scaled by sqrt(periods_per_year)."""
    _series(returns, name="returns")
    periods = _periods(periods_per_year)
    return _stdev(returns) * _sqrt(Decimal(periods))


def sharpe_ratio(returns: Sequence[Decimal], *, risk_free_rate: Decimal, periods_per_year: int) -> Decimal:
    """mean(excess) / sample stdev(excess) * sqrt(periods_per_year); risk_free_rate is per-period."""
    _series(returns, name="returns")
    periods = _periods(periods_per_year)
    if not isinstance(risk_free_rate, Decimal) or not risk_free_rate.is_finite():
        raise TypeError("risk_free_rate must be a finite Decimal per-period rate")
    excess = tuple(r - risk_free_rate for r in returns)
    dispersion = _stdev(excess)
    if dispersion == 0:
        raise ValueError("Sharpe is undefined for a zero-variance return series")
    return _mean(excess) / dispersion * _sqrt(Decimal(periods))


def max_drawdown(returns: Sequence[Decimal]) -> Decimal:
    """Signed trough/peak of the compounded equity curve (1 start); non-positive Decimal."""
    _series(returns, name="returns")
    equity = ONE
    peak = ONE
    worst = ZERO
    for value in returns:
        leg = ONE + value
        if leg <= 0:
            raise ValueError("A return at or below -100% destroys compounding; series is unusable")
        equity *= leg
        peak = equity if equity > peak else peak
        if equity < peak:
            depth = equity / peak - ONE
            worst = depth if depth < worst else worst
    return worst


def beta(strategy_returns: Sequence[Decimal], benchmark_returns: Sequence[Decimal]) -> Decimal:
    """cov(sample, ddof n-1) / var(sample, ddof n-1); the divisors cancel."""
    _paired(strategy_returns, benchmark_returns)
    dev_b = _deviations(benchmark_returns)
    dev_s = _deviations(strategy_returns)
    variance = sum((d * d for d in dev_b), ZERO)
    if variance == 0:
        raise ValueError("Beta is undefined for a zero-variance benchmark series")
    covariance = sum((s * b for s, b in zip(dev_s, dev_b)), ZERO)
    return covariance / variance


def jensen_alpha_linear(
    strategy_returns: Sequence[Decimal],
    benchmark_returns: Sequence[Decimal],
    *,
    risk_free_rate: Decimal,
    periods_per_year: int,
) -> Decimal:
    """Per-period intercept (mean_excess_s - beta * mean_excess_b) times periods_per_year."""
    periods = _periods(periods_per_year)
    intercept = _jensen_intercept(strategy_returns, benchmark_returns, risk_free_rate)
    return intercept * Decimal(periods)


def jensen_alpha_geometric(
    strategy_returns: Sequence[Decimal],
    benchmark_returns: Sequence[Decimal],
    *,
    risk_free_rate: Decimal,
    periods_per_year: int,
) -> Decimal:
    """The same per-period intercept compounded: (1 + intercept) ** periods_per_year - 1."""
    periods = _periods(periods_per_year)
    intercept = _jensen_intercept(strategy_returns, benchmark_returns, risk_free_rate)
    if ONE + intercept <= 0:
        raise ValueError("Negative alpha cannot be compounded")
    return (ONE + intercept) ** periods - ONE


def _jensen_intercept(
    strategy_returns: Sequence[Decimal],
    benchmark_returns: Sequence[Decimal],
    risk_free_rate: Decimal,
) -> Decimal:
    _paired(strategy_returns, benchmark_returns)
    if not isinstance(risk_free_rate, Decimal) or not risk_free_rate.is_finite():
        raise TypeError("risk_free_rate must be a finite Decimal per-period rate")
    exposure = beta(strategy_returns, benchmark_returns)
    return (_mean(strategy_returns) - risk_free_rate) - exposure * (_mean(benchmark_returns) - risk_free_rate)


def tracking_error(
    strategy_returns: Sequence[Decimal],
    benchmark_returns: Sequence[Decimal],
    *,
    periods_per_year: int,
) -> Decimal:
    """Sample stdev of paired active returns scaled by sqrt(periods_per_year)."""
    _paired(strategy_returns, benchmark_returns)
    periods = _periods(periods_per_year)
    active = tuple(s - b for s, b in zip(strategy_returns, benchmark_returns))
    return _stdev(active) * _sqrt(Decimal(periods))


def information_ratio(
    strategy_returns: Sequence[Decimal],
    benchmark_returns: Sequence[Decimal],
    *,
    periods_per_year: int,
) -> Decimal:
    """(geometric annualised strategy return - geometric annualised benchmark return) / tracking error.

    Convention note: the audit's section 6.7.3 prototype may have used the
    arithmetic mean(active)/sd(active)*sqrt(ppy) form; its definition must be
    confirmed before any comparison with those published numbers.
    """
    periods = _periods(periods_per_year)
    error = tracking_error(strategy_returns, benchmark_returns, periods_per_year=periods)
    if error == 0:
        raise ValueError("Information ratio is undefined for zero tracking error")
    excess = (annualised_return(strategy_returns, periods_per_year=periods)
              - annualised_return(benchmark_returns, periods_per_year=periods))
    return excess / error


def up_capture(strategy_returns: Sequence[Decimal], benchmark_returns: Sequence[Decimal]) -> Decimal:
    """Mean strategy return / mean benchmark return over periods where the benchmark return is positive."""
    return _capture(strategy_returns, benchmark_returns, positive=True)


def down_capture(strategy_returns: Sequence[Decimal], benchmark_returns: Sequence[Decimal]) -> Decimal:
    """Mean strategy return / mean benchmark return over periods where the benchmark return is negative."""
    return _capture(strategy_returns, benchmark_returns, positive=False)


def _capture(strategy: Sequence[Decimal], benchmark: Sequence[Decimal], *, positive: bool) -> Decimal:
    _paired(strategy, benchmark)
    if positive:
        subset_s = [s for s, b in zip(strategy, benchmark) if b > 0]
        subset_b = [b for b in benchmark if b > 0]
        side = "up"
    else:
        subset_s = [s for s, b in zip(strategy, benchmark) if b < 0]
        subset_b = [b for b in benchmark if b < 0]
        side = "down"
    if not subset_b:
        raise ValueError(f"No {side}-market periods; {side} capture is undefined")
    denominator = _mean(subset_b)
    if denominator == 0:
        raise ValueError(f"{side} capture denominator is zero")
    return _mean(subset_s) / denominator


def r_squared(strategy_returns: Sequence[Decimal], benchmark_returns: Sequence[Decimal]) -> Decimal:
    """Squared Pearson correlation: cov^2 / (var_s * var_b); exact Decimal arithmetic."""
    _paired(strategy_returns, benchmark_returns)
    dev_s = _deviations(strategy_returns)
    dev_b = _deviations(benchmark_returns)
    sum_s = sum((d * d for d in dev_s), ZERO)
    sum_b = sum((d * d for d in dev_b), ZERO)
    if sum_s == 0 or sum_b == 0:
        raise ValueError("R^2 is undefined when either series has zero variance")
    cov = sum((s * b for s, b in zip(dev_s, dev_b)), ZERO)
    return cov * cov / (sum_s * sum_b)


@dataclass(frozen=True)
class RiskSummary:
    periods_per_year: int
    annualised_return: Decimal
    annualised_volatility: Decimal
    sharpe_ratio: Decimal
    max_drawdown: Decimal


@dataclass(frozen=True)
class BenchmarkSummary:
    periods_per_year: int
    beta: Decimal
    jensen_alpha_linear: Decimal
    jensen_alpha_geometric: Decimal
    information_ratio: Decimal
    tracking_error: Decimal
    up_capture: Decimal
    down_capture: Decimal
    r_squared: Decimal


def risk_summary(
    returns: Sequence[Decimal],
    *,
    risk_free_rate: Decimal,
    periods_per_year: int,
) -> RiskSummary:
    return RiskSummary(
        periods_per_year=periods_per_year,
        annualised_return=annualised_return(returns, periods_per_year=periods_per_year),
        annualised_volatility=annualised_volatility(returns, periods_per_year=periods_per_year),
        sharpe_ratio=sharpe_ratio(returns, risk_free_rate=risk_free_rate, periods_per_year=periods_per_year),
        max_drawdown=max_drawdown(returns),
    )


def benchmark_summary(
    strategy_returns: Sequence[Decimal],
    benchmark_returns: Sequence[Decimal],
    *,
    risk_free_rate: Decimal,
    periods_per_year: int,
) -> BenchmarkSummary:
    return BenchmarkSummary(
        periods_per_year=periods_per_year,
        beta=beta(strategy_returns, benchmark_returns),
        jensen_alpha_linear=jensen_alpha_linear(
            strategy_returns, benchmark_returns, risk_free_rate=risk_free_rate, periods_per_year=periods_per_year),
        jensen_alpha_geometric=jensen_alpha_geometric(
            strategy_returns, benchmark_returns, risk_free_rate=risk_free_rate, periods_per_year=periods_per_year),
        information_ratio=information_ratio(strategy_returns, benchmark_returns, periods_per_year=periods_per_year),
        tracking_error=tracking_error(strategy_returns, benchmark_returns, periods_per_year=periods_per_year),
        up_capture=up_capture(strategy_returns, benchmark_returns),
        down_capture=down_capture(strategy_returns, benchmark_returns),
        r_squared=r_squared(strategy_returns, benchmark_returns),
    )
