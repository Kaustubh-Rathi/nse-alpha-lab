"""Shared, causal session observations consumed by every market-data component.

A Bar is one completed exchange session for one stable security key. It becomes
known only at CLOSE_VALUATION of its own session; nothing here reads files.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from .accounting_types import EventTime, Phase, decimal, identifier, integer

# The closed bar-quality vocabulary. It records what the bar actually certifies:
# a complete coherent OHLC tuple, a usable close without trustworthy OHLC, or a
# degraded record that must not be treated as usable by any consumer.
BAR_QUALITIES = ("complete", "close_only", "degraded")


def _price(value):
    """Convert a price, reporting negative inputs with the dedicated token."""
    if type(value) in (str, int, Decimal):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            parsed = None
        if parsed is not None and parsed.is_finite() and parsed < 0:
            raise ValueError("negative_price")
    return decimal(value)


def _require_coherent(bar: "Bar") -> None:
    """A complete bar must satisfy low <= min(open, close) <= max(open, close) <= high."""
    violations = []
    for lower, upper in (("low", "high"), ("low", "open"), ("low", "close"),
                         ("open", "high"), ("close", "high")):
        low_value, high_value = getattr(bar, lower), getattr(bar, upper)
        if low_value > high_value:
            violations.append(f"{lower}={low_value} > {upper}={high_value}")
    if violations:
        raise ValueError("incoherent_ohlc: " + "; ".join(violations))


@dataclass(frozen=True)
class Bar:
    """One session's observation, explicit about what its OHLC tuple certifies.

    ``bar_quality`` is inferred when omitted: a fully populated coherent tuple is
    ``complete``, a valid bar without all four prices is ``close_only``, and an
    unusable bar is ``degraded``. A ``valid_ohlcv`` flag never certifies an
    absent close.
    """

    key: str
    symbol: str
    series: str
    session: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: int
    valid_ohlcv: bool
    isin: str | None = None
    traded_value: Decimal | None = None
    trades: int | None = None
    bar_quality: str | None = None

    def __post_init__(self):
        identifier(self.key)
        identifier(self.symbol)
        identifier(self.series)
        if type(self.session) is not date or type(self.valid_ohlcv) is not bool:
            raise ValueError("A bar needs its session date and an explicit validity flag")
        integer(self.volume)
        if self.trades is not None:
            integer(self.trades)
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _price(value))
        if self.traded_value is not None:
            object.__setattr__(self, "traded_value", decimal(self.traded_value))
        if self.isin is not None and (not isinstance(self.isin, str) or not self.isin.strip()):
            raise ValueError("ISIN must be a nonempty string or None")

        quality = self.bar_quality
        if quality is not None and quality not in BAR_QUALITIES:
            raise ValueError("bar_quality_mismatch")
        if self.valid_ohlcv:
            if self.close is None:
                raise ValueError("valid_ohlcv requires close")
            if quality is None:
                complete = all(getattr(self, name) is not None
                               for name in ("open", "high", "low", "close"))
                quality = "complete" if complete else "close_only"
            elif quality == "degraded":
                raise ValueError("bar_quality_mismatch")
            if quality == "complete":
                if any(getattr(self, name) is None for name in ("open", "high", "low")):
                    raise ValueError("bar_quality_mismatch")
                _require_coherent(self)
            # close_only skips the OHLC-ordering check: only the close is certified.
        else:
            if quality is None:
                quality = "degraded"
            elif quality != "degraded":
                raise ValueError("bar_quality_mismatch")
        object.__setattr__(self, "bar_quality", quality)

    @property
    def available_at(self) -> EventTime:
        return EventTime(self.session, Phase.CLOSE_VALUATION)


class SessionStream:
    """Append-only observed exchange sessions; the only source of session indices."""

    def __init__(self, sessions=()):
        self._sessions: tuple[date, ...] = ()
        for on in sessions:
            self.observe(on)

    def observe(self, on: date) -> int:
        if type(on) is not date or (self._sessions and on <= self._sessions[-1]):
            raise ValueError("Observed sessions must be dates in strictly increasing order")
        self._sessions += (on,)
        return len(self._sessions) - 1

    @property
    def sessions(self) -> tuple[date, ...]:
        return self._sessions

    def index(self, on: date) -> int:
        try:
            return self._sessions.index(on)
        except ValueError:
            raise ValueError(f"{on} is not an observed session") from None

    def as_of(self, on: date) -> tuple[date, ...]:
        return self._sessions[: self.index(on) + 1]
