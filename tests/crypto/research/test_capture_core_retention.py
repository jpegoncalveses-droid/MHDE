"""Phase 0 — bounded retention + one-shot migration tests.

(c) Rolling retention prunes whole ``date=`` partitions older than the window,
    never today's, and never a non-firehose store.
(e) The one-shot migration compacts surviving days with pre/post row-count parity
    and removes the small parts only after verification.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone

import pyarrow.parquet as pq

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import maintenance
from crypto.research.capture_core import store

_DAY = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _aggtrade_row(symbol="BTCUSDT", *, recv_ns=1, a=10):
    e_ms = _ms(_DAY)
    return {"recv_ts_ns": recv_ns, "e": "aggTrade", "E": e_ms, "a": a, "s": symbol,
            "p": "100.5", "q": "2.0", "f": 1, "l": 2, "T": e_ms, "m": False}


def _read_all(root, dataset):
    files = sorted(pathlib.Path(root, dataset).rglob("*.parquet"))
    rows = []
    for fp in files:
        rows.extend(pq.read_table(str(fp)).to_pylist())
    return files, rows


def _touch_partition(root, dataset, symbol, date):
    d = pathlib.Path(root, dataset, f"symbol={symbol}", f"date={date}")
    d.mkdir(parents=True)
    (d / "part.parquet").write_bytes(b"x")
    return d


# -- shipped retention constant ------------------------------------------------

def test_shipped_retention_window():
    assert cfg.CAPTURE_RAW_RETENTION_DAYS == 14


# -- (c) retention prunes only beyond the window, never today ------------------

def test_expire_firehose_prunes_only_beyond_window_never_today(tmp_path):
    now_ms = _ms(datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc))
    dates = {"old": "2026-05-20", "edge_in": "2026-06-01", "today": "2026-06-14"}
    for ds in ("aggTrade", "depth"):
        for date in dates.values():
            _touch_partition(tmp_path, ds, "BTCUSDT", date)

    removed = maintenance.expire_firehose_partitions(
        str(tmp_path), days=14, now_ms=now_ms)

    assert len(removed) == 2 and all("date=2026-05-20" in p for p in removed)
    for ds in ("aggTrade", "depth"):
        assert not pathlib.Path(tmp_path, ds, "symbol=BTCUSDT",
                                "date=2026-05-20").exists()   # 25d -> pruned
        assert pathlib.Path(tmp_path, ds, "symbol=BTCUSDT",
                            "date=2026-06-01").exists()        # 13d -> kept
        assert pathlib.Path(tmp_path, ds, "symbol=BTCUSDT",
                            "date=2026-06-14").exists()        # today -> kept


def test_expire_firehose_never_touches_non_firehose_datasets(tmp_path):
    now_ms = _ms(datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc))
    old_kln = _touch_partition(tmp_path, "klines_1h", "BTCUSDT", "2026-05-01")
    old_fire = _touch_partition(tmp_path, "aggTrade", "BTCUSDT", "2026-05-01")

    maintenance.expire_firehose_partitions(str(tmp_path), days=14, now_ms=now_ms)

    assert old_kln.exists()           # the long-lived klines store is never pruned
    assert not old_fire.exists()      # the firehose day is pruned


# -- (e) one-shot migration: pre/post row-count parity -------------------------

def test_migrate_compact_preserves_row_counts_and_reduces_files(tmp_path):
    w = store.aggtrade_writer(str(tmp_path), flush_max_bytes=1, flush_interval_s=10 ** 9)
    for sym in ("BTCUSDT", "ETHUSDT"):
        for i in range(8):
            w.append(_aggtrade_row(sym, recv_ns=2000 + i, a=i))
            w.flush_due()
    files_before, _ = _read_all(str(tmp_path), "aggTrade")
    assert len(files_before) == 16                       # 2 symbols * 8 tiny parts

    report = maintenance.migrate_compact(str(tmp_path), datasets=["aggTrade"])

    assert report.rows_before == report.rows_after == 16  # parity gate held
    assert report.partitions_compacted == 2
    assert report.files_after < report.files_before
    assert report.mismatches == []

    files_after, rows_after = _read_all(str(tmp_path), "aggTrade")
    assert len(files_after) == 2                          # one compacted file/partition
    counts = {}
    for r in rows_after:
        counts[r["s"]] = counts.get(r["s"], 0) + 1
    assert counts == {"BTCUSDT": 8, "ETHUSDT": 8}         # every row preserved


def test_migrate_compact_skips_todays_partition(tmp_path):
    # Mirror retention's never-today rule: the one-shot migration must not compact
    # today's actively-written partition (avoids racing the live writer).
    now_ms = _ms(datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc))  # "today" == 05-29
    w = store.aggtrade_writer(str(tmp_path), flush_max_bytes=1, flush_interval_s=10 ** 9)
    for i in range(4):
        w.append(_aggtrade_row("BTCUSDT", recv_ns=1 + i, a=i))  # lands in date=2026-05-29
        w.flush_due()
    files_before, _ = _read_all(str(tmp_path), "aggTrade")

    report = maintenance.migrate_compact(str(tmp_path), datasets=["aggTrade"],
                                         now_ms=now_ms)

    files_after, _ = _read_all(str(tmp_path), "aggTrade")
    assert report.partitions_compacted == 0          # today's partition skipped
    assert len(files_after) == len(files_before)     # untouched on disk


def test_migrate_compact_dry_run_changes_nothing(tmp_path):
    w = store.aggtrade_writer(str(tmp_path), flush_max_bytes=1, flush_interval_s=10 ** 9)
    for i in range(5):
        w.append(_aggtrade_row("BTCUSDT", recv_ns=3000 + i, a=i))
        w.flush_due()
    files_before, _ = _read_all(str(tmp_path), "aggTrade")

    report = maintenance.migrate_compact(str(tmp_path), datasets=["aggTrade"],
                                         dry_run=True)

    files_after, _ = _read_all(str(tmp_path), "aggTrade")
    assert len(files_after) == len(files_before) == 5    # untouched on disk
    assert report.rows_before == 5 and report.partitions_scanned >= 1
