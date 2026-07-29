"""Discovery memory fix — run_discovery windowing wiring + noise-rejection preserved.

Store-level selection parity + the memory-bound decode are pinned in test_brain_discovery_windowed_load.
Here we prove (1) run_discovery derives the two floors from the frontier and threads them + the
horizon row_filter + the label column projection into the reads (so the streamed store guarantees
the windowed selection end-to-end), and (2) the permutation null still rejects pure noise on the
(now windowed) data — the exchangeability guarantee is untouched by the load change.
"""
from __future__ import annotations

import random

from crypto.research.brain import labels as brain_labels
from crypto.research.brain.discovery import config as dcfg
from crypto.research.brain.discovery import exits as X
from crypto.research.brain.discovery import rulestore as RS
from crypto.research.brain.discovery import runner
from crypto.research.brain.discovery import tradelog as TL

_W = 60_000_000_000
_FRONTIER = 1_800_000_000 * _W // 60 * 60          # a fixed frontier ns


def test_run_discovery_windows_reads_from_frontier(tmp_path, monkeypatch):
    calls = []

    def fake_read(root, dataset, symbol=None, *, after_recv_ts_ns=0, window_end_floor_ns=0,
                  columns=None, row_filter=None):
        calls.append({"dataset": dataset, "after": after_recv_ts_ns, "floor": window_end_floor_ns,
                      "columns": columns, "row_filter": row_filter})
        return []

    class _DummyReg:
        def close(self):
            pass

    monkeypatch.setattr(runner.brain_store, "read_snapshots", fake_read)
    monkeypatch.setattr(runner.brain_labels, "_markprice_frontier_ns", lambda reg: _FRONTIER)
    monkeypatch.setattr(runner.brain_registry, "connect", lambda p: _DummyReg())
    # short-circuit the heavy pass — we only assert the LOAD is windowed
    monkeypatch.setattr(runner, "run_discovery_pass", lambda *a, **k: {"survivors": 0})

    runner.run_discovery(discovery_db_path=str(tmp_path / "d.sqlite"), now_ns=_FRONTIER + _W)

    label_calls = [c for c in calls if c["dataset"] == brain_labels.LABEL_DATASET]
    prim_calls = [c for c in calls if c["dataset"] != brain_labels.LABEL_DATASET]

    assert len(label_calls) == 1
    lc = label_calls[0]
    assert lc["after"] == _FRONTIER - dcfg.DISCOVERY_HISTORY_NS          # 14d label floor
    assert lc["floor"] == _FRONTIER - dcfg.DISCOVERY_HISTORY_NS
    assert lc["columns"] == runner._LABEL_LOAD_COLUMNS                   # projection (drops fwd_return)
    assert lc["row_filter"] is not None                                 # horizon==60 predicate

    prim_floor = _FRONTIER - (dcfg.DISCOVERY_HISTORY_NS + dcfg.DISCOVERY_PRIMITIVE_LOOKBACK_NS)
    assert prim_calls, "primitives must be read"
    assert all(c["after"] == prim_floor and c["floor"] == prim_floor for c in prim_calls)   # 16d
    assert all(c["columns"] is None and c["row_filter"] is None for c in prim_calls)         # full cols
    # primitive floor is strictly older than the label floor (the z-lookback margin)
    assert prim_floor < lc["floor"]


def test_run_discovery_floors_never_go_negative(tmp_path, monkeypatch):
    # a young store (frontier < window) must floor at 0, not a negative ns
    calls = []
    monkeypatch.setattr(runner.brain_store, "read_snapshots",
                        lambda *a, **k: calls.append(k) or [])
    monkeypatch.setattr(runner.brain_labels, "_markprice_frontier_ns", lambda reg: 5 * _W)
    monkeypatch.setattr(runner.brain_registry, "connect",
                        lambda p: type("R", (), {"close": lambda s: None})())
    monkeypatch.setattr(runner, "run_discovery_pass", lambda *a, **k: {})
    runner.run_discovery(discovery_db_path=str(tmp_path / "d.sqlite"), now_ns=10 * _W)
    assert all(c["after_recv_ts_ns"] >= 0 and c["window_end_floor_ns"] >= 0 for c in calls)


def test_windowed_pure_noise_yields_no_survivors(tmp_path):
    # Noise-rejection (exchangeability), on windowed-shaped data: pure-noise lifts -> 0 survivors.
    rng = random.Random(7)
    syms = [f"S{j}" for j in range(8)]
    eng, lifts = {}, {}
    for i in range(240):                              # ample instances for the null at min_firing=20
        sym, w = syms[i % 8], (i + 1) * _W
        eng[(sym, w)] = {"a.raw": rng.random(), "b.raw": rng.random(), "c.raw": rng.random()}
        lifts[(sym, w)] = rng.gauss(0.0, 0.01)        # centred noise, NO relationship to features
    price_index = {s: {wi * _W: (100.0, 100.1, 99.9) for wi in range(250)} for s in syms}
    coin_vols = runner.coin_volatilities(price_index)

    conn = RS.connect(str(tmp_path / "d.sqlite"))
    TL.ensure_schema(conn)
    try:
        summary = runner.run_discovery_pass(
            conn, eng, lifts, price_index, coin_vols,
            feature_ids=["a.raw", "b.raw", "c.raw"], frontier_ns=1000 * _W, now_ns=10,
            n_bins=5, n_permutations=60, null_quantile=0.95, min_firing=20, max_depth=2,
            m=30, z=2.0, exit_grid=X.build_exit_grid((1.0,), (1.0,), (5,)), seed=1)
        assert summary["survivors"] == 0             # nothing beats the null on pure noise
    finally:
        conn.close()
