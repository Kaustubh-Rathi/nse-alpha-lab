"""Protect the real data tree before test collection, including native writes.

Some tests assert on content that is deliberately excluded from the public
release: the private ``verification/`` evidence tree, the separately published
``data/`` pack, and a handful of private source modules. Those tests skip
cleanly on a clean checkout, and still run when the private content is present.
Modules that import non-public content at module level are skipped before pytest
imports them, so a clean checkout never fails collection.
"""
from pathlib import Path

import pytest

from nse_pipeline.verification import protect_artifacts

ROOT = Path(__file__).resolve().parents[1]
VERIFICATION = ROOT / "verification"
DATA = ROOT / "data"
PRIVATE_SOURCES = (
    ROOT / "src" / "nse_pipeline" / "schemes" / "verify2.py",
    ROOT / "src" / "nse_pipeline" / "schemes" / "verify3.py",
    ROOT / "src" / "get_data.py",
)

# Whole modules whose tests read the private verification/ evidence tree.
VERIFICATION_MODULES = frozenset({
    "test_accounting_continuity_evidence.py",
    "test_accounting_report.py",
    "test_nifty_calendar_evidence.py",
    "test_nifty_membership_evidence.py",
    "test_stage1_continuity_evidence_lane.py",
    "test_stage1_document_evidence_lane.py",
    "test_stage1_liquidation_evidence_lane.py",
    "test_stage1_universe_evidence_lane.py",
    "test_verification_isolation.py",
})

# Whole modules whose tests read the separately published data/ pack.
DATA_MODULES = frozenset({
    "test_monthly_universe.py",
    "test_verify.py",
})

# Whole modules whose entry points live in private source modules.
PRIVATE_SOURCE_MODULES = frozenset({
    "test_cli_contract.py",
})

# Individual tests in otherwise self-contained modules, by node id.
VERIFICATION_TESTS = frozenset({
    "tests/test_stage1_baseline_freeze.py::test_reproduction_records_f1_f2_f3_f5",
    "tests/test_stage1_evidence.py::test_mandatory_skip_blocks_authorization_but_optional_pdf_skip_is_disclosed",
    "tests/test_stage1_evidence.py::test_skip_policy_file_contains_the_five_optional_pdf_tests",
    "tests/test_stage1_evidence.py::test_skip_policy_scenarios_match_the_hand_authored_golden",
})

DATA_TESTS = frozenset({
    "tests/test_canonical_schemes.py::CanonicalBuildTests::test_cached_integration_covers_346_ranked_plus_50_older_days_without_claiming_completeness",
    "tests/test_canonical_schemes.py::CanonicalBuildTests::test_official_cached_withdrawal_demotes_only_dropped_khl_leg",
    "tests/test_index_membership.py::MembershipTests::test_downloaded_snapshots_have_verified_original_sources",
    "tests/test_ledger.py::CoverageTests::test_builtin_sample_passes_validation",
    "tests/test_nifty_alt.py::NiftyAltTests::test_delivered_samples_have_verified_provenance",
    "tests/test_nifty_vendor.py::VendorValidatorTests::test_delivered_coverage_file_validates",
    "tests/test_nifty_vendor.py::VendorValidatorTests::test_public_price_only_where_genuinely_public",
    "tests/test_purchase.py::PurchasePackTests::test_delivered_pack_json_validates",
    "tests/test_purchase.py::PurchasePackTests::test_delivered_raw_files_match_module",
})

PRIVATE_SOURCE_TESTS = frozenset({
    "tests/test_foundation_paths.py::test_cli_uses_environment_root_from_unrelated_cwd",
    "tests/test_foundation_paths.py::test_verifier_queue_reader_honors_arbitrarily_named_explicit_root[2]",
    "tests/test_foundation_paths.py::test_verifier_queue_reader_honors_arbitrarily_named_explicit_root[3]",
})

_guard = None


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "needs_payload: test needs content deliberately excluded from the public "
        "release (verification/ evidence tree, data/ pack or private source modules)",
    )
    global _guard
    root = Path(__file__).resolve().parents[1]
    _guard = protect_artifacts([root / "data"])
    _guard.__enter__()


def pytest_unconfigure(config):
    if _guard is not None:
        _guard.__exit__(None, None, None)


def _module_skip_reason(module_name: str) -> str | None:
    if module_name in VERIFICATION_MODULES and not VERIFICATION.exists():
        return "requires the non-public verification/ evidence payload"
    if module_name in DATA_MODULES and not DATA.exists():
        return "requires the separately published data/ pack"
    if module_name in PRIVATE_SOURCE_MODULES and not all(
            path.exists() for path in PRIVATE_SOURCES):
        return "requires private source modules that are not part of the public export"
    return None


class _SkippedModule(pytest.File):
    """Report a module as skipped without importing it."""

    def collect(self):
        pytest.skip(_module_skip_reason(self.path.name) or "requires non-public content")


def pytest_pycollect_makemodule(module_path, parent):
    if _module_skip_reason(module_path.name):
        return _SkippedModule.from_parent(parent, path=module_path)
    return None


def _test_skip_reason(nodeid: str) -> str | None:
    if nodeid in VERIFICATION_TESTS and not VERIFICATION.exists():
        return "requires the non-public verification/ evidence payload"
    if nodeid in DATA_TESTS and not DATA.exists():
        return "requires the separately published data/ pack"
    if nodeid in PRIVATE_SOURCE_TESTS and not all(
            path.exists() for path in PRIVATE_SOURCES):
        return "requires private source modules that are not part of the public export"
    return None


def pytest_collection_modifyitems(config, items):
    for item in items:
        reason = _test_skip_reason(item.nodeid)
        if reason:
            item.add_marker(pytest.mark.needs_payload)
            item.add_marker(pytest.mark.skip(reason=reason))
