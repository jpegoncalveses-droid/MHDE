"""Tests for the intraday faithful-replay engine (pure logic).

Covers the pluggable entry rules, the 1-minute exit walk (floor / trail /
time-stop, first-touch ordering, within-bar adverse-first tie), the
``up1_before_dn5`` path metric, and net-return fee accounting. No DB or
network — bars are synthetic dicts.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from crypto.execution.backtest.costs import TradeCosts
from crypto.execution.backtest.intraday_replay import (
    DeployedEntry,
    FixedOffsetEntry,
    Prediction,
    compute_net_return,
    simulate_intraday_trade,
)


def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def _bar(t, o, hi, lo, c, v=10.0):
    return {"open_time": t, "open": o, "high": hi, "low": lo, "close": c, "volume": v}


def _series(start, ohlc_list):
    """Build a 1-minute bar series from a list of (o, hi, lo, c) tuples."""
    out = []
    t = start
    for (o, hi, lo, c) in ohlc_list:
        out.append(_bar(t, o, hi, lo, c))
        t = t + timedelta(minutes=1)
    return out


# ── Entry rules ─────────────────────────────────────────────────────────


def test_deployed_entry_picks_prediction_date_plus_one_at_0045():
    # prediction_date = features day (T-1); live entry is the NEXT day 00:45.
    pred = Prediction(symbol="BTCUSDT", prediction_date=date(2026, 2, 6),
                      probability=0.8)
    bars = _series(_utc(2026, 2, 7, 0, 43), [
        (1, 2, 0.5, 1.5),   # 00:43
        (2, 3, 1.5, 2.5),   # 00:44
        (100.0, 101, 99, 100.5),  # 00:45  ← entry bar
        (3, 4, 2.5, 3.5),   # 00:46
    ])
    entry = DeployedEntry().resolve(pred, bars)
    assert entry is not None
    assert entry.entry_time == _utc(2026, 2, 7, 0, 45)
    assert entry.entry_price == 100.0  # the open of the 00:45 bar


def test_deployed_entry_returns_none_when_0045_bar_absent():
    pred = Prediction(symbol="BTCUSDT", prediction_date=date(2026, 2, 6),
                      probability=0.8)
    # 00:45 bar missing (gap).
    bars = [
        _bar(_utc(2026, 2, 7, 0, 44), 2, 3, 1, 2),
        _bar(_utc(2026, 2, 7, 0, 46), 3, 4, 2, 3),
    ]
    assert DeployedEntry().resolve(pred, bars) is None


def test_fixed_offset_entry_picks_offset_hour_bar():
    pred = Prediction(symbol="BTCUSDT", prediction_date=date(2026, 2, 6),
                      probability=0.8)
    bars = [
        _bar(_utc(2026, 2, 7, 0, 45), 100, 101, 99, 100.5),
        _bar(_utc(2026, 2, 7, 6, 0), 200, 201, 199, 200.5),  # +6h bar
    ]
    entry = FixedOffsetEntry(hours=6).resolve(pred, bars)
    assert entry is not None
    assert entry.entry_time == _utc(2026, 2, 7, 6, 0)
    assert entry.entry_price == 200.0


def test_deployed_entry_day_offset_zero_uses_prediction_date():
    pred = Prediction(symbol="BTCUSDT", prediction_date=date(2026, 2, 7),
                      probability=0.8)
    bars = [_bar(_utc(2026, 2, 7, 0, 45), 100, 101, 99, 100.5)]
    entry = DeployedEntry(day_offset=0).resolve(pred, bars)
    assert entry is not None
    assert entry.entry_time == _utc(2026, 2, 7, 0, 45)


# ── simulate: exit reasons ──────────────────────────────────────────────


def test_simulate_floor_exit_while_unarmed():
    # Never reaches +1% activation; a dip to −5% (95) floors.
    bars = _series(_utc(2026, 2, 7, 0, 45), [
        (100.0, 100.5, 99.5, 100.0),
        (100.0, 100.4, 94.0, 96.0),   # low 94 ≤ floor 95
    ])
    out = simulate_intraday_trade(100.0, bars)
    assert out.exit_reason == "hard_floor"
    assert out.exit_price == pytest.approx(95.0)


def test_simulate_trail_exit_once_armed():
    # Arms (peak 130 ≥ 101), then gives back to the trail stop (121).
    bars = _series(_utc(2026, 2, 7, 0, 45), [
        (100.0, 130.0, 120.0, 125.0),   # arms; peak 130
        (125.0, 126.0, 120.9, 121.0),   # low 120.9 ≤ trail 121 → trailing
    ])
    out = simulate_intraday_trade(100.0, bars)
    assert out.exit_reason == "trailing"
    assert out.exit_price == pytest.approx(121.0)  # 130 − (130−100)*0.30


def test_simulate_time_stop_at_last_bar_close():
    bars = _series(_utc(2026, 2, 7, 0, 45), [
        (100.0, 100.6, 99.6, 100.2),
        (100.2, 100.7, 99.7, 100.3),
        (100.3, 100.8, 99.8, 100.4),   # last bar → time stop at close 100.4
    ])
    out = simulate_intraday_trade(100.0, bars)
    assert out.exit_reason == "time"
    assert out.exit_price == pytest.approx(100.4)
    assert out.exit_time == _utc(2026, 2, 7, 0, 47)


# ── simulate: ordering + within-bar tie ─────────────────────────────────


def test_simulate_first_touch_floor_beats_later_trail():
    # Bar 1 floors immediately; a later bar that *would* have trailed must
    # never be reached (earliest trigger wins).
    bars = _series(_utc(2026, 2, 7, 0, 45), [
        (100.0, 100.2, 94.0, 95.0),    # floors here
        (130.0, 140.0, 100.0, 121.0),  # would arm+trail, but trade is closed
    ])
    out = simulate_intraday_trade(100.0, bars)
    assert out.exit_reason == "hard_floor"
    assert out.hold_minutes == 0  # exited on the entry bar itself


def test_simulate_within_bar_arm_and_floor_is_adverse_first():
    # One bar both arms (high 130 ≥ 101) and breaches the floor (low 94).
    # Adverse-first → floor fires.
    bars = _series(_utc(2026, 2, 7, 0, 45), [
        (100.0, 130.0, 94.0, 96.0),
    ])
    out = simulate_intraday_trade(100.0, bars)
    assert out.exit_reason == "hard_floor"
    assert out.up1_before_dn5 is False


# ── simulate: up1_before_dn5 path metric ────────────────────────────────


def test_up1_before_dn5_true_when_plus1_reached_first():
    bars = _series(_utc(2026, 2, 7, 0, 45), [
        (100.0, 101.5, 99.8, 101.0),   # +1% (101) reached, no floor breach
        (101.0, 101.2, 100.5, 100.8),
        (100.8, 100.9, 100.4, 100.6),
    ])
    out = simulate_intraday_trade(100.0, bars)
    assert out.up1_before_dn5 is True


def test_up1_before_dn5_false_when_dn5_reached_first():
    bars = _series(_utc(2026, 2, 7, 0, 45), [
        (100.0, 100.5, 94.0, 96.0),    # −5% first
    ])
    out = simulate_intraday_trade(100.0, bars)
    assert out.up1_before_dn5 is False


# ── net-return fee accounting ───────────────────────────────────────────


def test_compute_net_return_subtracts_total_costs():
    costs = TradeCosts(entry_fee=0.0002, exit_fee=0.0005,
                       entry_slippage=0.0005, exit_slippage=0.0005,
                       funding=0.001)
    # gross +10%, total costs = 0.0002+0.0005+0.0005+0.0005+0.001 = 0.0027
    net = compute_net_return(0.10, costs)
    assert net == pytest.approx(0.10 - 0.0027)
