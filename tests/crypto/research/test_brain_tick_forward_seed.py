"""Cold-start forward-seed for the brain TICK loop (Phase 1 deploy safety).

The load-bearing test: a first tick over capture data that contains an OLD settled window
does NOT replay that backlog — the forward-seed pins the cursor to ``now - watermark`` so
only windows settling from now on are ever read. Disabling the seed (the escape hatch)
replays it, proving the seed is the thing that prevents the ~19GB/275K-file backlog chew.
Plus: cold sources seed to ~now, advanced cursors are never reseeded, the seed is
per-source, and a sub-watermark clock is a no-op (keeps the tiny-clock unit tests green).
"""
from __future__ import annotations

import pathlib
import types

from crypto.research.brain import config as cfg
from crypto.research.brain import registry
from crypto.research.brain import runner as R
from crypto.research.brain import sources as S
from crypto.research.brain import store as brain_store
from crypto.research.capture_core import store as capture_store

NOW_MS = 1_781_640_000_000          # 2026-06-16 20:00:00 UTC (a 60s boundary)
NOW_NS = NOW_MS * 1_000_000
WM = cfg.BRAIN_WATERMARK_NS
SEED_TO = NOW_NS - WM


def _spec(dataset, reader_name):
    return types.SimpleNamespace(dataset=dataset, reader_name=reader_name)


def _spy_runner(reg, *, sources, now_ns=NOW_NS, forward_seed=True):
    return R.BrainRunner(
        capture_root="cap", store_root="store", registry_path=reg, sources=sources,
        primitives_pass=lambda *a, **k: {}, labels_pass=lambda *a, **k: [],
        clock_ns=lambda: now_ns, sleep=lambda s: False, label_every_n_ticks=1,
        install_signals=False, forward_seed=forward_seed)


# -- seed semantics (registry-level) ------------------------------------------

def test_cold_source_seeds_cursor_to_now_minus_watermark(tmp_path):
    reg = str(tmp_path / "reg.sqlite")
    _spy_runner(reg, sources=[_spec("trades", "trades")]).tick(0)
    conn = registry.connect(reg)
    try:
        assert registry.get_cursor(conn, "trades") == SEED_TO
    finally:
        conn.close()


def test_advanced_cursor_is_not_reseeded(tmp_path):
    reg = str(tmp_path / "reg.sqlite")
    conn = registry.connect(reg)
    advanced = NOW_NS - 5 * WM                       # a real, already-advanced cursor
    registry.advance(conn, "trades", new_recv_ts_ns=advanced, now_ns=1)
    conn.close()
    _spy_runner(reg, sources=[_spec("trades", "trades")]).tick(0)
    conn = registry.connect(reg)
    try:
        assert registry.get_cursor(conn, "trades") == advanced     # untouched, no skip
    finally:
        conn.close()


def test_seed_is_per_source_independent(tmp_path):
    reg = str(tmp_path / "reg.sqlite")
    conn = registry.connect(reg)
    advanced = NOW_NS - 5 * WM
    registry.advance(conn, "markprice", new_recv_ts_ns=advanced, now_ns=1)  # one already advanced
    conn.close()
    _spy_runner(reg, sources=[_spec("trades", "trades"), _spec("markprice", "markprice")]).tick(0)
    conn = registry.connect(reg)
    try:
        assert registry.get_cursor(conn, "trades") == SEED_TO       # cold -> seeded
        assert registry.get_cursor(conn, "markprice") == advanced   # advanced -> untouched
    finally:
        conn.close()


def test_subwatermark_clock_is_a_noop_and_never_opens_the_registry(tmp_path):
    reg = str(tmp_path / "reg.sqlite")
    _spy_runner(reg, sources=[_spec("trades", "trades")], now_ns=1000).tick(0)
    assert not pathlib.Path(reg).exists()            # tiny clock -> no seed, no registry file


def test_forward_seed_disabled_does_not_seed(tmp_path):
    reg = str(tmp_path / "reg.sqlite")
    _spy_runner(reg, sources=[_spec("trades", "trades")], forward_seed=False).tick(0)
    assert not pathlib.Path(reg).exists()            # escape hatch: no seed at all


# -- the decisive no-backlog-replay test (real capture data + real pipeline) ---

def _write_old_backlog(cap_root):
    """One OLD settled window (~1h before now) — pure backlog a cold cursor would replay."""
    old_ms = NOW_MS - 3_600_000
    base = old_ms * 1_000_000
    w = capture_store.aggtrade_writer(str(cap_root))
    w.append({"recv_ts_ns": base + 1, "e": "aggTrade", "E": old_ms + 1_000, "a": 1,
              "s": "BTCUSDT", "p": "100", "q": "2", "f": 1, "l": 1, "T": old_ms + 1_000, "m": False})
    w.append({"recv_ts_ns": base + 2, "e": "aggTrade", "E": old_ms + 2_000, "a": 2,
              "s": "BTCUSDT", "p": "101", "q": "3", "f": 2, "l": 2, "T": old_ms + 2_000, "m": True})
    w.flush_all()


def _real_runner(tmp_path, *, forward_seed):
    return R.BrainRunner(
        capture_root=str(tmp_path / "capture"), store_root=str(tmp_path / "brain"),
        registry_path=str(tmp_path / "brain" / "registry.sqlite"), sources=[S.TRADES],
        labels_pass=lambda *a, **k: [], clock_ns=lambda: NOW_NS, sleep=lambda s: False,
        label_every_n_ticks=99, install_signals=False, forward_seed=forward_seed)


def test_forward_seed_skips_the_historical_backlog(tmp_path):
    _write_old_backlog(tmp_path / "capture")
    _real_runner(tmp_path, forward_seed=True).tick(0)
    # the 1h-old settled window is NEVER materialised — no backlog replay
    assert brain_store.read_snapshots(str(tmp_path / "brain"), "trades") == []
    conn = registry.connect(str(tmp_path / "brain" / "registry.sqlite"))
    try:
        assert registry.get_cursor(conn, "trades") == SEED_TO       # cursor forward-seeded
    finally:
        conn.close()


def test_disabling_forward_seed_replays_the_backlog(tmp_path):
    _write_old_backlog(tmp_path / "capture")
    _real_runner(tmp_path, forward_seed=False).tick(0)
    rows = brain_store.read_snapshots(str(tmp_path / "brain"), "trades")
    assert len(rows) == 1 and rows[0]["symbol"] == "BTCUSDT"        # backlog IS replayed without the seed
