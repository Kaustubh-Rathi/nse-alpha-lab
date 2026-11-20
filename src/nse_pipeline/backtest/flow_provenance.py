"""Replay-verifiable binding between an external-flow citation and its source digest.

The kernel records a canonical digest over the flow identity, amount, direction,
citation, event time and an optional caller-supplied source digest. Replay can
recompute that binding from the ledger event alone, so a locally substituted
citation or source digest fails closed instead of silently passing as a different
"evidenced" flow.

Boundary: this is an integrity binding, not authentication. A coordinated forger
who replaces the citation *and* the source digest *and* recomputes the binding can
produce a self-consistent event. Proving that a source digest belongs to an
owner-approved bank/source anchor requires a trusted external anchor outside this
module; replay cannot invent one.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from .accounting_types import EventTime, sha256_digest

FLOW_CITATION_SCHEME = "nse-external-flow-citation-v1"


def external_flow_citation_digest(flow_id, amount, direction, citation, at,
                                  source_digest="") -> str:
    """Return the canonical lowercase SHA-256 binding for one external flow.

    ``source_digest`` is optional for backward compatibility. When supplied it
    must already be a validated lowercase 64-character hex digest; replay binds
    it to the recorded citation and flow facts.
    """
    if not isinstance(flow_id, str) or not flow_id.strip():
        raise ValueError("External-flow digest requires a nonempty stable flow identifier")
    if type(amount) is not Decimal or not amount.is_finite() or amount <= 0:
        raise ValueError("External-flow digest requires a positive finite Decimal amount")
    if direction not in ("deposit", "withdrawal"):
        raise ValueError("External-flow digest requires a known direction")
    if not isinstance(citation, str) or not citation.strip():
        raise ValueError("External-flow digest requires a nonempty citation")
    if not isinstance(at, EventTime):
        raise ValueError("External-flow digest requires a typed event time")
    source_digest = "" if source_digest is None else source_digest
    if source_digest:
        sha256_digest(source_digest, "External-flow source digest")
    document = {
        "scheme": FLOW_CITATION_SCHEME,
        "flow_id": flow_id,
        "amount": format(amount, "f"),
        "direction": direction,
        "citation": citation,
        "session": at.session.isoformat(),
        "phase": at.phase.name,
        "sequence": at.sequence,
        "source_digest": source_digest,
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
