#!/usr/bin/env python3
"""ADR-039 §G-trial gap 2 — trial supervisor (monitor + anon watchdog).

The observability + OOM-safety harness for the UNPINNED Trial-1 (owner + N shards run by
hand in a transient scope). Each interval it samples and appends four pass/fail signals,
and aborts the trial scope if resident anon memory crosses a soft threshold:

  1) trial-scope ANON  — the ``anon`` field of the scope's cgroup-v2 ``memory.stat``,
     i.e. RESIDENT anonymous memory (the real heap). NOT ``memory.current`` — that
     includes reclaimable page cache and produced the ADR-038 trial's false OOM alarm.
  2) per-core CPU%     — computed from two ``/proc/stat`` per-cpu snapshots (<60%/core
     is the target that says sharding fixed the single-loop saturation).
  3) gap rate          — count of the runtime's connection-gap log lines.
  4) handshake-timeout rate — count of "timed out during opening handshake" lines (the
     reconnect-storm signal from the single-process incident).

WATCHDOG: when anon > threshold, stop the scope (``systemctl --user stop <scope>``) and
log the reason. This is the SOFT, earlier abort; Trial-1's launch ALSO sets a cgroup
``MemoryMax`` as a kernel-level hard backstop, so a watchdog-script failure still cannot
OOM the box.

This module launches NOTHING (the Trial-1 dispatch starts owner + shards), writes no
systemd units, and touches no cpuset. The orchestration is injectable so the logic is
unit-testable with mocked I/O; the ``__main__`` path wires the real readers.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import time
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger("mhde.crypto.capture_core.trial_monitor")

DEFAULT_ANON_THRESHOLD_GIB = 2.5
DEFAULT_ANON_THRESHOLD_BYTES = int(DEFAULT_ANON_THRESHOLD_GIB * 1024 ** 3)
DEFAULT_INTERVAL_S = 30.0
# The runtime's connection-level gap/reconnect log line (conn_manager.py) and the
# websockets opening-handshake timeout text that marks the reconnect storm.
DEFAULT_GAP_MARKER = "capture-core shard disconnected"
DEFAULT_HANDSHAKE_MARKER = "timed out during opening handshake"


# -- pure parsing / computation (unit-tested) ---------------------------------

def parse_anon_bytes(memory_stat_text: str) -> int:
    """Return the ``anon`` field (bytes) from a cgroup-v2 ``memory.stat`` blob.

    Resident anonymous memory — the real heap — NOT ``memory.current`` (which counts
    reclaimable page cache and caused the ADR-038 false OOM alarm). Raises if absent,
    so a malformed read can never silently substitute a page-cache value.
    """
    for line in memory_stat_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "anon":
            return int(parts[1])
    raise ValueError("no 'anon' field in memory.stat")


def parse_percpu_jiffies(proc_stat_text: str) -> Dict[str, Tuple[int, int]]:
    """Map ``cpuN -> (idle_jiffies, total_jiffies)`` from ``/proc/stat`` per-cpu lines.

    Skips the aggregate ``cpu`` line; idle = idle + iowait (fields 4 and 5).
    """
    out: Dict[str, Tuple[int, int]] = {}
    for line in proc_stat_text.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].startswith("cpu") or parts[0] == "cpu":
            continue
        nums = [int(x) for x in parts[1:]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)   # idle + iowait
        out[parts[0]] = (idle, sum(nums))
    return out


def percpu_util_pct(prev: Dict[str, Tuple[int, int]],
                    cur: Dict[str, Tuple[int, int]]) -> Dict[str, float]:
    """Per-core busy% between two ``/proc/stat`` snapshots (0.0 when no time elapsed)."""
    out: Dict[str, float] = {}
    for cpu, (idle0, total0) in prev.items():
        if cpu not in cur:
            continue
        idle1, total1 = cur[cpu]
        dt = total1 - total0
        di = idle1 - idle0
        out[cpu] = round(100.0 * (dt - di) / dt, 2) if dt > 0 else 0.0
    return out


def count_marker(text: str, marker: str) -> int:
    """Count log lines containing ``marker``."""
    return sum(1 for line in text.splitlines() if marker in line)


# -- I/O defaults (replaced by fakes in tests) --------------------------------

def _read_text(path: str) -> str:
    with open(path, "r") as f:
        return f.read()


def _resolve_scope_cgroup(scope: str) -> str:
    """Resolve a ``--user`` scope's cgroup path via systemctl, under /sys/fs/cgroup."""
    res = subprocess.run(
        ["systemctl", "--user", "show", "-p", "ControlGroup", "--value", scope],
        capture_output=True, text=True, check=True)
    return "/sys/fs/cgroup" + res.stdout.strip()


def _read_scope_memory_stat(scope: str) -> str:
    return _read_text(_resolve_scope_cgroup(scope) + "/memory.stat")


def _default_abort(scope: str) -> None:
    subprocess.run(["systemctl", "--user", "stop", scope], check=False)


# -- supervisor ---------------------------------------------------------------

class TrialMonitor:
    def __init__(
        self,
        *,
        scope: str,
        log_path: str,
        out_path: str,
        anon_threshold_bytes: int = DEFAULT_ANON_THRESHOLD_BYTES,
        interval_s: float = DEFAULT_INTERVAL_S,
        gap_marker: str = DEFAULT_GAP_MARKER,
        handshake_marker: str = DEFAULT_HANDSHAKE_MARKER,
        read_memory_stat: Optional[Callable[[], str]] = None,
        read_proc_stat: Optional[Callable[[], str]] = None,
        read_log: Optional[Callable[[], str]] = None,
        write_row: Optional[Callable[[dict], None]] = None,
        abort_fn: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.time,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.scope = scope
        self.log_path = log_path
        self.out_path = out_path
        self.anon_threshold_bytes = anon_threshold_bytes
        self.interval_s = interval_s
        self.gap_marker = gap_marker
        self.handshake_marker = handshake_marker
        self._read_memory_stat = read_memory_stat or (lambda: _read_scope_memory_stat(scope))
        self._read_proc_stat = read_proc_stat or (lambda: _read_text("/proc/stat"))
        self._read_log = read_log or (lambda: _read_text(self.log_path))
        self._write_row = write_row or self._append_row
        self._abort_fn = abort_fn or _default_abort
        self._clock = clock
        self._sleep = sleep_fn
        self.aborted = False

    def _append_row(self, row: dict) -> None:
        with open(self.out_path, "a") as f:
            f.write(json.dumps(row) + "\n")

    def sample(self, prev_cpu: Optional[Dict[str, Tuple[int, int]]]
               ) -> Tuple[dict, Dict[str, Tuple[int, int]]]:
        """Take one sample, append a timestamped row with all four signals, and return
        ``(row, cur_cpu)``. ``cur_cpu`` feeds the next call's per-core delta."""
        anon = parse_anon_bytes(self._read_memory_stat())
        cur_cpu = parse_percpu_jiffies(self._read_proc_stat())
        per_core = percpu_util_pct(prev_cpu, cur_cpu) if prev_cpu else {}
        log_text = self._read_log()
        row = {
            "ts": round(self._clock(), 3),
            "anon_bytes": anon,
            "anon_gib": round(anon / 1024 ** 3, 3),
            "per_core_pct": per_core,
            "gap_count": count_marker(log_text, self.gap_marker),
            "handshake_timeout_count": count_marker(log_text, self.handshake_marker),
        }
        self._write_row(row)
        return row, cur_cpu

    def check_watchdog(self, anon_bytes: int) -> bool:
        """Abort the trial scope iff anon is over the soft threshold. Returns True if it
        aborted (the run loop then exits)."""
        if anon_bytes > self.anon_threshold_bytes:
            self._abort_fn(self.scope)
            self.aborted = True
            logger.critical(
                "trial watchdog ABORT: anon=%.3f GiB > threshold=%.3f GiB; stopped scope %s",
                anon_bytes / 1024 ** 3, self.anon_threshold_bytes / 1024 ** 3, self.scope)
            return True
        return False

    def run(self) -> None:
        """Sample → watchdog → sleep, until an abort (or KeyboardInterrupt)."""
        prev_cpu: Optional[Dict[str, Tuple[int, int]]] = None
        try:
            while not self.aborted:
                row, prev_cpu = self.sample(prev_cpu)
                logger.info("trial sample: anon=%.3f GiB gaps=%d handshake_timeouts=%d "
                            "per_core=%s", row["anon_gib"], row["gap_count"],
                            row["handshake_timeout_count"], row["per_core_pct"])
                if self.check_watchdog(row["anon_bytes"]):
                    break
                self._sleep(self.interval_s)
        except KeyboardInterrupt:
            logger.info("trial monitor stopped (KeyboardInterrupt)")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="ADR-039 §G trial supervisor (monitor + anon "
                                            "watchdog). Samples the trial scope; aborts on "
                                            "an anon threshold. Launches nothing.")
    p.add_argument("--scope", required=True,
                   help="The transient --user scope the trial runs in (cgroup source).")
    p.add_argument("--log", required=True, dest="log_path",
                   help="Capture log path to parse for gap / handshake-timeout rates.")
    p.add_argument("--anon-threshold", type=float, default=DEFAULT_ANON_THRESHOLD_GIB,
                   help="Soft abort threshold in GiB of cgroup anon (default %(default)s).")
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                   help="Sample interval seconds (default %(default)s).")
    p.add_argument("--out", required=True, dest="out_path",
                   help="Sample log path (one JSON row appended per interval).")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    TrialMonitor(
        scope=args.scope, log_path=args.log_path, out_path=args.out_path,
        anon_threshold_bytes=int(args.anon_threshold * 1024 ** 3),
        interval_s=args.interval,
    ).run()


if __name__ == "__main__":
    main()
