"""Exit-policy implementations for the crypto execution backtest.

Per ``crypto/execution/backtest/SPEC.md`` — Policies to Test (Phase 1A):

  A: Fixed TP at +5%, no stop, time stop at horizon
  B: Fixed TP at +5%, fixed -3% stop, time stop at horizon
  C: Fixed TP at +5%, ATR-based stop (2x daily 14d ATR using
     ``atr_pct_14d``; ``stop_price = entry_price * (1 - atr_mult * atr_pct)``),
     time stop at horizon
  D: Trailing stop at 50% of peak profit with activation threshold
     (default 1%; trail arms only when peak >= entry × (1 + activation_pct)),
     no fixed TP, time stop at horizon
  E: Tiered — 50% off at +5%, remaining 50% with 50% trailing stop on peak,
     time stop at horizon for any remainder

All policies are **stateful** (one instance per simulated trade) and consume
one daily OHLC bar at a time via ``step(day_idx, high, low, close)``. Each
``step`` returns a list of ``ExitEvent`` objects describing the partial or
full exits triggered on that day. The harness drives the loop; this module
makes no DB calls and imports nothing from equity / FX / shared ML code.

Same-day priority — if both the stop and the take-profit could fire on the
same bar, **the stop wins**. That's the conservative choice for a daily-bar
backtest where the intraday order of high vs. low is unknown. The trailing
stop is computed from yesterday's peak; today's high only updates the peak
if the trade survived the day's check.

Sensitivity-grid parameters from SPEC.md are exposed as constructor kwargs
on each policy:
    tp_pct, sl_pct, atr_mult, trail_pct, activation_pct, tp_fraction
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Optional


# ──────────────────────────────────────────────────────────────────────
# ExitEvent — what a policy emits per bar
# ──────────────────────────────────────────────────────────────────────


VALID_EXIT_REASONS = frozenset(
    {"tp", "sl", "stop_loss", "trailing", "time", "data_gap", "hard_floor"}
)


@dataclass(frozen=True)
class ExitEvent:
    """One partial-or-full position exit triggered on a single bar.

    ``fraction`` is in (0, 1]; the harness sums fractions across events
    to verify every trade fully closes. ``reason`` is one of:
    ``'tp'``, ``'sl'``, ``'stop_loss'``, ``'trailing'``, ``'time'``,
    ``'data_gap'``.

    ``tp``/``sl``/``trailing`` are emitted by exit-policy logic (see
    sub-classes of :class:`ExitPolicy`). ``stop_loss`` is emitted by
    :class:`AtrStopOverlay` (the entry-time ATR stop overlaid on an inner
    policy). ``data_gap`` is a harness-level reason emitted by
    ``crypto/execution/backtest/harness.py`` when the forward-price stream
    has a gap of 3+ days inside the hold window (per SPEC.md "Missing data
    handling").
    """

    exit_price: float
    fraction: float
    reason: str

    def __post_init__(self) -> None:
        if self.reason not in VALID_EXIT_REASONS:
            raise ValueError(f"unknown exit reason: {self.reason!r}")
        if not 0.0 < self.fraction <= 1.0:
            raise ValueError(f"fraction must be in (0, 1], got {self.fraction}")
        if self.exit_price <= 0:
            raise ValueError(f"exit_price must be positive, got {self.exit_price}")


# ──────────────────────────────────────────────────────────────────────
# ExitPolicy base
# ──────────────────────────────────────────────────────────────────────


class ExitPolicy(abc.ABC):
    """Base class — one instance per trade, fed daily bars by the harness.

    ``day_idx`` counts forward days since entry: ``1`` is the first bar
    after the entry bar, ``horizon_days`` is the last bar at which the
    time stop fires (always at that day's ``close``).
    """

    def __init__(self, entry_price: float, horizon_days: int) -> None:
        if entry_price <= 0:
            raise ValueError(f"entry_price must be positive, got {entry_price}")
        if horizon_days <= 0:
            raise ValueError(f"horizon_days must be positive, got {horizon_days}")
        self.entry_price = float(entry_price)
        self.horizon_days = int(horizon_days)
        self.remaining_fraction: float = 1.0
        self.peak_high: float = float(entry_price)

    @abc.abstractmethod
    def step(
        self, day_idx: int, high: float, low: float, close: float,
        open_: Optional[float] = None,
    ) -> list[ExitEvent]: ...

    @property
    def is_complete(self) -> bool:
        """True once the position has been fully exited."""
        return self.remaining_fraction <= 1e-9


# ──────────────────────────────────────────────────────────────────────
# Policy A — Fixed TP, no stop
# ──────────────────────────────────────────────────────────────────────


class FixedTpNoStop(ExitPolicy):
    """Policy A: take-profit at ``+tp_pct``, no stop, time stop at horizon."""

    def __init__(
        self,
        entry_price: float,
        horizon_days: int,
        *,
        tp_pct: float = 0.05,
    ) -> None:
        super().__init__(entry_price, horizon_days)
        self.tp_pct = float(tp_pct)
        self.tp_price = self.entry_price * (1.0 + self.tp_pct)

    def step(self, day_idx, high, low, close, open_=None):
        if self.is_complete:
            return []
        events: list[ExitEvent] = []
        if high >= self.tp_price:
            events.append(ExitEvent(self.tp_price, self.remaining_fraction, "tp"))
            self.remaining_fraction = 0.0
            return events
        if day_idx >= self.horizon_days:
            events.append(ExitEvent(close, self.remaining_fraction, "time"))
            self.remaining_fraction = 0.0
        return events


# ──────────────────────────────────────────────────────────────────────
# Policy B — Fixed TP + fixed % stop
# ──────────────────────────────────────────────────────────────────────


class FixedTpFixedSl(ExitPolicy):
    """Policy B: TP at ``+tp_pct``, stop at ``-sl_pct``, time stop at horizon.

    On a same-bar conflict, the stop fires first.
    """

    def __init__(
        self,
        entry_price: float,
        horizon_days: int,
        *,
        tp_pct: float = 0.05,
        sl_pct: float = 0.03,
    ) -> None:
        super().__init__(entry_price, horizon_days)
        self.tp_pct = float(tp_pct)
        self.sl_pct = float(sl_pct)
        self.tp_price = self.entry_price * (1.0 + self.tp_pct)
        self.sl_price = self.entry_price * (1.0 - self.sl_pct)

    def step(self, day_idx, high, low, close, open_=None):
        if self.is_complete:
            return []
        events: list[ExitEvent] = []
        if low <= self.sl_price:
            events.append(ExitEvent(self.sl_price, self.remaining_fraction, "sl"))
            self.remaining_fraction = 0.0
            return events
        if high >= self.tp_price:
            events.append(ExitEvent(self.tp_price, self.remaining_fraction, "tp"))
            self.remaining_fraction = 0.0
            return events
        if day_idx >= self.horizon_days:
            events.append(ExitEvent(close, self.remaining_fraction, "time"))
            self.remaining_fraction = 0.0
        return events


# ──────────────────────────────────────────────────────────────────────
# Policy C — Fixed TP + ATR-based stop
# ──────────────────────────────────────────────────────────────────────


class FixedTpAtrSl(ExitPolicy):
    """Policy C: TP at ``+tp_pct``, stop at ``-atr_mult * atr_pct`` (fixed
    at entry), time stop at horizon.

    ``atr_pct`` is the ``atr_pct_14d`` value from ``crypto_ml_features``
    on the entry date — already a fraction, so the stop price is simply
    ``entry_price * (1 - atr_mult * atr_pct)``. The stop level is set at
    entry and does not move during the trade.

    On a same-bar conflict, the stop fires first.
    """

    def __init__(
        self,
        entry_price: float,
        horizon_days: int,
        *,
        atr_pct: float,
        tp_pct: float = 0.05,
        atr_mult: float = 2.0,
    ) -> None:
        super().__init__(entry_price, horizon_days)
        if atr_pct is None or atr_pct < 0:
            raise ValueError(f"atr_pct must be a non-negative fraction, got {atr_pct}")
        self.tp_pct = float(tp_pct)
        self.atr_pct = float(atr_pct)
        self.atr_mult = float(atr_mult)
        self.tp_price = self.entry_price * (1.0 + self.tp_pct)
        self.sl_price = self.entry_price * (1.0 - self.atr_mult * self.atr_pct)

    def step(self, day_idx, high, low, close, open_=None):
        if self.is_complete:
            return []
        events: list[ExitEvent] = []
        if low <= self.sl_price:
            events.append(ExitEvent(self.sl_price, self.remaining_fraction, "sl"))
            self.remaining_fraction = 0.0
            return events
        if high >= self.tp_price:
            events.append(ExitEvent(self.tp_price, self.remaining_fraction, "tp"))
            self.remaining_fraction = 0.0
            return events
        if day_idx >= self.horizon_days:
            events.append(ExitEvent(close, self.remaining_fraction, "time"))
            self.remaining_fraction = 0.0
        return events


# ──────────────────────────────────────────────────────────────────────
# Policy D — Trailing stop only
# ──────────────────────────────────────────────────────────────────────


class TrailingStopOnly(ExitPolicy):
    """Policy D: trailing stop giving up ``trail_pct`` of peak profit, with
    an activation threshold.

    The trail arms only once ``peak_high >= entry_price * (1 + activation_pct)``.
    Until the threshold is crossed the position has no stop active and rides
    forward toward the time stop. With ``activation_pct=0.0`` the policy
    reverts to a pure trail-from-first-positive-bar (the original
    interpretation, kept available for sensitivity testing).

    Stop level on bar N = ``peak_high(N-1) − (peak_high(N-1) − entry) * trail_pct``.
    Today's high updates ``peak_high`` only if the trade survives today's check;
    that's the conservative daily-bar convention.
    """

    def __init__(
        self,
        entry_price: float,
        horizon_days: int,
        *,
        trail_pct: float = 0.50,
        activation_pct: float = 0.01,
    ) -> None:
        super().__init__(entry_price, horizon_days)
        if not 0.0 < trail_pct <= 1.0:
            raise ValueError(f"trail_pct must be in (0, 1], got {trail_pct}")
        if activation_pct < 0:
            raise ValueError(
                f"activation_pct must be non-negative, got {activation_pct}"
            )
        self.trail_pct = float(trail_pct)
        self.activation_pct = float(activation_pct)
        self.activation_price = self.entry_price * (1.0 + self.activation_pct)

    @property
    def is_armed(self) -> bool:
        """True once the trail has armed — i.e. the peak has reached the
        activation threshold *and* is strictly above entry.

        Read **before** a bar's ``step`` is applied it reflects the peak set
        by prior bars (the trail does not arm on the bar that first reaches
        the threshold, since ``peak_high`` updates only after that bar's
        check survives). The :class:`HardFloorOverlay` arm-aware mode uses
        this to decide whether the floor or the inner trail has same-bar
        priority.
        """
        return (
            self.peak_high >= self.activation_price
            and self.peak_high > self.entry_price
        )

    def step(self, day_idx, high, low, close, open_=None):
        if self.is_complete:
            return []
        events: list[ExitEvent] = []
        # Trail arms only when peak has reached the activation threshold AND
        # is strictly above entry (the second clause is what handles
        # ``activation_pct == 0`` cleanly, since otherwise peak == entry would
        # produce a degenerate trail_stop equal to entry).
        if (
            self.peak_high >= self.activation_price
            and self.peak_high > self.entry_price
        ):
            peak_profit = self.peak_high - self.entry_price
            trail_stop = self.peak_high - peak_profit * self.trail_pct
            if low <= trail_stop:
                events.append(
                    ExitEvent(trail_stop, self.remaining_fraction, "trailing")
                )
                self.remaining_fraction = 0.0
                return events
        # Survived today's stop check — update peak.
        self.peak_high = max(self.peak_high, float(high))
        if day_idx >= self.horizon_days:
            events.append(ExitEvent(close, self.remaining_fraction, "time"))
            self.remaining_fraction = 0.0
        return events


# ──────────────────────────────────────────────────────────────────────
# Policy E — Tiered exit
# ──────────────────────────────────────────────────────────────────────


class TieredExit(ExitPolicy):
    """Policy E: take ``tp_fraction`` (default 50%) off at ``+tp_pct``,
    leave the rest under a ``trail_pct`` trailing stop on peak profit,
    time stop at horizon for any remainder.

    Within a single bar, if both the TP and (a freshly-applicable) trail
    stop could trigger, the TP partial fills first — the remaining tranche
    starts trailing from the next bar onward, using the peak set today.
    Same-bar conflict for the remaining tranche between trailing stop and
    take-profit cannot occur because the TP only fires on the first
    crossing.
    """

    def __init__(
        self,
        entry_price: float,
        horizon_days: int,
        *,
        tp_pct: float = 0.05,
        tp_fraction: float = 0.50,
        trail_pct: float = 0.50,
    ) -> None:
        super().__init__(entry_price, horizon_days)
        if not 0.0 < tp_fraction < 1.0:
            raise ValueError(f"tp_fraction must be in (0, 1), got {tp_fraction}")
        if not 0.0 < trail_pct <= 1.0:
            raise ValueError(f"trail_pct must be in (0, 1], got {trail_pct}")
        self.tp_pct = float(tp_pct)
        self.tp_fraction = float(tp_fraction)
        self.trail_pct = float(trail_pct)
        self.tp_price = self.entry_price * (1.0 + self.tp_pct)
        self.tp_taken: bool = False

    def step(self, day_idx, high, low, close, open_=None):
        if self.is_complete:
            return []
        events: list[ExitEvent] = []

        # Trailing stop only applies to the residual tranche after the TP
        # partial has been taken on a prior bar.
        if self.tp_taken and self.peak_high > self.entry_price:
            peak_profit = self.peak_high - self.entry_price
            trail_stop = self.peak_high - peak_profit * self.trail_pct
            if low <= trail_stop:
                events.append(
                    ExitEvent(trail_stop, self.remaining_fraction, "trailing")
                )
                self.remaining_fraction = 0.0
                return events

        # First-tier take-profit.
        if not self.tp_taken and high >= self.tp_price:
            events.append(ExitEvent(self.tp_price, self.tp_fraction, "tp"))
            self.remaining_fraction -= self.tp_fraction
            self.tp_taken = True

        # Update peak after any partial TP — today's high is the newest
        # candidate for the trail-stop reference on subsequent bars.
        self.peak_high = max(self.peak_high, float(high))

        # Time stop on whatever's left.
        if day_idx >= self.horizon_days and self.remaining_fraction > 1e-9:
            events.append(ExitEvent(close, self.remaining_fraction, "time"))
            self.remaining_fraction = 0.0
        return events


# ──────────────────────────────────────────────────────────────────────
# ATR stop-loss overlay — wraps any inner policy with a static entry-time stop
# ──────────────────────────────────────────────────────────────────────


class AtrStopOverlay(ExitPolicy):
    """Overlay an entry-time **static** ATR stop on an ``inner`` policy.

    Reuses Policy C's level mechanic — ``atr_pct`` is the ``atr_pct_14d``
    fraction at entry and the stop is set once at entry:
    ``sl_price = entry_price * (1 - atr_multiple * atr_pct)``. It does not
    move for the life of the trade.

    Each bar the stop is checked against the day's **low** *before* the
    inner policy runs, so on a same-bar conflict the ATR stop fires first
    (it takes priority over the inner trail / take-profit / time stop).

    Gap-through modelling — unlike Policy C (which always fills at the stop
    level), the overlay fills at ``min(sl_price, open_)`` when the bar's
    open is supplied. A day that gaps open below the stop fills at the
    open, the conservative price for a daily-bar backtest. When ``open_``
    is ``None`` the fill falls back to the stop level.

    Lifecycle (``remaining_fraction`` / ``is_complete``) mirrors the inner
    policy except when the ATR stop fires, at which point the position is
    fully closed.
    """

    def __init__(
        self,
        entry_price: float,
        horizon_days: int,
        *,
        inner: ExitPolicy,
        atr_pct: float,
        atr_multiple: float = 2.0,
    ) -> None:
        super().__init__(entry_price, horizon_days)
        if atr_pct is None or atr_pct < 0:
            raise ValueError(
                f"atr_pct must be a non-negative fraction, got {atr_pct}"
            )
        if atr_multiple is None or atr_multiple <= 0:
            raise ValueError(
                f"atr_multiple must be positive, got {atr_multiple}"
            )
        self.inner = inner
        self.atr_pct = float(atr_pct)
        self.atr_multiple = float(atr_multiple)
        self.sl_price = self.entry_price * (1.0 - self.atr_multiple * self.atr_pct)

    def step(self, day_idx, high, low, close, open_=None):
        if self.is_complete:
            return []
        # ATR stop is checked first → priority over the inner policy on a
        # same-bar conflict.
        if low <= self.sl_price:
            fill = self.sl_price if open_ is None else min(self.sl_price, float(open_))
            events = [ExitEvent(fill, self.remaining_fraction, "stop_loss")]
            self.remaining_fraction = 0.0
            # Close the inner policy too so its lifecycle stays consistent.
            self.inner.remaining_fraction = 0.0
            return events
        # Survived the stop — delegate to the inner policy and mirror its
        # remaining fraction (handles partial fills, e.g. Policy E).
        events = self.inner.step(day_idx, high, low, close, open_)
        self.remaining_fraction = self.inner.remaining_fraction
        return events


# ──────────────────────────────────────────────────────────────────────
# Hard-floor overlay — engine-side catastrophic stop modelled in backtest
# ──────────────────────────────────────────────────────────────────────


class HardFloorOverlay(ExitPolicy):
    """Overlay a fixed **hard floor** catastrophic stop on an ``inner`` policy.

    Mirrors :class:`AtrStopOverlay` but the stop level is a flat percentage
    of entry rather than an ATR multiple. The floor is the engine-side
    ``HARD_FLOOR_EXIT_PCT`` client-side market exit (see the
    crypto-trading-engine ``monitor.py``); it is NOT part of the MHDE Phase
    1B contract and Policy D does not model it. This overlay exists so a
    backtest can run all configs under the same exit rules the live engine
    applies, instead of letting losers ride to the time stop.

    ``hard_floor_pct`` is a negative fraction (e.g. ``-0.05`` = −5%). The
    stop level is set once at entry: ``floor_price = entry * (1 +
    hard_floor_pct)`` and does not move.

    Each bar the floor is checked against the day's **low** *before* the
    inner policy runs, so on a same-bar conflict the floor fires first (it
    takes priority over the inner trail / time stop). Fill is at
    ``min(floor_price, open_)`` — a bar that gaps open below the floor fills
    at the open, the conservative price for a daily-bar backtest; when
    ``open_`` is ``None`` the fill falls back to the floor level.

    **Intraday arm-aware mode** (``intraday_arm_aware=True``) — used by the
    1-minute intraday replay. The floor-first priority holds only while the
    inner trail is *unarmed*; once the inner reports ``is_armed`` the inner
    (trail) is checked first and wins. This is faithful to the live engine,
    which arms a trailing stop and thereafter exits at the trail rather than
    the catastrophic floor. Because the armed trail sits at or above entry
    (and therefore strictly above the −5% floor), a bar that breaches the
    floor while armed also breaches the trail, so delegating to the inner
    yields a ``trailing`` exit. The bar that *first* arms the trail is still
    treated as unarmed at its start (``is_armed`` reflects prior bars), so a
    bar that both arms and breaches the floor resolves **adverse-first**: the
    floor fires. Daily mode (the default) is unchanged.
    """

    def __init__(
        self,
        entry_price: float,
        horizon_days: int,
        *,
        inner: ExitPolicy,
        hard_floor_pct: float,
        intraday_arm_aware: bool = False,
    ) -> None:
        super().__init__(entry_price, horizon_days)
        if hard_floor_pct is None or hard_floor_pct >= 0:
            raise ValueError(
                f"hard_floor_pct must be a negative fraction, got {hard_floor_pct}"
            )
        self.inner = inner
        self.hard_floor_pct = float(hard_floor_pct)
        self.floor_price = self.entry_price * (1.0 + self.hard_floor_pct)
        self.intraday_arm_aware = bool(intraday_arm_aware)

    def step(self, day_idx, high, low, close, open_=None):
        if self.is_complete:
            return []
        # Arm-aware intraday mode: once the inner trail is armed it takes
        # same-bar priority over the floor (trail sits above the floor, so a
        # floor breach is also a trail breach → a ``trailing`` exit). The
        # arming bar itself is still unarmed at its start, so a bar that both
        # arms and breaches the floor falls through to the floor check below
        # (adverse-first).
        if self.intraday_arm_aware and getattr(self.inner, "is_armed", False):
            events = self.inner.step(day_idx, high, low, close, open_)
            self.remaining_fraction = self.inner.remaining_fraction
            return events
        # Floor is checked first → priority over the inner policy on a
        # same-bar conflict.
        if low <= self.floor_price:
            fill = (
                self.floor_price if open_ is None
                else min(self.floor_price, float(open_))
            )
            events = [ExitEvent(fill, self.remaining_fraction, "hard_floor")]
            self.remaining_fraction = 0.0
            self.inner.remaining_fraction = 0.0
            return events
        # Survived the floor — delegate to the inner policy and mirror its
        # remaining fraction (handles partial fills, e.g. Policy E).
        events = self.inner.step(day_idx, high, low, close, open_)
        self.remaining_fraction = self.inner.remaining_fraction
        return events


# ──────────────────────────────────────────────────────────────────────
# Factory — build a policy by spec ID ('A'..'E')
# ──────────────────────────────────────────────────────────────────────


_POLICY_BY_ID: dict[str, type[ExitPolicy]] = {
    "A": FixedTpNoStop,
    "B": FixedTpFixedSl,
    "C": FixedTpAtrSl,
    "D": TrailingStopOnly,
    "E": TieredExit,
}


def build_policy(
    policy_id: str,
    entry_price: float,
    horizon_days: int,
    params: Optional[dict[str, Any]] = None,
) -> ExitPolicy:
    """Construct an exit policy by SPEC.md identifier.

    ``params`` is the run's policy-specific parameter dict (will be stored
    as JSON in ``crypto_backtest_runs.parameters``); valid keys depend on
    the policy class.
    """
    pid = policy_id.upper()
    cls = _POLICY_BY_ID.get(pid)
    if cls is None:
        raise ValueError(
            f"unknown policy id: {policy_id!r} (expected one of {sorted(_POLICY_BY_ID)})"
        )
    return cls(entry_price=entry_price, horizon_days=horizon_days, **(params or {}))
