"""Four-state field-disposition registry for the Stage 1 specification surface.

D-16 classifies every declared specification field as ``IMPLEMENTED``,
``RESERVED``, ``OBSOLETE`` or ``DISCONNECTED``. The registry is intentionally
self-contained: it imports no specification, sizing or validation module, so it
can be audited while those modules change. Reader and behaviour-test names are
metadata, not imports.

``BASELINE_DISPOSITIONS`` is the D-16 snapshot (10 implemented, 12 reserved,
0 obsolete, 5 disconnected). ``CURRENT_DISPOSITIONS`` records the Batch 3
wiring and the owner-approved explicit-target trim admission reader.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
import re

DISPOSITION_STATES = ("IMPLEMENTED", "RESERVED", "OBSOLETE", "DISCONNECTED")
IMPLEMENTED, RESERVED, OBSOLETE, DISCONNECTED = DISPOSITION_STATES

DISCONNECTED_ALLOWLIST_FIELDS = ("counterpart", "owner", "issue", "expiry")

_OWNER = "stage1-specs"
_DISCONNECTED_EXPIRY = "2026-12-31"
_RESERVED_EFFECT_TEST = (
    "tests/test_spec_consumption.py::test_reserved_field_has_no_runtime_effect"
)


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _optional_text(name: str, value: object) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{name} must be a nonempty string when present")


def is_production_reader(reader: object) -> bool:
    """Return whether a reader names a production path rather than test code."""
    if not isinstance(reader, str) or not reader.strip():
        return False
    path = reader.split("::", 1)[0]
    tokens = [token for token in re.split(r"[./\\:]", path) if token]
    return not any(
        token in {"test", "tests"} or token.startswith(("test_", "tests_"))
        for token in tokens
    )


@dataclass(frozen=True)
class FieldDisposition:
    """One declared field's disposition and the evidence that bounds it."""

    spec: str
    field: str
    disposition: str
    reader: str | None = None
    behaviour_test: str | None = None
    contract: str | None = None
    owner: str | None = None
    rationale: str | None = None
    legal_value: object | None = None
    issue: str | None = None
    expiry: str | None = None
    counterpart: str | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        _require_text("spec", self.spec)
        _require_text("field", self.field)
        if self.disposition not in DISPOSITION_STATES:
            raise ValueError(
                f"disposition must be one of {DISPOSITION_STATES}, not {self.disposition!r}"
            )
        for name in (
            "reader",
            "behaviour_test",
            "contract",
            "owner",
            "rationale",
            "issue",
            "expiry",
            "counterpart",
            "notes",
        ):
            _optional_text(name, getattr(self, name))

        if self.disposition == IMPLEMENTED:
            if not is_production_reader(self.reader):
                raise ValueError(
                    "IMPLEMENTED requires a non-test production reader"
                )
            _require_text("behaviour_test", self.behaviour_test)
        elif self.disposition == RESERVED:
            if self.reader is not None:
                raise ValueError("RESERVED fields must not declare a production reader")
            if self.legal_value is None:
                raise ValueError("RESERVED requires exactly one legal_value")
            if isinstance(self.legal_value, (list, tuple, set, frozenset, dict)):
                raise ValueError("RESERVED legal_value must be a single scalar value")
            if not isinstance(self.legal_value, (str, int, float, bool)):
                raise ValueError("RESERVED legal_value must be a scalar")
            for name in ("contract", "owner", "rationale", "behaviour_test"):
                _require_text(name, getattr(self, name))
        elif self.disposition == OBSOLETE:
            _require_text("notes", self.notes)
        else:  # DISCONNECTED
            for name in DISCONNECTED_ALLOWLIST_FIELDS:
                _require_text(name, getattr(self, name))

    @property
    def key(self) -> tuple[str, str]:
        return (self.spec, self.field)

    @property
    def id(self) -> str:
        return f"{self.spec}.{self.field}"


def _implemented(
    spec: str,
    field: str,
    *,
    reader: str,
    behaviour_test: str,
    contract: str,
    rationale: str,
    notes: str | None = None,
) -> FieldDisposition:
    return FieldDisposition(
        spec=spec,
        field=field,
        disposition=IMPLEMENTED,
        reader=reader,
        behaviour_test=behaviour_test,
        contract=contract,
        owner=_OWNER,
        rationale=rationale,
        notes=notes,
    )


def _reserved(
    spec: str,
    field: str,
    *,
    contract: str,
    legal_value: object,
    rationale: str,
    notes: str | None = None,
) -> FieldDisposition:
    return FieldDisposition(
        spec=spec,
        field=field,
        disposition=RESERVED,
        contract=contract,
        owner=_OWNER,
        rationale=rationale,
        legal_value=legal_value,
        behaviour_test=_RESERVED_EFFECT_TEST,
        notes=notes,
    )


def _disconnected(
    spec: str,
    field: str,
    *,
    contract: str,
    counterpart: str,
    issue: str,
    rationale: str,
    notes: str | None = None,
) -> FieldDisposition:
    return FieldDisposition(
        spec=spec,
        field=field,
        disposition=DISCONNECTED,
        contract=contract,
        owner=_OWNER,
        rationale=rationale,
        issue=issue,
        expiry=_DISCONNECTED_EXPIRY,
        counterpart=counterpart,
        notes=notes,
    )


BASELINE_DISPOSITIONS: tuple[FieldDisposition, ...] = (
    _implemented(
        "MomentumSpec",
        "anchor_rule",
        reader="nse_pipeline.backtest.signals.rank_momentum",
        behaviour_test=(
            "tests/test_accounting_signals.py::test_anchor_is_exact_and_schema_two_only"
        ),
        contract="MOM-ANCHOR",
        rationale=(
            "The sole legal value dispatches to the exact-session-offset anchor; no "
            "nearest-session substitution exists."
        ),
        notes="Baseline IMPLEMENTED (implicit); Batch 3 makes the dispatch explicit.",
    ),
    _implemented(
        "MomentumSpec",
        "observation_rule",
        reader="nse_pipeline.backtest.signals.rank_momentum",
        behaviour_test=(
            "tests/test_accounting_signals.py::test_min_observations_counts_window_not_history"
        ),
        contract="MOM-OBS",
        rationale=(
            "The sole legal value dispatches to the window-count observation rule; "
            "history counts are diagnostic only."
        ),
        notes="Baseline IMPLEMENTED (implicit); Batch 3 makes the dispatch explicit.",
    ),
    _implemented(
        "MomentumSpec",
        "tie_break",
        reader="nse_pipeline.backtest.signals.rank_momentum",
        behaviour_test=(
            "tests/test_accounting_signals.py::test_momentum_controls_are_implemented_single_value"
        ),
        contract="MOM-TIE",
        rationale=(
            "The comparator is key-ascending; rank order cannot depend on the order "
            "of the eligible iterable."
        ),
        notes="Baseline IMPLEMENTED (implicit); Batch 3 makes the dispatch explicit.",
    ),
    _reserved(
        "ExecutionSpec",
        "fill_timing",
        contract="EXEC-FILL",
        legal_value="next_session_open",
        rationale=(
            "Phase-40 next-open execution has no production runner; the field has no "
            "runtime effect until EXEC-FILL lands."
        ),
        notes="Single live value; schema rejects any other value.",
    ),
    _reserved(
        "ExecutionSpec",
        "max_participation_rate",
        contract="EXEC-PARTICIPATION",
        legal_value=0.05,
        rationale=(
            "Participation is a reporting threshold only until the runtime cap "
            "contract exists."
        ),
        notes=(
            "The schema retains a [0, 1] range for the future contract; the reserved "
            "live value is 0.05 and has no runtime effect."
        ),
    ),
    _implemented(
        "ExecutionSpec",
        "settlement_funding",
        reader="nse_pipeline.specs.validation.validate_run_specs",
        behaviour_test=(
            "tests/test_specs.py::test_validate_run_specs_asserts_pinned_single_value_fields"
        ),
        contract="EXEC-FUNDING",
        rationale=(
            "Settled cash only is asserted; expected sale proceeds cannot fund a buy."
        ),
        notes=(
            "Baseline IMPLEMENTED (implicit); validate_run_specs asserts the sole "
            "value before sizing."
        ),
    ),
    _reserved(
        "ExecutionSpec",
        "unfilled_remainder",
        contract="EXEC-LIFECYCLE",
        legal_value="expire",
        rationale="Remainder expiry and reissue lifecycle are not implemented.",
        notes="Single live value; schema rejects any other value.",
    ),
    _reserved(
        "ExecutionSpec",
        "circuit_limit_pct",
        contract="EXEC-CIRCUIT",
        legal_value=0.20,
        rationale="Circuit-band fill behaviour has no production consumer.",
        notes=(
            "The schema retains a [0, 1] range; the reserved live value is 0.20 and "
            "has no runtime effect."
        ),
    ),
    _reserved(
        "ExecutionSpec",
        "hold_suspended_positions",
        contract="LIFE-SUSPENSION",
        legal_value=True,
        rationale="Hold-but-do-not-buy suspension semantics are not wired.",
        notes=(
            "The schema accepts both booleans; the reserved live value is True and "
            "has no runtime effect."
        ),
    ),
    _implemented(
        "ValuationPolicy",
        "dividend_cash_phase",
        reader="nse_pipeline.specs.validation.validate_run_specs",
        behaviour_test=(
            "tests/test_specs.py::test_validate_run_specs_asserts_pinned_single_value_fields"
        ),
        contract="DIV-PHASE",
        rationale=(
            "Dividend cash becomes spendable only after opening fills in post-open "
            "settlement."
        ),
        notes=(
            "Baseline IMPLEMENTED (implicit); validate_run_specs asserts the sole "
            "value before valuation."
        ),
    ),
    _reserved(
        "ValuationPolicy",
        "series_precedence",
        contract="VAL-SERIES",
        legal_value="EQ,BE,BZ,SM,ST",
        rationale=(
            "Valuation/mark precedence across series is not consumed; the ordered "
            "tuple is fixed."
        ),
        notes="The legal value records the sole ordered series tuple.",
    ),
    _reserved(
        "ValuationPolicy",
        "carried_position_is",
        contract="LIFE-ABSENCE",
        legal_value="unresolved",
        rationale=(
            "Carried positions remain unresolved; they are not valued at last close."
        ),
        notes="Single live value; schema rejects any other value.",
    ),
    _reserved(
        "ValuationPolicy",
        "absence_review_sessions",
        contract="LIFE-ABSENCE",
        legal_value=20,
        rationale="The absence review window has no runtime consumer.",
        notes=(
            "The schema accepts any positive integer; the reserved live value is 20 "
            "and has no runtime effect."
        ),
    ),
    _disconnected(
        "ValuationPolicy",
        "sizing_uses_screen_value",
        contract="SIZING-P3",
        counterpart="resolve_sizing_policy",
        issue="F14/SIZING-P3",
        rationale=(
            "The implemented counterpart exists; P3 must be selected explicitly "
            "through resolve_sizing_policy rather than inferred."
        ),
    ),
    _implemented(
        "ValuationPolicy",
        "allow_scenario_statistics",
        reader="nse_pipeline.specs.ValuationPolicy.__post_init__",
        behaviour_test=(
            "tests/test_specs.py::test_policy_validation_does_not_override_scenario_restriction"
        ),
        contract="VAL-SCENARIO",
        rationale=(
            "The constructor rejects allow_scenario_statistics=True, so "
            "scenario-valued series cannot produce performance statistics."
        ),
        notes="Baseline IMPLEMENTED (guard).",
    ),
    _implemented(
        "CostSpec",
        "schedule_basis",
        reader="nse_pipeline.specs.validation.validate_run_specs",
        behaviour_test=(
            "tests/test_specs.py::test_validate_run_specs_asserts_pinned_single_value_fields"
        ),
        contract="COST-SCHEDULE",
        rationale=(
            "The flat research schedule is the only legal basis and is asserted "
            "before fees are quoted."
        ),
        notes=(
            "Baseline IMPLEMENTED (implicit); validate_run_specs asserts the sole "
            "value before fee provenance is emitted."
        ),
    ),
    _implemented(
        "CostSpec",
        "monetary_rounding",
        reader="nse_pipeline.specs.validation.validate_run_specs",
        behaviour_test=(
            "tests/test_specs.py::test_validate_run_specs_asserts_pinned_single_value_fields"
        ),
        contract="COST-ROUNDING",
        rationale=(
            "Paise half-up per component is asserted and never silently changed."
        ),
        notes=(
            "Baseline IMPLEMENTED (implicit); validate_run_specs asserts the sole "
            "value before rounding."
        ),
    ),
    _disconnected(
        "PortfolioSpec",
        "max_positions",
        contract="PORT-SLOTS",
        counterpart="size_portfolio_orders",
        issue="F14/PORT-SLOTS",
        rationale="max_positions must map to sizing slots inside size_portfolio_orders.",
    ),
    _reserved(
        "PortfolioSpec",
        "initial_cash",
        contract="PORT-INIT",
        legal_value=10_000_000.0,
        rationale="Portfolio initialization is not wired to a production runner.",
        notes=(
            "The schema accepts any positive amount; the reserved live value is "
            "10,000,000.00 and has no runtime effect."
        ),
    ),
    _reserved(
        "PortfolioSpec",
        "weighting",
        contract="PORT-WEIGHT",
        legal_value="equal_weight",
        rationale="Equal-weight target construction is not implemented.",
        notes="Single live value; schema rejects any other value.",
    ),
    _disconnected(
        "PortfolioSpec",
        "sizing_policy",
        contract="SIZING-POLICY",
        counterpart="resolve_sizing_policy",
        issue="F14/SIZING-POLICY",
        rationale=(
            "The long default must resolve through resolve_sizing_policy at the "
            "sizing boundary."
        ),
    ),
    _implemented(
        "PortfolioSpec",
        "allocation_order",
        reader="nse_pipeline.backtest.sizing.size_portfolio_orders",
        behaviour_test=(
            "tests/test_specs.py::test_validate_run_specs_asserts_pinned_single_value_fields"
        ),
        contract="PORT-ORDER",
        rationale=(
            "Rank-then-key order is the sole legal order and determines the fill "
            "sequence."
        ),
        notes=(
            "size_portfolio_orders is the entry point; validate_run_specs also "
            "asserts rank_then_key."
        ),
    ),
    _reserved(
        "PortfolioSpec",
        "disposal_policy",
        contract="LIFE-DISPOSAL",
        legal_value="hold_unresolved",
        rationale=(
            "Hold/retire semantics are not wired; retire_zero remains scenario-only."
        ),
        notes=(
            "retire_zero is a scenario-only schema override; the reserved base "
            "portfolio value is hold_unresolved."
        ),
    ),
    _implemented(
        "PortfolioSpec",
        "scenario_only",
        reader="nse_pipeline.specs.PortfolioSpec.__post_init__",
        behaviour_test=(
            "tests/test_specs.py::test_policy_validation_does_not_override_scenario_restriction"
        ),
        contract="PORT-SCENARIO",
        rationale=(
            "The constructor rejects retire_zero unless scenario_only is explicitly "
            "true."
        ),
        notes="Baseline IMPLEMENTED (guard).",
    ),
    _disconnected(
        "PortfolioSpec",
        "rebalance_frequency",
        contract="PORT-REBALANCE",
        counterpart="resolve_rebalance_schedule",
        issue="F14/PORT-REBALANCE",
        rationale="The spec-to-schedule adapter is the implemented counterpart.",
    ),
    _disconnected(
        "PortfolioSpec",
        "rebalance_anchor",
        contract="PORT-REBALANCE",
        counterpart="resolve_rebalance_schedule",
        issue="F14/PORT-REBALANCE",
        rationale=(
            "The adapter must select the known-calendar-else-observed-month anchor."
        ),
    ),
    _reserved(
        "PortfolioSpec",
        "trim_overweight_targets",
        contract="PORT-TRIM",
        legal_value=True,
        rationale="Trim-overweight target behaviour is not implemented.",
        notes=(
            "The schema accepts both booleans; the reserved live value is True and "
            "has no runtime effect."
        ),
    ),
)

_WIRED_READERS: dict[tuple[str, str], tuple[str, str]] = {
    ("ValuationPolicy", "sizing_uses_screen_value"): (
        "resolve_sizing_policy",
        "tests/test_specs.py::test_policy_validation_runs_before_sizing",
    ),
    ("PortfolioSpec", "sizing_policy"): (
        "resolve_sizing_policy",
        "tests/test_specs.py::test_portfolio_default_is_consumable_as_p2",
    ),
    ("PortfolioSpec", "max_positions"): (
        "nse_pipeline.backtest.sizing.size_portfolio_orders",
        "tests/test_specs.py::test_portfolio_default_is_consumable_as_p2",
    ),
    ("PortfolioSpec", "rebalance_frequency"): (
        "resolve_rebalance_schedule",
        "tests/test_specs.py::test_rebalance_schedule_descriptor_is_frozen_and_fails_closed",
    ),
    ("PortfolioSpec", "rebalance_anchor"): (
        "resolve_rebalance_schedule",
        "tests/test_specs.py::test_rebalance_schedule_descriptor_is_frozen_and_fails_closed",
    ),
    ("PortfolioSpec", "trim_overweight_targets"): (
        "nse_pipeline.backtest.trims.plan_trims",
        "tests/test_accounting_trims.py::test_disabled_trims_reject_explicit_requests",
    ),
}


def _wired(entry: FieldDisposition) -> FieldDisposition:
    wiring = _WIRED_READERS.get(entry.key)
    if wiring is None:
        return entry
    reader, behaviour_test = wiring
    return replace(
        entry,
        disposition=IMPLEMENTED,
        reader=reader,
        behaviour_test=behaviour_test,
        expiry=None,
        legal_value=None,
        notes=f"Wired through {reader}; baseline disposition is retained separately.",
    )


CURRENT_DISPOSITIONS: tuple[FieldDisposition, ...] = tuple(
    _wired(entry) for entry in BASELINE_DISPOSITIONS
)


def _entries(entries: Iterable[FieldDisposition]) -> tuple[FieldDisposition, ...]:
    values = tuple(entries.values()) if isinstance(entries, Mapping) else tuple(entries)
    for entry in values:
        if not isinstance(entry, FieldDisposition):
            raise TypeError("entries must contain FieldDisposition records")
    return values


def _index(entries: Iterable[FieldDisposition]) -> dict[tuple[str, str], FieldDisposition]:
    registry: dict[tuple[str, str], FieldDisposition] = {}
    for entry in _entries(entries):
        if entry.key in registry:
            raise ValueError(f"duplicate FieldDisposition for {entry.id}")
        registry[entry.key] = entry
    return registry


def counts(entries: Iterable[FieldDisposition]) -> dict[str, int]:
    """Return the four disposition counts for the supplied registry."""
    tally = {state.lower(): 0 for state in DISPOSITION_STATES}
    for entry in _index(entries).values():
        tally[entry.disposition.lower()] += 1
    return tally


def contributions(entries: Iterable[FieldDisposition]) -> dict[str, int]:
    """Alias for :func:`counts`; the four states contribute the tally."""
    return counts(entries)


def _declared(
    declared_fields: Iterable[tuple[str, str] | str],
) -> tuple[tuple[str, str], ...]:
    declared: list[tuple[str, str]] = []
    for item in declared_fields:
        if isinstance(item, str):
            spec, separator, field = item.partition(".")
            if not separator:
                raise ValueError(f"declared field {item!r} must name spec.field")
        else:
            try:
                spec, field = item
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"declared field {item!r} must be a (spec, field) pair"
                ) from error
        _require_text("declared spec", spec)
        _require_text("declared field", field)
        declared.append((spec, field))
    if len(set(declared)) != len(declared):
        raise ValueError("declared fields must be unique")
    return tuple(declared)


def survey(
    entries: Iterable[FieldDisposition],
    declared_fields: Iterable[tuple[str, str] | str],
) -> dict[str, object]:
    """Compare the registry against an explicit declared-field inventory.

    ``unregistered`` holds declared fields absent from the registry; ``missing``
    holds registered fields absent from the declaration. Neither side is an
    allowlist, so a new or synthetic field is always detected.
    """
    registered = set(_index(entries))
    declared = set(_declared(declared_fields))
    unregistered = sorted(f"{spec}.{field}" for spec, field in declared - registered)
    missing = sorted(f"{spec}.{field}" for spec, field in registered - declared)
    return {
        "declared": len(declared),
        "registered": len(registered),
        "unregistered": unregistered,
        "missing": missing,
        "complete": not unregistered and not missing,
    }


def disconnected_allowlist(
    entries: Iterable[FieldDisposition],
) -> dict[str, dict[str, str]]:
    """Return the bounded DISCONNECTED allowlist, raising on an unbounded entry."""
    allowlist: dict[str, dict[str, str]] = {}
    for entry in _index(entries).values():
        if entry.disposition != DISCONNECTED:
            continue
        absent = [
            name
            for name in DISCONNECTED_ALLOWLIST_FIELDS
            if not isinstance(getattr(entry, name), str) or not getattr(entry, name).strip()
        ]
        if absent:
            raise ValueError(
                f"DISCONNECTED field {entry.id} is unbounded; missing {absent}"
            )
        allowlist[entry.id] = {
            name: getattr(entry, name) for name in DISCONNECTED_ALLOWLIST_FIELDS
        }
    return dict(sorted(allowlist.items()))


def configuration_row(
    entries: Iterable[FieldDisposition],
    declared_fields: Iterable[tuple[str, str] | str] | None = None,
) -> dict[str, object]:
    """Build the local configuration gate row.

    The row may report ``status="PASS"`` only when no field is ``DISCONNECTED``
    and the survey found no unregistered declared field. It always remains
    ``contract_status="CONTRACT_PARTIAL"``: a component configuration gate never
    promotes a contract row.
    """
    members = _entries(entries)
    tallied = counts(members)
    disconnected = sorted(disconnected_allowlist(members))
    row: dict[str, object] = {
        "status": "PASS",
        "contract_status": "CONTRACT_PARTIAL",
        "reason": "",
        "counts": tallied,
        "disconnected_fields": len(disconnected),
        "disconnected": disconnected,
        "unregistered_fields": 0,
        "unregistered": [],
        "missing": [],
    }
    if declared_fields is not None:
        surveyed = survey(members, declared_fields)
        row["unregistered_fields"] = len(surveyed["unregistered"])
        row["unregistered"] = surveyed["unregistered"]
        row["missing"] = surveyed["missing"]

    if disconnected:
        row["status"] = "FAIL"
        row["reason"] = f"disconnected_fields={len(disconnected)}"
    elif row["unregistered"]:
        row["status"] = "FAIL"
        row["reason"] = f"unregistered_fields={len(row['unregistered'])}"
    return row


__all__ = [
    "BASELINE_DISPOSITIONS",
    "CURRENT_DISPOSITIONS",
    "DISCONNECTED",
    "DISCONNECTED_ALLOWLIST_FIELDS",
    "DISPOSITION_STATES",
    "FieldDisposition",
    "IMPLEMENTED",
    "OBSOLETE",
    "RESERVED",
    "configuration_row",
    "contributions",
    "counts",
    "disconnected_allowlist",
    "is_production_reader",
    "survey",
]
