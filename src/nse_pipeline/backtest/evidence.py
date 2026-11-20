"""Append-only, key-based observation ledger; counts are in observed sessions (never calendar days) and are only lower bounds for left-censored keys."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Iterable, Mapping

from .accounting_types import identifier
from .observations import Bar, SessionStream


@dataclass(frozen=True)
class KeyHistory:
    key: str
    as_of: date
    first_session: date | None
    last_bar_session: date | None
    last_valid_bar: Bar | None
    bar_observations: int
    valid_observations: int
    sessions_since_first: int
    absence_sessions: int | None
    left_censored: bool

    @property
    def counts_are_lower_bounds(self) -> bool:
        return self.left_censored


@dataclass(frozen=True)
class SymbolSpan:
    symbol: str
    key: str
    first_session: date
    last_session: date


class ObservationLedger:
    """Ingests every bar of each observed session once, in SessionStream order.

    A key already trading on the first ingested session is left-censored: its
    earlier history is unknown. Keys first seen later are treated as new, so a
    resumption after a pre-data gap is not detectable here.
    """

    def __init__(self, stream: SessionStream):
        if not isinstance(stream, SessionStream):
            raise ValueError("The ledger needs the shared SessionStream")
        self._stream = stream
        self._sessions: list[date] = []
        self._index: dict[date, int] = {}
        self._by_session: list[Mapping[str, Bar]] = []
        self._bar_idx: dict[str, list[int]] = {}
        self._valid_idx: dict[str, list[int]] = {}
        self._valid_bars: dict[str, list[Bar]] = {}
        self._keys: list[str] = []
        self._key_first: list[int] = []
        self._symbols: dict[str, dict[str, list[int]]] = {}

    @property
    def stream(self) -> SessionStream:
        return self._stream

    @property
    def sessions(self) -> tuple[date, ...]:
        return tuple(self._sessions)

    def ingest(self, session: date, bars: Iterable[Bar]) -> int:
        n = len(self._sessions)
        observed = self._stream.sessions
        if type(session) is not date or len(observed) <= n or observed[n] != session:
            raise ValueError("Ingest each observed session exactly once, in SessionStream order")
        if isinstance(bars, (Bar, str, bytes)):
            raise ValueError("Supply an iterable of the session's bars")
        rows: dict[str, Bar] = {}
        for bar in tuple(bars):
            if not isinstance(bar, Bar) or bar.session != session:
                raise ValueError("Every ingested bar must belong to the ingested session")
            if bar.key in rows:
                raise ValueError(f"Duplicate key {bar.key!r} in session {session}")
            rows[bar.key] = bar
        self._sessions.append(session)
        self._index[session] = n
        self._by_session.append(MappingProxyType(dict(sorted(rows.items()))))
        for key, bar in rows.items():
            if key not in self._bar_idx:
                self._bar_idx[key], self._valid_idx[key], self._valid_bars[key] = [], [], []
                self._keys.append(key)
                self._key_first.append(n)
            self._bar_idx[key].append(n)
            if bar.valid_ohlcv:
                self._valid_idx[key].append(n)
                self._valid_bars[key].append(bar)
            self._symbols.setdefault(bar.symbol, {}).setdefault(key, []).append(n)
        return n

    def session_index(self, on: date) -> int:
        index = self._index.get(on) if type(on) is date else None
        if index is None:
            raise ValueError(f"{on!r} is not an ingested session")
        return index

    def window(self, as_of: date, sessions: int) -> tuple[date, ...] | None:
        if type(sessions) is not int or sessions < 1:
            raise ValueError("A window needs a positive integer session count")
        end = self.session_index(as_of) + 1
        return None if end < sessions else tuple(self._sessions[end - sessions:end])

    def bars_on(self, session: date) -> Mapping[str, Bar]:
        return self._by_session[self.session_index(session)]

    def bar(self, key: str, session: date) -> Bar | None:
        return self.bars_on(session).get(identifier(key))

    def keys_as_of(self, as_of: date) -> tuple[str, ...]:
        return tuple(sorted(self._keys[:bisect_right(self._key_first, self.session_index(as_of))]))

    def valid_count(self, key: str, as_of: date) -> int:
        return bisect_right(self._valid_idx.get(identifier(key), ()), self.session_index(as_of))

    def window_valid_count(self, key: str, as_of: date, sessions: int) -> int:
        """Valid bars in the closed window of the last `sessions` observed sessions ending at as_of.

        This is the momentum observation count under D-06. It is exactly the count inside
        [anchor, as_of]; older valid history is excluded, and history is never an input.
        """
        if type(sessions) is not int or sessions < 1:
            raise ValueError("A window needs a positive integer session count")
        end = self.session_index(as_of) + 1
        if end < sessions:
            raise ValueError("The window extends before the observed stream")
        indices = self._valid_idx.get(identifier(key), ())
        return bisect_right(indices, end - 1) - bisect_left(indices, end - sessions)

    def history(self, key: str, as_of: date) -> KeyHistory:
        t = self.session_index(as_of)
        bars = self._bar_idx.get(identifier(key), [])
        seen = bisect_right(bars, t)
        if not seen:
            return KeyHistory(key, as_of, None, None, None, 0, 0, 0, None, False)
        valid = bisect_right(self._valid_idx[key], t)
        return KeyHistory(
            key, as_of, self._sessions[bars[0]], self._sessions[bars[seen - 1]],
            self._valid_bars[key][valid - 1] if valid else None, seen, valid,
            t - bars[0] + 1, t - bars[seen - 1], bars[0] == 0)

    def symbol_history(self, symbol: str, as_of: date) -> tuple[SymbolSpan, ...]:
        t = self.session_index(as_of)
        spans = []
        for key, seen in self._symbols.get(identifier(symbol), {}).items():
            last = bisect_right(seen, t)
            if last:
                spans.append(SymbolSpan(symbol, key, self._sessions[seen[0]], self._sessions[seen[last - 1]]))
        return tuple(sorted(spans, key=lambda s: (s.first_session, s.last_session, s.key)))
