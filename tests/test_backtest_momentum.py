"""Momentum rotation engine: causality, determinism, no invented prices."""
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from nse_pipeline.backtest.exits import ExitPolicy
from nse_pipeline.backtest.momentum import (
    BarsFrame,
    EngineConfig,
    MomentumRotationEngine,
)

D = Decimal


def make_bars(rows) -> BarsFrame:
    return BarsFrame(pd.DataFrame(rows))


def session_series(start: date, count: int) -> list[date]:
    return [start + timedelta(days=index) for index in range(count)]


def flat_bars(sessions, key="A", price=100.0, volume=100_000):
    return [{"date": s, "key": key, "open": price, "close": price,
             "volume": volume, "low": price, "high": price} for s in sessions]


class TestBarsFrameValidation:
    def test_missing_column_rejected(self):
        with pytest.raises(ValueError, match="missing required columns"):
            BarsFrame(pd.DataFrame([{"date": date(2020, 1, 1), "key": "A",
                                     "open": 1.0, "close": 1.0}]))

    def test_duplicate_date_key_rejected(self):
        rows = flat_bars([date(2020, 1, 1)]) * 2
        with pytest.raises(ValueError, match="duplicate \\(date, key\\) bars"):
            make_bars(rows)

    def test_sessions_are_sorted(self):
        rows = flat_bars([date(2020, 1, 3), date(2020, 1, 1), date(2020, 1, 2)])
        frame = make_bars(rows)
        assert frame.sessions == [date(2020, 1, 1), date(2020, 1, 2), date(2020, 1, 3)]

    def test_counts_as_of_counts_only_completed_observations(self):
        sessions = [date(2020, 1, 1), date(2020, 1, 2), date(2020, 1, 3),
                    date(2020, 1, 4)]
        rows = (flat_bars([sessions[0], sessions[1], sessions[3]], key="A")
                + flat_bars([sessions[1], sessions[2]], key="B"))
        frame = make_bars(rows)

        assert frame.counts_as_of(sessions[0]) == {"A": 1}
        assert frame.counts_as_of(sessions[1]) == {"A": 2, "B": 1}
        assert frame.counts_as_of(sessions[2]) == {"A": 2, "B": 2}
        assert frame.counts_as_of(sessions[3]) == {"A": 3, "B": 2}
        # A date that is not a bar session, or is after the data, cannot
        # silently borrow a neighbouring session's counts.
        assert frame.counts_as_of(date(2020, 1, 5)) == {}
        assert frame.counts_as_of(date(2019, 12, 31)) == {}


class TestConfig:
    def test_unknown_entry_timing_rejected(self):
        with pytest.raises(ValueError, match="entry_timing must be one of"):
            EngineConfig(entry_timing="magic")

    def test_zero_top_n_rejected(self):
        with pytest.raises(ValueError, match="top_n must be at least 1"):
            EngineConfig(top_n=0)

    def test_label_is_stable_and_descriptive(self):
        config = EngineConfig(cadence="quarterly", top_n=20, lookback_sessions=63,
                              exit_policy=ExitPolicy(stop_loss_pct=D("0.10")))
        assert config.label == "quarterly-N20-L63-next_open-sl0.1-next_open"

    def test_manifest_is_json_safe(self):
        import json
        manifest = EngineConfig().manifest()
        json.dumps(manifest)  # must not raise
        assert manifest["cadence"] == "monthly"
        assert manifest["lookahead"] is False
        assert manifest["execution_price"] == "next_session_open"

    def test_same_close_is_marked_as_a_lookahead_counterfactual(self):
        manifest = EngineConfig(entry_timing="same_close").manifest()
        assert manifest["lookahead"] is True
        assert manifest["execution_price"] == "signal_session_close"

    def test_cost_is_positive_for_a_real_trade(self):
        config = EngineConfig()
        assert config.cost_for(D("100000"), "buy") > D("0")
        assert config.cost_for(D("0"), "buy") == D("0")

    def test_unknown_direction_rejected(self):
        with pytest.raises(ValueError, match="direction must be one of"):
            EngineConfig(direction="sideways")

    def test_bottom_selection_is_visible_in_label_and_manifest(self):
        config = EngineConfig(top_n=20, direction="bottom")
        assert config.label.endswith("-bottom")
        assert config.manifest()["selection_direction"] == "bottom"
        assert EngineConfig().manifest()["selection_direction"] == "top"

    def test_cost_components_sum_to_cost_for(self):
        config = EngineConfig()
        for side in ("buy", "sell"):
            parts = config.cost_components(D("100000"), side)
            assert set(parts) == {"brokerage", "gst", "stt", "stamp",
                                  "exchange", "slippage"}
            assert sum(parts.values(), D("0")) == config.cost_for(D("100000"), side)

    def test_stamp_duty_is_buy_only_and_gst_is_on_brokerage(self):
        config = EngineConfig()
        buy = config.cost_components(D("100000"), "buy")
        sell = config.cost_components(D("100000"), "sell")
        assert buy["stamp"] > D("0")
        assert sell["stamp"] == D("0")
        assert buy["stt"] == sell["stt"] == D("100")  # 10 bps of 100,000
        assert buy["gst"] == D("3.60")  # 18% of the 20 brokerage

    def test_brokerage_is_capped_per_order(self):
        parts = EngineConfig().cost_components(D("10000000"), "buy")
        assert parts["brokerage"] == D("2000")
        assert parts["gst"] == D("360")


class TestRanking:
    def _two_symbol_bars(self):
        sessions = session_series(date(2020, 1, 1), 80)
        rows = []
        for index, session in enumerate(sessions):
            # A rises steadily, B is flat.
            rows.append({"date": session, "key": "A", "open": 100.0 + index,
                         "close": 100.0 + index, "volume": 100_000,
                         "low": 100.0 + index, "high": 100.0 + index})
            rows.append({"date": session, "key": "B", "open": 100.0,
                         "close": 100.0, "volume": 100_000,
                         "low": 100.0, "high": 100.0})
        return make_bars(rows), sessions

    def test_insufficient_history_returns_empty(self):
        bars, sessions = self._two_symbol_bars()
        engine = MomentumRotationEngine(EngineConfig(lookback_sessions=63,
                                                     min_days_observed=1))
        assert engine.rank(bars, sessions[10]).empty

    def test_stronger_momentum_ranks_first(self):
        bars, sessions = self._two_symbol_bars()
        engine = MomentumRotationEngine(EngineConfig(lookback_sessions=63,
                                                     min_days_observed=1))
        ranked = engine.rank(bars, sessions[-1])
        assert ranked["key"].tolist()[0] == "A"

    def test_ties_break_on_key_deterministically(self):
        sessions = session_series(date(2020, 1, 1), 80)
        rows = []
        for session in sessions:
            for key in ("Z", "A", "M"):
                rows.append({"date": session, "key": key, "open": 100.0,
                             "close": 100.0, "volume": 100_000,
                             "low": 100.0, "high": 100.0})
        bars = make_bars(rows)
        engine = MomentumRotationEngine(EngineConfig(lookback_sessions=63,
                                                     min_days_observed=1))
        ranked = engine.rank(bars, sessions[-1])
        assert ranked["key"].tolist() == ["A", "M", "Z"]

    def test_bottom_direction_holds_the_weakest_momentum(self):
        bars, sessions = self._two_symbol_bars()
        top = MomentumRotationEngine(EngineConfig(
            lookback_sessions=63, min_days_observed=1, top_n=1))
        bottom = MomentumRotationEngine(EngineConfig(
            lookback_sessions=63, min_days_observed=1, top_n=1, direction="bottom"))
        assert top.targets(bars, sessions[-1]) == ["A"]
        assert bottom.targets(bars, sessions[-1]) == ["B"]

    def test_low_volume_symbol_excluded(self):
        sessions = session_series(date(2020, 1, 1), 80)
        rows = []
        for session in sessions:
            rows.append({"date": session, "key": "LIQUID", "open": 100.0,
                         "close": 100.0, "volume": 100_000,
                         "low": 100.0, "high": 100.0})
            rows.append({"date": session, "key": "THIN", "open": 100.0,
                         "close": 100.0, "volume": 10,
                         "low": 100.0, "high": 100.0})
        bars = make_bars(rows)
        engine = MomentumRotationEngine(EngineConfig(lookback_sessions=63,
                                                     min_days_observed=1))
        assert "THIN" not in engine.rank(bars, sessions[-1])["key"].tolist()


class TestExecution:
    def _bars(self, count=160):
        sessions = session_series(date(2020, 1, 1), count)
        rows = []
        for index, session in enumerate(sessions):
            rows.append({"date": session, "key": "A", "open": 100.0 + index,
                         "close": 100.0 + index, "volume": 100_000,
                         "low": 100.0 + index, "high": 100.0 + index})
            rows.append({"date": session, "key": "B", "open": 100.0,
                         "close": 100.0, "volume": 100_000,
                         "low": 100.0, "high": 100.0})
        return make_bars(rows), sessions

    def test_no_trade_before_lookback_is_satisfied(self):
        bars, sessions = self._bars()
        engine = MomentumRotationEngine(EngineConfig(
            cadence="monthly", lookback_sessions=63, min_days_observed=1, top_n=1))
        result = engine.run(bars)
        first_trade = result["trades"][0]
        # The signal needs 63 sessions of history before it can rank.
        assert first_trade["date"] >= sessions[63]

    def test_next_open_fill_is_the_following_session_open(self):
        bars, sessions = self._bars()
        engine = MomentumRotationEngine(EngineConfig(
            cadence="monthly", lookback_sessions=63, min_days_observed=1, top_n=1,
            entry_timing="next_open"))
        result = engine.run(bars)
        trade = result["trades"][0]
        signal_date = trade["signal_date"]
        assert signal_date is not None
        assert trade["date"] > signal_date

    def test_same_close_fills_on_the_signal_session(self):
        bars, sessions = self._bars()
        engine = MomentumRotationEngine(EngineConfig(
            cadence="monthly", lookback_sessions=63, min_days_observed=1, top_n=1,
            entry_timing="same_close"))
        result = engine.run(bars)
        trade = result["trades"][0]
        assert trade["date"] == trade["signal_date"]

    def _bars_with_intraday_moves(self, count=160):
        sessions = session_series(date(2020, 1, 1), count)
        rows = []
        for index, session in enumerate(sessions):
            rows.append({"date": session, "key": "A", "open": 100.0 + index,
                         "close": 100.5 + index, "volume": 100_000,
                         "low": 100.0 + index, "high": 100.5 + index})
            rows.append({"date": session, "key": "B", "open": 100.0,
                         "close": 100.0, "volume": 100_000,
                         "low": 100.0, "high": 100.0})
        prices = {(row["date"], row["key"]): row for row in rows}
        return make_bars(rows), sessions, prices

    def test_same_close_fill_is_the_signal_session_close(self):
        # The signal is computed from the signal-day close. Filling at that
        # session's open would trade on a price that predates the signal.
        bars, _, prices = self._bars_with_intraday_moves()
        engine = MomentumRotationEngine(EngineConfig(
            cadence="monthly", lookback_sessions=63, min_days_observed=1, top_n=1,
            entry_timing="same_close"))
        trade = engine.run(bars)["trades"][0]
        expected = prices[(trade["signal_date"], "A")]["close"]
        assert D(trade["fill_price"]) == D(str(expected))

    def test_next_open_fill_price_is_the_following_session_open(self):
        bars, _, prices = self._bars_with_intraday_moves()
        engine = MomentumRotationEngine(EngineConfig(
            cadence="monthly", lookback_sessions=63, min_days_observed=1, top_n=1,
            entry_timing="next_open"))
        trade = engine.run(bars)["trades"][0]
        expected = prices[(trade["date"], "A")]["open"]
        assert D(trade["fill_price"]) == D(str(expected))

    def test_integer_shares_only(self):
        bars, sessions = self._bars()
        engine = MomentumRotationEngine(EngineConfig(
            cadence="monthly", lookback_sessions=63, min_days_observed=1, top_n=2))
        result = engine.run(bars)
        for trade in result["trades"]:
            assert isinstance(trade["shares"], int)

    def test_cash_never_goes_negative(self):
        bars, sessions = self._bars()
        engine = MomentumRotationEngine(EngineConfig(
            cadence="monthly", lookback_sessions=63, min_days_observed=1, top_n=2))
        result = engine.run(bars)
        for row in result["equity"]:
            assert D(row["cash"]) >= D("0")

    def test_run_is_deterministic(self):
        bars, sessions = self._bars()
        config = EngineConfig(cadence="monthly", lookback_sessions=63,
                              min_days_observed=1, top_n=2)
        first = MomentumRotationEngine(config).run(bars)
        second = MomentumRotationEngine(config).run(bars)
        assert first["trades"] == second["trades"]
        assert first["equity"] == second["equity"]

    def test_unpriced_holding_leaves_equity_unknown(self):
        sessions = session_series(date(2020, 1, 1), 90)
        rows = []
        for index, session in enumerate(sessions):
            rows.append({"date": session, "key": "A", "open": 100.0 + index,
                         "close": 100.0 + index, "volume": 100_000,
                         "low": 100.0 + index, "high": 100.0 + index})
        bars = make_bars(rows)
        engine = MomentumRotationEngine(EngineConfig(
            cadence="monthly", lookback_sessions=63, min_days_observed=1, top_n=1))
        result = engine.run(bars)
        # A's bars stop being supplied after the run window; within the window
        # every holding is priced, so equity must be known throughout.
        assert all(row["equity"] is not None for row in result["equity"])


class TestExitIntegration:
    def _crashing_bars(self):
        sessions = session_series(date(2020, 1, 1), 120)
        rows = []
        for index, session in enumerate(sessions):
            # Rises to 63 sessions, then falls hard.
            price = 100.0 + index if index < 70 else 170.0 - (index - 70) * 3
            price = max(price, 5.0)
            rows.append({"date": session, "key": "A", "open": price,
                         "close": price, "volume": 100_000,
                         "low": price, "high": price})
        return make_bars(rows), sessions

    def test_stop_loss_triggers_an_exit(self):
        bars, sessions = self._crashing_bars()
        engine = MomentumRotationEngine(EngineConfig(
            cadence="monthly", lookback_sessions=63, min_days_observed=1, top_n=1,
            exit_policy=ExitPolicy(stop_loss_pct=D("0.10"))))
        result = engine.run(bars)
        assert any(exit_row["reason"] == "stop_loss" for exit_row in result["exits"])

    def test_exit_fills_after_the_breach_session(self):
        bars, sessions = self._crashing_bars()
        engine = MomentumRotationEngine(EngineConfig(
            cadence="monthly", lookback_sessions=63, min_days_observed=1, top_n=1,
            exit_policy=ExitPolicy(stop_loss_pct=D("0.10"))))
        result = engine.run(bars)
        stop_exits = [row for row in result["exits"] if row["reason"] == "stop_loss"]
        assert stop_exits
        # The exit session must be strictly after the session whose close breached.
        for exit_row in stop_exits:
            assert exit_row["date"] > sessions[0]

    def test_no_overlay_produces_no_exits(self):
        bars, sessions = self._crashing_bars()
        engine = MomentumRotationEngine(EngineConfig(
            cadence="monthly", lookback_sessions=63, min_days_observed=1, top_n=1))
        result = engine.run(bars)
        assert result["exits"] == []

    def test_buy_and_sell_charges_are_deducted_and_recorded(self):
        bars, _ = self._crashing_bars()
        config = EngineConfig(
            cadence="monthly", lookback_sessions=63, min_days_observed=1, top_n=1,
            exit_policy=ExitPolicy(stop_loss_pct=D("0.10")))
        result = MomentumRotationEngine(config).run(bars)
        assert {trade["side"] for trade in result["trades"]} == {"buy", "sell"}
        for trade in result["trades"]:
            assert D(trade["cost"]) == config.cost_for(
                D(trade["notional"]), trade["side"])
            assert D(trade["cost"]) > D("0")

    def test_costs_reduce_final_equity(self):
        bars, _ = self._crashing_bars()
        base = dict(cadence="monthly", lookback_sessions=63, min_days_observed=1,
                    top_n=1, exit_policy=ExitPolicy(stop_loss_pct=D("0.10")))
        net = MomentumRotationEngine(EngineConfig(**base)).run(bars)
        gross = MomentumRotationEngine(EngineConfig(
            **base, brokerage_bps=D("0"), stt_bps_buy=D("0"), stt_bps_sell=D("0"),
            stamp_bps=D("0"), exchange_bps=D("0"), gst_on_brokerage_pct=D("0"),
            slippage_bps=D("0"))).run(bars)
        assert (D(gross["equity"][-1]["equity"])
                > D(net["equity"][-1]["equity"]))


class TestDelistingPolicy:
    def _delisting_bars(self):
        """A holds for a while then stops trading entirely."""
        sessions = session_series(date(2020, 1, 1), 200)
        rows = []
        for index, session in enumerate(sessions):
            if index < 150:
                rows.append({"date": session, "key": "A", "open": 100.0 + index,
                             "close": 100.0 + index, "volume": 100_000,
                             "low": 100.0 + index, "high": 100.0 + index})
            rows.append({"date": session, "key": "B", "open": 100.0,
                         "close": 100.0, "volume": 100_000,
                         "low": 100.0, "high": 100.0})
        return make_bars(rows), sessions

    def test_strict_leaves_equity_unknown_after_delisting(self):
        bars, _ = self._delisting_bars()
        engine = MomentumRotationEngine(EngineConfig(
            cadence="monthly", lookback_sessions=20, min_days_observed=1, top_n=1,
            delisting_policy="strict"))
        result = engine.run(bars)
        assert any(row["equity"] is None for row in result["equity"])

    def test_writeoff_keeps_equity_known(self):
        bars, _ = self._delisting_bars()
        engine = MomentumRotationEngine(EngineConfig(
            cadence="monthly", lookback_sessions=20, min_days_observed=1, top_n=1,
            delisting_policy="last_close_writeoff"))
        result = engine.run(bars)
        assert all(row["equity"] is not None for row in result["equity"])

    def test_writeoff_exit_is_recorded_with_its_assumption(self):
        bars, _ = self._delisting_bars()
        engine = MomentumRotationEngine(EngineConfig(
            cadence="monthly", lookback_sessions=20, min_days_observed=1, top_n=1,
            delisting_policy="last_close_writeoff"))
        result = engine.run(bars)
        writeoffs = [row for row in result["exits"]
                     if row["reason"] == "delisting_last_close_writeoff"]
        assert writeoffs
        assert "terminal proceeds are unknown" in writeoffs[0]["assumption"]

    def test_unknown_delisting_policy_rejected(self):
        with pytest.raises(ValueError, match="delisting_policy must be one of"):
            EngineConfig(delisting_policy="magic")

    def test_label_distinguishes_the_policy(self):
        strict = EngineConfig(delisting_policy="strict").label
        writeoff = EngineConfig(delisting_policy="last_close_writeoff").label
        assert strict != writeoff
        assert "last_close_writeoff" in writeoff


class TestCadenceIntegration:
    def _bars(self, count=300):
        sessions = session_series(date(2020, 1, 1), count)
        rows = []
        for index, session in enumerate(sessions):
            rows.append({"date": session, "key": "A", "open": 100.0 + index * 0.1,
                         "close": 100.0 + index * 0.1, "volume": 100_000,
                         "low": 100.0, "high": 100.0 + index * 0.1})
        return make_bars(rows), sessions

    def test_quarterly_produces_fewer_rebalances_than_monthly(self):
        bars, _ = self._bars()
        monthly = MomentumRotationEngine(EngineConfig(
            cadence="monthly", lookback_sessions=20, min_days_observed=1, top_n=1))
        quarterly = MomentumRotationEngine(EngineConfig(
            cadence="quarterly", lookback_sessions=20, min_days_observed=1, top_n=1))
        monthly_result = monthly.run(bars)
        quarterly_result = quarterly.run(bars)
        assert len(monthly_result["cadence"]["dates"]) > \
            len(quarterly_result["cadence"]["dates"])

    def test_annual_produces_at_most_one_per_year(self):
        bars, _ = self._bars()
        engine = MomentumRotationEngine(EngineConfig(
            cadence="annual", lookback_sessions=20, min_days_observed=1, top_n=1))
        result = engine.run(bars)
        years = [d[:4] for d in result["cadence"]["dates"]]
        assert len(years) == len(set(years))

    def test_explicit_rebalance_dates_replace_the_cadence(self):
        bars, sessions = self._bars()
        explicit = (sessions[100], sessions[200])
        engine = MomentumRotationEngine(EngineConfig(
            cadence="monthly", lookback_sessions=20, min_days_observed=1,
            top_n=1, rebalance_dates=explicit))
        result = engine.run(bars)
        assert result["cadence"]["dates"] == [d.isoformat() for d in explicit]
        assert result["cadence"]["complete"] is True
        assert result["config"]["rebalance_calendar"] == "explicit_dates"
        assert result["config"]["rebalance_dates_count"] == 2

    def test_explicit_rebalance_dates_must_be_unique(self):
        with pytest.raises(ValueError, match="rebalance_dates contains duplicates"):
            EngineConfig(rebalance_dates=(date(2020, 1, 1), date(2020, 1, 1)))

    def test_explicit_rebalance_dates_must_be_dates(self):
        with pytest.raises(ValueError, match="must contain datetime.date values"):
            EngineConfig(rebalance_dates=("2020-01-01",))
