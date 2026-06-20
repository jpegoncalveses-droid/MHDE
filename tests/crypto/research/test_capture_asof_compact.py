"""Daily whole-partition seal-yesterday compaction for the REST as-of series, plus
adding klines_1h to the closed-hour compactor's coverage.

The 7 REST present-state series (open_interest, premium_index, the LS ratios, basis) and
klines_1h are NEVER date-pruned by the brain reader (every date partition is read every
tick), and they are LOW-RATE, so the closed-hour (flush-mtime-hour) compactor only buys
~1.5-2x. The big win is the WHOLE-PARTITION primitive (compact_partition / migrate_compact)
collapsing a SEALED (symbol,date) to ~1 file (~40x). This module covers:

  * a new SUBPROCESS-BOUNDED whole-partition chunked driver (migrate_compact_chunked,
    mirroring the closed-hour compact_firehose_chunked) + its seal-yesterday entrypoint
    (compact_asof_yesterday) over CAPTURE_ASOF_DATASETS, and
  * klines_1h joining the closed-hour compactor's coverage
    (CAPTURE_CLOSED_HOUR_COMPACT_DATASETS) — klines keeps its own 90d retention, so it is
    added to the COMPACTION coverage, NOT to FIREHOSE_PRUNABLE_DATASETS (the 7d expire).

No new merge logic: reuses the hardened compact_partition / _merge_files as-is.
"""
from __future__ import annotations

import os
import pathlib
from datetime import datetime, timezone
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import maintenance
from crypto.research.capture_core import rest_series

# 2026-06-20 01:30 UTC: a "now" whose YESTERDAY is 2026-06-19, used by the seal-yesterday
# entrypoint (the daily timer fires post-01:00 once the as-of date= has sealed).
_NOW_MS = int(datetime(2026, 6, 20, 1, 30, tzinfo=timezone.utc).timestamp() * 1000)


def _write_parts(part_dir, n, *, recv_base=1000, mtime=None):
    """n single-row part-*.parquet files in one partition dir (recv_ts_ns + a payload col).
    The compactor reads each file by its PHYSICAL schema, so the schema is irrelevant."""
    part_dir = pathlib.Path(part_dir)
    part_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        t = pa.table({"recv_ts_ns": [recv_base + i], "v": [f"x{i}"]})
        p = part_dir / f"part-{uuid4().hex}.parquet"
        pq.write_table(t, str(p))
        if mtime is not None:
            os.utime(p, (mtime, mtime))
    return part_dir


def _asof_partition(root, dataset, symbol, date, n=4, mtime=None):
    return _write_parts(pathlib.Path(root, dataset, f"symbol={symbol}", f"date={date}"),
                        n, mtime=mtime)


def _migrated(root, dataset):
    return sorted(pathlib.Path(root, dataset).rglob("compact-migrated*.parquet"))


def _parts(root, dataset):
    return sorted(pathlib.Path(root, dataset).rglob("part-*.parquet"))


def _parts_in(root, dataset, symbol, date):
    return sorted(pathlib.Path(root, dataset, f"symbol={symbol}",
                               f"date={date}").glob("part-*.parquet"))


def _inprocess_runner(calls):
    """Runs _migrate_chunk IN-PROCESS (fast) and records (remaining-paths, budget) per chunk."""
    def _run(root, paths, budget, now_ms):
        calls.append((len(paths), budget))
        return maintenance._migrate_chunk(root, list(paths), budget, now_ms)
    return _run


# -- config coverage constants -------------------------------------------------

def test_asof_datasets_mirror_rest_series():
    assert cfg.CAPTURE_ASOF_DATASETS == tuple(s.name for s in rest_series.SERIES)


def test_closed_hour_coverage_adds_klines_but_keeps_it_off_firehose_expire():
    cov = cfg.CAPTURE_CLOSED_HOUR_COMPACT_DATASETS
    assert "klines_1h" in cov
    assert set(cfg.FIREHOSE_PRUNABLE_DATASETS).issubset(set(cov))
    # klines must NOT be firehose-prunable, or the 7d firehose expire would shorten its 90d.
    assert "klines_1h" not in cfg.FIREHOSE_PRUNABLE_DATASETS


# -- the whole-partition chunk unit --------------------------------------------

def test_migrate_chunk_collapses_whole_partition_to_one_file(tmp_path):
    pd = _asof_partition(tmp_path, "premium_index", "BTCUSDT", "2026-06-19", n=4)
    res = maintenance._migrate_chunk(str(tmp_path), [str(pd)], 10, _NOW_MS)
    assert res["compacted"] == 1
    assert len(_migrated(tmp_path, "premium_index")) == 1
    assert _parts(tmp_path, "premium_index") == []          # originals removed after parity


def test_migrate_chunk_stops_at_budget(tmp_path):
    for k in range(5):
        _asof_partition(tmp_path, "premium_index", f"SYM{k:02d}USDT", "2026-06-19", n=3)
    paths = sorted(str(p) for p in
                   pathlib.Path(tmp_path, "premium_index").glob("symbol=*/date=*"))
    res = maintenance._migrate_chunk(str(tmp_path), paths, 2, _NOW_MS)
    assert res["compacted"] == 2 and res["completed"] == 2  # stopped at the merge budget


# -- the chunked driver: date-scoped, never-today, bounded per chunk -----------

def test_chunked_collapses_target_date_and_skips_today(tmp_path):
    _asof_partition(tmp_path, "premium_index", "BTCUSDT", "2026-06-19", n=4)
    _asof_partition(tmp_path, "premium_index", "ETHUSDT", "2026-06-19", n=4)
    _asof_partition(tmp_path, "premium_index", "BTCUSDT", "2026-06-20", n=4)   # today
    rep = maintenance.migrate_compact_chunked(
        str(tmp_path), datasets=["premium_index"], dates={"2026-06-19"},
        merges_per_chunk=10, now_ms=_NOW_MS, chunk_runner=_inprocess_runner([]))
    assert rep.partitions_compacted == 2
    assert len(_migrated(tmp_path, "premium_index")) == 2
    # today's partition is never touched
    assert len(_parts_in(tmp_path, "premium_index", "BTCUSDT", "2026-06-20")) == 4


def test_chunked_runs_in_bounded_chunks(tmp_path):
    for k in range(5):
        _asof_partition(tmp_path, "premium_index", f"SYM{k:02d}USDT", "2026-06-19", n=3)
    calls = []
    rep = maintenance.migrate_compact_chunked(
        str(tmp_path), datasets=["premium_index"], dates={"2026-06-19"},
        merges_per_chunk=2, now_ms=_NOW_MS, chunk_runner=_inprocess_runner(calls))
    assert rep.partitions_compacted == 5
    assert len(calls) == 3                                  # 5 merges / budget 2 -> 3 chunks


def test_chunked_idempotent_rerun_is_noop(tmp_path):
    _asof_partition(tmp_path, "premium_index", "BTCUSDT", "2026-06-19", n=4)
    kw = dict(datasets=["premium_index"], dates={"2026-06-19"}, merges_per_chunk=10,
              now_ms=_NOW_MS, chunk_runner=_inprocess_runner([]))
    maintenance.migrate_compact_chunked(str(tmp_path), **kw)
    rep2 = maintenance.migrate_compact_chunked(str(tmp_path), **kw)
    assert rep2.partitions_compacted == 0                  # 1-file partition is a no-op
    assert len(_migrated(tmp_path, "premium_index")) == 1


# -- the seal-yesterday entrypoint over the 7 as-of series ---------------------

def test_compact_asof_yesterday_collapses_yesterday_across_series_skips_today(tmp_path):
    _asof_partition(tmp_path, "premium_index", "BTCUSDT", "2026-06-19", n=4)
    _asof_partition(tmp_path, "basis", "BTCUSDT", "2026-06-19", n=4)
    _asof_partition(tmp_path, "premium_index", "BTCUSDT", "2026-06-20", n=4)   # today
    _asof_partition(tmp_path, "aggTrade", "BTCUSDT", "2026-06-19", n=4)        # NOT an as-of series
    rep = maintenance.compact_asof_yesterday(
        str(tmp_path), now_ms=_NOW_MS, chunk_runner=_inprocess_runner([]))
    assert rep.partitions_compacted == 2                   # premium_index + basis, yesterday only
    assert len(_migrated(tmp_path, "premium_index")) == 1
    assert len(_migrated(tmp_path, "basis")) == 1
    assert _parts(tmp_path, "aggTrade")                    # non-as-of dataset untouched
    assert len(_parts_in(tmp_path, "premium_index", "BTCUSDT", "2026-06-20")) == 4  # today untouched


# -- the real subprocess isolation path ----------------------------------------

def test_real_subprocess_migrate_worker_compacts(tmp_path):
    _asof_partition(tmp_path, "premium_index", "BTCUSDT", "2026-06-19", n=4)
    _asof_partition(tmp_path, "premium_index", "ETHUSDT", "2026-06-19", n=4)
    rep = maintenance.migrate_compact_chunked(           # default runner = real subprocess
        str(tmp_path), datasets=["premium_index"], dates={"2026-06-19"},
        merges_per_chunk=10, now_ms=_NOW_MS)
    assert rep.partitions_compacted == 2
    assert len(_migrated(tmp_path, "premium_index")) == 2


# -- klines_1h joins the closed-hour compactor's coverage ----------------------

def test_closed_hour_compacts_klines_under_new_coverage(tmp_path):
    HOUR = 1_000_000                                        # an arbitrary closed clock-hour
    now_ts = (HOUR + 1) * 3600 + 10_000                    # well past the hour + grace
    _write_parts(pathlib.Path(tmp_path, "klines_1h", "symbol=BTCUSDT", "date=2026-06-19"),
                 3, mtime=HOUR * 3600 + 1)
    rep = maintenance.compact_firehose_closed_hours(
        str(tmp_path), datasets=cfg.CAPTURE_CLOSED_HOUR_COMPACT_DATASETS, now_ts=now_ts)
    assert rep.hours_compacted == 1
    assert list(pathlib.Path(tmp_path, "klines_1h").rglob("compact-h*.parquet"))
