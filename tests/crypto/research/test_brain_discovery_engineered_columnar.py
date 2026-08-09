"""Columnar engineered (option B) — the vectorized primitive-read → compute_engineered path,
proven equivalent to the scalar per-row implementation.

Layered TDD: (1) each of the 13 base-feature extracts, vectorized over an arrow table, equals
the scalar ``bf.extract`` per row (incl. None/null, zero-denominator); (2) the rolling per-coin
z-score matches ``statistics.pstdev``/``fmean`` and stays lookahead-free; (3) the cross-universe
rank matches the scalar mid-rank; (4) the whole ``compute_engineered_columnar`` equals the scalar
``compute_engineered`` on a fixed substrate (the load-bearing oracle test lives here + on real data).
"""
from __future__ import annotations

import math
import random

import numpy as np
import pyarrow as pa

from crypto.research.brain import store
from crypto.research.brain.discovery import engineered as E

_DATASET_SCHEMAS = {
    "trades": store.TRADES_SNAPSHOT_SCHEMA,
    "bookticker": store.BOOKTICKER_SNAPSHOT_SCHEMA,
    "markprice": store.MARKPRICE_SNAPSHOT_SCHEMA,
    "depth": store.DEPTH_SNAPSHOT_SCHEMA,
    "forceorder": store.FORCEORDER_SNAPSHOT_SCHEMA,
}


def _table(fields: dict) -> pa.Table:
    return pa.table({k: pa.array(v, type=pa.float64() if not k.endswith("count") else pa.int64())
                     for k, v in fields.items()})


def _scalar_base(bf, table):
    return [bf.extract(s) for s in table.to_pylist()]


def _eq_nan(vec, scalar):
    assert len(vec) == len(scalar)
    for a, b in zip(list(vec), scalar):
        if b is None:
            assert a is None or (isinstance(a, float) and math.isnan(a)), f"{a!r} != None"
        else:
            assert a == b or math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12), f"{a!r} != {b!r}"


# -- (1) vectorized extracts vs scalar, per base feature ----------------------

def test_vectorized_extracts_match_scalar_per_row():
    N = None  # readability alias for a null cell
    by_ds = {
        "trades": _table({
            "taker_buy_vol": [10.0, N, 0.0, 5.0], "taker_sell_vol": [5.0, 3.0, 0.0, N],
            "taker_buy_quote_vol": [100.0, N, 0.0, 50.0], "taker_sell_quote_vol": [50.0, 30.0, 0.0, N],
            "trade_count": [7, 3, 0, 9], "price_open": [100.0, 0.0, N, 100.0],
            "price_high": [101.0, 101.0, 101.0, N], "price_low": [99.0, 99.0, N, 99.0],
            "price_close": [100.5, 101.0, 100.0, 100.0]}),
        "bookticker": _table({
            "spread_mean": [0.5, N, 0.5, 0.0], "bid_close": [100.0, 100.0, 0.0, 100.0],
            "ask_close": [100.2, 100.2, 0.0, 100.2], "bid_qty_mean": [10.0, N, 0.0, 5.0],
            "ask_qty_mean": [5.0, 5.0, 0.0, N]}),
        "markprice": _table({
            "funding_last": [0.0001, N, 0.0, -0.0002], "mark_open": [100.0, 0.0, N, 100.0],
            "mark_close": [100.5, 101.0, 100.0, N]}),
        "depth": _table({
            "bid_total_notional_mean": [1000.0, N, 0.0, 500.0],
            "ask_total_notional_mean": [500.0, 500.0, 0.0, N]}),
        "forceorder": _table({
            "liq_buy_vol": [2.0, N, 0.0, 1.0], "liq_sell_vol": [1.0, 1.0, 0.0, N],
            "liq_buy_quote_vol": [20.0, N, 0.0, 10.0], "liq_sell_quote_vol": [10.0, 10.0, 0.0, N]}),
    }
    for bf in E.BASE_FEATURES:
        table = by_ds[bf.dataset]
        vec = E._columnar_base_values(bf, table)
        _eq_nan(vec, _scalar_base(bf, table))


# -- (2) vectorized per-coin rolling z-score vs scalar pstdev/fmean -----------

def _scalar_z(symbols, windows, vals, W, m):
    import statistics
    by_symbol: dict = {}
    for sym, w, v in zip(symbols, windows, vals):
        if not (isinstance(v, float) and math.isnan(v)):
            by_symbol.setdefault(sym, []).append((int(w), float(v)))
    out: dict = {}
    for sym, series in by_symbol.items():
        series.sort(key=lambda wv: wv[0])
        vs = [v for _, v in series]
        for i, (w, v) in enumerate(series):
            prior = vs[:i][-W:]
            if len(prior) < m:
                continue
            sd = statistics.pstdev(prior)
            if sd == 0:
                continue
            out[(sym, w)] = (v - statistics.fmean(prior)) / sd
    return out


def _z_array_to_map(symbols, windows, z):
    return {(s, int(w)): float(zz) for s, w, zz in zip(symbols, windows, z)
            if not math.isnan(zz)}


def test_columnar_zscore_matches_scalar_pstdev_fmean():
    rng = random.Random(3)
    symbols, windows, vals = [], [], []
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        for i in range(80):
            symbols.append(sym)
            windows.append(i)
            # sprinkle absent (NaN) base values -> excluded from the series, like the scalar
            vals.append(float("nan") if rng.random() < 0.1 else rng.gauss(10, 3))
    symbols = np.array(symbols, dtype=object)
    windows = np.array(windows, dtype=np.int64)
    vals = np.array(vals, dtype=np.float64)
    z = E._columnar_zscore(symbols, windows, vals, window=20, min_history=5)
    got = _z_array_to_map(symbols, windows, z)
    want = _scalar_z(symbols, windows, vals, 20, 5)
    assert set(got) == set(want), "z-score key set (which windows get a z) must match"
    for k in want:
        assert math.isclose(got[k], want[k], rel_tol=1e-9, abs_tol=1e-9), f"{k}: {got[k]} != {want[k]}"


def test_columnar_zscore_is_lookahead_free():
    # appending a FUTURE window must not move any earlier window's z (§13a).
    symbols = np.array(["BTCUSDT"] * 40, dtype=object)
    windows = np.arange(40, dtype=np.int64)
    vals = np.array([10.0 + math.sin(i) for i in range(40)], dtype=np.float64)
    z_short = E._columnar_zscore(symbols[:30], windows[:30], vals[:30], window=15, min_history=5)
    z_full = E._columnar_zscore(symbols, windows, vals, window=15, min_history=5)
    for i in range(30):
        a, b = z_short[i], z_full[i]
        assert (math.isnan(a) and math.isnan(b)) or math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)


# -- (3) vectorized cross-universe mid-rank vs scalar -------------------------

def test_columnar_xrank_matches_scalar_mid_rank():
    rng = random.Random(9)
    symbols, windows, vals = [], [], []
    for w in range(15):
        n_coins = rng.choice([3, 6, 6, 8])           # some windows below min_coins=5
        base = rng.choice([1.0, 2.0])                 # force ties across coins
        for c in range(n_coins):
            symbols.append(f"C{c}")
            windows.append(w)
            vals.append(float(rng.choice([base, base, 1.0, 2.0, 3.0])))
    symbols = np.array(symbols, dtype=object)
    windows = np.array(windows, dtype=np.int64)
    vals = np.array(vals, dtype=np.float64)

    got = E._columnar_xrank(symbols, windows, vals, min_coins=5)

    # scalar reference, per window
    by_w: dict = {}
    for s, w, v in zip(symbols, windows, vals):
        by_w.setdefault(int(w), []).append((s, float(v)))
    want: dict = {}
    for w, members in by_w.items():
        if len(members) < 5:
            continue
        pop = [v for _, v in members]
        for s, v in members:
            want[(s, w)] = E._mid_rank_percentile(v, pop)

    got_map = {(s, int(w)): float(g) for s, w, g in zip(symbols, windows, got) if not math.isnan(g)}
    assert set(got_map) == set(want), "xrank key set (windows meeting min_coins) must match"
    for k in want:
        assert math.isclose(got_map[k], want[k], rel_tol=1e-12, abs_tol=1e-12), f"{k}"


# -- (4) THE load-bearing oracle test: columnar compute_engineered == scalar ---

_W = 60_000_000_000


def _substrate(seed=1):
    """Multi-dataset raw dicts exercising RAW + z (multi-window/coin) + xrank (multi-coin/window),
    with sprinkled nulls, sub-min-history early windows, and a sub-min-coins window."""
    rng = random.Random(seed)
    coins = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT"]
    raw: dict = {"trades": [], "bookticker": [], "markprice": [], "depth": [], "forceorder": []}
    for i in range(40):
        n = 2 if i == 7 else len(coins)             # window 7 is below xuniv_min_coins
        for c in coins[:n]:
            base = {"symbol": c, "window_start_ns": i * _W, "window_end_ns": (i + 1) * _W,
                    "recv_ts_ns": (i + 1) * _W}
            drop = rng.random() < 0.08               # sprinkle a null cell
            raw["trades"].append({**base, "taker_buy_vol": None if drop else rng.uniform(1, 100),
                                  "taker_sell_vol": rng.uniform(1, 100),
                                  "taker_buy_quote_vol": rng.uniform(10, 1000),
                                  "taker_sell_quote_vol": rng.uniform(10, 1000),
                                  "trade_count": rng.randint(1, 500),
                                  "price_open": 100.0 + rng.uniform(-1, 1), "price_high": 101.0,
                                  "price_low": 99.0, "price_close": 100.0 + rng.uniform(-1, 1)})
            raw["bookticker"].append({**base, "spread_mean": rng.uniform(0.01, 0.5),
                                      "bid_close": 100.0, "ask_close": 100.2,
                                      "bid_qty_mean": rng.uniform(1, 50), "ask_qty_mean": rng.uniform(1, 50)})
            raw["markprice"].append({**base, "funding_last": rng.uniform(-0.001, 0.001),
                                     "mark_open": 100.0, "mark_close": 100.0 + rng.uniform(-2, 2)})
            raw["depth"].append({**base, "bid_total_notional_mean": rng.uniform(1e3, 1e6),
                                 "ask_total_notional_mean": rng.uniform(1e3, 1e6)})
            raw["forceorder"].append({**base, "liq_buy_vol": rng.uniform(0, 10),
                                      "liq_sell_vol": rng.uniform(0, 10),
                                      "liq_buy_quote_vol": rng.uniform(0, 100),
                                      "liq_sell_quote_vol": rng.uniform(0, 100)})
    return raw


def _tables(raw):
    return {ds: pa.Table.from_pylist(rows) for ds, rows in raw.items() if rows}


def _tape_to_map(tape):
    return {k: dict(tape[k]) for k in tape}


def test_compute_engineered_columnar_equals_scalar_oracle():
    raw = _substrate(seed=1)
    kw = dict(zscore_windows=(10,), zscore_min_history=5, xuniv_min_coins=5)
    scalar = E.compute_engineered(raw, **kw)
    tables = _tables(raw)
    columnar = E.compute_engineered_columnar(lambda ds, cols=None: tables.get(ds, pa.table({})), **kw)

    ms, mc = _tape_to_map(scalar), _tape_to_map(columnar)
    assert set(ms) == set(mc), "key set (instances) must match the scalar oracle exactly"
    max_delta = 0.0
    for key in ms:
        fs, fc = ms[key], mc[key]
        assert set(fs) == set(fc), f"feature set for {key} must match ({set(fs) ^ set(fc)})"
        for fid in fs:
            d = abs(fs[fid] - fc[fid])
            max_delta = max(max_delta, d)
            assert math.isclose(fs[fid], fc[fid], rel_tol=1e-9, abs_tol=1e-12), \
                f"{key} {fid}: scalar {fs[fid]} != columnar {fc[fid]}"
    assert max_delta < 1e-9, f"max feature delta {max_delta:g}"


# -- (5) THE COMPOSED PRODUCTION SEAM: parquet -> read_snapshots_columnar ------
#        -> _read_ds (window_end_ns appended) -> compute_engineered_columnar.
# The oracle above feeds in-memory tables via a lambda, bypassing the real read layer.
# This exercises the whole runner seam end-to-end from parquet on disk and proves it
# byte-identical to the scalar read_snapshots -> compute_engineered path.

def test_composed_seam_columnar_equals_scalar_through_parquet(tmp_path):
    raw = _substrate(seed=3)
    root = str(tmp_path)
    for ds, rows in raw.items():
        store.write_snapshots(root, ds, _DATASET_SCHEMAS[ds], rows)

    kw = dict(zscore_windows=(10,), zscore_min_history=5, xuniv_min_coins=5)

    # scalar path: read_snapshots (list-of-dicts) -> compute_engineered
    scalar_raw = {ds: store.read_snapshots(root, ds, after_recv_ts_ns=0,
                                           window_end_floor_ns=0) for ds in raw}
    scalar = E.compute_engineered(scalar_raw, **kw)

    # columnar path: the PRODUCTION _read_ds closure (runner.py) — appends window_end_ns,
    # reads via read_snapshots_columnar — driving compute_engineered_columnar.
    def _read_ds(dataset, columns):
        cols = list(columns)
        if "window_end_ns" not in cols:
            cols.append("window_end_ns")
        return store.read_snapshots_columnar(root, dataset, after_recv_ts_ns=0,
                                             window_end_floor_ns=0, columns=cols)

    columnar = E.compute_engineered_columnar(_read_ds, **kw)

    ms, mc = _tape_to_map(scalar), _tape_to_map(columnar)
    assert ms, "substrate must produce a non-empty tape (guards a vacuous pass)"
    assert set(ms) == set(mc), "instance key set must match through the parquet seam"
    for key in ms:
        assert set(ms[key]) == set(mc[key]), f"feature set for {key} must match"
        for fid in ms[key]:
            assert math.isclose(ms[key][fid], mc[key][fid], rel_tol=1e-9, abs_tol=1e-12), \
                f"{key} {fid}: scalar {ms[key][fid]} != columnar {mc[key][fid]}"


# -- (6) CONCURRENT-WRITE consistency: a live tick write between PASS 1 and PASS 2 --------
# The 2026-08-08 real-store gate crashed here: compute_engineered_columnar reads each dataset
# TWICE (PASS 1 = key universe, PASS 2 = scatter). The live tick writes new rows between the
# two reads, so PASS 2 returns a (symbol, window) absent from key_to_row -> KeyError. A static
# store reads identically across both passes, so the in-memory/parquet oracle tests CANNOT
# reproduce it. This drives a read that returns an EXTRA row only on the second read.

def test_columnar_tolerates_concurrent_write_between_passes():
    raw = _substrate(seed=5)
    tables = _tables(raw)
    kw = dict(zscore_windows=(10,), zscore_min_history=5, xuniv_min_coins=5)

    # the correct answer: a consistent (no concurrent write) read.
    clean = E.compute_engineered_columnar(
        lambda ds, cols=None: tables.get(ds, pa.table({})), **kw)

    # A NEW coin landing on an EXISTING window (window 20 already has 6 coins) — the strong
    # case: if the fix only masked the extra row at scatter-time, this 7th coin would still
    # enter the cross-universe rank population for window 20 and CORRUPT the 6 processed
    # coins' xrank. Correct behaviour drops it BEFORE the vectorized compute.
    base_mp = tables["markprice"].slice(0, 1).to_pylist()[0]
    extra = dict(base_mp); extra["symbol"] = "ZZZZUSDT"; extra["window_start_ns"] = 20 * _W
    mp_with_extra = pa.Table.from_pylist(tables["markprice"].to_pylist() + [extra])

    seen: dict = {}

    def racy_read(ds, cols=None):
        seen[ds] = seen.get(ds, 0) + 1
        if ds == "markprice" and seen[ds] >= 2:      # the write lands before PASS 2's read
            return mp_with_extra
        return tables.get(ds, pa.table({}))

    racy = E.compute_engineered_columnar(racy_read, **kw)   # must NOT raise KeyError

    mc, mr = _tape_to_map(clean), _tape_to_map(racy)
    assert ("ZZZZUSDT", 20 * _W) not in mr, "the concurrent-write row must be excluded (forward-only)"
    assert set(mr) == set(mc), "processed key set must equal the clean run"
    for key in mc:
        assert mr[key] == mc[key], f"processed row {key} corrupted by the concurrent write"

