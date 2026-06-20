"""CLI wiring for the as-of/klines compaction:
  * ``capture-firehose-compact-recent`` (the hourly closed-hour timer entrypoint) now
    covers ``klines_1h`` (CAPTURE_CLOSED_HOUR_COMPACT_DATASETS), not just the WS firehose.
  * ``capture-asof-compact`` (the new daily seal-yesterday entrypoint) collapses the
    REST as-of partitions of a sealed date to ~1 file each.
"""
from __future__ import annotations

import os
import pathlib
import time
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from main import crypto


def _write_parts(part_dir, n, *, mtime=None):
    part_dir = pathlib.Path(part_dir)
    part_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        t = pa.table({"recv_ts_ns": [1000 + i], "v": [f"x{i}"]})
        p = part_dir / f"part-{uuid4().hex}.parquet"
        pq.write_table(t, str(p))
        if mtime is not None:
            os.utime(p, (mtime, mtime))


def test_recent_compact_cli_covers_klines(tmp_path):
    # part files flushed ~2h ago -> their clock-hour is closed past the grace under REAL now.
    two_h_ago = int(time.time()) - 7200
    _write_parts(pathlib.Path(tmp_path, "klines_1h", "symbol=BTCUSDT", "date=2026-06-19"),
                 3, mtime=two_h_ago)
    res = CliRunner().invoke(crypto, ["capture-firehose-compact-recent",
                                      "--root", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert list(pathlib.Path(tmp_path, "klines_1h").rglob("compact-h*.parquet"))


def test_asof_compact_cli_seals_a_given_date(tmp_path):
    _write_parts(pathlib.Path(tmp_path, "premium_index", "symbol=BTCUSDT", "date=2026-06-19"), 4)
    res = CliRunner().invoke(crypto, ["capture-asof-compact", "--root", str(tmp_path),
                                      "--date", "2026-06-19"])
    assert res.exit_code == 0, res.output
    assert list(pathlib.Path(tmp_path, "premium_index").rglob("compact-migrated*.parquet"))
    assert not list(pathlib.Path(tmp_path, "premium_index").rglob("part-*.parquet"))
