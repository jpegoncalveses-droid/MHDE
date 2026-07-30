"""OKX Stage C — seqId/prevSeqId order-book maintainer (in-band snapshots, no REST seed).

OKX `books` self-seeds: the first push is action:snapshot (prevSeqId == -1) with the full ladder;
subsequent action:update pushes carry seqId/prevSeqId and apply level SETs (sz=0 removes) under
prevSeqId == prior-seqId continuity. A continuity break (prevSeqId mismatch, or seqId < prevSeqId
from a maintenance reset) discards the book and goes unsynced (never applies across a gap). The
maintainer stores raw contract-size venue strings; ctVal->coin conversion is the normalizer's job.
"""
from __future__ import annotations

import pytest

from crypto.research.capture_core_okx.book_okx import OkxBookMaintainer


def _mk():
    m = OkxBookMaintainer("BTCUSDT")
    # snapshot: bids desc-ish, asks asc-ish; levels are [px_str, sz_contracts_str]
    m.on_snapshot(100, bids=[["100.0", "3"], ["99.0", "5"]], asks=[["101.0", "2"], ["102.0", "4"]])
    return m


def test_snapshot_seeds_and_syncs():
    m = _mk()
    assert m.synced is True
    assert m.last_seq_id == 100
    b, a = m.top_levels(10)
    assert b == [["100.0", "3"], ["99.0", "5"]]        # best (highest) bid first
    assert a == [["101.0", "2"], ["102.0", "4"]]       # best (lowest) ask first


def test_update_applies_sets_and_advances_seq():
    m = _mk()
    m.on_update(101, 100, bids=[["100.0", "7"]], asks=[["101.0", "1"]])   # SET existing levels
    assert m.synced is True and m.last_seq_id == 101
    b, a = m.top_levels(10)
    assert b[0] == ["100.0", "7"] and a[0] == ["101.0", "1"]


def test_update_zero_size_removes_level():
    m = _mk()
    m.on_update(101, 100, bids=[["99.0", "0"]], asks=[])    # sz=0 removes the 99.0 bid
    b, _ = m.top_levels(10)
    assert [lvl for lvl in b if lvl[0] == "99.0"] == []


def test_prevseq_mismatch_triggers_resync():
    m = _mk()
    m.on_update(105, 104, bids=[["100.0", "9"]], asks=[])   # prevSeqId 104 != last 100 -> gap
    assert m.synced is False and m.last_seq_id is None
    assert m.top_levels(10) == ([], [])                     # book discarded


def test_seqid_below_prevseq_is_maintenance_reset_resync():
    m = _mk()
    m.on_update(3, 100, bids=[["100.0", "9"]], asks=[])     # seqId 3 < prevSeqId 100 -> reset
    assert m.synced is False


def test_heartbeat_prevseq_equals_seq_is_noop():
    m = _mk()
    m.on_update(100, 100, bids=[], asks=[])                 # seqId==prevSeqId==last -> heartbeat
    assert m.synced is True and m.last_seq_id == 100
    b, a = m.top_levels(10)
    assert b == [["100.0", "3"], ["99.0", "5"]]             # unchanged


def test_update_before_snapshot_is_ignored():
    m = OkxBookMaintainer("BTCUSDT")
    m.on_update(101, 100, bids=[["1", "1"]], asks=[])       # never snapshotted
    assert m.synced is False and m.top_levels(10) == ([], [])


def test_on_update_bad_size_leaves_book_unchanged_and_synced():
    # atomic-on-failure: a non-numeric size must NOT partially mutate the book and must NOT
    # advance the seq — else a book mixing old+partial state would be sampled as valid depth_state.
    m = _mk()
    before = m.top_levels(10)
    with pytest.raises(ValueError):
        m.on_update(101, 100, bids=[["100.0", "7"], ["98.0", "notanumber"]], asks=[])
    assert m.top_levels(10) == before        # no partial apply (100.0 was NOT set to 7)
    assert m.last_seq_id == 100 and m.synced is True


def test_on_update_bad_price_leaves_book_unchanged():
    m = _mk()
    before = m.top_levels(10)
    with pytest.raises(ValueError):
        m.on_update(101, 100, bids=[["notaprice", "5"]], asks=[])
    assert m.top_levels(10) == before        # bad price never entered the book
    assert m.last_seq_id == 100


def test_on_snapshot_bad_level_does_not_seed_a_corrupt_book():
    m = OkxBookMaintainer("BTCUSDT")
    with pytest.raises(ValueError):
        m.on_snapshot(1, bids=[["100.0", "3"], ["99.0", "x"]], asks=[])
    assert m.synced is False and m.top_levels(10) == ([], [])   # nothing seeded


def test_top_levels_limited_and_best_first():
    m = OkxBookMaintainer("BTCUSDT")
    m.on_snapshot(1,
                  bids=[[str(p), "1"] for p in (95, 99, 97, 100, 96)],   # unordered
                  asks=[[str(p), "1"] for p in (105, 101, 103, 102)])
    b, a = m.top_levels(2)
    assert [lvl[0] for lvl in b] == ["100", "99"]           # top-2 highest bids
    assert [lvl[0] for lvl in a] == ["101", "102"]          # top-2 lowest asks
