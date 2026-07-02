"""Drift Fix 5: ONE INFO line per tick from the runner loop.

The runner logged nothing per tick (2 INFO lines at start, then silence), so diagnosing the
2026-07-02 overnight drift required external cursor sampling + registry archaeology. Each
tick now emits a single INFO line — tick index, wall seconds, sources ok/ran, snapshots
written, labels written, max cursor lag — cheap string work over the already-built tick
summary, after the tick's work and before the cadence sleep (no cadence impact).
"""
from __future__ import annotations

import logging
import types

from crypto.research.brain import runner as R

_MIN = 60_000_000_000


class _Spy:
    def __init__(self, result=None, raises=None):
        self.calls = []
        self._result = result
        self._raises = raises

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._raises is not None:
            raise self._raises
        return self._result


def _spec(name):
    return types.SimpleNamespace(dataset=name)


def _runner(**overrides):
    base = dict(
        capture_root="cap", store_root="store", registry_path="reg.sqlite",
        sources=[_spec("trades"), _spec("markprice")],
        primitives_pass=_Spy({"snapshots_written": 3, "cursor_after": 0}),
        labels_pass=_Spy([]),
        clock_ns=lambda: 1000,
        sleep=lambda s: False,
        label_every_n_ticks=1, install_signals=False,
    )
    base.update(overrides)
    return R.BrainRunner(**base)


# -- the pure line builder ----------------------------------------------------------------

def _summary(now_ns=600 * _MIN, *, prims=None, labels_ran=False, labels=None, tick=7):
    return {"tick": tick, "now_ns": now_ns,
            "primitives": prims if prims is not None else [],
            "labels_ran": labels_ran, "labels": labels}


def test_line_reports_wall_sources_snapshots_labels_and_max_lag():
    now = 600 * _MIN
    prims = [
        {"dataset": "trades", "ok": True, "ran": True,
         "summary": {"snapshots_written": 3, "cursor_after": now - 120 * 1_000_000_000}},
        {"dataset": "markprice", "ok": True, "ran": True,
         "summary": {"snapshots_written": 4, "cursor_after": now - 843 * 1_000_000_000}},
        {"dataset": "basis", "ok": True, "ran": False, "summary": None},   # skipped slow tick
    ]
    line = R._tick_log_line(_summary(now, prims=prims, labels_ran=True,
                                     labels={"ok": True, "written": 88_620}), 63.25)
    assert "brain tick 7:" in line
    assert "wall=63.2s" in line
    assert "sources=2/2 ok" in line, "the skipped slow source is not 'ran'"
    assert "snapshots=7" in line
    assert "labels=88620" in line
    assert "max_lag=843s" in line


def test_line_surfaces_source_and_label_errors():
    prims = [
        {"dataset": "trades", "ok": True, "ran": True,
         "summary": {"snapshots_written": 1, "cursor_after": 599 * _MIN}},
        {"dataset": "markprice", "ok": False, "ran": True, "error": "boom"},
    ]
    line = R._tick_log_line(_summary(prims=prims, labels_ran=True,
                                     labels={"ok": False, "error": "label boom"}), 10.0)
    assert "sources=1/2 ok" in line
    assert "labels=ERROR" in line


def test_line_without_labels_or_lag_uses_dashes():
    prims = [{"dataset": "trades", "ok": True, "ran": True, "summary": {"snapshots_written": 0}}]
    line = R._tick_log_line(_summary(prims=prims), 1.0)
    assert "labels=-" in line
    assert "max_lag=-" in line, "no cursor info -> a dash, never a crash"


def test_line_tolerates_minimal_test_double_summaries():
    # a primitives_pass double may return an arbitrary dict — the line must never crash on it.
    prims = [{"dataset": "a", "ok": True, "ran": True, "summary": {"ok": 1}}]
    line = R._tick_log_line(_summary(prims=prims), 0.5)
    assert "brain tick" in line and "snapshots=0" in line


# -- wired into the loop ------------------------------------------------------------------

def test_run_emits_one_info_line_per_tick(caplog):
    r = _runner()
    with caplog.at_level(logging.INFO, logger="mhde.crypto.brain.runner"):
        r.run(max_ticks=3)
    tick_lines = [rec.message for rec in caplog.records if rec.message.startswith("brain tick")]
    assert len(tick_lines) == 3
    assert tick_lines[0].startswith("brain tick 0:")
    assert tick_lines[2].startswith("brain tick 2:")
    for ln in tick_lines:
        assert "wall=" in ln and "sources=" in ln and "snapshots=" in ln and "max_lag=" in ln
