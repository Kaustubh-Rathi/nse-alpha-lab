"""Reason-conditioned delisting returns, following Shumway (1997).

The problem
-----------
When a held security stops trading, the last observed close is **not** an
achievable exit price. Shumway (1997, *Journal of Finance* 52(1) 327-340) shows
that roughly two-thirds of performance-related delistings are surprises, and the
cumulative abnormal return one day after the announcement is about -14%. The
information arrives at or after the last trade, so a backtest that exits at the
last close is systematically optimistic.

Shumway's evidence, and why one haircut is wrong
------------------------------------------------
Delisting returns are not homogeneous. From his Table I (CRSP 1962-1993):

* NYSE/AMEX **merger** delistings: 98.9% have a recorded return, mean **+1.94%**
* NYSE/AMEX **exchange transfer**: 98.2% recorded, mean **-0.74%**
* NYSE/AMEX **performance** delistings: only **11.7%** recorded, mean **-41.56%**
* Nasdaq **performance** delistings: **0.0%** recorded

So the missing-return problem is concentrated almost entirely in performance
delistings, and it is far worse on Nasdaq than on NYSE/AMEX. Shumway's
recommendations (p. 340) are therefore conditioned on the delisting reason:

* replace **missing performance** delisting returns with **-30%** (his Table V
  population estimate is -29.9% at 71.3% coverage)
* **-100%** is the stated upper bound on the bias
* Shumway & Warther (1999) put the Nasdaq equivalent at **-55%**, because the
  Nasdaq bias was 4.7x larger

The bias is also strongly size-dependent. Shumway's Table VII shows a -100%
treatment cuts the smallest size decile's annualised return by 5.21% and the
largest decile by 0.00%. A mid/small-cap momentum strategy is squarely in the
danger zone.

What this module does
---------------------
It maps an NSE delisting reason to a haircut, and it refuses to invent one when
the reason is unknown. The mapping is an explicit, cited modelling assumption,
recorded on every affected trade, never a silent default.

NSE publishes the reason in ``data/raw/security_master/delisted.csv`` with three
values: ``Compulsory Delisting``, ``Delisting - Liquidation`` and
``Voluntary Delisting``. The mapping below is a judgement call, and it is
documented as one:

* **Liquidation** -> total loss. A company in liquidation distributes nothing to
  equity holders in the ordinary case. This is the closest NSE analogue to
  Shumway's "worthless firms" (84 of his 1,029 performance delists).
* **Compulsory** -> the -30% performance haircut. Compulsory delisting is the
  exchange acting against the issuer for non-compliance or failure; it is the
  NSE analogue of a performance delisting.
* **Voluntary** -> no haircut. A voluntary delisting in India runs through
  reverse book building, which pays shareholders an exit price at or above a
  SEBI-set floor. The last traded close is a *conservative* proxy for that exit,
  not an optimistic one.

Every one of those three is an assumption. None is a measured Indian delisting
return distribution, which does not exist in this repository.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal(0)
ONE = Decimal(1)

#: Haircut applied to the last observed close, by NSE delisting reason.
#: A value of ``None`` means the reason is unrecognised and no haircut is applied
#: only if the caller explicitly allows it; otherwise the position is refused.
REASON_HAIRCUTS: dict[str, Decimal] = {
    "liquidation": Decimal("1.00"),
    "compulsory": Decimal("0.30"),
    "voluntary": Decimal("0.00"),
}

#: Normalised aliases for the raw NSE strings.
_REASON_ALIASES = {
    "delisting - liquidation": "liquidation",
    "liquidation": "liquidation",
    "compulsory delisting": "compulsory",
    "compulsory": "compulsory",
    "voluntary delisting": "voluntary",
    "voluntary": "voluntary",
}

CITATIONS = {
    "liquidation": ("Equity holders in liquidation ordinarily receive nothing; "
                    "Shumway (1997) Table V line 3 records 84 of 1,029 performance "
                    "delists as worthless (-100%)."),
    "compulsory": ("Shumway (1997) p. 340 recommends -30% for missing "
                   "performance-related delisting returns (population estimate "
                   "-29.9%, Table V line 5)."),
    "voluntary": ("Indian voluntary delisting runs through reverse book building "
                  "at or above a SEBI-set floor price, so the last traded close is "
                  "a conservative proxy rather than an optimistic one."),
}


@dataclass(frozen=True)
class DelistingTerms:
    """The resolved treatment for one delisted security."""

    reason: str
    haircut: Decimal
    exit_multiplier: Decimal
    citation: str

    def exit_price(self, last_close: Decimal) -> Decimal:
        """The modelled exit price for a given last observed close."""
        if not isinstance(last_close, Decimal) or last_close <= ZERO:
            raise ValueError("last_close must be a positive Decimal")
        return last_close * self.exit_multiplier


def normalise_reason(raw: str | None) -> str | None:
    """Map a raw NSE delisting reason to a canonical key, or ``None``."""
    if not raw:
        return None
    return _REASON_ALIASES.get(str(raw).strip().lower())


def terms_for(raw_reason: str | None) -> DelistingTerms | None:
    """Resolve delisting terms for a raw NSE reason string.

    Returns ``None`` when the reason is unrecognised. The caller must then
    decide explicitly: refuse the position, or record that no haircut was
    applied because the reason was unknown. This function never guesses.
    """
    reason = normalise_reason(raw_reason)
    if reason is None:
        return None
    haircut = REASON_HAIRCUTS[reason]
    return DelistingTerms(
        reason=reason,
        haircut=haircut,
        exit_multiplier=ONE - haircut,
        citation=CITATIONS[reason],
    )


def load_delisting_register(path) -> dict[str, dict]:
    """Read NSE's delisted-securities register into ``{symbol: record}``.

    The file has columns ``Symbol``, ``Company``, ``Delisted Date`` and
    ``Type of Delisting``. A symbol appearing more than once keeps its earliest
    record, because a symbol that was delisted and later reused belongs to a
    different security and the first delisting is the one that matters.
    """
    import csv
    from datetime import date, datetime
    from pathlib import Path

    path = Path(path)
    if not path.exists():
        return {}

    def parse_date(value: str):
        for fmt in ("%d-%b-%y", "%d-%b-%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except (ValueError, AttributeError):
                continue
        return None

    register: dict[str, dict] = {}
    # NSE ships this file as cp1252 with a UTF-8 BOM; a strict utf-8 read raises
    # on the 0xA0 non-breaking spaces inside company names.
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as stream:
        for row in csv.DictReader(stream):
            symbol = (row.get("Symbol") or "").strip().upper()
            if not symbol:
                continue
            record = {
                "symbol": symbol,
                "company": (row.get("Company") or "").strip(),
                "delisted_date": parse_date(row.get("Delisted Date") or ""),
                "reason_raw": (row.get("Type of Delisting") or "").strip(),
                "reason": normalise_reason(row.get("Type of Delisting")),
            }
            existing = register.get(symbol)
            if existing is None:
                register[symbol] = record
            elif record["delisted_date"] and existing["delisted_date"]:
                if record["delisted_date"] < existing["delisted_date"]:
                    register[symbol] = record
    return register
