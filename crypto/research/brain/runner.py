"""Continuous brain runner loop (Phase 1, step 1 of: loop -> label-read bound -> live-gate).

A thin, synchronous loop that WRAPS the existing passes — it adds orchestration, never new
state. Each cadence tick (default ``BRAIN_BASE_CADENCE_S`` = 60s):

  1. PRIMITIVES — run :func:`pipeline.run_pass` once per registered source (``sources.SOURCES``).
     Each pass reads its own per-source registry cursor, summarizes newly-settled windows, writes
     parquet, and advances the cursor + bookkeeping in ONE transaction. The dense recv-dated
     sources run EVERY tick; the SLOW sources (klines + the 7 as-of series, ``sources.
     SLOW_SOURCE_DATASETS``) run every ``slow_source_every_n_ticks`` ticks with an ``N × window``
     forward read (Fix 1 — they are sparse but were footer-scanned every tick beside the firehose).
  2. LABELS — every ``label_every_n_ticks`` ticks, run :func:`labels.run_once` (forward-path
     markprice labels), which reads the just-written markprice snapshots and the settlement
     frontier and appends newly-settled label rows.

IDEMPOTENT / CRASH-RESUME = RESTART. The loop holds NO durable state of its own (the tick
counter is in-memory only). All progress lives in the per-source registry cursor + snapshot/
label bookkeeping, advanced ONLY through the existing one-transaction path. So a crash is
recovered by simply restarting: the next tick reads each cursor and continues. There is no
separate resume logic, and nothing here advances state outside that path.

BUILD-ONLY. This is a runnable entrypoint (``python -m crypto.research.brain.runner``); it is
NOT wired to systemd/a timer (that lands at the live-gate, step 3) and does NOT change the label
read cost (the label-read bound is step 2). The two open lifecycle/cadence choices are
configurable and surfaced for the operator (see the module's PR):

  * LABEL SUB-CADENCE (``label_every_n_ticks``, default :data:`DEFAULT_LABEL_EVERY_N_TICKS` = 5):
    labels run every 5th tick (~5 min). RATIONALE: ``labels.run_once`` re-reads each symbol's
    FULL markprice history per call, and the shortest horizon is 5 min, so running it every 60s
    tick repeats that whole-history read up to 5× before any new 5-min label can settle. Every
    5th tick aligns the label cadence to the shortest horizon at ~1/5 the read cost. The
    alternative (every tick = 1) gives the lowest label latency at the highest read cost; the
    label-read bound (step 2) shrinks that cost and makes a faster cadence cheap. PROPOSAL:
    keep 5 until step 2 lands, then revisit — operator to confirm.
  * STOP LIFECYCLE: SIGTERM/SIGINT request a GRACEFUL stop — the in-flight tick finishes (or the
    inter-tick sleep is interrupted), then the loop exits. In-flight work is safe because each
    pass only advances state through its atomic registry transaction, so a stop between passes
    leaves a consistent cursor that the next start resumes from. PROPOSAL: graceful-finish (not
    hard-abort); operator to confirm.

ERROR ISOLATION. A failing pass for ONE source (or the label pass) is caught, logged, and the
loop continues — that source's cursor simply does not advance, so the work is retried on the next
tick (idempotent). One bad source never wedges the others or the loop. (Surfaced alongside the
lifecycle proposal; the alternative is fail-fast + restart.)

Like the rest of brain, this opens NO DuckDB / engine DB / capture store for writing.
"""
from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from typing import Callable, Optional, Sequence

from crypto.research.brain import config as cfg
from crypto.research.brain import labels
from crypto.research.brain import pipeline
from crypto.research.brain import registry
from crypto.research.brain import sources

logger = logging.getLogger("mhde.crypto.brain.runner")

#: Surfaced default: run the label pass every 5th tick (~5 min == the shortest horizon),
#: not every tick — see the module docstring for the rationale + the alternative.
DEFAULT_LABEL_EVERY_N_TICKS = 5


class BrainRunner:
    """A continuous loop wrapping the existing brain passes; owns no durable state of its own."""

    def __init__(
        self,
        *,
        capture_root: str,
        store_root: str,
        registry_path: str,
        sources: Optional[Sequence] = None,
        cadence_ns: int = cfg.BRAIN_BASE_CADENCE_NS,
        watermark_ns: int = cfg.BRAIN_WATERMARK_NS,
        batch_size: int = cfg.BRAIN_PASS_BATCH_SIZE,
        symbols: Optional[Sequence[str]] = None,
        label_store_root: Optional[str] = None,
        horizons_min: Optional[Sequence[int]] = None,
        label_every_n_ticks: int = DEFAULT_LABEL_EVERY_N_TICKS,
        slow_source_every_n_ticks: int = cfg.BRAIN_SLOW_SOURCE_EVERY_N_TICKS,
        slow_source_datasets: Optional[frozenset] = None,
        max_tick_window_ns: int = cfg.BRAIN_MAX_TICK_WINDOW_NS,
        tick_interval_s: float = cfg.BRAIN_BASE_CADENCE_S,
        clock_ns: Optional[Callable[[], int]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], bool]] = None,
        stop_event: Optional[threading.Event] = None,
        install_signals: bool = False,
        forward_seed: bool = True,
        primitives_pass: Optional[Callable] = None,
        labels_pass: Optional[Callable] = None,
        on_tick: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._capture_root = capture_root
        self._store_root = store_root
        self._registry_path = registry_path
        # default = the whole registered source universe (the 12 SOURCES specs; depth deferred, KI-159).
        self._sources = list(sources if sources is not None else sources_module_values())
        self._cadence_ns = cadence_ns
        self._watermark_ns = watermark_ns
        self._batch_size = batch_size
        self._symbols = symbols
        self._label_store_root = label_store_root
        self._horizons_min = list(horizons_min) if horizons_min is not None else list(labels.HORIZONS_MIN)
        if label_every_n_ticks < 1:
            raise ValueError("label_every_n_ticks must be >= 1")
        self._label_every_n_ticks = label_every_n_ticks
        if slow_source_every_n_ticks < 1:
            raise ValueError("slow_source_every_n_ticks must be >= 1")
        self._slow_source_every_n_ticks = slow_source_every_n_ticks
        # default = the real slow set (klines + as-of); resolved via a module helper because the
        # `sources` __init__ PARAM shadows the `sources` module name in this scope.
        self._slow_source_datasets = (slow_source_datasets if slow_source_datasets is not None
                                      else slow_source_datasets_default())
        self._max_tick_window_ns = max_tick_window_ns
        self._tick_interval_s = tick_interval_s
        self._clock_ns = clock_ns if clock_ns is not None else time.time_ns
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._stop_event = stop_event if stop_event is not None else threading.Event()
        # interruptible sleep: returns True iff a stop was requested DURING the wait.
        self._sleep = sleep if sleep is not None else self._stop_event.wait
        self._install_signals = install_signals
        self._forward_seed = forward_seed
        # default to the real passes; injectable for tests.
        self._primitives_pass = primitives_pass if primitives_pass is not None else pipeline.run_pass
        self._labels_pass = labels_pass if labels_pass is not None else labels.run_once
        self._on_tick = on_tick

    # -- lifecycle --

    def stop(self) -> None:
        """Request a graceful shutdown. Idempotent; safe from a signal handler."""
        self._stop_event.set()

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, lambda *_: self.stop())
            except (ValueError, OSError):           # not the main thread — caller wires stop()
                logger.warning("brain runner: could not install handler for %s "
                               "(not main thread); rely on stop()", sig)

    # -- one tick --

    def _run_primitives(self, now_ns: int, tick_index: int) -> list:
        """Run one primitives pass per source. The DENSE recv-dated sources run every tick; the
        SLOW sources (``self._slow_source_datasets``) run only every ``slow_source_every_n_ticks``
        ticks (Fix 1) and, when they do, use an ``N × window`` forward read so they cover ~N
        ticks of tape and stay gap-free + keep pace. A per-source failure is isolated (logged,
        the loop continues; that cursor simply doesn't advance, so it retries next due tick)."""
        results = []
        for spec in self._sources:
            dataset = getattr(spec, "dataset", None)
            is_slow = dataset in self._slow_source_datasets
            if is_slow and tick_index % self._slow_source_every_n_ticks != 0:
                # CONTRACT: every primitive result carries dataset/ok/ran; a ran=True result also
                # carries "summary" (or "error" when ok is False). A skipped (ran=False) result has
                # no pass, so "summary" is None — consumers must branch on "ran" before reading it.
                results.append({"dataset": dataset, "ok": True, "ran": False, "summary": None})
                continue
            window_ns = (self._slow_source_every_n_ticks * self._max_tick_window_ns
                         if is_slow else self._max_tick_window_ns)
            try:
                summary = self._primitives_pass(
                    spec, capture_root=self._capture_root, store_root=self._store_root,
                    registry_path=self._registry_path, now_ns=now_ns,
                    cadence_ns=self._cadence_ns, watermark_ns=self._watermark_ns,
                    symbols=self._symbols, batch_size=self._batch_size, max_window_ns=window_ns)
                results.append({"dataset": dataset, "ok": True, "ran": True, "summary": summary})
            except Exception as exc:                # noqa: BLE001 — one source must not wedge the loop
                logger.warning("brain runner: primitives pass failed for %s; retry next tick",
                               dataset, exc_info=True)
                results.append({"dataset": dataset, "ok": False, "ran": True, "error": str(exc)})
        return results

    def _run_labels(self, now_ns: int) -> dict:
        """Run one label pass. Isolated like the primitives passes (logged + retried next tick)."""
        try:
            written = self._labels_pass(
                store_root=self._store_root, capture_root=self._capture_root,
                registry_path=self._registry_path, label_store_root=self._label_store_root,
                horizons_min=self._horizons_min, symbols=self._symbols, now_ns=now_ns)
            return {"ok": True, "written": len(written) if written is not None else 0}
        except Exception as exc:                    # noqa: BLE001
            logger.warning("brain runner: label pass failed; retry next label tick", exc_info=True)
            return {"ok": False, "error": str(exc)}

    def _forward_seed_cold_cursors(self, now_ns: int) -> int:
        """COLD-START FORWARD SEED. Any source whose registry cursor is absent/zero is
        seeded forward to ``now - watermark``, so a first tick — or a cursor that was reset
        to 0 — reads ONLY windows that settle from now on, NEVER replaying the entire
        historical capture backlog (the fragmentation wall). Per-source and idempotent: an
        already-advanced cursor is never touched (gated on ``cursor == 0``; a real recv
        cursor is a large epoch-ns value, so 0 unambiguously means 'never advanced', and
        ``registry.advance`` is monotonic regardless). Returns the count seeded.

        Gated off when ``now - watermark <= 0`` (a sub-watermark clock, e.g. unit tests or
        the first seconds of the epoch): nothing is seeded and the registry is not opened.
        """
        if not self._forward_seed:
            return 0
        seed_to = now_ns - self._watermark_ns
        if seed_to <= 0:
            return 0
        conn = registry.connect(self._registry_path)
        try:
            seeded = 0
            for spec in self._sources:
                reader_name = getattr(spec, "reader_name", None) or getattr(spec, "dataset", None)
                if reader_name is None:
                    continue
                if registry.get_cursor(conn, reader_name) == 0:
                    registry.advance(conn, reader_name, new_recv_ts_ns=seed_to, now_ns=now_ns)
                    seeded += 1
            if seeded:
                logger.info("brain runner: forward-seeded %d cold cursor(s) to now-%.0fs "
                            "(no backlog replay)", seeded, self._watermark_ns / 1e9)
            return seeded
        finally:
            conn.close()

    def tick(self, tick_index: int) -> dict:
        """Run ONE tick: forward-seed any cold cursors, every source's primitives pass, then
        (on the label sub-cadence) labels. A single ``now_ns`` snapshot is shared by the tick."""
        now_ns = self._clock_ns()
        self._forward_seed_cold_cursors(now_ns)
        primitives = self._run_primitives(now_ns, tick_index)
        labels_ran = (tick_index % self._label_every_n_ticks == 0)
        labels_result = self._run_labels(now_ns) if labels_ran else None
        summary = {"tick": tick_index, "now_ns": now_ns, "primitives": primitives,
                   "labels_ran": labels_ran, "labels": labels_result}
        if self._on_tick is not None:
            self._on_tick(summary)
        return summary

    # -- the loop --

    def run(self, *, max_ticks: Optional[int] = None) -> dict:
        """Loop until SIGTERM/SIGINT / :meth:`stop` (or ``max_ticks`` for tests). Returns a
        summary of the run. The inter-tick sleep is drift-corrected to ``tick_interval_s`` and
        interruptible, so a stop request never waits out a full cadence."""
        if self._install_signals:
            self._install_signal_handlers()
        logger.info("brain runner starting: %d sources, tick %.0fs, labels every %d tick(s)",
                    len(self._sources), self._tick_interval_s, self._label_every_n_ticks)
        ticks = label_runs = 0
        while not self._stop_event.is_set():
            if max_ticks is not None and ticks >= max_ticks:
                break
            start = self._monotonic()
            summary = self.tick(ticks)
            if summary["labels_ran"]:
                label_runs += 1
            ticks += 1
            if self._stop_event.is_set():           # stop requested DURING the tick
                break
            # INVARIANT: the cadence sleep below must stay UNCONDITIONAL — every tick reaches
            # it, including one whose passes all errored (errors are isolated in tick(), never
            # raised here). That is what bounds a total-failure tick to one-per-cadence instead
            # of a hot CPU spin; never add an early continue/skip-sleep on error above this line.
            elapsed = self._monotonic() - start
            sleep_s = max(0.0, self._tick_interval_s - elapsed)
            if self._sleep(sleep_s):                # True == stop requested during the sleep
                break
        logger.info("brain runner stopped: %d tick(s), %d label run(s)", ticks, label_runs)
        return {"ticks": ticks, "label_runs": label_runs}


def sources_module_values() -> list:
    """The registered source specs (``sources.SOURCES`` values) — the default source universe."""
    return list(sources.SOURCES.values())


def slow_source_datasets_default() -> frozenset:
    """The default SLOW-source set (``sources.SLOW_SOURCE_DATASETS``). A module helper because
    the ``sources`` constructor param shadows the module name inside ``__init__``."""
    return sources.SLOW_SOURCE_DATASETS


# -- entrypoint (NO systemd/timer wiring — that lands at the live-gate) ---------

def build_runner(argv_namespace: argparse.Namespace, *, install_signals: bool) -> BrainRunner:
    return BrainRunner(
        capture_root=argv_namespace.capture_root,
        store_root=argv_namespace.store_root,
        registry_path=argv_namespace.registry_path,
        label_store_root=argv_namespace.label_store_root,
        label_every_n_ticks=argv_namespace.label_every_n_ticks,
        slow_source_every_n_ticks=argv_namespace.slow_source_every_n_ticks,
        tick_interval_s=argv_namespace.tick_interval_s,
        forward_seed=argv_namespace.forward_seed,
        install_signals=install_signals,
    )


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m crypto.research.brain.runner",
        description="Continuous brain runner loop (primitives each tick, then labels). "
                    "Not wired to systemd; run manually.")
    p.add_argument("--capture-root", default=cfg.CAPTURE_RAW_DIR)
    p.add_argument("--store-root", default=cfg.BRAIN_STORE_ROOT)
    p.add_argument("--registry-path", default=cfg.BRAIN_REGISTRY_PATH)
    p.add_argument("--label-store-root", default=None,
                   help="defaults to --store-root (labels land beside the primitives)")
    p.add_argument("--label-every-n-ticks", type=int, default=DEFAULT_LABEL_EVERY_N_TICKS)
    p.add_argument("--slow-source-every-n-ticks", type=int,
                   default=cfg.BRAIN_SLOW_SOURCE_EVERY_N_TICKS,
                   help="run the slow sources (klines + as-of) every Nth tick (default 5); the "
                        "dense recv-dated sources always run every tick")
    p.add_argument("--tick-interval-s", type=float, default=cfg.BRAIN_BASE_CADENCE_S)
    p.add_argument("--max-ticks", type=int, default=None,
                   help="stop after N ticks (default: run until SIGTERM/SIGINT)")
    p.add_argument("--no-forward-seed", dest="forward_seed", action="store_false",
                   help="DANGEROUS: disable cold-start forward-seeding, so an uninitialized "
                        "source replays the ENTIRE capture backlog from cursor 0 (a deliberate "
                        "full backfill). Default: forward-seed to now-watermark (no replay).")
    p.set_defaults(forward_seed=True)
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None, *, install_signals: bool = True) -> dict:
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
    runner = build_runner(args, install_signals=install_signals)
    return runner.run(max_ticks=args.max_ticks)


if __name__ == "__main__":
    main()
