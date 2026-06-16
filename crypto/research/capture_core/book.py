"""Per-symbol depth sequence maintenance for capture-core (cursor only).

This is **book MAINTENANCE, not a live order book**: it tracks only the diff
update-id cursor needed to (a) detect sequence gaps and (b) decide when a fresh
REST snapshot is required to resync. It never stores raw diffs (the service
stores every raw diff unconditionally) and never reconstructs bid/ask levels —
that is the offline replay tool's job, seeded from the separately-stored
``depth_snapshot`` dataset.

Binance USDT-M procedure implemented here:
  * Buffer diffs until a REST snapshot (``lastUpdateId``) applies.
  * Drop any diff with ``u < lastUpdateId``.
  * The first applied event must satisfy ``U <= lastUpdateId+1 <= u``.
  * Once synced, each event's ``pu`` must equal the previous event's ``u``
    (the futures continuity rule). A mismatch — or a first-event boundary
    failure (a hole between snapshot and the buffered diffs) — requests a fresh
    snapshot. The gap (start = last-good ts, end = resume ts) is emitted when
    capture actually resumes, so it bounds the true outage.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import NamedTuple, Optional

from crypto.research.capture_core import config as cfg


class _Diff(NamedTuple):
    U: int    # first update id in event
    u: int    # final update id in event
    pu: int   # previous final update id (futures continuity field)
    ts: int   # local receive timestamp (ns)


@dataclass
class SyncResult:
    """Outcome of feeding one event/snapshot to a :class:`DepthMaintainer`.

    ``synced_now`` reflects the maintainer's state AFTER this call (i.e. it is
    now synced) — not merely that a sync momentarily occurred. If a buffered
    event broke continuity again right after syncing, the maintainer re-enters
    resync and ``synced_now`` is False.
    """
    synced_now: bool = False                       # synced after this call
    needs_snapshot: bool = False                   # caller should request a REST snapshot
    gap: Optional[tuple[int, int, str]] = None     # (start_ts, end_ts, reason)


class DepthMaintainer:
    """Maintain the depth-diff cursor for one symbol; detect gaps + resync."""

    def __init__(self, symbol: str, *, buffer_maxlen: Optional[int] = None,
                 reseed_threshold: Optional[int] = None) -> None:
        self.symbol = symbol
        self.synced = False
        self.last_u: Optional[int] = None
        self.last_update_id: Optional[int] = None
        # BOUNDED: while AWAITING a snapshot the maintainer buffers diffs; an unsynced
        # symbol otherwise grows this forever (the firehose leak). maxlen drops the
        # OLDEST diff once full — only diffs near the lastUpdateId boundary can sync, so
        # stale ones are pure waste.
        self._maxlen = (buffer_maxlen if buffer_maxlen is not None
                        else cfg.CAPTURE_DEPTH_BUFFER_MAXLEN)
        self._reseed_threshold = (reseed_threshold if reseed_threshold is not None
                                  else cfg.CAPTURE_UNSYNCED_RESEED_THRESHOLD)
        self._buffer: "deque[_Diff]" = deque(maxlen=self._maxlen)
        self._last_good_ts: Optional[int] = None
        self._pending_gap_start: Optional[int] = None

    def on_diff(self, U: int, u: int, pu: int, ts: int) -> SyncResult:
        """Feed one raw depth-diff event's update ids."""
        if self.synced:
            if pu != self.last_u:
                return self._enter_resync(U, u, pu, ts)
            self.last_u = u
            self._last_good_ts = ts
            return SyncResult()
        # awaiting a snapshot: buffer (bounded; oldest evicted at maxlen), then try to
        # sync if a snapshot is in hand.
        self._buffer.append(_Diff(U, u, pu, ts))
        if self.last_update_id is None:
            # No snapshot has ever landed, so the synced/boundary paths that raise
            # needs_snapshot are unreachable — a stuck-unsynced book would otherwise never
            # ask for a (re)seed. Once it has buffered past the threshold, re-request one
            # (the scheduler dedups). Closes the never-synced re-request hole.
            if len(self._buffer) >= self._reseed_threshold:
                return SyncResult(needs_snapshot=True)
            return SyncResult()
        return self._try_sync()

    def on_snapshot(self, last_update_id: int, ts: int) -> SyncResult:
        """Feed a REST snapshot's ``lastUpdateId``; attempt to (re)sync."""
        self.last_update_id = last_update_id
        return self._try_sync()

    # -- internals --

    def _enter_resync(self, U: int, u: int, pu: int, ts: int) -> SyncResult:
        """A continuity break while synced: drop to awaiting, remember the gap."""
        if self._pending_gap_start is None:
            self._pending_gap_start = self._last_good_ts
        self.synced = False
        self.last_u = None
        self.last_update_id = None          # the prior snapshot is stale now
        self._buffer = deque([_Diff(U, u, pu, ts)], maxlen=self._maxlen)
        return SyncResult(needs_snapshot=True)

    def _try_sync(self) -> SyncResult:
        lid = self.last_update_id
        if lid is None:
            return SyncResult()
        # drop stale (u < lastUpdateId). Work on a list for the index/slice logic, then
        # store back a BOUNDED deque so the cap survives every sync attempt.
        buf = [d for d in self._buffer if d.u >= lid]
        idx = next((i for i, d in enumerate(buf)
                    if d.U <= lid + 1 <= d.u), None)
        if idx is None:
            self._buffer = deque(buf, maxlen=self._maxlen)
            if buf and buf[0].U > lid + 1:
                # hole between snapshot and earliest buffered diff -> re-snapshot
                return SyncResult(needs_snapshot=True)
            return SyncResult()             # not enough diffs yet; keep waiting
        sync_ev = buf[idx]
        self.synced = True
        self.last_u = sync_ev.u
        self._last_good_ts = sync_ev.ts
        gap = None
        if self._pending_gap_start is not None:
            gap = (self._pending_gap_start, sync_ev.ts, "sequence_gap")
            self._pending_gap_start = None
        rest = buf[idx + 1:]
        self._buffer = deque(maxlen=self._maxlen)
        for d in rest:
            if d.pu != self.last_u:
                follow = self._enter_resync(d.U, d.u, d.pu, d.ts)
                # We synced then immediately broke again -> NOT synced now. The
                # first gap (if any) is still reported; a new resync is pending.
                return SyncResult(synced_now=False, gap=gap,
                                  needs_snapshot=follow.needs_snapshot)
            self.last_u = d.u
            self._last_good_ts = d.ts
        return SyncResult(synced_now=True, gap=gap)
