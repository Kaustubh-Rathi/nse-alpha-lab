"""Evidence-based 2000+ research readiness (never infer missing coverage).

This is a data-quality gate, not an alpha detector. Fundamentals are deferred;
historical index membership is conditional on the requested universe. Reads
the market database in read-only mode and writes a separate, current report.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
from nse_pipeline.paths import data_root
from nse_pipeline.build.portable import open_catalog

DEFAULT_DATA = data_root()


def evaluate_evidence(evidence: dict, start: str, end: str,
                      universe: str = "research", total_return: bool = True) -> dict:
    start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
    if end_date < start_date:
        raise ValueError("End precedes start")
    if universe not in ("research", "nifty50", "nifty500"):
        raise ValueError("Unknown universe")
    blockers = []

    def require(ok, code, message, remedy):
        if not ok:
            blockers.append(dict(code=code, reason=message, next_action=remedy))

    prices = evidence.get("prices", {})
    require(bool(prices.get("rows")) and bool(prices.get("first")) and bool(prices.get("last"))
            and prices["first"] <= start and prices["last"] >= end,
            "price_window", "Observed bars do not span the requested window.",
            "Use the last verified session as the end; acquire official missing reports. Missing weekdays are not automatically holidays.")
    require(prices.get("duplicate_rows") == 0, "price_duplicates",
            "Security/session uniqueness is absent or failed.", "Resolve duplicate identities/series, never silently overwrite fills.")
    for name, message, remedy in (
        ("calendar", "The exchange calendar has unresolved dates or unassessed special sessions.",
         "Obtain official session/holiday evidence; do not fill absent sessions."),
        ("corporate_actions", "Corporate-action coverage is incomplete, including pre-2011 actions and rights.",
         "Acquire cited issuer/exchange actions with exact ex-dates and dated identities; normalize and rebuild factors."),
        ("schemes", "Scheme terms, entitlement dates or successor identities remain unresolved.",
         "Review canonical unresolved events; verify ZIP/OCR pages and final lifecycle terms, then bind each entitlement leg."),
        ("identity", "Historical security identity/asset-class coverage is not verified complete.",
         "Resolve dated symbol/ISIN transitions, symbol reuse and listing eligibility; today's master cannot backfill history."),
        ("terminal_proceeds", "Delisting/suspension proceeds and executable exits are not fully reconstructed.",
         "Obtain verified cash/stock consideration; preserve missing marks as uncertainty, not liquid cash."),
    ):
        record = evidence.get(name, {})
        require(record.get("complete") is True and record.get("unresolved") == 0,
                name, message, remedy)
    if total_return:
        dividends = evidence.get("dividends", {})
        require(dividends.get("complete") is True and dividends.get("unresolved") == 0,
                "dividends", "Dividend coverage is incomplete, especially before 2011.",
                "Acquire historical per-share cash amounts and dates; filing fundamentals are not needed for dividends.")
    execution = evidence.get("execution", {})
    require(execution.get("complete") is True, "execution_capabilities",
            "No fully reconciled raw-share execution path supports every corporate-action type.",
            "Reconcile dividends, splits, capital reductions, rights and pending successor entitlements; safety checks do not fabricate missing terms.")
    if universe != "research":
        membership = evidence.get("nifty_membership", {}).get(universe, {})
        require(membership.get("complete") is True and membership.get("unresolved") == 0
                and membership.get("first", "9999") <= start
                and membership.get("last", "0000") >= end,
                "nifty_membership", "Exact effective-date constituent history does not cover the requested index/window.",
                "Obtain a licensed, verified sample and full effective-date intervals; monthly snapshots are not daily membership.")
    return dict(schema_version=1, generated_at=datetime.now(timezone.utc).isoformat(),
                requested_start=start, requested_end=end, universe=universe,
                requirements={"fundamentals": "deferred_not_required",
                              "nifty_membership": "not_required" if universe == "research" else "required",
                              "total_return": total_return},
                ready_for_backtesting=not blockers, ready_for_alpha_claim=False,
                blocker_codes=[b["code"] for b in blockers], blockers=blockers,
                evidence=evidence,
                warnings=["Passing unit tests establishes code behavior, not historical completeness or alpha.",
                          "Unresolved-event exclusions are a research sensitivity, not proof of unbiased returns.",
                          "Old saved performance reports predate execution/eligibility corrections and must be rerun."])


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def build_readiness(data: Path, start: str = "2000-01-03", end: str = "2026-11-06",
                    universe: str = "research", out: Path | None = None) -> dict:
    import duckdb

    data = Path(data)
    reports = data / "reports"
    evidence = {}
    db_path = data / "market.duckdb"
    if db_path.exists():
        with open_catalog(database=db_path) as db:
            row = db.execute("SELECT count(*), min(date), max(date), count(DISTINCT date), "
                             "count(*) FILTER (WHERE NOT valid_ohlcv) FROM bars "
                             "WHERE date BETWEEN ? AND ?", [start, end]).fetchone()
            duplicates = db.execute("SELECT coalesce(sum(n-1),0) FROM "
                                    "(SELECT count(*) n FROM bars WHERE date BETWEEN ? AND ? "
                                    "GROUP BY date,symbol,series HAVING count(*) > 1)", [start, end]).fetchone()[0]
        evidence["prices"] = dict(rows=row[0], first=str(row[1]) if row[1] else None,
                                  last=str(row[2]) if row[2] else None, sessions=row[3],
                                  invalid_rows=row[4], duplicate_rows=int(duplicates),
                                  source=str(db_path), read_only=True)

    calendar = _read_json(reports / "coverage-audit.json")
    old_actions = _read_json(reports / "pre2011-actions-coverage.json")
    factors = _read_json(reports / "adjustment-coverage.json")
    identity = _read_json(reports / "identity-coverage.json")
    dividends = _read_json(reports / "dividends-coverage.json")
    canonical = _read_json(reports / "canonical-schemes.json")
    normalized = _read_json(reports / "verified-actions.json")
    queue = _read_json(data / "processed" / "actions" / "pre2011_queue" / "coverage.json")
    # Counts are diagnostics, never proof of completeness. Existing collectors
    # explicitly do not certify full calendars, listing histories or exits.
    dates = calendar.get("sessions", {}).get("missing_dates", [])
    unresolved_dates = [r["date"] for r in dates if start <= r["date"] <= end
                        and r.get("classification") in ("unresolved", "confirmed_source_gap")]
    evidence["calendar"] = dict(complete=False, unresolved=len(unresolved_dates) if calendar else None,
                                 unresolved_dates=unresolved_dates,
                                 source=str(reports / "coverage-audit.json"),
                                 note="Includes unassessed absent weekends; 404 is not closure evidence.")
    evidence["corporate_actions"] = dict(complete=False, unresolved=None,
                                          pre2011=old_actions.get("complete", False),
                                          factors=factors.get("pre2011", {}),
                                          normalized=normalized,
                                          discontinuities_to_classify=queue.get("source_discontinuities"),
                                          reason="Split-only factors cannot certify rights, schemes or compound actions.")
    evidence["schemes"] = dict(complete=False, unresolved=canonical.get("unresolved_events"), report=canonical)
    evidence["identity"] = dict(complete=False, unresolved=identity.get("unresolved_total"), report=identity)
    evidence["terminal_proceeds"] = dict(complete=False, unresolved=None,
                                          note="Last-price marks on missing listings are not verified exits.")
    evidence["dividends"] = dict(complete=False, unresolved=None, events=dividends.get("events", {}),
                                  excluded=dividends.get("excluded_universes", {}))
    evidence["execution"] = dict(complete=False,
                                  note="Research engines have explicit limitations; no unified full-history corporate-action execution is certified.")
    evidence["nifty_membership"] = {}
    result = evaluate_evidence(evidence, start, end, universe)
    target = out or reports / "full-history-readiness.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=data_root())
    parser.add_argument("--start", default="2000-01-03")
    parser.add_argument("--end", default="2026-11-06")
    parser.add_argument("--universe", choices=("research", "nifty50", "nifty500"), default="research")
    args = parser.parse_args()
    report = build_readiness(args.data, args.start, args.end, args.universe)
    print(json.dumps({k: report[k] for k in ("ready_for_backtesting", "requested_start", "requested_end", "blocker_codes")}, indent=2))
    return 0 if report["ready_for_backtesting"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
