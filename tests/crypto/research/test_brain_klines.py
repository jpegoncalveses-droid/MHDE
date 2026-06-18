"""Tests for the brain klines_1h source: the hourly-context bar as a multi-field
as-of source.

Same as-of mechanism as the 2b scalar series, but the value is the hourly bar's
native fields. THE load-bearing point: the as-of keys on recv_ts_ns (ARRIVAL),
NOT the bar's closeTime — a 1h bar is REST-backfilled, so its closeTime can
precede the recv_ts_ns at which the brain observed it; keying on closeTime would
hand the brain a bar before it was available (lookahead). No momentum/returns/MA
(Phase 3 composes those over the stored bar history).
"""
from __future__ import annotations

import pathlib
import shutil

import pytest

from crypto.research.capture_core import store as capture_store, klines_store
from crypto.research.brain import reader, asof, store, sources, config as cfg, pipeline


_CADENCE_NS = 60 * 1_000_000_000
_T0_MS = 1_781_640_000_000
_HUGE_NOW = 2_000_000_000_000 * 1_000_000  # past any real arrival, fits int64

_VALUE_FIELDS = ["open", "high", "low", "close", "volume", "quote_volume",
                 "trades", "taker_buy_base", "taker_buy_quote", "open_time", "close_time"]
_EXPECTED_SCHEMA = ["recv_ts_ns", "symbol", "window_start_ns", "window_end_ns",
                    "asof_event_time_ms"] + _VALUE_FIELDS
_FORBIDDEN = [
    "return", "momentum", "trend", "roc", "sma", "ema", "ma_", "_ma", "moving",
    "rolling", "lag", "ratio", "imbalance", "zscore", "z_score", "rank", "norm",
    "threshold", "thresh", "flag", "signal", "vwap", "ofi", "cvd", "skew", "pct", "percent",
]


def _kline_capture_row(symbol="BTCUSDT", *, recv_ns, openTime, closeTime,
                       o="100", h="105", l="99", c="101", vol="10",
                       qvol="1000", trades=42, tbb="6", tbq="600"):
    return {"recv_ts_ns": recv_ns, "s": symbol, "openTime": openTime, "open": o,
            "high": h, "low": l, "close": c, "volume": vol, "closeTime": closeTime,
            "quoteVolume": qvol, "trades": trades, "takerBuyBase": tbb, "takerBuyQuote": tbq}


def _write_klines(root, rows):
    w = capture_store.dataset_writer(str(root), "klines_1h", klines_store.KLINES_1H_SCHEMA,
                                     symbol_key="s", time_key="openTime")
    for r in rows:
        w.append(r)
    w.flush_all()


def _clean(*, recv_ns, event_time_ms, close_time, open_time=None, symbol="BTCUSDT", **vals):
    row = {"recv_ts_ns": recv_ns, "symbol": symbol, "event_time_ms": event_time_ms,
           "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
           "quote_volume": 1.0, "trades": 1, "taker_buy_base": 1.0, "taker_buy_quote": 1.0,
           "open_time": open_time if open_time is not None else close_time - 3600_000,
           "close_time": close_time}
    row.update(vals)
    return row


# -- reader: forward-only (event_time == recv arrival, NOT bar time) --

def test_reader_event_time_is_recv_arrival_not_bar_time(tmp_path):
    close_time = _T0_MS - 1                  # bar closed just before _T0
    recv_ms = _T0_MS + 200_000               # but observed ~3 min after _T0
    _write_klines(tmp_path, [_kline_capture_row(
        recv_ns=recv_ms * 1_000_000, openTime=_T0_MS - 3600_000, closeTime=close_time)])
    (row,) = reader.read_new_klines(str(tmp_path))
    assert row["event_time_ms"] == recv_ms            # ARRIVAL, not the bar's time
    assert row["close_time"] == close_time
    assert row["event_time_ms"] != row["close_time"]  # the forward-only gap


def test_reader_casts_varchar_and_ints_and_clean_names(tmp_path):
    _write_klines(tmp_path, [_kline_capture_row(
        "我踏马来了USDT", recv_ns=10 * 1_000_000, openTime=_T0_MS - 3600_000, closeTime=_T0_MS - 1,
        o="100.5", h="106.5", l="99.5", c="101.5", vol="3.5", qvol="350.0", trades=7,
        tbb="2.0", tbq="200.0")])
    (row,) = reader.read_new_klines(str(tmp_path))
    assert set(row) == {"recv_ts_ns", "symbol", "event_time_ms"} | set(_VALUE_FIELDS)
    assert row["open"] == 100.5 and isinstance(row["open"], float)
    assert row["close"] == 101.5 and row["volume"] == 3.5 and row["quote_volume"] == 350.0
    assert row["taker_buy_base"] == 2.0 and row["taker_buy_quote"] == 200.0
    assert row["trades"] == 7 and isinstance(row["trades"], int)
    assert row["open_time"] == _T0_MS - 3600_000 and isinstance(row["open_time"], int)
    assert row["symbol"] == "我踏马来了USDT"


def test_reader_recv_order_and_cursor(tmp_path):
    _write_klines(tmp_path, [
        _kline_capture_row(recv_ns=300, openTime=_T0_MS, closeTime=_T0_MS + 3599_999),
        _kline_capture_row(recv_ns=100, openTime=_T0_MS - 7200_000, closeTime=_T0_MS - 3600_001),
        _kline_capture_row(recv_ns=200, openTime=_T0_MS - 3600_000, closeTime=_T0_MS - 1),
    ])
    rows = reader.read_new_klines(str(tmp_path))
    assert [r["recv_ts_ns"] for r in rows] == [100, 200, 300]
    after = reader.read_new_klines(str(tmp_path), after_recv_ts_ns=150)
    assert [r["recv_ts_ns"] for r in after] == [200, 300]


# -- primitive: window keyed on arrival; sparse; batch tie-break --

def test_window_keyed_on_recv_arrival_not_closetime():
    close_time = _T0_MS - 1
    recv_ms = _T0_MS + 200_000
    rows = [_clean(recv_ns=recv_ms * 1_000_000, event_time_ms=recv_ms, close_time=close_time)]
    (snap,) = asof.bucket_asof(rows, cadence_ns=_CADENCE_NS, value_fields=_VALUE_FIELDS,
                               tiebreak_fields=("close_time",))
    assert snap["window_start_ns"] == (recv_ms // 60_000) * 60_000 * 1_000_000
    assert snap["window_start_ns"] != (close_time // 60_000) * 60_000 * 1_000_000  # NOT closeTime
    assert snap["asof_event_time_ms"] == recv_ms
    assert snap["close_time"] == close_time


def test_sparse_storage_one_snapshot_per_arrival_window():
    # two bars arriving in two different 60s windows -> two snapshots (no dense
    # per-60s repetition / forward-fill at write).
    rows = [
        _clean(recv_ns=(_T0_MS + 1_000) * 1_000_000, event_time_ms=_T0_MS + 1_000, close_time=_T0_MS - 1),
        _clean(recv_ns=(_T0_MS + 65_000) * 1_000_000, event_time_ms=_T0_MS + 65_000, close_time=_T0_MS + 3599_999),
    ]
    snaps = asof.bucket_asof(rows, cadence_ns=_CADENCE_NS, value_fields=_VALUE_FIELDS,
                             tiebreak_fields=("close_time",))
    assert len(snaps) == 2  # not 65+ dense windows


def test_batch_same_recv_keeps_highest_closetime_bar():
    # a backfill page delivers many bars at ONE recv -> the as-of for that arrival
    # window is the most recent (highest closeTime) bar, deterministically.
    recv_ns = (_T0_MS + 1_000) * 1_000_000
    rows = [
        _clean(recv_ns=recv_ns, event_time_ms=_T0_MS + 1_000, close_time=_T0_MS - 7200_000, close=10.0),
        _clean(recv_ns=recv_ns, event_time_ms=_T0_MS + 1_000, close_time=_T0_MS - 1, close=99.0),
        _clean(recv_ns=recv_ns, event_time_ms=_T0_MS + 1_000, close_time=_T0_MS - 3600_000, close=50.0),
    ]
    (snap,) = asof.bucket_asof(rows, cadence_ns=_CADENCE_NS, value_fields=_VALUE_FIELDS,
                               tiebreak_fields=("close_time",))
    assert snap["close_time"] == _T0_MS - 1   # highest closeTime
    assert snap["close"] == 99.0


# -- store schema NO-BIAS --

def test_no_bias_schema_native_bar_fields_only():
    assert list(store.KLINES_SNAPSHOT_SCHEMA.names) == _EXPECTED_SCHEMA
    for name in store.KLINES_SNAPSHOT_SCHEMA.names:
        low = name.lower()
        for bad in _FORBIDDEN:
            assert bad not in low, f"forbidden token {bad!r} in klines.{name}"


def test_no_bias_scan_catches_engineered_columns():
    for bad in ["return_1h", "ma_50", "close_ema", "roc_3", "vol_zscore"]:
        assert any(b in bad for b in _FORBIDDEN), bad
    for ok in ["open", "close", "volume", "taker_buy_quote", "open_time", "close_time"]:
        assert not any(b in ok for b in _FORBIDDEN), ok


# -- pipeline end-to-end: multi-field round-trip + resume --

def _run(tmp_path, now_ns=_HUGE_NOW):
    return pipeline.run_once(sources.KLINES, capture_root=str(tmp_path / "capture"),
                             store_root=str(tmp_path / "brain"),
                             registry_path=str(tmp_path / "brain" / "registry.sqlite"), now_ns=now_ns)


def test_end_to_end_multi_field_round_trip(tmp_path):
    _write_klines(tmp_path / "capture", [_kline_capture_row(
        recv_ns=(_T0_MS + 1_000) * 1_000_000, openTime=_T0_MS - 3600_000, closeTime=_T0_MS - 1,
        o="100", h="105", l="99", c="101", vol="10", qvol="1000", trades=42, tbb="6", tbq="600")])
    summary = _run(tmp_path)
    assert summary["snapshots_written"] == 1
    (snap,) = store.read_snapshots(str(tmp_path / "brain"), "klines_1h")
    assert (snap["open"], snap["high"], snap["low"], snap["close"]) == (100.0, 105.0, 99.0, 101.0)
    assert snap["volume"] == 10.0 and snap["quote_volume"] == 1000.0 and snap["trades"] == 42
    assert snap["taker_buy_base"] == 6.0 and snap["taker_buy_quote"] == 600.0
    assert snap["open_time"] == _T0_MS - 3600_000 and snap["close_time"] == _T0_MS - 1


def test_resume_no_double_count(tmp_path):
    _write_klines(tmp_path / "capture", [_kline_capture_row(
        recv_ns=(_T0_MS + 1_000) * 1_000_000, openTime=_T0_MS - 3600_000, closeTime=_T0_MS - 1)])
    assert _run(tmp_path)["snapshots_written"] == 1
    assert _run(tmp_path)["snapshots_written"] == 0
    assert len(store.read_snapshots(str(tmp_path / "brain"), "klines_1h")) == 1


def test_klines_in_sources_registry():
    assert "klines_1h" in sources.SOURCES
    assert len(sources.SOURCES) == 13   # + depth (step 3b)


def test_live_smoke_klines(tmp_path):
    live = pathlib.Path(cfg.CAPTURE_RAW_DIR, cfg.KLINES_CAPTURE_DATASET)
    sample = next(live.rglob("*.parquet"), None) if live.exists() else None
    if sample is None:
        pytest.skip("no live klines_1h data present")
    dest = tmp_path / "capture" / cfg.KLINES_CAPTURE_DATASET / sample.parent.parent.name / sample.parent.name
    dest.mkdir(parents=True)
    shutil.copy2(sample, dest / sample.name)

    rows = reader.read_new_klines(str(tmp_path / "capture"))
    assert rows, "sampled live klines partition should contain bars"
    # forward-only: event_time is recv ARRIVAL, never the bar's close_time
    assert all(r["event_time_ms"] == r["recv_ts_ns"] // 1_000_000 for r in rows)
    snaps = sources.KLINES.bucket_fn(rows, cadence_ns=cfg.BRAIN_BASE_CADENCE_NS)
    store.write_snapshots(str(tmp_path / "brain"), "klines_1h", store.KLINES_SNAPSHOT_SCHEMA, snaps)
    assert len(store.read_snapshots(str(tmp_path / "brain"), "klines_1h")) == len(snaps)
