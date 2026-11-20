"""P0-IDENTITY: causal security identity - keys fixed at first observation, dated evidence acting only once available."""
from dataclasses import FrozenInstanceError
from datetime import date, datetime

import pytest

from nse_pipeline.backtest.accounting_types import EventTime, Phase
from nse_pipeline.backtest.identity import (
    IdentityEdge, IdentityIssue, IdentityLedger, IdentityResolver, Migration, Observation,
    Resolution, Retirement, SecurityKey, resolve_history)

DAYS = (date(2024, 1, 5), date(2024, 1, 8), date(2024, 1, 10), date(2024, 1, 12))


def obs(symbol, session, isin=None, series="EQ"):
    return Observation(symbol, series, isin, session)


def edge(edge_id="E1", old="OLD", new="NEW", effective=None, available=None,
         citation="nse-corporate-actions.pdf", old_isin=None, new_isin=None):
    return IdentityEdge(edge_id, old, new,
                        effective if effective is not None else DAYS[2],
                        available if available is not None else EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS),
                        citation, old_isin, new_isin)


def retirement(retirement_id="R1", symbol="OLD", effective=None, available=None,
               citation="nse-corporate-actions.pdf", isin=None):
    return Retirement(retirement_id, symbol,
                      effective if effective is not None else DAYS[2],
                      available if available is not None else EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS),
                      citation, isin)


def resolver(edges=(), retirements=(), series=("EQ",), bind=False):
    return IdentityResolver(edges, retirements, series=series, bind_first_isin=bind)


def key_of(ledger, symbol):
    return next(k for k in ledger.keys if k.symbol == symbol)


def test_valid_records_carry_explicit_evidence():
    observation = obs("OLD", DAYS[0], "INE000000001")
    assert observation == Observation("OLD", "EQ", "INE000000001", DAYS[0])
    record = edge(old_isin="INE000000001", new_isin="INE000000002")
    assert (record.edge_id, record.old_symbol, record.new_symbol) == ("E1", "OLD", "NEW")
    assert record.effective_on == DAYS[2]
    assert record.available_at == EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS)
    assert record.citation == "nse-corporate-actions.pdf"
    retired = retirement()
    assert (retired.retirement_id, retired.symbol, retired.effective_on) == ("R1", "OLD", DAYS[2])
    assert retired.available_at == EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS)


@pytest.mark.parametrize("args", (
    ("", "EQ", None, DAYS[0]),
    (123, "EQ", None, DAYS[0]),
    ("AAA", "", None, DAYS[0]),
    ("AAA", "   ", None, DAYS[0]),
    ("AAA", "EQ", "", DAYS[0]),
    ("AAA", "EQ", "   ", DAYS[0]),
    ("AAA", "EQ", 42, DAYS[0]),
    ("AAA", "EQ", None, "2024-01-05"),
    ("AAA", "EQ", None, datetime(2024, 1, 5)),
))
def test_observation_rejects_empty_and_mistyped_fields(args):
    with pytest.raises(ValueError):
        Observation(*args)


@pytest.mark.parametrize("build", (
    lambda: edge(edge_id=""),
    lambda: edge(edge_id="   "),
    lambda: edge(citation=""),
    lambda: edge(citation=7),
    lambda: edge(effective="2024-01-10"),
    lambda: edge(effective=datetime(2024, 1, 10)),
    lambda: edge(available=DAYS[2]),
    lambda: edge(available="2024-01-10"),
    lambda: edge(old=""),
    lambda: edge(new="   "),
    lambda: edge(old_isin=""),
    lambda: edge(new_isin=42),
    lambda: retirement(retirement_id=""),
    lambda: retirement(symbol=""),
    lambda: retirement(citation=""),
    lambda: retirement(effective="2024-01-10"),
    lambda: retirement(available=DAYS[2]),
    lambda: retirement(isin=""),
))
def test_identity_evidence_needs_explicit_dates_and_availability(build):
    with pytest.raises(ValueError):
        build()


def test_same_symbol_edge_needs_two_different_isins():
    same = edge(old="AAA", new="AAA", old_isin="INE1", new_isin="INE2")
    assert (same.old_symbol, same.new_symbol) == ("AAA", "AAA")
    for old_isin, new_isin in (("INE1", "INE1"), (None, "INE2"), ("INE1", None), (None, None)):
        with pytest.raises(ValueError):
            edge(old="AAA", new="AAA", old_isin=old_isin, new_isin=new_isin)


def test_evidence_acts_only_after_its_availability():
    r = resolver([edge()])
    first = r.observe(obs("OLD", DAYS[0], "INE1"))
    assert first.status == "new" and first.key
    assert r.advance(EventTime(DAYS[1], Phase.PRE_OPEN_ACTIONS)) == ()
    assert r.ledger.migrations == () and r.ledger.issues == ()
    assert key_of(r.ledger, "OLD").retired_on is None
    second = r.observe(obs("OLD", DAYS[1], "INE1"))
    assert (second.status, second.key) == ("resolved", first.key)
    admitted = r.advance(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    assert len(admitted) == 1 and isinstance(admitted[0], Migration)
    migration = r.ledger.migrations[0]
    assert migration.migration_id == "E1"
    assert migration.old_key == first.key and migration.new_key != first.key
    assert migration.effective_on == DAYS[2]
    assert migration.available_at == EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS)
    assert migration.citation == "nse-corporate-actions.pdf"
    assert migration.admitted_at == EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS)
    old_key = key_of(r.ledger, "OLD")
    assert (old_key.key, old_key.retired_on, old_key.retired_by) == (first.key, DAYS[2], "E1")
    assert r.ledger.resolutions[0].key == first.key
    assert r.observe(obs("NEW", DAYS[2])).key == migration.new_key


def test_backdated_evidence_waits_for_its_availability():
    late = edge(effective=DAYS[1], available=EventTime(DAYS[2], Phase.PRE_OPEN_EVIDENCE))
    r = resolver([late])
    first = r.observe(obs("OLD", DAYS[0], "INE1"))
    assert r.advance(EventTime(DAYS[1], Phase.PRE_OPEN_ACTIONS)) == ()
    assert r.ledger.migrations == () and r.ledger.issues == ()
    assert key_of(r.ledger, "OLD").retired_on is None
    r.advance(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    migration = r.ledger.migrations[0]
    assert migration.old_key == first.key
    assert migration.effective_on == DAYS[1]
    assert migration.available_at == EventTime(DAYS[2], Phase.PRE_OPEN_EVIDENCE)
    assert migration.admitted_at == EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS)


def test_evidence_acts_only_from_its_effective_date():
    future = edge(effective=DAYS[3], available=EventTime(DAYS[0], Phase.PRE_OPEN_ACTIONS))
    r = resolver([future])
    first = r.observe(obs("OLD", DAYS[0], "INE1"))
    assert r.advance(EventTime(DAYS[1], Phase.PRE_OPEN_ACTIONS)) == ()
    assert r.advance(EventTime(DAYS[2], Phase.CLOSE_VALUATION)) == ()
    assert r.ledger.migrations == () and r.ledger.issues == ()
    assert key_of(r.ledger, "OLD").retired_on is None
    r.advance(EventTime(DAYS[3], Phase.PRE_OPEN_ACTIONS))
    migration = r.ledger.migrations[0]
    assert migration.old_key == first.key and migration.effective_on == DAYS[3]
    assert migration.admitted_at == EventTime(DAYS[3], Phase.PRE_OPEN_ACTIONS)


def test_retirement_acts_only_after_its_availability():
    r = resolver(retirements=[retirement()])
    first = r.observe(obs("OLD", DAYS[0], "INE1"))
    assert r.advance(EventTime(DAYS[1], Phase.PRE_OPEN_ACTIONS)) == ()
    assert key_of(r.ledger, "OLD").retired_on is None
    r.advance(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    retired = key_of(r.ledger, "OLD")
    assert (retired.key, retired.retired_on, retired.retired_by) == (first.key, DAYS[2], "R1")
    assert r.ledger.issues == ()


def test_retirement_named_by_isin_retires_only_that_key():
    r = resolver(retirements=[retirement(isin="INE1")])
    first = r.observe(obs("OLD", DAYS[0], "INE1"))
    r.advance(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    retired = key_of(r.ledger, "OLD")
    assert (retired.key, retired.retired_on, retired.retired_by) == (first.key, DAYS[2], "R1")
    assert r.ledger.issues == ()
    other = resolver(retirements=[retirement(isin="INE2")])
    other.observe(obs("OLD", DAYS[0], "INE1"))
    other.advance(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    assert [i.reason for i in other.ledger.issues] == ["no_live_key"]
    assert key_of(other.ledger, "OLD").retired_on is None


def test_migration_is_due_only_at_pre_open_actions_after_availability():
    r = resolver([edge()])
    r.observe(obs("OLD", DAYS[0], "INE1"))
    r.advance(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    migration = r.ledger.migrations[0]
    assert migration.due(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    assert not migration.due(EventTime(DAYS[2], Phase.PRE_OPEN_EVIDENCE))
    assert not migration.due(EventTime(DAYS[2], Phase.CLOSE_VALUATION))
    assert not migration.due(EventTime(DAYS[1], Phase.PRE_OPEN_ACTIONS))
    assert migration.due(EventTime(DAYS[3], Phase.PRE_OPEN_ACTIONS))
    late = resolver([edge(available=EventTime(DAYS[2], Phase.CLOSE_VALUATION))])
    late.observe(obs("OLD", DAYS[0], "INE1"))
    late.advance(EventTime(DAYS[2], Phase.CLOSE_VALUATION))
    late_migration = late.ledger.migrations[0]
    assert not late_migration.due(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    assert late_migration.due(EventTime(DAYS[3], Phase.PRE_OPEN_ACTIONS))


def test_identity_never_joins_on_name_isin_only_or_price():
    assert set(Observation.__dataclass_fields__) == {"symbol", "series", "isin", "session"}
    assert set(SecurityKey.__dataclass_fields__) == {"key", "symbol", "isin", "first_seen",
                                                     "last_seen", "retired_on", "retired_by"}
    r = resolver()
    first = r.observe(obs("AAA", DAYS[0], "INE1"))
    same_isin = r.observe(obs("BBB", DAYS[1], "INE1"))
    assert first.key != same_isin.key
    assert {k.symbol for k in r.ledger.keys} == {"AAA", "BBB"}
    changed = r.observe(obs("AAA", DAYS[2], "INE2"))
    assert changed.status == "new" and changed.key != first.key
    assert len(r.ledger.keys) == 3


def test_isin_binding_policy_is_explicit():
    bound = resolver(bind=True)
    first = bound.observe(obs("AAA", DAYS[0]))
    second = bound.observe(obs("AAA", DAYS[1], "INE9"))
    assert (second.status, second.key, second.reason) == ("resolved", first.key, "isin_bound")
    assert bound.ledger.keys[0].isin == "INE9"
    unbound = resolver(bind=False)
    first = unbound.observe(obs("AAA", DAYS[0]))
    second = unbound.observe(obs("AAA", DAYS[1], "INE9"))
    assert (second.status, second.reason) == ("new", "")
    assert second.key != first.key and len(unbound.ledger.keys) == 2


def test_a_migrated_key_is_never_joined_by_isin_alone():
    bound = resolver([edge()], bind=True)
    bound.observe(obs("OLD", DAYS[0], "INE1"))
    bound.advance(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    migration = bound.ledger.migrations[0]
    joined = bound.observe(obs("NEW", DAYS[2], "INE2"))
    assert (joined.status, joined.key, joined.reason) == ("resolved", migration.new_key, "isin_bound")
    unbound = resolver([edge()], bind=False)
    unbound.observe(obs("OLD", DAYS[0], "INE1"))
    unbound.advance(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    migration = unbound.ledger.migrations[0]
    separate = unbound.observe(obs("NEW", DAYS[2], "INE2"))
    assert (separate.status, separate.reason) == ("new", "")
    assert separate.key != migration.new_key


def test_unsupported_series_is_refused_without_a_key():
    r = resolver(series=("EQ",))
    result = r.observe(obs("AAA", DAYS[0], "INE1", series="MF"))
    assert (result.status, result.key) == ("unsupported_series", None)
    assert result.reason == "series MF is not admissible"
    assert r.ledger.keys == ()


def test_ambiguous_observations_are_never_guessed():
    r = resolver()
    first = r.observe(obs("AAA", DAYS[0], "INE1"))
    second = r.observe(obs("AAA", DAYS[1], "INE2"))
    assert first.key != second.key
    blind = r.observe(obs("AAA", DAYS[2]))
    assert (blind.status, blind.key) == ("ambiguous", None)
    assert blind.reason == "isin_missing_with_multiple_live_keys"
    exact = r.observe(obs("AAA", DAYS[3], "INE1"))
    assert (exact.status, exact.key) == ("resolved", first.key)


@pytest.mark.parametrize("build", (
    lambda: resolver(edges=("E1",)),
    lambda: resolver(edges=[edge()], retirements=[object()]),
    lambda: resolver(edges=[edge(edge_id="E1"), edge(edge_id="E1")]),
    lambda: resolver(edges=[edge(edge_id="E1")], retirements=[retirement(retirement_id="E1")]),
    lambda: resolver(series=()),
    lambda: resolver(series=("",)),
    lambda: resolver(series=("EQ", 1)),
    lambda: resolver(bind=1),
))
def test_resolver_rejects_untyped_or_ambiguous_evidence(build):
    with pytest.raises(ValueError):
        build()


def test_time_never_moves_backwards():
    r = resolver()
    r.observe(obs("AAA", DAYS[1], "INE1"))
    with pytest.raises(ValueError):
        r.observe(obs("AAA", DAYS[0], "INE1"))
    with pytest.raises(ValueError):
        r.advance(EventTime(DAYS[0], Phase.PRE_OPEN_ACTIONS))
    with pytest.raises(ValueError):
        r.advance(DAYS[1])
    with pytest.raises(ValueError):
        r.observe("AAA")


def test_ledger_reports_issues_instead_of_guessing():
    unobserved = resolver([edge(edge_id="E9", old="GHOST", new="NEW")])
    unobserved.observe(obs("AAA", DAYS[0], "INE1"))
    unobserved.advance(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    assert [i.reason for i in unobserved.ledger.issues] == ["old_symbol_unobserved"]
    assert unobserved.ledger.issues[0].admitted_at == EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS)
    assert unobserved.ledger.migrations == ()
    noretirement = resolver(retirements=[retirement(symbol="GHOST")])
    noretirement.observe(obs("AAA", DAYS[0], "INE1"))
    noretirement.advance(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    assert [i.reason for i in noretirement.ledger.issues] == ["no_live_key"]
    conflicting = resolver([edge(edge_id="E1", old="OLD1", new="NEW"),
                            edge(edge_id="E2", old="OLD2", new="NEW")])
    conflicting.observe(obs("OLD1", DAYS[0], "INE1"))
    conflicting.observe(obs("OLD2", DAYS[0], "INE2"))
    conflicting.advance(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    assert [i.reason for i in conflicting.ledger.issues] == ["conflicting_edges", "conflicting_edges"]
    assert conflicting.ledger.migrations == ()
    reused = resolver([edge(edge_id="E1", old="OLD", new="NEW", new_isin="INE2")])
    reused.observe(obs("OLD", DAYS[0], "INE1"))
    reused.observe(obs("NEW", DAYS[1], "INE2"))
    reused.advance(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    assert [i.reason for i in reused.ledger.issues] == ["new_symbol_in_use_before_effective"]
    ambiguous = resolver([edge(edge_id="E1", old="OLD", new="NEW")])
    ambiguous.observe(obs("OLD", DAYS[0], "INE1"))
    ambiguous.observe(obs("OLD", DAYS[1], "INE2"))
    ambiguous.advance(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    assert [i.reason for i in ambiguous.ledger.issues] == ["ambiguous_old_key"]
    assert len(ambiguous.ledger.issues[0].keys) == 2
    assert ambiguous.ledger.migrations == ()


def test_old_symbol_observed_before_the_effective_session_still_resolves_to_the_old_key():
    r = resolver([edge()])
    first = r.observe(obs("OLD", DAYS[0], "INE1"))
    before = r.observe(obs("OLD", DAYS[1], "INE1"))
    assert (before.status, before.key) == ("resolved", first.key)
    assert r.ledger.issues == ()
    r.advance(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    assert r.ledger.migrations[0].old_key == first.key
    reused = r.observe(obs("OLD", DAYS[3], "INE2"))
    assert reused.status == "new" and reused.key != first.key
    assert r.ledger.issues == ()


def test_symbol_observed_before_its_retirement_still_resolves_to_the_old_key():
    r = resolver(retirements=[retirement()])
    first = r.observe(obs("OLD", DAYS[0], "INE1"))
    before = r.observe(obs("OLD", DAYS[1], "INE1"))
    assert (before.status, before.key) == ("resolved", first.key)
    assert r.ledger.issues == ()
    r.advance(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    assert r.ledger.keys[0].retired_on == DAYS[2]
    reused = r.observe(obs("OLD", DAYS[3], "INE2"))
    assert reused.status == "new" and reused.key != first.key
    assert r.ledger.issues == ()


def test_admissible_series_names_are_still_accepted():
    r = resolver(series=("EQ", "MF"))
    assert r.observe(obs("AAA", DAYS[0], "INE1", series="EQ")).status == "new"
    assert r.observe(obs("BBB", DAYS[0], "INE2", series="MF")).status == "new"
    assert r.observe(obs("CCC", DAYS[0], "INE3", series="BE")).status == "unsupported_series"
    assert resolver(series={"EQ"}).observe(obs("AAA", DAYS[0], "INE1")).status == "new"


def test_identity_records_are_frozen():
    observation = obs("OLD", DAYS[0], "INE1")
    record = edge()
    retired = retirement()
    key = SecurityKey("K", "OLD", "INE1", DAYS[0])
    resolution = Resolution(observation, "new", "K", "")
    migration = Migration("E1", "K", "K2", DAYS[2], EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS),
                           "nse-corporate-actions.pdf", EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    issue = IdentityIssue("E1", "old_symbol_unobserved", (), EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    ledger = IdentityLedger((), (), (), ())
    for frozen, field, value in ((observation, "symbol", "NEW"),
                                 (record, "effective_on", DAYS[3]),
                                 (retired, "symbol", "NEW"),
                                 (key, "retired_on", DAYS[2]),
                                 (resolution, "status", "resolved"),
                                 (migration, "new_key", "K3"),
                                 (issue, "reason", "other"),
                                 (ledger, "keys", ())):
        with pytest.raises(FrozenInstanceError):
            setattr(frozen, field, value)


def test_resolve_history_orders_observations_and_applies_late_evidence():
    ledger = resolve_history([obs("OLD", DAYS[2], "INE1"), obs("OLD", DAYS[0], "INE1")], [edge()],
                             series=("EQ",), bind_first_isin=False)
    assert [r.observation.session for r in ledger.resolutions] == [DAYS[0], DAYS[2]]
    assert ledger.migrations[0].old_key == ledger.resolutions[0].key
    early = resolve_history([obs("OLD", DAYS[0], "INE1")], [edge()], series=("EQ",),
                            bind_first_isin=False, until=EventTime(DAYS[1], Phase.PRE_OPEN_ACTIONS))
    assert early.migrations == ()
    late = resolve_history([obs("OLD", DAYS[0], "INE1")], [edge()], series=("EQ",),
                           bind_first_isin=False, until=EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    assert late.migrations[0].admitted_at == EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS)


def test_old_symbol_observed_on_the_effective_session_is_reported_not_rekeyed():
    r = resolver([edge()])
    r.observe(obs("OLD", DAYS[0], "INE1"))
    r.observe(obs("OLD", DAYS[2], "INE1"))
    assert [i.reason for i in r.ledger.issues] == ["old_symbol_observed_on_or_after_effective"]
    assert len(r.ledger.keys) == 2


def test_retired_symbol_observed_after_its_retirement_is_reported_not_rekeyed():
    r = resolver(retirements=[retirement()])
    r.observe(obs("OLD", DAYS[0], "INE1"))
    r.advance(EventTime(DAYS[2], Phase.PRE_OPEN_ACTIONS))
    r.observe(obs("OLD", DAYS[2], "INE1"))
    assert [i.reason for i in r.ledger.issues] == ["observed_on_or_after_retirement"]
    assert len(r.ledger.keys) == 1


def test_admissible_series_must_be_declared_as_a_set_of_names():
    with pytest.raises(ValueError):
        resolver(series="EQ")
