"""
Corporate actions before 2011 are inventoried, never price-adjusted, from the official NSE API and Google's public price archive; third-party SQLite rows stay separate. Dates are ex-dates, not announcement dates or point-in-time knowledge.
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import re
import sqlite3
import urllib.request
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from nse_pipeline.paths import data_root
from nse_pipeline.build.portable import open_catalog

COMMIT = "ef02b775ef41274b5388f6b506e73dd27380183b"
BASE = f"https://raw.githubusercontent.com/BennyThadikaran/eod2_utils/{COMMIT}/"
DB_BLOB = "561ad71be25d7a4e4fced4ba0d6b08987ff23883"
API = "https://www.nseindia.com/api/corporates-corporateActions"
IST = timezone(timedelta(hours=5, minutes=30))
START = date(2000, 1, 1)
END = date(2010, 12, 31)
CUTOFF = date(2026, 11, 7)
# This collector owns the raw tag inventory only. The applied, terms-verified
# layer and the readiness verdict live in these reports.
AUTHORITATIVE_READINESS_REPORT = "data/reports/full-history-readiness.json"
VERIFIED_ACTIONS_REPORT = "data/reports/verified-actions.json"
ADJUSTMENT_COVERAGE_REPORT = "data/reports/adjustment-coverage.json"
PRE2011_COVERAGE_REPORT = "data/reports/pre2011-actions-coverage.json"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json_report(path: Path) -> dict:
    """Read a JSON report if it is present and readable.

    An absent, unreadable or non-object file is an empty mapping: a missing
    artifact must never become a number.
    """
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def normalized_actions_summary(reports: Path | None) -> dict:
    """Summarise the normalized, terms-verified and applied pre-2011 layer.

    Every count is read from the current artifacts at write time; a missing
    artifact yields ``None`` instead of a stale or invented number. The
    tags-only inventory is not repeated here: it is not evidence that a term
    was verified or that a factor was applied.
    """
    verified = read_json_report(reports / "verified-actions.json") if reports else {}
    adjustment = read_json_report(reports / "adjustment-coverage.json") if reports else {}
    applied_block = adjustment.get("verified_actions")
    applied_block = applied_block if isinstance(applied_block, dict) else {}
    pre2011 = adjustment.get("pre2011")
    pre2011 = pre2011 if isinstance(pre2011, dict) else {}
    applied = pre2011.get("applied")
    if applied is None:
        applied = applied_block.get("actually_applied_actions")
    status = applied_block.get("application_status") or verified.get("application_status")
    return {
        "normalized_action_rows": verified.get("normalized_action_rows"),
        "terms_verified_rows": verified.get("verified_terms_rows"),
        "execution_ready_actions": verified.get("execution_ready_actions"),
        "actually_applied_actions": applied,
        "verified_multipliers": pre2011.get("verified_multipliers"),
        "split_or_bonus_tagged_rows": pre2011.get("split_or_bonus_tagged_rows"),
        "factor_application": {
            "applied_pre2011_actions": applied,
            "application_status": status,
            "scope": "split/bonus share multipliers only",
            "note": ("Split/bonus factors are applied for the pre-2011 layer. Dividends, "
                     "rights and schemes stay unadjusted audit records; readiness is not "
                     "completeness."),
        },
        "reports": {
            "normalized_actions": VERIFIED_ACTIONS_REPORT,
            "adjustment_coverage": ADJUSTMENT_COVERAGE_REPORT,
            "full_history_readiness": AUTHORITATIVE_READINESS_REPORT,
        },
    }


def api_url(year: int) -> str:
    if not 2000 <= year <= 2010:
        raise ValueError("This collector is restricted to 2000-2010")
    return f"{API}?index=equities&from_date=01-01-{year}&to_date=31-12-{year}"


def parse_date(value: str | None) -> date | None:
    if value is None or value.strip() in ("", "-"):
        return None
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unrecognized source date: {value!r}")


def validate_api(data: object, year: int) -> list[dict]:
    """Reject errors, HTML, wrong years and undocumented wrapped responses."""
    if not isinstance(data, list):
        raise ValueError("Expected an NSE list response")
    for row in data:
        if not isinstance(row, dict) or not {"symbol", "subject", "exDate"} <= row.keys():
            raise ValueError("Missing required NSE action fields")
        ex = parse_date(row["exDate"])
        if ex is not None and ex.year != year:
            raise ValueError(f"Out-of-window NSE row: {ex}")
    return data


def acquire(raw: Path, include_upstream: bool = False) -> None:
    """At most one request per uncached file. Stop on any error, no retries."""
    raw.mkdir(parents=True, exist_ok=True)
    manifest_path = raw / "acquisition.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    jobs = [(f"nse-api-{year}.json", api_url(year)) for year in range(2000, 2011)]
    jobs += [(f"nse-{name}.csv", f"https://nsearchives.nseindia.com/content/equities/{name}.csv")
             for name in ("delisted", "symbolchange")]
    if include_upstream:
        jobs += [("eod2-main.db", BASE + "src/actions/db/main.db")]
        jobs += [("upstream-" + name.replace("/", "-"), BASE + name) for name in
                 ("src/download_actions.py", "src/actions/to_sqlite.py",
                  "src/actions/db/create.sql", "src/actions/meta.json", "README.md", "LICENSE")]
    for name, url in jobs:
        path = raw / name
        cached = path.exists()
        record = {"url": url, "cached_at_run": cached}
        try:
            if cached:
                content = path.read_bytes()
                expected = manifest.get(name, {}).get("sha256")
                if expected and hashlib.sha256(content).hexdigest() != expected:
                    raise ValueError(f"Cached source hash changed: {name}")
            else:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "NSEHistoricalResearch/1.0 (personal historical-data research)",
                    "Accept": "application/json,text/csv,*/*",
                })
                with urllib.request.urlopen(req, timeout=30) as response:
                    content = response.read()
                    record.update(http_status=response.status,
                                  retrieved_at=datetime.now(timezone.utc).isoformat())
            if name.startswith("nse-api-"):
                validate_api(json.loads(content), int(name[8:12]))
            if name == "eod2-main.db":
                blob = b"blob " + str(len(content)).encode() + b"\0" + content
                if hashlib.sha1(blob).hexdigest() != DB_BLOB:
                    raise ValueError("Upstream SQLite does not match pinned Git blob")
                record["git_blob_verified"] = DB_BLOB
            if not cached:
                path.write_bytes(content)
            record.update(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
            manifest[name] = {**manifest.get(name, {}), **record}
            manifest[name].pop("error", None)
            write_json(manifest_path, manifest)
            print(f"Ready {name}: {len(content)} bytes", flush=True)
        except Exception as error:
            manifest[name] = {**manifest.get(name, {}), **record, "error": repr(error)}
            write_json(manifest_path, manifest)
            raise  # Respect 401/403/429 and stop, rather than change identity.


def action_tags(subject: str) -> list[str]:
    """Search labels only: not parsed adjustment factors or executable actions."""
    terms = {"bonus": r"\bbonus\b", "split": r"\bsplit\b|\bsub.?division\b",
             "rights": r"\brights?\b", "dividend": r"\bdiv(?:idend)?\b",
             "scheme": r"\b(?:scheme|merger|demerger|amalgamation)\b"}
    return [tag for tag, pattern in terms.items() if re.search(pattern, subject, re.I)] or ["other"]


def sqlite_rows(path: Path) -> tuple[list[dict], dict]:
    """Read pinned upstream data in read-only mode, preserving timestamp evidence."""
    rows = []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Invalid upstream SQLite")
        for row in con.execute("SELECT a.rowid AS source_rowid, s.name AS symbol, a.hash, "
                               "a.subject, a.exDate, a.recDate FROM Actions a "
                               "JOIN Stocks s ON s.id=a.stock_id ORDER BY a.rowid"):
            ex = datetime.fromtimestamp(row["exDate"], IST).date() if row["exDate"] is not None else None
            rows.append({**dict(row), "ex_date": ex.isoformat() if ex else None,
                         "date_interpretation": "Asia/Kolkata assumed from naive timestamp writer",
                         "verification": "third_party_nse_derived_not_independently_verified"})
    dates = [r["ex_date"] for r in rows if r["ex_date"]]
    return rows, {"rows": len(rows), "earliest": min(dates, default=None),
                  "latest": max(dates, default=None),
                  "timestamp_day_remainders": dict(collections.Counter(
                      str(r["exDate"] % 86400) for r in rows if r["exDate"] is not None))}


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def csv_rows(path: Path) -> list[dict]:
    content = path.read_bytes()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252")  # NSE's legacy CSV includes nonbreaking spaces.
    return list(csv.DictReader(text.splitlines()))


LEGAL_WORDS = {"LTD", "LIMITED", "PVT", "PRIVATE", "CO", "COMPANY", "THE", "AND", "OF"}


def company_key(name: str) -> str:
    """Conservative name key for an exact-name lead, never an identity proof."""
    words = re.sub(r"[^A-Z0-9]+", " ", (name or "").upper()).split()
    return " ".join(word for word in words if word not in LEGAL_WORDS)


def canonical_row(row: dict) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False)


def probe_checks(raw: Path) -> dict:
    """Compare saved official quarter/rerun probes with the annual responses.

    This tests request-level truncation only. Equality does not prove that the
    exchange's historical database is complete.
    """
    quarters, full_year = {}, {}
    for year in range(2000, 2005):
        path = raw / f"probe-{year}-q1.json"
        annual_path = raw / f"nse-api-{year}.json"
        if not (path.exists() and annual_path.exists()):
            continue
        annual = validate_api(json.loads(annual_path.read_bytes()), year)
        probe = validate_api(json.loads(path.read_bytes()), year)
        annual_q1 = [row for row in annual
                     if (parse_date(row["exDate"]) or date(1, 1, 1)).month <= 3]
        quarters[str(year)] = {
            "probe_url": api_url(year).replace(f"31-12-{year}", f"31-03-{year}"),
            "probe_rows": len(probe), "annual_q1_rows": len(annual_q1),
            "equal_multisets": collections.Counter(map(canonical_row, probe))
            == collections.Counter(map(canonical_row, annual_q1)),
            "warning": "One quarter cannot prove every annual response complete",
        }
    rerun_path = raw / "probe-2004-full-rerun.json"
    annual_path = raw / "nse-api-2004.json"
    if rerun_path.exists() and annual_path.exists():
        annual = validate_api(json.loads(annual_path.read_bytes()), 2004)
        rerun = validate_api(json.loads(rerun_path.read_bytes()), 2004)
        full_year["2004"] = {
            "probe_rows": len(rerun), "annual_rows": len(annual),
            "equal_multisets": collections.Counter(map(canonical_row, rerun))
            == collections.Counter(map(canonical_row, annual)),
            "warning": "Byte hashes can differ from row order; multiset equality is the check",
        }
    symbol_probes = {}
    for symbol in ("GFL", "ANIKSHIP"):
        path = raw / f"probe-symbol-{symbol}.json"
        if path.exists():
            payload = json.loads(path.read_bytes())
            symbol_probes[symbol] = {
                "rows": len(payload) if isinstance(payload, list) else None,
                "interpretation": "Zero exact symbol-filter rows does not resolve company identity",
            }
    checks = list(quarters.values()) + list(full_year.values())
    return {
        "quarter_slices": quarters,
        "full_year_rerun": full_year,
        "symbol_filter_probes": symbol_probes,
        "all_tested_slices_equal": all(row["equal_multisets"] for row in checks) if checks else None,
        "interpretation": ("No tested slice shows request-level truncation. The sparse "
                           "2000-2004 counts remain a property of the public API's historical "
                           "database and are not evidence either way about completeness."),
    }


def build_delisted(delisted_path: Path, official: list[dict],
                   change_rows: list[dict]) -> list[dict]:
    """Reference-row diagnostics: exact symbol, same-name lead, symbol change, price presence."""
    by_company: dict[str, list[dict]] = collections.defaultdict(list)
    for row in official:
        key = company_key(row.get("comp", ""))
        if key:
            by_company[key].append(row)
    changes = []
    for row in change_rows:
        try:
            effective = parse_date(row.get("EFFECTIVE DATE"))
        except ValueError:
            effective = None
        changes.append({
            "company": (row.get("NAME OF THE COMPANY") or "").strip(),
            "old_symbol": (row.get("OLD SYMBOL") or "").strip(),
            "new_symbol": (row.get("NEW SYMBOL") or "").strip(),
            "effective_date": effective.isoformat() if effective else None,
        })
    rows = []
    for source in csv_rows(delisted_path):
        symbol = (source.get("Symbol") or "").strip()
        if not symbol:
            continue
        when = parse_date(source.get("Delisted Date"))
        if when is None or when > CUTOFF:
            continue
        company = (source.get("Company") or "").strip()
        matches = [row for row in official if row["symbol"].strip() == symbol]
        entry = {"symbol": symbol, "company": company,
                 "delisted_date": when.isoformat(),
                 "type": (source.get("Type of Delisting") or "").strip(),
                 "exact_symbol_api_rows": len(matches),
                 "identity_match": ("exact_symbol_only_not_isin_verified" if matches
                                    else "unresolved_not_isin_verified")}
        if when < date(2011, 1, 1):
            name_key = company_key(company)
            same_name = [row for row in by_company.get(name_key, [])
                         if row["symbol"].strip() != symbol]
            symbol_changes = [change for change in changes
                              if symbol in (change["old_symbol"], change["new_symbol"])
                              or (name_key and company_key(change["company"]) == name_key)]
            entry.update({
                "same_company_name_api_rows": len(same_name),
                "same_company_name_api_symbols": sorted({row["symbol"].strip()
                                                         for row in same_name}),
                "same_company_name_api_evidence": [
                    {"symbol": row["symbol"], "ex_date": row["ex_date"],
                     "subject": row["subject"]} for row in same_name],
                "symbol_change_evidence": symbol_changes,
                "same_company_name_match_note": (
                    "Exact normalized company-name lead only; not a verified ISIN or "
                    "dated security-lineage match"),
            })
            if matches:
                entry["resolution_status"] = "exact_symbol_rows_present_identity_not_isin_verified"
            elif same_name:
                entry["resolution_status"] = "unresolved_same_company_name_lead_not_verified"
            elif symbol_changes:
                entry["resolution_status"] = "unresolved_symbol_change_evidence_not_verified"
            else:
                entry["resolution_status"] = "unresolved_no_official_identity_evidence"
        else:
            entry["resolution_status"] = ("exact_symbol_rows_present_identity_not_isin_verified"
                                          if matches else "unresolved_not_isin_verified")
        rows.append(entry)
    return rows


def bars_diagnostics(db: Path, official: list[dict], unresolved: list[str],
                     queue_start: date = START, queue_end: date = END,
                     queue_move: float = 0.50) -> tuple[dict, list[dict], dict]:
    """Read-only continuity diagnostic. Never acts as an action or factor source.

    The exchange's ``previous_close`` field is an unadjusted prior close. A raw
    close discontinuity is therefore not proof of a corporate action, and no
    adjustment factor is derived here.
    """
    try:
        import duckdb  # Optional: only needed when a bars database is supplied.
    except ImportError as error:  # pragma: no cover - dependency is declared in pyproject
        raise RuntimeError("duckdb is required to read the bars diagnostic database") from error
    if not db.exists():
        raise FileNotFoundError(db)
    con = open_catalog(database=db)
    try:
        columns = {row[0] for row in con.execute("DESCRIBE bars").fetchall()}
        required = {"date", "symbol", "series", "close", "previous_close"}
        if not required <= columns:
            raise ValueError(f"bars view lacks required columns: {sorted(required - columns)}")
        total, earliest, latest = con.execute(
            "SELECT count(*), min(date), max(date) FROM bars").fetchone()
        eq_rows = dict(con.execute(
            "SELECT year(date), count(*) FROM bars WHERE series='EQ' GROUP BY 1").fetchall())
        summary_rows = con.execute("""
            WITH e AS (
              SELECT symbol, date, close, previous_close,
                     lag(close) OVER (PARTITION BY symbol ORDER BY date) AS prior_close
              FROM bars WHERE series = 'EQ'
            ), d AS (
              SELECT *, previous_close - prior_close AS pc_diff FROM e
              WHERE prior_close IS NOT NULL
            )
            SELECT year(date), count(*),
                   sum(CASE WHEN abs(pc_diff) <= 0.011 THEN 1 ELSE 0 END),
                   sum(CASE WHEN abs(pc_diff) > 0.011 THEN 1 ELSE 0 END),
                   sum(CASE WHEN abs(pc_diff) > 0.011
                            AND abs(pc_diff) / (abs(prior_close) + 0.01) > 0.01
                       THEN 1 ELSE 0 END),
                   sum(CASE WHEN abs(close / NULLIF(prior_close, 0) - 1) >= 0.20
                       THEN 1 ELSE 0 END),
                   sum(CASE WHEN abs(close / NULLIF(prior_close, 0) - 1) >= 0.20
                            AND abs(pc_diff) <= 0.011 THEN 1 ELSE 0 END)
            FROM d GROUP BY 1 ORDER BY 1
        """).fetchall()
        large = con.execute("""
            WITH e AS (
              SELECT symbol, date, close, previous_close,
                     lag(close) OVER (PARTITION BY symbol ORDER BY date) AS prior_close,
                     lag(date) OVER (PARTITION BY symbol ORDER BY date) AS prior_date
              FROM bars WHERE series = 'EQ'
            )
            SELECT symbol, date, prior_date, close, previous_close, prior_close,
                   abs(close / NULLIF(prior_close, 0) - 1) AS abs_move
            FROM e WHERE prior_close IS NOT NULL
              AND abs(close / NULLIF(prior_close, 0) - 1) >= 0.20
            ORDER BY date, symbol
        """).fetchall()
        presence = {symbol: [] for symbol in unresolved}
        if unresolved:
            marks = ",".join("?" for _ in unresolved)
            for symbol, series, count, first, last in con.execute(
                    f"SELECT symbol, series, count(*), min(date), max(date) FROM bars "
                    f"WHERE symbol IN ({marks}) GROUP BY 1, 2 ORDER BY 1, 2",
                    unresolved).fetchall():
                presence.setdefault(symbol, []).append({
                    "series": series, "rows": count,
                    "first_date": first.isoformat(), "last_date": last.isoformat()})
    finally:
        con.close()

    action_map: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for row in official:
        action_map[(row["symbol"].strip(), row["ex_date"])].append(row)
    queue, by_year = [], collections.defaultdict(collections.Counter)
    large_equal, split_bonus_matches, examples = 0, 0, []
    split_bonus_keys = {(row["symbol"].strip(), row["ex_date"]) for row in official
                        if {"split", "bonus"} & set(row["action_tags"])}
    for symbol, when, prior_date, close, previous_close, prior_close, move in large:
        matches = action_map.get((symbol, when.isoformat()), [])
        equal = abs(previous_close - prior_close) <= 0.011
        large_equal += equal
        tags = {tag for match in matches for tag in match["action_tags"]}
        if {"split", "bonus"} & tags:
            split_bonus_matches += 1
            if len(examples) < 3:
                examples.append({
                    "symbol": symbol, "date": when.isoformat(), "close": close,
                    "previous_close": previous_close, "prior_close": prior_close,
                    "official_action_subjects": [match["subject"] for match in matches],
                    "note": ("previous_close equals the prior close here; it does not "
                             "encode the observed price change"),
                })
        by_year[when.year]["large_moves"] += 1
        by_year[when.year]["with_exact_action"] += bool(matches)
        by_year[when.year]["previous_close_equals_prior_close"] += equal
        if (queue_start <= when <= queue_end and move >= queue_move and not matches):
            queue.append({
                "symbol": symbol, "date": when.isoformat(),
                "prior_trading_date": prior_date.isoformat() if prior_date else None,
                "prior_close": prior_close, "close": close,
                "previous_close": previous_close,
                "abs_close_change_pct": round(move * 100, 2),
                "previous_close_equals_prior_close": equal,
                "official_action_rows_same_symbol_date": [],
                "status": "unresolved_price_discontinuity_not_an_event",
                "note": ("Diagnostic candidate for manual official-document review. "
                         "Not proof of an action and not an adjustment factor."),
            })
    years = {}
    for year, checkable, pc_equal, pc_mismatch, pc_mismatch_1pct, big, big_equal in summary_rows:
        years[str(year)] = {
            "eq_rows": eq_rows.get(year, 0),
            "first_rows_without_prior": eq_rows.get(year, 0) - checkable,
            "checkable_rows": checkable,
            "previous_close_equals_prior_close": pc_equal,
            "previous_close_mismatch": pc_mismatch,
            "previous_close_mismatch_over_1pct": pc_mismatch_1pct,
            "large_close_moves_20pct": big,
            "large_close_moves_with_equal_previous_close": big_equal,
            "large_moves_with_exact_action": (
                by_year[year]["with_exact_action"] if 2000 <= year <= 2010 else None),
        }
    summary = {
        "source": str(db),
        "read_only": True,
        "is_official_action_source": False,
        "bars_rows": total,
        "bars_earliest": earliest.isoformat() if earliest else None,
        "bars_latest": latest.isoformat() if latest else None,
        "official_action_rows_available_to_match": len(official),
        "action_match_window": {"start": str(START), "end": str(END)},
        "years": years,
        "previous_close_semantics": {
            "finding": ("Observed previous_close is the unadjusted prior close, even on "
                        "split/bonus ex-dates; it cannot be used to infer an adjustment"),
            "large_close_moves_20pct": len(large),
            "large_close_moves_with_equal_previous_close": large_equal,
            "split_or_bonus_api_rows": len(split_bonus_keys),
            "split_or_bonus_rows_matched_to_large_move": split_bonus_matches,
            "examples": examples,
        },
        "large_close_move_review": {
            "window_start": queue_start.isoformat(),
            "window_end": queue_end.isoformat(),
            "threshold_abs_close_change": queue_move,
            "queue_rows": len(queue),
            "queue_file": "unresolved-price-discontinuities-2000-2004.jsonl",
            "warning": ("A raw close move is diagnostic only. It is not an event, not "
                        "proof of missing coverage and never an adjustment factor."),
        },
        "unresolved_pre2011_delisted_price_presence": presence,
        "limitations": [
            "Lagged close is within one symbol/series line; symbol reuse is not resolved",
            "A missing bhavcopy session can appear as a previous_close mismatch",
            "Absence from the bars archive is not proof that a security never traded",
            "No ISIN is available in the host bars before 2011",
        ],
    }
    return summary, queue, presence


def orders_page_checks(path: Path, symbols: list[str]) -> dict:
    """Summarize the saved official Orders of Delisting Committee page.

    Only the page's listed order dates are summarized. Absence from this page
    is not proof that no older order exists elsewhere.
    """
    html = path.read_text(encoding="utf-8", errors="ignore")
    text_dates = []
    for token in re.findall(r"\b\d{2}-[A-Za-z]{3}-\d{4}\b", html):
        try:
            text_dates.append(datetime.strptime(token, "%d-%b-%Y").date())
        except ValueError:
            continue
    pdf_links = re.findall(r'href="([^"]+\.pdf[^"]*)"', html, re.I)
    page_symbols = {symbol: (symbol in html.upper()) for symbol in symbols}
    return {
        "source_url": "https://www.nseindia.com/static/list/orders-of-delisting",
        "source_file": path.name,
        "listed_order_dates": len(text_dates),
        "earliest_listed_order_date": min(text_dates).isoformat() if text_dates else None,
        "latest_listed_order_date": max(text_dates).isoformat() if text_dates else None,
        "pre2011_listed_orders": sum(d < date(2011, 1, 1) for d in text_dates),
        "pdf_links": len(pdf_links),
        "unresolved_symbols_named_on_page": [s for s, hit in page_symbols.items() if hit],
        "warning": ("This public page lists recent orders only. Absence of a 2004 order "
                    "here does not prove no archived order exists; it remains a manual "
                    "purchase/archive request, not a silent gap-fill."),
    }


def build(raw: Path, processed: Path, report: Path,
          bars_db: Path | None = None, reports: Path | None = None) -> dict:
    official, yearly, missing = [], {}, []
    duplicate_keys: collections.Counter = collections.Counter()
    out_of_window = 0
    for year in range(2000, 2011):
        path = raw / f"nse-api-{year}.json"
        if not path.exists():
            missing.append(year)
            continue
        source_rows = validate_api(json.loads(path.read_bytes()), year)
        for i, row in enumerate(source_rows):
            ex = parse_date(row["exDate"])
            if ex is None or ex.year != year:
                out_of_window += 1
            duplicate_keys[(row["symbol"].strip(), ex.isoformat() if ex else "",
                            row["subject"].strip())] += 1
            official.append({**row, "ex_date": ex.isoformat() if ex else None,
                             "source_file": path.name, "source_row": i,
                             "source_url": api_url(year), "action_tags": action_tags(row["subject"]),
                             "identity_status": "NSE_returned_identity_not_historical_symbol_resolved",
                             "adjustment_status": "not_applied"})
        dates = [parse_date(r["exDate"]) for r in source_rows if parse_date(r["exDate"])]
        yearly[str(year)] = {"raw_rows": len(source_rows), "dated_rows": len(dates),
                             "earliest": str(min(dates)) if dates else None,
                             "latest": str(max(dates)) if dates else None,
                             "symbols": len({r["symbol"] for r in source_rows}),
                             "tags": dict(collections.Counter(t for r in source_rows
                                                               for t in action_tags(r["subject"])))}
    write_jsonl(processed / "nse-api-actions.jsonl", official)
    dates = [r["ex_date"] for r in official if r["ex_date"]]
    coverage = {"requested_start": str(START), "requested_end": str(END),
                "current_cutoff_inclusive": str(CUTOFF), "years": yearly, "missing_years": missing,
                "official_raw_rows": len(official), "official_dated_rows": len(dates),
                "earliest": min(dates, default=None), "latest": max(dates, default=None),
                "series": dict(collections.Counter(r.get("series") for r in official)),
                "isin_prefixes": dict(collections.Counter((r.get("isin") or "")[:3] for r in official)),
                "complete": False,
                "limitations": ["NSE API historical completeness is not documented or proven",
                                "Subjects can combine multiple actions; row count is not economic event count",
                                "API uses renamed identities; do not join historical bars on current symbol alone",
                                "ISIN/series may describe current identity, not the identity on the ex-date",
                                "No verified announcement time: unsuitable for point-in-time knowledge",
                                "Delisting CSV is a partial reference, not a complete historical universe"]}
    # Everything above is the raw, tags-only inventory. Label it as such and
    # point at the normalized/applied layer, so this file cannot be read as a
    # claim that the pre-2011 actions are unverified or unapplied.
    coverage["scope"] = "raw_tag_inventory"
    coverage["scope_note"] = (
        "This report inventories official NSE API pre-2011 action rows with search "
        "labels only. Verified terms, execution readiness and applied factors are "
        "summarised under 'normalized_actions'; the readiness verdict is "
        + AUTHORITATIVE_READINESS_REPORT + ".")
    coverage["raw_inventory"] = {
        "kind": "tags_only_nse_api_inventory",
        "rows": len(official),
        "dated_rows": len(dates),
        "earliest": min(dates, default=None),
        "latest": max(dates, default=None),
        "complete": False,
        "price_adjustment": "none applied by this tags-only collector",
        "note": ("No term is parsed here and no price is adjusted here. A row count "
                 "is not an economic event count and not a verified term."),
    }
    coverage["superseded_by"] = AUTHORITATIVE_READINESS_REPORT
    coverage["superseded_note"] = (
        "The authoritative pre-2011 action evidence is the normalized, terms-verified "
        "and applied layer reported in " + VERIFIED_ACTIONS_REPORT + " and "
        + ADJUSTMENT_COVERAGE_REPORT + "; the overall readiness verdict is "
        + AUTHORITATIVE_READINESS_REPORT + ".")
    coverage["normalized_actions"] = normalized_actions_summary(reports)
    coverage["validation"] = {
        "exact_key_duplicates": sum(count - 1 for count in duplicate_keys.values() if count > 1),
        "rows_outside_requested_window": out_of_window,
        "rows_missing_required_fields": 0,
        "official_dated_rows": len(dates),
        "official_blank_isin_rows": sum(not (r.get("isin") or "").strip() for r in official),
        "distinct_returned_symbols": len({r["symbol"].strip() for r in official}),
        "rows_with_blank_series": sum(not (r.get("series") or "").strip() for r in official),
        "note": ("Required-field validation raises before this point; duplicate count uses "
                 "exact returned symbol, ISO ex-date and stripped subject"),
    }
    coverage["source_probe_checks"] = probe_checks(raw)
    changes: list[dict] = []
    change_dates: list[date] = []
    changes_file = raw / "nse-symbolchange.csv"
    if changes_file.exists():
        changes = csv_rows(changes_file)
        change_dates = [parse_date(r["EFFECTIVE DATE"]) for r in changes]
        coverage["symbol_change_reference"] = {
            "rows": len(changes), "earliest": str(min(change_dates)), "latest": str(max(change_dates)),
            "pre2011_2000plus_rows": sum(START <= d <= END for d in change_dates),
            "applied_to_actions": False}
    delisted_file = raw / "nse-delisted.csv"
    if delisted_file.exists():
        delisted = build_delisted(delisted_file, official, changes)
        unresolved = [r["symbol"] for r in delisted
                      if r["delisted_date"] < "2011-01-01" and not r["exact_symbol_api_rows"]]
        if bars_db is not None and bars_db.exists():
            diag, queue, presence = bars_diagnostics(
                bars_db, official, unresolved, queue_end=date(2004, 12, 31))
            for row in delisted:
                if row["delisted_date"] < "2011-01-01":
                    row["nse_price_observations"] = presence.get(row["symbol"], [])
            write_json(processed / "bars-diagnostics.json", diag)
            write_jsonl(processed / "unresolved-price-discontinuities-2000-2004.jsonl", queue)
            coverage["bars_diagnostic"] = diag
        write_jsonl(processed / "delisted-coverage.jsonl", delisted)
        pre2011 = [r for r in delisted if r["delisted_date"] < "2011-01-01"]
        unresolved_rows = [r for r in pre2011 if not r["exact_symbol_api_rows"]]
        coverage["delisted_reference"] = {
            "rows_through_cutoff": len(delisted),
            "earliest": min((r["delisted_date"] for r in delisted), default=None),
            "latest": max((r["delisted_date"] for r in delisted), default=None),
            "pre2011_delistings": len(pre2011),
            "pre2011_with_exact_symbol_api_rows": sum(bool(r["exact_symbol_api_rows"]) for r in pre2011),
            "pre2011_without_exact_symbol_api_rows": [r["symbol"] for r in unresolved_rows],
            "with_exact_symbol_api_rows": sum(bool(r["exact_symbol_api_rows"]) for r in delisted),
            "without_exact_symbol_api_rows": sum(not r["exact_symbol_api_rows"] for r in delisted),
            "pre2011_unresolved_details": unresolved_rows,
            "pre2011_unresolved_summary": {
                "count": len(unresolved_rows),
                "same_company_name_api_lead_count": sum(
                    bool(r["same_company_name_api_rows"]) for r in unresolved_rows),
                "same_company_name_api_lead_symbols": [
                    r["symbol"] for r in unresolved_rows if r["same_company_name_api_rows"]],
                "symbol_change_evidence_count": sum(
                    bool(r["symbol_change_evidence"]) for r in unresolved_rows),
                "with_nse_price_observations": sum(
                    bool(r.get("nse_price_observations")) for r in unresolved_rows),
                "without_nse_price_observations": sum(
                    not r.get("nse_price_observations") for r in unresolved_rows),
                "note": ("Same-company-name leads and price presence are investigation aids, "
                         "not verified identity or action coverage"),
            },
            "warning": "Zero matches does not mean no events; spelling, rename and API gaps unresolved"}
        orders_file = raw / "probe-orders-page.html"
        if orders_file.exists():
            coverage["delisting_orders_page_check"] = orders_page_checks(
                orders_file, [r["symbol"] for r in unresolved_rows])
    db = raw / "eod2-main.db"
    if db.exists():
        upstream, stats = sqlite_rows(db)
        selected = [r for r in upstream if r["ex_date"] and str(START) <= r["ex_date"] <= str(END)]
        write_jsonl(processed / "eod2-unverified-actions.jsonl", selected)
        stats["pre2011_2000plus_rows"] = len(selected)
        stats["years"] = dict(sorted(collections.Counter(r["ex_date"][:4] for r in selected).items()))
        official_keys = {(r["symbol"], r["ex_date"], r["subject"].strip()) for r in official}
        upstream_keys = {(r["symbol"], r["ex_date"], r["subject"].strip()) for r in selected}
        stats["exact_official_matches"] = len(official_keys & upstream_keys)
        stats["upstream_only_unverified"] = len(upstream_keys - official_keys)
        stats["official_only"] = len(official_keys - upstream_keys)
        coverage["third_party_upstream"] = stats
    monthly_check = raw / "nse-api-2010-august-check.json"
    if monthly_check.exists():
        monthly = validate_api(json.loads(monthly_check.read_bytes()), 2010)
        fields = set(monthly[0]) if monthly else set()
        annual = [{k: r[k] for k in fields} for r in official if (r["ex_date"] or "").startswith("2010-08")]
        coverage["pagination_sample_check"] = {
            "month": "2010-08", "month_rows": len(monthly), "annual_subset_rows": len(annual),
            "equal_multisets": collections.Counter(map(canonical_row, monthly))
            == collections.Counter(map(canonical_row, annual)),
            "warning": "One monthly cross-check cannot establish completeness for every year"}
    write_json(report, coverage)
    return coverage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=data_root())
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--include-upstream", action="store_true")
    parser.add_argument("--bars-db", type=Path, default=None,
                        help="Read-only DuckDB bars database for continuity diagnostics")
    parser.add_argument("--no-bars", action="store_true",
                        help="Skip the optional bars diagnostic")
    args = parser.parse_args()
    raw = args.data / "raw/older_actions"
    if args.fetch:
        acquire(raw, args.include_upstream)
    bars_db = None if args.no_bars else (args.bars_db or args.data / "market.duckdb")
    result = build(raw, args.data / "processed/older_actions",
                   args.data / "processed/older_actions/coverage.json",
                   bars_db=bars_db if bars_db and bars_db.exists() else None,
                   reports=args.data / "reports")
    write_json(args.data / "reports/pre2011-actions-coverage.json", result)
    print(json.dumps({k: result[k] for k in ("official_raw_rows", "earliest", "latest", "missing_years")}))


if __name__ == "__main__":
    main()
