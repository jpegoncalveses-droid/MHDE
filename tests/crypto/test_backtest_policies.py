"""Tests for crypto/execution/backtest/policies.py.

Covers all 5 spec policies (A–E) plus cross-cutting harness conventions:

    1. Same-bar TP+SL priority (SL wins)                 — Policy B
    2. Time stop firing on horizon day at close          — cross-cutting
    3. Trail peak update semantics (yesterday's peak)    — Policy D
    4. Policy E partial fill (50% off, residual trails)  — Policy E
    5. Entry-day convention (day_idx=1 is first bar)     — cross-cutting
    6. Gap moves where open exceeds TP from prior close  — cross-cutting
    7. Policy D initial peak behavior under activation   — Policy D

Plus three activation-threshold cases requested explicitly:

    8. Below activation threshold → no stop, rides time stop
    9. Crossing activation threshold mid-bar → trail engages next bar
   10. Peak exactly at entry × (1 + activation_pct) edge

Imports nothing from equity / FX / shared ML; uses no DuckDB.
"""
from __future__ import annotations

import pytest

from crypto.execution.backtest.policies import (
    ExitEvent,
    ExitPolicy,
    FixedTpAtrSl,
    FixedTpFixedSl,
    FixedTpNoStop,
    TieredExit,
    TrailingStopOnly,
    build_policy,
)


# ──────────────────────────────────────────────────────────────────────
# ExitEvent validation
# ──────────────────────────────────────────────────────────────────────


def test_exit_event_rejects_unknown_reason():
    with pytest.raises(ValueError, match="unknown exit reason"):
        ExitEvent(exit_price=100.0, fraction=1.0, reason="foo")


def test_exit_event_rejects_zero_or_negative_fraction():
    with pytest.raises(ValueError, match="fraction"):
        ExitEvent(exit_price=100.0, fraction=0.0, reason="tp")
    with pytest.raises(ValueError, match="fraction"):
        ExitEvent(exit_price=100.0, fraction=-0.1, reason="tp")


def test_exit_event_rejects_fraction_above_one():
    with pytest.raises(ValueError, match="fraction"):
        ExitEvent(exit_price=100.0, fraction=1.5, reason="tp")


def test_exit_event_rejects_non_positive_price():
    with pytest.raises(ValueError, match="exit_price"):
        ExitEvent(exit_price=0.0, fraction=1.0, reason="tp")
    with pytest.raises(ValueError, match="exit_price"):
        ExitEvent(exit_price=-1.0, fraction=1.0, reason="tp")


# ──────────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "pid, cls, params",
    [
        ("A", FixedTpNoStop, {}),
        ("B", FixedTpFixedSl, {}),
        ("C", FixedTpAtrSl, {"atr_pct": 0.02}),
        ("D", TrailingStopOnly, {}),
        ("E", TieredExit, {}),
    ],
)
def test_build_policy_returns_correct_class(pid, cls, params):
    p = build_policy(pid, entry_price=100.0, horizon_days=5, params=params)
    assert isinstance(p, cls)
    assert isinstance(p, ExitPolicy)


def test_build_policy_unknown_id_raises():
    with pytest.raises(ValueError, match="unknown policy id"):
        build_policy("Z", entry_price=100.0, horizon_days=5)


def test_build_policy_lowercase_id_accepted():
    p = build_policy("a", entry_price=100.0, horizon_days=5)
    assert isinstance(p, FixedTpNoStop)


# ──────────────────────────────────────────────────────────────────────
# Policy A — Fixed TP, no stop
# ──────────────────────────────────────────────────────────────────────


def test_policy_a_tp_fires_on_high_crossing():
    p = FixedTpNoStop(entry_price=100.0, horizon_days=5, tp_pct=0.05)
    events = p.step(day_idx=1, high=106.0, low=99.0, close=104.0)
    assert len(events) == 1
    assert events[0].reason == "tp"
    assert events[0].exit_price == pytest.approx(105.0)
    assert events[0].fraction == 1.0
    assert p.is_complete


def test_policy_a_no_stop_protection_low_below_entry():
    """Policy A has no SL: a deep drawdown does not exit until time stop."""
    p = FixedTpNoStop(entry_price=100.0, horizon_days=3, tp_pct=0.05)
    assert p.step(1, high=99.0, low=80.0, close=82.0) == []
    assert p.step(2, high=83.0, low=78.0, close=80.0) == []
    events = p.step(3, high=85.0, low=80.0, close=82.0)
    assert len(events) == 1
    assert events[0].reason == "time"
    assert events[0].exit_price == pytest.approx(82.0)


# ──────────────────────────────────────────────────────────────────────
# Policy B — Fixed TP + fixed SL  (case 1: same-bar SL wins)
# ──────────────────────────────────────────────────────────────────────


def test_policy_b_same_bar_tp_and_sl_sl_wins():
    """When both the TP and the SL could fire on the same bar, the SL
    fills first (conservative daily-bar convention)."""
    p = FixedTpFixedSl(entry_price=100.0, horizon_days=5, tp_pct=0.05, sl_pct=0.03)
    events = p.step(day_idx=1, high=106.0, low=96.5, close=101.0)
    assert len(events) == 1
    assert events[0].reason == "sl"
    assert events[0].exit_price == pytest.approx(97.0)
    assert p.is_complete


def test_policy_b_only_tp_hit_fires_tp():
    p = FixedTpFixedSl(entry_price=100.0, horizon_days=5)
    events = p.step(1, high=106.0, low=99.5, close=104.0)
    assert events[0].reason == "tp"


def test_policy_b_only_sl_hit_fires_sl():
    p = FixedTpFixedSl(entry_price=100.0, horizon_days=5)
    events = p.step(1, high=102.0, low=96.5, close=98.0)
    assert events[0].reason == "sl"


def test_policy_b_neither_hit_no_event():
    p = FixedTpFixedSl(entry_price=100.0, horizon_days=5)
    assert p.step(1, high=104.0, low=98.0, close=101.0) == []
    assert not p.is_complete


# ──────────────────────────────────────────────────────────────────────
# Policy C — Fixed TP + ATR-based SL
# ──────────────────────────────────────────────────────────────────────


def test_policy_c_atr_stop_uses_atr_pct_14d_at_entry():
    """atr_pct=0.02 with atr_mult=2 → SL at entry × (1 - 0.04) = entry × 0.96."""
    p = FixedTpAtrSl(entry_price=100.0, horizon_days=5, atr_pct=0.02, atr_mult=2.0)
    assert p.sl_price == pytest.approx(96.0)
    events = p.step(1, high=104.0, low=95.5, close=97.0)
    assert events[0].reason == "sl"
    assert events[0].exit_price == pytest.approx(96.0)


def test_policy_c_atr_stop_is_fixed_not_dynamic():
    """A new peak does not move the SL — it stays at its entry-day level."""
    p = FixedTpAtrSl(entry_price=100.0, horizon_days=5, atr_pct=0.02, atr_mult=2.0)
    p.step(1, high=104.0, low=99.0, close=103.0)        # rallies
    p.step(2, high=104.5, low=100.0, close=104.0)       # new peak, but SL doesn't move
    events = p.step(3, high=104.0, low=95.5, close=96.0)
    assert events[0].reason == "sl"
    assert events[0].exit_price == pytest.approx(96.0)  # original SL level


def test_policy_c_rejects_negative_atr_pct():
    with pytest.raises(ValueError, match="atr_pct"):
        FixedTpAtrSl(entry_price=100.0, horizon_days=5, atr_pct=-0.01)


# ──────────────────────────────────────────────────────────────────────
# Policy D — Trailing stop with activation threshold
# ──────────────────────────────────────────────────────────────────────


def test_policy_d_default_activation_is_one_percent():
    p = TrailingStopOnly(entry_price=100.0, horizon_days=20)
    assert p.activation_pct == pytest.approx(0.01)
    assert p.activation_price == pytest.approx(101.0)


def test_policy_d_rejects_negative_activation_pct():
    with pytest.raises(ValueError, match="activation_pct"):
        TrailingStopOnly(entry_price=100.0, horizon_days=5, activation_pct=-0.01)


# Case 7: initial peak behavior — peak starts at entry, trail not armed
def test_policy_d_initial_peak_below_activation_no_trail():
    """Day 1: peak == entry_price < activation_price. No trail check."""
    p = TrailingStopOnly(
        entry_price=100.0, horizon_days=5, trail_pct=0.5, activation_pct=0.01
    )
    # Big drawdown on day 1 — would fire any active trail; doesn't here.
    assert p.step(1, high=100.5, low=80.0, close=82.0) == []
    assert not p.is_complete


# Case 8: position survives drawdown while below activation threshold
def test_policy_d_below_activation_threshold_survives_drawdown():
    """Peak never crosses activation_price → trail never arms → ride time stop."""
    p = TrailingStopOnly(
        entry_price=100.0, horizon_days=4, trail_pct=0.5, activation_pct=0.01
    )
    # Sequence stays below 101.0 for entire horizon, with low dips.
    p.step(1, high=100.8, low=99.0, close=99.5)
    p.step(2, high=100.9, low=98.5, close=99.0)
    p.step(3, high=100.95, low=98.0, close=99.5)
    events = p.step(4, high=100.7, low=98.5, close=99.2)
    assert len(events) == 1
    assert events[0].reason == "time"
    assert events[0].exit_price == pytest.approx(99.2)


# Case 9: crossing activation threshold mid-trade arms trail (next bar)
def test_policy_d_crossing_activation_arms_trail_on_next_bar():
    """Day where high >= activation: peak rises but trail check uses
    yesterday's peak. Next day's low can fire the now-armed trail."""
    p = TrailingStopOnly(
        entry_price=100.0, horizon_days=10, trail_pct=0.5, activation_pct=0.01
    )
    # Day 1: high 102 (above activation). peak_high becomes 102.0.
    assert p.step(1, high=102.0, low=99.5, close=101.5) == []
    assert p.peak_high == pytest.approx(102.0)
    # Day 2: trail_stop = 102 - (102-100)*0.5 = 101.0. low=100.8 < 101 fires.
    events = p.step(2, high=102.5, low=100.8, close=101.0)
    assert len(events) == 1
    assert events[0].reason == "trailing"
    assert events[0].exit_price == pytest.approx(101.0)


# Case 10: peak exactly at activation_price edge
def test_policy_d_peak_exactly_at_activation_edge_arms_trail():
    """`>=` activation means peak == activation_price arms the trail."""
    p = TrailingStopOnly(
        entry_price=100.0, horizon_days=10, trail_pct=0.5, activation_pct=0.01
    )
    # Day 1 high == activation exactly.
    p.step(1, high=101.0, low=100.0, close=100.5)
    assert p.peak_high == pytest.approx(101.0)
    # Day 2: trail_stop = 101 - (101-100)*0.5 = 100.5.
    events = p.step(2, high=101.0, low=100.4, close=100.4)
    assert len(events) == 1
    assert events[0].reason == "trailing"
    assert events[0].exit_price == pytest.approx(100.5)


def test_policy_d_peak_just_below_activation_does_not_arm():
    """Peak at 100.99 with activation 1% (= 101.0) → trail still inactive."""
    p = TrailingStopOnly(
        entry_price=100.0, horizon_days=10, trail_pct=0.5, activation_pct=0.01
    )
    p.step(1, high=100.99, low=100.0, close=100.5)
    # Day 2: peak 100.99 < 101.0; trail not armed; deep low does not fire.
    assert p.step(2, high=100.99, low=98.0, close=99.0) == []


# Case 3: trail uses yesterday's peak, not today's high
def test_policy_d_trail_uses_prior_bar_peak():
    """Today's new high does NOT move the stop in time to be checked today."""
    p = TrailingStopOnly(
        entry_price=100.0, horizon_days=10, trail_pct=0.5, activation_pct=0.01
    )
    p.step(1, high=104.0, low=99.5, close=103.0)   # peak → 104, trail armed
    # Day 2: yesterday's peak 104 → trail_stop = 104 - (104-100)*0.5 = 102.
    # Today's new high 110 should NOT raise stop to 110-(110-100)*0.5 = 105 in time
    # to fire today. So today's low=101 fires the stop at 102 (not at 105).
    events = p.step(2, high=110.0, low=101.0, close=109.0)
    assert len(events) == 1
    assert events[0].reason == "trailing"
    assert events[0].exit_price == pytest.approx(102.0)


def test_policy_d_zero_activation_matches_legacy_behavior():
    """activation_pct=0 → trail arms once any positive bar prints."""
    p = TrailingStopOnly(
        entry_price=100.0, horizon_days=10, trail_pct=0.5, activation_pct=0.0
    )
    # Day 1: tiny positive bar. Peak rises slightly to 100.10.
    p.step(1, high=100.10, low=100.0, close=100.05)
    # Day 2: trail_stop = 100.10 - 0.10*0.5 = 100.05. Low 100.04 fires.
    events = p.step(2, high=100.10, low=100.04, close=100.04)
    assert len(events) == 1
    assert events[0].reason == "trailing"
    assert events[0].exit_price == pytest.approx(100.05)


def test_policy_d_zero_activation_day_one_no_trail_when_peak_eq_entry():
    """activation=0 still requires strict peak > entry for a meaningful stop."""
    p = TrailingStopOnly(
        entry_price=100.0, horizon_days=5, trail_pct=0.5, activation_pct=0.0
    )
    # Day 1: high <= entry → peak never moves above entry → no trail.
    assert p.step(1, high=100.0, low=98.0, close=99.0) == []
    assert not p.is_complete


# ──────────────────────────────────────────────────────────────────────
# Policy E — Tiered exit (case 4: partial fill + residual trail)
# ──────────────────────────────────────────────────────────────────────


def test_policy_e_tp_takes_50_percent_then_trail_engages():
    """Day 1 hits TP → 50% exits at TP. Day 2 peak rises. Day 3 low
    fires trail on the residual 50%."""
    p = TieredExit(
        entry_price=100.0, horizon_days=10,
        tp_pct=0.05, tp_fraction=0.5, trail_pct=0.5,
    )
    # Day 1: high crosses TP.
    events = p.step(1, high=106.0, low=99.0, close=105.0)
    assert len(events) == 1
    assert events[0].reason == "tp"
    assert events[0].exit_price == pytest.approx(105.0)
    assert events[0].fraction == pytest.approx(0.5)
    assert p.remaining_fraction == pytest.approx(0.5)
    assert p.tp_taken
    # peak_high updated to today's high (106) post-TP.
    assert p.peak_high == pytest.approx(106.0)

    # Day 2: peak rises to 110. No exit (low=105 > yesterday's trail_stop
    # = 106 - (106-100)*0.5 = 103).
    assert p.step(2, high=110.0, low=105.0, close=109.0) == []
    assert p.peak_high == pytest.approx(110.0)

    # Day 3: trail_stop = 110 - (110-100)*0.5 = 105. low=104 fires.
    events = p.step(3, high=110.0, low=104.0, close=105.0)
    assert len(events) == 1
    assert events[0].reason == "trailing"
    assert events[0].exit_price == pytest.approx(105.0)
    assert events[0].fraction == pytest.approx(0.5)
    assert p.is_complete


def test_policy_e_trail_inactive_before_tp():
    """Until the TP fires, the trail does not guard the position. A deep
    drawdown survives if it doesn't reach the (non-existent) stop level."""
    p = TieredExit(
        entry_price=100.0, horizon_days=5,
        tp_pct=0.05, tp_fraction=0.5, trail_pct=0.5,
    )
    # Rally to 104 (below TP) then deep retrace.
    assert p.step(1, high=104.0, low=99.0, close=103.0) == []
    # Day 2: low 90, no trail because TP not yet taken.
    assert p.step(2, high=103.0, low=90.0, close=92.0) == []
    assert not p.is_complete


def test_policy_e_time_stop_takes_remaining_after_tp():
    """After day-1 TP at peak=106, trail_stop on day 2 = 106 − 0.5×6 = 103.
    Day-2 low must stay above 103 so the residual reaches the time stop."""
    p = TieredExit(
        entry_price=100.0, horizon_days=2,
        tp_pct=0.05, tp_fraction=0.5, trail_pct=0.5,
    )
    p.step(1, high=106.0, low=99.0, close=104.0)   # TP partial; peak → 106
    events = p.step(2, high=105.0, low=103.5, close=104.0)
    assert len(events) == 1
    assert events[0].reason == "time"
    assert events[0].fraction == pytest.approx(0.5)
    assert events[0].exit_price == pytest.approx(104.0)


# ──────────────────────────────────────────────────────────────────────
# Cross-cutting cases
# ──────────────────────────────────────────────────────────────────────


# Case 2: time stop on horizon day at close
def test_time_stop_fires_at_horizon_day_close():
    """For each policy that doesn't otherwise exit, day_idx == horizon
    fires the time stop at the bar's close.

    Bar pattern is deliberately tight — high=100.5 stays below Policy D's
    1% activation threshold (101) so D's trail never arms; lows stay
    above all SL levels (-3% / -10%); no policy's TP at +5% triggers.
    """
    for cls, kwargs in [
        (FixedTpNoStop,    {}),
        (FixedTpFixedSl,   {}),
        (FixedTpAtrSl,     {"atr_pct": 0.05}),
        (TrailingStopOnly, {}),
        (TieredExit,       {}),
    ]:
        p = cls(entry_price=100.0, horizon_days=3, **kwargs)
        p.step(1, high=100.5, low=99.5, close=100.0)
        p.step(2, high=100.5, low=99.5, close=100.0)
        events = p.step(3, high=100.5, low=99.5, close=100.7)
        assert len(events) == 1, f"{cls.__name__}: expected one event, got {events}"
        assert events[0].reason == "time", f"{cls.__name__}: {events[0].reason}"
        assert events[0].exit_price == pytest.approx(100.7)
        assert p.is_complete


# Case 6: gap above TP — exits at TP, not at the gap-open price
def test_gap_above_tp_exits_at_tp_level_not_gap_price():
    """Conservative limit-fill convention: even if the bar gaps fully
    above TP, the exit price is the TP level (not the gap open)."""
    p = FixedTpNoStop(entry_price=100.0, horizon_days=5, tp_pct=0.05)
    # Bar gaps massively: high 112, low 110, close 111.
    events = p.step(1, high=112.0, low=110.0, close=111.0)
    assert len(events) == 1
    assert events[0].reason == "tp"
    assert events[0].exit_price == pytest.approx(105.0)   # tp level, not 110+


# Case 5: entry-day convention (harness-driven; day_idx=1 is first post-entry bar)
def test_day_idx_1_is_first_post_entry_bar():
    """Documenting the harness contract: day_idx=1 is the first bar after
    entry. The policy itself accepts and processes day_idx=1 normally —
    a TP can fire there if the bar's high crosses the level."""
    p = FixedTpNoStop(entry_price=100.0, horizon_days=5, tp_pct=0.05)
    events = p.step(day_idx=1, high=106.0, low=99.0, close=104.0)
    assert len(events) == 1
    assert events[0].reason == "tp"
    # If the harness incorrectly skipped this bar, the trade would
    # remain open here (regression guard).
    assert p.is_complete


def test_step_after_completion_returns_empty():
    """Defensive: once the policy is complete, additional step() calls
    return [] without raising or mutating state."""
    p = FixedTpNoStop(entry_price=100.0, horizon_days=5, tp_pct=0.05)
    p.step(1, high=106.0, low=99.0, close=104.0)
    assert p.is_complete
    assert p.step(2, high=110.0, low=90.0, close=100.0) == []


# ──────────────────────────────────────────────────────────────────────
# Constructor validation (light coverage for the rest)
# ──────────────────────────────────────────────────────────────────────


def test_policy_rejects_non_positive_entry_price():
    with pytest.raises(ValueError, match="entry_price"):
        FixedTpNoStop(entry_price=0.0, horizon_days=5)


def test_policy_rejects_non_positive_horizon():
    with pytest.raises(ValueError, match="horizon_days"):
        FixedTpNoStop(entry_price=100.0, horizon_days=0)


def test_trailing_rejects_invalid_trail_pct():
    with pytest.raises(ValueError, match="trail_pct"):
        TrailingStopOnly(entry_price=100.0, horizon_days=5, trail_pct=0.0)
    with pytest.raises(ValueError, match="trail_pct"):
        TrailingStopOnly(entry_price=100.0, horizon_days=5, trail_pct=1.5)


def test_tiered_rejects_invalid_tp_fraction():
    with pytest.raises(ValueError, match="tp_fraction"):
        TieredExit(entry_price=100.0, horizon_days=5, tp_fraction=0.0)
    with pytest.raises(ValueError, match="tp_fraction"):
        TieredExit(entry_price=100.0, horizon_days=5, tp_fraction=1.0)
