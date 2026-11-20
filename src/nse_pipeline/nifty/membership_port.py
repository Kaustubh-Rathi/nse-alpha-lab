"""Integrity-bound reviewed-input port for exact-date snapshot membership.

This is an integrity-bound reviewed-input port, not source authentication or
coverage certification. Snapshot metadata and a matching digest are attestations;
they do not prove who published the bytes or that the source was authoritative.
The explicit security count validated by ``nifty.snapshots`` is a caller-reviewed
completeness attestation for a snapshot's declared scope, not independent proof.
No membership intervals or inferred events are reconstructed or applied.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from io import BytesIO
from types import MappingProxyType
from zipfile import BadZipFile, ZipFile

from nse_pipeline.backtest.accounting_types import EventTime
from nse_pipeline.nifty.snapshots import INDEX_CODES, SYMBOL, Snapshot, validate


class CoverageUnknown(ValueError):
    """Requested membership is not known from an admitted exact-date snapshot."""


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class AvailabilityEvidence:
    """Separate reviewer attestation of when bound source material was available."""

    index_code: str
    as_of: date
    available_at: EventTime
    citation: str
    basis: str
    source_sha256: str

    def __post_init__(self):
        if (self.index_code not in INDEX_CODES or type(self.as_of) is not date
                or not isinstance(self.available_at, EventTime)):
            raise ValueError("availability needs an index, snapshot date, and EventTime")
        if not isinstance(self.citation, str) or not self.citation.strip():
            raise ValueError("availability citation must be nonempty")
        if not isinstance(self.basis, str) or not self.basis.strip():
            raise ValueError("availability basis must be nonempty")
        if (not isinstance(self.source_sha256, str)
                or len(self.source_sha256) != 64
                or any(ch not in "0123456789abcdef" for ch in self.source_sha256)):
            raise ValueError("availability source binding must be a SHA-256 digest")


class SnapshotMembershipEvidence:
    """Answer only exact-date queries from digest-bound, reviewed snapshot inputs.

    Construction requires each Snapshot's primary-source bytes, an exact
    backtest-key-to-contemporaneous-symbol mapping, a separate availability
    attestation for every snapshot, and a bound decision EventTime. No symbol/ISIN
    fuzzy matching, interval reconstruction, or inference is performed. A missing
    mapped symbol yields False only under ``snapshots.validate``'s declared-count
    completeness attestation for that supplied snapshot scope. Unknown dates and
    unavailable evidence raise :class:`CoverageUnknown`, never False.
    """

    def __init__(self, snapshots: Sequence[Snapshot], key_to_symbol: Mapping[str, str],
                 source_bytes: Mapping[tuple[str, date], bytes],
                 availability_records: Sequence[AvailabilityEvidence], *,
                 decision_at: EventTime, index_code: str | None = None):
        if not isinstance(snapshots, Sequence) or isinstance(snapshots, (str, bytes)) or not snapshots:
            raise ValueError("at least one imported Snapshot is required")
        if not isinstance(decision_at, EventTime):
            raise ValueError("decision_at must be an explicit EventTime")
        if not isinstance(key_to_symbol, Mapping) or not key_to_symbol:
            raise ValueError("an exact key-to-symbol mapping is required")

        frozen_map = {}
        symbols = set()
        for key, symbol in key_to_symbol.items():
            if not isinstance(key, str) or not key or not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
                raise ValueError("key mapping requires nonempty exact keys and valid symbols")
            if symbol in symbols:
                raise ValueError("key mapping symbol collision")
            frozen_map[key] = symbol
            symbols.add(symbol)

        cloned = []
        for original in snapshots:
            if not isinstance(original, Snapshot):
                raise ValueError("snapshots must be imported Snapshot objects")
            metadata = _freeze(deepcopy(original.metadata))
            copied = Snapshot(original.index_code, original.as_of,
                              tuple(original.members), metadata)
            if type(copied.as_of) is not date:
                raise ValueError("snapshot as_of must be a date")
            validate(copied, cutoff=copied.as_of)
            cloned.append(copied)

        indices = {item.index_code for item in cloned}
        if index_code is None and (len(indices) > 1 or len(cloned) > 1):
            raise ValueError("index_code is required when multiple snapshots are supplied")
        if index_code is not None and index_code not in INDEX_CODES:
            raise ValueError("unsupported index_code")
        if index_code is not None and indices != {index_code}:
            raise ValueError("snapshots do not match index_code")
        selected_index = index_code or next(iter(indices))

        if not isinstance(source_bytes, Mapping):
            raise ValueError("source bytes mapping is required")
        copied_sources = {}
        for identity, content in source_bytes.items():
            if (not isinstance(identity, tuple) or len(identity) != 2
                    or not isinstance(identity[0], str) or type(identity[1]) is not date
                    or not isinstance(content, (bytes, bytearray, memoryview))):
                raise ValueError("source bytes must be keyed by (index_code, as_of)")
            copied_sources[identity] = bytes(content)

        evidence_by_identity = {}
        if not isinstance(availability_records, Sequence):
            raise ValueError("separate availability evidence records are required")
        for evidence in availability_records:
            if not isinstance(evidence, AvailabilityEvidence):
                raise ValueError("availability records must be AvailabilityEvidence")
            identity = (evidence.index_code, evidence.as_of)
            prior = evidence_by_identity.get(identity)
            if prior is not None and prior != evidence:
                raise ValueError("ambiguous availability evidence")
            evidence_by_identity[identity] = evidence

        accepted = {}
        for item in cloned:
            if item.index_code != selected_index:
                continue
            identity = (item.index_code, item.as_of)
            source = copied_sources.get(identity)
            if source is None:
                raise ValueError("primary source bytes are required for every snapshot")
            digest = sha256(source).hexdigest()
            if digest != item.metadata.get("source_sha256"):
                raise ValueError("source digest mismatch")
            member_name = item.metadata.get("source_member")
            member_digest = item.metadata.get("source_member_sha256")
            if member_name or member_digest:
                if not member_name or not member_digest:
                    raise ValueError("ZIP source member name and digest are both required")
                try:
                    with ZipFile(BytesIO(source)) as archive:
                        inner = archive.read(member_name)
                except (BadZipFile, KeyError, OSError) as exc:
                    raise ValueError("invalid ZIP source member") from exc
                if sha256(inner).hexdigest() != member_digest:
                    raise ValueError("source member digest mismatch")

            evidence = evidence_by_identity.get(identity)
            if evidence is None:
                raise ValueError("separate availability evidence is required")
            if evidence.source_sha256 != digest:
                raise ValueError("availability source binding mismatch")
            if evidence.available_at.session < item.as_of:
                raise ValueError("availability predates snapshot date")

            previous = accepted.get(identity)
            if previous is not None:
                old_snapshot, old_evidence = previous
                old_symbols = tuple(member.symbol for member in old_snapshot.members)
                new_symbols = tuple(member.symbol for member in item.members)
                if old_symbols != new_symbols or old_snapshot.metadata != item.metadata or old_evidence != evidence:
                    raise ValueError("ambiguous same-index/date snapshots")
                continue
            accepted[identity] = (item, evidence)

        if not accepted:
            raise ValueError("no snapshots supplied for selected index_code")
        self._snapshots = MappingProxyType(accepted)
        self._key_to_symbol = MappingProxyType(frozen_map)
        self._decision_at = decision_at
        self._index_code = selected_index

    def is_member(self, key: str, as_of: date) -> bool:
        """Return exact-date membership, or raise CoverageUnknown when not known."""
        if not isinstance(key, str) or not key or key not in self._key_to_symbol:
            raise ValueError("key must be an exact key present in the mapping")
        if type(as_of) is not date:
            raise ValueError("as_of must be a date, not a datetime or other value")
        if as_of > self._decision_at.session:
            raise CoverageUnknown("query date is after the bound decision_at")
        snapshot_row = self._snapshots.get((self._index_code, as_of))
        if snapshot_row is None:
            raise CoverageUnknown(f"membership coverage is unknown: no exact-date snapshot for {as_of}")
        snapshot, evidence = snapshot_row
        if evidence.available_at > self._decision_at:
            raise CoverageUnknown(f"membership known_at evidence is after decision_at for {as_of}")
        symbol = self._key_to_symbol[key]
        return any(member.symbol == symbol for member in snapshot.members)
