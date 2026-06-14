"""ADR-038 write-then-compact tests.

(a) the writer holds at most ~one flush interval in RAM under a multi-symbol burst
    (the in-RAM hourly roll-up is retired);
(b) closed-hour compaction merges only CLOSED hours, one file per hour, per-hour row
    parity, originals removed only after verify;
(c) BOUNDARY SAFETY — the compactor never touches the open hour or a just-closed hour
    within the grace margin; late-arriving data lands in the open hour and is never
    folded into an already-sealed hour;
(d) read contract — a mix of small ``part-*`` and compacted ``compact-*`` files reads
    back as ONE hive dataset with an intact ``recv_ts_ns`` cursor;
(e) retention prunes only beyond-7d ``date=`` partitions, never today's.
"""
from __future__ import annotations

import os
import pathlib
from datetime import datetime, timezone

import pyarrow.dataset as pads
import pyarrow.parquet as pq

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import maintenance
from crypto.research.capture_core import service as svc
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


# -- (a) writer holds <= one flush interval in RAM -----------------------------

def test_rollup_constant_is_retired():
    assert not hasattr(cfg, "CAPTURE_FIREHOSE_ROLLUP_S")  # the OOM mechanism is gone
    assert cfg.CAPTURE_FIREHOSE_FLUSH_S <= 60.0           # short flush, not hourly


def test_firehose_writers_use_short_flush_interval(tmp_path):
    s = svc.CaptureService(root=str(tmp_path), client=None, enable_snapshots=False,
                           install_signals=False, disk_guard_enabled=False,
                           inode_guard_enabled=False)
    for w in (s._agg, s._depth, s._bookticker, s._forceorder, s._markprice, s._snapshot):
        assert w._flush_interval_s == cfg.CAPTURE_FIREHOSE_FLUSH_S


def test_writer_holds_at_most_one_flush_interval(tmp_path):
    clock = [0.0]
    interval = cfg.CAPTURE_FIREHOSE_FLUSH_S
    w = store.aggtrade_writer(str(tmp_path), flush_interval_s=interval,
                              flush_max_bytes=10 ** 12, now_fn=lambda: clock[0])
    symbols = [f"S{i}USDT" for i in range(20)]
    per_interval = len(symbols)
    max_buffered = 0
    for _ in range(12):                       # 12 flush intervals of steady inflow
        for sym in symbols:
            w.append({**_row(sym), "s": sym})
        clock[0] += interval
        w.flush_due()
        buffered = sum(len(b.rows) for b in w._buffers.values())
        max_buffered = max(max_buffered, buffered)
    # RAM never accumulates more than ~one interval of inflow (vs an hour under roll-up)
    assert max_buffered <= per_interval


# -- (b) closed-hour compaction: one file per closed hour, per-hour parity -----

def test_closed_hour_compaction_merges_each_closed_hour(tmp_path):
    now = 488000 * _HOUR + 120                # 2 min into the open hour 488000
    h1, h2 = 488000 - 3, 488000 - 2           # two fully-closed hours
    w = store.aggtrade_writer(str(tmp_path), flush_max_bytes=1, flush_interval_s=10 ** 9)
    pd_ = _agg_part_dir(tmp_path)
    p1 = _make_parts(w, pd_, 4, recv_base=100); _set_hour(p1, h1)
    p2 = _make_parts(w, pd_, 5, recv_base=200); _set_hour(p2, h2)

    results = maintenance.compact_partition_closed_hours(str(pd_), now_ts=now)

    assert len(results) == 2                                  # one merged file per hour
    assert sorted(r.files_before for r in results) == [4, 5]
    assert all(r.rows_before == r.rows_after for r in results)   # per-hour parity
    assert all(r.files_after == 1 for r in results)
    # originals gone, replaced by two compact-* files
    assert all(not os.path.exists(p) for p in p1 + p2)
    compacted = sorted(pd_.glob("compact-*.parquet"))
    assert len(compacted) == 2
    _, rows = _read_all(str(tmp_path), "aggTrade")
    assert len(rows) == 9                                     # 4 + 5 rows preserved


# -- (c) BOUNDARY SAFETY -------------------------------------------------------

def test_compactor_skips_open_and_grace_hours(tmp_path):
    grace = cfg.CAPTURE_COMPACTION_GRACE_S
    H_open = 488000
    now = H_open * _HOUR + 120                # 2 min into open hour (< grace from H-1 end)
    H_grace = H_open - 1                      # just closed, still within grace
    H_closed = H_open - 2                     # fully closed (> grace ago)
    w = store.aggtrade_writer(str(tmp_path), flush_max_bytes=1, flush_interval_s=10 ** 9)
    pd_ = _agg_part_dir(tmp_path)
    closed = _make_parts(w, pd_, 3, recv_base=10); _set_hour(closed, H_closed)
    grace_p = _make_parts(w, pd_, 3, recv_base=20); _set_hour(grace_p, H_grace)
    open_p = _make_parts(w, pd_, 3, recv_base=30); _set_hour(open_p, H_open)

    results = maintenance.compact_partition_closed_hours(str(pd_), now_ts=now,
                                                         grace_s=grace)

    assert len(results) == 1 and results[0].files_before == 3   # only the closed hour
    assert all(not os.path.exists(p) for p in closed)           # closed merged away
    assert all(os.path.exists(p) for p in grace_p)              # grace hour UNTOUCHED
    assert all(os.path.exists(p) for p in open_p)               # open hour UNTOUCHED


def test_late_data_lands_in_open_hour_not_sealed_hour(tmp_path):
    # A closed hour is sealed; then a late part-* arrives in the open hour. It must
    # NOT be folded into the sealed hour and must NOT be lost.
    H_open = 488000
    now = H_open * _HOUR + 120
    H_closed = H_open - 2
    w = store.aggtrade_writer(str(tmp_path), flush_max_bytes=1, flush_interval_s=10 ** 9)
    pd_ = _agg_part_dir(tmp_path)
    closed = _make_parts(w, pd_, 3, recv_base=10); _set_hour(closed, H_closed)
    maintenance.compact_partition_closed_hours(str(pd_), now_ts=now)
    sealed = sorted(pd_.glob("compact-*.parquet"))
    assert len(sealed) == 1
    sealed_mtime_rows = pq.read_metadata(str(sealed[0])).num_rows

    late = _make_parts(w, pd_, 1, recv_base=999); _set_hour(late, H_open)  # open hour
    maintenance.compact_partition_closed_hours(str(pd_), now_ts=now)       # run again

    assert os.path.exists(late[0])                       # late datum preserved (open)
    assert pq.read_metadata(str(sealed[0])).num_rows == sealed_mtime_rows  # sealed unchanged
    _, rows = _read_all(str(tmp_path), "aggTrade")
    assert len(rows) == 4                                # 3 sealed + 1 late, none lost


# -- (d) read contract: small + compacted mix reads as one hive dataset --------

def test_part_and_compact_mix_reads_as_hive_with_cursor(tmp_path):
    H_open = 488000
    now = H_open * _HOUR + 120
    H_closed = H_open - 2
    w = store.aggtrade_writer(str(tmp_path), flush_max_bytes=1, flush_interval_s=10 ** 9)
    pd_ = _agg_part_dir(tmp_path)
    closed = _make_parts(w, pd_, 4, recv_base=1000); _set_hour(closed, H_closed)
    maintenance.compact_partition_closed_hours(str(pd_), now_ts=now)
    open_p = _make_parts(w, pd_, 3, recv_base=2000); _set_hour(open_p, H_open)
    # partition now holds 1 compact-* (closed) + 3 part-* (open)
    assert len(list(pd_.glob("compact-*.parquet"))) == 1
    assert len(list(pd_.glob("part-*.parquet"))) == 3

    ds_dir = str(pathlib.Path(tmp_path, "aggTrade"))
    for fp in pathlib.Path(ds_dir).rglob("*.parquet"):       # no baked partition cols
        assert pq.read_schema(str(fp)).names == list(store.AGGTRADE_SCHEMA.names)
    table = pads.dataset(ds_dir, partitioning="hive").to_table()
    assert table.num_rows == 7                               # 4 + 3, all readable
    assert set(table.column("symbol").to_pylist()) == {"BTCUSDT"}
    recv = sorted(table.column("recv_ts_ns").to_pylist())    # cursor intact + filterable
    assert [r for r in recv if r > 1003] == [2000, 2001, 2002]


# -- (e) retention at 7 days ---------------------------------------------------

def test_shipped_retention_is_seven_days():
    assert cfg.CAPTURE_RAW_RETENTION_DAYS == 7


def test_expire_prunes_only_beyond_7d_never_today(tmp_path):
    now_ms = _ms(datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc))
    for date in ("2026-06-01", "2026-06-08", "2026-06-14"):  # 13d, 6d, today
        d = pathlib.Path(tmp_path, "aggTrade", "symbol=BTCUSDT", f"date={date}")
        d.mkdir(parents=True)
        (d / "part.parquet").write_bytes(b"x")

    removed = maintenance.expire_firehose_partitions(str(tmp_path), now_ms=now_ms)

    assert any("2026-06-01" in p for p in removed)            # 13d > 7d -> pruned
    assert not pathlib.Path(tmp_path, "aggTrade", "symbol=BTCUSDT",
                            "date=2026-06-01").exists()
    assert pathlib.Path(tmp_path, "aggTrade", "symbol=BTCUSDT",
                        "date=2026-06-08").exists()           # 6d < 7d -> kept
    assert pathlib.Path(tmp_path, "aggTrade", "symbol=BTCUSDT",
                        "date=2026-06-14").exists()           # today -> kept
