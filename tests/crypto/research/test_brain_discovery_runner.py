"""Component 8 (§9) — discovery batch runner: orchestration + loaders.

An end-to-end pass over already-loaded synthetic data proves the wiring: Stage 1 discovers
and records the funnel, forward confirmation promotes a holding edge, Stage 2 attaches an
exit, and the trade log fills — all against the discovery DB. (The systemd unit itself has
no unit test; its file is validated by test_brain_discovery_systemd.py.)
"""
from __future__ import annotations

import random

import pytest

from crypto.research.brain.discovery import rulestore as RS
from crypto.research.brain.discovery import runner
from crypto.research.brain.discovery import tradelog as TL
from crypto.research.brain.discovery import exits as X
from crypto.research.brain.discovery.rules import Condition, make_rule
from crypto.research.brain.discovery.scoring import EntryResult

_W = 60_000_000_000


def _synth(seed=1):
    rng = random.Random(seed)
    syms = [f"S{j}" for j in range(7)]
    eng, lifts = {}, {}
    for i in range(60):
        sym, w = syms[i % 7], (i + 1) * _W
        high = (i % 3 != 0)                              # ~2/3 fire the planted rule
        eng[(sym, w)] = {"sig.raw": 0.9 if high else 0.1, "noise.raw": rng.random()}
        lifts[(sym, w)] = 0.02 if high else -0.02
    price_index = {}
    for sym in syms:
        wmap, c = {}, 100.0
        for wi in range(0, 75):
            c *= 1.0 + 0.006 + rng.uniform(-0.004, 0.008)  # drift up, varying -> vol>0
            wmap[wi * _W] = (c, c * 1.001, c * 0.999)
        price_index[sym] = wmap
    return eng, lifts, price_index


# -- loaders ------------------------------------------------------------------

def test_build_price_index_and_coin_vol_and_continuation():
    rows = [{"symbol": "BTCUSDT", "window_start_ns": i * _W, "mark_close": 100.0 + i,
             "mark_high": 100.5 + i, "mark_low": 99.5 + i} for i in range(5)]
    idx = runner.build_price_index(rows)
    assert idx["BTCUSDT"][2 * _W] == (102.0, 102.5, 101.5)
    vols = runner.coin_volatilities(idx)
    assert vols["BTCUSDT"] is not None and vols["BTCUSDT"] > 0
    cont = runner.build_continuation("BTCUSDT", 0, idx, {}, max_cap=3, window_ns=_W)
    assert len(cont) == 3 and cont[0]["rel_close"] == pytest.approx(101.0 / 100.0)


def test_continuation_truncates_on_missing_forward_window():
    idx = {"X": {0: (100.0, 100.0, 100.0), 1 * _W: (101.0, 101.0, 101.0)}}   # only k=1 present
    cont = runner.build_continuation("X", 0, idx, {}, max_cap=5, window_ns=_W)
    assert len(cont) == 1                                # stops at the gap


# -- end-to-end pass ----------------------------------------------------------

def test_run_discovery_pass_promotes_and_logs(tmp_path):
    eng, lifts, price_index = _synth(seed=1)
    coin_vols = runner.coin_volatilities(price_index)
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    TL.ensure_schema(conn)
    try:
        # pre-seed the planted rule as confirming, discovered in the PAST (window 0) so all
        # 40 of its fires (windows 1..60) are fresh; frontier is current (well past them).
        planted = make_rule([Condition("sig.raw", ">", 0.5)])
        res = EntryResult(rule=planted, edge=0.02, n_fires=40, depth=1, null_bar=0.005, margin=0.015)
        rid = RS.upsert_entry(conn, res, score_horizon_min=60, breadth=7,
                              discovery_window_ns=0, now_ns=1)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=1)

        summary = runner.run_discovery_pass(
            conn, eng, lifts, price_index, coin_vols,
            feature_ids=["sig.raw", "noise.raw"], frontier_ns=100 * _W, now_ns=10,
            n_bins=5, n_permutations=40, null_quantile=0.9, min_firing=20, max_depth=1,
            m=30, z=2.0, exit_grid=X.build_exit_grid((1.0,), (1.0,), (5,)), seed=1)

        # Stage 1 generated+scored candidates and the run funnel was recorded
        assert summary["diagnostics"][0]["n_candidates"] > 0
        assert RS.list_runs(conn)[0]["n_survivors"] == summary["survivors"]
        # the pre-seeded edge confirmed forward -> promoted, got an exit, and logged trades
        row = RS.get_rule(conn, rid)
        assert row["state"] == RS.PROMOTED
        assert row["exit_def"] is not None
        assert summary["trades_logged"] > 0 and TL.list_trades(conn, rule_id=rid)
    finally:
        conn.close()
