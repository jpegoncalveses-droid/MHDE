"""Continuous brain runner loop (Phase 1, step 1) — wrap the existing passes.

Each cadence tick runs the primitives pass (``pipeline.run_pass``) for every registered
source, then — every ``label_every_n_ticks`` ticks — the label pass (``labels.run_once``).
The loop owns NO durable state of its own: all progress lives in the per-source registry
cursor + bookkeeping, advanced only through the existing one-transaction path, so
crash-resume is just a restart.

These tests inject spy passes + a fake clock/sleep so the loop is exercised without real
capture data, real time, or real sleeping.
"""
from __future__ import annotations

import signal
import threading
import types

from crypto.research.brain import config as cfg
from crypto.research.brain import runner as R


# -- test doubles --------------------------------------------------------------

class _Spy:
    """Records (args, kwargs) per call; returns ``result`` or raises ``raises``."""
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


def _seq_clock(values):
    """A clock callable yielding ``values`` in order, then repeating the last."""
    it = iter(values)
    last = [0]

    def _c():
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]
    return _c


def _runner(**overrides):
    base = dict(
        capture_root="cap", store_root="store", registry_path="reg.sqlite",
        sources=[_spec("trades"), _spec("markprice")],
        primitives_pass=_Spy({"ok": 1}), labels_pass=_Spy([]),
        clock_ns=_seq_clock([1000, 2000, 3000, 4000, 5000, 6000]),
        sleep=lambda s: False,                 # no-op interruptible sleep (never interrupted)
        label_every_n_ticks=1, install_signals=False,
    )
    base.update(overrides)
    return R.BrainRunner(**base)


# -- one tick: every source, then labels ---------------------------------------

def test_tick_runs_each_source_then_labels():
    prim, lab = _Spy({"ok": 1}), _Spy([{"x": 1}])
    r = _runner(primitives_pass=prim, labels_pass=lab,
                sources=[_spec("trades"), _spec("markprice"), _spec("bookticker")],
                label_every_n_ticks=1)
    summary = r.tick(0)
    assert [c[0][0].dataset for c in prim.calls] == ["trades", "markprice", "bookticker"]
    assert len(lab.calls) == 1
    assert summary["labels_ran"] is True
    # primitives pass gets the full run_pass kwarg surface
    _, pkw = prim.calls[0]
    assert pkw["now_ns"] == 1000 and pkw["capture_root"] == "cap"
    assert pkw["store_root"] == "store" and pkw["registry_path"] == "reg.sqlite"
    for k in ("cadence_ns", "watermark_ns", "batch_size", "symbols"):
        assert k in pkw
    # labels pass gets the label kwarg surface, same now_ns
    _, lkw = lab.calls[0]
    assert lkw["now_ns"] == 1000 and lkw["store_root"] == "store"
    assert lkw["capture_root"] == "cap" and lkw["registry_path"] == "reg.sqlite"
    for k in ("label_store_root", "horizons_min", "symbols"):
        assert k in lkw


# -- the loop ------------------------------------------------------------------

def test_loop_runs_max_ticks_then_returns():
    prim, lab = _Spy({}), _Spy([])
    r = _runner(primitives_pass=prim, labels_pass=lab,
                sources=[_spec("a"), _spec("b")], label_every_n_ticks=1)
    out = r.run(max_ticks=3)
    assert out["ticks"] == 3
    assert len(prim.calls) == 6            # 2 sources × 3 ticks
    assert len(lab.calls) == 3
    assert out["label_runs"] == 3


def test_label_sub_cadence_runs_every_n_ticks():
    prim, lab = _Spy({}), _Spy([])
    r = _runner(primitives_pass=prim, labels_pass=lab, sources=[_spec("a")],
                label_every_n_ticks=2)
    r.run(max_ticks=4)
    assert len(prim.calls) == 4            # primitives every tick
    assert len(lab.calls) == 2            # labels on ticks 0 and 2


def test_now_ns_is_a_per_tick_snapshot_shared_by_all_passes():
    prim, lab = _Spy({}), _Spy([])
    r = _runner(primitives_pass=prim, labels_pass=lab,
                sources=[_spec("a"), _spec("b")],
                clock_ns=_seq_clock([111, 222, 333]), label_every_n_ticks=1)
    r.run(max_ticks=2)
    assert prim.calls[0][1]["now_ns"] == 111 and prim.calls[1][1]["now_ns"] == 111
    assert lab.calls[0][1]["now_ns"] == 111
    assert prim.calls[2][1]["now_ns"] == 222 and prim.calls[3][1]["now_ns"] == 222
    assert lab.calls[1][1]["now_ns"] == 222


# -- graceful stop -------------------------------------------------------------

def test_stop_during_sleep_breaks_the_loop():
    prim, lab = _Spy({}), _Spy([])
    ev = threading.Event()

    def _sleep_then_stop(_seconds):
        ev.set()
        return True                        # interrupted == stop requested during sleep

    r = _runner(primitives_pass=prim, labels_pass=lab, sources=[_spec("a")],
                stop_event=ev, sleep=_sleep_then_stop, label_every_n_ticks=1)
    out = r.run()                          # no max_ticks — relies on the stop
    assert out["ticks"] == 1
    assert len(prim.calls) == 1


def test_stop_requested_mid_tick_completes_current_tick_then_exits():
    ev = threading.Event()
    seen = []

    def _prim(spec, **_kw):
        seen.append(spec.dataset)
        if spec.dataset == "a":
            ev.set()                       # request stop while still inside tick 0
        return {}

    lab = _Spy([])
    r = _runner(primitives_pass=_prim, labels_pass=lab,
                sources=[_spec("a"), _spec("b")], stop_event=ev,
                sleep=lambda s: ev.is_set(), label_every_n_ticks=1)
    out = r.run(max_ticks=5)
    assert out["ticks"] == 1
    assert seen == ["a", "b"]              # the in-flight tick finishes both sources
    assert len(lab.calls) == 1            # and its labels


def test_stop_is_idempotent_and_sets_event():
    ev = threading.Event()
    r = _runner(stop_event=ev)
    r.stop()
    r.stop()
    assert ev.is_set()


# -- error isolation (one bad pass must not wedge the loop) ---------------------

def test_per_source_error_is_isolated_and_loop_continues():
    good = []

    def _prim(spec, **_kw):
        if spec.dataset == "bad":
            raise RuntimeError("boom")
        good.append(spec.dataset)
        return {}

    lab = _Spy([])
    r = _runner(primitives_pass=_prim, labels_pass=lab,
                sources=[_spec("bad"), _spec("good")], label_every_n_ticks=1)
    summary = r.tick(0)
    assert good == ["good"]               # the good source still ran
    assert len(lab.calls) == 1           # labels still ran
    res = {p["dataset"]: p for p in summary["primitives"]}
    assert res["bad"]["ok"] is False and res["good"]["ok"] is True
    # and the loop does not raise across ticks despite the recurring bad source
    assert r.run(max_ticks=2)["ticks"] == 2


def test_label_error_is_isolated():
    prim, lab = _Spy({}), _Spy(raises=RuntimeError("lbl"))
    r = _runner(primitives_pass=prim, labels_pass=lab, sources=[_spec("a")],
                label_every_n_ticks=1)
    summary = r.tick(0)
    assert summary["labels"]["ok"] is False
    assert r.run(max_ticks=2)["ticks"] == 2


# -- drift-corrected cadence ---------------------------------------------------

def test_sleep_is_drift_corrected_to_the_tick_interval():
    sleeps = []
    r = _runner(primitives_pass=_Spy({}), labels_pass=_Spy([]), sources=[_spec("a")],
                monotonic=_seq_clock([0.0, 10.0]),       # tick took 10s
                sleep=lambda s: (sleeps.append(s), False)[1],
                tick_interval_s=60.0, label_every_n_ticks=1)
    r.run(max_ticks=1)
    assert sleeps == [50.0]               # 60 - 10 elapsed


def test_sleep_is_zero_when_work_exceeds_the_interval():
    sleeps = []
    r = _runner(primitives_pass=_Spy({}), labels_pass=_Spy([]), sources=[_spec("a")],
                monotonic=_seq_clock([0.0, 90.0]),       # tick overran the interval
                sleep=lambda s: (sleeps.append(s), False)[1],
                tick_interval_s=60.0, label_every_n_ticks=1)
    r.run(max_ticks=1)
    assert sleeps == [0.0]                # never negative


# -- no durable state of its own (crash-resume == restart) ---------------------

def test_runner_persists_no_state_of_its_own(tmp_path):
    store, reg, cap = tmp_path / "store", tmp_path / "reg.sqlite", tmp_path / "cap"
    r = _runner(store_root=str(store), registry_path=str(reg), capture_root=str(cap),
                primitives_pass=_Spy({}), labels_pass=_Spy([]), sources=[_spec("a")])
    r.run(max_ticks=3)
    # the runner wrote nothing itself — only the (here no-op) injected passes would
    assert not store.exists() and not reg.exists()
    # a fresh runner re-run starts clean (no separate resume/checkpoint file to read)
    r2 = _runner(store_root=str(store), registry_path=str(reg), capture_root=str(cap),
                 primitives_pass=_Spy({}), labels_pass=_Spy([]), sources=[_spec("a")])
    assert r2.run(max_ticks=2)["ticks"] == 2


# -- defaults wire to the real passes + the whole source registry --------------

def test_defaults_wire_to_pipeline_labels_and_all_sources():
    from crypto.research.brain import labels as L
    from crypto.research.brain import pipeline
    from crypto.research.brain import sources as S
    r = R.BrainRunner(capture_root="c", store_root="s", registry_path="r",
                      install_signals=False)
    assert r._primitives_pass is pipeline.run_pass
    assert r._labels_pass is L.run_once
    assert list(r._sources) == list(S.SOURCES.values())
    assert r._horizons_min == L.HORIZONS_MIN
    assert r._cadence_ns == cfg.BRAIN_BASE_CADENCE_NS
    assert r._watermark_ns == cfg.BRAIN_WATERMARK_NS
    assert r._tick_interval_s == cfg.BRAIN_BASE_CADENCE_S
    assert r._label_every_n_ticks == R.DEFAULT_LABEL_EVERY_N_TICKS


def test_default_label_cadence_is_five_ticks():
    # surfaced proposal: labels every 5th tick (~5 min, the shortest horizon), not every tick
    assert R.DEFAULT_LABEL_EVERY_N_TICKS == 5


# -- entrypoint + signal lifecycle ---------------------------------------------

def test_main_runs_zero_ticks_without_looping():
    out = R.main(["--max-ticks", "0"], install_signals=False)
    assert out["ticks"] == 0


def test_install_signal_handlers_registers_a_stop_handler(monkeypatch):
    captured = {}
    monkeypatch.setattr(signal, "signal", lambda sig, handler: captured.__setitem__(sig, handler))
    ev = threading.Event()
    r = _runner(stop_event=ev, install_signals=True)
    r._install_signal_handlers()
    assert signal.SIGTERM in captured and signal.SIGINT in captured
    captured[signal.SIGTERM](signal.SIGTERM, None)     # firing it requests a graceful stop
    assert ev.is_set()
