"""Hand-computed regression tests for the inferential statistics layer.

Policy under test: short (n < 2), mismatched-length, constant/degenerate and
non-Decimal inputs raise; the bootstrap is deterministic for a fixed seed.

Fixture arithmetic is written out per test. All values enter as Decimal.
"""
from decimal import Decimal

import pytest

from nse_pipeline.analytics.statistics import (
    BootstrapInterval,
    bootstrap_confidence_interval,
    information_coefficient,
    rank_ic,
    t_statistic,
)

D = Decimal
TOL = D("1e-15")


def assert_close(actual: Decimal, expected: Decimal, tolerance: Decimal = TOL) -> None:
    assert abs(actual - expected) <= tolerance, f"{actual} != {expected}"


class TestInformationCoefficient:
    def test_perfect_correlation(self):
        x = [D("0.01"), D("0.02"), D("0.03"), D("0.04")]
        y = [D("0.10"), D("0.20"), D("0.30"), D("0.40")]
        assert information_coefficient(x, y) == D("1")

    def test_perfect_inverse(self):
        x = [D("1"), D("2"), D("3"), D("4")]
        y = [D("4"), D("3"), D("2"), D("1")]
        assert information_coefficient(x, y) == D("-1")

    def test_hand_computed_partial(self):
        # means: x = 2.5, y = 3.
        # dev_x = [-1.5, -0.5, 0.5, 1.5]; dev_y = [-1, -1, 1, 1].
        # cross = 1.5 + 0.5 + 0.5 + 1.5 = 4; Sxx = 5; Syy = 4.
        # r = 4 / sqrt(20) = 2 / sqrt(5) = 0.8944271909999158785636694674...
        x = [D("1"), D("2"), D("3"), D("4")]
        y = [D("2"), D("2"), D("4"), D("4")]
        assert_close(information_coefficient(x, y), D("0.8944271909999158785636694674"))

    def test_ties_do_not_matter_for_pearson(self):
        # raw values [1,2,2,3] vs [1,3,2,4]: dev_x = [-1,0,0,1], dev_y = [-1.5,0.5,-0.5,1.5].
        # cross = 1.5 + 0 + 0 + 1.5 = 3; Sxx = 2; Syy = 5; r = 3 / sqrt(10).
        x = [D("1"), D("2"), D("2"), D("3")]
        y = [D("1"), D("3"), D("2"), D("4")]
        assert_close(information_coefficient(x, y), D("0.9486832980505137995996680635"))

    def test_degenerate(self):
        with pytest.raises(ValueError):
            information_coefficient([D("1")], [D("1")])
        with pytest.raises(ValueError):
            information_coefficient([], [])
        with pytest.raises(ValueError):
            information_coefficient([D("1"), D("2"), D("3")], [D("1"), D("2")])
        with pytest.raises(ValueError):
            information_coefficient([D("2"), D("2"), D("2")], [D("1"), D("2"), D("3")])
        with pytest.raises(TypeError):
            information_coefficient([1, 2], [D("1"), D("2")])


class TestRankIC:
    def test_monotone_with_ties_is_one(self):
        # x ranks [1, 2.5, 2.5, 4]; y values order-preserving with the same tie
        # pattern, so both rank vectors are identical and r = 1.
        x = [D("1"), D("2"), D("2"), D("3")]
        y = [D("0.1"), D("0.2"), D("0.2"), D("0.3")]
        assert rank_ic(x, y) == D("1")

    def test_hand_computed(self):
        # x ranks: [1, 2.5, 2.5, 4]; y ranks: [1, 3, 2, 4].
        # dev_x = [-1.5, 0, 0, 1.5]; dev_y = [-1.5, 0.5, -0.5, 1.5].
        # cross = 2.25 + 0 + 0 + 2.25 = 4.5; Sxx = 4.5; Syy = 2.25+0.25+0.25+2.25 = 5.
        # r = 4.5 / sqrt(4.5 * 5) = 4.5 / sqrt(22.5) = sqrt(0.9) = 0.948683298050513799...
        x = [D("1"), D("2"), D("2"), D("3")]
        y = [D("1"), D("3"), D("2"), D("4")]
        assert_close(rank_ic(x, y), D("0.9486832980505137995996680633"))

    def test_rank_differs_from_pearson(self):
        # x = [1,1,2,3], y = [3,4,1,2]. Pearson: dev_x = [-0.75,-0.75,0.25,1.25],
        # dev_y = [0.5,1.5,-1.5,-0.5]; cross = -2.5, Sxx = 2.75, Syy = 5,
        # r = -2.5/sqrt(13.75) = -0.6741998624632420862464906764.
        # Ranks: rx = [1.5,1.5,3,4], ry = [3,4,1,2]; dev cross = -3.5,
        # Sxx = 4.5, Syy = 5, rho = -3.5/sqrt(22.5) = -0.7378647873726218441330751604.
        x = [D("1"), D("1"), D("2"), D("3")]
        y = [D("3"), D("4"), D("1"), D("2")]
        assert rank_ic(x, y) != information_coefficient(x, y)
        assert_close(rank_ic(x, y), D("-0.7378647873726218441330751604"))
        assert_close(information_coefficient(x, y), D("-0.6741998624632420862464906764"))

    def test_perfect_inverse_ranking(self):
        x = [D("1"), D("2"), D("3"), D("4")]
        y = [D("40"), D("30"), D("20"), D("10")]
        assert rank_ic(x, y) == D("-1")

    def test_constant_signal_raises(self):
        with pytest.raises(ValueError):
            rank_ic([D("1"), D("1"), D("1")], [D("1"), D("2"), D("3")])


class TestTStatistic:
    def test_hand_computed(self):
        # values [1,2,3,4]: mean 2.5, sample stdev sqrt(5/3);
        # t = 2.5 * sqrt(4) / sqrt(5/3) = 5 * sqrt(3/5) = sqrt(15) = 3.872983346207416885...
        assert_close(t_statistic([D("1"), D("2"), D("3"), D("4")]), D("3.872983346207416885179265400"))

    def test_mean_zero_gives_zero(self):
        result = t_statistic([D("-1"), D("1")])
        assert result == D("0")

    def test_negative(self):
        assert_close(t_statistic([D("-1"), D("-2"), D("-3"), D("-4")]), D("-3.872983346207416885179265400"))

    def test_zero_variance_raises(self):
        with pytest.raises(ValueError):
            t_statistic([D("2"), D("2"), D("2")])

    def test_short_raises(self):
        with pytest.raises(ValueError):
            t_statistic([D("2")])


class TestBootstrapConfidenceInterval:
    def test_constant_series_collapses(self):
        # Every resample of [5,5,5] has mean 5 regardless of the seed.
        interval = bootstrap_confidence_interval([D("5"), D("5"), D("5")], seed=7)
        assert isinstance(interval, BootstrapInterval)
        assert interval.lower == D("5")
        assert interval.upper == D("5")

    def test_deterministic_for_fixed_seed(self):
        values = [D("0"), D("2"), D("2"), D("0"), D("2"), D("0"), D("2"), D("0")]
        first = bootstrap_confidence_interval(values, seed=42, resamples=500)
        second = bootstrap_confidence_interval(values, seed=42, resamples=500)
        assert first == second

    def test_brackets_the_sample_mean(self):
        # Symmetric 0/2 fixture with mean 1: the percentile interval of the
        # resample means must straddle 1 for any reasonable seed.
        values = [D("0"), D("2"), D("2"), D("0"), D("2"), D("0"), D("2"), D("0")]
        for seed in (1, 42, 2026):
            interval = bootstrap_confidence_interval(values, seed=seed, resamples=2000)
            assert interval.lower < D("1") < interval.upper, seed

    def test_confidence_level_recorded(self):
        interval = bootstrap_confidence_interval([D("1"), D("2"), D("3"), D("4")], seed=3,
                                                 confidence=D("0.90"))
        assert interval.confidence_level == D("0.90")
        assert interval.lower <= interval.upper

    def test_validation(self):
        with pytest.raises(ValueError):
            bootstrap_confidence_interval([D("1")], seed=1)
        with pytest.raises(ValueError):
            bootstrap_confidence_interval([D("1"), D("2")], seed=1, resamples=0)
        with pytest.raises(ValueError):
            bootstrap_confidence_interval([D("1"), D("2")], seed=1, confidence=D("1"))
        with pytest.raises(ValueError):
            bootstrap_confidence_interval([D("1"), D("2")], seed=1, confidence=D("0"))
        with pytest.raises(ValueError):
            bootstrap_confidence_interval([D("1"), D("2")], seed=1, confidence=D("1.5"))
        with pytest.raises(TypeError):
            bootstrap_confidence_interval([D("1"), D("2")], seed=1.5)
        with pytest.raises(TypeError):
            bootstrap_confidence_interval([1.0, 2.0], seed=1)
