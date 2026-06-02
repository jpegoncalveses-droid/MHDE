"""Unit tests for the signal-probe causal feature math.

All functions are pure; we feed hand-built bar series and assert exact
values, plus the ``None`` contract when there is not enough lookback.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from crypto.research.signal_probe import features as F


def _bar(open_time, o, h, l, c, v, trades=10, taker=5.0, qv=None):
    return {
        "open_time": open_time, "open": o, "high": h, "low": l, "close": c,
        "volume": v, "quote_volume": qv if qv is not None else c * v,
        "trades": trades, "taker_buy_base": taker,
    }


def _series(closes, *, vols=None, start=None, step_min=1, trades=None, takers=None):
    start = start or datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    bars = []
    for i, c in enumerate(closes):
        v = vols[i] if vols else 10.0
        tr = trades[i] if trades else 10
        tk = takers[i] if takers else v / 2.0
        bars.append(_bar(start + timedelta(minutes=i * step_min), c, c + 0.5, c - 0.5, c, v, tr, tk))
    return bars


# ── ROC / acceleration / move shape ───────────────────────────────────────

def test_roc_basic():
    closes = [100.0, 101.0, 102.0, 104.0]
    assert F.roc(closes, 1) == pytest.approx(104.0 / 102.0 - 1)
    assert F.roc(closes, 3) == pytest.approx(104.0 / 100.0 - 1)


def test_roc_insufficient_returns_none():
    assert F.roc([100.0, 101.0], 5) is None


def test_acceleration_is_delta_of_equal_windows():
    closes = [100.0, 102.0, 105.0]  # roc1 now=105/102-1, prev=102/100-1
    expected = (105.0 / 102.0 - 1) - (102.0 / 100.0 - 1)
    assert F.acceleration(closes, 1) == pytest.approx(expected)


def test_acceleration_insufficient_none():
    assert F.acceleration([100.0, 101.0], 1) is None  # needs 2n+1 = 3


def test_move_shape_single_spike_vs_total():
    # net move 100->110 = 10; biggest single step is 100->108 = 8.
    closes = [100.0, 108.0, 109.0, 110.0]
    assert F.move_shape(closes, 3) == pytest.approx(8.0 / 10.0)


def test_move_shape_zero_net_move_none():
    closes = [100.0, 105.0, 100.0]
    assert F.move_shape(closes, 2) is None


# ── VWAP / SMA ─────────────────────────────────────────────────────────────

def test_dist_sma():
    closes = [10.0, 20.0, 30.0]  # sma3 = 20, last = 30 -> +0.5
    assert F.dist_sma(closes, 3) == pytest.approx(0.5)


def test_dist_sma_insufficient_none():
    assert F.dist_sma([1.0, 2.0], 3) is None


def test_dist_rolling_vwap_constant_price_is_zero():
    bars = _series([100.0] * 60, vols=[5.0] * 60)
    assert F.dist_rolling_vwap(bars, 60) == pytest.approx(0.0)


def test_dist_session_vwap_filters_to_ts_date():
    ts = datetime(2026, 6, 2, 3, 0, tzinfo=timezone.utc)
    # two bars today (price 100, vols 10 each), one yesterday that must be ignored
    bars = [
        _bar(datetime(2026, 6, 1, 23, 0, tzinfo=timezone.utc), 50, 50, 50, 50, 10),
        _bar(datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc), 100, 100, 100, 100, 10),
        _bar(datetime(2026, 6, 2, 1, 0, tzinfo=timezone.utc), 100, 100, 100, 100, 10),
    ]
    # session vwap = 100 (typical=100), price 110 -> +0.10
    assert F.dist_session_vwap(bars, 110.0, ts) == pytest.approx(0.10)


# ── breakout ───────────────────────────────────────────────────────────────

def test_breakout_new_high_positive():
    # prior 2-bar highs max = 100.5; current close 110 -> > 0
    bars = _series([100.0, 100.0, 110.0])
    val = F.breakout(bars, 2)
    assert val == pytest.approx(110.0 / 100.5 - 1)


def test_breakout_insufficient_none():
    bars = _series([100.0])
    assert F.breakout(bars, 5) is None


# ── volume / rvol ──────────────────────────────────────────────────────────

def test_up_down_vol_ratio():
    # alternate up (close>open) and down bars; explicit opens
    bars = [
        _bar(datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc), 100, 101, 99, 102, 30),  # up
        _bar(datetime(2026, 6, 2, 12, 1, tzinfo=timezone.utc), 102, 103, 99, 100, 10),  # down
    ]
    assert F.up_down_vol_ratio(bars, 2) == pytest.approx(3.0)


def test_rvol_1m():
    vols = [10.0] * 20 + [30.0]  # mean prior 20 = 10, last = 30
    assert F.rvol_1m(vols, 20) == pytest.approx(3.0)


def test_rvol_5m():
    vols = [10.0] * 20 + [4.0, 4.0, 4.0, 4.0, 4.0]
    # last-5 sum = 20; 5 * mean(prior 20=10) = 50 -> 0.4
    assert F.rvol_5m(vols, 20) == pytest.approx(0.4)


def test_rvol_insufficient_none():
    assert F.rvol_1m([1.0, 2.0], 20) is None


# ── taker imbalance ────────────────────────────────────────────────────────

def test_taker_imbalance():
    bars = [
        _bar(datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc), 100, 101, 99, 100, 10, taker=7.0),
        _bar(datetime(2026, 6, 2, 12, 1, tzinfo=timezone.utc), 100, 101, 99, 100, 10, taker=3.0),
    ]
    assert F.taker_imbalance(bars, 2) == pytest.approx(10.0 / 20.0)
    assert F.taker_imbalance(bars, 1) == pytest.approx(3.0 / 10.0)


# ── trade count / size ─────────────────────────────────────────────────────

def test_trade_count_ratio():
    bars = _series([100.0] * 21, trades=[10] * 20 + [40])
    assert F.trade_count_ratio(bars, 20) == pytest.approx(4.0)


def test_avg_trade_size():
    bar = _bar(datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc), 100, 101, 99, 100, 50, trades=10)
    assert F.avg_trade_size(bar) == pytest.approx(5.0)


def test_avg_trade_size_zero_trades_none():
    bar = _bar(datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc), 100, 101, 99, 100, 50, trades=0)
    assert F.avg_trade_size(bar) is None


# ── OI change ──────────────────────────────────────────────────────────────

def test_oi_change():
    oi = [100.0, 110.0, 121.0]
    assert F.oi_change(oi, 1) == pytest.approx(121.0 / 110.0 - 1)
    assert F.oi_change(oi, 2) == pytest.approx(0.21)


def test_oi_change_insufficient_none():
    assert F.oi_change([100.0], 1) is None


# ── depth ──────────────────────────────────────────────────────────────────

def test_depth_imbalance_and_spread():
    depth = {
        "bids": [["100.0", "5"], ["99.8", "3"], ["98.0", "100"]],  # 98 is outside 0.5%
        "asks": [["100.2", "2"], ["100.4", "1"], ["110.0", "100"]],  # 110 outside
    }
    imb, spread = F.depth_imbalance_and_spread(depth)
    # mid = 100.1; band [99.5995, 100.6005]; bid depth 5+3=8, ask depth 2+1=3
    assert imb == pytest.approx(8.0 / 3.0)
    assert spread == pytest.approx((100.2 - 100.0) / 100.1 * 10_000)


def test_depth_empty_none():
    assert F.depth_imbalance_and_spread({"bids": [], "asks": []}) == (None, None)


# ── cross-sectional ────────────────────────────────────────────────────────

def test_apply_cross_sectional_vs_btc_and_universe():
    base = {
        "BTCUSDT": {"roc_5m": 0.01, "roc_15m": None, "roc_60m": 0.0},
        "AAAUSDT": {"roc_5m": 0.03, "roc_15m": 0.02, "roc_60m": 0.0},
        "BBBUSDT": {"roc_5m": -0.01, "roc_15m": 0.04, "roc_60m": 0.0},
    }
    F.apply_cross_sectional(base, "BTCUSDT")

    # vs BTC at 5m: AAA 0.03-0.01=0.02 ; BBB -0.01-0.01=-0.02
    assert base["AAAUSDT"]["ret_vs_btc_5m"] == pytest.approx(0.02)
    assert base["BBBUSDT"]["ret_vs_btc_5m"] == pytest.approx(-0.02)
    # median of {0.01,0.03,-0.01} = 0.01 ; spread for AAA = 0.02
    assert base["AAAUSDT"]["ret_spread_median_5m"] == pytest.approx(0.02)
    # percentile: AAA is highest of 3 -> 2/2 = 1.0 ; BBB lowest -> 0.0
    assert base["AAAUSDT"]["ret_pct_5m"] == pytest.approx(1.0)
    assert base["BBBUSDT"]["ret_pct_5m"] == pytest.approx(0.0)
    # BTC roc_15m is None -> its vs-self stays None, others vs None BTC -> None
    assert base["BTCUSDT"]["ret_vs_btc_15m"] is None
    assert base["AAAUSDT"]["ret_vs_btc_15m"] is None
