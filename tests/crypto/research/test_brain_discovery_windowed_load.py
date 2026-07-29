"""Discovery memory fix — the store.read_snapshots windowed/streamed load.

run_discovery OOM'd loading the whole ~22G primitive+label store into python dicts. The fix makes
read_snapshots (1) decode each file in BATCHES (never a whole-file .read()), (2) project columns,
and (3) apply an extra row_filter (horizon==60 for labels) — so peak memory is one batch of the
projected columns, and out-of-window files are already skipped by the existing floor. These tests
pin the memory-bound behaviour and byte-identical selection at the reader level.
"""
from __future__ import annotations

import pathlib

import pyarrow.compute as pc
import pytest

from crypto.research.brain import store

_MIN_NS = 60_000_000_000
_DAY0_NS = 1_782_000_000 * 1_000_000_000       # 2026-06-21 00:00 UTC


def _w(i: int) -> int:
    return _DAY0_NS + i * _MIN_NS


def _snap(sym: str, i: int) -> dict:
    return {
        "recv_ts_ns": _w(i) + _MIN_NS + 5_000_000_000, "symbol": sym,
        "window_start_ns": _w(i), "window_end_ns": _w(i) + _MIN_NS,
        "liq_buy_vol": float(i), "liq_sell_vol": 0.0,
        "liq_buy_quote_vol": 0.0, "liq_sell_quote_vol": 0.0,
        "liq_buy_count": i, "liq_sell_count": 0,
    }


def _seed(root, sym, n):
    store.write_snapshots(str(root), "forceorder", store.FORCEORDER_SNAPSHOT_SCHEMA,
                          [_snap(sym, i) for i in range(n)])


def _spy_methods(monkeypatch):
    calls = {"read": 0, "iter_batches": 0}
    orig = store.pq.ParquetFile

    class _Spy:
        def __init__(self, path, *a, **k):
            self._pf = orig(path, *a, **k)

        def read(self, *a, **k):
            calls["read"] += 1
            return self._pf.read(*a, **k)

        def iter_batches(self, *a, **k):
            calls["iter_batches"] += 1
            return self._pf.iter_batches(*a, **k)

        def __getattr__(self, n):
            return getattr(self._pf, n)

    monkeypatch.setattr(store.pq, "ParquetFile", _Spy)
    return calls


def test_read_snapshots_decodes_in_batches_never_whole_file(tmp_path, monkeypatch):
    # MEMORY-BOUND (the guard the original gate lacked): never materialize a whole file.
    _seed(tmp_path, "AAAUSDT", 300)
    calls = _spy_methods(monkeypatch)
    rows = store.read_snapshots(str(tmp_path), "forceorder")
    assert calls["iter_batches"] >= 1 and calls["read"] == 0
    assert len(rows) == 300


def test_read_snapshots_batched_output_byte_identical(tmp_path):
    # Default (no projection/filter): batched decode returns the same rows in the same order.
    _seed(tmp_path, "AAAUSDT", 50)
    rows = store.read_snapshots(str(tmp_path), "forceorder")
    assert [r["window_start_ns"] for r in rows] == [_w(i) for i in range(50)]
    assert rows[0]["liq_buy_count"] == 0 and rows[49]["liq_buy_count"] == 49


def test_read_snapshots_column_projection(tmp_path):
    _seed(tmp_path, "AAAUSDT", 5)
    rows = store.read_snapshots(str(tmp_path), "forceorder",
                                columns=["symbol", "window_start_ns", "liq_buy_count"])
    assert rows and set(rows[0].keys()) == {"symbol", "window_start_ns", "liq_buy_count"}


def test_read_snapshots_row_filter_drops_nonmatching(tmp_path):
    _seed(tmp_path, "AAAUSDT", 10)
    rows = store.read_snapshots(str(tmp_path), "forceorder",
                                row_filter=pc.field("liq_buy_count") >= 5)
    assert sorted(r["liq_buy_count"] for r in rows) == [5, 6, 7, 8, 9]


def test_read_survives_repeated_compactor_races(tmp_path, monkeypatch):
    # discovery's long whole-store read overlaps the 12-min hourly compactor and can race more
    # than once; bounded re-list retries must survive several mid-read fragment disappearances.
    _seed(tmp_path, "AAAUSDT", 20)
    orig = store.pq.ParquetFile
    state = {"fails_left": 3}

    def flaky(path, *a, **k):
        if state["fails_left"] > 0:                # simulate the compactor unlinking mid-read
            state["fails_left"] -= 1
            raise FileNotFoundError(str(path))
        return orig(path, *a, **k)

    monkeypatch.setattr(store.pq, "ParquetFile", flaky)
    rows = store.read_snapshots(str(tmp_path), "forceorder")
    assert len(rows) == 20                          # survived the races, no rows lost
    assert state["fails_left"] == 0                 # each race triggered a re-list retry


def test_read_propagates_persistent_absence(tmp_path, monkeypatch):
    # a fragment that NEVER reappears (a real error, not a race) must still propagate after the cap.
    _seed(tmp_path, "AAAUSDT", 5)
    monkeypatch.setattr(store.pq, "ParquetFile",
                        lambda path, *a, **k: (_ for _ in ()).throw(FileNotFoundError(str(path))))
    with pytest.raises(FileNotFoundError):
        store.read_snapshots(str(tmp_path), "forceorder")


def test_row_filter_and_window_floor_combine(tmp_path):
    _seed(tmp_path, "AAAUSDT", 20)
    rows = store.read_snapshots(str(tmp_path), "forceorder",
                                window_end_floor_ns=_w(15) + _MIN_NS,
                                row_filter=pc.field("liq_buy_count") <= 17)
    # window floor keeps i>=15; row_filter keeps liq_buy_count<=17 -> intersection {15,16,17}
    assert sorted(r["liq_buy_count"] for r in rows) == [15, 16, 17]
