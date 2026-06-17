"""Tests for the online level-book maintainer + periodic top-N book-state dataset.

The cursor-only continuity logic is unchanged (see test_capture_core_book.py for
that regression surface); this validates the ADDITIVE level book: absolute-SET
application, qty-0 removal, the bracket + pu-chain protocol applied to LEVELS,
gap -> clear -> re-seed rebuild, top-N extraction, and the depth_state dataset
(schema, validity flag, sharding-safe round-trip).
"""
from __future__ import annotations

import pathlib

import pyarrow.parquet as pq

from crypto.research.capture_core.book import DepthMaintainer
from crypto.research.capture_core import store, config as cfg, service


# -- helpers --

def _bookf(levels: dict) -> dict:
    """Maintainer price_str->qty_str dict as float->float for robust comparison."""
    return {float(p): float(q) for p, q in levels.items()}


_A_BIDS = [["100.0", "5"], ["99.0", "3"], ["98.0", "2"]]
_A_ASKS = [["101.0", "4"], ["102.0", "6"], ["103.0", "1"]]


# -- synthetic protocol proof: seed A -> contiguous diffs -> exact book B --

def test_seed_then_contiguous_diffs_reconstruct_exact_book():
    m = DepthMaintainer("BTCUSDT")
    # Seed from snapshot A at lastUpdateId=100 (full book).
    res = m.on_snapshot(100, 1, bids=_A_BIDS, asks=_A_ASKS)
    assert res.synced_now is False          # nothing to bridge yet
    assert _bookf(m.bids) == {100.0: 5, 99.0: 3, 98.0: 2}
    assert _bookf(m.asks) == {101.0: 4, 102.0: 6, 103.0: 1}

    # Diff 1 brackets 100 (U=99 <= 101 <= u=105). Absolute SET + qty-0 removal.
    r1 = m.on_diff(99, 105, 98, 2,
                   bids=[["100.0", "7"], ["97.0", "9"]],     # set 100->7, add 97
                   asks=[["101.0", "0"], ["104.0", "8"]])    # remove 101, add 104
    assert r1.synced_now is True
    # Diff 2 chains (pu == prev u = 105).
    m.on_diff(106, 110, 105, 3,
              bids=[["99.0", "0"]],                          # remove 99
              asks=[["102.0", "10"]])                        # set 102->10

    assert _bookf(m.bids) == {100.0: 7, 98.0: 2, 97.0: 9}    # 99 removed, 100 set, 97 added
    assert _bookf(m.asks) == {102.0: 10, 103.0: 1, 104.0: 8}  # 101 removed, 102 set, 104 added
    assert m.synced is True and m.last_u == 110


def test_qty_zero_removes_level_absolute_set_replaces():
    m = DepthMaintainer("BTCUSDT")
    m.on_snapshot(10, 1, bids=[["5.0", "1"]], asks=[["6.0", "1"]])
    m.on_diff(10, 11, 9, 2, bids=[["5.0", "9"]], asks=[])     # SET 5 -> 9 (replace, not add)
    assert _bookf(m.bids) == {5.0: 9.0}                        # replaced, not 1+9
    m.on_diff(12, 13, 11, 3, bids=[["5.0", "0"]], asks=[])    # qty 0 removes
    assert 5.0 not in _bookf(m.bids) and m.bids == {}


def test_gap_clears_book_then_fresh_snapshot_rebuilds():
    m = DepthMaintainer("BTCUSDT")
    m.on_snapshot(100, 1, bids=_A_BIDS, asks=_A_ASKS)
    m.on_diff(99, 105, 98, 2, bids=[["100.0", "7"]], asks=[])
    assert m.synced and _bookf(m.bids)[100.0] == 7

    # continuity break (pu=200 != last_u=105) -> resync, book must be cleared (stale).
    res = m.on_diff(201, 210, 200, 3, bids=[["100.0", "999"]], asks=[])
    assert res.needs_snapshot is True and m.synced is False
    assert m.bids == {} and m.asks == {}                      # stale book discarded, not corrupted

    # fresh snapshot rebuilds the level book from scratch.
    m.on_snapshot(210, 4, bids=[["50.0", "1"]], asks=[["51.0", "1"]])
    m.on_diff(211, 212, 210, 5, bids=[["50.0", "2"]], asks=[])
    assert m.synced is True and _bookf(m.bids) == {50.0: 2.0}


def test_cursor_only_when_no_levels_passed_keeps_book_empty():
    # ADDITIVE: feeding ids without levels (the legacy call shape) tracks the
    # cursor exactly as before and never builds a book.
    m = DepthMaintainer("BTCUSDT")
    m.on_diff(11, 20, 10, 2)
    m.on_snapshot(15, 3)
    assert m.synced is True and m.last_u == 20
    assert m.bids == {} and m.asks == {}


def test_top_levels_sorted_and_capped():
    m = DepthMaintainer("BTCUSDT")
    bids = [[str(p), "1"] for p in (100, 95, 98, 90, 99)]
    asks = [[str(p), "1"] for p in (110, 105, 108, 120, 106)]
    m.on_snapshot(1, 1, bids=bids, asks=asks)
    tb, ta = m.top_levels(3)
    assert [float(p) for p, _ in tb] == [100.0, 99.0, 98.0]   # bids desc
    assert [float(p) for p, _ in ta] == [105.0, 106.0, 108.0]  # asks asc


# -- well-formedness invariant on any maintained book --

def _well_formed(m):
    bids = sorted((float(p), float(q)) for p, q in m.bids.items())
    asks = sorted((float(p), float(q)) for p, q in m.asks.items())
    assert all(q > 0 for _, q in bids) and all(q > 0 for _, q in asks)   # no zero/neg qty
    if bids and asks:
        assert max(p for p, _ in bids) < min(p for p, _ in asks)          # best bid < best ask


def test_maintained_book_stays_well_formed():
    m = DepthMaintainer("BTCUSDT")
    m.on_snapshot(100, 1, bids=_A_BIDS, asks=_A_ASKS)
    m.on_diff(99, 105, 98, 2, bids=[["100.5", "1"]], asks=[["100.7", "0"], ["104.0", "2"]])
    _well_formed(m)


# -- depth_state dataset: schema, validity, top-N, sharding-safe round-trip --

def test_book_state_row_and_schema_round_trip(tmp_path):
    m = DepthMaintainer("我踏马来了USDT")   # CJK symbol
    m.on_snapshot(100, 1, bids=_A_BIDS, asks=_A_ASKS)
    m.on_diff(99, 105, 98, 2, bids=[], asks=[])
    row = service.book_state_row("我踏马来了USDT", m, recv_ns=123456789, top_n=2)
    assert set(row) == {"recv_ts_ns", "s", "update_id", "valid", "b", "a"}
    assert row["valid"] is True and row["update_id"] == 105
    assert len(row["b"]) == 2 and len(row["a"]) == 2

    w = store.depth_state_writer(str(tmp_path))
    w.append(row)
    w.flush_all()
    files = list(pathlib.Path(tmp_path, cfg.DEPTH_STATE_DATASET).rglob("*.parquet"))
    assert files
    assert list(store.DEPTH_STATE_SCHEMA.names) == ["recv_ts_ns", "s", "update_id", "valid", "b", "a"]
    (got,) = pq.ParquetFile(str(files[0])).read().to_pylist()
    assert got["s"] == "我踏马来了USDT" and got["valid"] is True
    assert got["b"][0] == ["100.0", "5"]   # lossless venue strings, top bid first


def test_depth_state_writer_sharding_safe_part_naming(tmp_path):
    w = store.depth_state_writer(str(tmp_path), shard_id=2)
    m = DepthMaintainer("BTCUSDT")
    m.on_snapshot(1, 1, bids=[["1.0", "1"]], asks=[["2.0", "1"]])
    w.append(service.book_state_row("BTCUSDT", m, recv_ns=1, top_n=20))
    w.flush_all()
    files = list(pathlib.Path(tmp_path, cfg.DEPTH_STATE_DATASET).rglob("*.parquet"))
    assert files and files[0].name.startswith("part-2-")   # shard-prefixed, no cross-shard clobber


# -- service flush-loop integration: periodic write, synced-only, cadence-gated --

def _read_state(root) -> list:
    rows = []
    for fp in pathlib.Path(root, cfg.DEPTH_STATE_DATASET).rglob("*.parquet"):
        rows.extend(pq.ParquetFile(str(fp)).read().to_pylist())
    return rows


def _diff(symbol, U, u, pu, *, b=None, a=None):
    return {"e": "depthUpdate", "E": u, "T": u, "s": symbol, "U": U, "u": u, "pu": pu,
            "b": b or [], "a": a or []}


def test_service_periodic_write_synced_only_and_cadence_gated(tmp_path):
    s = service.CaptureService(root=str(tmp_path), client=None)
    # seed + sync BTCUSDT's level book
    s._on_snapshot_arrived("BTCUSDT", {"lastUpdateId": 100, "bids": _A_BIDS, "asks": _A_ASKS}, recv_ns=1)
    s._handle_depth(_diff("BTCUSDT", 99, 105, 98, b=[["100.0", "7"]]), recv_ns=2)
    # an UNSYNCED symbol (diff, no snapshot ever)
    s._handle_depth(_diff("ETHUSDT", 5, 6, 4), recv_ns=3)
    assert s._maintainers["BTCUSDT"].synced and not s._maintainers["ETHUSDT"].synced

    s._maybe_write_book_states()
    s._depth_state.flush_all()
    rows = _read_state(tmp_path)
    assert {r["s"] for r in rows} == {"BTCUSDT"}   # only the synced (valid) book is emitted
    assert rows[0]["valid"] is True and rows[0]["update_id"] == 105

    # cadence gate: an immediate second sample (< DEPTH_STATE_CADENCE_S) does not double-write
    s._maybe_write_book_states()
    s._depth_state.flush_all()
    assert len(_read_state(tmp_path)) == len(rows)


def test_on_snapshot_while_synced_uniform_reseed_stays_consistent():
    # A late/duplicate seed onto an already-synced maintainer must drop to awaiting
    # and rebuild — book / last_u / synced stay mutually consistent (no stale cursor
    # on a freshly-rebuilt book, which would emit a divergent valid=True state).
    m = DepthMaintainer("BTCUSDT")
    m.on_snapshot(100, 1, bids=_A_BIDS, asks=_A_ASKS)
    m.on_diff(99, 105, 98, 2, bids=[["100.0", "7"]], asks=[])
    assert m.synced and m.last_u == 105

    m.on_snapshot(200, 3, bids=[["50.0", "1"]], asks=[["51.0", "1"]])   # re-seed while synced
    assert m.synced is False and m.last_u is None                       # dropped to awaiting
    assert _bookf(m.bids) == {50.0: 1.0}                                 # rebuilt from new snapshot
    r = m.on_diff(200, 205, 199, 4, bids=[["50.0", "9"]], asks=[])       # re-syncs cleanly
    assert r.synced_now is True and m.last_u == 205 and _bookf(m.bids) == {50.0: 9.0}


def test_malformed_level_leaves_book_untouched_atomic():
    # A bad price/qty validates-before-mutate: the book is unchanged (no partial
    # apply) and the cursor does not advance, so the next diff re-seeds cleanly.
    m = DepthMaintainer("BTCUSDT")
    m.on_snapshot(10, 1, bids=[["5.0", "1"]], asks=[["6.0", "1"]])
    m.on_diff(10, 11, 9, 2, bids=[["5.0", "2"]], asks=[])
    before_b, before_a, before_u = dict(m.bids), dict(m.asks), m.last_u
    import pytest
    with pytest.raises(ValueError):
        m.on_diff(12, 13, 11, 3, bids=[["5.0", "2"], ["1.2.3", "9"]], asks=[])  # malformed price
    assert m.bids == before_b and m.asks == before_a and m.last_u == before_u   # atomic, untouched


def test_service_periodic_write_is_best_effort_per_symbol(tmp_path):
    # One symbol's corrupt book must NOT take down the flush loop or drop the others.
    s = service.CaptureService(root=str(tmp_path), client=None)
    s._on_snapshot_arrived("BTCUSDT", {"lastUpdateId": 100, "bids": _A_BIDS, "asks": _A_ASKS}, recv_ns=1)
    s._handle_depth(_diff("BTCUSDT", 99, 105, 98, b=[["100.0", "7"]]), recv_ns=2)
    s._on_snapshot_arrived("ETHUSDT", {"lastUpdateId": 50, "bids": [["10.0", "1"]], "asks": [["11.0", "1"]]}, recv_ns=3)
    s._handle_depth(_diff("ETHUSDT", 50, 51, 49, b=[["10.0", "2"]]), recv_ns=4)
    s._maintainers["ETHUSDT"].bids["corrupt-price"] = "1"   # inject a key that float() can't parse

    s._maybe_write_book_states()                            # must NOT raise (best-effort)
    s._depth_state.flush_all()
    syms = {r["s"] for r in _read_state(tmp_path)}
    assert "BTCUSDT" in syms                                # the good symbol is still emitted


def test_depth_state_retention_prunes_old_partitions(tmp_path):
    from datetime import datetime, timezone
    from crypto.research.capture_core import maintenance as mt
    root = str(tmp_path)
    m = DepthMaintainer("BTCUSDT")
    m.on_snapshot(1, 1, bids=[["1.0", "1"]], asks=[["2.0", "1"]])
    m.on_diff(1, 2, 0, 1, bids=[["1.0", "3"]], asks=[])     # sync
    old = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000) * 1_000_000
    new = int(datetime(2026, 6, 17, tzinfo=timezone.utc).timestamp() * 1000) * 1_000_000
    w = store.depth_state_writer(root)
    w.append(service.book_state_row("BTCUSDT", m, recv_ns=old, top_n=20))
    w.append(service.book_state_row("BTCUSDT", m, recv_ns=new, top_n=20))
    w.flush_all()

    now_ms = int(datetime(2026, 6, 18, tzinfo=timezone.utc).timestamp() * 1000)
    mt.expire_depth_state_partitions(root, days=2, now_ms=now_ms)   # cutoff 2026-06-16
    dates = {p.name for p in pathlib.Path(root, cfg.DEPTH_STATE_DATASET).glob("symbol=*/date=*")}
    assert "date=2026-06-17" in dates and "date=2026-06-01" not in dates


def test_service_raw_depth_persist_path_unchanged(tmp_path):
    # ADDITIVE guarantee: the new level-book/state work does not alter raw diff
    # persistence — every diff still lands in the depth dataset verbatim.
    s = service.CaptureService(root=str(tmp_path), client=None)
    s._handle_depth(_diff("BTCUSDT", 1, 2, 0, b=[["1.0", "2"]]), recv_ns=1)
    s._handle_depth(_diff("BTCUSDT", 3, 4, 2, a=[["9.0", "1"]]), recv_ns=2)
    s._depth.flush_all()
    raw = []
    for fp in pathlib.Path(tmp_path, "depth").rglob("*.parquet"):
        raw.extend(pq.ParquetFile(str(fp)).read().to_pylist())
    assert len(raw) == 2 and {r["u"] for r in raw} == {2, 4}
