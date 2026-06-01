"""Tests for the per-probability-bin aggregation of replay results."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from crypto.execution.backtest.intraday_replay import (
    TradeResult,
    aggregate_bins,
    stats_for,
)


def _tr(prob, net, *, reason="time", up1=True, hold=600, traded=False, gross=None):
    t = datetime(2026, 2, 7, 0, 45, tzinfo=timezone.utc)
    return TradeResult(
        symbol="X", prediction_date=date(2026, 2, 6), probability=prob,
        entry_time=t, entry_price=100.0, exit_time=t, exit_price=100.0,
        exit_reason=reason, hold_minutes=hold, peak_price=100.0,
        up1_before_dn5=up1, gross_return=(gross if gross is not None else net),
        net_return=net, volume_rank=1, traded=traded,
    )


def test_stats_basic_counts_and_winrate():
    res = [_tr(0.5, 0.10), _tr(0.5, -0.05), _tr(0.5, 0.20)]
    s = stats_for(res)
    assert s["n"] == 3
    assert s["win_rate"] == pytest.approx(2 / 3)
    assert s["avg_pnl"] == pytest.approx((0.10 - 0.05 + 0.20) / 3)
    assert s["median_pnl"] == pytest.approx(0.10)


def test_stats_profit_factor():
    res = [_tr(0.5, 0.30), _tr(0.5, -0.10), _tr(0.5, -0.05)]
    s = stats_for(res)
    # gains 0.30 / losses 0.15 = 2.0
    assert s["profit_factor"] == pytest.approx(2.0)


def test_stats_profit_factor_no_losses_is_none():
    res = [_tr(0.5, 0.30), _tr(0.5, 0.10)]
    s = stats_for(res)
    assert s["profit_factor"] is None  # undefined (no losing trades)


def test_stats_p_up1_before_dn5_and_hold_hours():
    res = [_tr(0.5, 0.1, up1=True, hold=600),
           _tr(0.5, 0.1, up1=False, hold=1200)]
    s = stats_for(res)
    assert s["p_up1_before_dn5"] == pytest.approx(0.5)
    assert s["avg_hold_hours"] == pytest.approx((600 + 1200) / 2 / 60)


def test_stats_exit_reason_mix():
    res = [_tr(0.5, 0.1, reason="time"), _tr(0.5, -0.1, reason="hard_floor"),
           _tr(0.5, 0.2, reason="time")]
    s = stats_for(res)
    assert s["exit_reason_mix"] == {"time": 2, "hard_floor": 1}


def test_aggregate_bins_groups_by_floor_prob_tenth():
    res = [_tr(0.52, 0.1), _tr(0.58, 0.2), _tr(0.71, -0.1), _tr(0.04, 0.0)]
    bins = aggregate_bins(res)
    by_bin = {b["bin"]: b for b in bins}
    assert set(by_bin) == {0.0, 0.5, 0.7}
    assert by_bin[0.5]["n"] == 2
    assert by_bin[0.7]["n"] == 1
    assert by_bin[0.0]["n"] == 1
    # bins are returned sorted ascending
    assert [b["bin"] for b in bins] == [0.0, 0.5, 0.7]


def test_stats_empty_is_safe():
    s = stats_for([])
    assert s["n"] == 0
    assert s["win_rate"] is None
    assert s["profit_factor"] is None
