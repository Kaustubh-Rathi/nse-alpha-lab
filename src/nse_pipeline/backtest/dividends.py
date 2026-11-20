"""Normal cum/ex-date entitlement terms and explicit dividend settlement models.

No payment date is inferred from record-date holdings. Documentary verification
and exceptional due-bill rules are upstream responsibilities, not guessed here.
"""
from dataclasses import dataclass
from datetime import date, timedelta

from nse_pipeline.specs import ValuationPolicy
from .accounting_types import Due, Evidence, EventTime, Phase, decimal, identifier, money


CONFIRMATION_PHASES = (Phase.PRE_OPEN_EVIDENCE, Phase.PRE_OPEN_ACTIONS, Phase.CLOSE_VALUATION)
GROSS_DIVIDEND_NO_WITHHOLDING_MODEL = "gross_dividend_no_withholding_model"


@dataclass(frozen=True)
class VerifiedPayment:
    on: date
    available_at: EventTime
    citation: str
    amount: object = None
    settlement_model: str = ""

    def __post_init__(self):
        if type(self.on) is not date or not isinstance(self.available_at, EventTime):
            raise ValueError("A verified payment needs dated availability evidence")
        identifier(self.citation)
        if self.amount is not None:
            object.__setattr__(self, "amount", money(self.amount))
        if self.settlement_model != GROSS_DIVIDEND_NO_WITHHOLDING_MODEL:
            raise ValueError(
                "gross_dividend_no_withholding_model_required: a full gross dividend payment "
                f"must be explicitly labelled {GROSS_DIVIDEND_NO_WITHHOLDING_MODEL!r}; "
                "partial-payment and withholding models are unsupported")


@dataclass(frozen=True)
class DividendTerms:
    dividend_id: str
    per_share: object
    evidence: Evidence
    payment: VerifiedPayment | None = None
    entitlement_rule: str = "normal_ex_date"

    def __post_init__(self):
        identifier(self.dividend_id)
        object.__setattr__(self, "per_share", decimal(self.per_share, positive=True))
        if not isinstance(self.evidence, Evidence) or self.entitlement_rule != "normal_ex_date":
            raise ValueError("Only verified normal ex-date entitlement rules are supported")
        if self.payment is not None and not isinstance(self.payment, VerifiedPayment):
            raise ValueError("Payment-date evidence must be typed and explicit")


def payment_schedule(terms: DividendTerms, policy: ValuationPolicy, session_index: int, at: EventTime):
    citation = terms.evidence.citation
    if policy.dividend_settlement_mode == "hold_receivable":
        return None, "modeled", citation + " [explicit hold-receivable assumption]"
    payment = terms.payment
    if payment is not None and payment.available_at <= at:
        if payment.on < terms.evidence.effective_on:
            raise ValueError("Payment precedes the entitlement date: conflicting evidence")
        return Due(Phase.POST_OPEN_SETTLEMENT, on=payment.on), "verified", payment.citation
    # Future evidence is wholly invisible: do not even disclose its presence in
    # the causal ledger or change an earlier modeled cash schedule.
    note = f" [modeled ex-date lag={policy.fallback_lag} {policy.lag_unit}]"
    if policy.lag_unit == "calendar_days":
        return Due(Phase.POST_OPEN_SETTLEMENT, on=at.session + timedelta(days=policy.fallback_lag)), "modeled", citation + note
    return Due(Phase.POST_OPEN_SETTLEMENT, session_index=session_index + policy.fallback_lag), "modeled", citation + note


def confirmation_due(payment: VerifiedPayment, at: EventTime, session_index: int) -> Due:
    """A payment whose settlement phase already passed can only pay next session.

    A payment later than ``at.session`` or still ahead in the current session is
    scheduled on its actual date; anything whose settlement window has closed is
    carried to the next observed session. The historical date is provenance only.
    """
    if payment.on > at.session or (payment.on == at.session and at.phase < Phase.POST_OPEN_SETTLEMENT):
        return Due(Phase.POST_OPEN_SETTLEMENT, on=payment.on)
    return Due(Phase.POST_OPEN_SETTLEMENT, session_index=session_index + 1)


def validate_confirmation(claim, payment, ex_date: date, at: EventTime, session_index: int) -> Due:
    """Reject unavailable, conflicting, retroactive or wrong-phase confirmation.

    Only a full gross payment under the explicit
    ``gross_dividend_no_withholding_model`` is admissible. The accrued amount is
    never restated; a partial or net amount is rejected rather than being treated
    as full settlement.
    """
    if not isinstance(payment, VerifiedPayment):
        raise ValueError("verified_payment_required: a typed payment notice is required")
    if payment.settlement_model != GROSS_DIVIDEND_NO_WITHHOLDING_MODEL:
        raise ValueError(
            "gross_dividend_no_withholding_model_required: partial-payment and "
            "withholding models are unsupported")
    if not isinstance(at, EventTime):
        raise ValueError("confirmation_phase_not_allowed: an explicit event time is required")
    if at.phase not in CONFIRMATION_PHASES:
        raise ValueError("confirmation_phase_not_allowed: confirm before the settlement phases")
    if payment.available_at > at:
        raise ValueError("payment_evidence_not_yet_available: the notice postdates this event")
    if type(ex_date) is not date or payment.on < ex_date:
        raise ValueError("payment_precedes_entitlement_date: a payment cannot precede the ex-date")
    if payment.amount is not None and money(payment.amount) != claim.amount:
        raise ValueError(
            "conflicting_dividend_payment_amount: partial or net payment evidence "
            "is unsupported and must not be treated as full settlement")
    return confirmation_due(payment, at, session_index)
