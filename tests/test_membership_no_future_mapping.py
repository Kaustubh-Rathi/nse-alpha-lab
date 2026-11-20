"""Regression tests: membership snapshots must never resolve to a future bar.

`resolve_membership_to_keys` maps a snapshot's symbol set onto stable security
keys by taking each symbol's *nearest preceding* observation. A symbol whose
first bar postdates the snapshot has no preceding observation, so it must be
dropped from that snapshot's keys and counted as unresolved -- mapping it to
`iloc[0]` (its first, future bar) would be look-ahead leakage into the
backtest universe.
"""
from datetime import date

import pandas as pd

from nse_pipeline.backtest.momentum_grid import resolve_membership_to_keys


def _frame(rows):
    return pd.DataFrame(rows, columns=["date", "symbol", "key"])


def test_first_bar_after_snapshot_is_dropped_and_counted():
    """A symbol that only starts trading after `as_of` must not leak in."""
    as_of = date(2020, 6, 10)
    frame = _frame([
        (date(2020, 1, 1), "OLDONE", "ISIN:OLD"),
        (date(2020, 6, 1), "OLDONE", "ISIN:OLD"),
        # First bar of NEWIPO is strictly after the snapshot.
        (date(2021, 1, 1), "NEWIPO", "ISIN:NEW"),
        (date(2021, 2, 1), "NEWIPO", "ISIN:NEW"),
    ])
    membership = {as_of: {"OLDONE", "NEWIPO", "GHOST"}}

    resolved = resolve_membership_to_keys(membership, frame)
    keys = resolved[as_of]

    # (a) The future security must be absent from the snapshot's keys.
    assert "ISIN:NEW" not in keys
    assert keys == {"ISIN:OLD"}
    # (b) The future symbol is unresolved and counted, as is the one with no bars.
    assert resolved["_unresolved_symbol_dates"] == 2


def test_preceding_bar_still_used_for_non_trading_snapshot_date():
    """The nearest preceding observation is still used (no behaviour change)."""
    as_of = date(2020, 6, 10)  # no bar exactly on this date
    frame = _frame([
        (date(2020, 1, 1), "AAA", "ISIN:A"),
        (date(2020, 3, 1), "AAA", "ISIN:A"),
        (date(2020, 9, 1), "AAA", "ISIN:A"),  # future bar must not be chosen
    ])
    resolved = resolve_membership_to_keys({as_of: {"AAA"}}, frame)

    assert resolved[as_of] == {"ISIN:A"}
    assert resolved["_unresolved_symbol_dates"] == 0


def test_snapshot_before_every_first_bar_yields_no_membership_date():
    """A snapshot that predates all bars resolves to nothing and counts each symbol."""
    as_of = date(2019, 1, 1)
    frame = _frame([
        (date(2020, 1, 1), "AAA", "ISIN:A"),
        (date(2021, 1, 1), "BBB", "ISIN:B"),
    ])
    resolved = resolve_membership_to_keys({as_of: {"AAA", "BBB"}}, frame)

    assert as_of not in resolved
    assert resolved["_unresolved_symbol_dates"] == 2


def test_renamed_ticker_resolves_to_its_canonical_key():
    """A rename whose own ticker postdates the snapshot is still a real member.

    The security traded as OLDTICK before the snapshot and as NEWTICK after it.
    The snapshot names NEWTICK, which has no bar on the snapshot date, but the
    canonical key was already trading under its predecessor ticker. Dropping it
    would remove a genuinely tradable security from the universe.
    """
    as_of = date(2020, 6, 10)
    frame = _frame([
        (date(2020, 1, 1), "OLDTICK", "ISIN:R"),
        (date(2020, 3, 1), "OLDTICK", "ISIN:R"),
        (date(2021, 1, 1), "NEWTICK", "ISIN:R"),
    ])
    resolved = resolve_membership_to_keys({as_of: {"NEWTICK"}}, frame)

    assert resolved[as_of] == {"ISIN:R"}
    assert resolved["_unresolved_symbol_dates"] == 0


def test_security_with_no_prior_history_anywhere_is_still_dropped():
    """The canonical-key fallback must not resurrect a security that never traded."""
    as_of = date(2020, 6, 10)
    frame = _frame([
        (date(2021, 1, 1), "FUTURE", "ISIN:F"),
        (date(2021, 2, 1), "FUTURE", "ISIN:F"),
    ])
    resolved = resolve_membership_to_keys({as_of: {"FUTURE"}}, frame)

    assert as_of not in resolved
    assert resolved["_unresolved_symbol_dates"] == 1
