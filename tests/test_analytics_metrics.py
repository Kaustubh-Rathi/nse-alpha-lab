"""Hand-computed regression tests for the Decimal metrics layer.

Policy under test: every undefined metric (short series, zero variance,
zero tracking error, one-sided capture, non-positive compounding leg)
raises; no sentinels are returned anywhere.

Core fixture, monthly returns (periods_per_year = 4):
    M = [0.01, 0.02, 0.03, 0.04]
    mean(M)   = 0.10 / 4 = 0.025
    deviations = [-0.015, -0.005, 0.005, 0.015]
    sample variance = (0.000225 + 0.000025 + 0.000025 + 0.000225) / 3
                    = 0.0005 / 3
    sample stdev    = sqrt(0.0005/3) = 0.012909944487358056...
Benchmark-replication fixture:
    S = 2*M + 0.01 = [0.03, 0.05, 0.07, 0.09], mean(S) = 0.06
"""
from decimal import Decimal

import pytest

from nse_pipeline.analytics.metrics import (
    BenchmarkSummary,
    RiskSummary,
    annualised_return,
    annualised_volatility,
    beta,
    benchmark_summary,
    down_capture,
    information_ratio,
    jensen_alpha_geometric,
    jensen_alpha_linear,
    max_drawdown,
    r_squared,
    risk_summary,
    sharpe_ratio,
    tracking_error,
    up_capture,
)

D = Decimal

M = [D("0.01"), D("0.02"), D("0.03"), D("0.04")]
S = [D("0.03"), D("0.05"), D("0.07"), D("0.09")]
PPY = 4
# Hand: 1.01 * 1.02 = 1.0302; * 1.03 = 1.061106; * 1.04 = 1.10355024.
# Annualising factor = 4/4 = 1, so the answer is exact.
GROWTH_M = D("1.10355024")
ANN_M = D("0.10355024")
# Hand: ann_vol = sqrt(4 * 0.0005/3) = sqrt(1/1500) = 0.02581988897471611238...
ANN_VOL_M = D("0.02581988897471611238426893017")
# Hand: sharpe^2 = mean^2 / var * ppy = 0.000625 / (0.0005/3) * 4 = 15, so sharpe = sqrt(15).
SHARPE_M = D("3.872983346207416885179265400")
# Hand with rf = 0.005: excess mean = 0.02, deviations unchanged, sharpe^2 =
# 0.0004 / (0.0005/3) * 4 = 9.6, so sharpe = sqrt(9.6).
SHARPE_M_RF = D("3.098386676965933508143412320")
TOL = D("1e-16")


def assert_close(actual: Decimal, expected: Decimal, tolerance: Decimal = TOL) -> None:
    assert abs(actual - expected) <= tolerance, f"{actual} != {expected}"


class TestAnnualisedReturn:
    def test_monthly_exact(self):
        assert annualised_return(M, periods_per_year=PPY) == ANN_M

    def test_half_year_pair(self):
        # Hand: 1.25 * 0.80 = 1.00 growth over two half-years, factor 2/2 = 1.
        assert annualised_return([D("0.25"), D("-0.20")], periods_per_year=2) == D("0")

    def test_monthly_series_annualised_by_twelve(self):
        # Integer exponent 12/4 = 3: 1.10355024^3 - 1, exact under Decimal.
        reference = GROWTH_M ** 3 - 1
        assert annualised_return(M, periods_per_year=12) == reference

    def test_zero_returns(self):
        assert annualised_return([D("0"), D("0"), D("0")], periods_per_year=3) == D("0")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            annualised_return([], periods_per_year=PPY)

    def test_single_raises(self):
        with pytest.raises(ValueError):
            annualised_return([D("0.05")], periods_per_year=PPY)

    def test_total_loss_raises(self):
        with pytest.raises(ValueError):
            annualised_return([D("-1"), D("0.01")], periods_per_year=PPY)

    def test_float_rejected(self):
        with pytest.raises(TypeError):
            annualised_return([0.01, 0.02], periods_per_year=PPY)

    def test_bad_period_count(self):
        for bad in (0, -4, D("4"), 4.0, True):
            with pytest.raises(ValueError):
                annualised_return(M, periods_per_year=bad)


class TestAnnualisedVolatility:
    def test_monthly(self):
        assert_close(annualised_volatility(M, periods_per_year=PPY), ANN_VOL_M)

    def test_short_and_empty(self):
        with pytest.raises(ValueError):
            annualised_volatility([], periods_per_year=PPY)
        with pytest.raises(ValueError):
            annualised_volatility([D("0.01")], periods_per_year=PPY)


class TestSharpe:
    def test_zero_risk_free(self):
        assert_close(sharpe_ratio(M, risk_free_rate=D("0"), periods_per_year=PPY), SHARPE_M)

    def test_positive_risk_free(self):
        assert_close(sharpe_ratio(M, risk_free_rate=D("0.005"), periods_per_year=PPY), SHARPE_M_RF)

    def test_negative_sharpe(self):
        # M negated keeps stdev, flips the mean: sharpe = -sqrt(15).
        negated = [-r for r in M]
        assert_close(sharpe_ratio(negated, risk_free_rate=D("0"), periods_per_year=PPY), -SHARPE_M)

    def test_zero_variance_raises(self):
        with pytest.raises(ValueError):
            sharpe_ratio([D("0.05"), D("0.05"), D("0.05")], risk_free_rate=D("0"), periods_per_year=PPY)

    def test_float_rate_rejected(self):
        with pytest.raises(TypeError):
            sharpe_ratio(M, risk_free_rate=0.005, periods_per_year=PPY)


class TestMaxDrawdown:
    def test_hand_computed_trough(self):
        # Equity: 1 -> 1.01 -> 1.0302 -> 1.0302*0.90 = 0.92718 -> 0.9642672.
        # Trough/peak = 0.92718 / 1.0302 = 0.9 exactly, so drawdown = -0.10.
        series = [D("0.01"), D("0.02"), D("-0.10"), D("0.04")]
        assert max_drawdown(series) == D("-0.1")

    def test_monotone_series_has_no_drawdown(self):
        assert max_drawdown(M) == D("0")

    def test_degenerate_inputs(self):
        with pytest.raises(ValueError):
            max_drawdown([])
        with pytest.raises(ValueError):
            max_drawdown([D("0.01")])
        with pytest.raises(ValueError):
            max_drawdown([D("-1.5"), D("0.1")])


class TestBeta:
    def test_perfect_linear_replication(self):
        # S = 2B + 0.01 => cov/var = 2 exactly (n-1 divisors cancel).
        assert beta(S, M) == D("2")

    def test_inverse_direction(self):
        assert beta(M, S) == D("0.5")

    def test_identical_series(self):
        assert beta(M, M) == D("1")

    def test_negative_beta(self):
        flipped = [-r for r in S]
        assert beta(flipped, M) == D("-2")

    def test_zero_variance_benchmark_raises(self):
        with pytest.raises(ValueError):
            beta(S, [D("0.02"), D("0.02"), D("0.02"), D("0.02")])

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            beta(S, M[:2])


class TestJensenAlpha:
    def test_linear_zero_risk_free(self):
        # alpha_period = 0.06 - 2 * 0.025 = 0.01; linear annual = 0.01 * 4.
        assert jensen_alpha_linear(S, M, risk_free_rate=D("0"), periods_per_year=PPY) == D("0.04")

    def test_linear_with_risk_free(self):
        # alpha_period = (0.06 - 0.005) - 2 * (0.025 - 0.005) = 0.015; * 4 = 0.06.
        assert jensen_alpha_linear(S, M, risk_free_rate=D("0.005"), periods_per_year=PPY) == D("0.06")

    def test_geometric_zero_risk_free(self):
        # (1 + 0.01)^4 - 1 = 1.04060401 - 1, exact.
        assert jensen_alpha_geometric(S, M, risk_free_rate=D("0"), periods_per_year=PPY) == D("0.04060401")

    def test_geometric_with_risk_free(self):
        # (1 + 0.015)^4 - 1 = 1.061363550625 - 1, exact.
        reference = D("1.015") ** 4 - 1
        assert jensen_alpha_geometric(S, M, risk_free_rate=D("0.005"), periods_per_year=PPY) == reference

    def test_identical_series_alpha_zero(self):
        assert jensen_alpha_linear(M, M, risk_free_rate=D("0.005"), periods_per_year=PPY) == D("0")
        assert jensen_alpha_geometric(M, M, risk_free_rate=D("0.005"), periods_per_year=PPY) == D("0")


class TestTrackingErrorAndInformationRatio:
    def test_tracking_error(self):
        # Active = S - M = [0.02, 0.03, 0.04, 0.05]; same deviation structure as M.
        assert_close(tracking_error(S, M, periods_per_year=PPY), ANN_VOL_M)

    def test_information_ratio(self):
        # ann(S) = 1.03*1.05*1.07*1.09 - 1 = 1.26135345 - 1 = 0.26135345 (exponent 1, exact).
        # numerator = 0.26135345 - 0.10355024 = 0.15780321.
        # TE = sqrt(1/1500), so IR = 0.15780321 * sqrt(1500) = 6.1116920430807171...
        expected = D("6.111692043080717102894895055")
        assert_close(information_ratio(S, M, periods_per_year=PPY), expected, TOL)

    def test_identical_series_zero_tracking_error_raises(self):
        with pytest.raises(ValueError):
            information_ratio(M, M, periods_per_year=PPY)


class TestCapture:
    # Up/down split fixture: benchmark positive in periods 1-2, negative in 3-4.
    B2 = [D("0.01"), D("0.02"), D("-0.01"), D("-0.03")]
    S2 = [D("0.005"), D("0.01"), D("-0.005"), D("-0.006")]

    def test_up_capture(self):
        # mean_s_up = 0.015/2 = 0.0075; mean_b_up = 0.03/2 = 0.015; ratio = 0.5.
        assert up_capture(self.S2, self.B2) == D("0.5")

    def test_down_capture(self):
        # mean_s_down = -0.011/2 = -0.0055; mean_b_down = -0.04/2 = -0.02; ratio = 0.275.
        assert down_capture(self.S2, self.B2) == D("0.275")

    def test_identical_series_capture_one(self):
        assert up_capture(self.B2, self.B2) == D("1")
        assert down_capture(self.B2, self.B2) == D("1")

    def test_all_down_market_up_capture_undefined(self):
        with pytest.raises(ValueError):
            up_capture([D("0.001"), D("0.002")], [D("-0.01"), D("-0.02")])

    def test_all_up_market_down_capture_undefined(self):
        with pytest.raises(ValueError):
            down_capture(M, M)

    def test_down_capture_sign(self):
        # Positive strategy in a falling market: 0.0015 / -0.015 = -0.1.
        assert down_capture([D("0.001"), D("0.002")], [D("-0.01"), D("-0.02")]) == D("-0.1")


class TestRSquared:
    def test_perfect_fit(self):
        assert r_squared(S, M) == D("1")

    def test_partial_correlation(self):
        # X = [0.01, -0.01, 0.02, -0.02], Y = [-0.01, 0.01, 0.02, -0.02]; both mean 0.
        # cross = -0.0001 - 0.0001 + 0.0004 + 0.0004 = 0.0006; sums of squares = 0.001 each.
        # r^2 = 0.0006^2 / (0.001 * 0.001) = 0.36, exact.
        x = [D("0.01"), D("-0.01"), D("0.02"), D("-0.02")]
        y = [D("-0.01"), D("0.01"), D("0.02"), D("-0.02")]
        assert r_squared(x, y) == D("0.36")

    def test_zero_variance_raises(self):
        flat = [D("0.01"), D("0.01"), D("0.01"), D("0.01")]
        with pytest.raises(ValueError):
            r_squared(S, flat)


class TestSummaries:
    def test_risk_summary_fields(self):
        summary = risk_summary(M, risk_free_rate=D("0"), periods_per_year=PPY)
        assert isinstance(summary, RiskSummary)
        assert summary.annualised_return == ANN_M
        assert summary.max_drawdown == D("0")
        assert_close(summary.sharpe_ratio, SHARPE_M)

    def test_benchmark_summary_fields(self):
        # Mixed-direction fixture: B4 = [0.03, 0.01, -0.01, -0.03], mean 0, sum b^2 = 0.002.
        # S4 = 2*B4 + 0.01 = [0.07, 0.03, -0.01, -0.05], mean 0.01, cross = 0.004.
        # beta = 0.004/0.002 = 2; alpha_period = 0.01 - 2*0 = 0.01.
        # up capture = ((0.07+0.03)/2) / ((0.03+0.01)/2) = 0.05/0.02 = 2.5;
        # down capture = ((-0.01-0.05)/2) / ((-0.01-0.03)/2) = -0.03/-0.02 = 1.5; r^2 = 1.
        # ann(S4) = 1.07*1.03*0.99*0.95 - 1 = 1.03652505 - 1 = 0.03652505 (exponent 1, exact).
        # ann(B4) = 1.03*1.01*0.99*0.97 - 1 = 0.99900009 - 1 = -0.00099991 (exact).
        # numerator = 0.03652505 - (-0.00099991) = 0.03752496.
        b4 = [D("0.03"), D("0.01"), D("-0.01"), D("-0.03")]
        s4 = [D("0.07"), D("0.03"), D("-0.01"), D("-0.05")]
        summary = benchmark_summary(s4, b4, risk_free_rate=D("0"), periods_per_year=PPY)
        assert isinstance(summary, BenchmarkSummary)
        assert summary.beta == D("2")
        assert summary.jensen_alpha_linear == D("0.04")
        assert summary.jensen_alpha_geometric == D("0.04060401")
        assert summary.r_squared == D("1")
        assert summary.up_capture == D("2.5")
        assert summary.down_capture == D("1.5")
        # TE = 2*sqrt(0.002/3), so IR = 0.03752496 * sqrt(1500)/2 = 0.7266677257354973...
        assert_close(summary.information_ratio, D("0.726667725735497351598382635"))

    def test_benchmark_summary_rejects_one_sided_benchmark(self):
        # Fail-closed: an all-positive benchmark has no down-market periods.
        with pytest.raises(ValueError):
            benchmark_summary(S, M, risk_free_rate=D("0"), periods_per_year=PPY)

    def test_summaries_are_frozen(self):
        summary = risk_summary(M, risk_free_rate=D("0"), periods_per_year=PPY)
        with pytest.raises(Exception):
            summary.sharpe_ratio = D("0")
