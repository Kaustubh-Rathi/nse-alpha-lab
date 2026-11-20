from datetime import date
from decimal import Decimal as D
from fractions import Fraction
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("case_id", ("mixed", "larger_unknown_reference", "no_reference", "empty"))
def test_production_valuation_matches_independent_reference_fixtures(case_id):
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Position, EventTime, Phase
    from nse_pipeline.backtest.valuation import Mark, value

    case = next(c for c in json.loads((Path(__file__).parent / "golden/valuation_cases.json").read_text()) if c["id"] == case_id)
    positions, marks, references = [], [], []
    at = EventTime(date(2024, 6, 4), Phase.CLOSE_VALUATION)
    old = EventTime(date(2024, 6, 3), Phase.CLOSE_VALUATION)
    for i, p in enumerate(case["positions"]):
        pos_id = f"p{i}"
        positions.append(Position(pos_id, f"KEY{i}", int(p["shares"]), date(2024, 5, 31)))
        if p["mark"] is not None:
            marks.append(Mark(pos_id, p["mark"], at.session, at))
        if p["reference"] is not None:
            references.append(Mark(pos_id, p["reference"], old.session, old, "reference"))
    book = Portfolio(case["cash"], on=at.session, positions=tuple(positions))
    result = value(book.snapshot, at, marks=marks, references=references)
    expected = case["expected"]
    assert result.nav == (None if expected["nav"] is None else D(expected["nav"]))
    assert result.position_completeness == Fraction(expected["position_completeness"])
    assert result.reference_notional_coverage == (None if expected["reference_coverage"] is None else Fraction(expected["reference_coverage"]))
    assert result.liquidation_known_zero_friction == D(expected["liquidation_known_zero_friction"])


def test_total_cash_not_available_cash_and_liabilities_enter_nav_once():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Claim, ClaimKind, EventTime, Phase
    from nse_pipeline.backtest.valuation import value
    book = Portfolio("1000", on=date(2024, 6, 3), claims=(
        Claim("r", ClaimKind.DIVIDEND, "50", "opening fixture"),
        Claim("l", ClaimKind.OTHER_PAYABLE, "100", "known obligation")))
    at = EventTime(date(2024, 6, 3), Phase.CLOSE_ORDERS)
    book.reserve("o", "AAA", 3, "300", "300", at=at)
    result = value(book.snapshot, at)
    assert result.cash_available == 600
    assert result.nav == 950  # settled1000 + receivable50 - payable100
    assert result.liquidation_known_zero_friction == 900
    assert result.nav_to_cash_liquidation_difference == 50


def test_stale_future_and_missing_prices_cannot_publish_nav():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Position, EventTime, Phase
    from nse_pipeline.backtest.valuation import Mark, value
    book = Portfolio("200", on=date(2024, 6, 4), positions=(Position("p", "AAA", 100, date(2024, 5, 31)),))
    at = EventTime(date(2024, 6, 4), Phase.CLOSE_VALUATION)
    old = EventTime(date(2024, 6, 3), Phase.CLOSE_VALUATION)
    future = EventTime(date(2024, 6, 5), Phase.CLOSE_VALUATION)
    for supplied in ([], [Mark("p", "100", old.session, old)], [Mark("p", "100", future.session, future)]):
        result = value(book.snapshot, at, marks=supplied)
        assert result.nav is None and result.unresolved_positions == ("p",)
        assert result.nav_to_cash_liquidation_difference is None
    book.observe_session(future.session)
    result = value(book.snapshot, future, marks=[Mark("p", "80", future.session, future)])
    assert result.nav == 8200  # golden absence/reopening, no forward filling
    with pytest.raises(ValueError):
        Mark("p", "0", at.session, at)


def test_snapshot_cannot_be_reused_for_a_past_or_unobserved_valuation_date():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.valuation import value
    book = Portfolio("100", on=date(2024, 6, 4))
    book.reserve("o", "AAA", 1, "100", "100", at=EventTime(date(2024, 6, 4), Phase.CLOSE_ORDERS))
    for at in (EventTime(date(2024, 6, 3), Phase.CLOSE_VALUATION),
               EventTime(date(2024, 6, 4), Phase.OPEN_FILLS),
               EventTime(date(2024, 6, 5), Phase.CLOSE_VALUATION)):
        with pytest.raises(ValueError):
            value(book.snapshot, at)


def test_unknown_receivable_or_liability_blocks_nav_not_zero_filled():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Claim, ClaimKind, EventTime, Phase
    from nse_pipeline.backtest.valuation import value
    at = EventTime(date(2024, 6, 4), Phase.CLOSE_VALUATION)
    for kind in (ClaimKind.OTHER, ClaimKind.OTHER_PAYABLE):
        book = Portfolio("1000", on=at.session, claims=(Claim("unknown", kind, None, "amount not established"),))
        result = value(book.snapshot, at)
        assert result.nav is None and result.unresolved_assets == ("unknown",)
        if kind == ClaimKind.OTHER_PAYABLE:
            assert result.cash_available is None and result.liquidation_known_zero_friction is None


GOLDEN_DIR = Path(__file__).parent / "golden"
VALUATION_SESSION = date(2024, 6, 4)
REFERENCE_SESSION = date(2024, 6, 3)


def _load_golden(name):
    payload = json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["derivation"].strip()
    return payload


COVERAGE_CASES = _load_golden("coverage_state_cases.json")["cases"]
PRECISION_CASES = _load_golden("precision_cases.json")["cases"]


def _coverage_case(case_id):
    return next(case for case in COVERAGE_CASES if case["id"] == case_id)


def _precision_case(case_id):
    return next(case for case in PRECISION_CASES if case["id"] == case_id)


def _value_for_coverage(case):
    from nse_pipeline.backtest.accounting_types import Claim, ClaimKind, EventTime, Phase, Position
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.valuation import Mark, value

    at = EventTime(VALUATION_SESSION, Phase.CLOSE_VALUATION)
    reference_at = EventTime(REFERENCE_SESSION, Phase.CLOSE_VALUATION)
    positions = tuple(
        Position(f"p{i}", f"KEY{i}", int(position["shares"]), date(2024, 5, 31),
                 blocked_reason=position.get("blocked_reason", ""))
        for i, position in enumerate(case["positions"]))
    claims = tuple(
        Claim(claim["id"], ClaimKind(claim["kind"]), claim["amount"], claim["citation"])
        for claim in case["claims"])
    book = Portfolio(case["cash"], on=VALUATION_SESSION, positions=positions, claims=claims)
    marks, references = [], []
    for i, position in enumerate(case["positions"]):
        if position["mark"] is not None:
            marks.append(Mark(f"p{i}", position["mark"], at.session, at))
        if position["reference"] is not None:
            references.append(Mark(f"p{i}", position["reference"], REFERENCE_SESSION,
                                   reference_at, "reference"))
    return value(book.snapshot, at, marks=tuple(marks), references=tuple(references))


def _value_for_precision(case):
    from nse_pipeline.backtest.accounting_types import EventTime, Phase, Position
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.valuation import Mark, value

    at = EventTime(VALUATION_SESSION, Phase.CLOSE_VALUATION)
    positions, marks = [], []
    for entry in case["positions"]:
        for _ in range(entry.get("count", 1)):
            index = len(positions)
            positions.append(Position(f"p{index}", f"KEY{index}", entry["shares"],
                                      date(2024, 5, 31)))
            marks.append(Mark(f"p{index}", entry["price"], at.session, at))
    book = Portfolio(case["cash"], on=VALUATION_SESSION, positions=tuple(positions),
                     max_positions=max(1, len(positions)))
    return value(book.snapshot, at, marks=tuple(marks))


@pytest.mark.parametrize("case", COVERAGE_CASES, ids=[case["id"] for case in COVERAGE_CASES])
def test_reference_coverage_state_table(case):
    result = _value_for_coverage(case)
    expected = case["expected"]
    assert result.reference_coverage_reason == expected["reference_coverage_reason"]
    assert result.marked_value_priced == D(expected["marked_value_priced"])
    assert result.known_reference_subtotal == (
        None if expected["known_reference_subtotal"] is None else D(expected["known_reference_subtotal"]))
    assert result.gross_reference_notional == (
        None if expected["gross_reference_notional"] is None else D(expected["gross_reference_notional"]))
    assert result.position_completeness == Fraction(expected["position_completeness"])
    assert result.reference_notional_coverage == (
        None if expected["reference_notional_coverage"] is None
        else Fraction(expected["reference_notional_coverage"]))
    assert result.nav == (None if expected["nav"] is None else D(expected["nav"]))
    assert (result.nav_reported is None) == (expected["nav"] is None)
    assert result.nav_reason == expected["nav_reason"]


def test_fully_priced_book_is_complete_not_mixed():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase, Position
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.valuation import Mark, value

    at = EventTime(VALUATION_SESSION, Phase.CLOSE_VALUATION)
    positions = tuple(Position(f"p{i}", f"KEY{i}", shares, date(2024, 5, 31))
                      for i, shares in enumerate((100, 200, 50)))
    marks = tuple(Mark(position.position_id, price, at.session, at)
                  for position, price in zip(positions, ("10", "5", "2")))
    result = value(Portfolio("0", on=VALUATION_SESSION, positions=positions).snapshot,
                   at, marks=marks)
    assert result.reference_coverage_reason == "complete_current_date"
    assert result.unresolved_positions == ()
    assert result.missing_reference_count == 0
    assert result.known_reference_subtotal == D("0")
    assert result.reference_notional_coverage == Fraction(1)
    assert result.gross_reference_notional == result.marked_value_priced == D("2100")
    assert result.nav == D("2100") and result.nav_reason == "nav_available"

    empty = value(Portfolio("1000", on=VALUATION_SESSION).snapshot, at)
    assert empty.reference_coverage_reason == "no_positions"
    assert empty.reference_notional_coverage is None
    assert empty.nav == D("1000") and empty.nav_reason == "nav_available"


def test_unknown_asset_blocks_nav_but_not_coverage():
    from nse_pipeline.backtest.accounting_types import Claim, ClaimKind, EventTime, Phase, Position
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.valuation import Mark, value

    b7 = _value_for_coverage(_coverage_case("B7"))
    assert b7.reference_coverage_reason == "complete_current_date"
    assert b7.nav is None and b7.nav_reason == "unknown_assets"
    assert b7.unresolved_settlement_claims == ()

    at = EventTime(VALUATION_SESSION, Phase.CLOSE_VALUATION)
    reference_at = EventTime(REFERENCE_SESSION, Phase.CLOSE_VALUATION)
    positions = (Position("p0", "KEY0", 100, date(2024, 5, 31)),
                 Position("p1", "KEY1", 100, date(2024, 5, 31)))
    book = Portfolio("15000", on=VALUATION_SESSION, positions=positions, claims=(
        Claim("claim", ClaimKind.OTHER, None, "amount not established"),))
    result = value(book.snapshot, at,
                   marks=(Mark("p0", "100", at.session, at),),
                   references=(Mark("p1", "50", REFERENCE_SESSION, reference_at, "reference"),))
    assert result.reference_coverage_reason == "mixed_current_and_dated_reference"
    assert result.nav is None
    assert result.nav_reason == "unpriced_positions_and_unknown_assets"


def test_reference_coverage_and_nav_reason_vocabularies_are_closed():
    from nse_pipeline.backtest.valuation import NAV_REASONS, REFERENCE_COVERAGE_REASONS

    assert REFERENCE_COVERAGE_REASONS == (
        "no_positions",
        "missing_reference",
        "zero_gross_notional",
        "complete_current_date",
        "dated_reference_only",
        "mixed_current_and_dated_reference",
    )
    assert NAV_REASONS == (
        "nav_available",
        "unpriced_positions",
        "unknown_assets",
        "unpriced_positions_and_unknown_assets",
    )
    observed_coverage = {_value_for_coverage(case).reference_coverage_reason
                         for case in COVERAGE_CASES}
    observed_nav = {_value_for_coverage(case).nav_reason for case in COVERAGE_CASES}
    assert observed_coverage == {
        "no_positions", "missing_reference", "complete_current_date",
        "dated_reference_only", "mixed_current_and_dated_reference"}
    assert observed_coverage <= set(REFERENCE_COVERAGE_REASONS)
    assert observed_nav <= set(NAV_REASONS)


def test_missing_reference_preserves_a_nonzero_partial_reference_subtotal():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase, Position
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.valuation import Mark, value

    at = EventTime(VALUATION_SESSION, Phase.CLOSE_VALUATION)
    reference_at = EventTime(REFERENCE_SESSION, Phase.CLOSE_VALUATION)
    positions = (Position("p0", "KEY0", 100, date(2024, 5, 31)),
                 Position("p1", "KEY1", 10, date(2024, 5, 31)),
                 Position("p2", "KEY2", 10, date(2024, 5, 31)))
    result = value(Portfolio("0", on=VALUATION_SESSION, positions=positions).snapshot, at,
                   marks=(Mark("p0", "10", at.session, at),),
                   references=(Mark("p1", "50", REFERENCE_SESSION, reference_at, "reference"),))
    assert result.reference_coverage_reason == "missing_reference"
    assert result.marked_value_priced == D("1000")
    assert result.known_reference_subtotal == D("500")
    assert result.gross_reference_notional is None
    assert result.reference_notional_coverage is None
    assert result.position_completeness == Fraction(1, 3)
    assert result.nav is None and result.nav_reason == "unpriced_positions"


def test_known_unscheduled_claims_are_disclosed_without_changing_diagnostics():
    from nse_pipeline.backtest.accounting_types import (
        Claim, ClaimKind, Due, EventTime, Phase, Position)
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.valuation import Mark, value

    at = EventTime(VALUATION_SESSION, Phase.CLOSE_VALUATION)
    claims = (
        Claim("z-claim", ClaimKind.OTHER, "50", "opening receivable"),
        Claim("a-claim", ClaimKind.OTHER, "25", "opening receivable"),
        Claim("scheduled", ClaimKind.OTHER, "10", "dated receivable",
              due=Due(Phase.POST_OPEN_SETTLEMENT, session_index=1)),
    )
    book = Portfolio("1000", on=VALUATION_SESSION,
                     positions=(Position("p", "KEY", 100, date(2024, 5, 31)),),
                     claims=claims)
    result = value(book.snapshot, at, marks=(Mark("p", "10", at.session, at),))
    assert result.unresolved_settlement_claims == ("a-claim", "z-claim")
    assert result.reference_coverage_reason == "complete_current_date"
    assert result.nav == D("2085") and result.nav_reason == "nav_available"


def test_sub_paise_nav_is_exact_then_reported():
    result = _value_for_precision(_precision_case("three_shares_33_335"))
    assert result.marked_value_priced == D("100.005")
    assert result.nav == D("100.005")
    assert result.nav_reported == D("100.01")
    assert result.nav_reported != result.nav


@pytest.mark.parametrize("case_id", ("two_positions_half_paise", "two_hundred_positions_half_paise"))
def test_per_position_rounding_is_not_applied(case_id):
    case = _precision_case(case_id)
    expected = case["expected"]
    result = _value_for_precision(case)
    assert result.marked_value_priced == D(expected["marked_value_priced"])
    assert result.nav == D(expected["nav"])
    assert result.nav_reported == D(expected["nav_reported"])
    assert result.nav != D(expected["per_position_rounding_total"])
    assert result.nav_reported != D(expected["per_position_rounding_total"])


def test_report_serializes_exact_and_reported_as_strings():
    """No production Valuation serializer exists; this local adapter is the report boundary.

    The assertion is deliberately test-local: changing production report code is out
    of scope, but the presentation contract (exact nav and rounded nav_reported as
    decimal strings) is still frozen here.
    """
    result = _value_for_precision(_precision_case("three_shares_33_335"))
    payload = {
        "nav": None if result.nav is None else str(result.nav),
        "nav_reported": None if result.nav_reported is None else str(result.nav_reported),
        "marked_value_priced": str(result.marked_value_priced),
        "reference_coverage_reason": result.reference_coverage_reason,
        "nav_reason": result.nav_reason,
        "position_completeness": str(result.position_completeness),
    }
    encoded = json.dumps(payload, sort_keys=True)
    decoded = json.loads(encoded)
    assert decoded["nav"] == "100.005"
    assert decoded["nav_reported"] == "100.01"
    assert decoded["marked_value_priced"] == "100.005"
    assert '"nav": "100.005"' in encoded
    assert '"nav_reported": "100.01"' in encoded

    def simple_return(previous, current):
        return Fraction(current) / Fraction(previous) - 1

    exact_navs = (D("100.005"), D("100.010"))
    reported_navs = (D("100.01"), D("100.01"))
    assert simple_return(*exact_navs) == Fraction(1, 20001)
    assert simple_return(*reported_navs) == 0
    assert simple_return(*exact_navs) != simple_return(*reported_navs)


@pytest.mark.parametrize("name", ("coverage_state_cases.json", "precision_cases.json"))
def test_new_goldens_carry_schema_derivation_and_working(name):
    payload = _load_golden(name)
    ids = [case["id"] for case in payload["cases"]]
    assert len(ids) == len(set(ids))
    assert all(case["working"].strip() for case in payload["cases"])
