"""Drift Fix 2b: ``store.read_snapshots`` mtime FILE-skip + the compactor-race retry.

The label pass fully decoded EVERY file under a kept ``symbol=/date=`` dir each run —
229,421 full reads (~1.6 GB) per pass on the 2026-07-02 store, ~99% of decoded bytes
discarded by the ``window_end`` row filter. A brain-store file's rows are written strictly
AFTER their windows settle, so ``window_end_ns < write-time (mtime)`` for every row: a file
whose ``st_mtime_ns + guard <= window_end_floor_ns`` provably holds ONLY rows the row filter
would drop -> skipped without opening. ``window_end_floor_ns == 0`` (every non-label caller)
disables the skip entirely. Compaction output is written later (newer mtime), so it is never
wrongly skipped.

The retry: the brain compactor's replace-then-delete can unlink a file between the reader's
glob and its open (the capture-side race that produced 52 unlogged gaps overnight). Labels
must not crash or silently lose rows: on FileNotFoundError the WHOLE listing+read is rebuilt
once from scratch (the merged compact file is picked up by the fresh glob); a second miss
propagates.
"""
from __future__ import annotations

import os
import pathlib

import pytest

from crypto.research.brain import store

_MIN_NS = 60_000_000_000
_DAY0_NS = 1_782_000_000 * 1_000_000_000       # 2026-06-21 00:00 UTC


def _w(i: int) -> int:
    """window start ns for minute i of day 0."""
    return _DAY0_NS + i * _MIN_NS


def _snap(sym: str, i: int) -> dict:
    return {
        "recv_ts_ns": _w(i) + _MIN_NS + 5_000_000_000,
        "symbol": sym,
        "window_start_ns": _w(i),
        "window_end_ns": _w(i) + _MIN_NS,
        "liq_buy_vol": float(i), "liq_sell_vol": 0.0,
        "liq_buy_quote_vol": 0.0, "liq_sell_quote_vol": 0.0,
        "liq_buy_count": i, "liq_sell_count": 0,
    }


def _write_one(root, sym, i) -> pathlib.Path:
    paths = store.write_snapshots(str(root), "forceorder", store.FORCEORDER_SNAPSHOT_SCHEMA,
                                  [_snap(sym, i)])
    assert len(paths) == 1
    return pathlib.Path(paths[0])


def _set_mtime_ns(path, ns):
    os.utime(path, (ns / 1e9, ns / 1e9))


def _spy_opens(monkeypatch):
    opened = []
    orig = store.pq.ParquetFile

    def spy(path, *a, **k):
        opened.append(pathlib.Path(str(path)).name)
        return orig(path, *a, **k)

    monkeypatch.setattr(store.pq, "ParquetFile", spy)
    return opened


def test_mtime_skip_drops_provably_below_floor_files(tmp_path, monkeypatch):
    sym = "AAAUSDT"
    old = _write_one(tmp_path, sym, 10)                 # window_end = _w(11)
    new = _write_one(tmp_path, sym, 500)                # window_end = _w(501)
    floor = _w(300)
    _set_mtime_ns(old, floor - 3600 * 1_000_000_000)    # written 1h before the floor
    _set_mtime_ns(new, floor + 3600 * 1_000_000_000)
    opened = _spy_opens(monkeypatch)
    rows = store.read_snapshots(str(tmp_path), "forceorder", sym, window_end_floor_ns=floor)
    assert old.name not in opened, \
        "a file whose mtime + guard is below the window_end floor is provably all-below-floor"
    assert new.name in opened
    assert [r["window_start_ns"] for r in rows] == [_w(500)]


def test_mtime_skip_output_identical_to_row_filter_alone(tmp_path):
    # Byte-identical: the skip only removes files whose EVERY row the row filter drops.
    # Every constructed file respects the write invariant mtime >= max(window_end) — a
    # brain-store row is always written AFTER its window settles, which is exactly the
    # invariant the skip trusts (a below-floor mtime therefore proves below-floor rows).
    sym = "AAAUSDT"
    floor = _w(300)
    for i, mt_off in ((10, -7200), (250, -300), (299, 30), (500, 12_600), (700, 24_600)):
        p = _write_one(tmp_path, sym, i)
        assert floor + mt_off * 1_000_000_000 >= _w(i) + _MIN_NS, "test data must respect the invariant"
        _set_mtime_ns(p, floor + mt_off * 1_000_000_000)
    with_skip = store.read_snapshots(str(tmp_path), "forceorder", sym,
                                     window_end_floor_ns=floor)
    everything = store.read_snapshots(str(tmp_path), "forceorder", sym)
    expected = [r for r in everything if r["window_end_ns"] >= floor]
    assert with_skip == expected


def test_mtime_skip_disabled_when_floor_zero(tmp_path, monkeypatch):
    sym = "AAAUSDT"
    old = _write_one(tmp_path, sym, 10)
    _set_mtime_ns(old, _w(0))
    opened = _spy_opens(monkeypatch)
    rows = store.read_snapshots(str(tmp_path), "forceorder", sym)
    assert old.name in opened, "floor=0 (every non-label caller) must disable the file skip"
    assert len(rows) == 1


def test_mtime_within_guard_still_opened(tmp_path, monkeypatch):
    sym = "AAAUSDT"
    edge = _write_one(tmp_path, sym, 10)
    floor = _w(300)
    _set_mtime_ns(edge, floor - 30 * 1_000_000_000)     # 30s inside the 60s guard
    opened = _spy_opens(monkeypatch)
    store.read_snapshots(str(tmp_path), "forceorder", sym, window_end_floor_ns=floor)
    assert edge.name in opened, "a file within the skew guard of the floor must be opened"


def test_filenotfound_race_retries_once_and_reads_replacement(tmp_path, monkeypatch):
    # Simulate the compactor swap: the FIRST open of the original raises FileNotFoundError
    # (unlinked after the glob); by then a compact-migrated file with the same rows exists.
    # The retry re-globs and returns the full row set — nothing lost, nothing duplicated.
    sym = "AAAUSDT"
    orig_file = _write_one(tmp_path, sym, 10)
    part_dir = orig_file.parent
    keep = _write_one(tmp_path, sym, 11)

    orig_pf = store.pq.ParquetFile
    state = {"fired": False}

    def racy(path, *a, **k):
        if pathlib.Path(str(path)) == orig_file and not state["fired"]:
            state["fired"] = True
            # the compactor: merged file lands, original unlinked — then the open fails
            merged = orig_pf(str(orig_file)).read()
            import pyarrow.parquet as pq2
            pq2.write_table(merged, str(part_dir / "compact-migrated-race.parquet"))
            os.remove(orig_file)
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return orig_pf(path, *a, **k)

    monkeypatch.setattr(store.pq, "ParquetFile", racy)
    rows = store.read_snapshots(str(tmp_path), "forceorder", sym)
    assert sorted(r["window_start_ns"] for r in rows) == [_w(10), _w(11)], \
        "the retry must re-glob and pick up the compacted replacement — no loss, no dupes"
    assert state["fired"]


def test_labels_byte_identical_with_mtime_skip_firing(tmp_path, monkeypatch):
    # END-TO-END labels proof: run_once over a markprice store whose historical files carry
    # REALISTIC aged mtimes (write-time = window_end + 5 min) produces byte-identical labels
    # with the mtime file-skip ACTIVE vs DISABLED — and the skip demonstrably fires (the
    # aged below-floor files are never opened on the active side).
    from crypto.research.brain import labels, registry

    sym = "AAAUSDT"
    mp_root = tmp_path / "mp"

    def _mp_snap(k: int) -> dict:
        return {
            "recv_ts_ns": _w(k) + _MIN_NS, "symbol": sym,
            "window_start_ns": _w(k), "window_end_ns": _w(k) + _MIN_NS,
            "mark_open": 100.0 + k, "mark_high": 101.0 + k, "mark_low": 99.0 + k,
            "mark_close": 100.0 + k,
            "index_open": 100.0, "index_high": 100.0, "index_low": 100.0, "index_close": 100.0,
            "settle_open": 100.0, "settle_high": 100.0, "settle_low": 100.0, "settle_close": 100.0,
            "funding_last": 0.0, "funding_min": 0.0, "funding_max": 0.0,
            "next_funding_time_last": 0, "update_count": 1,
        }

    # windows: an old block (0..90, below the eventual floor) + a fresh block near the
    # frontier (1440..1500). One file per window; mtime = window_end + 5 min (invariant-valid).
    ks = list(range(0, 91)) + list(range(1440, 1501))
    for k in ks:
        paths = store.write_snapshots(str(mp_root), "markprice",
                                      store.MARKPRICE_SNAPSHOT_SCHEMA, [_mp_snap(k)])
        _set_mtime_ns(pathlib.Path(paths[0]), _w(k) + _MIN_NS + 300 * 1_000_000_000)

    frontier_end = _w(1500)          # 25h of windows -> floor - 1day margin lands inside ks

    def _run(label_dir: str, reg_name: str) -> list[dict]:
        reg = str(tmp_path / reg_name)
        conn = registry.connect(reg)
        registry.advance(conn, "markprice", new_recv_ts_ns=frontier_end,
                         bookkeeping=[{"dataset": "markprice", "symbol": sym,
                                       "window_start_ns": frontier_end - _MIN_NS,
                                       "window_end_ns": frontier_end,
                                       "recv_ts_ns": frontier_end, "n_events": 1}],
                         now_ns=frontier_end)
        conn.close()
        return labels.run_once(store_root=str(mp_root), capture_root=str(tmp_path / "cap"),
                               registry_path=reg, label_store_root=str(tmp_path / label_dir),
                               horizons_min=[5], symbols=[sym], now_ns=frontier_end)

    opened = _spy_opens(monkeypatch)
    with_skip = _run("lab_a", "reg_a.sqlite")
    n_active = len(opened)

    monkeypatch.setattr(store, "_MTIME_SKIP_GUARD_NS", 10**22)   # guard so huge it never skips
    opened.clear()
    without_skip = _run("lab_b", "reg_b.sqlite")
    n_off = len(opened)

    key = lambda r: (r["symbol"], r["window_start_ns"], r["horizon_min"])  # noqa: E731
    assert sorted(with_skip, key=key) == sorted(without_skip, key=key), \
        "labels must be byte-identical with the mtime file-skip active vs disabled"
    assert len(with_skip) > 0, "the harness must actually label something"
    assert n_active < n_off, \
        "the skip must actually fire (fewer markprice files opened on the active side)"


def test_filenotfound_persistent_raises(tmp_path, monkeypatch):
    sym = "AAAUSDT"
    cursed = _write_one(tmp_path, sym, 10)

    orig_pf = store.pq.ParquetFile

    def always_missing(path, *a, **k):
        if pathlib.Path(str(path)) == cursed:
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return orig_pf(path, *a, **k)

    monkeypatch.setattr(store.pq, "ParquetFile", always_missing)
    with pytest.raises(FileNotFoundError):
        store.read_snapshots(str(tmp_path), "forceorder", sym)
