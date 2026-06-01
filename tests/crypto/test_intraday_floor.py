"""Tests for the INTRADAY arm-aware extension of the hard-floor overlay.

The base branch (``exp/trailstop-sweep``) added ``HardFloorOverlay`` in a
**daily floor-first** mode: the floor is always checked before the inner
policy, so on any same-bar conflict the floor wins. For the 1-minute
intraday replay we need an **arm-aware** mode instead (per the workstream
spec):

  * While the inner trailing stop is UNARMED, the −5% hard floor is the
    active stop (floor-first, same as before).
  * Once the inner trail is ARMED (peak ≥ entry × (1 + activation_pct)),
    the give-back trailing stop (which sits at or above entry, hence
    strictly above the floor) is checked first and WINS.
  * The rare 1-minute bar that both arms the trail (high crosses
    activation) and breaches the floor (low ≤ floor) resolves
    adverse-first: the floor fires (the down-move is assumed to happen
    before the up-move within the bar).

These tests pin the arm-aware behaviour. The existing daily floor-first
tests in ``test_backtest_hard_floor.py`` must remain green (default
``intraday_arm_aware=False``).
"""
from __future__ import annotations

import pytest

from crypto.execution.backtest.policies import (
    HardFloorOverlay,
    TrailingStopOnly,
)


def _inner(entry_price=100.0, *, trail_pct=0.30, activation_pct=0.01):
    return TrailingStopOnly(
        entry_price=entry_price, horizon_days=10,
        trail_pct=trail_pct, activation_pct=activation_pct,
    )


def _overlay(entry_price=100.0, *, hard_floor_pct=-0.05, arm_aware=True,
             trail_pct=0.30, activation_pct=0.01):
    inner = _inner(entry_price, trail_pct=trail_pct, activation_pct=activation_pct)
    return HardFloorOverlay(
        entry_price=entry_price, horizon_days=10,
        inner=inner, hard_floor_pct=hard_floor_pct,
        intraday_arm_aware=arm_aware,
    )


# ── TrailingStopOnly.is_armed ───────────────────────────────────────────


def test_trailing_stop_is_not_armed_at_entry():
    inner = _inner(100.0, activation_pct=0.01)  # activation_price = 101
    assert inner.is_armed is False


def test_trailing_stop_arms_after_peak_crosses_activation():
    inner = _inner(100.0, activation_pct=0.01)
    # A bar whose high reaches 102 (> activation 101) updates the peak after
    # surviving the check, arming the trail for subsequent bars.
    inner.step(1, high=102.0, low=99.5, close=101.5, open_=100.0)
    assert inner.is_armed is True


def test_trailing_stop_stays_unarmed_below_activation():
    inner = _inner(100.0, activation_pct=0.01)
    inner.step(1, high=100.5, low=99.5, close=100.2, open_=100.0)  # peak 100.5 < 101
    assert inner.is_armed is False


# ── arm-aware: trail wins once armed ────────────────────────────────────


def test_armed_trail_wins_over_floor_same_bar():
    # entry 100, activation 101, floor 95, trail_pct 0.30.
    ov = _overlay(100.0, arm_aware=True)
    # Bar 1 arms the trail (peak 130 ≥ 101).
    assert ov.step(1, high=130.0, low=120.0, close=125.0, open_=121.0) == []
    assert ov.inner.is_armed is True
    # Bar 2 crashes through BOTH the trail stop (121 = 130 − 30*0.30) and the
    # floor (95). Arm-aware → the trail fires at 121, not the floor at 95.
    events = ov.step(2, high=126.0, low=94.0, close=96.0, open_=120.0)
    assert len(events) == 1
    assert events[0].reason == "trailing"
    assert events[0].exit_price == pytest.approx(121.0)
    assert ov.is_complete


# ── arm-aware: floor still active while unarmed ─────────────────────────


def test_unarmed_floor_fires_in_arm_aware_mode():
    # Price never reaches activation (101); a dip to the floor must still
    # trigger the hard floor.
    ov = _overlay(100.0, arm_aware=True)
    events = ov.step(1, high=100.5, low=94.0, close=96.0, open_=99.0)
    assert len(events) == 1
    assert events[0].reason == "hard_floor"
    assert events[0].exit_price == pytest.approx(95.0)


def test_same_bar_arm_and_floor_resolves_adverse_first():
    # One bar both arms (high 130 ≥ 101) and breaches the floor (low 94 ≤ 95).
    # Adverse-first convention → the floor fires (down-move precedes up-move).
    ov = _overlay(100.0, arm_aware=True)
    events = ov.step(1, high=130.0, low=94.0, close=96.0, open_=99.0)
    assert len(events) == 1
    assert events[0].reason == "hard_floor"
    assert events[0].exit_price == pytest.approx(95.0)


# ── default daily mode unchanged (regression guard) ─────────────────────


def test_daily_mode_floor_first_even_when_armed():
    # Same scenario as test_armed_trail_wins_over_floor_same_bar but with the
    # default daily floor-first mode → the floor must win at 95.
    ov = _overlay(100.0, arm_aware=False)
    ov.step(1, high=130.0, low=120.0, close=125.0, open_=121.0)
    events = ov.step(2, high=126.0, low=94.0, close=96.0, open_=120.0)
    assert events[0].reason == "hard_floor"
    assert events[0].exit_price == pytest.approx(95.0)
