"""Intra-day closed-hour brain-store compactor — bound TODAY's part-file fan-out.

Where the sealed-partition compactor (PR #62) merges a whole ``(symbol,date)`` partition
once ``date < today``, this one compacts CLOSED EVENT-TIME HOURS *within* the live partition
(including today's), so a continuous runner never fans out unboundedly between midnights. It
reuses #62's merge primitive (``_merge_tables_to_file``), corruption tolerance (``_read_part``
+ ``_quarantine``) and the registry parity oracle, but runs the FULL oracle per closed hour:
a closed hour is sealed once its watermark passes, so the registry roster for that hour is
complete and per-hour COMPLETENESS (every registry window in the hour present in the merged
file) catches a missing window that event-count alone cannot.

The cornerstone is the STRADDLE RULE. ``store.write_snapshots`` splits a pass only by DATE,
never by hour, so a catch-up pass writes ONE part file spanning many event-hours. Bucketing is
therefore by EVENT-TIME hour (``window_start_ns // HOUR_NS``). A part file is eligible to be
CONSUMED only when its MAX ``window_start_ns`` hour is itself closed; otherwise it straddles
the open hour and is DEFERRED WHOLE. A closed hour is compacted+audited only when no deferred
file still holds a window in it (else its roster is incomplete and completeness would
false-positive). Late post-watermark writes are accepted as tolerance: a fresh window for an
already-compacted hour lands as a NEW part file (``seen_windows`` dedup guarantees it is not a
duplicate), is swept into a SECOND ``compact-h<hour>`` file, never re-merges the sealed one.

Subprocess-isolated + chunked (the PR #60 memory model) with every registry mismatch / corrupt
skip / chunk failure MARSHALLED back — never swallowed into a silent "0".
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone

import pyarrow.parquet as pq

from crypto.research.brain import compaction
from crypto.research.brain import config as cfg
from crypto.research.brain import registry
from crypto.research.brain import store

_DATASET = cfg.MARKPRICE_DATASET                       # clean count_fn = update_count
_SCHEMA = store.MARKPRICE_SNAPSHOT_SCHEMA
_DATE = "2026-06-19"
_HOUR_NS = 3_600 * 1_000_000_000
_MIN_NS = 60_000_000_000
_WM = cfg.BRAIN_WATERMARK_NS


def _day_start_ns(date_str=_DATE):
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(d.timestamp()) * 1_000_000_000


def _hour_ns(hour, date_str=_DATE):
    return _day_start_ns(date_str) + hour * _HOUR_NS


def _ns(hour, minute=0, date_str=_DATE):
    """An event-time ns at ``hour:minute`` UTC on ``date_str``."""
    return _hour_ns(hour, date_str) + minute * _MIN_NS


def _now_after(hour, date_str=_DATE):
    """A ``now_ns`` at which every hour <= ``hour`` is CLOSED and ``hour+1`` is the open hour."""
    # hour H closes at H_end + watermark = _hour_ns(H+1) + WM; sit one minute past it.
    return _hour_ns(hour + 1, date_str) + _WM + _MIN_NS


def _snap(symbol, window_start_ns, *, update_count=5):
    row = {name: 0 for name in _SCHEMA.names}
    row.update(symbol=symbol, window_start_ns=window_start_ns,
               window_end_ns=window_start_ns + _MIN_NS,
               recv_ts_ns=window_start_ns, mark_close=100.0,
               update_count=update_count)
    return row


def _write_pass(root, symbol, window_starts, *, update_count=5):
    """One write pass -> ONE part file holding every window in ``window_starts`` (same date)."""
    snaps = [_snap(symbol, ws, update_count=update_count) for ws in window_starts]
    store.write_snapshots(str(root), _DATASET, _SCHEMA, snaps)


def _part_dir(root, symbol, date=_DATE):
    return pathlib.Path(root, _DATASET, f"symbol={symbol}", f"date={date}")


def _part_files(root, symbol, date=_DATE):
    return sorted(_part_dir(root, symbol, date).glob("part-*.parquet"))


def _hour_compact_files(root, symbol, hour_ns, date=_DATE):
    return sorted(_part_dir(root, symbol, date).glob(f"compact-h{hour_ns}-*.parquet"))


def _all_compact_files(root, symbol, date=_DATE):
    return sorted(_part_dir(root, symbol, date).glob("compact-*.parquet"))


def _reg_path(root):
    return str(pathlib.Path(root, "registry.sqlite"))


def _record(root, symbol, windows, *, dataset=_DATASET):
    """Record bookkeeping for ``windows`` = [(window_start_ns, n_events), ...]."""
    conn = registry.connect(_reg_path(root))
    bk = [{"dataset": dataset, "symbol": symbol, "window_start_ns": ws,
           "window_end_ns": ws + _MIN_NS, "recv_ts_ns": ws, "n_events": nev}
          for ws, nev in windows]
    registry.record_windows(conn, bk, now_ns=1)
    conn.close()


def _poison(part_dir):
    (part_dir / "part-poison.parquet").write_bytes(b"not a parquet at all")


def _windows(path):
    return sorted(r["window_start_ns"] for r in pq.ParquetFile(str(path)).read().to_pylist())


def _result_for(results, hour_ns):
    return next(r for r in results if r.hour_ns == hour_ns)


# -- the per-hour merge primitive ----------------------------------------------

def test_closed_hour_merges_parts_into_one_compact_file(tmp_path):
    for m in (0, 5, 10):                                  # 3 passes, same closed hour 10
        _write_pass(tmp_path, "BTCUSDT", [_ns(10, m)])
    assert len(_part_files(tmp_path, "BTCUSDT")) == 3
    res = compaction.compact_partition_closed_hours(
        str(_part_dir(tmp_path, "BTCUSDT")), now_ns=_now_after(10))
    h10 = _hour_ns(10)
    r = _result_for(res, h10)
    assert r.out_path is not None
    assert len(_hour_compact_files(tmp_path, "BTCUSDT", h10)) == 1
    assert _part_files(tmp_path, "BTCUSDT") == []         # writer parts consumed
    assert _windows(_hour_compact_files(tmp_path, "BTCUSDT", h10)[0]) == [_ns(10, m) for m in (0, 5, 10)]


def test_open_hour_is_never_touched(tmp_path):
    for m in (0, 5):                                      # hour 12 = the open hour
        _write_pass(tmp_path, "BTCUSDT", [_ns(12, m)])
    res = compaction.compact_partition_closed_hours(
        str(_part_dir(tmp_path, "BTCUSDT")), now_ns=_now_after(11))   # <=11 closed, 12 open
    assert res == []
    assert len(_part_files(tmp_path, "BTCUSDT")) == 2
    assert _all_compact_files(tmp_path, "BTCUSDT") == []


# -- the straddle rule ---------------------------------------------------------

def test_straddle_file_with_max_in_open_hour_is_deferred_whole(tmp_path):
    # ONE part file (one catch-up pass) holding a closed-hour-10 window and an open-hour-11 one
    _write_pass(tmp_path, "BTCUSDT", [_ns(10, 30), _ns(11, 5)])
    assert len(_part_files(tmp_path, "BTCUSDT")) == 1
    res = compaction.compact_partition_closed_hours(
        str(_part_dir(tmp_path, "BTCUSDT")), now_ns=_now_after(10))   # hour 10 closed, 11 open
    assert res == []                                     # max hour (11) open -> deferred whole
    assert _all_compact_files(tmp_path, "BTCUSDT") == []
    assert len(_part_files(tmp_path, "BTCUSDT")) == 1    # file survives intact (no rows lost)
    # next pass once hour 11 has closed: both hours compact, file consumed once
    res2 = compaction.compact_partition_closed_hours(
        str(_part_dir(tmp_path, "BTCUSDT")), now_ns=_now_after(11))
    assert len(_hour_compact_files(tmp_path, "BTCUSDT", _hour_ns(10))) == 1
    assert len(_hour_compact_files(tmp_path, "BTCUSDT", _hour_ns(11))) == 1
    assert _part_files(tmp_path, "BTCUSDT") == []


def test_catchup_file_spanning_two_closed_hours_routes_rows_by_event_hour(tmp_path):
    _write_pass(tmp_path, "BTCUSDT", [_ns(10, 15), _ns(11, 20)])   # one file, both closed
    compaction.compact_partition_closed_hours(
        str(_part_dir(tmp_path, "BTCUSDT")), now_ns=_now_after(11))
    h10, h11 = _hour_ns(10), _hour_ns(11)
    assert len(_hour_compact_files(tmp_path, "BTCUSDT", h10)) == 1
    assert len(_hour_compact_files(tmp_path, "BTCUSDT", h11)) == 1
    assert _part_files(tmp_path, "BTCUSDT") == []         # source deleted once
    assert _windows(_hour_compact_files(tmp_path, "BTCUSDT", h10)[0]) == [_ns(10, 15)]
    assert _windows(_hour_compact_files(tmp_path, "BTCUSDT", h11)[0]) == [_ns(11, 20)]


def test_hour_with_row_in_deferred_file_is_itself_deferred(tmp_path):
    _write_pass(tmp_path, "BTCUSDT", [_ns(10, 5)])                 # eligible file, hour 10
    _write_pass(tmp_path, "BTCUSDT", [_ns(10, 40), _ns(11, 5)])    # deferred straddle, also hour 10
    _record(tmp_path, "BTCUSDT", [(_ns(10, 5), 5), (_ns(10, 40), 5)])
    res = compaction.compact_partition_closed_hours(
        str(_part_dir(tmp_path, "BTCUSDT")), now_ns=_now_after(10),
        registry_path=_reg_path(tmp_path))
    h10 = _hour_ns(10)
    assert all(r.hour_ns != h10 for r in res)             # hour 10's roster incomplete -> deferred
    assert _hour_compact_files(tmp_path, "BTCUSDT", h10) == []
    assert all(not r.registry_mismatches for r in res)    # no false-positive 'missing' window
    assert len(_part_files(tmp_path, "BTCUSDT")) == 2      # both files kept


# -- the per-hour registry parity oracle ---------------------------------------

def test_per_hour_completeness_catches_missing_window(tmp_path):
    _write_pass(tmp_path, "BTCUSDT", [_ns(10, 0)])
    _write_pass(tmp_path, "BTCUSDT", [_ns(10, 1)])                 # store has 0,1 (2 parts)
    _record(tmp_path, "BTCUSDT", [(_ns(10, 0), 5), (_ns(10, 1), 5), (_ns(10, 2), 5)])
    res = compaction.compact_partition_closed_hours(
        str(_part_dir(tmp_path, "BTCUSDT")), now_ns=_now_after(10),
        registry_path=_reg_path(tmp_path))
    r = _result_for(res, _hour_ns(10))
    assert r.rows_after == 2                               # merge mechanically faithful
    assert len(r.registry_mismatches) == 1
    assert str(_ns(10, 2)) in r.registry_mismatches[0]


def test_per_hour_event_count_catches_corrupted_count(tmp_path):
    _write_pass(tmp_path, "BTCUSDT", [_ns(10, 0)], update_count=4)  # row says 4
    _record(tmp_path, "BTCUSDT", [(_ns(10, 0), 5)])                 # registry says 5
    res = compaction.compact_partition_closed_hours(
        str(_part_dir(tmp_path, "BTCUSDT")), now_ns=_now_after(10),
        registry_path=_reg_path(tmp_path))
    r = _result_for(res, _hour_ns(10))
    assert len(r.registry_mismatches) == 1
    assert "n_events" in r.registry_mismatches[0]


def test_per_hour_oracle_scopes_to_hour_not_day(tmp_path):
    _write_pass(tmp_path, "BTCUSDT", [_ns(10, 0)])
    _write_pass(tmp_path, "BTCUSDT", [_ns(10, 1)])                 # hour 10 complete (2 parts)
    _record(tmp_path, "BTCUSDT", [(_ns(10, 0), 5), (_ns(10, 1), 5)])
    _write_pass(tmp_path, "BTCUSDT", [_ns(11, 0)])                 # hour 11 short a window
    _record(tmp_path, "BTCUSDT", [(_ns(11, 0), 5), (_ns(11, 1), 5)])
    res = compaction.compact_partition_closed_hours(
        str(_part_dir(tmp_path, "BTCUSDT")), now_ns=_now_after(11),
        registry_path=_reg_path(tmp_path))
    assert _result_for(res, _hour_ns(10)).registry_mismatches == []
    r11 = _result_for(res, _hour_ns(11))
    assert len(r11.registry_mismatches) == 1
    assert str(_ns(11, 1)) in r11.registry_mismatches[0]


def test_unrecorded_store_window_not_flagged(tmp_path):
    _write_pass(tmp_path, "BTCUSDT", [_ns(10, 0)])
    _write_pass(tmp_path, "BTCUSDT", [_ns(10, 1)])                 # store has 0,1
    _record(tmp_path, "BTCUSDT", [(_ns(10, 0), 5)])                # registry knows only 0
    res = compaction.compact_partition_closed_hours(
        str(_part_dir(tmp_path, "BTCUSDT")), now_ns=_now_after(10),
        registry_path=_reg_path(tmp_path))
    r = _result_for(res, _hour_ns(10))
    assert r.registry_mismatches == []                    # registry->store only
    assert r.rows_after == 2


def test_clean_closed_hour_has_no_mismatch(tmp_path):
    for m in range(3):
        _write_pass(tmp_path, "BTCUSDT", [_ns(10, m)], update_count=5)
    _record(tmp_path, "BTCUSDT", [(_ns(10, m), 5) for m in range(3)])
    res = compaction.compact_partition_closed_hours(
        str(_part_dir(tmp_path, "BTCUSDT")), now_ns=_now_after(10),
        registry_path=_reg_path(tmp_path))
    assert _result_for(res, _hour_ns(10)).registry_mismatches == []
    assert len(_hour_compact_files(tmp_path, "BTCUSDT", _hour_ns(10))) == 1


# -- idempotency, crash self-heal, late writes ---------------------------------

def test_idempotent_rerun_is_noop(tmp_path):
    for m in range(3):
        _write_pass(tmp_path, "BTCUSDT", [_ns(10, m)])
    now = _now_after(10)
    compaction.compact_partition_closed_hours(str(_part_dir(tmp_path, "BTCUSDT")), now_ns=now)
    h10 = _hour_ns(10)
    assert len(_hour_compact_files(tmp_path, "BTCUSDT", h10)) == 1
    res2 = compaction.compact_partition_closed_hours(str(_part_dir(tmp_path, "BTCUSDT")), now_ns=now)
    assert res2 == []                                     # no part-* remain -> strict no-op
    assert len(_hour_compact_files(tmp_path, "BTCUSDT", h10)) == 1
    assert _part_files(tmp_path, "BTCUSDT") == []


def test_crash_between_replace_and_delete_self_heals(tmp_path):
    for m in range(3):
        _write_pass(tmp_path, "BTCUSDT", [_ns(10, m)])
    now = _now_after(10)
    compaction.compact_partition_closed_hours(str(_part_dir(tmp_path, "BTCUSDT")), now_ns=now)
    h10 = _hour_ns(10)
    assert len(_hour_compact_files(tmp_path, "BTCUSDT", h10)) == 1
    # simulate a post-replace/pre-delete crash: the merged originals are still on disk
    for m in range(3):
        _write_pass(tmp_path, "BTCUSDT", [_ns(10, m)])
    assert len(_part_files(tmp_path, "BTCUSDT")) == 3
    compaction.compact_partition_closed_hours(str(_part_dir(tmp_path, "BTCUSDT")), now_ns=now)
    assert len(_hour_compact_files(tmp_path, "BTCUSDT", h10)) == 1   # still ONE compact file
    assert _part_files(tmp_path, "BTCUSDT") == []                    # redundant parts swept
    rows = store.read_snapshots(str(tmp_path), _DATASET, "BTCUSDT")
    assert sorted(r["window_start_ns"] for r in rows) == [_ns(10, m) for m in range(3)]


def test_late_write_after_sealed_hour_compacts_into_new_part_no_remerge(tmp_path):
    for m in range(2):
        _write_pass(tmp_path, "BTCUSDT", [_ns(10, m)])
    now = _now_after(10)
    compaction.compact_partition_closed_hours(str(_part_dir(tmp_path, "BTCUSDT")), now_ns=now)
    h10 = _hour_ns(10)
    first = _hour_compact_files(tmp_path, "BTCUSDT", h10)
    assert len(first) == 1
    sealed_bytes = first[0].read_bytes()
    # a late post-watermark window for the already-sealed hour 10 (watermark violation)
    _write_pass(tmp_path, "BTCUSDT", [_ns(10, 30)])
    assert len(_part_files(tmp_path, "BTCUSDT")) == 1
    compaction.compact_partition_closed_hours(str(_part_dir(tmp_path, "BTCUSDT")), now_ns=now)
    after = _hour_compact_files(tmp_path, "BTCUSDT", h10)
    assert len(after) == 2                                # a SECOND compact-h<10> for the late window
    assert first[0].read_bytes() == sealed_bytes          # original never re-merged
    assert _part_files(tmp_path, "BTCUSDT") == []          # late part consumed
    rows = store.read_snapshots(str(tmp_path), _DATASET, "BTCUSDT")
    assert sorted(set(r["window_start_ns"] for r in rows)) == [_ns(10, 0), _ns(10, 1), _ns(10, 30)]
    assert len(rows) == 3                                  # no double-count


# -- corruption tolerance + the no-op merge ------------------------------------

def test_corrupt_fragment_skipped_quarantined_and_surfaced(tmp_path):
    _write_pass(tmp_path, "BTCUSDT", [_ns(10, 0)])
    _write_pass(tmp_path, "BTCUSDT", [_ns(10, 1)])
    _poison(_part_dir(tmp_path, "BTCUSDT"))
    _record(tmp_path, "BTCUSDT", [(_ns(10, 0), 5), (_ns(10, 1), 5), (_ns(10, 2), 5)])
    res = compaction.compact_partition_closed_hours(
        str(_part_dir(tmp_path, "BTCUSDT")), now_ns=_now_after(10),
        registry_path=_reg_path(tmp_path))
    r = _result_for(res, _hour_ns(10))
    assert len(r.corrupt_skipped) == 1
    assert (_part_dir(tmp_path, "BTCUSDT") / "part-poison.parquet.corrupt").exists()
    assert not (_part_dir(tmp_path, "BTCUSDT") / "part-poison.parquet").exists()
    assert len(r.registry_mismatches) == 1                # registry window 2 surfaced as missing
    assert str(_ns(10, 2)) in r.registry_mismatches[0]


def test_zero_or_one_part_hour_is_noop_merge_but_audited(tmp_path):
    _write_pass(tmp_path, "BTCUSDT", [_ns(10, 0)])         # a single part file in hour 10
    _record(tmp_path, "BTCUSDT", [(_ns(10, 0), 5)])
    res = compaction.compact_partition_closed_hours(
        str(_part_dir(tmp_path, "BTCUSDT")), now_ns=_now_after(10),
        registry_path=_reg_path(tmp_path))
    r = _result_for(res, _hour_ns(10))
    assert r.out_path is None                              # nothing to merge
    assert _hour_compact_files(tmp_path, "BTCUSDT", _hour_ns(10)) == []
    assert r.registry_mismatches == []                    # but still audited
    assert len(_part_files(tmp_path, "BTCUSDT")) == 1      # left in place


# -- the chunked, subprocess-bounded driver ------------------------------------

def test_chunked_marshals_registry_mismatch_back(tmp_path):
    _write_pass(tmp_path, "BTCUSDT", [_ns(10, 0)])         # store windows 0,2 (2 parts)
    _write_pass(tmp_path, "BTCUSDT", [_ns(10, 2)])
    _record(tmp_path, "BTCUSDT", [(_ns(10, 0), 5), (_ns(10, 1), 5), (_ns(10, 2), 5)])   # knows 1
    _write_pass(tmp_path, "ETHUSDT", [_ns(10, 0)])         # a clean second partition
    _write_pass(tmp_path, "ETHUSDT", [_ns(10, 1)])
    _record(tmp_path, "ETHUSDT", [(_ns(10, 0), 5), (_ns(10, 1), 5)])
    rep = compaction.compact_brain_closed_hours_chunked(
        str(tmp_path), datasets=[_DATASET], merges_per_chunk=10, now_ns=_now_after(10),
        registry_path=_reg_path(tmp_path),
        chunk_runner=compaction._inprocess_closed_hours_chunk_runner())
    assert len(rep.registry_mismatches) == 1              # not swallowed as a silent 0
    assert str(_ns(10, 1)) in rep.registry_mismatches[0]
    assert rep.partitions_compacted == 2                  # both still compact


def test_chunked_real_subprocess_isolation(tmp_path):
    for sym in ("SYM0USDT", "SYM1USDT"):
        for m in range(2):
            _write_pass(tmp_path, sym, [_ns(10, m)])
        _record(tmp_path, sym, [(_ns(10, 0), 5), (_ns(10, 1), 5)])
    rep = compaction.compact_brain_closed_hours_chunked(   # default runner = real subprocess
        str(tmp_path), datasets=[_DATASET], merges_per_chunk=10, now_ns=_now_after(10),
        registry_path=_reg_path(tmp_path))
    assert rep.partitions_compacted == 2
    assert rep.registry_mismatches == []
    for sym in ("SYM0USDT", "SYM1USDT"):
        assert len(_hour_compact_files(tmp_path, sym, _hour_ns(10))) == 1


def test_chunked_subprocess_failure_surfaces_not_silent_zero(tmp_path):
    for m in range(2):
        _write_pass(tmp_path, "BTCUSDT", [_ns(10, m)])

    def _failing_runner(root, paths, budget, now_ns, registry_path):
        return {"completed": 0, "compacted": 0, "merges": 0, "files_before": 0,
                "files_after": 0, "mismatches": [], "registry_mismatches": [],
                "corrupt_skipped": [], "failed": True}

    rep = compaction.compact_brain_closed_hours_chunked(
        str(tmp_path), datasets=[_DATASET], merges_per_chunk=10, now_ns=_now_after(10),
        chunk_runner=_failing_runner)
    assert rep.chunk_failures                              # surfaced, not a clean 0-mismatch advance


# -- the close boundary + per-dataset count_fn ---------------------------------

def test_hour_close_boundary_off_by_one(tmp_path):
    for m in range(2):
        _write_pass(tmp_path, "BTCUSDT", [_ns(10, m)])
    h10 = _hour_ns(10)
    hour_end = h10 + compaction.HOUR_NS
    # one ns BEFORE the close: hour 10 is still OPEN -> skipped, parts untouched
    res_open = compaction.compact_partition_closed_hours(
        str(_part_dir(tmp_path, "BTCUSDT")), now_ns=hour_end + _WM - 1)
    assert res_open == []
    assert len(_part_files(tmp_path, "BTCUSDT")) == 2
    # exactly AT the close: hour 10 is CLOSED -> compacted
    res_closed = compaction.compact_partition_closed_hours(
        str(_part_dir(tmp_path, "BTCUSDT")), now_ns=hour_end + _WM)
    assert any(r.hour_ns == h10 and r.out_path is not None for r in res_closed)
    assert _part_files(tmp_path, "BTCUSDT") == []


def test_count_fn_per_dataset_used_in_oracle(tmp_path):
    fds = cfg.FORCEORDER_DATASET
    fschema = store.FORCEORDER_SNAPSHOT_SCHEMA

    def _fsnap(window_start_ns, *, lb, ls):
        row = {name: 0 for name in fschema.names}
        row.update(symbol="BTCUSDT", window_start_ns=window_start_ns,
                   window_end_ns=window_start_ns + _MIN_NS, recv_ts_ns=window_start_ns,
                   liq_buy_count=lb, liq_sell_count=ls)
        return row

    # window 0: liq_buy 2 + liq_sell 3 = 5 == registry 5 (match); window 1: 1+1=2 != registry 9
    store.write_snapshots(str(tmp_path), fds, fschema, [_fsnap(_ns(10, 0), lb=2, ls=3)])
    store.write_snapshots(str(tmp_path), fds, fschema, [_fsnap(_ns(10, 1), lb=1, ls=1)])
    _record(tmp_path, "BTCUSDT", [(_ns(10, 0), 5), (_ns(10, 1), 9)], dataset=fds)
    fpart_dir = pathlib.Path(tmp_path, fds, "symbol=BTCUSDT", f"date={_DATE}")
    res = compaction.compact_partition_closed_hours(
        str(fpart_dir), now_ns=_now_after(10), registry_path=_reg_path(tmp_path))
    r = _result_for(res, _hour_ns(10))
    assert len(r.registry_mismatches) == 1                # liq_buy+liq_sell count_fn used
    assert str(_ns(10, 1)) in r.registry_mismatches[0]
    assert "n_events" in r.registry_mismatches[0]
