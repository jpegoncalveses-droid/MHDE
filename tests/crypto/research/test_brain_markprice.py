"""Tests for the brain markPrice source: reader + within-window primitive + schema.

markPrice carries mark/index/est-settle prices and the funding rate. The central
footgun: the venue ``T`` is the NEXT FUNDING TIME (a future stamp), NOT an event
time — the primitive MUST bucket on ``E`` (event time). Only raw native fields
are summarized; no engineered mark-index premium signal.
"""
from __future__ import annotations

import pytest

from crypto.research.capture_core import store as capture_store
from crypto.research.brain import reader, markprice, store


_CADENCE_NS = 60 * 1_000_000_000
_T0_MS = 1_781_640_000_000          # 2026-06-16 20:00:00 UTC, a 60s boundary
_R0 = _T0_MS * 1_000_000
_NFT = _T0_MS + 8 * 3600 * 1000     # next funding time: 8h in the future

_EXPECTED_COLUMNS = [
    "recv_ts_ns", "symbol", "window_start_ns", "window_end_ns",
    "mark_open", "mark_high", "mark_low", "mark_close",
    "index_open", "index_high", "index_low", "index_close",
    "settle_open", "settle_high", "settle_low", "settle_close",
    "funding_last", "funding_min", "funding_max",
    "next_funding_time_last", "update_count",
]
_FORBIDDEN = [
    "ratio", "imbalance", "zscore", "z_score", "rank", "norm",
    "threshold", "thresh", "flag", "signal", "vwap", "ofi", "cvd",
    "skew", "pct", "percent", "premium",
]


def _mp_capture_row(symbol="BTCUSDT", *, recv_ns, E_ms, p, i, P, r, T_ms=_NFT):
    return {
        "recv_ts_ns": recv_ns, "e": "markPriceUpdate", "E": E_ms, "s": symbol,
        "p": p, "i": i, "P": P, "r": r, "T": T_ms,
    }


def _write_capture(root, rows):
    w = capture_store.markprice_writer(str(root))
    for r in rows:
        w.append(r)
    w.flush_all()


def _clean(*, recv_ns, E_ms, mark, index, settle, funding, nft=_NFT, symbol="BTCUSDT"):
    return {
        "recv_ts_ns": recv_ns, "symbol": symbol, "event_time_ms": E_ms,
        "mark": mark, "index": index, "settle": settle, "funding": funding,
        "next_funding_time_ms": nft,
    }


# -- reader --

def test_reader_casts_varchar_and_keeps_next_funding_time_separate(tmp_path):
    _write_capture(tmp_path, [
        _mp_capture_row("0GUSDT", recv_ns=10, E_ms=_T0_MS + 1000,
                        p="0.30060000", i="0.30114545", P="0.30245174", r="0.00005000"),
    ])
    (row,) = reader.read_new_markprice(str(tmp_path))
    assert set(row) == {"recv_ts_ns", "symbol", "event_time_ms", "mark",
                        "index", "settle", "funding", "next_funding_time_ms"}
    assert row["mark"] == pytest.approx(0.3006) and isinstance(row["mark"], float)
    assert row["index"] == pytest.approx(0.30114545)
    assert row["funding"] == pytest.approx(0.00005)
    assert row["next_funding_time_ms"] == _NFT   # future, kept as a field (int)
    assert row["event_time_ms"] == _T0_MS + 1000


# -- primitive --

def test_buckets_on_event_time_not_future_funding_time():
    # Two different E windows but the SAME next funding time. If the primitive
    # wrongly bucketed on T (the future funding time) all rows would collapse to
    # one (future) window. Bucketing on E must give two windows.
    rows = [
        _clean(recv_ns=_R0 + 1, E_ms=_T0_MS + 1000, mark=100.0, index=99.0, settle=100.0, funding=0.0001),
        _clean(recv_ns=(_T0_MS + 60_000) * 1_000_000 + 1, E_ms=_T0_MS + 61_000,
               mark=110.0, index=99.0, settle=100.0, funding=0.0002),
    ]
    snaps = markprice.bucket_markprice(rows, cadence_ns=_CADENCE_NS)
    starts = sorted(s["window_start_ns"] for s in snaps)
    assert starts == [_T0_MS * 1_000_000, (_T0_MS + 60_000) * 1_000_000]


def test_summarizes_mark_index_settle_funding_and_next_funding_time():
    rows = [
        _clean(recv_ns=_R0 + 1, E_ms=_T0_MS + 1000, mark=100.0, index=99.5, settle=100.2, funding=0.0001),
        _clean(recv_ns=_R0 + 2, E_ms=_T0_MS + 2000, mark=101.0, index=99.6, settle=100.3, funding=0.0002),
        _clean(recv_ns=_R0 + 3, E_ms=_T0_MS + 3000, mark=99.0, index=99.4, settle=100.1, funding=0.00005),
    ]
    (snap,) = markprice.bucket_markprice(rows, cadence_ns=_CADENCE_NS)
    assert (snap["mark_open"], snap["mark_high"], snap["mark_low"], snap["mark_close"]) == (100.0, 101.0, 99.0, 99.0)
    assert (snap["index_open"], snap["index_high"], snap["index_low"], snap["index_close"]) == (99.5, 99.6, 99.4, 99.4)
    assert (snap["settle_open"], snap["settle_high"], snap["settle_low"], snap["settle_close"]) == (100.2, 100.3, 100.1, 100.1)
    assert snap["funding_last"] == pytest.approx(0.00005)
    assert snap["funding_min"] == pytest.approx(0.00005)
    assert snap["funding_max"] == pytest.approx(0.0002)
    assert snap["next_funding_time_last"] == _NFT
    assert snap["update_count"] == 3
    assert snap["recv_ts_ns"] == _R0 + 3


def test_multiple_symbols_and_empty():
    rows = [
        _clean(recv_ns=1, E_ms=_T0_MS + 1000, mark=1.0, index=1.0, settle=1.0, funding=0.0, symbol="BTCUSDT"),
        _clean(recv_ns=2, E_ms=_T0_MS + 1000, mark=1.0, index=1.0, settle=1.0, funding=0.0, symbol="ETHUSDT"),
    ]
    snaps = markprice.bucket_markprice(rows, cadence_ns=_CADENCE_NS)
    assert {s["symbol"] for s in snaps} == {"BTCUSDT", "ETHUSDT"}
    assert markprice.bucket_markprice([], cadence_ns=_CADENCE_NS) == []


# -- no-bias schema --

def test_no_bias_primitive_and_schema():
    rows = [_clean(recv_ns=1, E_ms=_T0_MS + 1000, mark=1.0, index=1.0, settle=1.0, funding=0.0)]
    (snap,) = markprice.bucket_markprice(rows, cadence_ns=_CADENCE_NS)
    assert set(snap.keys()) == set(_EXPECTED_COLUMNS)
    assert list(store.MARKPRICE_SNAPSHOT_SCHEMA.names) == _EXPECTED_COLUMNS
    # No engineered premium / mark-index signal, no ratios.
    for name in snap:
        low = name.lower()
        for bad in _FORBIDDEN:
            assert bad not in low, f"forbidden token {bad!r} in {name!r}"
