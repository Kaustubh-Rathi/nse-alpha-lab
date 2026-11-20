"""Purchase enquiry pack for per-index gaps and exact effective-date history.

This module validates and renders a *ready-to-send* enquiry pack. It does
not send email, does not purchase, and invents no price or coverage date.

Background (from ``data/reports/nifty-vendor-research.md`` and
``data/reports/nifty-doc-research.md``):

- Public documentary baselines are Nifty 50 **2008-01-31** and Nifty 500
  **2013-04-18**. The retained research establishes no complete earlier list.
- Missing pre-baseline windows run through 2008-01-30 for Nifty 50 and
  2013-04-17 for Nifty 500, inclusive. Public snapshots after those dates
  are NOT continuous daily effective-date history. Orphan changelog deltas
  cannot fabricate an unknown baseline.
- The retained research establishes no vendor commitment back to 2000.
  A price is ``enquiry_required`` (null), never a guessed number.

Fail-closed decision rule, encoded in :func:`decide_purchase` and in the
flowchart: **pay only on written confirmation that coverage starts on or
before 2000-01-01**, plus a gap report, a verified sample, and a written
local-research licence. An auto-reply is not written confirmation.

Scope (owned files only)::

    src/nse_pipeline/nifty/purchase.py
    tests/test_purchase.py
    data/reports/nifty-purchase-pack.md
    data/reports/nifty-purchase-pack.json
    data/raw/nifty_purchase/   (.eml/.txt email, sample request,
                                checklist PDF/MD, decision flowchart)
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime
from email import message_from_bytes
from pathlib import Path

RECIPIENT = "indices@nseids.co.in"
SUBJECT = (
    "Historical Nifty 50/500 constituency enquiry 2000-2026 "
    "(point-in-time, effective dates, gaps, corrections, DVR/dummy, ISIN)"
)
GAP_START = date(2000, 1, 1)
# Legacy COMMON SUBSET for backwards-compatible size arithmetic only. This
# is NOT the end of either index's missing pre-baseline window.
GAP_END = date(2007, 12, 31)
PUBLIC_BASELINE_NIFTY50 = date(2008, 1, 31)
PUBLIC_BASELINE_NIFTY500 = date(2013, 4, 18)
REQUESTED_END = date(2026, 11, 7)
SAMPLE_DATES = ("2000-01-03", "2004-01-05", "2007-12-31")
MISSING_WINDOWS = {
    "NIFTY50": {"start": "2000-01-01", "end": "2008-01-30", "baseline": "2008-01-31"},
    "NIFTY500": {"start": "2000-01-01", "end": "2013-04-17", "baseline": "2013-04-18"},
}
EXACT_EFFECTIVE_HISTORY = {
    "start": GAP_START.isoformat(), "end": REQUESTED_END.isoformat(),
    "date_bounds": "inclusive", "required": True,
    "snapshot_overlap": "reconciliation_only_not_continuous_effective_coverage",
    "include_post_baseline_exact_intervals": True,
}

TOP_FIELDS = (
    "generated_at_utc",
    "recipient",
    "subject",
    "email_body",
    "gap_request",
    "size_estimate",
    "checklist",
    "decision_flowchart",
    "notes",
    "tests",
)
GAP_FIELDS = (
    "index_codes",
    "start",
    "end",
    "sample_dates",
    "include_post_baseline_repurchase",
    "cost_public",
    "cost_status",
    "missing_windows",
    "exact_effective_history",
)
ESTIMATE_FIELDS = (
    "method",
    "months",
    "rows_per_month_nifty50",
    "rows_per_month_nifty500",
    "monthly_rows_estimate",
    "trading_days_per_year_assumed",
    "daily_rows_estimate",
    "bytes_per_row_assumed",
    "csv_size_mb_estimate",
    "note",
)
CHECKLIST_FIELDS = ("check_id", "question", "why_matters", "must_have")
FLOWCHART_FIELDS = ("nodes", "edges", "mermaid", "pay_guard")
NODE_FIELDS = ("node_id", "label", "kind")
EDGE_FIELDS = ("from_node", "to_node", "label")

REQUIRED_BODY_TERMS = (
    "2000",
    "effective",
    "gap report",
    "corrections",
    "dvr",
    "dummy",
    "isin",
    "sample",
    "licence",
    "price",
    "will not proceed to purchase",
    "confirmed in writing",
)
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

EMAIL_BODY = """Dear NSE Indices data team,

We are conducting local academic backtesting research and enquire about
purchasing official point-in-time constituent data for Nifty 50 and
Nifty 500 covering 2000-01-01 through 2026-11-07.

Our retained public documentary baselines are 2008-01-31 (Nifty 50)
and 2013-04-18 (Nifty 500). Please quote these pre-baseline windows
separately, with inclusive bounds:
  - Nifty 50: 2000-01-01 through 2008-01-30 (including January 2008).
  - Nifty 500: 2000-01-01 through 2013-04-17 (including 2008-2013).
The 2000-01-01 to 2007-12-31 common subset is NOT the whole gap.

Public snapshots are not continuous daily effective-date history.
We also need exact effective-date intervals through 2026-11-07,
including after each baseline wherever effective history is not
independently verified. Please itemise this separately from duplicate
snapshot delivery. Overlap with our snapshots is for reconciliation;
it does not justify excluding purchased exact effective dates.

Please confirm in writing:

1. Earliest supported date for each index, and whether the full
   2000-01-01 to 2026-11-07 range is supplied without gaps.
2. A gap report listing every missing date, index, or security so we
   can reconcile against public monthly reports.
3. Effective dates: effective_from and effective_to per constituent
   (not just as-of snapshots), plus announcement/availability
   timestamps if you hold them.
4. Corrections: how restated or corrected past constituents are
   flagged and redelivered.
5. DVR and dummy handling: how DVR classes (e.g. TATAMTRDVR) and
   non-trading dummy rows (e.g. DUMMYTATAM, DUMMYREL) are treated
   and identified.
6. Identifiers: symbol (as traded on the day), company name, and ISIN
   per row; how symbol renames and share-class changes are carried.
7. A priced sample extract for both pre-baseline windows (e.g.
   2000-01-03, 2004-01-05, 2007-12-31, 2008-01-02 for both indices,
   and 2010-01-04 / 2013-04-17 for Nifty 500), plus both baseline
   joins and an intra-month effective change, including weights and market
   capitalisation if available, with the estimated file size and
   row count per index/window for the full effective-history delivery.
8. Delivery format, update cadence, licence scope for local research
   and internal research outputs, redistribution limits, itemised
   price for the gap backfill (taxes, term and renewal included),
   and whether an ongoing subscription is needed or a one-off
   archive extract suffices.

We will not proceed to purchase until the earliest date and gap
position are confirmed in writing. An auto-reply alone will not be
treated as confirmation. Thank you.

[Your full name]
[Affiliation / institution]
[Contact email and phone]
"""

SAMPLE_REQUEST = """Sample request: per-index missing windows and exact effective-date history

To: indices@nseids.co.in
Subject: Priced effective-history sample: Nifty 50/500, 2000-2026

Scope
-----
- Indices: Nifty 50 and Nifty 500 (with contemporaneous pre-2016
  names, e.g. S&P CNX Nifty / CNX 500, mapped in writing).
- Missing pre-baseline windows, inclusive:
  Nifty 50: 2000-01-01 through 2008-01-30 (baseline 2008-01-31).
  Nifty 500: 2000-01-01 through 2013-04-17 (baseline 2013-04-18).
- Public snapshots are not continuous daily effective-date history.
  Itemise exact effective-date intervals for 2000-01-01 to 2026-11-07,
  including after both baselines wherever not independently verified.
  Snapshot overlap is for reconciliation, NOT an exclusion of purchased
  exact effective dates. Avoid repricing duplicate snapshots only.
- No purchase is authorised by this request. It asks for a priced
  sample and an itemised estimate only.

Priced sample requested (per-index gaps, both joins, intra-month change)
------------------------------------------------------
- 2000-01-03 (first trading days of 2000)
- 2004-01-05 (mid-gap era)
- 2007-12-31 and 2008-01-02 (both indices; January 2008 is missing too)
- 2010-01-04 and 2013-04-17 (Nifty 500's longer pre-baseline gap)
- 2008-01-31 (Nifty 50 join) and 2013-04-18 (Nifty 500 join)
- An actual intra-month membership change, with precise effective dates
For each applicable sample date/index, with: symbol as traded that day,
company name, ISIN, effective_from/effective_to, weight and market
capitalisation if available, and the DVR/dummy classification rule
applied (e.g. TATAMTRDVR; DUMMYTATAM, DUMMYREL).

Size estimate requested from vendor
-----------------------------------
Please state separately for each missing window and full effective history:
  * number of dates and rows per index, and total rows;
  * delivery format and uncompressed/compressed file size;
  * whether delivery is monthly snapshots or daily / effective-date
    intervals, and how entries/exits inside a month are dated;
  * the gap report (every missing date/index/security).

Legacy COMMON-SUBSET arithmetic only, NOT the size of this request:
2000-01-01 to 2007-12-31 is 96 months x (50 + 500) = 52,800
monthly snapshot rows, or about 1.1 million assumed daily rows.
It excludes January 2008, Nifty 500's 2008-2013 gap, and later exact
effective history. Monthly row counts cannot size interval delivery.
Vendor must provide the actual per-index interval counts and file size.
No current vendor quote has been verified: cost_public is null /
enquiry_required. Do not infer a price from this arithmetic.

Acceptance before any payment
-----------------------------
Written confirmation of: earliest date on or before 2000-01-01;
gap report; effective dates; corrections policy; DVR/dummy rule;
ISIN per tradable row; samples verified at the 2008-01-31 Nifty 50
and 2013-04-18 Nifty 500 joins, plus exact intra-month effective dates;
local-research licence; itemised price. See the decision
flowchart in this pack: pay only on written 2000 confirmation.
"""

CHECKLIST: tuple[dict, ...] = (
    {"check_id": "earliest-date", "question": "What is the earliest date for which Nifty 50 and Nifty 500 constituents are supplied?",
     "why_matters": "The research need starts 2000-01-01; a subscription must not be assumed to reach back that far.", "must_have": True},
    {"check_id": "full-range", "question": "Is the full 2000-01-01 to 2026-11-07 range supplied for both indices?",
     "why_matters": "Partial history leaves uncovered years that cannot be filled by rules without breaking the official-membership claim.", "must_have": True},
    {"check_id": "gap-report", "question": "Will you provide a gap report listing every missing date, index, or security?",
     "why_matters": "Without a gap report, silence looks like continuity and corrupts backtests.", "must_have": True},
    {"check_id": "effective-dates", "question": "Are effective_from and effective_to dates supplied per constituent?",
     "why_matters": "As-of snapshots alone cannot date entries and exits inside intervals; effective dates make membership intervals exact.", "must_have": True},
    {"check_id": "announcement-vs-effective", "question": "Are announcement dates and availability timestamps supplied separately from effective dates?",
     "why_matters": "A point-in-time backtest must know what was knowable on each day, not only what was effective.", "must_have": True},
    {"check_id": "corrections", "question": "How are corrections and restatements to past constituents flagged and redelivered?",
     "why_matters": "Silent corrections rewrite history; versioned corrections can be reconciled against the public archive.", "must_have": True},
    {"check_id": "dvr-dummy", "question": "How are DVR classes and dummy/non-trading rows (e.g. DUMMYTATAM, DUMMYREL) classified and counted?",
     "why_matters": "Security counts of 51 in a 50-stock index and DUMMY rows occur in official snapshots; the rule must be explicit, not guessed.", "must_have": True},
    {"check_id": "isin", "question": "Is an ISIN supplied per row, and does it reflect the share class traded on that date?",
     "why_matters": "Public MCWB/PDF snapshots carry no ISINs; ISINs are needed to join membership to prices without symbol-string collisions.", "must_have": True},
    {"check_id": "symbol-identity", "question": "Which symbol is carried for each date (as traded that day), and how are renames mapped?",
     "why_matters": "Symbols are reused over time; carrying today's symbol back in time would misjoin prices.", "must_have": True},
    {"check_id": "pre-rename-names", "question": "Do pre-2016 rows use contemporaneous names (S&P CNX Nifty, CNX Nifty/CNX 500) with a mapping to Nifty 50/500?",
     "why_matters": "Official titles changed (e.g. cnx500 PDFs in 2013-2014); a rename map prevents silently relabelling old indices.", "must_have": False},
    {"check_id": "weights-mcap", "question": "Are weights and market capitalisation supplied per constituent per date, and under which methodology regime?",
     "why_matters": "Free-float versus full-cap regimes changed in 2009-2010; weights must not be mixed across regimes without a flag.", "must_have": False},
    {"check_id": "sample", "question": "Will you provide a priced sample extract covering at least two eras (e.g. 2000 and 2010)?",
     "why_matters": "A sample proves the earliest date and field semantics before money is spent.", "must_have": True},
    {"check_id": "delivery", "question": "What are delivery format, cadence, and historical-backfill mechanism (file/API/SFTP)?",
     "why_matters": "Delivery determines whether the archive can be loaded and diffed against the public snapshots we already hold.", "must_have": False},
    {"check_id": "licensing", "question": "Does the licence permit local research use and internal publication of derived results? What is redistributed?",
     "why_matters": "Public downloadability is not a redistribution licence; terms must be granted explicitly in writing.", "must_have": True},
    {"check_id": "price-terms", "question": "What is the price for the 2000-2026 backfill plus ongoing feed, including taxes, term, and renewal?",
     "why_matters": "Direct NSE Indices pricing varies by data type and period and is not published; the quote must be itemised.", "must_have": False},
    {"check_id": "survivor", "question": "Does the history include delisted, suspended, and exited constituents (no survivor bias)?",
     "why_matters": "A present-day universe would drop exactly the securities that matter for a 2000-2026 backtest.", "must_have": True},
)

FLOW_NODES: tuple[dict, ...] = (
    {"node_id": "send_enquiry", "label": "Send enquiry email to indices@nseids.co.in (no payment, no commitment)", "kind": "action"},
    {"node_id": "await_reply", "label": "Await written reply (auto-reply is not confirmation)", "kind": "wait"},
    {"node_id": "written_2000", "label": "Written confirmation: coverage starts on or before 2000-01-01 for both indices?", "kind": "decision"},
    {"node_id": "gap_report", "label": "Gap report received and covers 2000-2007 with no unexplained holes?", "kind": "decision"},
    {"node_id": "sample_verified", "label": "Priced gap-only sample verified (2000/2004/2007; ISIN, effective dates, DVR/dummy; joins 2008-01-31 baseline)?", "kind": "decision"},
    {"node_id": "licence_ok", "label": "Written local-research licence and itemised price (taxes, term, renewal) accepted?", "kind": "decision"},
    {"node_id": "pay_allowed", "label": "PAY: authorise payment for the gap backfill only", "kind": "terminal_pay"},
    {"node_id": "do_not_pay", "label": "DO NOT PAY: stop; keep public baselines (Nifty 50 2008-01-31, Nifty 500 2013-04-18); consider authorised-vendor route (Bloomberg/FactSet) with the same gates", "kind": "terminal_stop"},
)

FLOW_EDGES: tuple[dict, ...] = (
    {"from_node": "send_enquiry", "to_node": "await_reply", "label": "sent"},
    {"from_node": "await_reply", "to_node": "written_2000", "label": "reply received"},
    {"from_node": "written_2000", "to_node": "gap_report", "label": "yes, in writing"},
    {"from_node": "written_2000", "to_node": "do_not_pay", "label": "no / auto-reply only / starts after 2000"},
    {"from_node": "gap_report", "to_node": "sample_verified", "label": "yes"},
    {"from_node": "gap_report", "to_node": "do_not_pay", "label": "no"},
    {"from_node": "sample_verified", "to_node": "licence_ok", "label": "yes"},
    {"from_node": "sample_verified", "to_node": "do_not_pay", "label": "no"},
    {"from_node": "licence_ok", "to_node": "pay_allowed", "label": "yes"},
    {"from_node": "licence_ok", "to_node": "do_not_pay", "label": "no"},
)

PAY_GUARD = (
    "PAY is allowed only downstream of written_2000 (earliest coverage "
    "on or before 2000-01-01, confirmed in writing, not an auto-reply), "
    "then gap_report, sample_verified and licence_ok, in that order. "
    "No edge reaches pay_allowed except from licence_ok. The gap report and "
    "verified sample must address each missing_windows entry, both baseline "
    "joins and exact_effective_history, not merely the legacy 2000-2007 subset. "
    "Public snapshots do not satisfy continuous effective-date coverage."
)


def render_email_txt() -> str:
    return f"To: {RECIPIENT}\nSubject: {SUBJECT}\n\n{EMAIL_BODY}"


def render_eml() -> bytes:
    text = (
        f"To: {RECIPIENT}\r\nSubject: {SUBJECT}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: 8bit\r\n\r\n" + EMAIL_BODY
    )
    return text.encode("utf-8")


def render_sample_request() -> str:
    return SAMPLE_REQUEST


def render_checklist_md() -> str:
    lines = [
        "# Nifty purchase checklist (16 items)",
        "",
        "Must-have items are marked ★. Every must-have must be answered "
        "in writing before payment; see the decision flowchart.",
        "",
    ]
    for i, item in enumerate(CHECKLIST, 1):
        star = " ★" if item["must_have"] else ""
        lines.append(f"{i}. [{item['check_id']}]{star} {item['question']}")
        lines.append(f"   Why: {item['why_matters']}")
        lines.append("")
    return "\n".join(lines)


def render_flowchart_mmd() -> str:
    lines = ["flowchart TD"]
    for node in FLOW_NODES:
        safe = node["label"].replace('"', "'")
        if node["kind"] == "decision":
            rendered = '{' + f'"{safe}"' + '}'
        elif node["kind"] in ("terminal_pay", "terminal_stop"):
            rendered = f'(["{safe}"])'
        elif node["kind"] == "wait":
            rendered = f'[/"{safe}"/]'
        else:
            rendered = f'["{safe}"]'
        lines.append(f'    {node["node_id"]}{rendered}')
    for edge in FLOW_EDGES:
        lines.append(f'    {edge["from_node"]} -->|{edge["label"]}| {edge["to_node"]}')
    return "\n".join(lines) + "\n"


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def render_checklist_pdf() -> bytes:
    """Minimal valid PDF (stdlib only) carrying the 16-item checklist."""
    title = "Nifty purchase checklist - 16 items (must-have marked *)"
    lines = [title, "Pay only on written 2000 confirmation. See nifty-purchase-pack.md.", ""]
    for i, item in enumerate(CHECKLIST, 1):
        star = " *" if item["must_have"] else ""
        lines.append(f"{i}. [{item['check_id']}]{star} {item['question']}")
        lines.append(f"   Why: {item['why_matters']}")
        lines.append("")
    pages: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        current.append(line)
        if len(current) >= 40:
            pages.append(current)
            current = []
    if current:
        pages.append(current)
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    page_ids = []
    content_ids = []
    for page_lines in pages:
        stream_lines = ["BT /F1 10 Tf 40 800 Td 14 TL"]
        for line in page_lines:
            safe = _pdf_escape(line)[:140]
            stream_lines.append(f"({safe}) Tj T*")
        stream_lines.append("ET")
        stream = ("\n".join(stream_lines)).encode("latin-1", "replace")
        content_ids.append(add(b"<< /Length " + str(len(stream)).encode()
                               + b" >>\nstream\n" + stream + b"\nendstream"))
        page_ids.append(None)  # placeholder, filled after font/catalogue
    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    # Page objects reference font_id; build them now (ids shift-safe).
    for idx, content_id in enumerate(content_ids):
        page_ids[idx] = add(
            b"<< /Type /Page /Parent 0 0 R /MediaBox [0 0 612 842] "
            b"/Resources << /Font << /F1 " + str(font_id).encode()
            + b" 0 R >> >> /Contents " + str(content_id).encode() + b" 0 R >>")
    kids = b" ".join(str(pid).encode() + b" 0 R" for pid in page_ids)
    pages_id = add(b"<< /Type /Pages /Kids [" + kids + b"] /Count "
                   + str(len(page_ids)).encode() + b" >>")
    # Patch parent references (objects are bytes; rebuild page bodies).
    for idx, pid in enumerate(page_ids):
        objects[pid - 1] = (
            b"<< /Type /Page /Parent " + str(pages_id).encode()
            + b" 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 "
            + str(font_id).encode() + b" 0 R >> >> /Contents "
            + str(content_ids[idx]).encode() + b" 0 R >>")
    catalog_id = add(b"<< /Type /Catalog /Pages " + str(pages_id).encode() + b" 0 R >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


def estimate_gap_size(start: date = GAP_START, end: date = GAP_END,
                      rows50: int = 50, rows500: int = 500,
                      trading_days: int = 252, bytes_per_row: int = 140) -> dict:
    """Legacy common-subset arithmetic, NOT an estimate of the complete request.

    Monthly rows are exact arithmetic from the stated constituents;
    daily rows assume ``trading_days`` per year and are labelled an
    estimate. Cost is deliberately absent: no public price exists.
    """
    if start > end:
        raise ValueError("gap start is after gap end")
    if rows50 <= 0 or rows500 <= 0 or trading_days <= 0 or bytes_per_row <= 0:
        raise ValueError("size assumptions must be positive")
    months = (end.year - start.year) * 12 + (end.month - start.month) + 1
    years = months / 12
    monthly_rows = months * (rows50 + rows500)
    daily_rows = round(years * trading_days * (rows50 + rows500))
    return {
        "method": ("months x (rows50+rows500) for monthly; "
                   "years x trading_days x (rows50+rows500) for daily; "
                   "CSV size = rows x bytes_per_row, uncompressed. "
                   "Vendor to confirm actual cadence, rows and size."),
        "months": months,
        "rows_per_month_nifty50": rows50,
        "rows_per_month_nifty500": rows500,
        "monthly_rows_estimate": monthly_rows,
        "trading_days_per_year_assumed": trading_days,
        "daily_rows_estimate": daily_rows,
        "bytes_per_row_assumed": bytes_per_row,
        "csv_size_mb_estimate": round(daily_rows * bytes_per_row / 1_000_000, 1),
        "note": ("Legacy 2000-2007 common-subset arithmetic only; excludes "
                 "January 2008, Nifty 500's longer gap and post-baseline "
                 "exact effective history. NOT the size of the full request "
                 "or an interval-row estimate. Not a quote; price remains "
                 "enquiry_required (null)."),
    }


def decide_purchase(*, written_2000_confirmation: bool,
                    earliest_date: str | None,
                    gap_report_received: bool,
                    sample_verified: bool,
                    licence_confirmed: bool) -> dict:
    """Pay only on written 2000 confirmation (+ all later gates)."""
    reasons: list[str] = []
    if not written_2000_confirmation:
        reasons.append("no written 2000 confirmation (auto-reply does not count)")
    if earliest_date is not None:
        try:
            if not isinstance(earliest_date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", earliest_date):
                raise ValueError("strict ISO date required")
            earliest = date.fromisoformat(earliest_date)
        except ValueError:
            reasons.append("earliest_date is not ISO YYYY-MM-DD")
        else:
            if earliest > GAP_START:
                reasons.append(f"earliest coverage {earliest} starts after 2000-01-01")
    elif written_2000_confirmation:
        reasons.append("written confirmation without a stated earliest date")
    if not gap_report_received:
        reasons.append("gap report not received")
    if not sample_verified:
        reasons.append("gap-only sample not verified")
    if not licence_confirmed:
        reasons.append("local-research licence not confirmed in writing")
    return {"pay_allowed": not reasons,
            "verdict": "PAY_ALLOWED" if not reasons else "DO_NOT_PAY",
            "reasons": reasons}


def _parse_iso(value: object) -> date | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def validate_email(recipient: str, subject: str, body: str) -> None:
    if recipient != RECIPIENT or not EMAIL_RE.fullmatch(recipient):
        raise ValueError("recipient must be indices@nseids.co.in")
    if not isinstance(subject, str) or SUBJECT not in subject:
        raise ValueError("subject must be the final enquiry subject")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("email body must be a non-empty string")
    low = unicodedata.normalize("NFC", body).lower()
    missing = [term for term in REQUIRED_BODY_TERMS if term not in low]
    if missing:
        raise ValueError(f"email body must cover {missing}")
    if any(term not in body for term in (
            "2000-01-01", "2008-01-30", "2013-04-17", "2026-11-07")):
        raise ValueError("email body must state both per-index missing windows and full scope")
    if "snapshots are not continuous" not in low:
        raise ValueError("email body must distinguish snapshots from continuous effective history")


def validate_eml_bytes(data: bytes) -> None:
    msg = message_from_bytes(data)
    if msg.get("To") != RECIPIENT:
        raise ValueError("eml To must be indices@nseids.co.in")
    payload = msg.get_payload(decode=True)
    body = payload.decode("utf-8") if payload else ""
    validate_email(str(msg.get("To")), str(msg.get("Subject", "")), body)


def validate_gap_request(req: dict) -> None:
    if not isinstance(req, dict) or set(req) != set(GAP_FIELDS):
        raise ValueError(f"gap_request fields must be exactly {sorted(GAP_FIELDS)}")
    if req["index_codes"] != ["NIFTY50", "NIFTY500"]:
        raise ValueError("gap_request must cover NIFTY50 and NIFTY500")
    if _parse_iso(req["start"]) != GAP_START:
        raise ValueError("gap_request start must be 2000-01-01")
    if _parse_iso(req["end"]) != GAP_END:
        raise ValueError("legacy common-subset end must be 2007-12-31; see missing_windows for actual scope")
    if req["missing_windows"] != MISSING_WINDOWS:
        raise ValueError("missing_windows must include January 2008 and Nifty 500 through 2013-04-17")
    if req["exact_effective_history"] != EXACT_EFFECTIVE_HISTORY:
        raise ValueError("exact_effective_history must include post-baseline intervals; snapshots are not continuous")
    samples = req["sample_dates"]
    if not isinstance(samples, list) or len(samples) < 2:
        raise ValueError("gap_request needs at least two sample dates")
    for item in samples:
        parsed = _parse_iso(item)
        if parsed is None or not (GAP_START <= parsed <= GAP_END):
            raise ValueError("sample dates must lie inside the 2000-2007 gap")
    if req["include_post_baseline_repurchase"] is not False:
        raise ValueError("do not reprice duplicate snapshots; exact effective-date intervals remain required")
    if req["cost_public"] is not None:
        raise ValueError("cost_public must be null: no public price is known")
    if req["cost_status"] != "enquiry_required":
        raise ValueError("cost_status must be enquiry_required")


def validate_estimate(est: dict) -> None:
    if not isinstance(est, dict) or set(est) != set(ESTIMATE_FIELDS):
        raise ValueError(f"size_estimate fields must be exactly {sorted(ESTIMATE_FIELDS)}")
    expected = estimate_gap_size()
    if est != expected:
        raise ValueError("size_estimate must equal estimate_gap_size() for the 2000-2007 gap")
    if est["months"] != 96 or est["monthly_rows_estimate"] != 52800:
        raise ValueError("96-month gap must estimate 52,800 monthly rows")


def validate_checklist(items: object) -> None:
    if not isinstance(items, list) or len(items) != 16:
        raise ValueError("checklist must contain exactly the 16-item checklist")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != set(CHECKLIST_FIELDS):
            raise ValueError(f"checklist fields must be exactly {sorted(CHECKLIST_FIELDS)}")
        if not isinstance(item["check_id"], str) or not SLUG_RE.fullmatch(item["check_id"]):
            raise ValueError("check_id must be a lowercase slug")
        if item["check_id"] in seen:
            raise ValueError("duplicate check_id")
        seen.add(item["check_id"])
        for field in ("question", "why_matters"):
            if not isinstance(item[field], str) or not item[field].strip():
                raise ValueError(f"checklist {field} must be a non-empty string")
        if not isinstance(item["must_have"], bool):
            raise ValueError("checklist must_have must be a boolean")
    if sum(1 for i in items if i["must_have"]) < 10:
        raise ValueError("checklist needs at least 10 must-have items")


def validate_flowchart(flow: dict) -> None:
    if not isinstance(flow, dict) or set(flow) != set(FLOWCHART_FIELDS):
        raise ValueError(f"decision_flowchart fields must be exactly {sorted(FLOWCHART_FIELDS)}")
    nodes, edges = flow["nodes"], flow["edges"]
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("flowchart nodes/edges must be lists")
    ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or set(node) != set(NODE_FIELDS):
            raise ValueError(f"flowchart node fields must be exactly {sorted(NODE_FIELDS)}")
        if node["node_id"] in ids:
            raise ValueError("duplicate flowchart node_id")
        ids.add(node["node_id"])
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) != set(EDGE_FIELDS):
            raise ValueError(f"flowchart edge fields must be exactly {sorted(EDGE_FIELDS)}")
        if edge["from_node"] not in ids or edge["to_node"] not in ids:
            raise ValueError("flowchart edge references an unknown node")
    if "pay_allowed" not in ids or "do_not_pay" not in ids or "written_2000" not in ids:
        raise ValueError("flowchart needs pay, do-not-pay and written-2000 nodes")
    # Pay must be guarded: the only incoming edge is from licence_ok,
    # which is itself downstream of written_2000. No direct path.
    incoming = [e["from_node"] for e in edges if e["to_node"] == "pay_allowed"]
    if incoming != ["licence_ok"]:
        raise ValueError("pay_allowed must be reachable only via licence_ok")
    if any(e["to_node"] == "pay_allowed" and e["from_node"] in ("send_enquiry", "await_reply")
           for e in edges):
        raise ValueError("no direct edge from enquiry to payment")
    chain = ["written_2000", "gap_report", "sample_verified", "licence_ok", "pay_allowed"]
    for a, b in zip(chain, chain[1:]):
        if not any(e["from_node"] == a and e["to_node"] == b for e in edges):
            raise ValueError(f"flowchart gate chain broken between {a} and {b}")
    if not isinstance(flow["mermaid"], str) or "written_2000" not in flow["mermaid"]:
        raise ValueError("flowchart mermaid must name the written-2000 gate")
    if not isinstance(flow["pay_guard"], str) or "written" not in flow["pay_guard"]:
        raise ValueError("flowchart pay_guard must state the written-confirmation rule")


def build_pack(generated_at_utc: str) -> dict:
    return {
        "generated_at_utc": generated_at_utc,
        "recipient": RECIPIENT,
        "subject": SUBJECT,
        "email_body": EMAIL_BODY,
        "gap_request": {
            "index_codes": ["NIFTY50", "NIFTY500"],
            "start": GAP_START.isoformat(),
            "end": GAP_END.isoformat(),
            "sample_dates": list(SAMPLE_DATES),
            "include_post_baseline_repurchase": False,
            "cost_public": None,
            "cost_status": "enquiry_required",
            "missing_windows": {index: dict(window) for index, window in MISSING_WINDOWS.items()},
            "exact_effective_history": dict(EXACT_EFFECTIVE_HISTORY),
        },
        "size_estimate": estimate_gap_size(),
        "checklist": [dict(item) for item in CHECKLIST],
        "decision_flowchart": {
            "nodes": [dict(n) for n in FLOW_NODES],
            "edges": [dict(e) for e in FLOW_EDGES],
            "mermaid": render_flowchart_mmd(),
            "pay_guard": PAY_GUARD,
        },
        "notes": {
            "method": ("Built from data/reports/nifty-vendor-research.md "
                       "(9 vendors, enquiry template, 16-item checklist) and "
                       "data/reports/nifty-doc-research.md documentary "
                       "baselines (Nifty 50 2008-01-31, Nifty 500 2013-04-18)."),
            "no_email_sent": True,
            "no_purchase_made": True,
            "public_baselines": {"NIFTY50": "2008-01-31", "NIFTY500": "2013-04-18"},
            "gap_months": 96,
            "legacy_fields_note": ("gap_request.start/end/sample_dates and gap_months/size_estimate "
                                   "describe only the legacy 2000-2007 common subset. "
                                   "missing_windows and exact_effective_history are authoritative. "
                                   "include_post_baseline_repurchase=false excludes duplicate "
                                   "snapshot delivery only, NOT exact effective-date intervals. "
                                   "The legacy diagram's 2000-2007 report/sample labels describe "
                                   "a minimum subset only; pay_guard requires the complete scope."),
            "cost_note": ("No current quote is verified. cost_public is null; "
                          "coverage, licence and itemised price require written confirmation."),
            "raw_files": [
                "data/raw/nifty_purchase/enquiry_indices_nseids.eml",
                "data/raw/nifty_purchase/enquiry_indices_nseids.txt",
                "data/raw/nifty_purchase/sample_request_2000-2008.txt",
                "data/raw/nifty_purchase/checklist.md",
                "data/raw/nifty_purchase/checklist.pdf",
                "data/raw/nifty_purchase/decision_flowchart.mmd",
            ],
        },
        "tests": {"command": "uv run python -m unittest tests.test_purchase -v",
                  "coverage_file": "data/reports/nifty-purchase-pack.json",
                  "report_file": "data/reports/nifty-purchase-pack.md",
                  "module": "src/nse_pipeline/nifty/purchase.py"},
    }


def validate_pack(payload: dict) -> None:
    """Fail closed on the whole purchase-pack payload."""
    if not isinstance(payload, dict) or set(payload) != set(TOP_FIELDS):
        raise ValueError(f"top-level fields must be exactly {sorted(TOP_FIELDS)}")
    try:
        parsed = datetime.fromisoformat(payload["generated_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("generated_at_utc is not ISO-8601") from exc
    if parsed.utcoffset() is None:
        raise ValueError("generated_at_utc needs a timezone")
    validate_email(payload["recipient"], payload["subject"], payload["email_body"])
    validate_gap_request(payload["gap_request"])
    validate_estimate(payload["size_estimate"])
    validate_checklist(payload["checklist"])
    validate_flowchart(payload["decision_flowchart"])
    notes = payload["notes"]
    if not isinstance(notes, dict) or notes.get("no_email_sent") is not True \
            or notes.get("no_purchase_made") is not True:
        raise ValueError("notes must record that no email was sent and nothing bought")
    if not isinstance(payload["tests"], dict) or not payload["tests"].get("command"):
        raise ValueError("tests.command is required")


def write_pack(payload: dict, path: Path) -> Path:
    """Validate then write a new pack JSON without overwriting."""
    validate_pack(payload)
    if path.exists():
        raise FileExistsError(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as target:
        json.dump(payload, target, indent=2, ensure_ascii=False)
        target.write("\n")
    return path


def load_pack(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_pack(payload)
    return payload


def write_raw_files(root: Path) -> list[Path]:
    """Write the ready-to-send raw artifacts (never overwrites)."""
    raw = root / "data" / "raw" / "nifty_purchase"
    raw.mkdir(parents=True, exist_ok=True)
    files = {
        "enquiry_indices_nseids.eml": render_eml(),
        "enquiry_indices_nseids.txt": render_email_txt().encode("utf-8"),
        "sample_request_2000-2008.txt": render_sample_request().encode("utf-8"),
        "checklist.md": render_checklist_md().encode("utf-8"),
        "checklist.pdf": render_checklist_pdf(),
        "decision_flowchart.mmd": render_flowchart_mmd().encode("utf-8"),
    }
    written = []
    for name, data in files.items():
        target = raw / name
        if target.exists():
            raise FileExistsError(str(target))
        target.write_bytes(data)
        written.append(target)
    return written
