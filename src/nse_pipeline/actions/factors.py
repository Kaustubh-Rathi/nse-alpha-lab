"""Share-count multipliers parsed from raw corporate-action ``purpose`` text.

Closes part of the gap where bonus/split rows already carry a
``share_multiplier`` but rights and face-value consolidations carry only their
ratio inside the free-text ``purpose`` string.

The returned value is a SHARE-COUNT multiplier, matching the semantics of the
existing ``share_multiplier`` on split/bonus rows:

* a rights issue ``N:M`` issues ``N`` new shares per ``M`` held, so the share
  count scales by ``(M+N)/M`` (the subscription premium affects price, not the
  number of shares);
* a face-value consolidation ``RE A TO RS B`` replaces ``A`` shares of face
  value ``A`` with ``A/B`` shares of face value ``B``, so the share count
  scales by ``A/B``.

Both return ``None`` (not a guessed value) when the ratio cannot be read
cleanly, so callers stay fail-closed.
"""
from __future__ import annotations

import re

# "RHTS 2:5 @PREMIUM RS.140" -> ("2", "5"); "RIGHTS 2:5@ PREM RS 75" -> ("2","5").
_RIGHTS = re.compile(r"(\d+)\s*:\s*(\d+)")
# "CONSOLIDATION RE1 TO RS10" -> ("1", "10")  (face value 1 -> 10).
_CONSOL = re.compile(r"RE\s*(\d+(?:\.\d+)?)\s+TO\s+RS\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def rights_share_multiplier(purpose: str) -> float | None:
    """Return the share-count multiplier for a rights issue, or ``None``.

    A clean rights ratio is ``N:M`` preceded only by rights text. Complex
    instruments such as ``1NCD&2WRNTS:8EQSH`` or ``7 Ccps And 7 Warrants:40``
    carry letters/``&`` before the ratio and are rejected.
    """
    text = str(purpose or "")
    m = _RIGHTS.search(text)
    if not m:
        return None
    prefix = text[: m.start()]
    stripped = re.sub(r"RIGHTS|RHTS|RIGHT", "", prefix, flags=re.IGNORECASE)
    if re.search(r"[A-Za-z]", stripped):
        return None
    n, m_ = int(m.group(1)), int(m.group(2))
    if n <= 0 or m_ <= 0:
        return None
    return (m_ + n) / m_


def consolidation_share_multiplier(purpose: str) -> float | None:
    """Return the share-count multiplier for a face-value consolidation.

    ``RE A TO RS B`` means ``A`` shares of face value ``A`` become ``A/B``
    shares of face value ``B``.
    """
    m = _CONSOL.search(str(purpose or ""))
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    if a <= 0 or b <= 0:
        return None
    return a / b
