"""Free-space disk guard for the capture-core FIREHOSE datasets (PR-3 safety).

Two tiers protect the volume without starving the small, long-lived stores:

  * **SOFT floor** — prune the OLDEST firehose date-partitions first, across the
    firehose datasets, until free space is back above the floor.
  * **CRITICAL floor** — HALT firehose writes and emit a CRITICAL log; resume once
    free recovers above the SOFT floor (hysteresis). Data dropped during a halt is
    acceptable: the substrate is forward-only (skip, never backfill).

Only the firehose datasets (:data:`config.FIREHOSE_PRUNABLE_DATASETS`) are ever
scanned or pruned — ``klines_1h``, the REST present-state series, and the ``_gaps``
manifest (tiny / longer-lived / audit) are never touched, because they are simply
not in that list. The threshold + selection helpers are pure and unit-tested; the
:class:`DiskGuard` wires them to ``statvfs`` + ``rmtree`` and is invoked from the
firehose flush loop. NEVER opens the production DB.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Callable, Sequence

from crypto.research.capture_core import config as cfg

logger = logging.getLogger("mhde.crypto.capture_core.disk_guard")

_GIB = 1024 ** 3


# -- pure threshold helpers --

def disk_state(free: int, *, soft: int, critical: int) -> str:
    """Classify free space: ``"critical"`` < critical <= ``"soft"`` < soft <= ``"ok"``."""
    if free < critical:
        return "critical"
    if free < soft:
        return "soft"
    return "ok"


def next_halt_state(free: int, *, soft: int, critical: int, halted: bool) -> bool:
    """Hysteresis for the firehose write halt: halt below CRITICAL, resume at/above
    SOFT, hold the prior state in the band between (so it does not flap)."""
    if free < critical:
        return True
    if free >= soft:
        return False
    return halted


# -- firehose partition enumeration + oldest-first selection --

@dataclass(frozen=True)
class Partition:
    path: str
    date: str
    size: int


def _dir_size(path: str) -> int:
    total = 0
    for entry in os.scandir(path):
        if entry.is_file(follow_symlinks=False):
            total += entry.stat(follow_symlinks=False).st_size
        elif entry.is_dir(follow_symlinks=False):
            total += _dir_size(entry.path)
    return total


def list_firehose_partitions(root: str, datasets: Sequence[str]) -> list[Partition]:
    """All ``<root>/<dataset>/symbol=*/date=*`` partitions of the given firehose
    datasets, with on-disk size. Only those datasets are scanned, so non-firehose
    stores can never be selected for pruning."""
    parts: list[Partition] = []
    for ds in datasets:
        ds_dir = os.path.join(root, ds)
        if not os.path.isdir(ds_dir):
            continue
        for sym in os.scandir(ds_dir):
            if not (sym.is_dir() and sym.name.startswith("symbol=")):
                continue
            for date_entry in os.scandir(sym.path):
                if not (date_entry.is_dir() and date_entry.name.startswith("date=")):
                    continue
                parts.append(Partition(
                    path=date_entry.path,
                    date=date_entry.name.split("date=", 1)[1],
                    size=_dir_size(date_entry.path),
                ))
    return parts


def select_oldest_to_reclaim(parts: Sequence[Partition], deficit: int) -> list[Partition]:
    """Oldest-first selection whose cumulative size covers ``deficit`` bytes.

    Pure. Sorts by ``(date, path)`` ascending so the oldest exchange-day partitions
    go first, uniformly across datasets. Returns everything available if the deficit
    exceeds the total; empty when ``deficit <= 0``.
    """
    if deficit <= 0:
        return []
    chosen: list[Partition] = []
    freed = 0
    for p in sorted(parts, key=lambda x: (x.date, x.path)):
        chosen.append(p)
        freed += p.size
        if freed >= deficit:
            break
    return chosen


def prune_paths(paths: Sequence[str]) -> int:
    """``rmtree`` each path; return bytes reclaimed (summed before removal)."""
    reclaimed = 0
    for p in paths:
        try:
            reclaimed += _dir_size(p)
            shutil.rmtree(p)
        except FileNotFoundError:
            continue
    return reclaimed


def free_bytes(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


# -- the guard --

@dataclass
class DiskGuardResult:
    state: str
    free_after: int
    pruned: list[str] = field(default_factory=list)
    halted: bool = False


class DiskGuard:
    """Stateful two-tier guard. :meth:`enforce` is called from the firehose flush
    loop; :attr:`halted` is read on the write path to drop incoming data during a
    CRITICAL halt (forward-only)."""

    def __init__(
        self,
        root: str,
        *,
        datasets: Sequence[str] = cfg.FIREHOSE_PRUNABLE_DATASETS,
        soft_floor: int = cfg.CAPTURE_DISK_SOFT_FLOOR_BYTES,
        critical_floor: int = cfg.CAPTURE_DISK_CRITICAL_FLOOR_BYTES,
        free_fn: Callable[[str], int] = free_bytes,
        prune_fn: Callable[[Sequence[str]], int] = prune_paths,
        list_fn: Callable[[str, Sequence[str]], list[Partition]] = list_firehose_partitions,
        log: logging.Logger = logger,
    ) -> None:
        self._root = root
        self._datasets = tuple(datasets)
        self._soft = soft_floor
        self._critical = critical_floor
        self._free_fn = free_fn
        self._prune_fn = prune_fn
        self._list_fn = list_fn
        self._log = log
        self.halted = False

    def enforce(self) -> DiskGuardResult:
        free = self._free_fn(self._root)
        pruned: list[str] = []
        if free < self._soft:
            deficit = self._soft - free
            victims = select_oldest_to_reclaim(
                self._list_fn(self._root, self._datasets), deficit)
            if victims:
                reclaimed = self._prune_fn([v.path for v in victims])
                pruned = [v.path for v in victims]
                free += reclaimed
        prev = self.halted
        self.halted = next_halt_state(free, soft=self._soft,
                                      critical=self._critical, halted=prev)
        self._log_transition(prev, free, pruned)
        return DiskGuardResult(
            state=disk_state(free, soft=self._soft, critical=self._critical),
            free_after=free, pruned=pruned, halted=self.halted)

    def _log_transition(self, prev_halted: bool, free: int, pruned: list[str]) -> None:
        if self.halted and not prev_halted:
            self._log.critical(
                "capture disk guard: free %.1fGiB < critical %.1fGiB — HALTING "
                "firehose writes (forward-only: dropping until recovery)",
                free / _GIB, self._critical / _GIB)
        elif prev_halted and not self.halted:
            self._log.warning(
                "capture disk guard: free recovered to %.1fGiB (>= soft %.1fGiB) — "
                "RESUMING firehose writes", free / _GIB, self._soft / _GIB)
        elif pruned:
            self._log.warning(
                "capture disk guard: free below soft %.1fGiB — pruned %d oldest "
                "firehose partitions, free now %.1fGiB",
                self._soft / _GIB, len(pruned), free / _GIB)


# -- inode guard (Phase 0) ----------------------------------------------------
# The byte guard above cannot see the failure mode that took the box down on
# 2026-06-09: millions of tiny files exhaust the root-filesystem INODE table while
# bytes-free stays healthy. This guard tracks inode usage and makes capture fail
# itself first — WARN (Telegram) at the warn fraction, CRITICAL + HALT writes at
# the critical fraction, hysteresis so it does not flap. Pure helpers are unit
# tested; the class wires them to ``statvfs`` + an injected notifier. Like the byte
# guard, this NEVER opens DuckDB; the default notifier reaches Telegram through the
# DB-free :func:`monitoring.alert.send_text` text path only.


def inode_used(total: int, avail: int) -> float:
    """Inode usage fraction from statvfs counts. ``avail`` mirrors the byte guard's
    use of the non-root-available count. Empty/!inode filesystems (total <= 0) are
    reported as 0.0 (not "full") so the guard never false-halts on them."""
    if total <= 0:
        return 0.0
    return (total - avail) / total


def inode_used_fraction(path: str) -> float:
    """Fraction of the inode table USED on ``path``'s filesystem (the root fs for the
    capture store). ``statvfs`` is cheap; called on the guard cadence."""
    st = os.statvfs(path)
    return inode_used(st.f_files, st.f_favail)


def inode_state(used: float, *, warn: float, critical: float) -> str:
    """Classify inode usage: ``"critical"`` >= critical > ``"warn"`` >= warn > ``"ok"``."""
    if used >= critical:
        return "critical"
    if used >= warn:
        return "warn"
    return "ok"


def next_inode_halt_state(used: float, *, warn: float, critical: float,
                          halted: bool) -> bool:
    """Hysteresis for the inode write-halt: halt at/above CRITICAL, resume below WARN,
    hold the prior state in the band between (so it does not flap)."""
    if used >= critical:
        return True
    if used < warn:
        return False
    return halted


def _default_inode_notify(text: str) -> None:
    """Best-effort Telegram via the DB-free monitoring text path. Lazily imported so
    constructing the guard never pulls in the alerting/fx stack (or DuckDB), and any
    failure degrades to a log line — an alert must never crash the capture loop."""
    try:
        from monitoring.alert import send_text
        send_text(text)
    except Exception as exc:  # pragma: no cover - notifier must never raise
        logger.error("capture inode guard: Telegram notify failed: %s", exc)


@dataclass
class InodeGuardResult:
    state: str
    used: float
    halted: bool


class InodeGuard:
    """Stateful root-inode guard. :meth:`enforce` runs on the firehose flush-loop
    cadence; :attr:`halted` is read on the write path to drop incoming data during a
    CRITICAL halt (forward-only). Alerts fire only on threshold TRANSITIONS."""

    def __init__(
        self,
        root: str,
        *,
        warn_fraction: float = cfg.CAPTURE_INODE_WARN_FRACTION,
        critical_fraction: float = cfg.CAPTURE_INODE_CRITICAL_FRACTION,
        used_fn: Callable[[str], float] = inode_used_fraction,
        notify_fn: Callable[[str], None] = _default_inode_notify,
        log: logging.Logger = logger,
    ) -> None:
        self._root = root
        self._warn = warn_fraction
        self._critical = critical_fraction
        self._used_fn = used_fn
        self._notify_fn = notify_fn
        self._log = log
        self.halted = False
        self._state = "ok"

    def enforce(self) -> InodeGuardResult:
        used = self._used_fn(self._root)
        state = inode_state(used, warn=self._warn, critical=self._critical)
        self.halted = next_inode_halt_state(
            used, warn=self._warn, critical=self._critical, halted=self.halted)
        prev = self._state
        if state != prev:
            self._on_transition(prev, state, used)
        self._state = state
        return InodeGuardResult(state=state, used=used, halted=self.halted)

    def _on_transition(self, prev: str, state: str, used: float) -> None:
        pct = used * 100.0
        if state == "critical":
            msg = (f"🛑 capture inode guard: root inodes {pct:.1f}% used "
                   f"(>= {self._critical * 100:.0f}%) — HALTING firehose writes "
                   f"(forward-only).")
            self._log.critical(msg)
            self._notify_fn(msg)
        elif state == "warn":
            msg = (f"⚠️ capture inode guard: root inodes {pct:.1f}% used "
                   f"(>= {self._warn * 100:.0f}%) — approaching the cap; "
                   f"retention/compaction should reclaim.")
            self._log.warning(msg)
            self._notify_fn(msg)
        elif state == "ok" and prev != "ok":
            msg = (f"✅ capture inode guard: root inodes recovered to {pct:.1f}% used "
                   f"(< {self._warn * 100:.0f}%) — RESUMING firehose writes.")
            self._log.warning(msg)
            self._notify_fn(msg)
