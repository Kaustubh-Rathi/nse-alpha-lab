from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

from nse_pipeline.backtest.momentum import (
    BarsFrame,
    EngineConfig,
    MomentumRotationEngine,
)


def test_empty_rebalance_universe_holds_existing_position_unchanged():
    sessions = [date(2020, 1, 1) + timedelta(days=index) for index in range(91)]
    bars = BarsFrame(pd.DataFrame([
        {
            "date": session,
            "key": "A",
            "open": 100.0,
            "close": 100.0,
            "volume": 100_000,
            "low": 100.0,
            "high": 100.0,
        }
        for session in sessions
    ]))
    config = EngineConfig(
        cadence="monthly",
        top_n=1,
        lookback_sessions=2,
        entry_timing="same_close",
        min_days_observed=1,
        initial_cash=Decimal("10000"),
    )
    universe = {
        date(2020, 1, 31): {"A"},
        date(2020, 2, 29): set(),
        date(2020, 3, 31): set(),
    }

    result = MomentumRotationEngine(config).run(bars, universe=universe)

    assert result["final_positions"]
    assert result["final_positions"][0]["key"] == "A"
    february_skip = [
        skip for skip in result["skips"]
        if skip["date"] == date(2020, 2, 29)
        and skip["reason"] == "no_proven_universe_hold_unchanged"
    ]
    assert february_skip == [{
        "date": date(2020, 2, 29),
        "key": None,
        "side": "rebalance",
        "reason": "no_proven_universe_hold_unchanged",
    }]
    assert not any(
        trade["date"] == date(2020, 2, 29) and trade["side"] == "sell"
        for trade in result["trades"]
    )
