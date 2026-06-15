"""ADR-039 Stage 1 — shard-aware writer + symbol splitter (writer-level only).

No process orchestration / cpuset / snapshot-owner here (that is Stage 2). This pins
the writer-level multi-shard readiness:

  (a) the symbol->shard splitter is deterministic (stable across processes/restarts,
      NOT the salted builtin hash), in range [0, N), and spreads ~evenly over N;
  (b) the writer names files ``part-<shard>-*`` so two shards writing the SAME
      ``symbol=/date=`` partition never collide (and the unsharded default is
      unchanged);
  (c) closed-hour compaction merges the multi-shard ``part-<shard>-*`` of a closed
      hour into ONE ``compact-h*`` with row parity, originals removed only after
      verify, the open hour left untouched — the key correctness check;
  (d) the read contract holds: a mixed multi-shard ``part-*`` + ``compact-*`` tree
      reads back as ONE hive dataset with an intact ``recv_ts_ns`` cursor.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
from datetime import datetime, timezone

import pyarrow.dataset as pads
import pyarrow.parquet as pq

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import maintenance
from crypto.research.capture_core import sharding
from crypto.research.capture_core import store

_DAY = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
_HOUR = 3600


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _row(symbol="BTCUSDT", *, recv_ns=1, a=10, p="100.5"):
    e = _ms(_DAY)
    return {"recv_ts_ns": recv_ns, "e": "aggTrade", "E": e, "a": a, "s": symbol,
            "p": p, "q": "2.0", "f": 1, "l": 2, "T": e, "m": False}


def _agg_part_dir(root):
    return pathlib.Path(root, "aggTrade", "symbol=BTCUSDT", "date=2026-05-29")


def _read_all(root, dataset):
    files = sorted(pathlib.Path(root, dataset).rglob("*.parquet"))
    rows = []
    for fp in files:
        rows.extend(pq.read_table(str(fp)).to_pylist())
    return files, rows


def _make_parts(w, part_dir, n, *, recv_base):
    """Append n rows (size cap 1 => one part file each); return the new file paths."""
    before = {p.name for p in part_dir.glob("part-*.parquet")} if part_dir.exists() else set()
    for i in range(n):
        w.append(_row(recv_ns=recv_base + i, a=recv_base + i))
        w.flush_due()
    return sorted(str(p) for p in part_dir.glob("part-*.parquet") if p.name not in before)


def _set_hour(paths, hour_idx):
    t = hour_idx * _HOUR + 60  # 1 min into the hour
    for p in paths:
        os.utime(p, (t, t))


# -- (a) symbol -> shard splitter ---------------------------------------------

_UNIVERSE = [f"SYM{i}USDT" for i in range(527)]


def test_shard_for_symbol_in_range():
    for n in (1, 2, 3, 4, 5):
        for s in _UNIVERSE:
            assert 0 <= sharding.shard_for_symbol(s, n) < n


def test_shard_for_symbol_single_shard_is_zero():
    assert sharding.shard_for_symbol("BTCUSDT", 1) == 0
    assert sharding.shard_for_symbol("ANYTHING", 1) == 0


def test_shard_for_symbol_is_stable_seed_independent_hash():
    # MUST be a stable CONTENT hash, never the builtin salted hash() (which would
    # re-map every symbol on each process restart and split a partition's history
    # across shards). Pin to blake2b so the seed-independence contract is enforced.
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"):
        for n in (2, 3, 4):
            ref = int.from_bytes(
                hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big") % n
            assert sharding.shard_for_symbol(s, n) == ref


def test_shard_for_symbol_deterministic_on_repeat():
    for s in ("BTCUSDT", "ETHUSDT"):
        first = sharding.shard_for_symbol(s, 3)
        assert all(sharding.shard_for_symbol(s, 3) == first for _ in range(100))


def test_symbols_for_shard_disjoint_and_covers_universe():
    for n in (2, 3, 4):
        buckets = [sharding.symbols_for_shard(_UNIVERSE, i, n) for i in range(n)]
        flat = [s for b in buckets for s in b]
        assert sorted(flat) == sorted(_UNIVERSE)       # full coverage, nothing dropped
        assert len(flat) == len(set(flat))             # disjoint, no symbol in two shards


def test_shard_distribution_roughly_even_over_527():
    for n in (2, 3, 4):
        counts = [0] * n
        for s in _UNIVERSE:
            counts[sharding.shard_for_symbol(s, n)] += 1
        expected = len(_UNIVERSE) / n
        for c in counts:
            assert abs(c - expected) <= 0.25 * expected   # within +/-25% of even


def test_default_n_shards_is_three():
    assert cfg.CAPTURE_N_SHARDS == 3


# -- (b) shard-aware writer: part-<shard>-* naming, no collision ---------------

def test_writer_names_files_with_shard_prefix(tmp_path):
    w = store.aggtrade_writer(str(tmp_path), shard_id=0, flush_max_bytes=1)
    w.append(_row(recv_ns=1)); w.flush_all()
    f = list(_agg_part_dir(tmp_path).glob("*.parquet"))
    assert len(f) == 1 and f[0].name.startswith("part-0-")


def test_two_shards_same_partition_do_not_collide(tmp_path):
    w0 = store.aggtrade_writer(str(tmp_path), shard_id=0, flush_max_bytes=1)
    w1 = store.aggtrade_writer(str(tmp_path), shard_id=1, flush_max_bytes=1)
    w0.append(_row(recv_ns=100)); w0.flush_all()
    w1.append(_row(recv_ns=200)); w1.flush_all()
    pd_ = _agg_part_dir(tmp_path)
    files = list(pd_.glob("part-*.parquet"))
    shard_ids = {p.name.split("-")[1] for p in files}
    assert len(files) == 2 and shard_ids == {"0", "1"}     # both coexist, no clobber
    _, rows = _read_all(str(tmp_path), "aggTrade")
    assert sorted(r["recv_ts_ns"] for r in rows) == [100, 200]


def test_default_writer_unsharded_naming_unchanged(tmp_path):
    w = store.aggtrade_writer(str(tmp_path), flush_max_bytes=1)   # no shard_id
    w.append(_row(recv_ns=1)); w.flush_all()
    f = list(_agg_part_dir(tmp_path).glob("*.parquet"))[0]
    assert f.name.startswith("part-")
    assert not f.name.startswith("part-0-")
    assert not f.name.startswith("part-None-")


# -- (c) closed-hour compaction merges multi-shard part-<shard>-* --------------

def test_closed_hour_compaction_merges_across_shards(tmp_path):
    H_open = 488000
    now = H_open * _HOUR + 120                 # 2 min into the open hour
    H_closed = H_open - 2                       # fully closed (> grace ago)
    pd_ = _agg_part_dir(tmp_path)
    w0 = store.aggtrade_writer(str(tmp_path), shard_id=0, flush_max_bytes=1,
                               flush_interval_s=10 ** 9)
    w1 = store.aggtrade_writer(str(tmp_path), shard_id=1, flush_max_bytes=1,
                               flush_interval_s=10 ** 9)
    p0 = _make_parts(w0, pd_, 3, recv_base=100)             # shard 0: 3 files
    p1 = _make_parts(w1, pd_, 4, recv_base=200)             # shard 1: 4 files
    _set_hour(p0 + p1, H_closed)                            # all in one closed hour
    open0 = _make_parts(w0, pd_, 2, recv_base=900); _set_hour(open0, H_open)

    results = maintenance.compact_partition_closed_hours(str(pd_), now_ts=now)

    assert len(results) == 1                               # one merged file for the hour
    assert results[0].files_before == 7                    # 3 + 4 shards merged together
    assert results[0].rows_before == results[0].rows_after == 7   # row parity
    assert all(not os.path.exists(p) for p in p0 + p1)     # originals dropped after verify
    assert all(os.path.exists(p) for p in open0)           # open hour untouched
    assert len(list(pd_.glob("compact-h*.parquet"))) == 1


# -- (d) read contract: mixed multi-shard part + compacted reads as one hive ---

def test_multishard_mixed_tree_reads_as_one_hive_dataset(tmp_path):
    H_open = 488000
    now = H_open * _HOUR + 120
    H_closed = H_open - 2
    pd_ = _agg_part_dir(tmp_path)
    w0 = store.aggtrade_writer(str(tmp_path), shard_id=0, flush_max_bytes=1,
                               flush_interval_s=10 ** 9)
    w1 = store.aggtrade_writer(str(tmp_path), shard_id=1, flush_max_bytes=1,
                               flush_interval_s=10 ** 9)
    c0 = _make_parts(w0, pd_, 2, recv_base=1000)
    c1 = _make_parts(w1, pd_, 3, recv_base=1100)
    _set_hour(c0 + c1, H_closed)
    maintenance.compact_partition_closed_hours(str(pd_), now_ts=now)   # seal closed hour
    o0 = _make_parts(w0, pd_, 1, recv_base=2000); _set_hour(o0, H_open)
    o1 = _make_parts(w1, pd_, 1, recv_base=2100); _set_hour(o1, H_open)

    assert len(list(pd_.glob("compact-h*.parquet"))) == 1          # 5 merged
    assert len(list(pd_.glob("part-*.parquet"))) == 2              # 2 open (one per shard)

    ds_dir = str(pathlib.Path(tmp_path, "aggTrade"))
    for fp in pathlib.Path(ds_dir).rglob("*.parquet"):            # no baked partition cols
        assert pq.read_schema(str(fp)).names == list(store.AGGTRADE_SCHEMA.names)
    table = pads.dataset(ds_dir, partitioning="hive").to_table()
    assert table.num_rows == 7                                    # 5 sealed + 2 open
    assert set(table.column("symbol").to_pylist()) == {"BTCUSDT"}
    recv = sorted(table.column("recv_ts_ns").to_pylist())        # cursor intact + filterable
    assert recv[-2:] == [2000, 2100]
