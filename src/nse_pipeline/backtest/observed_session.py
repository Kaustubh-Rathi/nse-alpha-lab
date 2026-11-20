"""Production session slice over explicitly supplied observed inputs.

This is an accounting/execution composition, not a strategy or a historical-readiness
claim. Inputs are caller supplied and their provenance records are disclosures, not
source authentication.

Optional reviewed-evidence admission (R6): when a ``CalendarEvidencePort`` is supplied,
each stepped session date must be admitted as an exact-date exchange session before any
fill or decision mutation, and when a ``SnapshotMembershipEvidence`` port is supplied,
every buy/sell target security must be admitted for the session date. Unknown coverage
propagates ``CalendarCoverageUnknown``/``CoverageUnknown`` and an admitted-false answer
rejects the step. The ports bind reviewed inputs, not source authentication. Without a
supplied port the corresponding admission is not enforced for the run, which the runner
limitations disclose.

Unfilled-remainder expiry is orchestrated by this slice: ``ExecutionSpec`` admits only
``unfilled_remainder="expire"``, and a frozen buy or sell whose security has no supplied
opening print expires when — and only when — the session supplies explicit
``HaltEvidence`` naming that order id. Buy expiry releases the frozen reserve through
the kernel CANCEL verb at PRE_OPEN_ACTIONS because the kernel admits no OPEN_FILLS-phase
cancellation; sell expiry is a runner-level removal recorded at the end of OPEN_FILLS.
Either way the expiry appears as an explicit ``ExpiredOrder`` record on the
``SessionResult``. Without matching halt evidence the missing print stays an explicit
evidence halt that rejects the whole step; silent absence must never become a skip and
expiry must never be silent.

Phase-30 corporate actions and dividends are orchestrated only from caller-supplied
typed evidence, in a fixed order (share actions, entitlements, accruals, confirmations,
voids, corrections) before any opening fill. Every kernel call consumes an explicit
``InputProvenance`` that must already be available at ``PRE_OPEN_ACTIONS``. The kernel
still enforces its own applicability, verification and lifecycle rules; this slice only
sequences inputs and never infers, authenticates or repairs them.

Explicit external cash flows run at phase 10 before settlements or orders, on the
same copied book. A later input failure rolls them back with the entire session.
They are outside capital, never trading income, and do not authenticate bank
records; their economic rule is owner-approved under OD-COMP-1, while unknown
payables still leave available cash unknown for withdrawals.

At decision close, an unaffordable lower-ranked explicit buy target rejects the whole
step rather than silently re-ranking to the remaining targets.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING
from nse_pipeline.nifty.calendar_port import CalendarEvidencePort
from nse_pipeline.nifty.membership_port import SnapshotMembershipEvidence
from nse_pipeline.specs import CostSpec, ExecutionSpec, PortfolioSpec, ValuationPolicy
from .accounting_types import (BookState, BuyOrder, Evidence, EventTime, ExternalFlowDirection,
                               LedgerEvent, OpeningCondition, OpeningPrint, Phase, decimal,
                               identifier, integer, money, sha256_digest)
from .core import Portfolio
from .dividends import DividendTerms, VerifiedPayment
from .fees import affordable_buy
from .identity import (IdentityEdge, IdentityIssue, IdentityResolver, Migration, Observation,
                       Resolution, Retirement)
from .invariants import Reconciliation, reconcile
from .lifecycle import (LifecycleRecord, LifecycleStatus, blocking_reason, resolve_lifecycle)
from .observations import Bar
from .share_actions import ShareAction
from .valuation import Mark, Valuation, value

if TYPE_CHECKING:
    from .trims import TrimRequest


@dataclass(frozen=True)
class InputProvenance:
    citation: str
    digest: str
    available_at: EventTime
    assumption: str

    def __post_init__(self):
        if not isinstance(self.citation, str) or not self.citation.strip():
            raise ValueError("Input provenance requires a nonempty citation")
        sha256_digest(self.digest)
        if not isinstance(self.available_at, EventTime):
            raise ValueError("Input provenance requires an explicit availability time")
        if not isinstance(self.assumption, str) or not self.assumption.strip():
            raise ValueError("Input assumptions must be disclosed")


@dataclass(frozen=True)
class ExternalFlowInput:
    """Explicit outside capital, not income; provenance supplies its citation and source digest.

    The runner binds that digest into the ledger event so replay can detect
    citation/source substitution. The digest is not an owner-authenticated anchor.
    """
    flow_id: str
    amount: Decimal
    direction: ExternalFlowDirection
    provenance: InputProvenance

    def __post_init__(self):
        identifier(self.flow_id)
        object.__setattr__(self, "amount", money(self.amount, positive=True))
        if not isinstance(self.direction, ExternalFlowDirection):
            raise ValueError("External flows require a typed direction")
        if not isinstance(self.provenance, InputProvenance):
            raise ValueError("External flows require explicit provenance")


@dataclass(frozen=True)
class OpeningObservation:
    print: OpeningPrint
    provenance: InputProvenance


@dataclass(frozen=True)
class CloseBar:
    bar: Bar
    provenance: InputProvenance


@dataclass(frozen=True)
class CapacityInput:
    principal_notional: Decimal
    provenance: InputProvenance

    def __post_init__(self):
        object.__setattr__(self, "principal_notional", money(self.principal_notional, positive=True))


@dataclass(frozen=True)
class SellCapacityInput:
    """Explicit nonnegative sell/trim capacity with provenance.

    Zero is a *known* no-executable capacity, distinct from unknown/absent
    capacity, and is never conflated with ``None``/unbounded. The supplied rupee
    bound is floored to paise so a raw capacity can never be rounded upward.
    Buy capacity keeps its stricter positive-only ``CapacityInput`` contract.
    """
    principal_notional: Decimal
    provenance: InputProvenance

    def __post_init__(self):
        from decimal import ROUND_FLOOR
        object.__setattr__(self, "principal_notional",
                           decimal(self.principal_notional).quantize(
                               Decimal("0.01"), rounding=ROUND_FLOOR))
        if not isinstance(self.provenance, InputProvenance):
            raise ValueError("Sell capacity requires explicit provenance")


def frozen_capacity(capacity) -> Decimal:
    """The frozen rupee bound carried by either accepted capacity record."""
    if isinstance(capacity, (CapacityInput, SellCapacityInput)):
        return capacity.principal_notional
    raise ValueError("An explicit sourced capacity record is required")


@dataclass(frozen=True)
class BuyTarget:
    order_id: str
    key: str
    requested_quantity: int
    max_cash_budget: Decimal
    capacity: CapacityInput
    rank: int
    provenance: InputProvenance
    condition: OpeningCondition | None = None

    def __post_init__(self):
        if not self.order_id.strip() or not self.key.strip():
            raise ValueError("Buy targets require stable order and security identifiers")
        if self.condition is not None and not isinstance(self.condition, OpeningCondition):
            raise ValueError("Buy targets require an OpeningCondition or None")
        integer(self.requested_quantity, minimum=1)
        integer(self.rank)
        object.__setattr__(self, "max_cash_budget", money(self.max_cash_budget, positive=True))
        if not isinstance(self.capacity, CapacityInput):
            raise ValueError("Buy targets require explicit sourced principal capacity")


@dataclass(frozen=True)
class SellTarget:
    order_id: str
    position_id: str
    requested_quantity: int
    rank: int
    provenance: InputProvenance
    condition: OpeningCondition | None = None
    capacity: CapacityInput | SellCapacityInput | None = None

    def __post_init__(self):
        if not self.order_id.strip() or not self.position_id.strip():
            raise ValueError("Sell targets require stable order and position identifiers")
        if self.condition is not None and not isinstance(self.condition, OpeningCondition):
            raise ValueError("Sell targets require an OpeningCondition or None")
        if self.capacity is not None and not isinstance(self.capacity, (CapacityInput, SellCapacityInput)):
            raise ValueError("Sell targets require explicit sourced capacity or None")
        integer(self.requested_quantity, minimum=1)
        integer(self.rank)


@dataclass(frozen=True)
class TrimTarget:
    """A caller-supplied holding-value trim, planned by the pure trim module.

    The request, capacity, provenance and optional opening condition are frozen here
    before the modelled opening. A positive planned quantity becomes a frozen sell;
    a zero decision is an explicit, visible no-op, never a silent omission. A known
    zero capacity must use ``SellCapacityInput`` so it is never confused with unknown
    or unbounded capacity.
    """
    request: TrimRequest
    capacity: CapacityInput | SellCapacityInput
    provenance: InputProvenance
    condition: OpeningCondition | None = None

    def __post_init__(self):
        from .trims import TrimRequest
        if not isinstance(self.request, TrimRequest):
            raise ValueError("Trim targets require an explicit TrimRequest")
        if not isinstance(self.capacity, (CapacityInput, SellCapacityInput)):
            raise ValueError("Trim targets require explicit sourced capacity")
        if not isinstance(self.provenance, InputProvenance):
            raise ValueError("Trim targets require explicit provenance")
        if self.condition is not None and not isinstance(self.condition, OpeningCondition):
            raise ValueError("Trim targets require an OpeningCondition or None")


@dataclass(frozen=True)
class IdentityEvidence:
    """Optional complete causal identity evidence supplied to a runner.

    The runner constructs a streaming :class:`IdentityResolver` from exactly
    these edges, retirements, admissible series and ISIN-binding policy. The
    evidence must already carry its own availability times; nothing is inferred
    from symbols, ISINs, prices or missing observations.
    """
    edges: tuple[IdentityEdge, ...]
    retirements: tuple[Retirement, ...]
    series: tuple[str, ...]
    bind_first_isin: bool

    def __post_init__(self):
        object.__setattr__(self, "edges", tuple(self.edges))
        object.__setattr__(self, "retirements", tuple(self.retirements))
        object.__setattr__(self, "series", tuple(self.series))
        # The resolver owns the id/policy validation; constructing it here does
        # not admit or infer anything.
        IdentityResolver(self.edges, self.retirements, series=self.series,
                         bind_first_isin=self.bind_first_isin)


@dataclass(frozen=True)
class LifecycleEvidence:
    """Optional complete dated lifecycle record set supplied to a runner.

    An empty record set is deliberate: admission is still enforced and every
    security resolves to UNKNOWN, never to an inferred listed/suspended state.
    """
    records: tuple[LifecycleRecord, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "records", tuple(self.records))
        if not all(isinstance(record, LifecycleRecord) for record in self.records):
            raise ValueError("Lifecycle evidence must use typed dated records")


@dataclass(frozen=True)
class LifecycleAdmission:
    """One explicit lifecycle query made by the runner at a decision horizon."""
    security_key: str
    status: LifecycleStatus
    decision_at: EventTime

    def __post_init__(self):
        identifier(self.security_key)
        if not isinstance(self.status, LifecycleStatus):
            raise ValueError("Lifecycle admissions require a typed lifecycle status")
        if not isinstance(self.decision_at, EventTime):
            raise ValueError("Lifecycle admissions require an explicit decision time")


@dataclass(frozen=True)
class HaltEvidence:
    order_id: str
    provenance: InputProvenance

    def __post_init__(self):
        if not isinstance(self.order_id, str) or not self.order_id.strip():
            raise ValueError("Halt evidence requires a stable order identifier")
        if not isinstance(self.provenance, InputProvenance):
            raise ValueError("Halt evidence requires explicit provenance")


@dataclass(frozen=True)
class ShareActionInput:
    """A caller-supplied split/bonus action; ``position_id=None`` is order-only."""
    action: ShareAction
    position_id: str | None
    provenance: InputProvenance

    def __post_init__(self):
        if not isinstance(self.action, ShareAction):
            raise ValueError("Share actions must use typed split/bonus terms")
        if self.position_id is not None:
            identifier(self.position_id)
        if not isinstance(self.provenance, InputProvenance):
            raise ValueError("Share actions require explicit provenance")


@dataclass(frozen=True)
class DividendEntitlementInput:
    position_id: str
    terms: DividendTerms
    provenance: InputProvenance

    def __post_init__(self):
        identifier(self.position_id)
        if not isinstance(self.terms, DividendTerms):
            raise ValueError("Dividend entitlements require typed dividend terms")
        if not isinstance(self.provenance, InputProvenance):
            raise ValueError("Dividend entitlements require explicit provenance")


@dataclass(frozen=True)
class DividendAccrualInput:
    dividend_id: str
    provenance: InputProvenance

    def __post_init__(self):
        identifier(self.dividend_id)
        if not isinstance(self.provenance, InputProvenance):
            raise ValueError("Dividend accruals require explicit provenance")


@dataclass(frozen=True)
class DividendConfirmationInput:
    dividend_id: str
    payment: VerifiedPayment
    provenance: InputProvenance

    def __post_init__(self):
        identifier(self.dividend_id)
        if not isinstance(self.payment, VerifiedPayment):
            raise ValueError("Dividend confirmations require a typed verified payment")
        if not isinstance(self.provenance, InputProvenance):
            raise ValueError("Dividend confirmations require explicit provenance")


@dataclass(frozen=True)
class DividendVoidInput:
    dividend_id: str
    evidence: Evidence
    reason: str
    provenance: InputProvenance

    def __post_init__(self):
        identifier(self.dividend_id)
        if not isinstance(self.evidence, Evidence):
            raise ValueError("Dividend voids require typed documentary evidence")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("Dividend voids require a nonempty reason")
        if not isinstance(self.provenance, InputProvenance):
            raise ValueError("Dividend voids require explicit provenance")


@dataclass(frozen=True)
class DividendCorrectionInput:
    dividend_id: str
    corrected_amount: Decimal
    evidence: Evidence
    reason: str
    provenance: InputProvenance

    def __post_init__(self):
        identifier(self.dividend_id)
        object.__setattr__(self, "corrected_amount", money(self.corrected_amount, positive=True))
        if not isinstance(self.evidence, Evidence):
            raise ValueError("Dividend corrections require typed documentary evidence")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("Dividend corrections require a nonempty reason")
        if not isinstance(self.provenance, InputProvenance):
            raise ValueError("Dividend corrections require explicit provenance")


@dataclass(frozen=True)
class ExpiredOrder:
    order_id: str
    key: str
    at: EventTime
    halt_provenance_consumed: bool

    def __post_init__(self):
        if not isinstance(self.order_id, str) or not self.order_id.strip():
            raise ValueError("Expired order records require a stable order identifier")
        if not isinstance(self.key, str) or not self.key.strip():
            raise ValueError("Expired order records require the security key")
        if not isinstance(self.at, EventTime):
            raise ValueError("Expired order records require an explicit expiry event time")
        if self.halt_provenance_consumed is not True:
            raise ValueError("Expiry requires its halt provenance to be consumed")


@dataclass(frozen=True)
class SessionInput:
    on: date
    session_provenance: InputProvenance
    opening_prints: tuple[OpeningObservation, ...] = ()
    close_bars: tuple[CloseBar, ...] = ()
    buy_targets: tuple[BuyTarget, ...] = ()
    sell_targets: tuple[SellTarget, ...] = ()
    halt_evidence: tuple[HaltEvidence, ...] = ()
    share_actions: tuple[ShareActionInput, ...] = ()
    dividend_entitlements: tuple[DividendEntitlementInput, ...] = ()
    dividend_accruals: tuple[DividendAccrualInput, ...] = ()
    dividend_confirmations: tuple[DividendConfirmationInput, ...] = ()
    dividend_voids: tuple[DividendVoidInput, ...] = ()
    dividend_corrections: tuple[DividendCorrectionInput, ...] = ()
    external_flows: tuple[ExternalFlowInput, ...] = ()
    trim_targets: tuple[TrimTarget, ...] = ()
    identity_observations: tuple[Observation, ...] = ()
    decision_at: EventTime | None = None

    def __post_init__(self):
        if type(self.on) is not date:
            raise ValueError("An observed session date is required")
        if self.decision_at is not None and not isinstance(self.decision_at, EventTime):
            raise ValueError("A session decision time must be an explicit EventTime or None")
        for name in ("opening_prints", "close_bars", "buy_targets", "sell_targets", "halt_evidence",
                     "share_actions", "dividend_entitlements", "dividend_accruals",
                     "dividend_confirmations", "dividend_voids", "dividend_corrections",
                     "external_flows", "trim_targets", "identity_observations"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not all(isinstance(observation, Observation) for observation in self.identity_observations):
            raise ValueError("Session identity observations must use typed dated observations")
        if not all(isinstance(flow, ExternalFlowInput) for flow in self.external_flows):
            raise ValueError("Session external flows must use explicit typed inputs")
        if len({flow.flow_id for flow in self.external_flows}) != len(self.external_flows):
            raise ValueError("External flow identifiers must be unique in a session")
        if not all(isinstance(a, ShareActionInput) for a in self.share_actions):
            raise ValueError("Session share actions must use explicit typed action inputs")
        if not all(isinstance(e, DividendEntitlementInput) for e in self.dividend_entitlements):
            raise ValueError("Session entitlements must use explicit typed dividend inputs")
        if not all(isinstance(a, DividendAccrualInput) for a in self.dividend_accruals):
            raise ValueError("Session accruals must use explicit typed dividend inputs")
        if not all(isinstance(c, DividendConfirmationInput) for c in self.dividend_confirmations):
            raise ValueError("Session confirmations must use explicit typed dividend inputs")
        if not all(isinstance(v, DividendVoidInput) for v in self.dividend_voids):
            raise ValueError("Session voids must use explicit typed dividend inputs")
        if not all(isinstance(c, DividendCorrectionInput) for c in self.dividend_corrections):
            raise ValueError("Session corrections must use explicit typed dividend inputs")
        if len({a.action.action_id for a in self.share_actions}) != len(self.share_actions):
            raise ValueError("Share action identifiers must be unique in a session")
        if len({e.terms.dividend_id for e in self.dividend_entitlements}) != len(self.dividend_entitlements):
            raise ValueError("Dividend entitlement identifiers must be unique in a session")
        if len({a.dividend_id for a in self.dividend_accruals}) != len(self.dividend_accruals):
            raise ValueError("Dividend accrual identifiers must be unique in a session")
        if len({c.dividend_id for c in self.dividend_confirmations}) != len(self.dividend_confirmations):
            raise ValueError("Dividend confirmation identifiers must be unique in a session")
        if len({v.dividend_id for v in self.dividend_voids}) != len(self.dividend_voids):
            raise ValueError("Dividend void identifiers must be unique in a session")
        if len({c.dividend_id for c in self.dividend_corrections}) != len(self.dividend_corrections):
            raise ValueError("Dividend correction identifiers must be unique in a session")
        if not all(isinstance(t, TrimTarget) for t in self.trim_targets):
            raise ValueError("Session trim targets must use explicit typed trim records")


@dataclass(frozen=True)
class PhaseSnapshot:
    at: EventTime
    state: BookState


@dataclass(frozen=True)
class FrozenSell:
    target: SellTarget
    frozen_at: EventTime


@dataclass(frozen=True)
class SessionResult:
    on: date
    phase_states: tuple[PhaseSnapshot, ...]
    events: tuple[LedgerEvent, ...]
    valuation: Valuation
    next_orders: tuple[BuyOrder, ...]
    next_sells: tuple[FrozenSell, ...]
    consumed_provenance: tuple[InputProvenance, ...]
    reconciliation: Reconciliation
    limitations: tuple[str, ...]
    expired_orders: tuple[ExpiredOrder, ...] = ()
    trim_decisions: tuple = ()
    identity_migrations: tuple[Migration, ...] = ()
    identity_resolutions: tuple[Resolution, ...] = ()
    lifecycle_admissions: tuple[LifecycleAdmission, ...] = ()


class SessionRunner:
    """Atomic step boundary: SessionInput -> immutable ledger/valuation snapshot."""

    limitations = (
        "Caller supplied sessions are not asserted to be the authoritative exchange calendar.",
        "Strategy intent is explicit input; no automatic or inferred weights, trim targets, "
        "or signal generation exist.",
        "Phase-30 share actions and dividends are orchestrated only from caller-supplied evidence "
        "(not authenticated); identity migration and lifecycle admission run only from optional "
        "typed evidence supplied to this runner (also not authenticated) and are not enforced "
        "when those inputs are absent.",
        "Input validation does not authenticate sources or establish complete historical readiness.",
        "Execution uses modeled opening-price proxies and explicitly supplied cost/settlement assumptions.",
        "External cash flows are explicit phase-10 caller inputs, not income or "
        "authenticated bank evidence; their economic rule is owner-approved under OD-COMP-1. "
        "The kernel binds each flow citation to its supplied source digest and replay verifies "
        "that binding, but the digest is not authenticated against an owner-held source anchor. "
        "Unknown payables still make available cash unknown for withdrawals; a deposit remains "
        "admissible because it cannot consume protected cash.",
    )

    def __init__(self, initial_cash, *, on: date, initial_provenance: InputProvenance,
                 opening_claims=(), opening_positions=(), max_positions=20,
                 costs: CostSpec, execution: ExecutionSpec,
                 valuation_policy: ValuationPolicy | None = None,
                 calendar_evidence: CalendarEvidencePort | None = None,
                 membership_evidence: SnapshotMembershipEvidence | None = None,
                 portfolio_spec: PortfolioSpec | None = None,
                 identity_evidence: IdentityEvidence | None = None,
                 lifecycle_evidence: LifecycleEvidence | None = None):
        if not isinstance(initial_provenance, InputProvenance):
            raise ValueError("Initial balances/holdings require explicit provenance")
        if type(on) is not date or initial_provenance.available_at.session > on or (
                initial_provenance.available_at.session == on and
                initial_provenance.available_at.phase > Phase.PRE_OPEN_EVIDENCE):
            raise ValueError("Initial balance/position provenance must be available by the initial session")
        if not isinstance(costs, CostSpec) or not isinstance(execution, ExecutionSpec):
            raise ValueError("Explicit cost and execution specifications are required")
        if valuation_policy is not None and not isinstance(valuation_policy, ValuationPolicy):
            raise ValueError("valuation_policy must be an explicit ValuationPolicy or None")
        if calendar_evidence is not None and not isinstance(calendar_evidence, CalendarEvidencePort):
            raise ValueError("calendar_evidence must be a reviewed CalendarEvidencePort or None")
        if membership_evidence is not None and not isinstance(membership_evidence, SnapshotMembershipEvidence):
            raise ValueError("membership_evidence must be reviewed SnapshotMembershipEvidence or None")
        if portfolio_spec is not None and not isinstance(portfolio_spec, PortfolioSpec):
            raise ValueError("portfolio_spec must be an explicit PortfolioSpec or None")
        if identity_evidence is not None and not isinstance(identity_evidence, IdentityEvidence):
            raise ValueError("identity_evidence must be explicit IdentityEvidence or None")
        if lifecycle_evidence is not None and not isinstance(lifecycle_evidence, LifecycleEvidence):
            raise ValueError("lifecycle_evidence must be explicit LifecycleEvidence or None")
        if portfolio_spec is not None and portfolio_spec.max_positions != max_positions:
            raise ValueError(
                "PortfolioSpec.max_positions must agree with the runner max_positions; "
                "a trim configuration is never silently overridden")
        self._book = Portfolio(initial_cash, on=on, claims=opening_claims,
                               positions=opening_positions, max_positions=max_positions)
        self._on = on
        self._max_positions = max_positions
        self._costs = costs
        self._execution = execution
        self._valuation_policy = valuation_policy
        self._initial_provenance = initial_provenance
        self._calendar_evidence = calendar_evidence
        self._membership_evidence = membership_evidence
        self._portfolio_spec = portfolio_spec
        self._identity_evidence = identity_evidence
        self._lifecycle_evidence = lifecycle_evidence
        self._identity = (None if identity_evidence is None else
                          IdentityResolver(identity_evidence.edges, identity_evidence.retirements,
                                           series=identity_evidence.series,
                                           bind_first_isin=identity_evidence.bind_first_isin))
        self._identity_initialized = identity_evidence is None
        self._applied_migrations: tuple[str, ...] = ()
        self._pending_sells: tuple[FrozenSell, ...] = ()
        self._results: tuple[SessionResult, ...] = ()
        calendar_disclosure = (
            "Session dates are admitted only through the supplied reviewed calendar port; "
            "unknown dates raise CalendarCoverageUnknown and are never inferred."
            if calendar_evidence is not None else
            "No calendar evidence port is supplied; calendar admission is not enforced for this run.")
        membership_disclosure = (
            "Target securities are admitted only through the supplied reviewed membership port; "
            "unknown coverage raises CoverageUnknown and admitted-false targets reject the step."
            if membership_evidence is not None else
            "No membership evidence port is supplied; membership admission is not enforced for this run.")
        identity_disclosure = (
            "Causal identity observations and dated edges/retirements are supplied explicitly; "
            "migrations are applied only at PRE_OPEN_ACTIONS once their availability time and "
            "effective date are both reached. Held positions and frozen orders are re-keyed, while "
            "emitted historical events keep their original stable keys and no identity is inferred "
            "from a name, ISIN alone or price."
            if identity_evidence is not None else
            "No identity evidence is supplied; identity migration and retirement admission are "
            "inactive for this run.")
        lifecycle_disclosure = (
            "Lifecycle admission uses only supplied dated records visible at each decision phase; "
            "UNKNOWN is never treated as termination, and SUSPENDED/DELISTED/UNKNOWN block "
            "negotiation while every non-LISTED state blocks NAV. Terminal proceeds are never "
            "inferred from a last close."
            if lifecycle_evidence is not None else
            "No lifecycle evidence is supplied; lifecycle state admission and lifecycle NAV blocking "
            "are inactive for this run.")
        self.limitations = (
            *SessionRunner.limitations,
            calendar_disclosure,
            membership_disclosure,
            identity_disclosure,
            lifecycle_disclosure,
            "Missing-print expiry honours ExecutionSpec.unfilled_remainder "
            f"'{execution.unfilled_remainder}' (the only admitted policy) only through explicit "
            "per-order HaltEvidence: without a matching halt, a frozen order whose security has "
            "no supplied opening print is an evidence halt, never a silent skip. Buy expiry "
            "releases the reserve via the kernel CANCEL verb at PRE_OPEN_ACTIONS because the "
            "kernel admits no OPEN_FILLS-phase cancellation; sell expiry is a runner-level "
            "ExpiredOrder record at the end of OPEN_FILLS.",
            "Every cost/settlement assumption is the caller's explicit specification, not a "
            f"runner default: adverse slippage buy {execution.adverse_slippage_bps_buy} bps and "
            f"sell {execution.adverse_slippage_bps_sell} bps, partial_fill_policy "
            f"'{execution.partial_fill_policy}', unfilled_remainder "
            f"'{execution.unfilled_remainder}', min_order_notional "
            f"{execution.min_order_notional}, max_participation_rate "
            f"{execution.max_participation_rate}, cost schedule_basis '{costs.schedule_basis}'.",
            "Holding-value trims are planned only by the separately supplied pure trim module "
            "from explicit TrimTarget requests, frozen capacity/condition inputs and this "
            "session's valid raw bars. An explicit SellTarget without capacity remains a "
            "legacy, disclosed sell and is not certified against any market ADV.",
        )

    @property
    def result(self):
        return self._results

    @property
    def snapshot(self):
        return self._book.snapshot

    @property
    def provenance(self):
        """Construction metadata is separate from each result's consumed prefix."""
        return self._initial_provenance

    @property
    def identity_ledger(self):
        """Committed identity state, or None when no identity evidence was supplied."""
        return None if self._identity is None else self._identity.ledger

    @property
    def lifecycle_evidence(self):
        return self._lifecycle_evidence

    @property
    def events(self):
        return self._book.events

    def _validate_availability(self, provenance, on, phase):
        if not isinstance(provenance, InputProvenance):
            raise ValueError("Every external input requires provenance")
        if provenance.available_at.session > on or (provenance.available_at.session == on and
                                                     provenance.available_at.phase > phase):
            raise ValueError("Input is unavailable at its consumption phase")

    def _settle_phase(self, book, on, phase, sequence):
        # Portfolio owns the due-date semantics and performs the actual postings.
        due_claims = tuple(c for c in book.snapshot.claims if c.amount is not None and c.due is not None
                           and c.due.phase == phase
                           and (book.session_index >= c.due.session_index if c.due.session_index is not None
                                else on >= c.due.on))
        for claim in due_claims:
            book.settle(claim.claim_id, at=EventTime(on, phase, sequence[0]))
            sequence[0] += 1

    def _decision_horizon(self, session, phase):
        """The information horizon for a decision phase, capped by the caller's time."""
        at = EventTime(session.on, phase)
        if session.decision_at is None:
            return at
        if session.decision_at.session != session.on:
            raise ValueError("A session decision time must fall on the observed session date")
        return min(session.decision_at, at)

    @staticmethod
    def _reject_identity_issues(events):
        issues = tuple(event for event in events if isinstance(event, IdentityIssue))
        if issues:
            detail = ", ".join(f"{issue.evidence_id}:{issue.reason}" for issue in issues)
            raise ValueError("Identity evidence is rejected instead of guessed: " + detail)

    @staticmethod
    def _identity_key_state(resolver, key, *, live):
        match = next((item for item in resolver.ledger.keys if item.key == key), None)
        if match is None:
            raise ValueError(f"Identity key {key} has no causal observation evidence in this run")
        if live and match.retired_on is not None:
            raise ValueError(f"Identity key {key} is retired and cannot be newly opened")
        return match

    def _check_identity_coverage(self, resolver, book, *, new_open_keys=()):
        """Every live accounting key must be established by explicit observations."""
        if resolver is None:
            return
        for position in book.snapshot.positions:
            if position.shares > 0:
                self._identity_key_state(resolver, position.key, live=False)
        for order in book.snapshot.orders:
            self._identity_key_state(resolver, order.key, live=True)
        for key in new_open_keys:
            self._identity_key_state(resolver, key, live=True)

    def _apply_identity_pre_open(self, resolver, book, on):
        """Admit due migrations and re-key live positions/orders, never history."""
        at = EventTime(on, Phase.PRE_OPEN_ACTIONS, 0)
        self._reject_identity_issues(resolver.advance(at))
        due = tuple(migration for migration in resolver.ledger.migrations
                    if migration.migration_id not in self._applied_migrations
                    and migration.due(at))
        if not due:
            return ()
        for migration in due:
            positions = tuple(replace(position, key=migration.new_key)
                              if position.shares > 0 and position.key == migration.old_key
                              else position for position in book.snapshot.positions)
            orders = tuple(replace(order, key=migration.new_key)
                           if order.key == migration.old_key else order
                           for order in book.snapshot.orders)
            # Entitlements are live claims on the continuing security, so their
            # descriptive key follows the migration too. Historical events and the
            # initial snapshot keep their immutable stable keys. Claims carry no
            # security key: they resolve through their position_id at settlement.
            entitlements = tuple(replace(entitlement, key=migration.new_key)
                                 if entitlement.key == migration.old_key
                                 else entitlement
                                 for entitlement in book.snapshot.entitlements)
            # Live accounting state is re-keyed on the copied book. Emitted events
            # and the initial snapshot retain their historical stable keys.
            book._state = replace(book.snapshot, positions=positions, orders=orders,
                                  entitlements=entitlements)
        book._validate_funding(book.snapshot)
        return due

    def _feed_identity_observations(self, resolver, session):
        resolutions = []
        for observation in session.identity_observations:
            if observation.session != session.on:
                raise ValueError("Identity observations must be dated to the observed session")
            resolution = resolver.observe(observation)
            if resolution.key is None:
                raise ValueError(
                    f"Identity observation {observation.symbol} on {observation.session} is "
                    f"{resolution.status}: {resolution.reason}")
            resolutions.append(resolution)
        self._reject_identity_issues(resolver.advance(EventTime(session.on, Phase.CLOSE_AUDIT)))
        return tuple(resolutions)

    def _lifecycle_status(self, session, phase, key, admissions, seen):
        horizon = self._decision_horizon(session, phase)
        marker = (key, horizon)
        status = resolve_lifecycle(self._lifecycle_evidence.records, key, session.on,
                                   decision_at=horizon)
        if marker not in seen:
            seen.add(marker)
            admissions.append(LifecycleAdmission(key, status, horizon))
        return status

    def _admit_negotiation(self, session, phase, key, admissions, seen, context):
        if self._lifecycle_evidence is None:
            return None
        status = self._lifecycle_status(session, phase, key, admissions, seen)
        reason = blocking_reason(status)
        if reason:
            raise ValueError(
                f"{reason}: lifecycle admission blocks {context} for security {key}")
        return status

    def _canonical_key_map(self, resolver):
        alias = {}
        for migration in resolver.ledger.migrations:
            prior = alias.get(migration.old_key)
            if prior is not None and prior != migration.new_key:
                raise ValueError("Contradictory identity migrations")
            alias[migration.old_key] = migration.new_key
        return alias

    @staticmethod
    def _canonical_key(alias, key):
        seen = set()
        while key in alias:
            if key in seen:
                raise ValueError("Identity migration cycle")
            seen.add(key)
            key = alias[key]
        return key

    @classmethod
    def _canonical_state(cls, state, alias):
        canonical = lambda key: cls._canonical_key(alias, key)
        return replace(
            state,
            orders=tuple(replace(order, key=canonical(order.key)) for order in state.orders),
            positions=tuple(replace(position, key=canonical(position.key))
                            for position in state.positions),
            entitlements=tuple(replace(entitlement, key=canonical(entitlement.key))
                               for entitlement in state.entitlements))

    @classmethod
    def _canonical_event(cls, event, alias):
        evidence = event.evidence
        if evidence is not None:
            evidence = replace(evidence, key=cls._canonical_key(alias, evidence.key))
        return replace(event, key=cls._canonical_key(alias, event.key), evidence=evidence)

    def step(self, session: SessionInput) -> SessionResult:
        if not isinstance(session, SessionInput):
            raise TypeError("step accepts exactly one SessionInput")
        previous = self._results[-1].on if self._results else None
        if (previous is not None and session.on <= previous) or (previous is None and session.on != self._on):
            raise ValueError("The first date must match construction and observed sessions must strictly increase")
        consumed = [self._initial_provenance]
        self._validate_availability(session.session_provenance, session.on, Phase.PRE_OPEN_EVIDENCE)
        consumed.append(session.session_provenance)
        for flow in session.external_flows:
            self._validate_availability(flow.provenance, session.on, Phase.PRE_OPEN_EVIDENCE)
        for target in (*session.buy_targets, *session.sell_targets):
            if not isinstance(target, (BuyTarget, SellTarget)):
                raise ValueError("Session intents must use explicit typed target records")
            self._validate_availability(target.provenance, session.on, Phase.CLOSE_ORDERS)
        if len({t.order_id for t in session.buy_targets}) != len(session.buy_targets):
            raise ValueError("Buy order identifiers must be unique in a session")
        if len({t.order_id for t in session.sell_targets}) != len(session.sell_targets):
            raise ValueError("Sell order identifiers must be unique in a session")
        if session.trim_targets and self._portfolio_spec is None:
            raise ValueError("Trim targets require an explicit PortfolioSpec on the runner")
        if session.trim_targets and not self._portfolio_spec.trim_overweight_targets:
            raise ValueError("Trim targets require PortfolioSpec.trim_overweight_targets=True")
        trim_ids = [target.request.order_id for target in session.trim_targets]
        if len(set(trim_ids)) != len(trim_ids):
            raise ValueError("Trim order identifiers must be unique in a session")
        intent_ids = ({t.order_id for t in session.buy_targets}
                      | {t.order_id for t in session.sell_targets})
        if set(trim_ids) & intent_ids:
            raise ValueError("Trim order identifiers must not collide with explicit buy/sell intent")
        for target in session.trim_targets:
            if not isinstance(target, TrimTarget):
                raise ValueError("Session trim targets must use explicit typed trim records")
            self._validate_availability(target.provenance, session.on, Phase.CLOSE_ORDERS)
            self._validate_availability(target.capacity.provenance, session.on, Phase.CLOSE_ORDERS)
        if len({h.order_id for h in session.halt_evidence}) != len(session.halt_evidence):
            raise ValueError("Halt order identifiers must be unique in a session")
        for halt in session.halt_evidence:
            if not isinstance(halt, HaltEvidence):
                raise ValueError("Session halt inputs must use explicit typed halt records")
            self._validate_availability(halt.provenance, session.on, Phase.OPEN_FILLS)
        if session.dividend_entitlements and self._valuation_policy is None:
            raise ValueError("A dividend entitlement requires an explicit ValuationPolicy on the runner")
        for action in session.share_actions:
            if not isinstance(action, ShareActionInput):
                raise ValueError("Session share actions must use explicit typed action inputs")
            self._validate_availability(action.provenance, session.on, Phase.PRE_OPEN_ACTIONS)
        for entitlement in session.dividend_entitlements:
            if not isinstance(entitlement, DividendEntitlementInput):
                raise ValueError("Session entitlements must use explicit typed dividend inputs")
            self._validate_availability(entitlement.provenance, session.on, Phase.PRE_OPEN_ACTIONS)
        for accrual in session.dividend_accruals:
            if not isinstance(accrual, DividendAccrualInput):
                raise ValueError("Session accruals must use explicit typed dividend inputs")
            self._validate_availability(accrual.provenance, session.on, Phase.PRE_OPEN_ACTIONS)
        for confirmation in session.dividend_confirmations:
            if not isinstance(confirmation, DividendConfirmationInput):
                raise ValueError("Session confirmations must use explicit typed dividend inputs")
            self._validate_availability(confirmation.provenance, session.on, Phase.PRE_OPEN_ACTIONS)
        for void in session.dividend_voids:
            if not isinstance(void, DividendVoidInput):
                raise ValueError("Session voids must use explicit typed dividend inputs")
            self._validate_availability(void.provenance, session.on, Phase.PRE_OPEN_ACTIONS)
        for correction in session.dividend_corrections:
            if not isinstance(correction, DividendCorrectionInput):
                raise ValueError("Session corrections must use explicit typed dividend inputs")
            self._validate_availability(correction.provenance, session.on, Phase.PRE_OPEN_ACTIONS)
        if self._calendar_evidence is not None and not self._calendar_evidence.is_session(session.on):
            raise ValueError(f"Session date {session.on} is not an admitted exchange session")
        if self._membership_evidence is not None:
            for target in session.buy_targets:
                if not self._membership_evidence.is_member(target.key, session.on):
                    raise ValueError(
                        f"Buy target {target.order_id} security {target.key} is outside the "
                        f"admitted membership snapshot for {session.on}")
            for sell in session.sell_targets:
                position = self._book.positions.get(sell.position_id)
                if position is not None and not self._membership_evidence.is_member(position.key, session.on):
                    raise ValueError(
                        f"Sell target {sell.order_id} position security {position.key} is "
                        f"outside the admitted membership snapshot for {session.on}")
        if session.identity_observations and self._identity is None:
            raise ValueError("Identity observations require identity_evidence on the runner")
        if session.decision_at is not None and session.decision_at.session != session.on:
            raise ValueError("A session decision time must fall on the observed session date")
        book = deepcopy(self._book)
        pending_sells = self._pending_sells
        old_event_count = len(book.events)
        if previous is not None:
            book.observe_session(session.on)
        identity = None if self._identity is None else deepcopy(self._identity)
        applied_migrations = ()
        identity_resolutions = ()
        lifecycle_admissions = []
        lifecycle_seen = set()
        sequence = [0]
        phase_states = []

        def phase(phase_value):
            phase_states.append(PhaseSnapshot(EventTime(session.on, phase_value), book.snapshot))

        for flow in session.external_flows:
            at = EventTime(session.on, Phase.PRE_OPEN_EVIDENCE, sequence[0])
            if flow.provenance.available_at > at:
                raise ValueError("External flow provenance is unavailable at its consumption time")
            book.record_external_flow(flow.flow_id, flow.amount, flow.direction,
                                      flow.provenance.citation, at=at,
                                      source_digest=flow.provenance.digest)
            sequence[0] += 1
            consumed.append(flow.provenance)
        phase(Phase.PRE_OPEN_EVIDENCE)
        self._settle_phase(book, session.on, Phase.PRE_OPEN_SETTLEMENT, sequence)
        phase(Phase.PRE_OPEN_SETTLEMENT)

        # Identity migrations act only at PRE_OPEN_ACTIONS once both availability
        # and effectiveness are reached, before any same-session action/order decision.
        if identity is not None:
            applied_migrations = self._apply_identity_pre_open(identity, book, session.on)
            if self._identity_initialized:
                self._check_identity_coverage(identity, book)

        # Opening prints are isolated from completed bars. A missing print is an
        # error, never substituted with same-session close values, unless the session
        # supplies explicit halt evidence naming the frozen order that must expire.
        opens = {}
        for observed in session.opening_prints:
            if not isinstance(observed, OpeningObservation) or not isinstance(observed.print, OpeningPrint):
                raise ValueError("Opening inputs must use separately supplied typed opening prints")
            self._validate_availability(observed.provenance, session.on, Phase.OPEN_FILLS)
            if observed.print.session != session.on or observed.print.key in opens:
                raise ValueError("Opening prints must be unique and match the observed session")
            opens[observed.print.key] = observed.print
            consumed.append(observed.provenance)
        halts = {halt.order_id: halt for halt in session.halt_evidence}
        live_sells = tuple(sorted((s for s in pending_sells),
                                  key=lambda x: (x.target.rank, x.target.position_id, x.target.order_id)))
        pending_sells = ()
        live_buys = tuple(sorted((o for o in book.snapshot.orders
                                  if o.signal_session_index + 1 == book.session_index),
                                 key=lambda o: (o.rank, o.key, o.order_id)))
        sell_positions = {}
        missing_sells = []
        for frozen in live_sells:
            position = book.positions.get(frozen.target.position_id)
            if position is None:
                raise ValueError("Frozen sell target refers to an unknown position")
            sell_positions[frozen.target.order_id] = position
            if position.key not in opens:
                missing_sells.append((frozen.target.order_id, position.key))
        for order_id, key in missing_sells:
            if order_id not in halts:
                raise ValueError(
                    f"Missing opening print for security {key}: frozen sell order "
                    f"{order_id} has no supplied opening evidence for this session and "
                    f"the run cannot fabricate a fill for it")
        expired = []
        used_halts = set()
        # Expire explicitly halted missing-print buys before phase 30, as required.
        # A missing-print buy without a halt remains pending until after share actions:
        # only a successfully applied action cancellation can make it no longer live.
        for order in live_buys:
            if order.key in opens or order.order_id not in halts:
                continue
            at = EventTime(session.on, Phase.PRE_OPEN_ACTIONS, sequence[0])
            book.cancel(order.order_id, at=at)
            sequence[0] += 1
            consumed.append(halts[order.order_id].provenance)
            used_halts.add(order.order_id)
            expired.append(ExpiredOrder(order.order_id, order.key, at, True))

        # Phase 30 runs only from caller-supplied evidence, in one deterministic order.
        # The kernel owns every applicability/lifecycle rule; this slice only sequences
        # the calls, gives each a strictly increasing event time and discloses provenance.
        for action in session.share_actions:
            book.apply_share_action(action.position_id, action.action,
                                    at=EventTime(session.on, Phase.PRE_OPEN_ACTIONS, sequence[0]))
            sequence[0] += 1
            consumed.append(action.provenance)
        for entitlement in session.dividend_entitlements:
            book.entitle_dividend(entitlement.position_id, entitlement.terms,
                                  self._valuation_policy,
                                  at=EventTime(session.on, Phase.PRE_OPEN_ACTIONS, sequence[0]))
            sequence[0] += 1
            consumed.append(entitlement.provenance)
        for accrual in session.dividend_accruals:
            book.accrue_dividend(accrual.dividend_id,
                                 at=EventTime(session.on, Phase.PRE_OPEN_ACTIONS, sequence[0]))
            sequence[0] += 1
            consumed.append(accrual.provenance)
        for confirmation in session.dividend_confirmations:
            book.confirm_dividend_payment(confirmation.dividend_id, confirmation.payment,
                                          at=EventTime(session.on, Phase.PRE_OPEN_ACTIONS, sequence[0]))
            sequence[0] += 1
            consumed.append(confirmation.provenance)
        for void in session.dividend_voids:
            book.void_dividend(void.dividend_id, void.evidence, void.reason,
                               at=EventTime(session.on, Phase.PRE_OPEN_ACTIONS, sequence[0]))
            sequence[0] += 1
            consumed.append(void.provenance)
        for correction in session.dividend_corrections:
            book.correct_opening_dividend(correction.dividend_id, correction.corrected_amount,
                                          correction.evidence, correction.reason,
                                          at=EventTime(session.on, Phase.PRE_OPEN_ACTIONS, sequence[0]))
            sequence[0] += 1
            consumed.append(correction.provenance)
        # A share-unit action invalidates a frozen sell quantity. Reject the whole
        # copied step rather than ever applying a pre-action quantity to post-action units.
        changed_sell_positions = {action.position_id for action in session.share_actions
                                  if action.position_id is not None}
        stale_sells = sorted({frozen.target.order_id for frozen in live_sells
                              if frozen.target.position_id in changed_sell_positions})
        if stale_sells:
            raise ValueError("Phase-30 share actions invalidate frozen sell quantity for order(s): "
                             + ", ".join(stale_sells))
        # Reconsider missing-print buys only after real kernel action application: an
        # order canceled by phase 30 is no longer live and needs no opening print/halt.
        # A supplied halt for such an order is still invalid rather than silently ignored.
        live_buys = tuple(sorted((o for o in book.snapshot.orders
                                  if o.signal_session_index + 1 == book.session_index),
                                 key=lambda o: (o.rank, o.key, o.order_id)))
        missing_live_buys = {order.order_id for order in live_buys if order.key not in opens}
        for order in live_buys:
            if order.key not in opens and order.order_id not in halts:
                raise ValueError(
                    f"Missing opening print for security {order.key}: frozen buy order "
                    f"{order.order_id} has no supplied opening evidence for this session and "
                    f"the run cannot fabricate a fill for it")
        expected_halts = used_halts | {order_id for order_id, _ in missing_sells} | missing_live_buys
        unmatched = sorted(set(halts) - expected_halts)
        if unmatched:
            raise ValueError("Halt evidence must name a frozen order lacking its opening print: "
                             + ", ".join(unmatched))
        phase(Phase.PRE_OPEN_ACTIONS)
        for frozen in live_sells:
            target = frozen.target
            position = sell_positions[target.order_id]
            opening = opens.get(position.key)
            if opening is None:
                at = EventTime(session.on, Phase.OPEN_FILLS, sequence[0])
                sequence[0] += 1
                consumed.extend((target.provenance, halts[target.order_id].provenance))
                expired.append(ExpiredOrder(target.order_id, position.key, at, True))
                continue
            self._admit_negotiation(session, Phase.OPEN_FILLS, position.key,
                                    lifecycle_admissions, lifecycle_seen,
                                    f"frozen sell order {target.order_id}")
            book.sell(target.order_id, target.position_id, target.requested_quantity, opening,
                      costs=self._costs, execution=self._execution,
                      at=EventTime(session.on, Phase.OPEN_FILLS, sequence[0]),
                      condition=target.condition,
                      capacity_notional=(frozen_capacity(target.capacity)
                                         if target.capacity is not None else None))
            sequence[0] += 1
            consumed.append(target.provenance)
        for order in live_buys:
            if order.key not in opens:
                continue
            self._admit_negotiation(session, Phase.OPEN_FILLS, order.key,
                                    lifecycle_admissions, lifecycle_seen,
                                    f"frozen buy order {order.order_id}")
            book.buy(order.order_id, opens[order.key], costs=self._costs, execution=self._execution,
                     at=EventTime(session.on, Phase.OPEN_FILLS, sequence[0]))
            sequence[0] += 1
        phase(Phase.OPEN_FILLS)
        self._settle_phase(book, session.on, Phase.POST_OPEN_SETTLEMENT, sequence)
        phase(Phase.POST_OPEN_SETTLEMENT)

        # Completed bars become visible only here, after every opening fill.
        bars = {}
        for close in session.close_bars:
            if not isinstance(close, CloseBar) or not isinstance(close.bar, Bar):
                raise ValueError("Close-bar inputs must use typed Bar records")
            self._validate_availability(close.provenance, session.on, Phase.CLOSE_VALUATION)
            bar = close.bar
            if bar.session != session.on or bar.key in bars:
                raise ValueError("Completed close bars must be unique and match this session")
            bars[bar.key] = close
            consumed.append(close.provenance)
        if identity is not None:
            identity_resolutions = self._feed_identity_observations(identity, session)
            self._check_identity_coverage(
                identity, book, new_open_keys=tuple(target.key for target in session.buy_targets))
        marks = []
        for position in book.snapshot.positions:
            close = bars.get(position.key)
            if close is not None and close.bar.valid_ohlcv and close.bar.close is not None:
                marks.append(Mark(position.position_id, close.bar.close, session.on,
                                  EventTime(session.on, Phase.CLOSE_VALUATION), "close"))
        valuation_state = book.snapshot
        if self._lifecycle_evidence is not None:
            blocked_positions = []
            for position in book.snapshot.positions:
                if position.shares > 0:
                    status = self._lifecycle_status(
                        session, Phase.CLOSE_VALUATION, position.key,
                        lifecycle_admissions, lifecycle_seen)
                    reason = blocking_reason(status)
                    blocked_positions.append(replace(
                        position, blocked_reason=(position.blocked_reason or reason)))
                else:
                    blocked_positions.append(position)
            # Admission is a valuation view only: the position is retained with its
            # shares and no synthetic disposal, cash or terminal proceeds are created.
            valuation_state = replace(book.snapshot, positions=tuple(blocked_positions))
        valuation = value(valuation_state, EventTime(session.on, Phase.CLOSE_VALUATION), marks=marks)
        phase(Phase.CLOSE_VALUATION)

        # Sell intent is aggregate, not per order: siblings on one position are summed so a
        # split oversell cannot freeze and then poison the next session's committed sells.
        frozen_sells_by_position: dict[str, int] = {}
        for sell in session.sell_targets:
            if not isinstance(sell, SellTarget):
                raise ValueError("Sell intents must use explicit frozen target records")
            self._validate_availability(sell.provenance, session.on, Phase.CLOSE_ORDERS)
            position = book.positions.get(sell.position_id)
            if position is None:
                raise ValueError("Close sell target must name available delivered shares")
            self._admit_negotiation(session, Phase.CLOSE_ORDERS, position.key,
                                    lifecycle_admissions, lifecycle_seen,
                                    f"close sell target {sell.order_id}")
            requested = frozen_sells_by_position.get(sell.position_id, 0) + sell.requested_quantity
            if requested > position.delivered_shares:
                raise ValueError(
                    f"Aggregate close sell targets for position {sell.position_id} exceed "
                    f"{position.delivered_shares} delivered shares")
            frozen_sells_by_position[sell.position_id] = requested
            pending_sells += (FrozenSell(sell, EventTime(session.on, Phase.CLOSE_ORDERS)),)
            consumed.append(sell.provenance)

        # Explicit decisions are priced at this close. Budgets and capacity are
        # frozen now; the next opening may only reduce/skip the reserved quantity.
        close_by_key = {key: record for key, record in bars.items()
                        if record.bar.valid_ohlcv and record.bar.close is not None}
        # Phase-70 holding-value trims are planned on the copied book, after valuation
        # and before buy reservations, from valid raw same-session bars and the frozen
        # per-target capacity/condition. Every decision—including an intentional no-op—
        # is persisted; only a positive quantity becomes a frozen sell for the next open.
        trim_decisions = ()
        if session.trim_targets:
            from .trims import plan_trims
            target_by_order = {target.request.order_id: target for target in session.trim_targets}
            explicit_sell_positions = {sell.position_id for sell in session.sell_targets}
            explicit_buy_keys = {buy.key for buy in session.buy_targets}
            capacities = {}
            for target in session.trim_targets:
                position = book.positions.get(target.request.position_id)
                if position is None or position.shares == 0:
                    raise ValueError(
                        f"Trim target {target.request.order_id} names no open position")
                self._admit_negotiation(session, Phase.CLOSE_ORDERS, position.key,
                                        lifecycle_admissions, lifecycle_seen,
                                        f"trim target {target.request.order_id}")
                if target.request.position_id in explicit_sell_positions:
                    raise ValueError(
                        f"Trim target {target.request.order_id} conflicts with an explicit "
                        f"sell of the same position {target.request.position_id}")
                if position.key in explicit_buy_keys:
                    raise ValueError(
                        f"Trim target {target.request.order_id} conflicts with an explicit "
                        f"buy of the same security {position.key}")
                if position.key in capacities:
                    raise ValueError(
                        f"Multiple trim targets for security {position.key} are never netted")
                capacities[position.key] = frozen_capacity(target.capacity)
            trim_bars = tuple(record.bar for record in close_by_key.values())
            plan = plan_trims(book.snapshot, session.on,
                              tuple(target.request for target in session.trim_targets),
                              bars=trim_bars, capacities=capacities,
                              portfolio=self._portfolio_spec)
            trim_decisions = tuple(plan.decisions)
            for decision in trim_decisions:
                target = target_by_order.get(decision.request.order_id)
                if target is None:
                    raise ValueError("Trim plan returned a decision for an unknown request")
                if decision.requested_quantity > 0:
                    pending_sells += (FrozenSell(
                        SellTarget(decision.request.order_id, decision.request.position_id,
                                   decision.requested_quantity, decision.request.rank,
                                   target.provenance, condition=target.condition,
                                   capacity=target.capacity),
                        EventTime(session.on, Phase.CLOSE_ORDERS)),)
            for target in session.trim_targets:
                consumed.extend((target.provenance, target.capacity.provenance))
        for target in sorted(session.buy_targets, key=lambda t: (t.rank, t.key, t.order_id)):
            if not isinstance(target, BuyTarget):
                raise ValueError("Buy intents must use explicit frozen target records")
            self._validate_availability(target.capacity.provenance, session.on, Phase.CLOSE_ORDERS)
            self._admit_negotiation(session, Phase.CLOSE_ORDERS, target.key,
                                    lifecycle_admissions, lifecycle_seen,
                                    f"close buy target {target.order_id}")
            close = close_by_key.get(target.key)
            if close is None:
                raise ValueError(f"Missing valid same-session close for buy target {target.order_id}")
            available = book.balances.cash_available
            if available is None:
                raise ValueError("Unknown liabilities block close reservations")
            budget = min(target.max_cash_budget, available)
            quote = affordable_buy(target.requested_quantity, close.bar.close, budget,
                                  target.capacity.principal_notional, self._costs, self._execution)
            if quote.quantity == 0:
                # A zero reservation is not a target order and cannot be represented
                # by Portfolio.reserve; reject rather than invent a fallback.
                raise ValueError(f"Explicit buy target {target.order_id} is unaffordable at decision close")
            reserve = quote.notional + quote.fees.total
            book.reserve(target.order_id, target.key, quote.quantity, reserve,
                         target.capacity.principal_notional,
                         at=EventTime(session.on, Phase.CLOSE_ORDERS, sequence[0]), rank=target.rank,
                         condition=target.condition)
            sequence[0] += 1
            consumed.extend((target.provenance, target.capacity.provenance))
        phase(Phase.CLOSE_ORDERS)
        # Phase 80 is an observation wrapper only; no audit/accounting event is
        # fabricated and the enclosed BookState retains its actual as_of clock.
        phase(Phase.CLOSE_AUDIT)

        all_sessions = book.observed_sessions
        if identity is None or not identity.ledger.migrations:
            check = reconcile(book.initial, book.events, book.snapshot,
                              sessions=all_sessions, max_positions=self._max_positions)
        else:
            # Reconciliation is a pure view over canonical successor keys. Stored
            # initial records and emitted events keep their immutable historical keys.
            alias = self._canonical_key_map(identity)
            check = reconcile(
                self._canonical_state(book.initial, alias),
                tuple(self._canonical_event(event, alias) for event in book.events),
                self._canonical_state(book.snapshot, alias),
                sessions=all_sessions, max_positions=self._max_positions)
        if not check.passed:
            raise ValueError("Session reconciliation failed: " + repr(check.checks))
        result = SessionResult(session.on, tuple(phase_states), book.events[old_event_count:],
                               valuation, book.snapshot.orders, pending_sells,
                               tuple(consumed), check, self.limitations, tuple(expired),
                               trim_decisions,
                               identity_migrations=tuple(applied_migrations),
                               identity_resolutions=tuple(identity_resolutions),
                               lifecycle_admissions=tuple(lifecycle_admissions))
        # Commit only after every validation and independent reconciliation succeeds.
        self._book = book
        self._identity = identity if identity is not None else self._identity
        self._applied_migrations = self._applied_migrations + tuple(
            migration.migration_id for migration in applied_migrations)
        self._identity_initialized = self._identity is not None
        self._pending_sells = pending_sells
        self._results += (result,)
        return result
