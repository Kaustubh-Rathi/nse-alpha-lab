"""Causal stop-loss and take-profit exit rules.

Policy: an exit decision may only use information that existed before the
execution it triggers. Two modes are supported and they are NOT equivalent:

``next_open`` (default, causal)
    A breach is detected on a completed session's close. The position is
    exited at the *next* session's open. No same-day information is used to
    trade at a same-day price, and no intraday fill is assumed.

``intraday_stop`` (optimistic, labelled)
    A breach is detected against the session's own low (stop) or high
    (target) and filled at the trigger price. This assumes the order was
    resting in the book and filled exactly at the trigger, which ignores
    gaps, slippage and the possibility that the low printed after the
    position was already gone. Callers must label results produced in this
    mode as optimistic.

Both modes are pure: no I/O, no clock, no global state.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

ZERO = Decimal(0)
ONE = Decimal(1)

STOP_LOSS = "stop_loss"
TAKE_PROFIT = "take_profit"

VALID_MODES = ("next_open", "intraday_stop")


@dataclass(frozen=True)
class ExitPolicy:
    """A stop-loss and/or take-profit overlay.

    ``stop_loss_pct`` is a positive magnitude: ``Decimal("0.10")`` exits when
    the position is 10% below entry. ``take_profit_pct`` is likewise a positive
    magnitude: ``Decimal("0.20")`` exits when the position is 20% above entry.
    Both ``None`` means no overlay and the policy is inert.
    """

    stop_loss_pct: Decimal | None = None
    take_profit_pct: Decimal | None = None
    mode: str = "next_open"

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}, got {self.mode!r}")
        for name in ("stop_loss_pct", "take_profit_pct"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be a Decimal or None")
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
            if value <= ZERO:
                raise ValueError(f"{name} must be a positive magnitude, got {value}")
            if value >= ONE:
                raise ValueError(
                    f"{name} must be below 1.0 (100%); got {value}. "
                    "A 100% stop is a total loss, not a stop.")

    @property
    def active(self) -> bool:
        """True when at least one leg is configured."""
        return self.stop_loss_pct is not None or self.take_profit_pct is not None

    def stop_price(self, entry_price: Decimal) -> Decimal | None:
        if self.stop_loss_pct is None:
            return None
        return entry_price * (ONE - self.stop_loss_pct)

    def target_price(self, entry_price: Decimal) -> Decimal | None:
        if self.take_profit_pct is None:
            return None
        return entry_price * (ONE + self.take_profit_pct)


@dataclass(frozen=True)
class ExitSignal:
    """The outcome of evaluating a policy for one held position on one session."""

    reason: str
    trigger_price: Decimal | None
    executes_next_open: bool


def evaluate_exit(
    policy: ExitPolicy,
    *,
    entry_price: Decimal,
    close: Decimal,
    low: Decimal | None = None,
    high: Decimal | None = None,
) -> ExitSignal | None:
    """Return the exit signal for a held position, or ``None`` to keep holding.

    ``entry_price`` is the position's average entry. ``close`` is the completed
    session's close. ``low``/``high`` are only consulted in ``intraday_stop``
    mode and are required there.

    In ``next_open`` mode the returned ``trigger_price`` is ``None`` because the
    fill price is not known until the next session opens; the caller fills it.
    """
    if not policy.active:
        return None
    if not isinstance(entry_price, Decimal) or entry_price <= ZERO:
        raise ValueError("entry_price must be a positive Decimal")
    if not isinstance(close, Decimal) or close <= ZERO:
        raise ValueError("close must be a positive Decimal")

    if policy.mode == "next_open":
        if policy.stop_loss_pct is not None:
            stop = policy.stop_price(entry_price)
            if close <= stop:
                return ExitSignal(STOP_LOSS, None, True)
        if policy.take_profit_pct is not None:
            target = policy.target_price(entry_price)
            if close >= target:
                return ExitSignal(TAKE_PROFIT, None, True)
        return None

    # intraday_stop: the session's own range decides, filled at the trigger.
    if low is None or high is None:
        raise ValueError("intraday_stop mode requires both low and high")
    if not isinstance(low, Decimal) or not isinstance(high, Decimal):
        raise TypeError("low and high must be Decimals")
    if low <= ZERO or high <= ZERO:
        raise ValueError("low and high must be positive")

    # When both legs trigger in the same session the outcome is ambiguous: the
    # session's range order is unknown. Resolve pessimistically to the stop,
    # because assuming the target filled first would flatter the result.
    if policy.stop_loss_pct is not None:
        stop = policy.stop_price(entry_price)
        if low <= stop:
            return ExitSignal(STOP_LOSS, stop, False)
    if policy.take_profit_pct is not None:
        target = policy.target_price(entry_price)
        if high >= target:
            return ExitSignal(TAKE_PROFIT, target, False)
    return None


def describe(policy: ExitPolicy) -> str:
    """A short stable label for reports and config manifests."""
    if not policy.active:
        return "no_exit_overlay"
    parts = []
    if policy.stop_loss_pct is not None:
        parts.append(f"sl{policy.stop_loss_pct.normalize()}")
    if policy.take_profit_pct is not None:
        parts.append(f"tp{policy.take_profit_pct.normalize()}")
    return f"{'-'.join(parts)}-{policy.mode}"


def parse_exit_policy(spec: str) -> ExitPolicy:
    """Parse an explicit overlay spec: ``none``, ``sl0.05``, ``tp0.20`` or
    ``sl0.10+tp0.20``. Percentages are positive magnitudes below 1.0."""
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError(f"exit policy spec must be a non-empty string, got {spec!r}")
    token = spec.strip().lower()
    if token == "none":
        return ExitPolicy()
    stop = target = None
    for part in token.split("+"):
        if not part:
            raise ValueError(f"empty exit policy leg in {spec!r}")
        if part.startswith("sl"):
            if stop is not None:
                raise ValueError(f"duplicate stop-loss leg in {spec!r}")
            try:
                stop = Decimal(part[2:])
            except InvalidOperation as exc:
                raise ValueError(
                    f"invalid stop-loss magnitude in exit policy leg {part!r}: {spec!r}") from exc
        elif part.startswith("tp"):
            if target is not None:
                raise ValueError(f"duplicate take-profit leg in {spec!r}")
            try:
                target = Decimal(part[2:])
            except InvalidOperation as exc:
                raise ValueError(
                    f"invalid take-profit magnitude in exit policy leg {part!r}: {spec!r}") from exc
        else:
            raise ValueError(
                f"exit policy leg {part!r} must start with 'sl' or 'tp': {spec!r}")
    return ExitPolicy(stop_loss_pct=stop, take_profit_pct=target)
