"""Per-symbol depth maintenance for capture-core: update-id cursor + (optional)
online level book.

The cursor logic tracks the diff update-id sequence to (a) detect gaps and (b)
decide when a fresh REST snapshot is required to resync — it stores no raw diffs
(the service stores every raw diff unconditionally). This file ADDITIONALLY
maintains an online bid/ask level book when fed the diff/snapshot level arrays:
diffs are applied as absolute SETs (qty 0 removes the level), the book is seeded
from a REST snapshot, cleared on any gap, and rebuilt from the next snapshot.
Feeding ids WITHOUT level arrays (the legacy call shape) is pure cursor
maintenance and never builds a book — behaviour identical to before.

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
    bids: Optional[list] = None   # bid level deltas [[price_str, qty_str], ...] (None = cursor-only)
    asks: Optional[list] = None   # ask level deltas


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
    """Maintain the depth-diff cursor (and optional level book) for one symbol."""

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
        # ADDITIVE online level book. Empty + untouched unless fed level arrays.
        # price_str -> qty_str, kept lossless (cast only for the qty-0 test + sorting).
        self.bids: dict[str, str] = {}
        self.asks: dict[str, str] = {}

    def on_diff(self, U: int, u: int, pu: int, ts: int,
                bids: Optional[list] = None, asks: Optional[list] = None) -> SyncResult:
        """Feed one raw depth-diff event's update ids (and optionally its levels)."""
        if self.synced:
            if pu != self.last_u:
                return self._enter_resync(U, u, pu, ts, bids, asks)
            self._apply_levels(bids, asks)
            self.last_u = u
            self._last_good_ts = ts
            return SyncResult()
        # awaiting a snapshot: buffer (bounded; oldest evicted at maxlen), then try to
        # sync if a snapshot is in hand.
        self._buffer.append(_Diff(U, u, pu, ts, bids, asks))
        if self.last_update_id is None:
            # No snapshot has ever landed, so the synced/boundary paths that raise
            # needs_snapshot are unreachable — a stuck-unsynced book would otherwise never
            # ask for a (re)seed. Once it has buffered past the threshold, re-request one
            # (the scheduler dedups). Closes the never-synced re-request hole.
            if len(self._buffer) >= self._reseed_threshold:
                return SyncResult(needs_snapshot=True)
            return SyncResult()
        return self._try_sync()

    def on_snapshot(self, last_update_id: int, ts: int,
                    bids: Optional[list] = None, asks: Optional[list] = None) -> SyncResult:
        """Feed a REST snapshot's ``lastUpdateId`` (and optionally its full book).

        UNIFORM (re)seed: always drop to awaiting and rebuild the book from THIS
        snapshot, so ``book`` / ``last_u`` / ``synced`` stay mutually consistent
        whether or not we were already synced — a late or duplicate seed must not
        leave a stale cursor on a freshly-rebuilt book. ``_try_sync`` then
        re-establishes sync via the normal bracket + pu-chain path.
        """
        self.synced = False
        self.last_u = None
        self.last_update_id = last_update_id
        if bids is not None or asks is not None:
            self._rebuild_from_snapshot(bids or [], asks or [])
        else:
            self.bids = {}
            self.asks = {}
        return self._try_sync()

    def top_levels(self, n: int) -> tuple[list, list]:
        """Top-``n`` bids (desc by price) and asks (asc by price), as
        ``[[price_str, qty_str], ...]`` — the lossless venue strings."""
        bids = sorted(self.bids.items(), key=lambda kv: float(kv[0]), reverse=True)[:n]
        asks = sorted(self.asks.items(), key=lambda kv: float(kv[0]))[:n]
        return [[p, q] for p, q in bids], [[p, q] for p, q in asks]

    # -- internals --

    @staticmethod
    def _validate(deltas: Optional[list]) -> list:
        """Parse + validate every level (price AND qty must parse as float) and
        return ``[(price_str, qty_str, is_zero), ...]``. Raises on a malformed
        level — callers validate BEFORE mutating, so a bad level leaves the book
        untouched (no partial apply) and a non-numeric price can never reach
        ``top_levels``' float sort. Prices/qtys are stored as the lossless venue
        strings; the float parse is for the zero-test + key validation only."""
        out = []
        for level in (deltas or []):
            price_s, qty_s = level[0], level[1]
            float(price_s)                       # validate the key
            out.append((price_s, qty_s, float(qty_s) == 0.0))
        return out

    @staticmethod
    def _set_side(book: dict, validated: list) -> None:
        for price_s, qty_s, is_zero in validated:
            if is_zero:
                book.pop(price_s, None)          # qty 0 removes the level
            else:
                book[price_s] = qty_s            # absolute SET (replace)

    def _apply_levels(self, bids: Optional[list], asks: Optional[list]) -> None:
        """Apply diff level deltas as absolute SETs; qty 0 removes the price level.
        Atomic-on-failure: both sides are validated before either is mutated."""
        vb = self._validate(bids)
        va = self._validate(asks)
        self._set_side(self.bids, vb)
        self._set_side(self.asks, va)

    def _rebuild_from_snapshot(self, bids: list, asks: list) -> None:
        """Replace the level book wholesale from a REST snapshot's full ladders."""
        vb = self._validate(bids)
        va = self._validate(asks)
        self.bids = {p: q for p, q, is_zero in vb if not is_zero}
        self.asks = {p: q for p, q, is_zero in va if not is_zero}

    def _enter_resync(self, U: int, u: int, pu: int, ts: int,
                      bids: Optional[list] = None, asks: Optional[list] = None) -> SyncResult:
        """A continuity break while synced: drop to awaiting, remember the gap,
        discard the now-stale level book (rebuilt on the next snapshot)."""
        if self._pending_gap_start is None:
            self._pending_gap_start = self._last_good_ts
        self.synced = False
        self.last_u = None
        self.last_update_id = None          # the prior snapshot is stale now
        self.bids = {}                      # stale book discarded; never apply across the gap
        self.asks = {}
        self._buffer = deque([_Diff(U, u, pu, ts, bids, asks)], maxlen=self._maxlen)
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
        self._apply_levels(sync_ev.bids, sync_ev.asks)   # bracket event's levels (SET; idempotent overlap)
        gap = None
        if self._pending_gap_start is not None:
            gap = (self._pending_gap_start, sync_ev.ts, "sequence_gap")
            self._pending_gap_start = None
        rest = buf[idx + 1:]
        self._buffer = deque(maxlen=self._maxlen)
        for d in rest:
            if d.pu != self.last_u:
                follow = self._enter_resync(d.U, d.u, d.pu, d.ts, d.bids, d.asks)
                # We synced then immediately broke again -> NOT synced now. The
                # first gap (if any) is still reported; a new resync is pending.
                return SyncResult(synced_now=False, gap=gap,
                                  needs_snapshot=follow.needs_snapshot)
            self._apply_levels(d.bids, d.asks)
            self.last_u = d.u
            self._last_good_ts = d.ts
        return SyncResult(synced_now=True, gap=gap)
