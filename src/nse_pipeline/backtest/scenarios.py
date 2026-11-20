"""Hypothetical scenario portfolios that cannot mutate authoritative accounting.

A scenario is built from a frozen reference snapshot and owns its own cash,
holdings, marks and statistics. Account values are typed hypothetical and are
rejected at any authoritative-publication or authoritative-statistics boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import hashlib

from .accounting_types import BookState, EventTime, ZERO, decimal, identifier, integer, money


class ScenarioStatisticError(ValueError):
    """Raised when a hypothetical scenario value is used as authoritative evidence."""


def _holding_rows(holdings):
    rows = []
    seen = set()
    for row in tuple(holdings):
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            raise ValueError("Scenario/reference holdings must be (security_key, shares) pairs")
        key, shares = row
        identifier(key)
        integer(shares)
        if key in seen:
            raise ValueError("Duplicate holding key in a snapshot")
        seen.add(key)
        if shares:
            rows.append((key, shares))
    return tuple(sorted(rows))


@dataclass(frozen=True)
class ReferenceSnapshot:
    cash: Decimal
    holdings: tuple[tuple[str, int], ...]
    claims: tuple[tuple, ...] = ()
    as_of: date | None = None

    def __post_init__(self):
        object.__setattr__(self, "cash", money(self.cash))
        object.__setattr__(self, "holdings", _holding_rows(self.holdings))
        claims = tuple(tuple(claim) for claim in self.claims)
        object.__setattr__(self, "claims", claims)
        if self.as_of is not None and type(self.as_of) is not date:
            raise ValueError("Reference as_of must be a date or None")

    @classmethod
    def from_book_state(cls, state: BookState) -> ReferenceSnapshot:
        if not isinstance(state, BookState):
            raise ValueError("A reference snapshot is derived from an immutable BookState")
        holdings = _holding_rows((position.key, position.shares)
                                 for position in state.positions if position.shares)
        claims = tuple((claim.claim_id, claim.kind.value,
                        None if claim.amount is None else str(claim.amount), claim.state)
                       for claim in state.claims)
        return cls(state.cash, holdings, claims,
                   None if state.as_of is None else state.as_of.session)

    def fingerprint(self) -> str:
        payload = "\n".join((
            "reference_snapshot",
            f"cash={self.cash}",
            f"as_of={self.as_of.isoformat() if self.as_of else ''}",
            "holdings=" + ";".join(f"{key}:{shares}" for key, shares in self.holdings),
            "claims=" + ";".join(repr(claim) for claim in self.claims),
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ScenarioHolding:
    security_key: str
    shares: int

    def __post_init__(self):
        identifier(self.security_key)
        integer(self.shares)


@dataclass(frozen=True)
class ScenarioMark:
    security_key: str
    price: Decimal
    on: date
    available_at: EventTime

    def __post_init__(self):
        identifier(self.security_key)
        object.__setattr__(self, "price", decimal(self.price, positive=True))
        if type(self.on) is not date or not isinstance(self.available_at, EventTime):
            raise ValueError("A scenario mark needs an observation date and availability time")
        if self.available_at.session < self.on:
            raise ValueError("A scenario mark cannot be available before it was observed")


@dataclass(frozen=True)
class ScenarioNav:
    nav: Decimal | None
    cash: Decimal
    marked_value: Decimal
    unpriced_keys: tuple[str, ...]
    reason: str
    liquidation_value: Decimal | None = None
    hypothetical: bool = True
    authoritative: bool = False
    assumption: str = ("Hypothetical scenario valuation; marked holdings are not "
                       "authoritative accounting or realizable liquidation proceeds.")

    def __post_init__(self):
        if self.hypothetical is not True or self.authoritative is not False:
            raise ValueError("Scenario NAV is always hypothetical and never authoritative")
        object.__setattr__(self, "cash", money(self.cash))
        object.__setattr__(self, "marked_value", money(self.marked_value))
        object.__setattr__(self, "unpriced_keys", tuple(sorted(set(self.unpriced_keys))))
        if self.liquidation_value is not None:
            object.__setattr__(self, "liquidation_value", money(self.liquidation_value))

    @property
    def authoritative_nav(self):
        return None

    @property
    def nav_to_liquidation(self) -> Decimal | None:
        if self.nav is None or self.liquidation_value is None:
            return None
        return self.nav - self.liquidation_value

    def require_authoritative(self):
        raise ValueError("hypothetical_scenario_value_is_not_authoritative")


@dataclass(frozen=True)
class ScenarioPortfolio:
    reference: ReferenceSnapshot
    cash: Decimal
    holdings: tuple[ScenarioHolding, ...]
    label: str = "synthetic_fixture"

    def __post_init__(self):
        if not isinstance(self.reference, ReferenceSnapshot):
            raise ValueError("A scenario requires an immutable ReferenceSnapshot")
        object.__setattr__(self, "cash", money(self.cash))
        rows = tuple(self.holdings)
        if not all(isinstance(row, ScenarioHolding) for row in rows):
            raise ValueError("Scenario holdings must be typed ScenarioHolding records")
        keys = [row.security_key for row in rows if row.shares]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate live scenario holding")
        object.__setattr__(self, "holdings", tuple(row for row in rows if row.shares))
        identifier(self.label)

    @classmethod
    def from_reference(cls, reference: ReferenceSnapshot) -> ScenarioPortfolio:
        if not isinstance(reference, ReferenceSnapshot):
            raise ValueError("A scenario requires an immutable ReferenceSnapshot")
        return cls(reference, reference.cash,
                   tuple(ScenarioHolding(key, shares) for key, shares in reference.holdings))

    @property
    def holdings_map(self) -> dict[str, int]:
        return {row.security_key: row.shares for row in self.holdings}

    def _quantity(self, key, quantity):
        identifier(key)
        integer(quantity, minimum=1)
        return self.holdings_map.get(key, 0)

    def buy(self, key, quantity, price, *, fee) -> ScenarioPortfolio:
        self._quantity(key, quantity)
        unit = decimal(price, positive=True)
        fee = money(fee)
        cost = money(unit * quantity) + fee
        if cost > self.cash:
            raise ValueError("Scenario cash is insufficient for the hypothetical purchase")
        holdings = self.holdings_map
        holdings[key] = holdings.get(key, 0) + quantity
        return ScenarioPortfolio(
            self.reference, self.cash - cost,
            tuple(ScenarioHolding(name, shares) for name, shares in sorted(holdings.items())),
            self.label)

    def sell(self, key, quantity, price, *, fee) -> ScenarioPortfolio:
        held = self._quantity(key, quantity)
        if quantity > held:
            raise ValueError("Scenario cannot sell more shares than it holds")
        unit = decimal(price, positive=True)
        fee = money(fee)
        proceeds = money(unit * quantity) - fee
        if proceeds < 0:
            raise ValueError("Scenario fees exceed hypothetical sale proceeds")
        holdings = self.holdings_map
        holdings[key] = held - quantity
        return ScenarioPortfolio(
            self.reference, self.cash + proceeds,
            tuple(ScenarioHolding(name, shares) for name, shares in sorted(holdings.items())
                  if shares),
            self.label)

    def nav(self, marks, *, at: EventTime, liquidation_value=None) -> ScenarioNav:
        if not isinstance(at, EventTime):
            raise ValueError("A scenario valuation needs an explicit event time")
        marks = tuple(marks)
        if not all(isinstance(mark, ScenarioMark) for mark in marks):
            raise ValueError("Scenario marks must be typed ScenarioMark records")
        indexed = {}
        for mark in marks:
            if mark.security_key in indexed:
                raise ValueError("Ambiguous multiple scenario marks for one security")
            indexed[mark.security_key] = mark
        marked = ZERO
        unpriced = []
        for holding in self.holdings:
            mark = indexed.get(holding.security_key)
            if (mark is None or mark.available_at > at or mark.on > at.session):
                unpriced.append(holding.security_key)
            else:
                marked += holding.shares * mark.price
        if unpriced:
            return ScenarioNav(None, self.cash, marked, tuple(unpriced), "unpriced_holdings",
                               liquidation_value)
        if not self.holdings:
            return ScenarioNav(self.cash, self.cash, ZERO, (), "no_holdings", liquidation_value)
        return ScenarioNav(self.cash + marked, self.cash, marked, (), "complete_marks",
                           liquidation_value)


@dataclass(frozen=True)
class ScenarioStatistics:
    navs: tuple[ScenarioNav, ...] = ()
    scope: str = "hypothetical_scenario_only"

    def __post_init__(self):
        if self.scope != "hypothetical_scenario_only":
            raise ScenarioStatisticError(
                "scenario_statistics_are_not_authoritative")
        navs = tuple(self.navs)
        if not all(isinstance(nav, ScenarioNav) for nav in navs):
            raise ScenarioStatisticError("scenario_statistics_require_scenario_navs")
        object.__setattr__(self, "navs", navs)

    @classmethod
    def from_navs(cls, navs) -> ScenarioStatistics:
        return cls(tuple(navs))

    @classmethod
    def from_valuation(cls, valuation) -> ScenarioStatistics:
        raise ScenarioStatisticError(
            "authoritative_valuation_cannot_be_used_as_scenario_statistics")

    @property
    def latest_nav(self) -> Decimal | None:
        return self.navs[-1].nav if self.navs else None

    def authoritative_statistics(self):
        raise ScenarioStatisticError("scenario_statistics_are_not_authoritative")
