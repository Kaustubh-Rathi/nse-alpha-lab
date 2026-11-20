"""Bounded-memory, deliberately incomplete full-history research diagnostic.

Only one year's SQL result, one day's bars, and 64 sessions of scalar adjusted
closes are retained. Raw bars come from the read-only catalog's ``bars`` view,
NOT its all-time symbol-excluding cash-equity view. The research universe is EQ
with an INE ISIN or no ISIN *on that date*. Missing prices are unknown, not last
prices, cash exits, or zeroes. This module does not certify total returns.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter, defaultdict, deque
from contextlib import ExitStack
import csv
from datetime import date, datetime, timezone
import hashlib
from itertools import groupby
import json
import math
from pathlib import Path
from nse_pipeline.paths import data_root
from nse_pipeline.build.portable import open_catalog
import time
from urllib.parse import urlparse

from nse_pipeline.audit.readiness import build_readiness
from nse_pipeline.backtest.engine import (
    CostConfig,
    KeyStabilization,
    _root_for_symbol,
    build_key_stabilization,
)
from nse_pipeline.identity.lineage import (
    EDGE_SOURCE_SYMBOLCHANGE,
    load_merged_identity_edges,
)

#: A handover whose documented share-count change is not carried by the
#: applied action audit may not carry a holding across.
REFUSAL_RATIO_NOT_APPLIED = "handover_share_count_change_not_carried_by_the_applied_action_audit"


class HistoryReadinessError(RuntimeError):
    """Strict runs refuse incomplete evidence before reading trading inputs."""

    def __init__(self, readiness: dict):
        self.readiness = readiness
        self.blocker_codes = readiness.get("blocker_codes", [])
        super().__init__("Full-history backtest blocked: " + ", ".join(self.blocker_codes))


def _date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _positive(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def _jsonl(path):
    if path.exists():
        with path.open(encoding="utf-8-sig") as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


def _fingerprint(path: Path) -> dict:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path.resolve()), "bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "sha256": digest.hexdigest()}


def _quote(path: Path) -> str:
    return "'" + str(path.resolve()).replace("\\", "/").replace("'", "''") + "'"


def _days(db, adjusted: Path, start: date, end: date):
    """Year-bounded SQL joins; fetch batches, never fetchdf/all-history rows.

    Duplicate joins intentionally survive until the per-day uniqueness check;
    they must not be silently deduplicated into a fictitious executable price.
    """
    for year in range(start.year, end.year + 1):
        lo, hi = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
        query = f"""
            WITH raw AS (
                SELECT date,symbol,series,isin,open,close,previous_close,
                       volume,valid_ohlcv FROM bars
                WHERE date BETWEEN ? AND ? AND series='EQ'
                  AND (isin LIKE 'INE%' OR isin IS NULL OR trim(isin)='')
            ), adjusted AS (
                SELECT date,symbol,series,adj_close
                FROM read_parquet({_quote(adjusted)}) WHERE date BETWEEN ? AND ?
            )
            SELECT r.*, a.adj_close FROM raw r LEFT JOIN adjusted a
            USING(date,symbol,series) ORDER BY r.date,r.symbol,r.isin
        """
        cursor = db.cursor()
        try:
            cursor.execute(query, [lo, hi, lo, hi])

            def rows():
                while batch := cursor.fetchmany(8192):
                    yield from batch

            for on, group in groupby(rows(), key=lambda row: row[0]):
                yield on, list(group)
        finally:
            cursor.close()


def _risk_inputs(data: Path, inputs: set[Path]):
    """Project queues onto date/symbol/id only; no price-derived terms."""
    risks = []
    canonical = data / "processed/ledger/canonical-events.jsonl"
    queue = data / "processed/schemes/verified3/ranked-scheme-days.json"
    if canonical.exists():
        inputs.add(canonical)
        schemes = _jsonl(canonical)
    elif queue.exists():
        inputs.add(queue)
        schemes = json.loads(queue.read_text(encoding="utf-8-sig")).get("rows", [])
    else:
        schemes = []
    for row in schemes:
        dates = row.get("dates") or {}
        on = _date(row.get("ex_date") or dates.get("ex_date"))
        if row.get("symbol"):
            risks.append({"date": on, "symbol": row["symbol"], "kind": "scheme",
                          "id": row.get("event_id", f"scheme:{row['symbol']}:{on}"),
                          "reason": "scheme_execution_not_supported_no_terms_inferred"})
    candidates = [data / "processed/actions/pre2011_queue/queue-ranked-all.jsonl",
                  data / "processed/older_actions/unresolved-price-discontinuities-2000-2004.jsonl"]
    for path in candidates:
        if not path.exists():
            continue
        inputs.add(path)
        for row in _jsonl(path):
            on = _date(row.get("date"))
            if on and row.get("symbol"):
                risks.append({"date": on, "symbol": row["symbol"],
                              "kind": "pre2011_discontinuity",
                              "id": f"discontinuity:{row['symbol']}:{on}",
                              "reason": "unresolved_price_move_not_action_evidence"})
        break  # full ranked queue OR original queue, never just the top 100
    return risks


class _Identity:
    """Only dated, proven identity continuity; otherwise flag, don't guess.

    The official symbol-change rows (``identity_edges.jsonl``) and the
    corporate-actions API edges (``api_identity_edges.jsonl``) are loaded
    together and every edge keeps its own provenance (``edge_source``). The
    official rows keep exactly the preconditions they always had. An
    API-derived edge may only remap a key when it cleared the identity
    layer's fail-closed gate: a dated step with one unambiguous predecessor
    and successor, no symbol reuse, no branch or merge in the merged graph,
    an ordered bars handover, and a share count that is provably continuous
    across the handover.

    A refused edge is never applied and never silently dropped: it is
    recorded as an identity risk on the predecessor's side, so the holding
    that crosses the handover stays unresolved and its equity stays unknown.

    The key stabilizer is a separate, bar-proven layer: a symbol whose own
    bars prove a single security (null-ISIN bars, then exactly one ISIN, a
    contiguous handover, no reuse and no rename boundary) keeps one ISIN key
    for its whole life. It never merges two securities: every symbol the
    bars do not prove keeps the key it has today.
    """

    def __init__(self, data, inputs, risks, stabilization=None, db=None):
        self.pred, self.succ = defaultdict(list), defaultdict(list)
        self.merged = None
        self.refused: list[dict] = []
        self.stabilization: KeyStabilization = (
            stabilization if stabilization is not None
            else build_key_stabilization(Path(data) / "market.duckdb", data,
                                         connection=db))
        self.applied_splits = _applied_split_multipliers(data)
        for name in ("identity_edges.jsonl", "api_identity_edges.jsonl"):
            path = Path(data) / "processed/identity" / name
            if path.exists():
                inputs.add(path)
        merged = load_merged_identity_edges(Path(data))
        self.merged = merged
        self.refused = merged["refused"]
        for edge in merged["edges"]:
            if edge.get("edge_source") == EDGE_SOURCE_SYMBOLCHANGE:
                self._official(edge, risks)
                continue
            if not edge.get("remap_admissible"):
                self._refused_api(edge, risks)
                continue
            if not self._handover_ratio_is_applied(edge):
                self._refused_api(edge, risks, codes=[REFUSAL_RATIO_NOT_APPLIED])
                continue
            self._api(edge)

    def _handover_ratio_is_applied(self, edge) -> bool:
        """A documented handover ratio must be carried by the applied audit.

        The identity layer may say that two symbols are one security; it
        never multiplies shares. The share count changes only where the
        applied action audit carries them, so a handover whose ratio is
        documented in the official sources may carry a holding across only
        when that change is actually applied to the successor.
        """
        multiplier = edge.get("share_count_multiplier")
        if multiplier is None:
            return True
        applied = self.applied_splits.get((edge.get("new_symbol"), "EQ",
                                           edge.get("remap_effective_date")))
        return (applied is not None
                and math.isclose(float(applied), float(multiplier), rel_tol=1e-9))

    def _official(self, edge, risks):
        """The official symbol-change rows: unchanged, dated, no-ISIN only."""
        on = _date(edge.get("effective_date"))
        old, new = edge.get("old_symbol"), edge.get("new_symbol")
        safe = (on and old and new
                and edge.get("verification") == "official_symbolchange_row"
                and not edge.get("isin_old") and not edge.get("isin_new")
                and not edge.get("bars_order_violation")
                and not edge.get("isin_old_ambiguous") and not edge.get("isin_new_ambiguous")
                and not any((edge.get("reuse_flags") or {}).values()))
        if safe:
            self.pred[new].append((on, old))
            self.succ[old].append((on, new))
        else:
            for symbol in {old, new} - {None}:
                risks.append({"date": on, "symbol": symbol, "kind": "identity",
                              "id": f"identity:{old}:{new}:{on}:{symbol}",
                              "reason": "rename_or_isin_binding_not_proven_no_alias_applied"})

    def _api(self, edge):
        """An API-derived edge that cleared the fail-closed gate."""
        on = _date(edge.get("remap_effective_date") or edge.get("effective_date"))
        old, new = edge.get("old_symbol"), edge.get("new_symbol")
        if on and old and new:
            self.pred[new].append((on, old))
            self.succ[old].append((on, new))

    def _refused_api(self, edge, risks, codes=None):
        """A refused API edge: no alias, and the gap is recorded, not hidden.

        The party whose entitlement is unknown is the holder of the
        predecessor: the successor's own holders are unaffected. The
        holding therefore stays unpriceable and its equity stays unknown.
        """
        codes = list(codes or edge.get("refusal_codes") or [])
        reason = codes[0] if codes else "api_edge_refused_no_alias_applied"
        on = _date(edge.get("remap_effective_date") or edge.get("effective_date"))
        old, new = edge.get("old_symbol"), edge.get("new_symbol")
        for symbol in {old} - {None}:
            risks.append({"date": on, "symbol": symbol, "kind": "identity",
                          "id": f"api_identity:{old}:{new}:{on}:{symbol}",
                          "reason": reason,
                          "detail": " ".join(edge.get("refusal_reasons") or []),
                          "edge_source": edge.get("edge_source")})

    def key(self, symbol, isin, on):
        """The security key of one bar.

        A symbol whose own bars prove a single security keeps one key for
        its whole life: the ISIN key, on the bars that predate the ISIN as
        well. Without that proof an ISIN-keyed row already carries its own
        continuity, and a row without an ISIN falls back to the SYMBOL key of
        the earliest ancestor that the dated, unambiguous identity steps
        reach.
        """
        stabilized = self.stabilization.key_for(symbol)
        if stabilized:
            return stabilized
        if isin and str(isin).strip():
            return "ISIN:" + str(isin).strip()
        return "SYMBOL:" + _root_for_symbol(symbol, on, self.pred, self.succ)


def _applied_split_multipliers(data) -> dict:
    """Applied split/bonus multipliers per ``(symbol, series, ex_date)``.

    Read-only. The applied action audit is the only source of share-count
    changes in this runner: the identity layer may decide that two symbols
    are one security, but it never multiplies shares. A malformed audit
    raises, exactly as it does in :func:`_events`.
    """
    path = Path(data) / "processed/adjusted/action_audit.parquet"
    if not path.exists():
        return {}
    import duckdb

    quoted = str(path.resolve()).replace("\\", "/").replace("'", "''")
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT symbol, series, CAST(ex_date AS DATE), share_multiplier "
            f"FROM read_parquet('{quoted}') WHERE decision = 'applied'").fetchall()
    finally:
        con.close()
    return {(symbol, series, on): multiplier
            for symbol, series, on, multiplier in rows}


def _events(db, data, inputs, risks, start, end):
    splits, dividends = [], []
    audit = data / "processed/adjusted/action_audit.parquet"
    if audit.exists():
        inputs.add(audit)
        columns = {r[0] for r in db.execute(f"DESCRIBE SELECT * FROM read_parquet({_quote(audit)})").fetchall()}
        if not {"ex_date", "symbol", "series", "share_multiplier", "decision"} <= columns:
            raise ValueError("Split audit lacks the fields required for raw-share accounting")
        if {"ex_date", "symbol", "series", "share_multiplier", "decision"} <= columns:
            rows = db.execute(f"SELECT symbol,series,CAST(ex_date AS DATE),share_multiplier,decision "
                              f"FROM read_parquet({_quote(audit)}) WHERE CAST(ex_date AS DATE) BETWEEN ? AND ?",
                              [start, end]).fetchall()
            grouped = defaultdict(list)
            for symbol, series, on, multiplier, decision in rows:
                if decision == "applied":
                    grouped[(symbol, series, on)].append(multiplier)
                else:
                    risks.append({"date": on, "symbol": symbol, "kind": "corporate_action",
                                  "id": f"unapplied:{symbol}:{series}:{on}",
                                  "reason": "action_not_applied_by_verified_split_pipeline"})
            for (symbol, series, on), values in grouped.items():
                # Even identical duplicate action rows could be two different
                # events. Do not multiply twice or silently collapse them.
                if len(values) == 1 and _positive(values[0]):
                    splits.append({"date": on, "symbol": symbol, "series": series,
                                   "multiplier": values[0]})
                else:
                    risks.append({"date": on, "symbol": symbol, "kind": "corporate_action",
                                  "id": f"ambiguous_split:{symbol}:{series}:{on}",
                                  "reason": "ambiguous_or_invalid_applied_split_rows"})
    path = data / "processed/actions_dividends/dividend_events.jsonl"
    if path.exists():
        inputs.add(path)
    for row in _jsonl(path):
        on, symbol = _date(row.get("ex_date")), row.get("symbol")
        if not symbol:
            continue
        if on is None:
            risks.append({"date": None, "symbol": symbol, "kind": "dividend",
                          "id": f"undated_dividend:{symbol}:{row.get('series')}",
                          "reason": "undated_dividend_no_cash_or_date_inferred"})
            continue
        if not start <= on <= end:
            continue
        host = urlparse(str(row.get("source_url") or "")).hostname or ""
        safe = (row.get("included_in_factor") is True
                and row.get("amount_verified") is True and row.get("ex_date_verified") is True
                and row.get("party_binding_verified") is True and row.get("verified_by")
                and (host == "nseindia.com" or host.endswith(".nseindia.com"))
                and _positive(row.get("dividend_per_share")) and row.get("series"))
        dividends.append({"date": on, "symbol": symbol, "series": row.get("series"),
                          "isin": row.get("isin"), "amount": row.get("dividend_per_share"),
                          "safe": bool(safe)})
        if not safe:
            risks.append({"date": on, "symbol": symbol, "kind": "dividend",
                          "id": f"dividend:{symbol}:{row.get('series')}:{on}",
                          "reason": "raw_per_share_amount_date_or_party_binding_not_verified"})
    duplicate_dividends = Counter((e["date"], e["symbol"], e["series"]) for e in dividends)
    for event in dividends:
        if duplicate_dividends[(event["date"], event["symbol"], event["series"])] > 1:
            event["safe"] = False
            risks.append({"date": event["date"], "symbol": event["symbol"], "kind": "dividend",
                          "id": f"duplicate_dividend:{event['symbol']}:{event['series']}:{event['date']}",
                          "reason": "duplicate_or_compound_dividend_rows_require_verified_aggregation"})
    return splits, dividends


def run_history(data: Path, start="2000-01-03", end="2026-11-06", out=None,
                allow_incomplete=False, initial_cash=1e7, top_n=20,
                use_key_stabilization=True) -> dict:
    """Run monthly 63-session momentum, or raise :class:`HistoryReadinessError`.

    130 in-window valid observations are required; no implicit pre-start warmup.
    Signals use split-adjusted closes, raw close >=5 and volume >=5000. Empty
    month-end targets still schedule liquidation at the next observed session.
    ``allow_incomplete=True`` is diagnostic consent, never a completeness waiver.
    ``use_key_stabilization=False`` reproduces the pre-stabilization keys
    (the diagnostic before/after switch); it never relaxes a safety rule.
    Outputs are buffered CSVs and JSON evidence, not in-memory history frames.
    """
    import duckdb

    began = time.monotonic()
    data = Path(data)
    first, last = _date(start), _date(end)
    if first is None or last is None or last < first:
        raise ValueError("Expected an ordered ISO start/end date window")
    if not _positive(initial_cash) or not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 1:
        raise ValueError("initial_cash must be positive and top_n a positive integer")
    out = Path(out) if out is not None else data / "processed/backtest/full_history_research"
    out.mkdir(parents=True, exist_ok=True)
    # Redirect the gate's artifact into this runner's owned output directory.
    # Calling the default writer would overwrite another agent's report.
    readiness = build_readiness(data, str(first), str(last), universe="research", out=out / "readiness.json")
    if not readiness.get("ready_for_backtesting") and not allow_incomplete:
        raise HistoryReadinessError(readiness)
    adjusted = data / "processed/adjusted/adjusted_bars.parquet"
    if not adjusted.exists():
        raise FileNotFoundError("Split-adjusted signal input is required; no raw fallback: " + str(adjusted))
    action_audit = data / "processed/adjusted/action_audit.parquet"
    if not action_audit.exists():
        raise FileNotFoundError("Raw-share accounting requires the applied split audit: " + str(action_audit))
    inputs = {data / "market.duckdb", adjusted}
    inputs.update(p for name in (
        "coverage-audit.json", "pre2011-actions-coverage.json", "adjustment-coverage.json",
        "identity-coverage.json", "dividends-coverage.json", "canonical-schemes.json",
    ) if (p := data / "reports" / name).exists())
    inputs.update(p for p in (data / "processed/prices").glob("year=*/bars.parquet")
                  if first.year <= int(p.parent.name.split("=")[-1]) <= last.year)
    risks = _risk_inputs(data, inputs)
    costs = CostConfig()
    counts, audit_counts, row_counts = Counter(), Counter(), Counter()
    positions, last_symbol_key = {}, {}
    history = deque(maxlen=64)
    cash = float(initial_cash)
    pending = None  # (signal_date, targets), including an explicitly empty list
    last_equity, peak, max_dd = None, initial_cash, 0.0
    invalid_equity_days, sessions_seen, streamed_rows, last_day = 0, 0, 0, None
    gross_dividends = total_costs = 0.0
    seen_impacts = set()

    with ExitStack() as stack:
        db = stack.enter_context(open_catalog(data,
                                               config={"memory_limit": "512MB", "threads": 2}))
        # The identity layer reuses the open read-only connection: the
        # database file may not be opened twice with a different config.
        identity = _Identity(data, inputs, risks, db=db,
                             stabilization=None if use_key_stabilization
                             else KeyStabilization())
        writers = {}
        schemas = {
            "trades": ["date", "signal_date", "key", "symbol", "side", "fill_price", "shares", "notional", "cost", "cash_after"],
            "ledger": ["date", "event_date", "key", "symbol", "event", "shares_before", "shares_after", "multiplier", "cash_amount"],
            "signals": ["date", "execution_date", "key", "symbol", "rank", "momentum", "raw_close", "adj_close", "observations", "n_targets"],
            "equity_curve": ["date", "cash", "positions_value", "known_positions_value", "equity", "n_positions", "unpriced_positions"],
            "impact_audit": ["date", "event_date", "event_id", "scope", "kind", "key", "symbol", "shares", "reference_value", "reason"],
            "positions": ["key", "symbol", "series", "shares", "last_observed_date", "last_observed_close", "terminal_price_known", "blocked"],
        }
        for name, fields in schemas.items():
            stream = stack.enter_context((out / f"{name}.csv").open("w", newline="", encoding="utf-8", buffering=65536))
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writers[name] = writer

        def write(name, **row):
            writers[name].writerow(row)
            row_counts[name] += 1

        def impact(on, key, symbol, scope, kind, reason, event_id=None, event_date=None, shares=None, reference_value=None):
            token = (event_id or reason, key, scope)
            # Per-day data/order failures can number millions: stream them
            # directly, rather than retaining an all-history set of row IDs.
            if scope not in {"data", "order"}:
                if token in seen_impacts:
                    return
                seen_impacts.add(token)
            audit_counts[f"{scope}:{kind}"] += 1
            write("impact_audit", date=on, event_date=event_date, event_id=event_id,
                  scope=scope, kind=kind, key=key, symbol=symbol, shares=shares,
                  reference_value=reference_value, reason=reason)

        splits, dividends = _events(db, data, inputs, risks, first, last)
        sessions = [r[0] for r in db.execute("SELECT DISTINCT date FROM bars WHERE date BETWEEN ? AND ? ORDER BY date", [first, last]).fetchall()]
        by_month = {}
        for on in sessions:
            by_month[(on.year, on.month)] = on
        month_ends = set(by_month.values())
        next_session = dict(zip(sessions, sessions[1:]))
        session_index = {on: i for i, on in enumerate(sessions)}
        action_days, dividend_days = defaultdict(list), defaultdict(list)
        for records, buckets in ((splits, action_days), (dividends, dividend_days)):
            for record in records:
                i = bisect_left(sessions, record["date"])
                if i < len(sessions):
                    buckets[sessions[i]].append(record)
        risk_by_symbol = defaultdict(list)
        for risk in risks:
            risk_by_symbol[risk["symbol"]].append(risk)
        risk_counts = Counter(r["kind"] for r in risks)

        def exposure(on, key, symbol, scope, anchor=None, position=None):
            for risk in risk_by_symbol.get(symbol, []):
                event_date = risk["date"]
                if event_date is not None and event_date > on:
                    continue
                if (scope == "signal" and risk["kind"] in {"pre2011_discontinuity", "dividend"}
                        and anchor is not None and event_date is not None and event_date <= anchor):
                    continue
                impact(on, key, symbol, scope, risk["kind"], risk["reason"],
                       event_id=risk["id"], event_date=event_date,
                       shares=position.get("shares") if position else None,
                       reference_value=(position["shares"] * position["reference_close"]
                                        if position and _positive(position.get("reference_close")) else None))
            if key.startswith("SYMBOL:"):
                impact(on, key, symbol, scope, "identity", "symbol_fallback_identity_not_verified")
            elif identity.stabilization.key_for(symbol):
                impact(on, key, symbol, scope, "identity",
                       "symbol_key_stabilized_to_the_single_isin_proved_by_its_own_bars")

        def tradable(row):
            # Do not inspect today's close or full-day volume/valid_ohlcv at
            # the open. Those are EOD facts. Use only auction/preceding data.
            return row is not None and _positive(row[4]) and _positive(row[6]) and 0.8 < row[4] / row[6] < 1.2

        def trade(on, signal, key, row, side, shares):
            nonlocal cash, total_costs
            notional = shares * row[4]
            cost = costs.cost_for(notional, side)
            cash += (notional - cost) if side == "sell" else -(notional + cost)
            total_costs += cost
            write("trades", date=on, signal_date=signal, key=key, symbol=row[1], side=side,
                  fill_price=row[4], shares=shares, notional=notional, cost=cost, cash_after=cash)

        # Include observed sessions without EQ candidates: an empty universe
        # is still a session and can be a month-end liquidation signal.
        days = iter(_days(db, adjusted, first, last))
        item = next(days, None)
        for on in sessions:
            rows = []
            if item is not None and item[0] == on:
                rows = item[1]
                item = next(days, None)
            streamed_rows += len(rows)
            sessions_seen += 1
            last_day = on
            grouped = defaultdict(list)
            for row in rows:
                key = identity.key(row[1], row[3], on)
                grouped[key].append(row)
            day = {}
            for key, candidates in grouped.items():
                if len(candidates) != 1:
                    impact(on, key, candidates[0][1], "data", "identity", "duplicate_security_session_quarantined", event_id=f"duplicate:{on}:{key}")
                    continue
                row = candidates[0]
                day[key] = row
                previous = last_symbol_key.get(row[1])
                if previous and previous != key:
                    impact(on, key, row[1], "data", "identity", "observed_key_change_no_automatic_holding_transfer", event_id=f"keychange:{row[1]}:{previous}:{key}")
                    if previous in positions:
                        positions[previous]["blocked"] = True
                        impact(on, previous, row[1], "holding", "identity", "held_identity_change_requires_verified_binding")
                last_symbol_key[row[1]] = key
                if key in positions:
                    positions[key]["symbol"] = row[1]

            for event in action_days.get(on, []):
                for key, p in positions.items():
                    row = day.get(key)
                    symbol = row[1] if row else p["symbol"]
                    if symbol != event["symbol"] or p["series"] != event["series"]:
                        continue
                    before, multiplier = p["shares"], event["multiplier"]
                    after = before * multiplier
                    if not math.isclose(after, round(after), abs_tol=1e-8):
                        p["blocked"] = True
                        impact(on, key, symbol, "holding", "corporate_action", "fractional_split_entitlement_unsupported", event_id=f"fraction:{symbol}:{event['date']}")
                        continue
                    p["shares"] = round(after)
                    if _positive(p.get("reference_close")):
                        p["reference_close"] /= multiplier
                    write("ledger", date=on, event_date=event["date"], key=key, symbol=symbol,
                          event="split_raw_shares", shares_before=before, shares_after=p["shares"],
                          multiplier=multiplier, cash_amount=0)
            for event in dividend_days.get(on, []):
                for key, p in positions.items():
                    if p["symbol"] != event["symbol"] or p["series"] != event["series"]:
                        continue
                    compound = any(e["symbol"] == p["symbol"] and e["series"] == p["series"] for e in action_days.get(on, []))
                    bound = not event["isin"] or key == "ISIN:" + str(event["isin"])
                    if not event["safe"] or not bound or compound or p.get("blocked"):
                        impact(on, key, p["symbol"], "holding", "dividend", "dividend_not_credited_unsafe_or_compound_binding", event_id=f"dividend_cash:{p['symbol']}:{event['date']}")
                        continue
                    amount = p["shares"] * event["amount"]
                    cash += amount
                    gross_dividends += amount
                    write("ledger", date=on, event_date=event["date"], key=key, symbol=p["symbol"], event="gross_dividend_cash_ex_date", shares_before=p["shares"], shares_after=p["shares"], cash_amount=amount)
            for key, p in positions.items():
                exposure(on, key, p["symbol"], "holding", position=p)

            if pending is not None:
                signal_date, targets = pending
                pending = None
                # All opening weights use today's raw opens only. An unknown
                # held opening mark prevents buys; it never releases cash.
                opening_known = all(_positive(day.get(k, (None,) * 5)[4]) and not p.get("blocked") for k, p in positions.items())
                opening_equity = cash + sum(p["shares"] * day[k][4] for k, p in positions.items()) if opening_known else None
                per = opening_equity / len(targets) if opening_equity is not None and targets else 0.0
                for key, p in list(positions.items()):
                    row = day.get(key)
                    wanted = math.floor(per / row[4]) if key in targets and per and row and _positive(row[4]) else (p["shares"] if key in targets else 0)
                    sell = p["shares"] - wanted
                    if sell <= 0:
                        continue
                    if p.get("blocked") or not tradable(row):
                        impact(on, key, p["symbol"], "order", "execution", "sell_not_executable_no_fictitious_exit", event_id=f"sell:{on}:{key}")
                        continue
                    trade(on, signal_date, key, row, "sell", sell)
                    p["shares"] -= sell
                    if not p["shares"]:
                        del positions[key]
                for key in targets:
                    row = day.get(key)
                    if not opening_known or not tradable(row) or positions.get(key, {}).get("blocked"):
                        impact(on, key, row[1] if row else key, "order", "execution", "buy_missing_open_or_unknown_held_opening_value", event_id=f"buy:{on}:{key}")
                        continue
                    held = positions.get(key, {}).get("shares", 0)
                    budget = min(cash, max(0.0, per - held * row[4]))
                    # Monotone integer search includes fees in both the target
                    # budget and cash; no leverage and no O(millions) decrement.
                    lo, hi = 0, max(0, math.floor(budget / row[4]))
                    while lo < hi:
                        mid = (lo + hi + 1) // 2
                        n = mid * row[4]
                        if n + costs.cost_for(n, "buy") <= budget:
                            lo = mid
                        else:
                            hi = mid - 1
                    if lo:
                        trade(on, signal_date, key, row, "buy", lo)
                        if key in positions:
                            positions[key]["shares"] += lo
                        else:
                            positions[key] = {"symbol": row[1], "series": row[2], "shares": lo, "last_close": None, "reference_close": row[4], "last_date": None, "blocked": False}
                        exposure(on, key, row[1], "holding", position=positions[key])
                if cash < -1e-6:
                    raise ArithmeticError("Cash conservation failure: negative cash")

            # Only here do today's closes, volume and validity become visible.
            closes = {}
            for key, row in day.items():
                if row[8] and _positive(row[5]) and _positive(row[7]):
                    counts[key] += 1
                    if _positive(row[9]):
                        closes[key] = row[9]
                    else:
                        impact(on, key, row[1], "data", "signal_price", "missing_split_adjusted_close_no_raw_fallback", event_id=f"adjusted:{on}:{key}")
            history.append((on, closes))
            known_value, missing = 0.0, 0
            for key, p in positions.items():
                row = day.get(key)
                valid_mark = row is not None and row[8] and _positive(row[5]) and not p.get("blocked")
                if valid_mark:
                    p["last_close"], p["last_date"] = row[5], on
                    p["reference_close"] = row[5]
                    known_value += p["shares"] * row[5]
                else:
                    missing += 1
                    impact(on, key, p["symbol"], "holding", "terminal_price", "no_current_valid_close_equity_unknown_not_forward_filled", event_id=f"unpriced:{key}", shares=p["shares"])
            eq = cash + known_value if not missing else None
            if eq is None:
                invalid_equity_days += 1
            else:
                peak = max(peak, eq)
                max_dd = min(max_dd, eq / peak - 1)
            last_equity = eq
            write("equity_curve", date=on, cash=cash, positions_value=known_value if not missing else None,
                  known_positions_value=known_value, equity=eq, n_positions=len(positions), unpriced_positions=missing)

            if on in month_ends:
                ranked = []
                anchor_date, anchor = history[0] if len(history) == 64 else (None, {})
                for key, row in day.items():
                    if (counts[key] >= 130 and row[8] and _positive(row[5]) and row[5] >= 5
                            and _positive(row[7]) and row[7] >= 5000 and key in closes and key in anchor
                            and session_index[on] - session_index[anchor_date] == 63):
                        momentum = closes[key] / anchor[key] - 1
                        ranked.append((key, row, momentum))
                        exposure(on, key, row[1], "signal", anchor=anchor_date)
                ranked.sort(key=lambda r: (-r[2], r[0]))
                targets = [r[0] for r in ranked[:top_n]]
                if not targets:
                    write("signals", date=on, execution_date=next_session.get(on), n_targets=0)
                for rank, (key, row, momentum) in enumerate(ranked[:top_n], 1):
                    write("signals", date=on, execution_date=next_session.get(on), key=key, symbol=row[1], rank=rank,
                          momentum=momentum, raw_close=row[5], adj_close=row[9], observations=counts[key], n_targets=len(targets))
                if on in next_session:
                    pending = (on, targets)

        for key, p in positions.items():
            write("positions", key=key, symbol=p["symbol"], series=p["series"], shares=p["shares"],
                  last_observed_date=p["last_date"], last_observed_close=p["last_close"],
                  terminal_price_known=p["last_date"] == last_day and not p.get("blocked"), blocked=p.get("blocked", False))

    # Hash inputs with fixed-size buffers, including backing raw year partitions
    # (the DuckDB file may contain only views). Never treat a stat as a hash.
    provenance = [_fingerprint(path) for path in sorted(inputs) if path.exists()]
    all_marks_known = bool(sessions_seen) and invalid_equity_days == 0
    summary = {
        "schema_version": 1, "mode": "full_history_research_diagnostic",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ready_for_backtesting": False, "ready_for_alpha_claim": False,
        "allow_incomplete": bool(allow_incomplete), "start": str(first), "end": str(last),
        "readiness": readiness, "initial_cash": initial_cash, "top_n": top_n,
        "universe": "date-local EQ INE-or-missing-ISIN research universe; not official Nifty",
        "price_mode": "split_adjusted_signal_raw_open_execution_raw_share_accounting",
        "total_return": False, "gross_dividend_cash": gross_dividends,
        "signal_eligibility": {"raw_min_close": 5.0, "min_volume": 5000,
                               "valid_observations": 130, "lookback_exchange_sessions": 63},
        "stats": {"sessions": sessions_seen, "streamed_join_rows": streamed_rows,
                  "valid_observations": sum(counts.values()),
                  "final_cash": cash, "final_equity": last_equity, "n_positions": len(positions),
                  "total_costs": total_costs, "unpriced_equity_sessions": invalid_equity_days,
                  "diagnostic_total_return": last_equity / initial_cash - 1 if all_marks_known else None,
                  "diagnostic_max_drawdown": max_dd if all_marks_known else None},
        "impact_counts": dict(sorted(audit_counts.items())),
        "input_risk_counts": dict(sorted(risk_counts.items())),
        "output_rows": dict(row_counts), "costs": costs.__dict__,
        "provenance": {"inputs": provenance, "database_read_only": True,
                       "duckdb_memory_limit": "512MB", "duckdb_threads": 2,
                       "fetch_batch_rows": 8192, "signal_history_sessions": 64,
                       "elapsed_seconds": time.monotonic() - began},
        "limitations": [
            "Diagnostic only, never complete/unbiased/production-ready or total-return coverage.",
            "Fundamentals deferred; official historical Nifty membership is not used or claimed.",
            "No same-day close or full-day volume/validity used for open sizing; executable volume at the auction is unavailable.",
            "Open-limit screen is a stylized flat 20% of raw previous_close, not an historical circuit/auction simulation.",
            "130 valid in-window observations; 63 exact observed exchange sessions; no forward-filled signal anchors.",
            "Missing/ambiguous current holding marks leave equity unknown. Last observed prices are reference evidence only.",
            "Unknown schemes, rights, dividends, capital/fractional entitlements and identities are audited, not inferred or settled.",
            "Scheme and identity uncertainty is conservatively persistent after its event date; holding impacts are first-encounter records, not realized-loss estimates.",
            "Dividends require verified raw per-share amount, ex-date, official source and party binding; safe credits are gross immediate ex-date cash (not a payment-date model). Compound split/dividend dates are not credited.",
            "Stylized current constant fees and slippage, not a historical tax schedule; raw integer shares and buy-only stamp duty.",
            "No forced terminal liquidation or fictitious exit for delisted/suspended holdings.",
        ],
        "outputs": {name: str(out / f"{name}.csv") for name in schemas},
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (out / "README.md").write_text(
        "# Full-history diagnostic — NOT ready for backtesting\n\n"
        + "\n".join(f"- {item}" for item in summary["limitations"])
        + "\n\n## Unresolved input counts (not proof of coverage)\n\n```json\n"
        + json.dumps(summary["input_risk_counts"], indent=2)
        + "\n```\n\n## Affected holding/signal/order counts\n\n```json\n"
        + json.dumps(summary["impact_counts"], indent=2)
        + "\n```\n\nSee readiness.json for blockers and their source evidence; impact_audit.csv for affected keys and first encounters. No factor is inferred from a price discontinuity.\n",
        encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    """Command line entry point: ``python -m nse_pipeline.backtest.history``.

    Strict by default: while readiness blockers remain the run refuses and
    exits 2 without writing any trading output. ``--allow-incomplete`` is
    explicit diagnostic consent, never a completeness waiver.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Full-history raw-share research diagnostic (incomplete-data "
                    "diagnostic; never alpha, never a total-return series)")
    parser.add_argument("--data", type=Path, default=data_root())
    parser.add_argument("--start", default="2000-01-03")
    parser.add_argument("--end", default="2026-11-06")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="Explicit diagnostic consent while readiness blockers remain")
    parser.add_argument("--cash", type=float, default=10_000_000.0)
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--no-key-stabilization", action="store_true",
                        help="Reproduce the pre-stabilization keys (diagnostic "
                             "before/after switch; never a completeness waiver)")
    args = parser.parse_args(argv)

    try:
        summary = run_history(args.data, args.start, args.end, out=args.out,
                              allow_incomplete=args.allow_incomplete,
                              initial_cash=args.cash, top_n=args.top_n,
                              use_key_stabilization=not args.no_key_stabilization)
    except HistoryReadinessError as exc:
        print(json.dumps({
            "status": "refused",
            "reason": "readiness_blockers_remain",
            "blocker_codes": exc.blocker_codes,
            "ready_for_backtesting": False,
            "ready_for_alpha_claim": False,
            "note": ("Strict by default. Re-run with --allow-incomplete to produce an "
                     "incomplete-data diagnostic, which still never claims alpha."),
        }, indent=2))
        return 2
    print(json.dumps({
        "status": "ok",
        "mode": summary["mode"],
        "ready_for_backtesting": summary["ready_for_backtesting"],
        "ready_for_alpha_claim": summary["ready_for_alpha_claim"],
        "total_return": summary["total_return"],
        "allow_incomplete": summary["allow_incomplete"],
        "sessions": summary["stats"]["sessions"],
        "unpriced_equity_sessions": summary["stats"]["unpriced_equity_sessions"],
        "final_equity": summary["stats"]["final_equity"],
        "elapsed_seconds": round(summary["provenance"]["elapsed_seconds"], 1),
        "outputs": summary["outputs"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
