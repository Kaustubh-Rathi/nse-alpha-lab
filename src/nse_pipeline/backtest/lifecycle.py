"""Dated security lifecycle states that never guess from missing market data.

A missing bar is an observation gap, not a suspension or delisting. A state
exists only when a dated authoritative record says so; absent evidence the state
is UNKNOWN. DELISTED terminal proceeds are null unless a verified amount is
explicitly documented. The last close is never a terminal-proceeds substitute.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum

from .accounting_types import EventTime, decimal, identifier


class LifecycleState(str, Enum):
    LISTED = "listed"
    SUSPENDED = "suspended"
    DELISTED = "delisted"
    TEMPORARY_NON_TRADING = "temporary_non_trading"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LifecycleRecord:
    security_key: str
    state: LifecycleState
    effective_from: date
    available_at: EventTime
    citation: str
    terminal_proceeds: Decimal | None = None
    effective_to: date | None = None
    proceeds_verified: bool = False

    def __post_init__(self):
        identifier(self.security_key)
        identifier(self.citation)
        if not isinstance(self.state, LifecycleState):
            raise ValueError("Lifecycle state must be one of the explicit typed states")
        if type(self.effective_from) is not date:
            raise ValueError("A lifecycle record needs an effective date")
        if not isinstance(self.available_at, EventTime):
            raise ValueError("A lifecycle record needs explicit evidence availability")
        if self.effective_to is not None:
            if type(self.effective_to) is not date or self.effective_to < self.effective_from:
                raise ValueError("effective_to must be a date on or after effective_from")
        if type(self.proceeds_verified) is not bool:
            raise ValueError("proceeds_verified must be an explicit boolean")
        if self.terminal_proceeds is not None:
            if self.state != LifecycleState.DELISTED:
                raise ValueError("Only a delisted security can carry terminal proceeds")
            if not self.proceeds_verified:
                raise ValueError("Terminal proceeds require verified terminal evidence")
            object.__setattr__(self, "terminal_proceeds", decimal(self.terminal_proceeds))
        elif self.proceeds_verified:
            raise ValueError("Verified terminal evidence needs an explicit proceeds amount")

    def covers(self, on: date) -> bool:
        return (self.effective_from <= on
                and (self.effective_to is None or on <= self.effective_to))


@dataclass(frozen=True)
class LifecycleStatus:
    security_key: str
    state: LifecycleState
    effective_from: date | None
    available_at: EventTime | None
    citation: str
    terminal_proceeds: Decimal | None
    reason: str

    @property
    def blocks_nav(self) -> bool:
        return self.state is not LifecycleState.LISTED

    @property
    def is_known(self) -> bool:
        return self.state is not LifecycleState.UNKNOWN


def resolve_lifecycle(records, security_key, on, *, decision_at: EventTime) -> LifecycleStatus:
    """Latest available record whose effective interval covers ``on``, else UNKNOWN."""
    identifier(security_key)
    if type(on) is not date:
        raise ValueError("A lifecycle query needs an explicit date")
    if not isinstance(decision_at, EventTime):
        raise ValueError("A lifecycle query needs an explicit decision event time")
    records = tuple(records)
    if not all(isinstance(record, LifecycleRecord) for record in records):
        raise ValueError("Lifecycle evidence must use typed records")

    candidates = tuple(record for record in records
                       if record.security_key == security_key
                       and record.available_at <= decision_at
                       and record.covers(on))
    if not candidates:
        return LifecycleStatus(security_key, LifecycleState.UNKNOWN, None, None, "",
                               None, "no_authoritative_evidence")

    latest = max(record.effective_from for record in candidates)
    winners = tuple(record for record in candidates if record.effective_from == latest)
    signatures = {(record.state, record.terminal_proceeds, record.proceeds_verified,
                   record.citation, record.available_at, record.effective_to)
                  for record in winners}
    if len(signatures) > 1:
        raise ValueError("conflicting_lifecycle_evidence")
    chosen = winners[0]

    if chosen.state is LifecycleState.LISTED:
        reason = "listed"
    elif chosen.state is LifecycleState.SUSPENDED:
        reason = "suspended"
    elif chosen.state is LifecycleState.TEMPORARY_NON_TRADING:
        reason = "temporary_non_trading"
    elif chosen.state is LifecycleState.DELISTED:
        reason = ("delisted_terminal_proceeds_unknown" if chosen.terminal_proceeds is None
                  else "delisted_verified_proceeds")
    else:
        reason = "unknown"
    return LifecycleStatus(chosen.security_key, chosen.state, chosen.effective_from,
                           chosen.available_at, chosen.citation, chosen.terminal_proceeds, reason)


def blocking_reason(status: LifecycleStatus) -> str:
    """The explicit position block implied by a lifecycle status, without disposal."""
    if not isinstance(status, LifecycleStatus):
        raise ValueError("A blocking decision requires a typed LifecycleStatus")
    return {
        LifecycleState.LISTED: "",
        LifecycleState.SUSPENDED: "lifecycle_suspended",
        LifecycleState.TEMPORARY_NON_TRADING: "lifecycle_temporary_non_trading",
        LifecycleState.DELISTED: ("lifecycle_delisted_terminal_proceeds_unknown"
                                  if status.terminal_proceeds is None
                                  else "lifecycle_delisted_terminal_proceeds_known"),
        LifecycleState.UNKNOWN: "lifecycle_unknown",
    }[status.state]


def require_negotiable(status: LifecycleStatus) -> None:
    if not isinstance(status, LifecycleStatus):
        raise ValueError("A trading decision requires a typed LifecycleStatus")
    reason = blocking_reason(status)
    if reason:
        raise ValueError(reason)


def require_terminal_proceeds(status: LifecycleStatus) -> Decimal:
    if (not isinstance(status, LifecycleStatus)
            or status.state is not LifecycleState.DELISTED
            or status.terminal_proceeds is None):
        raise ValueError("terminal_proceeds_unknown")
    return status.terminal_proceeds
