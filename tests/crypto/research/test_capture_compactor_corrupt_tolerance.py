"""Compactor fragment-tolerance — the PR #53 reader pattern applied to compaction.

A capture restart can leave a truncated, footerless part-*.parquet (a partial flush).
The brain reader already SKIPS+LOGS such fragments (PR #53); the COMPACTOR did not —
``_merge_files`` (``pq.ParquetFile(p).read()``) and the metadata baselines in
``compact_partition`` / ``migrate_compact`` raised ``ArrowInvalid`` and aborted the whole
run. Since the closed-hour timer shares ``_merge_files``, one corrupt file blocked BOTH the
one-shot backlog sweep AND the steady-state timer (real incident: depth 06-18, LPTUSDT's
3505-byte file).

The fix: skip the corrupt fragment, EXCLUDE its (unreadable) rows from the parity baseline,
QUARANTINE it (rename out of the *.parquet namespace so it is never re-processed), and RECORD
a gap so the missing data is flagged not silently dropped. A footerless file has no readable
span, so the gap is inferred from the neighbor bounds: the previous readable fragment's max
recv_ts -> the next readable fragment's min recv_ts (ordered by flush mtime, the only signal a
corrupt file still carries).
"""
from __future__ import annotations

import os
import pathlib
import tempfile
from datetime import datetime, timezone

import pyarrow.parquet as pq

from crypto.research.capture_core import store as capture_store
from crypto.research.capture_core import maintenance

_DAY = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
_E_MS = int(_DAY.timestamp() * 1000)
_BASE_NS = _E_MS * 1_000_000                       # recv ~ event time, in ns


def _agg_row(symbol="BTCUSDT", *, recv_ns):
    return {"recv_ts_ns": recv_ns, "e": "aggTrade", "E": _E_MS, "a": 1, "s": symbol,
            "p": "100.5", "q": "2.0", "f": 1, "l": 2, "T": _E_MS, "m": False}


def _part_dir(root, symbol="BTCUSDT"):
    return pathlib.Path(root, "aggTrade", f"symbol={symbol}", "date=2026-05-29")


def _truncated_real_parquet() -> bytes:
    """A REAL aggTrade parquet with its FOOTER dropped — the byte-faithful shape of a
    partial flush (valid PAR1 header + row group + schema, only the trailing footer/magic
    gone), exactly like LPTUSDT's 3505-byte file. ``ParquetFile.read()`` raises
    ``ArrowInvalid: magic bytes not found in footer``."""
    with tempfile.TemporaryDirectory() as d:
        w = capture_store.aggtrade_writer(d, flush_max_bytes=1, flush_interval_s=10 ** 9)
        w.append(_agg_row(recv_ns=_BASE_NS))
        w.flush_all()
        raw = next(pathlib.Path(d, "aggTrade").rglob("part-*.parquet")).read_bytes()
    return raw[: len(raw) * 2 // 3]                 # drop the trailing footer + PAR1 magic


_CORRUPT = _truncated_real_parquet()


def _write_part(root, symbol="BTCUSDT", *, recv_ns, mtime=None):
    """One single-row part-*.parquet (recv_ns controls its only row; optional mtime)."""
    pd = _part_dir(root, symbol)
    before = set(pd.glob("part-*.parquet")) if pd.exists() else set()
    w = capture_store.aggtrade_writer(str(root), flush_max_bytes=1, flush_interval_s=10 ** 9)
    w.append(_agg_row(symbol, recv_ns=recv_ns))
    w.flush_all()
    # the file JUST written, by set-difference — robust to an mtime tie between two fast
    # writes (max-by-mtime could return the prior file, leaving the new one un-utimed in a
    # different flush-hour bucket -> a ~10% flake in the closed-hour test).
    f = (set(pd.glob("part-*.parquet")) - before).pop()
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


def _drop_corrupt(root, symbol="BTCUSDT", *, mtime=None, name="part-zzcorrupt.parquet"):
    p = _part_dir(root, symbol) / name
    p.write_bytes(_CORRUPT)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _parquet_files(part_dir):
    return sorted(part_dir.glob("part-*.parquet")) + sorted(part_dir.glob("compact-*.parquet"))


# T1 — compact_partition skips a corrupt fragment and merges the readable rest -------

def test_compact_partition_skips_corrupt_fragment_and_merges_readable(tmp_path):
    for k in range(3):
        _write_part(tmp_path, recv_ns=_BASE_NS + k)
    _drop_corrupt(tmp_path)
    pd = _part_dir(tmp_path)
    assert len(list(pd.glob("*.parquet"))) == 4              # 3 good + 1 corrupt

    res = maintenance.compact_partition(str(pd))

    # the 3 readable rows survive into ONE compact-migrated file; corrupt one skipped.
    assert res.rows_after == 3 and res.rows_before == 3      # parity on READABLE rows only
    merged = list(pd.glob("compact-migrated-*.parquet"))
    assert len(merged) == 1
    assert pq.read_table(str(merged[0])).num_rows == 3
    assert len(list(pd.glob("part-*.parquet"))) == 0         # readable parts merged away


# T2 — the skip records a gap with neighbor bounds (prev max recv -> next min recv) --

def test_skipped_fragment_recorded_as_gap_with_neighbor_bounds(tmp_path):
    # mtime order A < corrupt < B; A's row recv +1ms, B's row recv +9ms.
    _write_part(tmp_path, recv_ns=_BASE_NS + 1_000_000, mtime=100)
    _drop_corrupt(tmp_path, mtime=200)
    _write_part(tmp_path, recv_ns=_BASE_NS + 9_000_000, mtime=300)

    res = maintenance.compact_partition(str(_part_dir(tmp_path)))

    assert len(res.gaps) == 1
    g = res.gaps[0]
    assert g["symbol"] == "BTCUSDT" and g["stream"] == "aggTrade"
    assert g["reason"] == "compaction_skipped_corrupt"
    assert g["gap_start_ms"] == (_BASE_NS + 1_000_000) // 1_000_000   # prev readable max recv
    assert g["gap_end_ms"] == (_BASE_NS + 9_000_000) // 1_000_000     # next readable min recv


# T3 — the corrupt fragment is quarantined, so a re-run never re-skips it ------------

def test_corrupt_fragment_quarantined_and_not_reprocessed(tmp_path):
    _write_part(tmp_path, recv_ns=_BASE_NS)
    _write_part(tmp_path, recv_ns=_BASE_NS + 1)
    corrupt = _drop_corrupt(tmp_path)
    pd = _part_dir(tmp_path)

    maintenance.compact_partition(str(pd))
    assert not corrupt.exists()                              # renamed out of *.parquet
    assert list(pd.glob("*.corrupt"))                        # quarantined, preserved
    assert not list(pd.glob("part-*.parquet"))              # no live part-* remains

    # a second pass sees only the merged file + the quarantined .corrupt -> no-op, no new gap.
    res2 = maintenance.compact_partition(str(pd))
    assert res2.gaps == []


# T4 — migrate_compact tolerates corruption end-to-end and PERSISTS the gap ----------

def test_migrate_compact_tolerates_corruption_and_persists_gap(tmp_path):
    for k in range(3):
        _write_part(tmp_path, recv_ns=_BASE_NS + k)
    _drop_corrupt(tmp_path)

    rep = maintenance.migrate_compact(str(tmp_path), datasets=["aggTrade"],
                                      dates=["2026-05-29"])
    assert rep.partitions_compacted == 1 and rep.mismatches == []
    assert rep.rows_before == 3 and rep.rows_after == 3      # readable parity preserved

    # the gap was written to the capture _gaps manifest (flag-don't-drop).
    gaps_dir = pathlib.Path(tmp_path, "_gaps")
    rows = []
    for f in gaps_dir.rglob("*.parquet"):
        rows.extend(pq.read_table(str(f)).to_pylist())
    skip_gaps = [r for r in rows if r["reason"] == "compaction_skipped_corrupt"]
    assert len(skip_gaps) == 1 and skip_gaps[0]["symbol"] == "BTCUSDT"


# T5 — an all-corrupt partition does not crash; gaps recorded, no merged file --------

def test_all_corrupt_partition_does_not_crash(tmp_path):
    _part_dir(tmp_path).mkdir(parents=True)
    _drop_corrupt(tmp_path, name="part-aacorrupt.parquet")
    _drop_corrupt(tmp_path, name="part-bbcorrupt.parquet")

    res = maintenance.compact_partition(str(_part_dir(tmp_path)))
    assert res.rows_after == 0                               # nothing readable
    assert not list(_part_dir(tmp_path).glob("compact-migrated-*.parquet"))
    assert len(res.gaps) >= 1                                # the loss is still flagged


# T6 — the closed-hour timer path tolerates corruption (shares _merge_files) ---------

def test_closed_hour_compaction_tolerates_corrupt_fragment(tmp_path):
    # two good parts + a corrupt one, all in the same long-closed flush hour.
    a = _write_part(tmp_path, recv_ns=_BASE_NS)
    b = _write_part(tmp_path, recv_ns=_BASE_NS + 1)
    c = _drop_corrupt(tmp_path)
    hour_mtime = 1000 * 3600                                 # an arbitrary clock hour
    for f in (a, b, c):
        os.utime(f, (hour_mtime + 1, hour_mtime + 1))
    now_ts = hour_mtime + 3600 + 10_000                      # well past the hour + grace

    results = maintenance.compact_partition_closed_hours(
        str(_part_dir(tmp_path)), now_ts=now_ts, grace_s=300.0)
    assert sum(r.rows_after for r in results) == 2           # 2 readable rows merged
    assert sum(r.gaps and len(r.gaps) or 0 for r in results) == 1   # corrupt flagged
