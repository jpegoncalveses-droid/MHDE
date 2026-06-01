"""Tests for the hard-floor catastrophic-stop overlay on Policy D.

The overlay (``HardFloorOverlay`` in ``policies.py``) wraps an inner exit
policy and applies a fixed-percentage floor set once at entry
(``floor_price = entry_price * (1 + hard_floor_pct)``), with conservative
gap-through modelling (fill at ``min(floor, open)``). It models the
engine-side ``HARD_FLOOR_EXIT_PCT`` client-side market exit so a backtest
can run under the same exit rules the live engine applies.

Cases:
  1. ``hard_floor`` is a valid exit reason.
  2. Floor fires when the daily low breaches the level; reason/price.
  3. Static level — set at entry, not recomputed mid-trade.
  4. Gap-through: day opens below the floor → exit at the open.
  5. Floor vs trail same day → floor takes priority.
  6. No breach → delegates to the inner policy (control parity).
  7. ``hard_floor_pct >= 0`` is rejected.
  8. Wired through ``_build_position`` via the ``hard_floor_pct`` param.
"""
from __future__ import annotations

from datetime import date

import pytest

from crypto.execution.backtest.harness import _build_position
from crypto.execution.backtest.policies import (
    ExitEvent,
    HardFloorOverlay,
    TrailingStopOnly,
)


def _overlay(entry_price=100.0, horizon_days=10, *, hard_floor_pct=-0.05,
             trail_pct=0.30, activation_pct=0.01):
    inner = TrailingStopOnly(
        entry_price=entry_price, horizon_days=horizon_days,
        trail_pct=trail_pct, activation_pct=activation_pct,
    )
    return HardFloorOverlay(
        entry_price=entry_price, horizon_days=horizon_days,
        inner=inner, hard_floor_pct=hard_floor_pct,
    )


def test_hard_floor_is_a_valid_exit_reason():
    ev = ExitEvent(exit_price=95.0, fraction=1.0, reason="hard_floor")
    assert ev.reason == "hard_floor"


def test_floor_fires_on_low_breach_at_floor_price():
    # entry 100, floor -5% → 95. Day low pierces 95 but open is above it.
    ov = _overlay(entry_price=100.0, hard_floor_pct=-0.05)
    events = ov.step(1, high=101.0, low=94.0, close=96.0, open_=99.0)
    assert len(events) == 1
    assert events[0].reason == "hard_floor"
    assert events[0].exit_price == pytest.approx(95.0)  # filled at floor
    assert ov.is_complete


def test_floor_level_is_static_across_bars():
    ov = _overlay(entry_price=100.0, hard_floor_pct=-0.05)
    # Bar 1 rallies; floor must stay at 95 (not ratchet up with price).
    assert ov.step(1, high=120.0, low=110.0, close=115.0, open_=111.0) == []
    assert ov.floor_price == pytest.approx(95.0)
    # Bar 2 dips to 95 → still fires at the original 95.
    events = ov.step(2, high=116.0, low=95.0, close=97.0, open_=114.0)
    assert events[0].reason == "hard_floor"
    assert events[0].exit_price == pytest.approx(95.0)


def test_gap_through_fills_at_open():
    # Open gaps below the floor → conservative fill at the open, not 95.
    ov = _overlay(entry_price=100.0, hard_floor_pct=-0.05)
    events = ov.step(1, high=93.0, low=90.0, close=92.0, open_=93.0)
    assert events[0].reason == "hard_floor"
    assert events[0].exit_price == pytest.approx(93.0)  # min(95, open=93)


def test_floor_takes_priority_over_trail_same_bar():
    # Arm the trail with a high bar, then a bar that breaches BOTH the
    # trail stop and the floor on the same day → floor wins.
    ov = _overlay(entry_price=100.0, hard_floor_pct=-0.05,
                  trail_pct=0.30, activation_pct=0.01)
    ov.step(1, high=130.0, low=120.0, close=125.0, open_=121.0)  # peak=130
    # Trail stop now ≈ 130 - (30*0.30) = 121. A crash bar to low 94 breaches
    # both the trail (121) and the floor (95); the floor must be the reason.
    events = ov.step(2, high=126.0, low=94.0, close=96.0, open_=120.0)
    assert len(events) == 1
    assert events[0].reason == "hard_floor"
    assert events[0].exit_price == pytest.approx(95.0)


def test_no_breach_delegates_to_inner():
    # Low never reaches the floor → behaves exactly like the inner trail.
    ov = _overlay(entry_price=100.0, hard_floor_pct=-0.05)
    inner_only = TrailingStopOnly(
        entry_price=100.0, horizon_days=10, trail_pct=0.30, activation_pct=0.01,
    )
    bars = [
        (1, 105.0, 99.0, 104.0, 100.0),
        (2, 108.0, 103.0, 107.0, 104.0),
    ]
    for d, hi, lo, cl, op in bars:
        ov_ev = ov.step(d, hi, lo, cl, op)
        in_ev = inner_only.step(d, hi, lo, cl, op)
        assert [(e.reason, e.exit_price) for e in ov_ev] == \
               [(e.reason, e.exit_price) for e in in_ev]
    assert ov.remaining_fraction == inner_only.remaining_fraction


def test_rejects_non_negative_floor():
    inner = TrailingStopOnly(entry_price=100.0, horizon_days=10)
    with pytest.raises(ValueError):
        HardFloorOverlay(entry_price=100.0, horizon_days=10,
                         inner=inner, hard_floor_pct=0.05)
    with pytest.raises(ValueError):
        HardFloorOverlay(entry_price=100.0, horizon_days=10,
                         inner=inner, hard_floor_pct=0.0)


def test_build_position_wires_hard_floor_param():
    # The harness must pop hard_floor_pct and wrap the inner Policy D.
    pos, reason = _build_position(
        coin="BTCUSDT", pred_date=date(2025, 5, 1), entry_date=date(2025, 5, 2),
        entry_price=100.0, horizon="10d", horizon_days=10, exit_policy_id="D",
        policy_params={"trail_pct": 0.30, "activation_pct": 0.01,
                       "hard_floor_pct": -0.05},
        probability=0.6, atr_lookup={}, trade_id="t1",
    )
    assert reason is None
    assert isinstance(pos.policy, HardFloorOverlay)
    assert isinstance(pos.policy.inner, TrailingStopOnly)
    assert pos.policy.floor_price == pytest.approx(95.0)
