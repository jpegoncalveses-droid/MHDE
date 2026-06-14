"""Phase 0 — writer compaction tests.

(a) The firehose writer must produce a BOUNDED file count under a sustained
    multi-symbol burst (hourly roll-up), not one part-file per flush.
(b) The compaction primitive must round-trip every row with fields intact and a
    still-monotonic ``recv_ts_ns`` cursor (the read contract the brain Phase 1
    reader is built against).
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone

import pyarrow.parquet as pq

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import maintenance
from crypto.research.capture_core import service as svc
from crypto.research.capture_core import store

_DAY = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _aggtrade_row(symbol="BTCUSDT", *, recv_ns=1, a=10, p="100.5", q="2.0"):
    e_ms = _ms(_DAY)
    return {"recv_ts_ns": recv_ns, "e": "aggTrade", "E": e_ms, "a": a, "s": symbol,
            "p": p, "q": q, "f": 1, "l": 2, "T": e_ms, "m": False}


def _read_all(root, dataset):
    files = sorted(pathlib.Path(root, dataset).rglob("*.parquet"))
    rows = []
    for fp in files:
        rows.extend(pq.read_table(str(fp)).to_pylist())
    return files, rows


# -- (a) bounded file count ----------------------------------------------------

def test_firehose_writers_use_hourly_rollup_interval(tmp_path):
    # The service must construct EVERY firehose writer with the hourly roll-up
    # window, not the old 30s age cadence that exploded the inode table.
    s = svc.CaptureService(root=str(tmp_path), client=None, enable_snapshots=False,
                           install_signals=False, disk_guard_enabled=False)
    assert cfg.CAPTURE_FIREHOSE_ROLLUP_S >= 3600.0
    for w in (s._agg, s._depth, s._bookticker, s._forceorder, s._markprice,
              s._snapshot):
        assert w._flush_interval_s == cfg.CAPTURE_FIREHOSE_ROLLUP_S


def test_writer_bounded_files_over_multi_symbol_burst(tmp_path):
    # 50 symbols trickle steadily for 3 simulated hours, polled every 30s (the old
    # cadence). Under the hourly roll-up each symbol's partition must emit only a
    # few files (~one per elapsed hour), not one-per-poll (~360).
    clock = [0.0]
    w = store.aggtrade_writer(str(tmp_path),
                              flush_interval_s=cfg.CAPTURE_FIREHOSE_ROLLUP_S,
                              flush_max_bytes=10 ** 12, now_fn=lambda: clock[0])
    symbols = [f"SYM{i}USDT" for i in range(50)]
    for _ in range(360):                       # 3h at a 30s poll
        for sym in symbols:
            w.append(_aggtrade_row(sym))
        clock[0] += 30.0
        w.flush_due()
    w.flush_all()

    files, _ = _read_all(str(tmp_path), "aggTrade")
    files_per_symbol = len(files) / len(symbols)
    assert files_per_symbol <= 5               # ~3-4 under hourly; ~360 under 30s


# -- (b) compaction round-trip preserves the read contract ---------------------

def test_compact_partition_preserves_rows_fields_and_recv_cursor(tmp_path):
    # 20 tiny size-capped parts in one symbol=/date= partition.
    w = store.aggtrade_writer(str(tmp_path), flush_max_bytes=1, flush_interval_s=10 ** 9)
    for i in range(20):
        w.append(_aggtrade_row("BTCUSDT", recv_ns=1000 + i, a=i, p=f"{100 + i}.0"))
        w.flush_due()
    part_dir = pathlib.Path(tmp_path, "aggTrade", "symbol=BTCUSDT", "date=2026-05-29")
    assert len(list(part_dir.glob("*.parquet"))) == 20

    res = maintenance.compact_partition(str(part_dir))
    assert res.rows_before == 20 and res.rows_after == 20
    assert res.files_before == 20 and res.files_after == 1

    files, rows = _read_all(str(tmp_path), "aggTrade")
    assert len(files) == 1                                  # collapsed to one file
    assert len(rows) == 20                                  # every row preserved
    assert {r["p"] for r in rows} == {f"{100 + i}.0" for i in range(20)}  # str-lossless
    recv = [r["recv_ts_ns"] for r in rows]
    assert recv == sorted(recv)                             # monotonic cursor preserved
    after = [r for r in rows if r["recv_ts_ns"] > 1009]     # incremental cursor read
    assert len(after) == 10 and min(r["recv_ts_ns"] for r in after) == 1010


def test_compact_partition_single_file_is_noop(tmp_path):
    w = store.aggtrade_writer(str(tmp_path))
    w.append(_aggtrade_row("BTCUSDT"))
    w.flush_all()
    part_dir = pathlib.Path(tmp_path, "aggTrade", "symbol=BTCUSDT", "date=2026-05-29")
    res = maintenance.compact_partition(str(part_dir))
    assert res.files_before == 1 and res.files_after == 1
    assert res.rows_before == res.rows_after == 1
