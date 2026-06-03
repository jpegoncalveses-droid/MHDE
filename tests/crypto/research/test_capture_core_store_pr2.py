"""Round-trip tests for the PR-2 capture-core datasets (depth/bookTicker/
forceOrder/markPrice/depth_snapshot)."""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone

import pyarrow.parquet as pq

from crypto.research.capture_core import store


def _ms(dt):
    return int(dt.timestamp() * 1000)


_E = _ms(datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc))


def _read(root, dataset):
    files = sorted(pathlib.Path(root, dataset).rglob("*.parquet"))
    rows = []
    for fp in files:
        rows.extend(pq.read_table(str(fp)).to_pylist())
    return files, rows


def test_depth_writer_round_trips_nested_bids_asks(tmp_path):
    w = store.depth_writer(str(tmp_path))
    w.append({"recv_ts_ns": 7, "e": "depthUpdate", "E": _E, "T": _E, "s": "BTCUSDT",
              "U": 100, "u": 110, "pu": 99,
              "b": [["67000.0", "1.5"], ["66999.0", "0.0"]],  # zero-qty level kept
              "a": [["67001.0", "2.0"]]})
    w.flush_all()
    files, rows = _read(str(tmp_path), "depth")
    assert files[0].relative_to(tmp_path / "depth").parts[:2] == ("symbol=BTCUSDT", "date=2026-05-29")
    assert rows[0]["pu"] == 99
    assert rows[0]["b"] == [["67000.0", "1.5"], ["66999.0", "0.0"]]  # lossless, zero kept
    assert rows[0]["a"] == [["67001.0", "2.0"]]


def test_bookticker_writer_round_trips(tmp_path):
    w = store.bookticker_writer(str(tmp_path))
    w.append({"recv_ts_ns": 1, "e": "bookTicker", "u": 400, "s": "ETHUSDT",
              "b": "42.0", "B": "10", "a": "42.1", "A": "5", "T": _E, "E": _E})
    w.flush_all()
    _, rows = _read(str(tmp_path), "bookTicker")
    assert rows[0]["s"] == "ETHUSDT" and rows[0]["b"] == "42.0" and rows[0]["u"] == 400


def test_forceorder_writer_round_trips(tmp_path):
    w = store.forceorder_writer(str(tmp_path))
    w.append({"recv_ts_ns": 2, "E": _E, "s": "SOLUSDT", "S": "SELL", "o": "LIMIT",
              "f": "IOC", "q": "100", "p": "150.0", "ap": "149.9", "X": "FILLED",
              "l": "20", "z": "100", "T": _E})
    w.flush_all()
    files, rows = _read(str(tmp_path), "forceOrder")
    assert files[0].relative_to(tmp_path / "forceOrder").parts[0] == "symbol=SOLUSDT"
    assert rows[0]["S"] == "SELL" and rows[0]["ap"] == "149.9"


def test_markprice_writer_round_trips(tmp_path):
    w = store.markprice_writer(str(tmp_path))
    w.append({"recv_ts_ns": 3, "e": "markPriceUpdate", "E": _E, "s": "BTCUSDT",
              "p": "67000.0", "i": "67005.0", "P": "67010.0", "r": "0.0001", "T": _E})
    w.flush_all()
    _, rows = _read(str(tmp_path), "markPrice")
    assert rows[0]["r"] == "0.0001" and rows[0]["i"] == "67005.0"


def test_depth_snapshot_writer_round_trips(tmp_path):
    w = store.depth_snapshot_writer(str(tmp_path))
    w.append({"recv_ts_ns": 9, "s": "BTCUSDT", "lastUpdateId": 123456, "E": _E, "T": _E,
              "b": [["67000.0", "1.0"]], "a": [["67001.0", "2.0"]]})
    w.flush_all()
    files, rows = _read(str(tmp_path), "depth_snapshot")
    assert files[0].relative_to(tmp_path / "depth_snapshot").parts[:2] == \
        ("symbol=BTCUSDT", "date=2026-05-29")
    assert rows[0]["lastUpdateId"] == 123456
    assert rows[0]["b"] == [["67000.0", "1.0"]]
