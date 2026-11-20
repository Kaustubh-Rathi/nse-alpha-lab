"""Spec-driven research eligibility at a session from ledger evidence up to that session.

The default result is explicitly ``RESEARCH_ELIGIBILITY_ONLY``: it is a liquidity
and listing screen, never point-in-time index membership. Official membership
requires an explicit :class:`MembershipEvidence` port and is intersected with the
screen; without that port the request fails closed as
``unsupported_membership_kind``. No membership or security status is inferred from
an absent bar, and ``P0-UNIVERSE`` remains ``CONTRACT_BLOCKED`` while authoritative
dated membership evidence is unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from typing import Protocol, runtime_checkable

from nse_pipeline.specs import UniverseSpec

from .adv import exact, trailing_mean
from .evidence import ObservationLedger

RESEARCH_ELIGIBILITY_KIND = "research_eligibility"
RESEARCH_ELIGIBILITY_ONLY = "RESEARCH_ELIGIBILITY_ONLY"
OFFICIAL_MEMBERSHIP_KIND = "official_membership"
OFFICIAL_MEMBERSHIP_LABEL = "OFFICIAL_MEMBERSHIP_EVIDENCE"
UNSUPPORTED_MEMBERSHIP_KIND = "unsupported_membership_kind"

# Screen reasons that describe an absent bar without asserting a lifecycle state.
NO_BAR_REASON = "no_bar_on_session"
SESSION_GAP_REASON = "session_gap"
ABSENT_FOR_MULTIPLE_SESSIONS_REASON = "absent_for_multiple_sessions"
NOT_LISTED_AS_OF_REASON = "not_listed_as_of"
NOT_A_MEMBER_AS_OF_REASON = "not_a_member_as_of"


class UnsupportedMembershipKind(ValueError):
    """A membership kind was requested without the explicit evidence it requires."""


@runtime_checkable
class MembershipEvidence(Protocol):
    """Explicit, caller-supplied dated membership evidence.

    Implementations must answer causally: only evidence available at ``as_of`` may
    contribute. The port is the only admissible source of official membership;
    absence from its answer is never treated as membership. Batch 5 supplies the
    authoritative, digest-bound adapter.
    """

    def is_member(self, key: str, as_of: date) -> bool:
        ...


@dataclass(frozen=True)
class Membership:
    """An as-of screen result; the default kind is explicitly non-official research eligibility."""

    as_of: date
    label: str
    eligible: tuple[str, ...]
    exclusions: tuple[tuple[str, tuple[str, ...]], ...]
    censored_lower_bound: tuple[str, ...] = ()
    kind: str = RESEARCH_ELIGIBILITY_KIND
    official: bool = False
    spec_label: str = ""
    gapped: tuple[str, ...] = ()

    def reasons(self, key: str) -> tuple[str, ...]:
        return dict(self.exclusions).get(key, ())

    def has_gap(self, key: str) -> bool:
        return key in self.gapped


def _isin_ok(isin: str | None, policy: str) -> bool:
    return policy == "any" or (isin is None and policy == "ine_or_null") or (
        isin is not None and isin.startswith("INE"))


def _mean_reasons(ledger, key, as_of, spec, field, threshold, name) -> tuple[str, ...]:
    if threshold == 0:
        return ()
    mean = trailing_mean(ledger, key, as_of, spec.min_adv_window_sessions, field).exact
    if mean is None:
        return (f"{name}_unknown",)
    return () if mean >= threshold else (f"below_min_{name}",)


def _history_gap_count(history) -> int:
    """Unobserved sessions strictly between the first and last observed bars.

    Trailing absence after the last bar is not a gap; it is classified separately
    so a suspended/delisted/absent key is never reported as continuous membership.
    """
    if history.first_session is None or history.last_bar_session is None:
        return 0
    observed_span = history.sessions_since_first - (history.absence_sessions or 0)
    return max(0, observed_span - history.bar_observations)


def _absence_reason(history) -> str:
    if history.first_session is None:
        return NOT_LISTED_AS_OF_REASON
    if _history_gap_count(history):
        return SESSION_GAP_REASON
    if (history.absence_sessions or 0) > 1:
        return ABSENT_FOR_MULTIPLE_SESSIONS_REASON
    return NO_BAR_REASON


def _key_reasons(ledger: ObservationLedger, key: str, as_of: date,
                 spec: UniverseSpec) -> tuple[tuple[str, ...], bool, bool]:
    bar = ledger.bar(key, as_of)
    history = ledger.history(key, as_of)
    has_gap = _history_gap_count(history) > 0
    if bar is None:
        return (_absence_reason(history),), history.left_censored, has_gap
    if not bar.valid_ohlcv:
        return ("invalid_bar",), history.left_censored, has_gap
    reasons = []
    if bar.series in spec.exclude_series:
        reasons.append("series_excluded")
    elif bar.series not in spec.series:
        reasons.append("series_not_active")
    if not _isin_ok(bar.isin, spec.isin_policy):
        reasons.append("isin_policy")
    if bar.close is None:
        reasons.append("price_unknown")
    elif Fraction(bar.close) < exact(spec.min_price):
        reasons.append("below_min_price")
    if bar.volume < spec.min_volume:
        reasons.append("below_min_volume")
    reasons += _mean_reasons(ledger, key, as_of, spec, "traded_value",
                             exact(spec.min_adv_traded_value), "adv_traded_value")
    reasons += _mean_reasons(ledger, key, as_of, spec, "trades", exact(spec.min_avg_trades), "avg_trades")
    if history.sessions_since_first < spec.min_listed_sessions:
        reasons.append("listed_sessions_censored" if history.left_censored else "below_min_listed_sessions")
    return tuple(reasons), history.left_censored, has_gap


def _screen(ledger: ObservationLedger, as_of: date, spec: UniverseSpec):
    eligible, exclusions, lower, gapped = [], [], [], []
    for key in ledger.keys_as_of(as_of):
        reasons, censored, has_gap = _key_reasons(ledger, key, as_of, spec)
        if has_gap:
            gapped.append(key)
        if reasons:
            exclusions.append((key, reasons))
        else:
            eligible.append(key)
            if censored:
                lower.append(key)
    return tuple(eligible), tuple(exclusions), tuple(lower), tuple(gapped)


def build_universe(ledger: ObservationLedger, as_of: date, spec: UniverseSpec | None = None,
                   *, kind: str = RESEARCH_ELIGIBILITY_KIND,
                   membership_evidence: MembershipEvidence | None = None) -> Membership:
    """Eligible keys have a valid bar on as_of and pass every UniverseSpec filter.

    The default is a research eligibility screen labelled
    ``RESEARCH_ELIGIBILITY_ONLY`` and marked non-official. An official-membership
    request without an explicit ``MembershipEvidence`` port raises
    ``unsupported_membership_kind``; when supplied, the port's answer is
    intersected with the screen and absence from it is never inferred as
    membership. P0-UNIVERSE remains ``CONTRACT_BLOCKED`` without authoritative
    dated membership evidence.

    Listed age is observed exchange sessions since the key's first bar (any
    series, any validity). A left-censored key whose lower-bound age already
    meets min_listed_sessions is eligible; otherwise its age is unknown and it
    is excluded as listed_sessions_censored. Thresholds equal to 0 disable the
    ADV/average-trades filters, so unknown liquidity cannot exclude a key then.

    Absence is classified from observed evidence only and never asserted as a
    lifecycle state: a fresh absence after a contiguous history is
    ``no_bar_on_session`` (suspension/delisting unknown), a non-contiguous
    observed history is ``session_gap``, and a last bar more than one session
    back is ``absent_for_multiple_sessions``. Every non-contiguous key is also
    disclosed in ``Membership.gapped`` even when its as_of bar is eligible.
    """
    spec = UniverseSpec() if spec is None else spec
    if not isinstance(ledger, ObservationLedger) or not isinstance(spec, UniverseSpec):
        raise ValueError("Membership needs an ObservationLedger and a UniverseSpec")
    if kind not in (RESEARCH_ELIGIBILITY_KIND, OFFICIAL_MEMBERSHIP_KIND):
        raise UnsupportedMembershipKind(
            f"{UNSUPPORTED_MEMBERSHIP_KIND}: {kind!r} is not a supported membership kind")
    if kind == RESEARCH_ELIGIBILITY_KIND and membership_evidence is not None:
        raise ValueError("membership_evidence requires kind='official_membership'")

    eligible, exclusions, lower, gapped = _screen(ledger, as_of, spec)
    if kind == RESEARCH_ELIGIBILITY_KIND:
        return Membership(as_of, RESEARCH_ELIGIBILITY_ONLY, eligible, exclusions, lower,
                          kind=kind, official=False, spec_label=spec.label, gapped=gapped)

    if membership_evidence is None or not isinstance(membership_evidence, MembershipEvidence):
        raise UnsupportedMembershipKind(
            f"{UNSUPPORTED_MEMBERSHIP_KIND}: official membership requires a MembershipEvidence port")
    member_eligible = []
    official_exclusions = list(exclusions)
    for key in eligible:
        if membership_evidence.is_member(key, as_of):
            member_eligible.append(key)
        else:
            official_exclusions.append((key, (NOT_A_MEMBER_AS_OF_REASON,)))
    official_exclusions.sort(key=lambda row: row[0])
    members = set(member_eligible)
    return Membership(as_of, OFFICIAL_MEMBERSHIP_LABEL, tuple(member_eligible),
                      tuple(official_exclusions), tuple(key for key in lower if key in members),
                      kind=kind, official=True, spec_label=spec.label,
                      gapped=tuple(key for key in gapped if key in members))


def build_membership(ledger: ObservationLedger, as_of: date, spec: UniverseSpec | None = None,
                     *, membership_evidence: MembershipEvidence | None = None) -> Membership:
    """Official membership as the screen intersected with explicit dated evidence."""
    return build_universe(ledger, as_of, spec, kind=OFFICIAL_MEMBERSHIP_KIND,
                          membership_evidence=membership_evidence)
