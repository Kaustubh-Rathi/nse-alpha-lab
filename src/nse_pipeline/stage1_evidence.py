"""Stage-1 evidence primitives shared by the inventory, report and checker.

This module is deliberately free of pytest, strategy and runner imports. It owns
three things that must never diverge between artifacts:

* the declared 29-row contract matrix (D-17);
* the eight-state inventory integrity machine (D-18);
* revision binding, skip classification and authorization reasons (D-19/D-20).
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Iterable

SCHEMA_VERSION = 2

CONTRACT_STATUSES = (
    "COMPONENT_IMPLEMENTED",
    "COMPONENT_VERIFIED",
    "CONTRACT_PARTIAL",
    "CONTRACT_PASS",
    "CONTRACT_NOT_IMPLEMENTED",
    "CONTRACT_FAIL",
    "CONTRACT_BLOCKED",
)
AUTHORIZING_CONTRACT_STATUS = "CONTRACT_PASS"

INVENTORY_STATES = (
    "CLEAN",
    "AUTHORIZED_EXCEPTION",
    "AUTHORIZED_EXCEPTION_ABSENT",
    "UNEXPECTED_CHANGE",
    "MISSING_FILE",
    "READ_ERROR",
    "EXCEPTION_LIST_CHANGED",
    "AUTHORIZED_EXCEPTION_MISMATCH",
)
INTEGRITY_OK_STATES = frozenset({
    "CLEAN",
    "AUTHORIZED_EXCEPTION",
    "AUTHORIZED_EXCEPTION_ABSENT",
})

GENERATED_MANIFEST_TRUST = "UNTRUSTED_NOT_AN_AUTHORITATIVE_INPUT"
SOURCE_ROOTS = ("src", "scripts", "tests")
SOURCE_FILES = ("pyproject.toml", "uv.lock")
EXCLUDED_PARTS = frozenset({"__pycache__"})
EXCLUDED_SUFFIXES = frozenset({".pyc"})
HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ContractSpec:
    contract_id: str
    title: str
    contract_status: str
    component_status: str | None
    component_modules: tuple[str, ...]
    blocking_reason: str
    required_proof: str


def _partial_proof(extra: str) -> str:
    return ("Independent clause-level evidence at the tested revision: " + extra)


# The order follows docs/STAGE1_P0_CONTRACT_MATRIX.md: the 28 report rows in
# report order, then the derived P0-E2E row. Do not reorder without reissuing
# the hand-authored golden.
CONTRACTS: tuple[ContractSpec, ...] = (
    ContractSpec(
        "P0-CASH", "Settled cash, reservations and prefunded liabilities",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_accounting_core", "test_accounting_valuation"), "",
        "Settled-cash, reservation and prefunded-liability clauses remain independently proven."),
    ContractSpec(
        "P0-FEES", "Fee- and slippage-inclusive integer buy/sell limits",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_accounting_fees", "test_accounting_core"),
        "",
        "All seven fee clauses have R5 clause evidence and are reverified with "
        "independent cap replay at the reissued revision."),
    ContractSpec(
        "P0-ACTIONS", "Verified-scope raw-share split/bonus and quarantine",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_accounting_actions",), "",
        "Verified-scope split/bonus and quarantine clauses remain independently proven."),
    ContractSpec(
        "P0-DIVIDEND", "Normal entitlement, accrual and explicit delayed settlement",
        "CONTRACT_PARTIAL", "COMPONENT_VERIFIED",
        ("test_accounting_dividends", "test_accounting_dividend_decisions",
         "test_accounting_replay"),
        "Gross-only payment evidence and opening-balance correction events pass; production "
        "withholding and partial-payment evidence is absent. The production document lane "
        "(verification/stage1/batch9/documents/) binds official dated dividend term filings: "
        "INFY (payment 2026-11-11) and TCS (payment 2026-10-30) are resolved from "
        "digest-bound filings, while HDFCBANK, ITC, LT, RELIANCE and TRENT payment dates "
        "remain unresolved and are refused honestly in the lane's refusals.json.",
        _partial_proof("production entitlement, withholding/partial-payment and settlement "
                       "documents plus clause proofs.")),
    ContractSpec(
        "P0-COVERAGE", "Operational and reference-notional diagnostics",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_accounting_valuation",),
        "",
        "All seven coverage clauses are proven by the R5 clause reconciliation, including the "
        "owner-approved OD-R5-1 positive-input adjudication of AT-COV-07."),
    ContractSpec(
        "P0-NAV-GAP", "Missing/stale/unknown assets block NAV",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_accounting_valuation", "test_accounting_dividends"), "",
        "NAV-gap clauses remain independently proven."),
    ContractSpec(
        "P0-CONSIDERATION", "Explicit verified proceeds, no immediate cash",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_accounting_core", "test_accounting_reconciliation"), "",
        "Verified-proceeds and deferred-cash clauses remain independently proven."),
    ContractSpec(
        "P0-CONSERVATION", "Independent typed-event replay and conservation",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_accounting_reconciliation", "test_accounting_replay",
         "test_accounting_external_flows", "test_accounting_observed_session_external_flows",
         "test_accounting_replay_independence"),
        "",
        "OD-COMP-1 approves the external-flow economic rule; AT-RECON-04's unknown-payable "
        "deposit false rejection is closed and replay detects citation/source-digest "
        "substitution. OD-B9-4 (2026-11-16) records the explicit owner scope decision that "
        "AT-RECON-04 requires replay-verifiable substitution detection, not authentication "
        "of the underlying bank or broker document; the coordinated-forger residual gap is "
        "accepted and the conservative check was not weakened. Revision-bound evidence was "
        "regenerated at c6044d5 (2026-11-17): the five conservation lane suites ran 114 "
        "passed, 0 failed, 0 skipped."),
    ContractSpec(
        "P0-SIZING", "Rank-sequential fee-inclusive supplied budgets",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_accounting_sizing", "test_accounting_signal_sizing_clause_contract"),
        "",
        "All six sizing clauses are proven, including the D-13 resolver and report-row "
        "consultation; F12/F20/F21 are stale at the reissued revision."),
    ContractSpec(
        "P0-CAPACITY", "Integer fills against supplied as-of capacity",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_accounting_fees", "test_accounting_core", "test_accounting_adv"),
        "",
        "All five capacity clauses are proven by the R5 clause reconciliation; F1 and F9 "
        "dependencies are stale at the reissued revision."),
    ContractSpec(
        "P0-SLOT-CAP", "Unresolved holdings retain slots, no forced disposal",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_accounting_core",), "",
        "Slot-retention clauses remain independently proven."),
    ContractSpec(
        "P0-LIQUIDATION", "Named zero-friction subtotals/scenarios",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_accounting_valuation",), "",
        "Zero-friction liquidation clauses remain independently proven."),
    ContractSpec(
        "P0-VERIFY", "Artifact guards, inventory checker and fail-closed report authorization",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_verification_isolation", "test_inventory_verification"),
        "",
        "Revision-bound evidence, inventory state machine, skip policy and HEAD-bound authorization are proven (F16/F17/F18/F22 resolved)."),
    ContractSpec(
        "P0-GATE", "Mandatory-report completeness blocks migration",
        "CONTRACT_PARTIAL", "COMPONENT_VERIFIED",
        ("test_accounting_report",),
        "The mandatory set and non-PASS contract rows keep the aggregate gate closed.",
        _partial_proof("29-row derivation, skip policy and HEAD-bound authorization.")),
    ContractSpec(
        "P0-CONTINUITY", "Documentary security boundaries and evidence chains",
        "CONTRACT_BLOCKED", "COMPONENT_VERIFIED",
        ("test_accounting_continuity", "test_accounting_continuity_evidence",
         "test_accounting_evidence_contract", "test_accounting_batch5_integration"),
        "The combined typed ledger binds 73 two-sided documentary boundaries: 59 NSE "
        "Indices Limited parent releases (30 mergers, 29 demergers) proving effective "
        "dates 2018-02-05..2026-09-07 and 14 company-filing history edges proving "
        "2018-03-31..2022-05-03. The merged ledger admits 63 of the 73 boundary objects "
        "and fails closed on 10 conflicting records (5 issues). The parent corpus stores "
        "1,113 of 1,119 in-interval releases (six publisher retrieval failures), so it "
        "is a screened subset, not a certified-complete archive; the early parent gap "
        "(2018-01-01..2018-02-04), the late parent gap (2026-09-08..2026-11-16), the "
        "history lane's own two screened gaps and the absent symbol/ISIN-change "
        "evidence remain. OD-B9-2 (2026-11-16) requires a complete publisher corpus or "
        "an owner-approved equivalent before this row can pass.",
        _partial_proof("a complete publisher corpus or owner-approved equivalent with "
                       "approved symbol/ISIN-change evidence (OD-B9-2).")),
    ContractSpec(
        "P0-IDENTITY", "Causal identity migration with stable IDs",
        "CONTRACT_PARTIAL", "COMPONENT_VERIFIED",
        ("test_accounting_identity", "test_accounting_batch5_integration"),
        "Causal identity component evidence is verified with synthetic typed observations, "
        "edges, retirements, entitlements and frozen orders. The optional production "
        "SessionRunner identity/lifecycle admission is exercised by dedicated lane tests "
        "(verification/stage1/batch9/identity/README.md, 14 nodes) and is not counted as "
        "this row's component evidence; contract-level mapping to authoritative production "
        "identity documents remains incomplete.",
        _partial_proof("contract-level identity fixtures spanning events, lifecycle and production evidence.")),
    ContractSpec(
        "P0-SCOPED-ADJ", "Horizon-admissible historical factors over proven scope",
        "CONTRACT_PARTIAL", "COMPONENT_VERIFIED",
        ("test_accounting_scoped_adjustment", "test_accounting_evidence_contract",
         "test_accounting_batch5_integration"),
        "Synthetic factors preserve raw prices and point-in-time scope. The production "
        "document lane (verification/stage1/batch9/documents/) binds authoritative dated "
        "action terms for MCX (face-value split 10->2, exact factor 1/5), TRENT (1:2 bonus, "
        "factor 2/3), VEDL (1:1 demerger basket, no scalar factor) and 5PAISA rights terms; "
        "the 5PAISA rights adjustment factor and the DRAGARWQ merger ratio remain "
        "unresolved and are refused honestly in the lane's refusals.json.",
        _partial_proof("dated action terms proving each factor, scope and version.")),
    ContractSpec(
        "P0-UNIVERSE", "Spec-driven membership boundaries",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_accounting_universe", "test_nifty_membership_intervals",
         "test_nifty_membership_evidence", "test_stage1_universe_evidence_lane"),
        "",
        "OD-B9-3 (2026-11-16) requires exact-date point evidence plus a post-2022 "
        "snapshot chain. Batch 8 binds 108 exact-date snapshots (2013-04-18..2022-03-31); "
        "the batch9/universe lane extends the chain to 65 digest-bound anchors "
        "(2022-03-31..2026-09-30) built walk-forward from official NSE Indices press "
        "releases only, including 16 corporate-action groups over 26 documents, 32,670 "
        "one-day intervals and 65 one-day windows. Dates between anchors and after "
        "2026-09-30 stay CoverageUnknown; three unresolved exclusion rows and the "
        "DUMMYHEG chain-end position are disclosed, not repaired; 688 refusals are "
        "recorded. No continuous or daily membership is claimed."),
    ContractSpec(
        "P0-SIGNALS", "Exact causal momentum ranks with spec-sized fixtures",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_accounting_signals", "test_accounting_signal_sizing_clause_contract",
         "test_accounting_strict_formulas", "test_accounting_strict_formula_integration",
         "test_accounting_strict_formula_integrity"),
        "",
        "Strict admitted formula ranking and action-adjusted evidence paths are implemented "
        "and tested. AT-SIG-05's known bound-method/partial/closure counterexample and the "
        "mutation-after-provenance gap are closed by an enforceable live-state fingerprint "
        "and a post-ranking recheck. OD-B9-5 (2026-11-16) accepts the enforceable rule with "
        "the four adversarial limits (state restored after mutation, external/clock reads, "
        "descriptor-backed state, bytecode replacement) as documented limits of the "
        "experimental legacy callable interface; production admission rejects caller "
        "callables and caller-authored price snapshots. Revision-bound evidence was "
        "regenerated at c6044d5 (2026-11-17): the five signals lane suites ran 85 passed, "
        "0 failed, 0 skipped."),
    ContractSpec(
        "P0-LIFECYCLE", "Causal absence, series and termination lifecycle",
        "CONTRACT_PARTIAL", "COMPONENT_VERIFIED",
        ("test_accounting_lifecycle", "test_accounting_evidence_contract",
         "test_accounting_batch5_integration"),
        "Dated synthetic lifecycle evidence passes. The production document lane "
        "(verification/stage1/batch9/documents/) binds the digest-bound DRAGARWQ suspension "
        "circular (CML77433, effective 2026-11-18) and the HAMPTON conditional-suspension "
        "circular (CML77365, outcome observable only after 2026-12-07); terminal proceeds "
        "(JINDALPHOT delisting exit price) remain unresolved and are refused honestly in "
        "the lane's refusals.json.",
        _partial_proof("dated exchange records for each state and verified terminal proceeds.")),
    ContractSpec(
        "P0-POLICIES", "Full P1/P2/P3 sizing and slot-policy comparisons",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_accounting_sizing", "test_specs",
         "test_accounting_signal_sizing_clause_contract",
         "test_accounting_trims", "test_accounting_observed_session_trims"),
        "",
        "All five AT-POL clauses are proven, including the direct slot-versus-trim "
        "comparison under the owner-approved explicit-target rule (OD-COMP-2/4/6); "
        "trim_overweight_targets gates explicit trim admission rather than inferring "
        "weights, and the baseline D-16 snapshot remains unchanged."),
    ContractSpec(
        "P0-ADV", "Trailing as-of ADV and observation validation",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_accounting_adv",),
        "",
        "All seven ADV clauses are proven by the R5 clause reconciliation under the "
        "owner-approved OD-R5-2 coverage-sufficiency wording; F9 is stale."),
    ContractSpec(
        "P0-EXECUTION", "Complete frozen-order perturbation contracts",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_execution_fixtures", "test_accounting_observed_session",
         "test_stage1_production_runner_contracts"), "",
        "Production runner ordering, next-open-only execution, irrelevant-order "
        "perturbations and reconciled economic/provenance prefixes are proven."),
    ContractSpec(
        "P0-BOUNDARY", "Causal month-end calendar and signal scheduling",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_accounting_boundary", "test_nifty_calendar_evidence"),
        "",
        "Owner-signed digest-bound calendar acceptance (OD-B9-1, 2026-11-16) admits the "
        "published exchange calendar through 2026-10-31: 106 month-end calendars are "
        "pinned against the real acceptance record, each admits only sessions published "
        "by its month end, and forged coverage/digest acceptance is refused."),
    ContractSpec(
        "P0-EVIDENCE", "Session-based counts, censoring and ticker-reuse isolation",
        "CONTRACT_PARTIAL", "COMPONENT_VERIFIED",
        ("test_accounting_evidence", "test_accounting_evidence_contract"),
        "Component and Batch 5 horizon evidence exist; production observation coverage proof "
        "is incomplete.",
        _partial_proof("session counts, censoring and ticker-reuse isolation proofs.")),
    ContractSpec(
        "P0-PREFIX", "Future-input appends and economic/provenance prefix",
        "CONTRACT_PASS", "COMPONENT_VERIFIED",
        ("test_accounting_observed_session", "test_stage1_production_runner_contracts"), "",
        "Production SessionRunner snapshots preserve economic and consumed-provenance "
        "prefixes under later appends; run metadata is retained separately."),
    ContractSpec(
        "P0-SCENARIO", "Separate scenario portfolios and statistics isolation",
        "CONTRACT_PARTIAL", "COMPONENT_VERIFIED",
        ("test_accounting_scenarios", "test_accounting_batch5_integration"),
        "A frozen production-data reference book and isolated scenario replay now pass with "
        "reference immutability and future-mark exclusion, but the book is an explicitly "
        "constructed non-authoritative selection and the sell-side schedules are modeled; "
        "no certified stress run exists.",
        _partial_proof("certified production reference book and isolated scenario replays "
                       "with authoritative provenance.")),
    ContractSpec(
        "P0-FULL-LIQUIDATION", "Friction-aware per-order liquidation",
        "CONTRACT_PARTIAL", "COMPONENT_VERIFIED",
        ("test_accounting_liquidation", "test_accounting_batch5_integration"),
        "A dated liquidation run over a frozen production-data book now passes with "
        "per-order fees, adverse slippage, capacity limits and fail-closed unknowns, but "
        "the dated cost tariff and 5%-of-traded-value capacity are explicit models, not "
        "broker-authoritative costs or verified executable liquidity.",
        _partial_proof("broker-authoritative dated sell-side costs, verified executable "
                       "capacity and a revision-bound liquidation run.")),
    ContractSpec(
        "P0-E2E", "End-to-end causal integration",
        "CONTRACT_PARTIAL", "COMPONENT_IMPLEMENTED",
        ("test_stage1_production_runner_contracts", "test_stage1_production_e2e",
         "test_accounting_observed_session_phase30", "test_accounting_target_composition"),
        "Production phase 10-80 accounting orchestration, explicit-target trims, opening "
        "conditions and sell/trim capacity are integrated and tested. The optional causal "
        "identity migration/lifecycle admission is exercised by dedicated lane tests "
        "(verification/stage1/batch9/identity/README.md, 14 nodes) and is not counted in "
        "this row's component modules; those inputs remain synthetic or caller-supplied, "
        "not authenticated production evidence, and dependent contract "
        "proofs remain incomplete.",
        _partial_proof("identity/lifecycle integration and all dependent contract proofs "
                       "with authoritative dated evidence.")),
)

MANDATORY_CONTRACTS = frozenset(contract.contract_id for contract in CONTRACTS)

# Legacy report-row identifiers stay stable for existing consumers.
REPORT_IDS = {
    "P0-CASH": "cash",
    "P0-FEES": "fees",
    "P0-ACTIONS": "actions",
    "P0-DIVIDEND": "dividend",
    "P0-COVERAGE": "coverage",
    "P0-NAV-GAP": "nav_gap",
    "P0-CONSIDERATION": "consideration",
    "P0-CONSERVATION": "conservation",
    "P0-SIZING": "sizing_budget",
    "P0-CAPACITY": "capacity_fill",
    "P0-SLOT-CAP": "slot_cap",
    "P0-LIQUIDATION": "liquidation",
    "P0-VERIFY": "verification",
    "P0-GATE": "gate",
    "P0-CONTINUITY": "continuity",
    "P0-IDENTITY": "identity",
    "P0-SCOPED-ADJ": "scoped_adjustment",
    "P0-UNIVERSE": "universe",
    "P0-SIGNALS": "signals",
    "P0-LIFECYCLE": "lifecycle",
    "P0-POLICIES": "policies",
    "P0-ADV": "adv",
    "P0-EXECUTION": "execution_causality",
    "P0-BOUNDARY": "boundary",
    "P0-EVIDENCE": "evidence",
    "P0-PREFIX": "prefix",
    "P0-SCENARIO": "scenario_isolation",
    "P0-FULL-LIQUIDATION": "full_liquidation",
    "P0-E2E": "e2e",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _row_contract_id(row) -> str | None:
    if isinstance(row, ContractSpec):
        return row.contract_id
    return row.get("contract_id")


def _row_status(row) -> str | None:
    if isinstance(row, ContractSpec):
        return row.contract_status
    return row.get("contract_status", row.get("status"))


def contract_counts(rows: Iterable) -> dict[str, int]:
    """Count rows by declared status, including zeroes for the whole vocabulary."""
    counts = {status: 0 for status in CONTRACT_STATUSES}
    for row in rows:
        status = _row_status(row)
        if status is None:
            continue
        counts[status] = counts.get(status, 0) + 1
    return counts


def validate_contract_rows(rows: Iterable, require_pass: bool = True) -> list[str]:
    """Return structural and (optionally) authorization errors for contract rows."""
    rows = list(rows)
    reasons: list[str] = []
    ids = [_row_contract_id(row) for row in rows]
    if len(ids) != len(set(ids)):
        duplicates = sorted({contract_id for contract_id in ids if ids.count(contract_id) > 1})
        reasons.append(f"duplicate contract ids: {', '.join(str(value) for value in duplicates)}")
    expected = set(MANDATORY_CONTRACTS)
    actual = {contract_id for contract_id in ids if contract_id}
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        reasons.append(f"missing contract ids: {', '.join(missing)}")
    if extra:
        reasons.append(f"extra contract ids: {', '.join(extra)}")
    for row in rows:
        status = _row_status(row)
        contract_id = _row_contract_id(row) or "<unknown>"
        if status not in CONTRACT_STATUSES:
            reasons.append(f"contract {contract_id} has disallowed status {status!r}")
        elif require_pass and status != AUTHORIZING_CONTRACT_STATUS:
            reasons.append(f"contract {contract_id} is {status}, not {AUTHORIZING_CONTRACT_STATUS}")
    return reasons


def _case_module(case: dict) -> str:
    classname = case.get("classname")
    if not classname:
        classname = case.get("id", "").split("::", 1)[0]
    parts = classname.split(".")
    if len(parts) >= 2 and parts[0] == "tests":
        return parts[1]
    return parts[0] if parts else ""


def _component_result(contract: ContractSpec, cases: list[dict]) -> str:
    if not contract.component_modules:
        return "NO COMPONENT"
    groups = [[case for case in cases if _case_module(case) == module]
              for module in contract.component_modules]
    selected = [case for group in groups for case in group]
    statuses = {case.get("status") for case in selected}
    if "FAIL" in statuses:
        return "FAIL"
    if "SKIP" in statuses:
        return "SKIP"
    if not all(groups):
        return "NOT IMPLEMENTED"
    return "PASS"


def component_rows(cases: Iterable[dict], contracts: Iterable[ContractSpec] = CONTRACTS) -> list[dict]:
    """One declared row per contract. A component PASS never edits contract_status."""
    cases = list(cases)
    rows = []
    for contract in contracts:
        selected = [case for case in cases
                    if _case_module(case) in contract.component_modules]
        rows.append({
            "id": REPORT_IDS.get(contract.contract_id, contract.contract_id.lower()),
            "contract_id": contract.contract_id,
            "title": contract.title,
            "contract_status": contract.contract_status,
            "component_status": contract.component_status,
            "component_result": _component_result(contract, cases),
            "component_modules": contract.component_modules,
            "test_results": selected,
            "blocking_reason": contract.blocking_reason,
            "required_proof": contract.required_proof,
        })
    return rows


def integration_authorized(rows: Iterable, regression: dict | None) -> bool:
    """Legacy component/regression gate; the report uses authorization_reasons."""
    if validate_contract_rows(rows, require_pass=True):
        return False
    if not regression:
        return False
    tests = regression.get("tests", 0)
    passed = regression.get("pass", 0)
    failed = regression.get("fail", 0)
    skipped = regression.get("skip", 0)
    return tests > 0 and passed == tests and failed == 0 and skipped == 0


def _canonical_node(value: str) -> str:
    return value.replace("::", ".")


def _coerce_policy(policy) -> dict:
    if policy is None:
        return {"schema_version": 1, "default": "MANDATORY", "rules": []}
    if isinstance(policy, (str, os.PathLike, Path)):
        return json.loads(Path(policy).read_text(encoding="utf-8"))
    return dict(policy)


def skip_policy(cases: Iterable[dict], policy=None) -> dict:
    """Classify every JUnit skip; an unmatched skip is MANDATORY by default."""
    policy = _coerce_policy(policy)
    default = policy.get("default", "MANDATORY")
    rules = {_canonical_node(rule["match"]): rule for rule in policy.get("rules", [])}
    counts = {"tests": 0, "pass": 0, "fail": 0, "skip": 0,
              "mandatory_skips": 0, "optional_skips": 0, "diagnostic_skips": 0}
    details = []
    for case in cases:
        counts["tests"] += 1
        status = case.get("status")
        if status == "PASS":
            counts["pass"] += 1
        elif status == "FAIL":
            counts["fail"] += 1
        elif status == "SKIP":
            counts["skip"] += 1
            rule = rules.get(_canonical_node(case.get("id", "")))
            classification = rule.get("classification", default) if rule else default
            dependency = rule.get("dependency") if rule else None
            detail = {
                "id": case.get("id"),
                "classification": classification,
                "dependency": dependency,
                "matched_rule": rule is not None,
            }
            details.append(detail)
            if classification == "OPTIONAL_DEPENDENCY":
                counts["optional_skips"] += 1
            elif classification == "DIAGNOSTIC":
                counts["diagnostic_skips"] += 1
            else:
                counts["mandatory_skips"] += 1
    counts["skip_details"] = details
    counts["mandatory_skip_ids"] = [detail["id"] for detail in details
                                    if detail["classification"] not in
                                    ("OPTIONAL_DEPENDENCY", "DIAGNOSTIC")]
    counts["optional_skip_details"] = [detail for detail in details
                                       if detail["classification"] == "OPTIONAL_DEPENDENCY"]
    counts["policy_schema_version"] = policy.get("schema_version", 1)
    counts["default"] = default
    return counts


def authorization_reasons(rows: Iterable, regression: dict | None, *,
                          binding: dict | None = None,
                          inventory: dict | None = None,
                          skip_report: dict | None = None) -> list[str]:
    """Every reason the aggregate gate must stay CLOSED."""
    reasons = list(validate_contract_rows(rows, require_pass=True))
    if binding is not None:
        reasons.extend(binding.get("reasons", []))
    if inventory is not None and not inventory_integrity_ok(inventory):
        reasons.append(f"inventory integrity is not OK (state={inventory.get('state')})")
    if regression is None:
        reasons.append("regression run is missing")
        regression = {}
    tests = regression.get("tests", 0)
    failed = regression.get("fail", 0)
    if tests <= 0:
        reasons.append("regression run has zero tests")
    if failed > 0:
        reasons.append(f"regression run has {failed} failures")
    mandatory = regression.get("mandatory_skips", 0)
    if skip_report is not None:
        mandatory = skip_report.get("mandatory_skips", mandatory)
        if skip_report.get("skip", 0) != regression.get("skip", 0):
            reasons.append("regression skip counters disagree with the skip policy report")
    elif regression.get("skip", 0) > 0:
        mandatory = max(mandatory, regression.get("skip", 0))
    if mandatory > 0:
        reasons.append(f"regression run has {mandatory} mandatory skips")
    # Preserve order while removing exact duplicates.
    return list(dict.fromkeys(reasons))


# ---------------------------------------------------------------------------
# Inventory: D-18 eight-state machine
# ---------------------------------------------------------------------------

def _coerce_entries(entries) -> list[dict]:
    if isinstance(entries, dict):
        return list(entries.get("data", entries.get("entries", [])))
    return list(entries)


def _coerce_acknowledged(acknowledged) -> list[dict]:
    if not acknowledged:
        return []
    if isinstance(acknowledged, dict):
        return [{"path": path, "original_sha256": None, "accepted_sha256": digest}
                for path, digest in acknowledged.items()]
    rows = []
    for item in acknowledged:
        if not isinstance(item, dict):
            raise TypeError("acknowledged exceptions must be mappings")
        rows.append({
            "path": item["path"],
            "original_sha256": (item.get("original_sha256") or item.get("original")
                                or item.get("before_sha256")),
            "accepted_sha256": (item.get("accepted_sha256") or item.get("accepted")
                                or item.get("after_sha256")),
        })
    return rows


def _canonical_entries_sha(entries: list[dict]) -> str:
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256(payload)


def _inventory_hash(root: Path, entries: list[dict]) -> str:
    inventory_file = root / "data" / "manifests" / "dataset_hashes.json"
    if inventory_file.is_file():
        try:
            parsed = json.loads(inventory_file.read_text(encoding="utf-8"))
            file_entries = parsed.get("data") if isinstance(parsed, dict) else parsed
            if file_entries == entries:
                return _sha256(inventory_file.read_bytes())
        except (OSError, ValueError, TypeError):
            pass
    return _canonical_entries_sha(entries)


def inventory_state(root, entries, acknowledged=()) -> dict:
    """Read every inventoried byte and classify one of the eight D-18 states."""
    root = Path(root).resolve()
    entries = _coerce_entries(entries)
    ack_rows = _coerce_acknowledged(acknowledged)
    ack_by_path = {row["path"]: row for row in ack_rows}
    inventory_paths = {entry["path"] for entry in entries}

    unexpected_changes: list[dict] = []
    acknowledged_changes: list[dict] = []
    exception_mismatch: list[dict] = []
    exception_list_changed: list[dict] = []
    missing_files: list[str] = []
    read_errors: list[str] = []
    errors: list[dict] = []
    original_seen: set[str] = set()

    for entry in entries:
        relative = entry["path"]
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            read_errors.append(relative)
            errors.append({"path": relative, "error": "inventory entry escapes the checkout"})
            continue
        try:
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        except FileNotFoundError as error:
            missing_files.append(relative)
            errors.append({"path": relative, "error": str(error)})
            continue
        except OSError as error:
            read_errors.append(relative)
            errors.append({"path": relative, "error": str(error)})
            continue

        baseline = entry["sha256"]
        if digest == baseline:
            if relative in ack_by_path:
                original_seen.add(relative)
            continue

        change = {"path": relative, "before_sha256": baseline, "after_sha256": digest}
        ack = ack_by_path.get(relative)
        if ack is None:
            unexpected_changes.append(change)
            continue
        if ack["original_sha256"] is None:
            ack["original_sha256"] = baseline
        if baseline != ack["original_sha256"]:
            exception_list_changed.append({
                "path": relative,
                "reason": "inventory baseline differs from the acknowledged original",
                "inventory_sha256": baseline,
                "acknowledged_original_sha256": ack["original_sha256"],
            })
        elif digest == ack["accepted_sha256"]:
            acknowledged_changes.append(change)
        else:
            exception_mismatch.append({
                **change,
                "accepted_sha256": ack["accepted_sha256"],
            })

    for path in sorted(set(ack_by_path) - inventory_paths):
        exception_list_changed.append({
            "path": path,
            "reason": "acknowledged entry is absent from the inventory",
        })

    exception_absent = sorted(
        path for path in ack_by_path
        if path in original_seen)

    if missing_files:
        state = "MISSING_FILE"
    elif read_errors:
        state = "READ_ERROR"
    elif unexpected_changes:
        state = "UNEXPECTED_CHANGE"
    elif exception_mismatch:
        state = "AUTHORIZED_EXCEPTION_MISMATCH"
    elif exception_list_changed:
        state = "EXCEPTION_LIST_CHANGED"
    elif acknowledged_changes:
        state = "AUTHORIZED_EXCEPTION"
    elif exception_absent:
        state = "AUTHORIZED_EXCEPTION_ABSENT"
    else:
        state = "CLEAN"

    result = {
        "state": state,
        "integrity_ok": state in INTEGRITY_OK_STATES,
        "checked_files": len(entries),
        "unexpected_changes": unexpected_changes,
        "errors": errors,
        "missing_files": missing_files,
        "read_errors": read_errors,
        "acknowledged_changes": acknowledged_changes,
        "acknowledged_unrecovered_changes": acknowledged_changes,
        "exception_absent": exception_absent,
        "inventory_sha256": _inventory_hash(root, entries),
        "all_hashes_match": not (unexpected_changes or errors or exception_mismatch
                                 or exception_list_changed),
        # Compatibility only: the integrity predicate is state-based.
        "only_accepted_exception": state == "AUTHORIZED_EXCEPTION",
        "raw_files_checked": sum(entry["path"].startswith("data/raw/")
                                 for entry in entries),
        "database_files_checked": sum(entry["path"].startswith("data/market.duckdb")
                                      for entry in entries),
        "generated_manifest_trust": GENERATED_MANIFEST_TRUST,
    }
    return result


def inventory_integrity_ok(result: dict) -> bool:
    """The single CLI/report predicate: state in INTEGRITY_OK_STATES."""
    return result.get("state") in INTEGRITY_OK_STATES


# ---------------------------------------------------------------------------
# Revision binding: D-19
# ---------------------------------------------------------------------------

def _iter_source_files(root: Path):
    files: dict[str, Path] = {}
    for relative_root in SOURCE_ROOTS:
        base = root / relative_root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            parts = set(Path(relative).parts)
            if parts & EXCLUDED_PARTS:
                continue
            if path.suffix in EXCLUDED_SUFFIXES:
                continue
            if relative.startswith("verification/"):
                continue
            files[relative] = path
    for name in SOURCE_FILES:
        path = root / name
        if path.is_file():
            files[name] = path
    return files


def source_fingerprint(root) -> str:
    """SHA-256 over path/length/bytes for src, scripts, tests, pyproject and uv.lock."""
    root = Path(root).resolve()
    chunks = []
    for relative, path in sorted(_iter_source_files(root).items()):
        data = path.read_bytes()
        chunks.append(relative.encode("utf-8") + b"\0" + str(len(data)).encode("ascii")
                      + b"\0" + data)
    return _sha256(b"".join(chunks))


def golden_sha256(root) -> dict[str, str]:
    root = Path(root).resolve()
    golden = root / "tests" / "golden"
    if not golden.is_dir():
        return {}
    return {path.relative_to(root).as_posix(): _sha256(path.read_bytes())
            for path in sorted(golden.glob("*.json")) if path.is_file()}


def _fallback_dirty(root: Path) -> tuple[str, str | None]:
    files = _iter_source_files(root)
    if not files:
        return "clean", None
    chunks = [relative.encode("utf-8") + b"\0" + path.read_bytes()
              for relative, path in sorted(files.items())]
    return "dirty", _sha256(b"".join(chunks))


def dirty_fingerprint(root) -> tuple[str, str | None]:
    """Tracked modifications/deletions plus untracked files under source paths.

    ``verification/`` is deliberately outside the pathspec so writing evidence
    never changes the fingerprint of the tree that produced it.
    """
    root = Path(root).resolve()
    pathspec = ["src", "scripts", "tests", "pyproject.toml", "uv.lock"]
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "-z",
             "--untracked-files=all", "--", *pathspec],
            capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return _fallback_dirty(root)

    records = [record for record in completed.stdout.split("\0") if record]
    changes: list[tuple[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        code = record[:2]
        path = record[3:]
        changes.append((code, path))
        if code[:1] in {"R", "C"} or code[1:2] in {"R", "C"}:
            if index < len(records):
                changes.append((code, records[index]))
                index += 1

    chunks = []
    for code, relative in sorted(changes):
        relative = relative.replace("\\", "/")
        parts = set(Path(relative).parts)
        if parts & EXCLUDED_PARTS or Path(relative).suffix in EXCLUDED_SUFFIXES:
            continue
        path = root / relative
        if path.is_file():
            data = path.read_bytes()
            chunks.append(f"{code}\0{relative}\0{len(data)}".encode("utf-8")
                          + b"\0" + data)
        else:
            chunks.append(f"{code}\0{relative}\0MISSING".encode("utf-8"))
    if not chunks:
        return "clean", None
    return "dirty", _sha256(b"".join(chunks))


def revision_binding(tested_commit, head_commit, *, worktree_state,
                     dirty_fingerprint_value, source_fingerprint_value,
                     expected_source_fingerprint=None, golden_hashes=None,
                     expected_golden_hashes=None, inventory_sha256,
                     expected_inventory_sha256=None,
                     allow_evidence_only_descendant=False,
                     changed_paths=()) -> dict:
    """Bind evidence to a commit, worktree state, source, goldens and inventory."""
    reasons: list[str] = []
    evidence_only_descendant = False
    commit_matches = bool(tested_commit) and tested_commit == head_commit
    if not commit_matches:
        if allow_evidence_only_descendant:
            changed = [str(path).replace("\\", "/") for path in changed_paths]
            if changed and all(path.startswith("verification/") for path in changed):
                commit_matches = True
                evidence_only_descendant = True
        if not commit_matches:
            reasons.append(
                f"tested_commit {tested_commit!r} does not match head_at_verification {head_commit!r}")

    if worktree_state not in {"clean", "dirty"}:
        reasons.append(f"invalid worktree_state {worktree_state!r}")
    if worktree_state == "dirty":
        if not (isinstance(dirty_fingerprint_value, str)
                and HEX64.match(dirty_fingerprint_value)):
            reasons.append("dirty worktree has no reproducible 64-hex dirty_fingerprint")

    source_matches = (expected_source_fingerprint is None
                      or source_fingerprint_value == expected_source_fingerprint)
    if not source_matches:
        reasons.append("source_fingerprint does not match the expected source tree")

    golden_matches = True
    if expected_golden_hashes is not None:
        golden_matches = dict(golden_hashes or {}) == dict(expected_golden_hashes)
        if not golden_matches:
            reasons.append("golden_sha256 does not match the expected golden set")

    inventory_matches = (expected_inventory_sha256 is None
                         or inventory_sha256 == expected_inventory_sha256)
    if not inventory_matches:
        reasons.append("inventory_sha256 does not match the expected inventory")

    return {
        "tested_commit": tested_commit,
        "head_commit": head_commit,
        "commit_matches_head": tested_commit == head_commit,
        "evidence_only_descendant": evidence_only_descendant,
        "worktree_state": worktree_state,
        "dirty_fingerprint": dirty_fingerprint_value,
        "source_fingerprint": source_fingerprint_value,
        "source_matches": source_matches,
        "golden_hashes": dict(golden_hashes or {}),
        "golden_matches": golden_matches,
        "inventory_sha256": inventory_sha256,
        "inventory_matches": inventory_matches,
        "ok": not reasons,
        "reasons": reasons,
    }
