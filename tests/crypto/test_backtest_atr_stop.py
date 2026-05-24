"""Tests for the entry-time static ATR stop-loss overlay on Policy D.

The overlay (``AtrStopOverlay`` in ``policies.py``) wraps an inner exit
policy and applies a static ATR stop set once at entry, reusing Policy C's
level mechanic (``sl_price = entry_price * (1 - atr_multiple * atr_pct)``)
but adding conservative gap-through modelling (fill at ``min(stop, open)``)
that Policy C does not have.

Cases (per the Phase 2 dispatch):
  1. Control mode (no stop) reproduces current behaviour exactly.
  2. ATR stop fires when the daily low breaches the level; reason/price.
  3. Entry-time ATR is used, not recomputed mid-trade (static level).
  4. Gap-through: day opens below stop → exit at the open.
  5. Stop vs trail same day → stop takes priority.
  6. ``stop_loss`` counted correctly in the metrics exit breakdown.

Pure-Python policy/harness tests use no DuckDB; the metrics breakdown
test uses the in-memory ``temp_db`` fixture.
"""
from __future__ import annotations

from datetime import date

import pytest

from crypto.execution.backtest.harness import (
    Position,
    _build_position,
    _walk_position_forward,
    ensure_backtest_tables,
)
from crypto.execution.backtest.metrics import EXIT_REASONS, compute_summary
from crypto.execution.backtest.policies import (
    AtrStopOverlay,
    ExitEvent,
    TrailingStopOnly,
    build_policy,
)


def _overlay(entry_price=100.0, horizon_days=10, *, atr_pct=0.02,
             atr_multiple=2.0, trail_pct=0.5, activation_pct=0.01):
    inner = TrailingStopOnly(
        entry_price=entry_price, horizon_days=horizon_days,
        trail_pct=trail_pct, activation_pct=activation_pct,
    )
    return AtrStopOverlay(
        entry_price=entry_price, horizon_days=horizon_days,
        inner=inner, atr_pct=atr_pct, atr_multiple=atr_multiple,
    )


# ── ExitEvent / valid reasons ─────────────────────────────────────────


def test_stop_loss_is_a_valid_exit_reason():
    # Must not raise — 'stop_loss' is a recognised reason.
    ev = ExitEvent(exit_price=96.0, fraction=1.0, reason="stop_loss")
    assert ev.reason == "stop_loss"


# ── Case 2: fires on low breach; reason + price correct ───────────────


def test_atr_stop_fires_on_low_breach_at_stop_level():
    """atr_pct=0.02, mult=2 → sl = 100*(1-0.04)=96. low 95.5 breaches;
    open above stop so the fill is the stop level, not the open."""
    p = _overlay(atr_pct=0.02, atr_multiple=2.0)
    assert p.sl_price == pytest.approx(96.0)
    events = p.step(1, high=99.0, low=95.5, close=97.0, open_=98.0)
    assert len(events) == 1
    assert events[0].reason == "stop_loss"
    assert events[0].exit_price == pytest.approx(96.0)
    assert events[0].fraction == pytest.approx(1.0)
    assert p.is_complete


def test_atr_stop_does_not_fire_when_low_above_level():
    p = _overlay(atr_pct=0.02, atr_multiple=2.0)   # sl = 96.0
    assert p.step(1, high=100.5, low=96.5, close=99.0, open_=99.5) == []
    assert not p.is_complete


# ── Case 4: gap-through → fill at the open (conservative) ─────────────


def test_atr_stop_gap_through_fills_at_open_below_stop():
    """Day opens at 94 (below the 96 stop) and trades lower → the
    realistic fill is the open, not the stop level."""
    p = _overlay(atr_pct=0.02, atr_multiple=2.0)   # sl = 96.0
    events = p.step(1, high=95.0, low=93.0, close=94.5, open_=94.0)
    assert len(events) == 1
    assert events[0].reason == "stop_loss"
    assert events[0].exit_price == pytest.approx(94.0)   # min(96, 94)


def test_atr_stop_open_above_stop_fills_at_stop_level():
    """Open above the stop, intraday low breaches → fill at the stop."""
    p = _overlay(atr_pct=0.02, atr_multiple=2.0)   # sl = 96.0
    events = p.step(1, high=99.0, low=95.0, close=96.5, open_=98.0)
    assert events[0].exit_price == pytest.approx(96.0)   # min(96, 98)


# ── Case 3: entry-time ATR is static, not recomputed mid-trade ────────


def test_atr_stop_level_is_static_after_a_rally():
    """A rally must not move the stop — it stays at the entry-day level."""
    p = _overlay(atr_pct=0.02, atr_multiple=2.0)   # sl = 96.0
    assert p.step(1, high=110.0, low=100.0, close=109.0, open_=100.5) == []
    # Stop level unchanged despite the new peak.
    events = p.step(2, high=108.0, low=95.9, close=97.0, open_=104.0)
    assert events[0].reason == "stop_loss"
    assert events[0].exit_price == pytest.approx(96.0)


# ── Case 5: stop takes priority over the trail on the same bar ────────


def test_atr_stop_takes_priority_over_trail_same_bar():
    """Day 1 arms the trail (peak 110). Day 2 the low (95) breaches both
    the trail stop (105) and the ATR stop (96); the ATR stop wins."""
    p = _overlay(atr_pct=0.02, atr_multiple=2.0, trail_pct=0.5)  # sl = 96
    assert p.step(1, high=110.0, low=99.0, close=109.0, open_=100.0) == []
    events = p.step(2, high=110.0, low=95.0, close=104.0, open_=100.0)
    assert len(events) == 1
    assert events[0].reason == "stop_loss"           # not 'trailing'
    assert events[0].exit_price == pytest.approx(96.0)  # min(96, open=100)


def test_atr_stop_delegates_to_inner_when_not_breached():
    """When the ATR stop is untouched, the inner trail still fires."""
    p = _overlay(atr_pct=0.10, atr_multiple=2.0, trail_pct=0.5)  # sl = 80
    assert p.step(1, high=110.0, low=99.0, close=109.0, open_=100.0) == []
    # trail_stop = 110 - (110-100)*0.5 = 105; low 104 fires the trail,
    # ATR stop (80) untouched.
    events = p.step(2, high=110.0, low=104.0, close=105.0, open_=109.0)
    assert len(events) == 1
    assert events[0].reason == "trailing"
    assert events[0].exit_price == pytest.approx(105.0)


# ── Constructor validation ────────────────────────────────────────────


def test_atr_overlay_rejects_non_positive_multiple():
    inner = TrailingStopOnly(entry_price=100.0, horizon_days=5)
    with pytest.raises(ValueError, match="atr_multiple"):
        AtrStopOverlay(entry_price=100.0, horizon_days=5, inner=inner,
                       atr_pct=0.02, atr_multiple=0.0)


def test_atr_overlay_rejects_negative_atr_pct():
    inner = TrailingStopOnly(entry_price=100.0, horizon_days=5)
    with pytest.raises(ValueError, match="atr_pct"):
        AtrStopOverlay(entry_price=100.0, horizon_days=5, inner=inner,
                       atr_pct=-0.01, atr_multiple=2.0)


# ── _build_position wiring ────────────────────────────────────────────


def test_build_position_no_stop_returns_plain_policy_d():
    """Control: stop_mode absent → plain TrailingStopOnly, no overlay."""
    pos, reason = _build_position(
        coin="BTC", pred_date=date(2025, 4, 5), entry_date=date(2025, 4, 6),
        entry_price=100.0, horizon="10d", horizon_days=10, exit_policy_id="D",
        policy_params={}, probability=0.7,
        atr_lookup={("BTC", date(2025, 4, 5)): 0.02}, trade_id="t1",
    )
    assert reason is None
    assert isinstance(pos.policy, TrailingStopOnly)
    assert not isinstance(pos.policy, AtrStopOverlay)


def test_build_position_stop_mode_none_returns_plain_policy_d():
    pos, reason = _build_position(
        coin="BTC", pred_date=date(2025, 4, 5), entry_date=date(2025, 4, 6),
        entry_price=100.0, horizon="10d", horizon_days=10, exit_policy_id="D",
        policy_params={"stop_mode": "none"}, probability=0.7,
        atr_lookup={("BTC", date(2025, 4, 5)): 0.02}, trade_id="t1",
    )
    assert reason is None
    assert isinstance(pos.policy, TrailingStopOnly)
    assert not isinstance(pos.policy, AtrStopOverlay)


def test_build_position_atr_stop_wraps_policy_d_with_overlay():
    pos, reason = _build_position(
        coin="BTC", pred_date=date(2025, 4, 5), entry_date=date(2025, 4, 6),
        entry_price=100.0, horizon="10d", horizon_days=10, exit_policy_id="D",
        policy_params={"stop_mode": "atr", "atr_multiple": 1.5},
        probability=0.7,
        atr_lookup={("BTC", date(2025, 4, 5)): 0.02}, trade_id="t1",
    )
    assert reason is None
    assert isinstance(pos.policy, AtrStopOverlay)
    # sl = 100 * (1 - 1.5 * 0.02) = 97.0
    assert pos.policy.sl_price == pytest.approx(97.0)
    # inner is still Policy D
    assert isinstance(pos.policy.inner, TrailingStopOnly)


def test_build_position_atr_stop_missing_atr_skips():
    """Reuses the engine's existing skip — no fabricated fallback."""
    pos, reason = _build_position(
        coin="BTC", pred_date=date(2025, 4, 5), entry_date=date(2025, 4, 6),
        entry_price=100.0, horizon="10d", horizon_days=10, exit_policy_id="D",
        policy_params={"stop_mode": "atr", "atr_multiple": 1.5},
        probability=0.7, atr_lookup={}, trade_id="t1",
    )
    assert pos is None
    assert reason == "missing_atr"


def test_build_position_require_atr_skips_control_when_atr_missing():
    """require_atr forces the missing-ATR skip on a no-stop control run so
    its entry set matches the ATR-stop runs (no universe contamination)."""
    pos, reason = _build_position(
        coin="BTC", pred_date=date(2025, 4, 5), entry_date=date(2025, 4, 6),
        entry_price=100.0, horizon="10d", horizon_days=10, exit_policy_id="D",
        policy_params={"require_atr": True}, probability=0.7,
        atr_lookup={}, trade_id="t1",
    )
    assert pos is None
    assert reason == "missing_atr"


def test_build_position_require_atr_builds_plain_policy_d_when_atr_present():
    """require_atr only gates entry — it does not attach a stop overlay."""
    pos, reason = _build_position(
        coin="BTC", pred_date=date(2025, 4, 5), entry_date=date(2025, 4, 6),
        entry_price=100.0, horizon="10d", horizon_days=10, exit_policy_id="D",
        policy_params={"require_atr": True}, probability=0.7,
        atr_lookup={("BTC", date(2025, 4, 5)): 0.02}, trade_id="t1",
    )
    assert reason is None
    assert isinstance(pos.policy, TrailingStopOnly)
    assert not isinstance(pos.policy, AtrStopOverlay)


def test_build_position_without_require_atr_does_not_skip_control():
    """Regression: default control behaviour is unchanged — a missing ATR
    does NOT skip a plain Policy D run (this is the contamination the
    Phase-3 grid avoids via require_atr, not a change to defaults)."""
    pos, reason = _build_position(
        coin="BTC", pred_date=date(2025, 4, 5), entry_date=date(2025, 4, 6),
        entry_price=100.0, horizon="10d", horizon_days=10, exit_policy_id="D",
        policy_params={}, probability=0.7, atr_lookup={}, trade_id="t1",
    )
    assert reason is None
    assert isinstance(pos.policy, TrailingStopOnly)


def test_build_position_unknown_stop_mode_skips():
    pos, reason = _build_position(
        coin="BTC", pred_date=date(2025, 4, 5), entry_date=date(2025, 4, 6),
        entry_price=100.0, horizon="10d", horizon_days=10, exit_policy_id="D",
        policy_params={"stop_mode": "bogus"}, probability=0.7,
        atr_lookup={("BTC", date(2025, 4, 5)): 0.02}, trade_id="t1",
    )
    assert pos is None
    assert reason is not None and "stop_mode" in reason


# ── Case 1: control mode reproduces current behaviour exactly ─────────


def test_walk_control_mode_matches_plain_policy_d_exactly():
    """A no-stop build walks identically to a hand-built Policy D over the
    same path (exit reason, price, date, fraction)."""
    ohlcv = {
        ("BTC", date(2025, 4, 7)): (100.0, 104.0, 99.0, 103.0),
        ("BTC", date(2025, 4, 8)): (103.0, 110.0, 101.0, 109.0),
        ("BTC", date(2025, 4, 9)): (109.0, 110.0, 101.0, 102.0),  # trail fires
        ("BTC", date(2025, 4, 10)): (102.0, 103.0, 101.0, 102.0),
    }
    # Control build (no stop).
    pos_ctrl, _ = _build_position(
        coin="BTC", pred_date=date(2025, 4, 6), entry_date=date(2025, 4, 7),
        entry_price=100.0, horizon="10d", horizon_days=10, exit_policy_id="D",
        policy_params={}, probability=0.7, atr_lookup={}, trade_id="ctrl",
    )
    _walk_position_forward(pos_ctrl, horizon_days=10, ohlcv_by_key=ohlcv)

    # Hand-built plain Policy D.
    plain = build_policy("D", entry_price=100.0, horizon_days=10, params={})
    pos_plain = Position(
        trade_id="plain", coin="BTC", entry_date=date(2025, 4, 7),
        entry_price=100.0, horizon="10d", policy=plain, probability_at_entry=0.7,
    )
    _walk_position_forward(pos_plain, horizon_days=10, ohlcv_by_key=ohlcv)

    assert pos_ctrl.exit_reason == pos_plain.exit_reason
    assert pos_ctrl.exit_price == pytest.approx(pos_plain.exit_price)
    assert pos_ctrl.exit_date == pos_plain.exit_date
    assert [(e.reason, e.fraction) for e in pos_ctrl.exits] == \
           [(e.reason, e.fraction) for e in pos_plain.exits]


# ── Case 4 (integration): gap-through through the harness walk loop ───


def test_walk_atr_stop_gap_through_uses_bar_open():
    """The walk loop must pass the bar's open so the overlay can model a
    gap-through fill at the open."""
    overlay = _overlay(atr_pct=0.02, atr_multiple=2.0)  # sl = 96
    pos = Position(
        trade_id="g1", coin="BTC", entry_date=date(2025, 4, 7),
        entry_price=100.0, horizon="10d", policy=overlay,
        probability_at_entry=0.7,
    )
    ohlcv = {
        # Day 1 gaps down: open 94 (< stop 96), low 93.
        ("BTC", date(2025, 4, 8)): (94.0, 95.0, 93.0, 94.5),
    }
    _walk_position_forward(pos, horizon_days=10, ohlcv_by_key=ohlcv)
    assert pos.exit_reason == "stop_loss"
    assert pos.exit_price == pytest.approx(94.0)
    assert pos.exit_date == date(2025, 4, 8)


# ── Case 6: stop_loss counted in the metrics exit breakdown ───────────


def test_stop_loss_in_exit_reasons_constant():
    assert "stop_loss" in EXIT_REASONS


def _seed_run(conn, run_id):
    conn.execute(
        "INSERT INTO crypto_backtest_runs (run_id, horizon, exit_policy, "
        "selection_rule, parameters, date_start, date_end, "
        "n_predictions_seen, n_trades) VALUES (?,?,?,?,?,?,?,?,?)",
        [run_id, "10d", "D", "top_n", '{}', date(2025, 4, 5),
         date(2025, 5, 5), 100, 4],
    )


def _seed_trade(conn, run_id, tid, reason, net):
    conn.execute(
        "INSERT INTO crypto_backtest_trades (run_id, trade_id, coin, "
        "entry_date, entry_price, exit_date, exit_price, exit_reason, "
        "holding_days, gross_pnl_pct, fee_pct, slippage_pct, funding_pct, "
        "net_pnl_pct, probability_at_entry, forward_fill_days) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [run_id, tid, "BTCUSDT", date(2025, 4, 6), 100.0, date(2025, 4, 8),
         100.0 + net, reason, 2, net / 100.0, 0.0, 0.0, 0.0,
         net / 100.0, 0.7, 0],
    )


def test_metrics_counts_stop_loss_in_exit_breakdown(temp_db):
    ensure_backtest_tables(temp_db)
    run_id = "atr-stop-metrics"
    _seed_run(temp_db, run_id)
    # 2 stop_loss losers, 1 trailing winner, 1 time loser → 4 trades.
    _seed_trade(temp_db, run_id, "t1", "stop_loss", -2.0)
    _seed_trade(temp_db, run_id, "t2", "stop_loss", -2.0)
    _seed_trade(temp_db, run_id, "t3", "trailing", +3.0)
    _seed_trade(temp_db, run_id, "t4", "time", -1.0)

    summary = compute_summary(temp_db, run_id)
    assert summary.pct_exits_stop_loss == pytest.approx(0.5)   # 2 of 4
    assert summary.pct_exits_trailing == pytest.approx(0.25)
    assert summary.pct_exits_time == pytest.approx(0.25)
    # Breakdown fractions sum to 1 across all reasons.
    total = (summary.pct_exits_tp + summary.pct_exits_sl
             + summary.pct_exits_trailing + summary.pct_exits_time
             + summary.pct_exits_data_gap + summary.pct_exits_stop_loss)
    assert total == pytest.approx(1.0)
