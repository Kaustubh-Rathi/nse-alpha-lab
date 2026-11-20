"""Causal security identity: keys fixed at first observation; dated edges/retirements act only once available; no name, ISIN-only or price joins.

Status
------
**Active, but scoped to the Stage-1 accounting ledger -- not the research
backtest.** There are two distinct identity systems in ``backtest/`` and they
solve different problems at different layers:

=============================  ==============================================  ==============================
Module                         Role                                            Consumer
=============================  ==============================================  ==============================
``security_identity.py``       **CANONICAL for backtests.** Attaches a stable  ``research/run_sweep.py``,
                               ``ISIN:``/``SYMBOL:`` key to a bar DataFrame.    ``research/run_alpha_backtest.py``,
                                                                               ``backtest/momentum_grid.py``
``identity.py`` (this module)  Event-sourced ``IdentityResolver`` with an      ``backtest/observed_session.py``,
                               ``EventTime`` availability gate and a           ``tests/stage1_harness.py``,
                               ``Migration``/``IdentityIssue`` audit ledger.   the ``test_accounting_*`` suite
=============================  ==============================================  ==============================

Why this module is not deprecated
---------------------------------
The two are **not** interchangeable, so a shim re-export would be a behaviour
change, not a refactor. This module's objects are ``EventTime``-gated: an
identity change may only take effect once its evidence was *available*, which is
the invariant the Stage-1 accounting ledger depends on. ``security_identity``
resolves over already-materialised bars and has no availability gate. Neither
module imports the other and neither imports ``engine.py``.

Do not replace this module with ``security_identity``. See ``docs/IDENTITY.md``.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from .accounting_types import EventTime, Phase, identifier

NO_ISIN = "NOISIN"


def _isin(value):
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError("ISIN must be a nonempty string or None")
    return value


def _evidence_fields(evidence_id, effective_on, available_at, citation):
    identifier(evidence_id)
    identifier(citation)
    if type(effective_on) is not date or not isinstance(available_at, EventTime):
        raise ValueError("Identity evidence needs an effective date and an availability time")


@dataclass(frozen=True)
class Observation:
    symbol: str
    series: str
    isin: str | None
    session: date

    def __post_init__(self):
        identifier(self.symbol)
        identifier(self.series)
        _isin(self.isin)
        if type(self.session) is not date:
            raise ValueError("An observation needs its session date")


@dataclass(frozen=True)
class IdentityEdge:
    edge_id: str
    old_symbol: str
    new_symbol: str
    effective_on: date
    available_at: EventTime
    citation: str
    old_isin: str | None = None
    new_isin: str | None = None

    def __post_init__(self):
        _evidence_fields(self.edge_id, self.effective_on, self.available_at, self.citation)
        identifier(self.old_symbol)
        identifier(self.new_symbol)
        _isin(self.old_isin)
        _isin(self.new_isin)
        if self.old_symbol == self.new_symbol and (None in (self.old_isin, self.new_isin) or self.old_isin == self.new_isin):
            raise ValueError("A same-symbol edge must name two different ISINs")


@dataclass(frozen=True)
class Retirement:
    retirement_id: str
    symbol: str
    effective_on: date
    available_at: EventTime
    citation: str
    isin: str | None = None

    def __post_init__(self):
        _evidence_fields(self.retirement_id, self.effective_on, self.available_at, self.citation)
        identifier(self.symbol)
        _isin(self.isin)


@dataclass(frozen=True)
class SecurityKey:
    key: str
    symbol: str
    isin: str | None
    first_seen: date
    last_seen: date | None = None
    retired_on: date | None = None
    retired_by: str = ""


@dataclass(frozen=True)
class Resolution:
    observation: Observation
    status: str
    key: str | None
    reason: str = ""


@dataclass(frozen=True)
class Migration:
    migration_id: str
    old_key: str
    new_key: str
    effective_on: date
    available_at: EventTime
    citation: str
    admitted_at: EventTime

    def due(self, at: EventTime) -> bool:
        return at.phase == Phase.PRE_OPEN_ACTIONS and self.available_at <= at and self.effective_on <= at.session


@dataclass(frozen=True)
class IdentityIssue:
    evidence_id: str
    reason: str
    keys: tuple[str, ...]
    admitted_at: EventTime


@dataclass(frozen=True)
class IdentityLedger:
    resolutions: tuple[Resolution, ...]
    migrations: tuple[Migration, ...]
    issues: tuple[IdentityIssue, ...]
    keys: tuple[SecurityKey, ...]


def _fits(key: SecurityKey, isin):
    return isin is None or key.isin is None or key.isin == isin


class IdentityResolver:
    """Streaming resolver; time only moves forward and nothing already emitted is renamed."""

    def __init__(self, edges=(), retirements=(), *, series, bind_first_isin: bool):
        edges, retirements = tuple(edges), tuple(retirements)
        if not all(isinstance(e, IdentityEdge) for e in edges) or not all(isinstance(r, Retirement) for r in retirements):
            raise ValueError("Identity evidence must use typed edges and retirements")
        ids = [e.edge_id for e in edges] + [r.retirement_id for r in retirements]
        if len(set(ids)) != len(ids):
            raise ValueError("Identity evidence IDs must be unique")
        if isinstance(series, str):
            raise ValueError("Declare the admissible series set and the ISIN-binding policy explicitly")
        series = frozenset(series)
        if not series or not all(isinstance(s, str) and s for s in series) or type(bind_first_isin) is not bool:
            raise ValueError("Declare the admissible series set and the ISIN-binding policy explicitly")
        self._series, self._bind = series, bind_first_isin
        self._pending = sorted(edges + retirements, key=lambda e: (e.effective_on, getattr(e, "edge_id", "")
                               or e.retirement_id))
        self._keys: dict[str, SecurityKey] = {}
        self._retired_by: dict[str, IdentityEdge | Retirement] = {}
        self._now: EventTime | None = None
        self._resolutions: list[Resolution] = []
        self._migrations: list[Migration] = []
        self._issues: list[IdentityIssue] = []

    @property
    def ledger(self) -> IdentityLedger:
        return IdentityLedger(tuple(self._resolutions), tuple(self._migrations), tuple(self._issues),
                              tuple(sorted(self._keys.values(), key=lambda k: k.key)))

    def advance(self, at: EventTime) -> tuple[Migration | IdentityIssue, ...]:
        if not isinstance(at, EventTime) or (self._now is not None and at < self._now):
            raise ValueError("Identity time is an EventTime that never moves backwards")
        self._now = at
        ready = [e for e in self._pending if e.available_at <= at and e.effective_on <= at.session]
        if not ready:
            return ()
        self._pending = [e for e in self._pending if e not in ready]
        before = len(self._migrations), len(self._issues)
        for retirement in (e for e in ready if isinstance(e, Retirement)):
            self._retire(retirement, at)
        for on in sorted({e.effective_on for e in ready if isinstance(e, IdentityEdge)}):
            self._admit_edges([e for e in ready if isinstance(e, IdentityEdge) and e.effective_on == on], at)
        return tuple(self._migrations[before[0]:]) + tuple(self._issues[before[1]:])

    def observe(self, observation: Observation) -> Resolution:
        if not isinstance(observation, Observation):
            raise ValueError("Resolve typed observations only")
        self.advance(EventTime(observation.session, Phase.CLOSE_VALUATION))
        result = self._resolve(observation)
        if result.key is not None:
            key = self._keys[result.key]
            self._keys[result.key] = replace(key, last_seen=observation.session)
        self._resolutions.append(result)
        return result

    def _resolve(self, obs: Observation) -> Resolution:
        if obs.series not in self._series:
            return Resolution(obs, "unsupported_series", None, f"series {obs.series} is not admissible")
        conflict = self._conflict(obs)
        if conflict is not None:
            return conflict
        live = [k for k in self._keys.values() if k.symbol == obs.symbol and k.retired_on is None]
        if obs.isin is None:
            if len(live) > 1:
                return Resolution(obs, "ambiguous", None, "isin_missing_with_multiple_live_keys")
            return Resolution(obs, "resolved", live[0].key) if live else self._new(obs)
        exact = [k for k in live if k.isin == obs.isin]
        if exact:
            return Resolution(obs, "resolved", exact[0].key)
        if any(k.isin is None for k in live) and self._bind:
            if len(live) != 1:
                return Resolution(obs, "ambiguous", None, "unbound_key_among_multiple_live_keys")
            self._keys[live[0].key] = replace(live[0], isin=obs.isin)
            return Resolution(obs, "resolved", live[0].key, "isin_bound")
        return self._new(obs)

    def _new(self, obs: Observation) -> Resolution:
        key = self._create(obs.symbol, obs.isin, obs.session)
        return Resolution(obs, "new", key.key)

    def _conflict(self, obs: Observation) -> Resolution | None:
        retired = [k for k in self._keys.values() if k.symbol == obs.symbol and k.retired_on is not None
                   and k.retired_on <= obs.session and (obs.isin is None or k.isin == obs.isin)]
        if not retired:
            return None
        key = max(retired, key=lambda k: (k.retired_on, k.key))
        evidence = self._retired_by[key.key]
        if isinstance(evidence, IdentityEdge):
            reason = "old_symbol_observed_on_or_after_effective"
            evidence_id = evidence.edge_id
        else:
            reason = "observed_on_or_after_retirement"
            evidence_id = evidence.retirement_id
        self._issue(evidence_id, reason, [key.key], self._now)
        return Resolution(obs, "reported", None, reason)

    def _create(self, symbol, isin, on) -> SecurityKey:
        name, n = f"{symbol}|{isin or NO_ISIN}|{on.isoformat()}", 1
        while (f"{name}#{n}" if n > 1 else name) in self._keys:
            n += 1
        key = SecurityKey(f"{name}#{n}" if n > 1 else name, symbol, isin, on)
        self._keys[key.key] = key
        return key

    def _issue(self, evidence_id, reason, keys, at):
        self._issues.append(IdentityIssue(evidence_id, reason, tuple(sorted(keys)), at))

    def _retire(self, retirement: Retirement, at):
        found = [k for k in self._keys.values() if k.symbol == retirement.symbol and k.retired_on is None
                 and k.first_seen < retirement.effective_on and _fits(k, retirement.isin)]
        if len(found) != 1:
            return self._issue(retirement.retirement_id, "ambiguous_retirement" if found else "no_live_key",
                               [k.key for k in found], at)
        key = found[0]
        self._keys[key.key] = replace(key, retired_on=retirement.effective_on, retired_by=retirement.retirement_id)
        self._retired_by.setdefault(key.key, retirement)
        if key.last_seen is not None and key.last_seen >= retirement.effective_on:
            self._issue(retirement.retirement_id, "observed_on_or_after_retirement", [key.key], at)

    def _old_candidates(self, edge):
        return [k for k in self._keys.values() if k.symbol == edge.old_symbol and k.first_seen < edge.effective_on
                and (k.retired_on is None or k.retired_on >= edge.effective_on) and _fits(k, edge.old_isin)]

    def _admit_edges(self, edges, at):
        plans = {}
        for edge in sorted(edges, key=lambda e: e.edge_id):
            old = self._old_candidates(edge)
            if len(old) != 1:
                self._issue(edge.edge_id, "ambiguous_old_key" if old else "old_symbol_unobserved",
                            [k.key for k in old], at)
            elif old[0].last_seen is not None and old[0].last_seen >= edge.effective_on:
                self._issue(edge.edge_id, "old_symbol_observed_on_or_after_effective", [old[0].key], at)
            else:
                plans[edge.edge_id] = (edge, old[0])
        sources = [old.key for _, old in plans.values()] + [m.old_key for m in self._migrations]
        targets = [(e.new_symbol, e.new_isin) for e, _ in plans.values()]
        for edge_id, (edge, old) in sorted(plans.items()):
            if sources.count(old.key) > 1 or targets.count((edge.new_symbol, edge.new_isin)) > 1:
                self._issue(edge_id, "conflicting_edges", [old.key], at)
                continue
            self._migrate(edge, self._keys[old.key], at)

    def _migrate(self, edge, old, at):
        eff = edge.effective_on
        live = [k for k in self._keys.values() if k.symbol == edge.new_symbol and k.retired_on is None
                and k.key != old.key and _fits(k, edge.new_isin)]
        if any(k.first_seen < eff for k in live):
            return self._issue(edge.edge_id, "new_symbol_in_use_before_effective",
                               [k.key for k in live if k.first_seen < eff], at)
        if len(live) > 1:
            return self._issue(edge.edge_id, "ambiguous_new_key", [k.key for k in live], at)
        new = live[0] if live else self._create(edge.new_symbol, edge.new_isin, eff)
        if any(m.new_key == new.key for m in self._migrations):
            return self._issue(edge.edge_id, "conflicting_edges", [new.key], at)
        retired_on = eff if old.retired_on is None else min(old.retired_on, eff)
        self._keys[old.key] = replace(old, retired_on=retired_on, retired_by=edge.edge_id)
        self._retired_by.setdefault(old.key, edge)
        self._migrations.append(Migration(edge.edge_id, old.key, new.key, eff, edge.available_at, edge.citation, at))


def resolve_history(observations, edges=(), retirements=(), *, series, bind_first_isin: bool,
                    until: EventTime | None = None) -> IdentityLedger:
    resolver = IdentityResolver(edges, retirements, series=series, bind_first_isin=bind_first_isin)
    for observation in sorted(observations, key=lambda o: (o.session, o.symbol, o.isin or "", o.series)):
        resolver.observe(observation)
    if until is not None:
        resolver.advance(until)
    return resolver.ledger
