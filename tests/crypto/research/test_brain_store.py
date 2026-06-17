"""Tests for the brain parquet event store (write -> read round-trip + schema).

Mirrors the capture_core store convention (pyarrow + zstd + Hive
``symbol=/date=`` partitions), but persists the brain's numeric snapshot
columns. The schema is the persistence-layer half of the NO-BIAS guardrail:
its field names must be exactly the raw-primitive whitelist.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone

import pyarrow as pa

from crypto.research.brain import store


_CADENCE_NS = 60 * 1_000_000_000

# Ordered whitelist — hardcoded so any composed column added to the schema
# breaks exact equality.
_EXPECTED_ORDER = [
    "recv_ts_ns", "symbol", "window_start_ns", "window_end_ns",
    "taker_buy_vol", "taker_sell_vol",
    "taker_buy_quote_vol", "taker_sell_quote_vol",
    "buy_trade_count", "sell_trade_count", "trade_count",
    "price_open", "price_high", "price_low", "price_close",
    "qty_sum", "qty_max", "qty_mean",
]


def _ws_ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1000) * 1_000_000


def _snapshot(symbol="BTCUSDT", *, window_start_ns, recv=999, factor=1.0):
    return {
        "recv_ts_ns": recv,
        "symbol": symbol,
        "window_start_ns": window_start_ns,
        "window_end_ns": window_start_ns + _CADENCE_NS,
        "taker_buy_vol": 3.0 * factor,
        "taker_sell_vol": 5.0 * factor,
        "taker_buy_quote_vol": 300.0 * factor,
        "taker_sell_quote_vol": 500.0 * factor,
        "buy_trade_count": 2,
        "sell_trade_count": 1,
        "trade_count": 3,
        "price_open": 100.0 * factor,
        "price_high": 105.0 * factor,
        "price_low": 98.0 * factor,
        "price_close": 101.0 * factor,
        "qty_sum": 8.0 * factor,
        "qty_max": 5.0 * factor,
        "qty_mean": 8.0 / 3.0,
    }


def _key(rows):
    return {(r["symbol"], r["window_start_ns"]): r for r in rows}


def test_schema_field_names_are_exactly_the_raw_primitive_whitelist():
    assert list(store.TRADES_SNAPSHOT_SCHEMA.names) == _EXPECTED_ORDER


def test_schema_types_are_int_string_float_only():
    sch = store.TRADES_SNAPSHOT_SCHEMA
    assert sch.field("recv_ts_ns").type == pa.int64()
    assert sch.field("window_start_ns").type == pa.int64()
    assert sch.field("window_end_ns").type == pa.int64()
    assert sch.field("symbol").type == pa.string()
    for c in ("buy_trade_count", "sell_trade_count", "trade_count"):
        assert sch.field(c).type == pa.int64()
    for c in ("taker_buy_vol", "taker_sell_vol", "taker_buy_quote_vol",
              "taker_sell_quote_vol", "price_open", "price_high",
              "price_low", "price_close", "qty_sum", "qty_max", "qty_mean"):
        assert sch.field(c).type == pa.float64()


def test_write_then_read_round_trips_identically(tmp_path):
    ws = _ws_ns(datetime(2026, 6, 16, 20, 0, 0, tzinfo=timezone.utc))
    snaps = [
        _snapshot("BTCUSDT", window_start_ns=ws, recv=111),
        _snapshot("ETHUSDT", window_start_ns=ws, recv=222, factor=0.5),
    ]
    paths = store.write_snapshots(str(tmp_path), snaps)
    assert paths
    got = store.read_snapshots(str(tmp_path))
    assert _key(got) == _key(snaps)


def test_partition_layout_is_symbol_then_event_date(tmp_path):
    dt = datetime(2026, 6, 16, 20, 0, 0, tzinfo=timezone.utc)
    store.write_snapshots(str(tmp_path), [_snapshot("BTCUSDT", window_start_ns=_ws_ns(dt))])
    files = list(pathlib.Path(tmp_path, "trades").rglob("*.parquet"))
    parts = {fp.relative_to(tmp_path / "trades").parts[:2] for fp in files}
    assert ("symbol=BTCUSDT", "date=2026-06-16") in parts


def test_event_date_partition_splits_across_utc_midnight(tmp_path):
    before = _ws_ns(datetime(2026, 6, 16, 23, 59, 0, tzinfo=timezone.utc))
    after = _ws_ns(datetime(2026, 6, 17, 0, 1, 0, tzinfo=timezone.utc))
    store.write_snapshots(str(tmp_path), [
        _snapshot("BTCUSDT", window_start_ns=before),
        _snapshot("BTCUSDT", window_start_ns=after),
    ])
    dates = {fp.parent.name for fp in pathlib.Path(tmp_path, "trades").rglob("*.parquet")}
    assert dates == {"date=2026-06-16", "date=2026-06-17"}


def test_utf8_and_digit_leading_symbols_round_trip(tmp_path):
    # CJK and digit-leading symbols both exist on Binance USDT-M; the partition
    # path and the in-row value must both survive (no ASCII regex anywhere).
    ws = _ws_ns(datetime(2026, 6, 16, 20, 0, 0, tzinfo=timezone.utc))
    symbols = ["0GUSDT", "1000PEPEUSDT", "我踏马来了USDT"]
    for sym in symbols:
        store.write_snapshots(str(tmp_path), [_snapshot(sym, window_start_ns=ws)])
    got = {r["symbol"] for r in store.read_snapshots(str(tmp_path))}
    assert set(symbols) <= got
    dirs = {p.name for p in pathlib.Path(tmp_path, "trades").glob("symbol=*")}
    assert "symbol=我踏马来了USDT" in dirs
    # Reading back filtered by a UTF-8 symbol works too.
    only = store.read_snapshots(str(tmp_path), symbol="我踏马来了USDT")
    assert only and all(r["symbol"] == "我踏马来了USDT" for r in only)


def test_empty_write_is_noop(tmp_path):
    assert store.write_snapshots(str(tmp_path), []) == []
    assert store.read_snapshots(str(tmp_path)) == []
