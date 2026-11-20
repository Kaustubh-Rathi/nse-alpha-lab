"""Immutable accounting records and exact monetary input boundaries."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum, IntEnum
from fractions import Fraction
import re
from nse_pipeline.specs import CostSpec, ExecutionSpec, ValuationPolicy

ZERO = Decimal("0")
CENT = Decimal("0.01")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def decimal(value, *, positive=False) -> Decimal:
    if type(value) not in (str, int, Decimal):
        raise TypeError("Use Decimal, an integer, or a decimal string; not float/bool")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Invalid decimal input") from error
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError("Amount must be finite and nonnegative (positive where required)")
    return result


def money(value, *, positive=False) -> Decimal:
    try:
        result = decimal(value, positive=positive).quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as error:
        raise ValueError("Amount exceeds supported Decimal precision") from error
    if positive and result == 0:
        raise ValueError("Amount is below monetary precision")
    return result


def integer(value, *, minimum=0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"Raw shares/counts must be integers >= {minimum}")
    return value


def identifier(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("An explicit nonempty stable identifier is required")
    return value


def sha256_digest(value, name: str = "digest") -> str:
    """Validate a lowercase 64-character hex SHA-256 digest without normalizing it."""
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase 64-character hex digest")
    return value


class Phase(IntEnum):
    PRE_OPEN_EVIDENCE = 10
    PRE_OPEN_SETTLEMENT = 20
    PRE_OPEN_ACTIONS = 30
    OPEN_FILLS = 40
    POST_OPEN_SETTLEMENT = 50
    CLOSE_VALUATION = 60
    CLOSE_ORDERS = 70
    CLOSE_AUDIT = 80


@dataclass(frozen=True, order=True)
class EventTime:
    session: date
    phase: Phase
    sequence: int = 0

    def __post_init__(self):
        if type(self.session) is not date or not isinstance(self.phase, Phase):
            raise ValueError("Event time requires a session date and a defined Phase")
        integer(self.sequence)


class ClaimKind(str, Enum):
    BUY_PAYABLE = "buy_payable"
    OTHER_PAYABLE = "other_payable"
    SALE = "sale_receivable"
    DIVIDEND = "dividend_receivable"
    OTHER = "other_receivable"


@dataclass(frozen=True)
class Due:
    phase: Phase
    session_index: int | None = None
    on: date | None = None

    def __post_init__(self):
        if (self.session_index is None) == (self.on is None):
            raise ValueError("Settlement must use exactly one dated or session-lag basis")
        if not isinstance(self.phase, Phase) or self.phase not in (Phase.PRE_OPEN_SETTLEMENT, Phase.POST_OPEN_SETTLEMENT):
            raise ValueError("Not a settlement phase")
        if self.session_index is not None:
            integer(self.session_index)
        if self.on is not None and type(self.on) is not date:
            raise ValueError("Payment date must be a date")

    def reached(self, at: EventTime, session_index: int) -> bool:
        return (at.phase == self.phase and
                (session_index >= self.session_index if self.session_index is not None else at.session >= self.on))


@dataclass(frozen=True)
class Claim:
    claim_id: str
    kind: ClaimKind
    amount: Decimal | None
    citation: str
    due: Due | None = None
    position_id: str = ""
    delivery_shares: int = 0
    basis: str = "verified"
    state: str = ""
    unresolved_reason: str = ""

    def __post_init__(self):
        identifier(self.claim_id)
        identifier(self.citation)
        if not isinstance(self.kind, ClaimKind):
            raise ValueError("Unknown claim kind")
        if self.amount is not None:
            object.__setattr__(self, "amount", money(self.amount))
        integer(self.delivery_shares)
        if self.due is not None and not isinstance(self.due, Due):
            raise ValueError("A claim needs an explicit typed settlement schedule")
        if self.kind == ClaimKind.BUY_PAYABLE:
            if not self.position_id or self.delivery_shares == 0 or self.amount is None:
                raise ValueError("A purchase payable must identify known funded quantity and ownership")
        elif self.delivery_shares:
            raise ValueError("Only purchase payables may deliver shares")
        if self.basis not in ("verified", "modeled", "opening_balance"):
            raise ValueError("Claim basis must disclose its evidence/model status")
        if self.state not in ("", "unbound", "accrued_held", "scheduled", "confirmed", "voided"):
            raise ValueError("Unknown claim lifecycle state")
        if not self.state:
            if self.due is None:
                object.__setattr__(self, "state", "unbound")
                if not self.unresolved_reason:
                    object.__setattr__(self, "unresolved_reason", "no_settlement_schedule")
            else:
                object.__setattr__(self, "state", "scheduled")
        if self.state == "unbound" and self.due is not None:
            raise ValueError("An unbound claim cannot carry a settlement schedule")
        if self.state == "scheduled" and self.due is None:
            raise ValueError("A scheduled claim needs a causal settlement schedule")
        if self.state in ("accrued_held", "confirmed") and self.amount is None:
            raise ValueError("A held or confirmed claim needs a known amount")
        if self.state == "accrued_held" and self.due is not None:
            raise ValueError("A held claim cannot carry a settlement schedule before confirmation")
        if self.state == "accrued_held" and not self.unresolved_reason:
            raise ValueError("A held claim must disclose why its payment evidence is pending")
        if self.state == "confirmed" and (self.due is None or self.basis != "verified"):
            raise ValueError("A confirmed claim needs verified evidence and a causal due date")
        if self.state == "confirmed" and self.unresolved_reason:
            raise ValueError("A confirmed claim has no unresolved payment reason")


@dataclass(frozen=True)
class OpeningCondition:
    """A predeclared inclusive price band evaluated only at the next opening.

    Bounds are exact positive Decimal/string/integer inputs; floats and bools are
    deliberately rejected. Strategy logic that computes a threshold belongs outside
    this accounting record and must supply the resulting bound when freezing an order.
    """
    min_price: Decimal | None = None
    max_price: Decimal | None = None

    def __post_init__(self):
        if self.min_price is None and self.max_price is None:
            raise ValueError("An opening condition needs at least one price bound")
        for name in ("min_price", "max_price"):
            bound = getattr(self, name)
            if bound is not None:
                object.__setattr__(self, name, decimal(bound, positive=True))
        if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
            raise ValueError("Opening condition minimum price exceeds maximum price")

    def met(self, price) -> bool:
        """Evaluate against the raw opening print, never a slipped fill price."""
        actual = decimal(price, positive=True)
        return ((self.min_price is None or actual >= self.min_price)
                and (self.max_price is None or actual <= self.max_price))


@dataclass(frozen=True)
class BuyOrder:
    order_id: str
    key: str
    quantity: int
    reserved: Decimal
    capacity_notional: Decimal
    signal_at: EventTime
    signal_session_index: int
    rank: int = 0
    condition: OpeningCondition | None = None

    def __post_init__(self):
        if self.condition is not None and not isinstance(self.condition, OpeningCondition):
            raise ValueError("A frozen order condition must be an OpeningCondition or None")

    @property
    def opening_condition(self):
        """Read-only descriptive alias for the frozen condition."""
        return self.condition


@dataclass(frozen=True)
class Position:
    position_id: str
    key: str
    shares: int
    opened_on: date
    delivered_shares: int | None = None
    blocked_reason: str = ""

    def __post_init__(self):
        identifier(self.position_id)
        identifier(self.key)
        integer(self.shares)
        delivered = self.shares if self.delivered_shares is None else integer(self.delivered_shares)
        if delivered > self.shares or type(self.opened_on) is not date:
            raise ValueError("Invalid delivery quantity or ownership date")
        object.__setattr__(self, "delivered_shares", delivered)


@dataclass(frozen=True)
class OpeningPrint:
    key: str
    session: date
    price: Decimal

    def __post_init__(self):
        identifier(self.key)
        if type(self.session) is not date:
            raise ValueError("Opening print needs its actual session")
        object.__setattr__(self, "price", decimal(self.price, positive=True))


@dataclass(frozen=True)
class Evidence:
    """A supplied documentary decision, not an automatic document verifier.

    Cross-ISIN continuity is deliberately NOT inferred by this component.
    """
    citation: str
    key: str
    effective_on: date
    available_at: EventTime
    terms_verified: bool

    def __post_init__(self):
        identifier(self.citation)
        identifier(self.key)
        if (type(self.effective_on) is not date or not isinstance(self.available_at, EventTime)
                or type(self.terms_verified) is not bool):
            raise ValueError("Evidence needs explicit dates, availability phase and verification status")

    def require_applicable(self, key, at):
        if self.key != key or self.available_at > at or self.effective_on != at.session:
            raise ValueError("Evidence is not applicable to this security, date and information horizon")


@dataclass(frozen=True)
class Balances:
    cash: Decimal
    order_reserved: Decimal
    payable_reserved: Decimal | None
    sale_receivable: Decimal | None
    dividend_receivable: Decimal | None
    other_receivable: Decimal | None
    liabilities: Decimal | None

    @property
    def receivables(self):
        values = (self.sale_receivable, self.dividend_receivable, self.other_receivable)
        return None if None in values else sum(values, ZERO)

    @property
    def cash_available(self):
        return None if self.payable_reserved is None else self.cash - self.order_reserved - self.payable_reserved


class ExternalFlowDirection(str, Enum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"


class EventKind(str, Enum):
    RESERVE = "reserve"
    CANCEL = "cancel"
    BUY = "buy"
    BUY_SKIPPED = "buy_skipped"
    SELL = "sell"
    SELL_SKIPPED = "sell_skipped"
    SETTLE_BUY = "settle_buy"
    SETTLE_SALE = "settle_sale"
    SETTLE_DIVIDEND = "settle_dividend"
    SETTLE_OTHER = "settle_other"
    SETTLE_LIABILITY = "settle_liability"
    SPLIT = "split"
    BONUS = "bonus"
    ACTION_BLOCKED = "action_blocked"
    ACTION_ORDERS_CANCELED = "action_orders_canceled"
    DIVIDEND_ENTITLEMENT = "dividend_entitlement"
    DIVIDEND_ACCRUAL = "dividend_accrual"
    DIVIDEND_PAYMENT_CONFIRMED = "dividend_payment_confirmed"
    DIVIDEND_VOIDED = "dividend_voided"
    DIVIDEND_CORRECTED = "dividend_corrected"
    DIVIDEND_BLOCKED = "dividend_blocked"
    LIABILITY = "liability"
    REDEMPTION = "redemption"
    EXTERNAL_FLOW = "external_flow"


@dataclass(frozen=True)
class Posting:
    account: str
    amount: Decimal
    position_id: str = ""


@dataclass(frozen=True)
class LedgerEvent:
    event_id: str
    at: EventTime
    kind: EventKind
    amount: Decimal = ZERO
    quantity: int = 0
    price: Decimal = ZERO
    fee: Decimal = ZERO
    position_id: str = ""
    key: str = ""
    order_id: str = ""
    claim_id: str = ""
    released_reserve: Decimal = ZERO
    postings: tuple[Posting, ...] = ()
    delivery_quantity: int = 0
    fees: tuple[tuple[str, Decimal], ...] = ()
    due: Due | None = None
    citation: str = ""
    reason: str = ""
    factor: Fraction = Fraction(1)
    canceled_orders: tuple[str, ...] = ()
    reference_price: Decimal = ZERO
    cost_spec: CostSpec | None = None
    execution_spec: ExecutionSpec | None = None
    evidence: Evidence | None = None
    dividend_policy: ValuationPolicy | None = None
    payment_on: date | None = None
    payment_available_at: EventTime | None = None
    payment_model: str = ""
    entitlement_id: str = ""
    capacity_notional: Decimal = ZERO
    rank: int = 0
    requested_quantity: int = 0
    condition: OpeningCondition | None = None
    condition_met: bool | None = None
    sell_capacity_notional: Decimal | None = None
    flow_source_digest: str = ""
    flow_citation_digest: str = ""

    @property
    def opening_condition(self):
        """Read-only descriptive alias for the condition frozen on this event."""
        return self.condition


@dataclass(frozen=True)
class BookState:
    cash: Decimal
    orders: tuple[BuyOrder, ...] = ()
    claims: tuple[Claim, ...] = ()
    positions: tuple[Position, ...] = ()
    entitlements: tuple[Entitlement, ...] = ()
    as_of: EventTime | None = None

    @property
    def balances(self) -> Balances:
        def total(kinds):
            amounts = [claim.amount for claim in self.claims if claim.kind in kinds]
            return None if None in amounts else sum(amounts, ZERO)
        liabilities = total((ClaimKind.BUY_PAYABLE, ClaimKind.OTHER_PAYABLE))
        return Balances(self.cash, sum((o.reserved for o in self.orders), ZERO), liabilities,
                        total((ClaimKind.SALE,)), total((ClaimKind.DIVIDEND,)),
                        total((ClaimKind.OTHER,)), liabilities)


@dataclass(frozen=True)
class Entitlement:
    dividend_id: str
    position_id: str
    key: str
    shares: int
    per_share: Decimal
    ex_date: date
    due: Due | None
    basis: str
    citation: str
    accrued: bool = False
    unresolved_reason: str = ""
