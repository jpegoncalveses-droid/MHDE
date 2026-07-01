"""Fix 1: sub-cadence the slow, footer-scanned-but-low-frequency sources.

klines_1h + the 7 REST as-of series are sparse in ROWS but were footer-scanned EVERY tick
alongside the dense firehose. They run instead every ``BRAIN_SLOW_SOURCE_EVERY_N_TICKS`` ticks
(mirroring ``label_every_n_ticks``), while the four dense recv-dated sources (trades, bookticker,
markprice, forceorder) keep running every tick. A slow source, when it runs, uses a forward
window SCALED by N so it still reads its full cursor-forward span and advances gap-free +
keeps pace (each run covers ~N ticks of tape).

Classification is by explicit dataset-name membership (``sources.SLOW_SOURCE_DATASETS``);
anything else defaults to FAST (every tick), so an unknown/injected source is never silently
starved.
"""
from __future__ import annotations

import types
from collections import Counter

from crypto.research.brain import config as cfg
from crypto.research.brain import runner as R
from crypto.research.brain import sources as S


def _spec(name):
    return types.SimpleNamespace(dataset=name)


class _RecordPass:
    """Records ``(dataset, max_window_ns)`` per primitives call; returns an empty summary."""
    def __init__(self):
        self.calls = []

    def __call__(self, spec, **kw):
        self.calls.append((spec.dataset, kw.get("max_window_ns")))
        return {}

    def datasets(self):
        return [d for d, _ in self.calls]


def _runner(**overrides):
    base = dict(
        capture_root="c", store_root="s", registry_path="r",
        labels_pass=lambda **k: [],
        clock_ns=lambda: 1000,              # sub-watermark -> forward-seed gated off (no I/O)
        sleep=lambda s: False, forward_seed=False, install_signals=False,
        label_every_n_ticks=99,            # keep labels out of the way of these assertions
    )
    base.update(overrides)
    return R.BrainRunner(**base)


# -- the slow/fast classification ----------------------------------------------

def test_slow_source_datasets_are_asof_and_klines():
    expected = {S.KLINES.dataset, *(s.dataset for s in S.ASOF_SOURCES)}
    assert S.SLOW_SOURCE_DATASETS == expected
    for fast in (S.TRADES, S.BOOKTICKER, S.MARKPRICE, S.FORCEORDER):
        assert fast.dataset not in S.SLOW_SOURCE_DATASETS, f"{fast.dataset} must stay fast"


def test_default_slow_cadence_is_five():
    assert cfg.BRAIN_SLOW_SOURCE_EVERY_N_TICKS == 5
    r = R.BrainRunner(capture_root="c", store_root="s", registry_path="r", install_signals=False)
    assert r._slow_source_every_n_ticks == 5
    assert r._slow_source_datasets == S.SLOW_SOURCE_DATASETS


# -- fast every tick, slow every N ---------------------------------------------

def test_fast_runs_every_tick_slow_runs_every_n():
    rp = _RecordPass()
    r = _runner(sources=[_spec("markprice"), _spec("klines_1h"), _spec("open_interest")],
                primitives_pass=rp, slow_source_every_n_ticks=3)
    r.run(max_ticks=6)
    c = Counter(rp.datasets())
    assert c["markprice"] == 6, "a dense recv-dated source runs every tick"
    assert c["klines_1h"] == 2, "a slow source runs on ticks 0 and 3"
    assert c["open_interest"] == 2


def test_slow_source_runs_on_tick_zero():
    rp = _RecordPass()
    r = _runner(sources=[_spec("basis")], primitives_pass=rp, slow_source_every_n_ticks=5)
    r.run(max_ticks=1)                      # only tick 0
    assert rp.datasets() == ["basis"], "tick 0 (0 % N == 0) runs the slow sources"


def test_unknown_dataset_defaults_to_fast_every_tick():
    rp = _RecordPass()
    r = _runner(sources=[_spec("a"), _spec("b")], primitives_pass=rp, slow_source_every_n_ticks=3)
    r.run(max_ticks=3)
    c = Counter(rp.datasets())
    assert c["a"] == 3 and c["b"] == 3, "an unknown source is fast (never silently sub-cadenced)"


# -- the scaled forward window (gap-free + keep-pace) ---------------------------

def test_slow_source_uses_scaled_window_fast_uses_base():
    rp = _RecordPass()
    r = _runner(sources=[_spec("markprice"), _spec("klines_1h")],
                primitives_pass=rp, slow_source_every_n_ticks=4)
    r.run(max_ticks=1)                      # tick 0: both run
    wins = dict(rp.calls)
    assert wins["markprice"] == cfg.BRAIN_MAX_TICK_WINDOW_NS, "fast source uses the base window"
    assert wins["klines_1h"] == 4 * cfg.BRAIN_MAX_TICK_WINDOW_NS, \
        "a slow source running every 4th tick reads a 4x window so it stays gap-free + keeps pace"


def test_skipped_slow_source_is_marked_not_run_in_the_summary():
    rp = _RecordPass()
    r = _runner(sources=[_spec("markprice"), _spec("klines_1h")],
                primitives_pass=rp, slow_source_every_n_ticks=5)
    summary = r.tick(1)                     # tick 1: markprice runs, klines skipped (1 % 5 != 0)
    by = {p["dataset"]: p for p in summary["primitives"]}
    assert by["markprice"]["ran"] is True
    assert by["klines_1h"]["ran"] is False and by["klines_1h"]["ok"] is True
    assert "markprice" in rp.datasets() and "klines_1h" not in rp.datasets()


# -- the knob is surfaced on the CLI (operator-tunable, like --label-every-n-ticks) --

def test_slow_cadence_is_a_cli_flag():
    out = R.main(["--max-ticks", "0", "--slow-source-every-n-ticks", "7"], install_signals=False)
    assert out["ticks"] == 0
    ns = R._parse_args(["--slow-source-every-n-ticks", "7"])
    assert ns.slow_source_every_n_ticks == 7
