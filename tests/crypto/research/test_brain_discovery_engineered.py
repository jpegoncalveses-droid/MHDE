"""Component 1 (§3) — coin-relative engineered primitive layer.

The load-bearing property is LOOKAHEAD-FREEDOM (§13a): the per-coin z-score for a
window uses ONLY that coin's strictly-prior windows, and the cross-universe rank is
cross-sectional within a single window. The decisive tests append a FUTURE window and
assert no earlier engineered value moves.
"""
from __future__ import annotations

import math

import pytest

from crypto.research.brain.discovery.engineered import (
    RAW, XRANK, Z, BaseFeature, compute_engineered, engineered_feature_ids, _safe_ratio,
)

TOTAL_VOL = BaseFeature(
    "trades.total_vol", "trades",
    lambda s: s["taker_buy_vol"] + s["taker_sell_vol"], (Z, XRANK))
BUY_RATIO = BaseFeature(
    "trades.taker_buy_ratio", "trades",
    lambda s: _safe_ratio(s["taker_buy_vol"], s["taker_buy_vol"] + s["taker_sell_vol"]),
    (RAW, Z, XRANK))
FEATS = [TOTAL_VOL, BUY_RATIO]

_W = 60_000_000_000  # one window (ns)


def _snap(sym, i, buy, sell):
    return {"symbol": sym, "window_start_ns": i * _W,
            "taker_buy_vol": float(buy), "taker_sell_vol": float(sell)}


def _series(sym, totals):
    # split each total into buy=0.5*total, sell=0.5*total (ratio constant 0.5)
    return [_snap(sym, i, t / 2.0, t / 2.0) for i, t in enumerate(totals)]


# -- z-score: lookahead-free, min-history, std==0 -----------------------------

def test_zscore_uses_only_strictly_prior_windows():
    totals = [10, 12, 11, 13, 9, 50]
    rows = _series("BTCUSDT", totals)
    eng = compute_engineered({"trades": rows}, zscore_windows=(3,),
                             zscore_min_history=2, xuniv_min_coins=1, base_features=FEATS)
    # w3: prior window [10,12,11] -> mean 11, pstd sqrt(2/3); z=(13-11)/pstd
    pstd = math.sqrt(((10 - 11) ** 2 + (12 - 11) ** 2 + (11 - 11) ** 2) / 3)
    assert eng[("BTCUSDT", 3 * _W)]["trades.total_vol.z3"] == pytest.approx((13 - 11) / pstd)


def test_zscore_is_lookahead_free_future_window_does_not_move_past_z():
    base = [10, 12, 11, 13, 9]
    full = base + [50]                       # one extra FUTURE window
    a = compute_engineered({"trades": _series("BTCUSDT", base)}, zscore_windows=(3,),
                           zscore_min_history=2, xuniv_min_coins=1, base_features=FEATS)
    b = compute_engineered({"trades": _series("BTCUSDT", full)}, zscore_windows=(3,),
                           zscore_min_history=2, xuniv_min_coins=1, base_features=FEATS)
    # every window present in BOTH must have identical z (the future point is invisible)
    for i in range(len(base)):
        k = ("BTCUSDT", i * _W)
        assert a.get(k, {}).get("trades.total_vol.z3") == b.get(k, {}).get("trades.total_vol.z3")
    # and the future window's z exists ONLY in the longer run
    assert ("BTCUSDT", 5 * _W) not in a
    assert "trades.total_vol.z3" in b[("BTCUSDT", 5 * _W)]


def test_zscore_absent_before_min_history_and_when_std_zero():
    eng = compute_engineered({"trades": _series("BTCUSDT", [10, 10, 10, 10])},
                             zscore_windows=(3,), zscore_min_history=2,
                             xuniv_min_coins=1, base_features=FEATS)
    # w0,w1 below min_history; w2,w3 have zero-variance prior -> no z anywhere
    for i in range(4):
        assert "trades.total_vol.z3" not in eng.get(("BTCUSDT", i * _W), {})


# -- cross-universe rank: cross-sectional, bounded ----------------------------

def test_xrank_is_cross_sectional_and_bounded():
    rows = []
    for sym, total in zip(["A", "B", "C", "D", "E"], [1, 2, 3, 4, 5]):
        rows += _series(sym, [total])        # each coin one window at i=0
    eng = compute_engineered({"trades": rows}, zscore_windows=(3,), zscore_min_history=2,
                             xuniv_min_coins=5, base_features=FEATS)
    ranks = {sym: eng[(sym, 0)]["trades.total_vol.xrank"] for sym in ["A", "B", "C", "D", "E"]}
    assert ranks == pytest.approx({"A": 0.1, "B": 0.3, "C": 0.5, "D": 0.7, "E": 0.9})
    assert all(0.0 <= v <= 1.0 for v in ranks.values())


def test_xrank_absent_below_min_coins():
    rows = _series("A", [1]) + _series("B", [2])
    eng = compute_engineered({"trades": rows}, zscore_windows=(3,), zscore_min_history=2,
                             xuniv_min_coins=5, base_features=FEATS)
    assert "trades.total_vol.xrank" not in eng[("A", 0)]


def test_xrank_one_window_unaffected_by_another_window():
    rows = []
    for sym, t0, t1 in [("A", 1, 99), ("B", 2, 1), ("C", 3, 2), ("D", 4, 3), ("E", 5, 4)]:
        rows += _series(sym, [t0, t1])
    eng = compute_engineered({"trades": rows}, zscore_windows=(3,), zscore_min_history=2,
                             xuniv_min_coins=5, base_features=FEATS)
    # window 0 ranks reflect ONLY window-0 cross-section [1,2,3,4,5]
    assert eng[("A", 0)]["trades.total_vol.xrank"] == pytest.approx(0.1)
    assert eng[("E", 0)]["trades.total_vol.xrank"] == pytest.approx(0.9)


# -- raw passthrough gating + determinism -------------------------------------

def test_raw_only_for_bounded_features():
    rows = _series("A", [10, 12]) + _series("B", [20, 22])
    eng = compute_engineered({"trades": rows}, zscore_windows=(3,), zscore_min_history=2,
                             xuniv_min_coins=2, base_features=FEATS)
    feats0 = eng[("A", 0)]
    assert "trades.taker_buy_ratio.raw" in feats0          # bounded ratio -> raw offered
    assert feats0["trades.taker_buy_ratio.raw"] == pytest.approx(0.5)
    assert "trades.total_vol.raw" not in feats0            # unbounded -> raw excluded


def test_engineered_feature_ids_enumeration():
    ids = set(engineered_feature_ids(FEATS, zscore_windows=(3,)))
    assert ids == {
        "trades.total_vol.z3", "trades.total_vol.xrank",
        "trades.taker_buy_ratio.raw", "trades.taker_buy_ratio.z3", "trades.taker_buy_ratio.xrank",
    }


def test_deterministic():
    rows = _series("A", [10, 12, 11, 13]) + _series("B", [5, 6, 7, 8])
    kw = dict(zscore_windows=(3,), zscore_min_history=2, xuniv_min_coins=2, base_features=FEATS)
    assert compute_engineered({"trades": rows}, **kw) == compute_engineered({"trades": rows}, **kw)
