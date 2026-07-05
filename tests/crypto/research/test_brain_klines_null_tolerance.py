"""The ONE brain-side diff of the OKX migration: null-tolerant klines reading.

OKX candles carry no trade count or taker-buy split, so the OKX collector
persists honest NULLs for ``trades`` / ``takerBuyBase`` / ``takerBuyQuote``
(never 0 — the skipped-fragment-is-a-gap no-bias rule: missing is missing).
The brain's ``read_new_klines`` previously hard-cast ``int(r["trades"])`` /
``float(r["takerBuyBase"])`` and crashed on null.

Operator condition (2026-07-03): the None must propagate CLEANLY end-to-end —
reader cast -> klines bucket_fn -> brain-store snapshot write — with no
``int(None)`` resurfacing one layer down. This test IS that round-trip, on the
real modules (no mocks). Binance rows (all three fields present) must be
untouched by the change.
"""
from __future__ import annotations

import pathlib

import pyarrow as pa
import pyarrow.parquet as pq

from crypto.research.brain import reader
from crypto.research.brain import store as brain_store
from crypto.research.brain.sources import KLINES
from crypto.research.capture_core.klines_store import KLINES_1H_SCHEMA

_OPEN_MS = 1_783_101_600_000                  # 2026-07-03T15:00Z hour open
_RECV_NS = 1_783_105_260_000 * 1_000_000      # arrival ~an hour later
_CADENCE_NS = 60 * 1_000_000_000


def _capture_row(**overrides):
    row = {
        "recv_ts_ns": _RECV_NS, "s": "BTCUSDT",
        "openTime": _OPEN_MS, "open": "62100", "high": "62250", "low": "62050",
        "close": "62200", "volume": "300.0", "closeTime": _OPEN_MS + 3_599_999,
        "quoteVolume": "18600000.0",
        "trades": None, "takerBuyBase": None, "takerBuyQuote": None,
    }
    row.update(overrides)
    return row


def _write_capture(root, row):
    part_dir = pathlib.Path(root, "klines_1h", "symbol=BTCUSDT", "date=2026-07-03")
    part_dir.mkdir(parents=True)
    table = pa.Table.from_pylist([row], schema=KLINES_1H_SCHEMA)
    pq.write_table(table, part_dir / "part-test.parquet", compression="zstd")


def test_null_klines_round_trip_reader_bucket_store(tmp_path):
    capture_root = tmp_path / "capture"
    brain_root = tmp_path / "brain"
    _write_capture(capture_root, _capture_row())

    # 1) reader cast: nulls become None, real fields still parse
    rows = reader.read_new_klines(str(capture_root))
    assert len(rows) == 1
    r = rows[0]
    assert r["trades"] is None
    assert r["taker_buy_base"] is None and r["taker_buy_quote"] is None
    assert r["close"] == 62200.0 and r["volume"] == 300.0
    assert r["open_time"] == _OPEN_MS and r["close_time"] == _OPEN_MS + 3_599_999

    # 2) the real klines bucket_fn passes None through as the as-of value
    snaps = KLINES.bucket_fn(rows, cadence_ns=_CADENCE_NS)
    assert len(snaps) == 1
    assert snaps[0]["trades"] is None and snaps[0]["taker_buy_base"] is None
    assert snaps[0]["close"] == 62200.0

    # 3) the brain-store snapshot write accepts the nulls, and the brain's OWN
    #    read path (read_snapshots) returns them as None
    written = brain_store.write_snapshots(
        str(brain_root), "klines_1h", brain_store.KLINES_SNAPSHOT_SCHEMA, snaps)
    assert written
    back = brain_store.read_snapshots(str(brain_root), "klines_1h")
    assert len(back) == 1
    assert back[0]["trades"] is None and back[0]["taker_buy_base"] is None
    assert back[0]["taker_buy_quote"] is None
    assert back[0]["close"] == 62200.0


def test_binance_rows_with_all_fields_are_unchanged(tmp_path):
    capture_root = tmp_path / "capture"
    _write_capture(capture_root, _capture_row(
        trades=12345, takerBuyBase="150.5", takerBuyQuote="9300000.1"))
    r = reader.read_new_klines(str(capture_root))[0]
    assert r["trades"] == 12345
    assert r["taker_buy_base"] == 150.5 and r["taker_buy_quote"] == 9300000.1
