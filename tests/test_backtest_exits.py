"""Exit overlay: causal detection, no same-day fill, pessimistic ambiguity."""
from decimal import Decimal

import pytest

from nse_pipeline.backtest.exits import (
    STOP_LOSS,
    TAKE_PROFIT,
    ExitPolicy,
    describe,
    evaluate_exit,
    parse_exit_policy,
)

D = Decimal


def policy(**kwargs) -> ExitPolicy:
    return ExitPolicy(**kwargs)


class TestPolicyValidation:
    def test_inert_policy_has_no_legs(self):
        assert policy().active is False
        assert policy().stop_price(D("100")) is None
        assert policy().target_price(D("100")) is None

    def test_stop_and_target_prices_are_exact(self):
        p = policy(stop_loss_pct=D("0.10"), take_profit_pct=D("0.20"))
        assert p.stop_price(D("100")) == D("90")
        assert p.target_price(D("100")) == D("120")

    def test_negative_magnitude_rejected(self):
        with pytest.raises(ValueError, match="positive magnitude"):
            policy(stop_loss_pct=D("-0.10"))

    def test_zero_magnitude_rejected(self):
        with pytest.raises(ValueError, match="positive magnitude"):
            policy(take_profit_pct=D("0"))

    def test_full_loss_stop_rejected(self):
        with pytest.raises(ValueError, match="below 1.0"):
            policy(stop_loss_pct=D("1"))

    def test_float_rejected(self):
        with pytest.raises(TypeError, match="must be a Decimal"):
            policy(stop_loss_pct=0.10)

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError, match="mode must be one of"):
            policy(mode="magic")


class TestNextOpenMode:
    def test_no_breach_keeps_holding(self):
        p = policy(stop_loss_pct=D("0.10"), take_profit_pct=D("0.20"))
        assert evaluate_exit(p, entry_price=D("100"), close=D("105")) is None

    def test_stop_breach_defers_to_next_open(self):
        p = policy(stop_loss_pct=D("0.10"))
        signal = evaluate_exit(p, entry_price=D("100"), close=D("89"))
        assert signal.reason == STOP_LOSS
        assert signal.executes_next_open is True
        # The fill price is unknown at decision time and must not be invented.
        assert signal.trigger_price is None

    def test_stop_boundary_is_inclusive(self):
        p = policy(stop_loss_pct=D("0.10"))
        assert evaluate_exit(p, entry_price=D("100"), close=D("90")).reason == STOP_LOSS

    def test_target_breach_defers_to_next_open(self):
        p = policy(take_profit_pct=D("0.20"))
        signal = evaluate_exit(p, entry_price=D("100"), close=D("120"))
        assert signal.reason == TAKE_PROFIT
        assert signal.executes_next_open is True

    def test_target_boundary_is_inclusive(self):
        p = policy(take_profit_pct=D("0.20"))
        assert evaluate_exit(p, entry_price=D("100"), close=D("120")).reason == TAKE_PROFIT

    def test_stop_wins_when_both_could_apply(self):
        # A close cannot breach both legs at once, but the ordering must be stable.
        p = policy(stop_loss_pct=D("0.10"), take_profit_pct=D("0.20"))
        assert evaluate_exit(p, entry_price=D("100"), close=D("85")).reason == STOP_LOSS

    def test_invalid_entry_rejected(self):
        with pytest.raises(ValueError, match="entry_price must be a positive"):
            evaluate_exit(policy(stop_loss_pct=D("0.10")),
                          entry_price=D("0"), close=D("100"))

    def test_invalid_close_rejected(self):
        with pytest.raises(ValueError, match="close must be a positive"):
            evaluate_exit(policy(stop_loss_pct=D("0.10")),
                          entry_price=D("100"), close=D("0"))

    def test_inert_policy_never_signals(self):
        assert evaluate_exit(policy(), entry_price=D("100"), close=D("1")) is None


class TestIntradayMode:
    def test_requires_range(self):
        p = policy(stop_loss_pct=D("0.10"), mode="intraday_stop")
        with pytest.raises(ValueError, match="requires both low and high"):
            evaluate_exit(p, entry_price=D("100"), close=D("95"))

    def test_stop_fills_at_trigger(self):
        p = policy(stop_loss_pct=D("0.10"), mode="intraday_stop")
        signal = evaluate_exit(p, entry_price=D("100"), close=D("95"),
                               low=D("88"), high=D("101"))
        assert signal.reason == STOP_LOSS
        assert signal.trigger_price == D("90")
        assert signal.executes_next_open is False

    def test_target_fills_at_trigger(self):
        p = policy(take_profit_pct=D("0.20"), mode="intraday_stop")
        signal = evaluate_exit(p, entry_price=D("100"), close=D("118"),
                               low=D("99"), high=D("122"))
        assert signal.reason == TAKE_PROFIT
        assert signal.trigger_price == D("120")

    def test_both_legs_ambiguous_resolves_to_stop(self):
        # Assuming the target filled first would flatter the result.
        p = policy(stop_loss_pct=D("0.10"), take_profit_pct=D("0.20"),
                   mode="intraday_stop")
        signal = evaluate_exit(p, entry_price=D("100"), close=D("100"),
                               low=D("85"), high=D("125"))
        assert signal.reason == STOP_LOSS

    def test_no_breach_keeps_holding(self):
        p = policy(stop_loss_pct=D("0.10"), take_profit_pct=D("0.20"),
                   mode="intraday_stop")
        assert evaluate_exit(p, entry_price=D("100"), close=D("105"),
                             low=D("95"), high=D("110")) is None


class TestDescribe:
    def test_inert_label(self):
        assert describe(policy()) == "no_exit_overlay"

    def test_full_label(self):
        p = policy(stop_loss_pct=D("0.10"), take_profit_pct=D("0.20"))
        assert describe(p) == "sl0.1-tp0.2-next_open"

    def test_stop_only_label(self):
        assert describe(policy(stop_loss_pct=D("0.10"))) == "sl0.1-next_open"


class TestParseExitPolicy:
    def test_none_is_inert(self):
        parsed = parse_exit_policy("none")
        assert parsed.active is False
        assert describe(parsed) == "no_exit_overlay"

    def test_momentum_grid_exposes_exit_policy_parser(self):
        from nse_pipeline.backtest.momentum_grid import main, parse_exit_policy as grid_parser

        assert callable(main)
        assert grid_parser is parse_exit_policy

    def test_single_legs(self):
        assert describe(parse_exit_policy("sl0.05")) == "sl0.05-next_open"
        assert describe(parse_exit_policy("tp0.20")) == "tp0.2-next_open"

    def test_combined_leg(self):
        parsed = parse_exit_policy("sl0.10+tp0.20")
        assert parsed.stop_loss_pct == D("0.10")
        assert parsed.take_profit_pct == D("0.20")

    def test_case_and_whitespace_insensitive(self):
        assert parse_exit_policy("  SL0.15  ") == policy(stop_loss_pct=D("0.15"))

    def test_unknown_leg_rejected(self):
        with pytest.raises(ValueError, match="must start with"):
            parse_exit_policy("0.10")

    def test_duplicate_leg_rejected(self):
        with pytest.raises(ValueError, match="duplicate"):
            parse_exit_policy("sl0.10+sl0.15")

    def test_empty_spec_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            parse_exit_policy("")

    def test_empty_leg_rejected(self):
        with pytest.raises(ValueError, match="empty exit policy leg"):
            parse_exit_policy("sl0.10+")

    @pytest.mark.parametrize("spec", ["slabc", "sl0.x"])
    def test_malformed_magnitude_raises_value_error(self, spec):
        with pytest.raises(ValueError, match="invalid .* magnitude"):
            parse_exit_policy(spec)

    def test_invalid_magnitude_delegates_to_policy_validation(self):
        with pytest.raises(ValueError, match="positive magnitude"):
            parse_exit_policy("sl0")
        with pytest.raises(ValueError, match="below 1.0"):
            parse_exit_policy("sl1.5")
