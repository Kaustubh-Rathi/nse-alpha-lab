"""Reproduce the four Batch 0 findings (F1, F2, F3, F5) as machine-readable JSON.

The probes are read-only. They exercise production components at the tested
revision and record the observed contradiction with the planned contract; they
do not mark any finding fixed and never write under data/.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ROOT = Path("verification/stage1")
CAPS = ("99.994", "99.996", "19.999", "100.00", "99.99", "0.00")
EXPECTED_CAPS = {
    "99.994": {"applied_cap": "99.99", "quantity": 9, "notional": "90.00"},
    "99.996": {"applied_cap": "99.99", "quantity": 9, "notional": "90.00"},
    "19.999": {"applied_cap": "19.99", "quantity": 1, "notional": "10.00"},
    "100.00": {"applied_cap": "100.00", "quantity": 10, "notional": "100.00"},
    "99.99": {"applied_cap": "99.99", "quantity": 9, "notional": "90.00"},
    "0.00": {"applied_cap": "0.00", "quantity": 0, "notional": "0.00"},
}


def check_output(root: Path, path: Path) -> Path:
    root = root.resolve()
    output = Path(path)
    output = (output if output.is_absolute() else root / output).resolve()
    if not output.is_relative_to((root / ALLOWED_ROOT).resolve()):
        raise ValueError("Reproduction artifacts must be written under verification/stage1/")
    return output


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _finding(identifier: str, command: str, observed: dict, expected: str) -> dict:
    body = {"id": identifier, "status": "DEFECT_REPRODUCED", "command": command,
            "observed": observed, "expected_contract": expected}
    body["artifact_sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
    return body


def _zero_costs():
    from nse_pipeline.specs import CostSpec
    return CostSpec(brokerage_bps=0, brokerage_cap_per_order=0, stt_bps_buy=0,
                    stt_bps_sell=0, stamp_bps_buy=0, exchange_bps=0,
                    gst_on_brokerage_pct=0)


def _execution(cap):
    from nse_pipeline.specs import ExecutionSpec
    return ExecutionSpec(trade_settlement_lag_sessions=1, adverse_slippage_bps_buy=0,
                         adverse_slippage_bps_sell=0, max_order_notional=float(cap))


def probe_f1() -> dict:
    from nse_pipeline.backtest.fees import FillQuote, affordable_buy, bounded_sell
    costs = _zero_costs()
    observed = {"applied_principal_cap_exposed": "applied_principal_cap" in FillQuote.__dataclass_fields__,
                "buy": {}, "sell": {}, "expected": EXPECTED_CAPS}
    for cap in CAPS:
        execution = _execution(cap)
        buy = affordable_buy(100, "10", "1000000", "1000000", costs, execution)
        sell = bounded_sell(100, "10", costs, execution)
        observed["buy"][cap] = {"quantity": buy.quantity, "notional": str(buy.notional),
                                "reason": buy.reason}
        observed["sell"][cap] = {"quantity": sell.quantity, "notional": str(sell.notional),
                                 "reason": sell.reason}
    return _finding(
        "F1",
        "uv run --frozen python scripts/reproduce_stage1_findings.py --out <baseline>/reproduction.json",
        observed,
        "Declared max_order_notional is floored to paise at use; 99.994/99.996 admit at most "
        "99.99 principal (9 shares at 10.00) on both sides and FillQuote records applied_principal_cap.",
    )


def probe_f2() -> dict:
    from nse_pipeline.backtest.evidence import ObservationLedger
    from nse_pipeline.backtest.observations import Bar, SessionStream
    from nse_pipeline.backtest.signals import MomentumRanks, rank_momentum
    from nse_pipeline.specs import MomentumSpec

    first, second = date(2024, 6, 3), date(2024, 6, 4)
    ledger = ObservationLedger(SessionStream((first, second)))
    for session, close in ((first, "10"), (second, "11")):
        ledger.ingest(session, (Bar("K", "K", "EQ", session, None, None, None, close,
                                    100, True),))
    spec = MomentumSpec(lookback_sessions=1, min_observations=1, price_basis="close")

    def basis_one(key, session, as_of):
        bar = ledger.bar(key, session)
        return bar.close if bar is not None and bar.valid_ohlcv else None

    def basis_two(key, session, as_of):
        bar = ledger.bar(key, session)
        return bar.close + 1 if bar is not None and bar.valid_ohlcv else None

    one = rank_momentum(ledger, second, ("K",), spec, basis_one)
    two = rank_momentum(ledger, second, ("K",), spec, basis_two)
    observed = {
        "basis_provenance_exposed": "basis_provenance" in MomentumRanks.__dataclass_fields__,
        "first_price_basis": one.price_basis,
        "second_price_basis": two.price_basis,
        "first_momentum": str(one.ranked[0].momentum),
        "second_momentum": str(two.ranked[0].momentum),
    }
    return _finding(
        "F2",
        "uv run --frozen python scripts/reproduce_stage1_findings.py --out <baseline>/reproduction.json",
        observed,
        "MomentumRanks emits BasisProvenance(kind, basis_id, name, version, source_digest, "
        "parameters); two behaviourally different callables never share price_basis 'close'.",
    )


def probe_f3() -> dict:
    from nse_pipeline.backtest.accounting_types import EventTime, Phase, Position
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.valuation import Mark, value

    session = date(2024, 6, 4)
    at = EventTime(session, Phase.CLOSE_VALUATION)
    positions = tuple(Position(f"p{i}", f"KEY{i}", 100, date(2024, 5, 31)) for i in range(2))
    marks = tuple(Mark(position.position_id, "10", session, at) for position in positions)
    book = Portfolio("0", on=session, positions=positions)
    result = value(book.snapshot, at, marks=marks)
    observed = {
        "reference_coverage_reason": result.reference_coverage_reason,
        "expected_reference_coverage_reason": "complete_current_date",
        "nav": None if result.nav is None else str(result.nav),
        "nav_reason_exposed": "nav_reason" in type(result).__dataclass_fields__,
        "position_completeness": str(result.position_completeness),
        "unresolved_positions": list(result.unresolved_positions),
    }
    return _finding(
        "F3",
        "uv run --frozen python scripts/reproduce_stage1_findings.py --out <baseline>/reproduction.json",
        observed,
        "A fully priced current-session book reports complete_current_date with a separate "
        "nav_reason=nav_available; the six reference states are a closed vocabulary.",
    )


def probe_f5() -> dict:
    from nse_pipeline.backtest.accounting_types import Evidence, EventTime, Phase, Position
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.dividends import DividendTerms
    from nse_pipeline.specs import ValuationPolicy

    session = date(2024, 6, 3)
    book = Portfolio("10000", on=session,
                     positions=(Position("p", "AAA", 1000, date(2024, 5, 31)),))
    policy = ValuationPolicy(fallback_lag=None, lag_unit="calendar_days",
                             dividend_settlement_mode="hold_receivable")
    terms = DividendTerms("d", "2", Evidence("issuer dividend fixture", "AAA", session,
        EventTime(date(2024, 5, 31), Phase.CLOSE_VALUATION), True))
    at = EventTime(session, Phase.PRE_OPEN_ACTIONS)
    book.entitle_dividend("p", terms, policy, at=at)
    book.accrue_dividend("d", at=EventTime(session, Phase.PRE_OPEN_ACTIONS, 1))
    claim = next(c for c in book.snapshot.claims if c.claim_id == "dividend:d")
    book.observe_session(date(2024, 6, 4))
    error = ""
    try:
        book.settle("dividend:d", at=EventTime(date(2024, 6, 4), Phase.POST_OPEN_SETTLEMENT))
    except ValueError as caught:
        error = str(caught)
    observed = {
        "claim_due": None if claim.due is None else str(claim.due),
        "claim_basis": claim.basis,
        "confirm_operation": hasattr(book, "confirm_dividend_payment"),
        "void_operation": hasattr(book, "void_dividend"),
        "settle_error": error,
        "cash": str(book.balances.cash),
        "dividend_receivable": str(book.balances.dividend_receivable),
        "expected_lifecycle": "accrued_held -> confirmed -> settled (or voided)",
    }
    return _finding(
        "F5",
        "uv run --frozen python scripts/reproduce_stage1_findings.py --out <baseline>/reproduction.json",
        observed,
        "hold_receivable is a pending accrued_held claim: confirm_dividend_payment attaches later "
        "verified evidence with a causal due date, void_dividend writes off terminally, and the "
        "claim is disclosed as an unresolved settlement claim until confirmation.",
    )


def run_probes(root: Path) -> dict:
    sys.path.insert(0, str(Path(root).resolve() / "src"))
    findings = [probe_f1(), probe_f2(), probe_f3(), probe_f5()]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tested_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "findings": findings,
        "overall_p0_gate": "CLOSED",
        "note": "No finding is fixed at the baseline; every status is DEFECT_REPRODUCED.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    output = check_output(ROOT, args.out)
    result = run_probes(ROOT)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"tested_commit": result["tested_commit"],
                      "finding_ids": [f["id"] for f in result["findings"]],
                      "statuses": [f["status"] for f in result["findings"]]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
