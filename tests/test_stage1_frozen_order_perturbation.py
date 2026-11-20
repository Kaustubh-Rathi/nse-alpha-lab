"""Batch 6 frozen-order perturbation: sells before buys, rank/key order, and
unrelated-rank invariance.

These tests use the public ``Portfolio`` boundary directly and never reach the
legacy engine. They freeze an order set, perturb a later/unrelated rank, and
assert that only the intended later allocation changes while earlier fills stay
byte-identical.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from nse_pipeline.backtest.accounting_types import EventKind, EventTime, OpeningPrint, Phase, Position
from nse_pipeline.backtest.core import Portfolio
from tests.stage1_harness import ZERO_COSTS, execution

D0 = date(2024, 5, 31)
D1 = date(2024, 6, 3)
D2 = date(2024, 6, 4)


def _book(cash="1000", *, positions=()) -> Portfolio:
    return Portfolio(cash, on=D1, positions=positions, max_positions=4)


def test_buys_must_follow_frozen_rank_key_order_and_rejection_is_atomic():
    book = _book()
    book.reserve("o0", "AAA", 1, "100", "100", rank=0,
                 at=EventTime(D1, Phase.CLOSE_ORDERS, 0))
    book.reserve("o1", "BBB", 1, "100", "100", rank=1,
                 at=EventTime(D1, Phase.CLOSE_ORDERS, 1))
    book.observe_session(D2)

    before_state = book.snapshot
    before_events = book.events

    with pytest.raises(ValueError):
        book.buy("o1", OpeningPrint("BBB", D2, "100"), ZERO_COSTS, execution(),
                 at=EventTime(D2, Phase.OPEN_FILLS, 0))  # out of rank order
    assert book.snapshot == before_state
    assert book.events == before_events

    # Correct order fills both.
    book.buy("o0", OpeningPrint("AAA", D2, "100"), ZERO_COSTS, execution(),
             at=EventTime(D2, Phase.OPEN_FILLS, 0))
    book.buy("o1", OpeningPrint("BBB", D2, "100"), ZERO_COSTS, execution(),
             at=EventTime(D2, Phase.OPEN_FILLS, 1))


def test_opening_sells_precede_opening_buys():
    # A sell is rejected once a buy has already occurred this session.
    book = _book(positions=(Position("p", "CCC", 5, D0),))
    book.reserve("o0", "AAA", 1, "100", "100", rank=0,
                 at=EventTime(D1, Phase.CLOSE_ORDERS, 0))
    book.observe_session(D2)
    book.buy("o0", OpeningPrint("AAA", D2, "100"), ZERO_COSTS, execution(),
             at=EventTime(D2, Phase.OPEN_FILLS, 0))
    with pytest.raises(ValueError):
        book.sell("s1", "p", 1, OpeningPrint("CCC", D2, "100"), ZERO_COSTS, execution(),
                  at=EventTime(D2, Phase.OPEN_FILLS, 1))

    # A sell that precedes every buy is permitted, and the later buy still fills.
    book2 = _book(positions=(Position("p", "CCC", 5, D0),))
    book2.reserve("o0", "AAA", 1, "100", "100", rank=0,
                  at=EventTime(D1, Phase.CLOSE_ORDERS, 0))
    book2.observe_session(D2)
    book2.sell("s1", "p", 1, OpeningPrint("CCC", D2, "100"), ZERO_COSTS, execution(),
               at=EventTime(D2, Phase.OPEN_FILLS, 0))
    book2.buy("o0", OpeningPrint("AAA", D2, "100"), ZERO_COSTS, execution(),
              at=EventTime(D2, Phase.OPEN_FILLS, 1))


def test_perturbing_an_unrelated_rank_does_not_change_earlier_fills():
    def build(quantity):
        book = _book(cash="2000")
        book.reserve("o0", "AAA", 5, "500", "500", rank=0,
                     at=EventTime(D1, Phase.CLOSE_ORDERS, 0))
        book.reserve("o1", "BBB", quantity, "990", "990", rank=1,
                     at=EventTime(D1, Phase.CLOSE_ORDERS, 1))
        book.observe_session(D2)
        return book

    base = build(2)
    perturbed = build(9)

    base.buy("o0", OpeningPrint("AAA", D2, "100"), ZERO_COSTS, execution(),
             at=EventTime(D2, Phase.OPEN_FILLS, 0))
    perturbed.buy("o0", OpeningPrint("AAA", D2, "100"), ZERO_COSTS, execution(),
                  at=EventTime(D2, Phase.OPEN_FILLS, 0))

    # The earlier fill's economics are identical regardless of the later rank.
    base_fill = next(e for e in base.events if e.kind == EventKind.BUY and e.order_id == "o0")
    perturbed_fill = next(e for e in perturbed.events if e.kind == EventKind.BUY and e.order_id == "o0")
    assert (base_fill.quantity, base_fill.amount, base_fill.price, base_fill.key) == (
        perturbed_fill.quantity, perturbed_fill.amount, perturbed_fill.price, perturbed_fill.key)
    assert base.snapshot.positions == perturbed.snapshot.positions
    assert {c.claim_id: c.amount for c in base.snapshot.claims} == {
        c.claim_id: c.amount for c in perturbed.snapshot.claims}
