"""Columnar engineered layer (§ memory floor) — ``compute_engineered`` returns an
``EngineeredTape`` backed by a dense float64 matrix (NaN = absent), NOT a dict-of-dicts.

The Mapping CONTRACT (``eng[key][fid]``, ``.get``, ``in``, ``==``, row iteration) is pinned
by the existing test_brain_discovery_engineered suite, which must stay green unchanged — the
tape is a drop-in Mapping. Here we pin the NEW guarantees: columnar backing, exact key-set
parity with the old sparse dict (all-NaN keys excluded), and that the columnar fast path the
scorer uses is identical to reading the tape as a generic Mapping and to a plain-dict feed.
"""
from __future__ import annotations

import random

import numpy as np

from crypto.research.brain.discovery import scoring
from crypto.research.brain.discovery.engineered import (
    RAW, XRANK, Z, BaseFeature, EngineeredTape, compute_engineered, engineered_feature_ids,
    _safe_ratio,
)

_W = 60_000_000_000

TOTAL_VOL = BaseFeature("trades.total_vol", "trades",
                        lambda s: s["taker_buy_vol"] + s["taker_sell_vol"], (Z, XRANK))
BUY_RATIO = BaseFeature("trades.taker_buy_ratio", "trades",
                        lambda s: _safe_ratio(s["taker_buy_vol"], s["taker_buy_vol"] + s["taker_sell_vol"]),
                        (RAW, Z, XRANK))
FEATS = [TOTAL_VOL, BUY_RATIO]


def _snap(sym, i, buy, sell):
    return {"symbol": sym, "window_start_ns": i * _W,
            "taker_buy_vol": float(buy), "taker_sell_vol": float(sell)}


def _series(sym, totals):
    return [_snap(sym, i, t / 2.0, t / 2.0) for i, t in enumerate(totals)]


# -- columnar backing ---------------------------------------------------------

def test_compute_engineered_returns_columnar_tape():
    rows = _series("A", [10, 12, 11, 13]) + _series("B", [5, 6, 7, 8])
    eng = compute_engineered({"trades": rows}, zscore_windows=(3,), zscore_min_history=2,
                             xuniv_min_coins=2, base_features=FEATS)
    assert isinstance(eng, EngineeredTape)
    n_feats = len(engineered_feature_ids(FEATS, zscore_windows=(3,)))
    assert isinstance(eng._values, np.ndarray) and eng._values.dtype == np.float64
    assert eng._values.shape == (len(eng), n_feats)              # dense (n_keys x n_feats)


def test_tape_excludes_keys_with_no_computable_feature():
    # one coin, 2 windows, min_history 5 (never met) + min_coins 5 (never met), no RAW feature
    # -> NO engineered value anywhere -> the old sparse dict had zero keys; the tape must too.
    rows = _series("A", [10, 20])
    eng = compute_engineered({"trades": rows}, zscore_windows=(3,), zscore_min_history=5,
                             xuniv_min_coins=5, base_features=[TOTAL_VOL])
    assert len(eng) == 0
    assert ("A", 0) not in eng and ("A", 1 * _W) not in eng


def test_tape_getitem_raises_keyerror_for_absent_key():
    eng = compute_engineered({"trades": _series("A", [10, 12, 11, 13])}, zscore_windows=(3,),
                             zscore_min_history=2, xuniv_min_coins=1, base_features=FEATS)
    import pytest
    with pytest.raises(KeyError):
        _ = eng[("NOPE", 0)]


# -- the scorer's columnar fast path == generic Mapping read == plain-dict feed ------

def _random_raw(n_windows, syms, seed):
    rng = random.Random(seed)
    rows = []
    for i in range(n_windows):
        for sym in syms:
            b, s = rng.uniform(1, 100), rng.uniform(1, 100)
            rows.append(_snap(sym, i, b, s))
    return rows


def test_tape_labeled_columns_matches_generic_mapping_read():
    rows = _random_raw(40, [f"S{j}" for j in range(6)], seed=2)
    eng = compute_engineered({"trades": rows}, zscore_windows=(3,), zscore_min_history=2,
                             xuniv_min_coins=3, base_features=FEATS)
    fids = engineered_feature_ids(FEATS, zscore_windows=(3,))
    keys = sorted(eng.keys())
    fast = eng.labeled_columns(fids, keys)                       # columnar fast path
    # generic path: read the tape purely through the Mapping interface
    plain = {k: dict(eng[k]) for k in keys}
    slow = scoring._labeled_feature_columns(plain, fids, keys)
    for fid in fids:
        a, b = fast[fid], slow[fid]
        assert np.array_equal(a, b, equal_nan=True), f"column {fid} mismatch"


def test_tape_build_atoms_equals_generic_mapping_build_atoms():
    # The columnar fast path in build_atoms must produce the IDENTICAL atom set (same thresholds)
    # as the generic dict-of-dicts scan -- the atoms are the search's alphabet; drift = drift.
    from crypto.research.brain.discovery.rules import build_atoms
    rows = _random_raw(50, [f"S{j}" for j in range(6)], seed=4)
    eng = compute_engineered({"trades": rows}, zscore_windows=(3,), zscore_min_history=2,
                             xuniv_min_coins=3, base_features=FEATS)
    fids = engineered_feature_ids(FEATS, zscore_windows=(3,))
    plain = {k: dict(eng[k]) for k in eng}
    a_tape = build_atoms(eng, fids, 10)                  # columnar fast path
    a_dict = build_atoms(plain, fids, 10)                # generic scan
    key = lambda atoms: sorted((c.feature, c.op, c.threshold) for c in atoms)
    assert key(a_tape) == key(a_dict)


def test_tape_fires_keys_matches_generic_fires():
    from crypto.research.brain.discovery.rules import Condition, fires, make_rule
    rows = _random_raw(40, [f"S{j}" for j in range(5)], seed=6)
    eng = compute_engineered({"trades": rows}, zscore_windows=(3,), zscore_min_history=2,
                             xuniv_min_coins=3, base_features=FEATS)
    plain = {k: dict(eng[k]) for k in eng}
    r1 = make_rule([Condition("trades.taker_buy_ratio.raw", ">", 0.5)])
    r2 = make_rule([Condition("trades.taker_buy_ratio.raw", ">", 0.4),
                    Condition("trades.taker_buy_ratio.xrank", "<", 0.6)])
    r_absent = make_rule([Condition("nonexistent.feature", ">", 0.0)])
    for rule in (r1, r2, r_absent):
        assert fires(rule, eng) == fires(rule, plain), f"fires mismatch for {rule.canonical_id}"


def test_run_discovery_pass_over_a_real_tape_promotes_and_logs(tmp_path):
    """The GATE path: a tape from compute_engineered flows through the whole pass — Stage-1
    search, forward confirmation (R.fires over the tape), continuations (a _RowView as ``fv``),
    exit discovery (primitive_cond.holds on the _RowView) and trade logging — end to end."""
    from crypto.research.brain.discovery import rulestore as RS
    from crypto.research.brain.discovery import runner
    from crypto.research.brain.discovery import tradelog as TL
    from crypto.research.brain.discovery import exits as X
    from crypto.research.brain.discovery.rules import Condition, make_rule
    from crypto.research.brain.discovery.scoring import EntryResult

    rng = random.Random(1)
    syms = [f"S{j}" for j in range(7)]
    raw, lifts, price_index = [], {}, {}
    for i in range(60):
        sym, w = syms[i % 7], (i + 1) * _W
        high = (i % 3 != 0)                              # ~2/3 fire the planted ratio>0.5 rule
        buy, sell = (9.0, 1.0) if high else (1.0, 9.0)  # taker_buy_ratio.raw = 0.9 / 0.1
        raw.append(_snap(sym, i + 1, buy, sell))
        lifts[(sym, w)] = 0.02 if high else -0.02
    for sym in syms:
        wmap, c = {}, 100.0
        for wi in range(0, 75):
            c *= 1.0 + 0.006 + rng.uniform(-0.004, 0.008)
            wmap[wi * _W] = (c, c * 1.001, c * 0.999)
        price_index[sym] = wmap

    eng = compute_engineered({"trades": raw}, zscore_windows=(3,), zscore_min_history=2,
                             xuniv_min_coins=2, base_features=FEATS)
    assert isinstance(eng, EngineeredTape)
    coin_vols = runner.coin_volatilities(price_index)
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    TL.ensure_schema(conn)
    try:
        planted = make_rule([Condition("trades.taker_buy_ratio.raw", ">", 0.5)])
        res = EntryResult(rule=planted, edge=0.02, n_fires=40, depth=1, null_bar=0.005, margin=0.015)
        rid = RS.upsert_entry(conn, res, score_horizon_min=60, breadth=7,
                              discovery_window_ns=0, now_ns=1)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=1)

        summary = runner.run_discovery_pass(
            conn, eng, lifts, price_index, coin_vols,
            feature_ids=engineered_feature_ids(FEATS, zscore_windows=(3,)),
            frontier_ns=100 * _W, now_ns=10, n_bins=5, n_permutations=40, null_quantile=0.9,
            min_firing=20, max_depth=1, m=30, z=2.0,
            exit_grid=X.build_exit_grid((1.0,), (1.0,), (5,)), seed=1)

        assert summary["diagnostics"][0]["n_candidates"] > 0     # Stage-1 ran over the tape
        row = RS.get_rule(conn, rid)
        assert row["state"] == RS.PROMOTED                       # confirmation via R.fires(tape)
        assert row["exit_def"] is not None                       # exit discovery via _RowView fv
        assert summary["trades_logged"] > 0                      # tradelog over the tape
    finally:
        conn.close()


def test_tape_fed_discover_entries_equals_dict_fed():
    rng = random.Random(11)
    syms = [f"S{j}" for j in range(8)]
    rows = _random_raw(60, syms, seed=11)
    eng = compute_engineered({"trades": rows}, zscore_windows=(3,), zscore_min_history=2,
                             xuniv_min_coins=4, base_features=FEATS)
    fids = engineered_feature_ids(FEATS, zscore_windows=(3,))
    keys = sorted(eng.keys())
    lifts = {k: rng.gauss(0, 1) for k in keys}
    plain = {k: dict(eng[k]) for k in keys}
    kw = dict(feature_ids=fids, n_bins=5, n_permutations=50, null_quantile=0.95,
              min_firing=20, max_depth=2, seed=11)
    st, _ = scoring.discover_entries(eng, lifts, **kw)           # tape (fast path)
    sd, _ = scoring.discover_entries(plain, lifts, **kw)         # dict (generic path)
    assert sorted((s.rule.canonical_id, s.n_fires) for s in st) == \
           sorted((s.rule.canonical_id, s.n_fires) for s in sd)
