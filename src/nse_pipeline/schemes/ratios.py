"""Fail-closed scheme-ratio extractor: parse explicit swap/demerger terms from PDFs.

Only stdlib is required. ``pypdf`` is imported lazily and is supplied by the
caller (e.g. ``uv run --with pypdf python ...``); this module never installs
or vendors it. If ``pypdf`` is unavailable the extractor fails closed and
reports ``pdf_text_extract_unavailable`` instead of guessing.

Terms are admitted ONLY from an explicit textual construction in the PDF
body, e.g.::

    "one fully paid-up Equity Share ... for every one fully paid-up
     Equity Share held"

A ratio/cash term is returned only with a page citation and a verbatim
snippet. If no such construction exists (scanned PDF, OCR failure, a
filing that names the scheme but not its terms, ...) the record is
``no_explicit_ratio`` with null terms. Terms are NEVER inferred from a
price move, return, discount/premium, or announcement-window evidence;
``validate_ratio_record`` rejects any price-derived field or
``extraction_source`` mentioning price/return/discontinuity.

No network access. Does not write to the host DB; it emits ``ratios.jsonl``
candidates for human verification against the cited page.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from nse_pipeline.paths import data_root

# --------------------------------------------------------------------------
# Number-word handling (schemes often spell "One (1) equity share")
# --------------------------------------------------------------------------
_WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "twenty-five": 25, "fifty": 50, "hundred": 100,
}
_NUM_TOKEN = (
    r"(?:(\d[\d,]*)\s*(?:\(\s*\d[\d,]*\s*\))?"
    r"|(?:\(\s*(\d[\d,]*)\s*\))"
    r"|(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"fifteen|twenty(?:[- ]five)?|fifty|hundred))"
)


def _parse_token_text(token_text: str) -> int | None:
    """Parse one ``_NUM_TOKEN``-shaped substring ('1', 'One (1)', 'one')."""
    text = (token_text or "").strip()
    m = re.fullmatch(r"(\d[\d,]*)\s*\(\s*\d[\d,]*\s*\)", text)
    if m:
        return int(m.group(1).replace(",", ""))
    m = re.fullmatch(r"\(\s*(\d[\d,]*)\s*\)", text)
    if m:
        return int(m.group(1).replace(",", ""))
    m = re.fullmatch(r"(\d[\d,]*)", text)
    if m:
        return int(m.group(1).replace(",", ""))
    key = text.lower().replace(" ", "-")
    return _WORD_NUMBERS.get(key, _WORD_NUMBERS.get(key.replace("-", " ")))


def _parse_number_token(m: re.Match) -> int | None:
    """Parse the digit-or-word token captured by ``_NUM_TOKEN``."""
    return _parse_token_text(m.group("num"))


def _norm_ws(text: str) -> str:
    """Collapse all whitespace (PDF line breaks, hyphen joins where safe)."""
    text = (text or "").replace(" ", " ")
    # Join words split by end-of-line hyphens only before a lower-case
    # continuation; anything else is a normal space.
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------
# Swap / demerger ratio patterns
# --------------------------------------------------------------------------
# "X share(s) ... for every Y share(s)" is the canonical NCLT construction.
_RATIO_FOR_EVERY = re.compile(
    r"(?P<num>" + _NUM_TOKEN + r")\s+(?:fully paid[- ]up\s+)?"
    r"(?:equity\s+)?shares?\s+to\s+be\s+issued[^.]{0,300}?"
    r"for every\s+(?P<den>" + _NUM_TOKEN + r")\s+"
    r"(?:fully paid[- ]up\s+)?(?:equity\s+)?shares?",
    re.I,
)
_RATIO_BARE_FOR_EVERY = re.compile(
    r"(?P<num>" + _NUM_TOKEN + r")\s+(?:fully paid[- ]up\s+)?"
    r"(?:equity\s+)?shares?\s+for every\s+(?P<den>" + _NUM_TOKEN + r")\s+"
    r"(?:fully paid[- ]up\s+)?(?:equity\s+)?shares?",
    re.I,
)
_RATIO_EACH = re.compile(
    r"(?P<num>" + _NUM_TOKEN + r")\s+(?:fully paid[- ]up\s+)?"
    r"(?:equity\s+)?share[s]?\s+of\s+[A-Z][^.,]{0,80}?"
    r"for each\s+(?P<den>" + _NUM_TOKEN + r")\s+"
    r"(?:fully paid[- ]up\s+)?(?:equity\s+)?share",
    re.I,
)
_RATIO_COLON = re.compile(
    r"(?:exchange\s+ratio|swap\s+ratio|share\s+ratio)\s*(?:of|:|=|is|was)?\s*"
    r"(\d[\d,]*)\s*[:/]\s*(\d[\d,]*)",
    re.I,
)
_RATIO_BARE_COLON = re.compile(r"\bratio\s+(\d+)\s*:\s*(0?\d+)\b", re.I)

# Cash terms: "Rs. 25 per equity share", "cash consideration of Rs 10",
# "payment of Rs. 5/- per share". The ₹ symbol appears in some PDFs.
_CASH = re.compile(
    r"(?:cash\s+(?:consideration|payment)|payment\s+of|consideration\s+of)?"
    r"\s*(?:Rs\.?|Rupees?|INR\s*|₹)\s*(?P<amount>\d[\d,]*(?:\.\d{1,2})?)"
    r"(?:\s*(?:/-)?\s*(?:per|for each)\s+(?:fully paid[- ]up\s+)?"
    r"(?:equity\s+)?shares?)?",
    re.I,
)
_CASH_CONTEXT = re.compile(
    r"(cash|cash(?:[- ]out)?\s+(?:consideration|payment)|"
    r"exit\s+(?:price|proceeds)|buy[- ]out|"
    r"consideration\s+(?:of|shall)|payment\s+of|per\s+(?:equity\s+)?share)",
    re.I,
)
_FRACTIONAL = re.compile(
    r"fractional\s+(?:entitlement|shareholding|shares?)", re.I)

_COMPANY = re.compile(
    r"\b([A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Za-z0-9&.\-]+){0,4}?"
    r"\s+(?:Limited|Ltd\.?|Private Limited|Inc\.?|Pty Ltd|LLP|PLC))\b")
_ROLE_LABEL = re.compile(
    r"(resulting|transferee|survivor|acquiring|acquirer)[^\n]{0,120}?"
    r"company", re.I)
_ISSUE_BY = re.compile(
    r"issue\s+(?:and\s+allot[^.]{0,300}?by\s+)"
    r"(?P<company>[A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Za-z0-9&.\-]+){0,4}?"
    r"\s+(?:Limited|Ltd\.?|Private Limited))", re.I)

BANNED_PRICE_FIELDS = {
    "price_factor", "share_multiplier", "inferred_ratio",
    "inferred_from_price", "price_discontinuity_ratio",
    "implied_ratio_from_return", "raw_return_used",
}


def _clean_company(name: str) -> str:
    tokens = name.split()
    cut = 0
    for i, tok in enumerate(tokens):
        if tok.isalpha() and tok.islower():
            cut = i + 1
    tokens = tokens[cut:]
    while tokens and tokens[0].lower().rstrip("-") in ("company", "the"):
        tokens.pop(0)
    return " ".join(tokens) or name.strip()


# --------------------------------------------------------------------------
# PDF text
# --------------------------------------------------------------------------
def extract_pages(pdf_path: Path | str) -> list[str]:
    """Return per-page raw text; raises ValueError if unreadable (fail-closed)."""
    path = Path(pdf_path)
    if not path.exists():
        raise ValueError(f"PDF not found: {path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Not a PDF (wrong suffix): {path.name}")
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError as error:  # pragma: no cover - environment dependent
        raise ValueError(
            "pypdf unavailable; run with `uv run --with pypdf ...`") from error
    reader = PdfReader(str(path))
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # a single bad page must not tank the record
            pages.append("")
    return pages


def extract_pages_from_bytes(data: bytes) -> list[str]:
    """Same as ``extract_pages`` but from raw bytes (tests / cached blobs)."""
    import io

    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError as error:
        raise ValueError(
            "pypdf unavailable; run with `uv run --with pypdf ...`") from error
    reader = PdfReader(io.BytesIO(data))
    return [page.extract_text() or "" for page in reader.pages]


# --------------------------------------------------------------------------
# Term parsing
# --------------------------------------------------------------------------
def _find_ratio(norm_pages: list[str]):
    """Return (num, den, page_index0, snippet, kind) or None."""
    # Order: explicit "for every" (canonical), then colon formulations.
    for kind, pattern in (
        ("shares_for_every", _RATIO_FOR_EVERY),
        ("shares_for_every", _RATIO_BARE_FOR_EVERY),
        ("shares_for_each", _RATIO_EACH),
        ("colon_exchange_ratio", _RATIO_COLON),
        ("colon_ratio", _RATIO_BARE_COLON),
    ):
        for idx, text in enumerate(norm_pages):
            m = pattern.search(text)
            if not m:
                continue
            if kind.startswith("colon"):
                num = int(m.group(1).replace(",", ""))
                den = int(m.group(2).replace(",", ""))
            else:
                num = _parse_token_text(m.group("num"))
                den = _parse_token_text(m.group("den"))
                if num is None or den is None:
                    continue
            if num <= 0 or den <= 0:
                continue
            snippet = text[max(0, m.start() - 40): m.end() + 80].strip()
            return num, den, idx, snippet, kind
    return None


def _find_all_distinct_ratios(norm_pages: list[str]) -> set[tuple[int, int]]:
    found: set[tuple[int, int]] = set()
    for pattern in (_RATIO_FOR_EVERY, _RATIO_BARE_FOR_EVERY, _RATIO_EACH):
        for text in norm_pages:
            for m in pattern.finditer(text):
                num = _parse_token_text(m.group("num"))
                den = _parse_token_text(m.group("den"))
                if num is not None and den is not None and num > 0 and den > 0:
                    found.add((num, den))
    for text in norm_pages:
        for m in _RATIO_COLON.finditer(text):
            found.add((int(m.group(1)), int(m.group(2))))
    return found


def _find_cash(norm_pages: list[str]):
    """Return (cash_per_share, snippet, page0) or (None, None, None)."""
    for idx, text in enumerate(norm_pages):
        for m in _CASH.finditer(text):
            amount_text = m.group("amount").replace(",", "")
            try:
                amount = float(amount_text)
            except ValueError:
                continue
            if amount <= 0 or amount > 100000:  # implausible per-share cash
                continue
            start = max(0, m.start() - 160)
            window = text[start: m.end() + 60]
            if not _CASH_CONTEXT.search(window):
                continue
            snippet = text[max(0, m.start() - 40): m.end() + 60].strip()
            return amount, snippet, idx
    return None, None, None


def _find_fractional(norm_pages: list[str]) -> bool:
    return any(_FRACTIONAL.search(t) for t in norm_pages)


def _find_successor_company(norm_pages: list[str]) -> tuple[str | None, str | None]:
    """Best-effort resulting/transferee company name + snippet."""
    best: tuple[str | None, str | None] = (None, None)
    for text in norm_pages:
        m = _ISSUE_BY.search(text)
        if m:
            best = (_clean_company(m.group("company")),
                    text[max(0, m.start() - 40): m.end() + 60].strip())
            continue
        m = _ROLE_LABEL.search(text)
        if m:
            window = text[m.start(): m.end() + 200]
            comp = _COMPANY.search(window)
            if comp:
                best = (_clean_company(comp.group(1)),
                        window.strip()[:200])
    return best


def parse_pages(pages: list[str], pdf_url: str | None = None,
                source_name: str | None = None) -> dict:
    """Extract candidate terms from already-extracted page texts (fail-closed).

    Returns a ratio record. Never raises for content problems: unparsable
    input yields ``status = no_explicit_ratio`` with null terms.
    """
    norm_pages = [_norm_ws(p) for p in pages]
    distinct = _find_all_distinct_ratios(norm_pages)
    record: dict = {
        "ratio_num": None, "ratio_den": None, "cash_per_share": None,
        "fractional_cash": _find_fractional(norm_pages),
        "successor_company_name": None, "successor_symbol": None,
        "page_ref": None, "snippet": None, "ratio_kind": None,
        "pdf_url": pdf_url, "source": source_name,
        "status": "no_explicit_ratio",
        "extraction_source": "pypdf_pdf_text_extractor_v1",
        "ratio_from_price": False,
    }
    if not any(t.strip() for t in norm_pages):
        record["status"] = "scanned_no_text"
        return record
    ratio = _find_ratio(norm_pages)
    cash, cash_snippet, cash_page = _find_cash(norm_pages)
    successor, successor_snippet = _find_successor_company(norm_pages)
    record["successor_company_name"] = successor
    if cash is not None:
        record["cash_per_share"] = cash
    if len(distinct) > 1:
        # Conflicting explicit ratios (multi-step scheme, two legs): admit
        # none of them; a human must disambiguate legs on the cited pages.
        record["status"] = "needs_review_conflicting_ratios"
        record["distinct_ratios"] = sorted(
            [f"{a}:{b}" for a, b in distinct])
        if ratio is not None:
            record["page_ref"] = f"p.{ratio[2] + 1}"
            record["snippet"] = ratio[3]
        return record
    if ratio is not None:
        num, den, page0, snippet, kind = ratio
        record["ratio_num"] = num
        record["ratio_den"] = den
        record["page_ref"] = f"p.{page0 + 1}"
        record["snippet"] = snippet
        record["ratio_kind"] = kind
        record["status"] = "candidate"
        if cash_snippet and cash_page is not None:
            # Prefer the ratio's page as the citation anchor; keep cash
            # evidence in the snippet trail for the verifier.
            record["cash_snippet"] = cash_snippet
            record["cash_page_ref"] = f"p.{cash_page + 1}"
        return record
    if cash is not None:
        # Cash-only buyout: still needs a page citation to be admitted.
        record["page_ref"] = f"p.{cash_page + 1}" if cash_page is not None else None
        record["snippet"] = cash_snippet
        record["ratio_kind"] = "cash_only"
        record["status"] = "candidate"
        return record
    return record


def validate_ratio_record(record: dict) -> dict:
    """Reject price-derived fields and uncited terms (fail-closed)."""
    for banned in BANNED_PRICE_FIELDS:
        if banned in record:
            raise ValueError(f"Banned price-derived field present: {banned}")
    source = str(record.get("extraction_source") or "").lower()
    if "price" in source or "raw_return" in source or "discontinuity" in source:
        raise ValueError("Terms must never be inferred from a price move")
    if record.get("ratio_from_price") is not False:
        raise ValueError("ratio_from_price must be False")
    has_terms = (record.get("ratio_num") is not None
                 or record.get("cash_per_share") is not None)
    if has_terms:
        if not record.get("pdf_url"):
            raise ValueError("Candidate terms require pdf_url citation")
        if not record.get("page_ref"):
            raise ValueError("Candidate terms require page_ref citation")
        if not record.get("snippet"):
            raise ValueError("Candidate terms require a verbatim snippet")
        for v in (record.get("ratio_num"), record.get("ratio_den")):
            if v is not None and (not isinstance(v, int) or v <= 0):
                raise ValueError("ratio_num/ratio_den must be positive ints")
        if (record.get("ratio_num") is None) != (record.get("ratio_den") is None):
            raise ValueError("ratio_num and ratio_den must be set together")
    if record.get("successor_symbol") is not None and not re.match(
            r"^[A-Z0-9.\-]{1,30}$", str(record["successor_symbol"])):
        raise ValueError("successor_symbol must be an exchange symbol")
    return record


def extract_pdf(pdf_path: Path | str, pdf_url: str | None = None) -> dict:
    """Extract a ratio record from a cached PDF (fail-closed)."""
    path = Path(pdf_path)
    if pdf_url is None:
        pdf_url = str(path)  # local-file citation; scheme PDFs use NSE URLs
    try:
        pages = extract_pages(path)
    except ValueError as error:
        return {
            "ratio_num": None, "ratio_den": None, "cash_per_share": None,
            "fractional_cash": None, "successor_company_name": None,
            "successor_symbol": None, "page_ref": None, "snippet": None,
            "ratio_kind": None, "pdf_url": pdf_url,
            "source": f"{path.name}: {error}",
            "status": "pdf_text_extract_unavailable",
            "extraction_source": "pypdf_pdf_text_extractor_v1",
            "ratio_from_price": False,
        }
    record = parse_pages(pages, pdf_url=pdf_url, source_name=path.name)
    try:
        validate_ratio_record(record)
    except ValueError:
        record["status"] = "invalid_record"
    return record


def scan_cached_pdfs(data_root: Path, press_limit: int | None = None) -> list[dict]:
    """Scan cached scheme/press PDFs; returns one record per PDF.

    ``raw/schemes`` is scanned fully (it is empty: scheme filings live only
    as URLs so far). The ``nifty_doc`` press/pdfs corpora hold ~1,800 index
    press PDFs; ``press_limit`` bounds that sample (default 30) so a single
    run stays under a minute. Pass ``press_limit=None`` to scan everything.
    """
    records: list[dict] = []
    schemes_dir = data_root / "raw/schemes"
    if schemes_dir.exists():
        for pdf in sorted(schemes_dir.glob("*.pdf")):
            records.append(extract_pdf(pdf))
    for name in ("press", "pdfs"):
        directory = data_root / f"raw/nifty_doc/{name}"
        if not directory.exists():
            continue
        pdfs = sorted(directory.glob("*.pdf"))
        if press_limit is not None:
            pdfs = pdfs[:press_limit]
        for pdf in pdfs:
            records.append(extract_pdf(pdf))
    return records


def load_ledger_stubs(data_root: Path) -> list[dict]:
    """Load scheme-ledger stubs so uncovered schemes stay visible (PDF URLs)."""
    candidates = [
        data_root / "processed/schemes/ratio-input-ledger.jsonl",
        data_root / "processed/ledger/scheme-ledger.jsonl",
    ]
    for path in candidates:
        if path.exists():
            return [json.loads(line) for line in
                    path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return []


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def summarize(records: list[dict], ledger_rows: int) -> dict:
    candidates = sum(1 for r in records if r.get("status") == "candidate")
    return {
        "pdfs_scanned": len(records),
        "candidate_records": candidates,
        "non_candidate_records": len(records) - candidates,
        "ratios_inferred_from_price": False,
        "pdfs_without_explicit_ratio": sum(
            1 for r in records
            if r.get("status") in ("no_explicit_ratio", "scanned_no_text",
                                   "pdf_text_extract_unavailable")),
        "ledger_stub_rows": ledger_rows,
        "scheme_days_still_need_pdf": 346 - max(ledger_rows, 0) if ledger_rows else 346,
        "limitations": [
            "No scheme PDF bodies are cached locally; pdf_url alone is not a term source",
            "Extractor admits only explicit 'X shares for every Y shares' / exchange-ratio text with page citation",
            "Cash terms require an Rs./cash-context + amount; fractional-share text alone sets no cash amount",
            "successor_symbol is never guessed from a company name",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=data_root())
    parser.add_argument("--press-limit", type=int, default=30,
                        help="PDFs to scan per nifty_doc press/pdfs dir (0 = all)")
    args = parser.parse_args()
    limit = None if args.press_limit == 0 else args.press_limit
    records = scan_cached_pdfs(args.data, press_limit=limit)
    ledger_rows = load_ledger_stubs(args.data)
    summary = summarize(records, len(ledger_rows))
    out_dir = args.data / "processed/schemes/ratios"
    write_jsonl(out_dir / "ratios.jsonl", records)
    write_json(args.data / "reports/scheme-ratios.json", summary)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
