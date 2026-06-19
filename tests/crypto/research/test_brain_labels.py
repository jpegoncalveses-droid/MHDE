"""Forward-path LABEL STORE (the label factory) — locked contract.

The label store reads the brain ``markprice`` primitive FORWARD and computes the
exit-agnostic forward-path label per ``(symbol, window, horizon)``:

  ref      = mark_close[t]                         (entry-window close)
  return   = mark_close[t+H] / ref - 1
  MFE      = max(mark_high[t+1 .. t+H]) / ref - 1
  MAE      = min(mark_low [t+1 .. t+H]) / ref - 1

on the 60s grid (H windows forward), for horizons [5,15,30,60,120,240,480,720]
minutes. Three numbers only — no timing, no roughness, no exit/threshold/strategy.

The store is TALL (one record per (symbol, window_start_ns, horizon)), append-only,
written forward-only: a (symbol, window, H) record is emitted ONLY once
``window_end_ns + H*60s <= frontier`` (the latest settled markprice window's
``window_end_ns`` from the registry), and re-running never duplicates it.

Per-horizon validity: a horizon H for (symbol, window) is INVALID if a mark gap
overlaps its forward span ``[window_end_ns, window_end_ns + H*60s]``. Mark gaps are
recorded FLEET-WIDE under the array stream ``!markPrice@arr@<speed>`` (the full
stream string lands in BOTH the symbol and stream columns — see capture
service ``_on_gap``), so the consumer matches by PREFIX ``!markPrice@arr`` and an
overlapping gap invalidates that (window, H) for EVERY symbol — per-horizon, not
whole-window. ``_gaps`` is MILLISECONDS; windows/recv_ts are NANOSECONDS.

Test (f) (missing forward data -> invalid) is a no-bias hardening BEYOND the locked
five: a settled, ungapped window whose forward span has a HOLE in the markprice
store must NOT be emitted valid (the operator's skipped-fragment-is-a-gap rule —
missing data is never allowed to look like a quiet window). Flagged in the PR for
operator ratification.
"""
from __future__ import annotations

import pytest

from crypto.research.brain import labels
from crypto.research.brain import store as brain_store
from crypto.research.brain import registry
from crypto.research.capture_core import store as capture_store

_MIN_NS = 60_000_000_000               # 60s grid == 1 window == 1 minute
_MS_TO_NS = 1_000_000
# 2026-06-16 20:00:00 UTC, as ns — a clean grid origin.
_BASE_NS = 1_781_640_000_000 * _MS_TO_NS
# The REAL recorded mark-gap stream string (MARKPRICE_SPEED == "1s"); the consumer
# must match this by the "!markPrice@arr" prefix, not by exact equality.
_MARK_GAP_STREAM = "!markPrice@arr@1s"


def _w(k: int) -> int:
    """window_start_ns for the k-th window on the 60s grid."""
    return _BASE_NS + k * _MIN_NS


def _mp(symbol, k, *, close, high=None, low=None):
    """A complete MARKPRICE_SNAPSHOT_SCHEMA row for window k (mark OHLC; the rest
    are inert raw fields the label never reads, filled to satisfy the schema)."""
    high = close if high is None else high
    low = close if low is None else low
    return {
        "recv_ts_ns": _w(k) + 59 * _MS_TO_NS, "symbol": symbol,
        "window_start_ns": _w(k), "window_end_ns": _w(k + 1),
        "mark_open": close, "mark_high": high, "mark_low": low, "mark_close": close,
        "index_open": close, "index_high": high, "index_low": low, "index_close": close,
        "settle_open": close, "settle_high": high, "settle_low": low, "settle_close": close,
        "funding_last": 0.0, "funding_min": 0.0, "funding_max": 0.0,
        "next_funding_time_last": 0, "update_count": 1,
    }


def _seed_frontier(registry_path, frontier_end_ns):
    """Record a markprice bookkeeping window so the label store's frontier query
    (MAX(window_end_ns) WHERE dataset='markprice') returns ``frontier_end_ns``."""
    conn = registry.connect(str(registry_path))
    registry.advance(
        conn, "markprice", new_recv_ts_ns=frontier_end_ns,
        bookkeeping=[{
            "dataset": "markprice", "symbol": "AAAUSDT",
            "window_start_ns": frontier_end_ns - _MIN_NS,
            "window_end_ns": frontier_end_ns, "recv_ts_ns": frontier_end_ns,
            "n_events": 1,
        }],
        now_ns=frontier_end_ns,
    )
    conn.close()


def _write_markprice(root, snaps):
    brain_store.write_snapshots(
        str(root), "markprice", brain_store.MARKPRICE_SNAPSHOT_SCHEMA, snaps)


def _write_mark_gap(capture_root, gap_start_ms, gap_end_ms):
    """Record a FLEET-WIDE markPrice gap exactly as capture's ``_on_gap`` does for an
    array stream: the full stream string lands in BOTH the symbol and stream cols."""
    w = capture_store.gap_writer(str(capture_root))
    w.append({
        "symbol": _MARK_GAP_STREAM, "stream": _MARK_GAP_STREAM,
        "gap_start_ms": gap_start_ms, "gap_end_ms": gap_end_ms,
        "reason": "ws_disconnect", "recorded_recv_ts_ns": gap_start_ms * _MS_TO_NS,
    })
    w.flush_all()


def _labels_by_key(rows):
    return {(r["symbol"], r["window_start_ns"], r["horizon_min"]): r for r in rows}


def _run(tmp_path, *, horizons, symbols):
    return labels.run_once(
        store_root=str(tmp_path / "store"), capture_root=str(tmp_path / "capture"),
        registry_path=str(tmp_path / "reg.db"), horizons_min=horizons, symbols=symbols)


def _read_labels(tmp_path):
    return _labels_by_key(
        brain_store.read_snapshots(str(tmp_path / "store"), labels.LABEL_DATASET))


# (a) return / MFE / MAE correct over a known synthetic markprice path -----------

def test_label_for_horizon_computes_return_mfe_mae_over_known_path():
    # closes 100..105; a high spike to 106 at window 5 and a low dip to 98 at window 3.
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    highs = [100.0, 101.0, 102.0, 103.0, 104.0, 106.0]
    lows = [100.0, 99.0, 100.0, 98.0, 102.0, 105.0]
    mark_by_window = {
        _w(k): _mp("AAAUSDT", k, close=closes[k], high=highs[k], low=lows[k])
        for k in range(6)
    }
    ref = closes[0]  # 100.0

    h5 = labels.label_for_horizon(mark_by_window, _w(0), ref, 5)
    assert h5["fwd_return"] == pytest.approx(0.05)          # 105/100 - 1
    assert h5["mfe"] == pytest.approx(0.06)                 # max high 1..5 = 106
    assert h5["mae"] == pytest.approx(-0.02)                # min low 1..5 = 98

    # H=3 is the off-by-one detector: its extrema do NOT sit on the t+H boundary
    # the way H=5's do, so a range that drops t+H would mis-score it.
    h3 = labels.label_for_horizon(mark_by_window, _w(0), ref, 3)
    assert h3["fwd_return"] == pytest.approx(0.03)          # 103/100 - 1
    assert h3["mfe"] == pytest.approx(0.03)                 # max high 1..3 = 103
    assert h3["mae"] == pytest.approx(-0.02)                # min low 1..3 = 98


# (b) fleet-wide markPrice gap -> per-symbol-window, per-horizon invalidation -----

@pytest.mark.parametrize("victim", ["AAAUSDT", "BBBUSDT"])
def test_fleet_gap_invalidates_h15_keeps_h5_for_every_symbol(tmp_path, victim):
    snaps = []
    for sym in ("AAAUSDT", "BBBUSDT"):
        snaps += [_mp(sym, k, close=100.0 + k) for k in range(17)]
    _write_markprice(tmp_path / "store", snaps)
    _seed_frontier(tmp_path / "reg.db", _w(16))

    # window-0 ends at _w(1). h=5 span = [_w(1), _w(6)]; h=15 span = [_w(1), _w(16)].
    # A fleet gap at minutes 10..11 overlaps the h=15 span but NOT the h=5 span.
    _write_mark_gap(tmp_path / "capture", _w(10) // _MS_TO_NS, _w(11) // _MS_TO_NS)

    _run(tmp_path, horizons=[5, 15], symbols=["AAAUSDT", "BBBUSDT"])
    rows = _read_labels(tmp_path)

    # h=5 valid for BOTH symbols; h=15 invalid for BOTH (fleet-wide invalidation).
    assert rows[(victim, _w(0), 5)]["valid"] is True
    assert rows[(victim, _w(0), 15)]["valid"] is False
    # ... yet the h=15 numbers are still computed (data present; only the gap invalidates).
    assert rows[(victim, _w(0), 15)]["fwd_return"] is not None


# (c) settlement gate: not written until frontier passes window_end_ns + H ---------

def test_settlement_gate_writes_only_settled_windows_and_advances(tmp_path):
    _write_markprice(tmp_path / "store",
                     [_mp("AAAUSDT", k, close=100.0 + k) for k in range(12)])

    # window k settles h=5 once frontier >= _w(k+1) + 5*MIN == _w(k+6).
    # Frontier = _w(6): only window 0 settles.
    _seed_frontier(tmp_path / "reg.db", _w(6))
    _run(tmp_path, horizons=[5], symbols=["AAAUSDT"])
    assert set(_read_labels(tmp_path)) == {("AAAUSDT", _w(0), 5)}

    # Advance frontier to _w(7): window 1 now settles; window 0 not re-written.
    _seed_frontier(tmp_path / "reg.db", _w(7))
    _run(tmp_path, horizons=[5], symbols=["AAAUSDT"])
    assert set(_read_labels(tmp_path)) == {("AAAUSDT", _w(0), 5), ("AAAUSDT", _w(1), 5)}


# (d) re-run idempotent: no duplicate (symbol, window, horizon) -------------------

def test_rerun_is_idempotent_no_duplicate_records(tmp_path):
    _write_markprice(tmp_path / "store",
                     [_mp("AAAUSDT", k, close=100.0 + k) for k in range(12)])
    _seed_frontier(tmp_path / "reg.db", _w(8))

    _run(tmp_path, horizons=[5], symbols=["AAAUSDT"])
    first = brain_store.read_snapshots(str(tmp_path / "store"), labels.LABEL_DATASET)
    new2 = _run(tmp_path, horizons=[5], symbols=["AAAUSDT"])
    second = brain_store.read_snapshots(str(tmp_path / "store"), labels.LABEL_DATASET)

    assert new2 == []                                    # nothing newly written
    assert len(second) == len(first)                     # no rows appended
    keys = [(r["symbol"], r["window_start_ns"], r["horizon_min"]) for r in second]
    assert len(keys) == len(set(keys))                   # no duplicates


# (e) guard: label carries ONLY return/MFE/MAE + valid + provenance ---------------

def test_label_schema_carries_only_path_label_and_provenance():
    names = list(labels.LABEL_SCHEMA.names)
    assert names == [
        "recv_ts_ns", "symbol", "window_start_ns", "window_end_ns",   # provenance/bounds
        "horizon_min",                                                # tall key
        "fwd_return", "mfe", "mae", "valid",                          # the label
    ]
    banned = (
        "exit", "entry", "threshold", "strategy", "signal", "stop", "target",
        "pnl", "profit", "loss", "ratio", "imbalance", "zscore", "norm", "side",
        "position", "leverage", "fee", "commission", "score", "prob", "pred",
        "alpha", "edge", "sharpe", "bucket", "class",
    )
    for n in names:
        assert not any(b in n.lower() for b in banned), f"engineered field leaked: {n}"


# (f) NO-BIAS HARDENING: missing forward data -> invalid (never a silent valid) ---

def test_missing_forward_window_marks_invalid_without_a_recorded_gap(tmp_path):
    # Windows 0..11 written EXCEPT window 3 (a hole inside window-0's h=5 span 1..5).
    # No _gaps recorded at all, so the only thing that can invalidate is the hole.
    snaps = [_mp("AAAUSDT", k, close=100.0 + k) for k in range(12) if k != 3]
    _write_markprice(tmp_path / "store", snaps)
    _seed_frontier(tmp_path / "reg.db", _w(11))

    _run(tmp_path, horizons=[5], symbols=["AAAUSDT"])
    rows = _read_labels(tmp_path)

    # window 0: forward span {1,2,3,4,5} has a hole at 3 -> invalid (missing != quiet).
    assert rows[("AAAUSDT", _w(0), 5)]["valid"] is False
    # window 5: forward span {6,7,8,9,10} is intact -> valid. Proves the hole is local.
    assert rows[("AAAUSDT", _w(5), 5)]["valid"] is True
