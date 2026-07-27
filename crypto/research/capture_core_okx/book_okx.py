"""OKX `books` order-book maintainer — in-band snapshots, no REST seed.

The OKX analog of ``capture_core/book.py`` (Binance's REST-seeded diff maintainer), but simpler:
OKX's `books` channel self-seeds with an ``action:snapshot`` (``prevSeqId == -1``) carrying the
full ladder, then ``action:update`` diffs apply level SETs (size ``0`` removes) under the
continuity rule ``prevSeqId == prior seqId``. A break — ``prevSeqId`` mismatch, or ``seqId <
prevSeqId`` from a maintenance reset — discards the book and goes unsynced; the client re-seeds by
re-subscribing (no REST fetch). A heartbeat (``seqId == prevSeqId``, ~60s idle) is a no-op.

Levels are stored as raw ``[px_str, size_contracts_str]`` venue strings; the contracts->coin ctVal
conversion is applied later in the normalizer, so this class stays venue-value-pure.
"""
from __future__ import annotations

from typing import Optional


class OkxBookMaintainer:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.synced = False
        self.last_seq_id: Optional[int] = None
        self._bids: dict[str, str] = {}     # px_str -> size_contracts_str
        self._asks: dict[str, str] = {}

    def on_snapshot(self, seq_id: int, bids: list, asks: list) -> None:
        """Wholesale rebuild from an ``action:snapshot`` (``prevSeqId == -1``)."""
        self._bids = {px: sz for px, sz in bids}
        self._asks = {px: sz for px, sz in asks}
        self.last_seq_id = seq_id
        self.synced = True

    def on_update(self, seq_id: int, prev_seq_id: int, bids: list, asks: list) -> None:
        """Apply an ``action:update`` diff under ``prevSeqId == prior seqId`` continuity."""
        if not self.synced:
            return                                  # never applied before a snapshot
        if prev_seq_id != self.last_seq_id or seq_id < prev_seq_id:
            self._resync()                          # continuity break / maintenance reset
            return
        if seq_id == prev_seq_id:
            return                                  # heartbeat: no book change
        self._apply(self._bids, bids)
        self._apply(self._asks, asks)
        self.last_seq_id = seq_id

    @staticmethod
    def _apply(side: dict, levels: list) -> None:
        for px, sz in levels:
            if float(sz) == 0.0:
                side.pop(px, None)                  # size 0 removes the level
            else:
                side[px] = sz

    def reset(self) -> None:
        """Drop the book to unsynced — called on a socket gap (never apply across a break)."""
        self._resync()

    def _resync(self) -> None:
        self.synced = False
        self.last_seq_id = None
        self._bids = {}
        self._asks = {}

    def top_levels(self, n: int) -> tuple[list, list]:
        """Top-``n`` levels each side, best-first: bids high->low, asks low->high (lossless)."""
        bids = sorted(self._bids.items(), key=lambda kv: float(kv[0]), reverse=True)[:n]
        asks = sorted(self._asks.items(), key=lambda kv: float(kv[0]))[:n]
        return [[px, sz] for px, sz in bids], [[px, sz] for px, sz in asks]
