"""Tests for the brain depth source (Phase 1 step 3b): reader + within-window
primitive + schema over capture's ``depth_state`` top-N book snapshots.

depth_state is a periodically-sampled top-20 book. The primitive emits RAW,
SEPARABLE within-window primitives under the no-bias guardrail:
  * the per-level raw ladder, levels 2-20 (L1 = best bid/ask is bookTicker's
    domain): per-level price OHLC + per-level qty last/min/max/mean;
  * the full-book (levels 1-20) per-SAMPLE total qty — kept only as max/min (the
    IRRECOVERABLE summaries; the mean total is recoverable from the per-level qty
    means by linearity, so it is omitted);
  * the full-book per-SAMPLE total notional (Σ price·qty) — mean/max/min, all
    irrecoverable (the per-sample product can't be rebuilt from separate price/qty
    summaries, the trades-notional rule);
  * provenance: sample_count, update_id_last, recv_ts_ns (max sample arrival).
Arrival-keyed (event_time_ms = recv // 1e6), forward-only. No engineered signal
(imbalance/ratio/z-score/threshold/slope/mid) at the primitive layer.
"""
from __future__ import annotations

import pytest

from crypto.research.capture_core import store as capture_store
from crypto.research.brain import reader, depth, store, sources, pipeline


_CADENCE_NS = 60 * 1_000_000_000
_T0_MS = 1_781_640_000_000          # 2026-06-16 20:00:00 UTC, a 60s boundary
_R0 = _T0_MS * 1_000_000            # arrival ns at the window start
_5S = 5 * 1_000_000_000


def _clean(*, recv_ns, update_id, bids, asks, symbol="BTCUSDT"):
    """A clean depth row as the reader produces: float (price, qty) tuples."""
    return {
        "recv_ts_ns": recv_ns, "symbol": symbol,
        "event_time_ms": recv_ns // 1_000_000, "update_id": update_id,
        "bids": [(float(p), float(q)) for p, q in bids],
        "asks": [(float(p), float(q)) for p, q in asks],
    }


# Two samples, 3 levels each (so L4-L20 are a thin-book null test simultaneously).
_S1 = _clean(recv_ns=_R0, update_id=101,
             bids=[(100.0, 1.0), (99.0, 2.0), (98.0, 3.0)],
             asks=[(101.0, 1.0), (102.0, 2.0), (103.0, 4.0)])
_S2 = _clean(recv_ns=_R0 + _5S, update_id=105,
             bids=[(100.0, 2.0), (99.0, 5.0), (98.0, 1.0)],
             asks=[(101.0, 3.0), (102.0, 1.0), (103.0, 2.0)])


def _one(snaps):
    assert len(snaps) == 1
    return snaps[0]


# -- core within-window summaries (hand-computed) --

def test_provenance_and_arrival_keying():
    s = _one(depth.bucket_depth([_S1, _S2], cadence_ns=_CADENCE_NS))
    assert s["symbol"] == "BTCUSDT"
    assert s["window_start_ns"] == _R0                     # arrival-keyed to the 60s window
    assert s["window_end_ns"] == _R0 + _CADENCE_NS
    assert s["recv_ts_ns"] == _R0 + _5S                    # max sample arrival
    assert s["sample_count"] == 2
    assert s["update_id_last"] == 105                      # latest sample's venue sequence


def test_per_level_ladder_skips_L1_and_summarizes_each_level():
    s = _one(depth.bucket_depth([_S1, _S2], cadence_ns=_CADENCE_NS))
    assert "bid_l1_price_open" not in s and "ask_l1_qty_last" not in s   # L1 is bookTicker's
    # bid L2 price is constant 99 across both samples; qty 2 -> 5
    assert s["bid_l2_price_open"] == 99.0 and s["bid_l2_price_close"] == 99.0
    assert s["bid_l2_price_high"] == 99.0 and s["bid_l2_price_low"] == 99.0
    assert s["bid_l2_qty_last"] == 5.0 and s["bid_l2_qty_min"] == 2.0
    assert s["bid_l2_qty_max"] == 5.0 and s["bid_l2_qty_mean"] == 3.5
    # bid L3 qty 3 -> 1
    assert s["bid_l3_qty_min"] == 1.0 and s["bid_l3_qty_max"] == 3.0 and s["bid_l3_qty_mean"] == 2.0
    # ask L2 qty 2 -> 1
    assert s["ask_l2_qty_last"] == 1.0 and s["ask_l2_qty_mean"] == 1.5


def test_thin_book_deep_levels_are_null():
    s = _one(depth.bucket_depth([_S1, _S2], cadence_ns=_CADENCE_NS))
    for lvl in (4, 10, 20):
        assert s[f"bid_l{lvl}_price_open"] is None and s[f"bid_l{lvl}_qty_mean"] is None
        assert s[f"ask_l{lvl}_qty_last"] is None


def test_total_qty_keeps_only_irrecoverable_max_min():
    s = _one(depth.bucket_depth([_S1, _S2], cadence_ns=_CADENCE_NS))
    # per-sample totals: bid s1=6, s2=8 ; ask s1=7, s2=6
    assert s["bid_total_qty_max"] == 8.0 and s["bid_total_qty_min"] == 6.0
    assert s["ask_total_qty_max"] == 7.0 and s["ask_total_qty_min"] == 6.0
    assert "bid_total_qty_mean" not in s and "ask_total_qty_mean" not in s  # recoverable -> omitted


def test_total_notional_keeps_mean_max_min_all_irrecoverable():
    s = _one(depth.bucket_depth([_S1, _S2], cadence_ns=_CADENCE_NS))
    # bid notional s1=592, s2=793 ; ask s1=717, s2=611
    assert s["bid_total_notional_max"] == 793.0 and s["bid_total_notional_min"] == 592.0
    assert s["bid_total_notional_mean"] == pytest.approx(692.5)
    assert s["ask_total_notional_max"] == 717.0 and s["ask_total_notional_min"] == 611.0
    assert s["ask_total_notional_mean"] == pytest.approx(664.0)


def test_single_sample_window_summaries_equal_that_sample():
    s = _one(depth.bucket_depth([_S1], cadence_ns=_CADENCE_NS))
    assert s["sample_count"] == 1
    assert s["bid_l2_qty_last"] == 2.0 and s["bid_l2_qty_mean"] == 2.0  # = the single sample
    assert s["bid_total_qty_max"] == 6.0 and s["bid_total_qty_min"] == 6.0


def test_empty_input_emits_no_rows():
    assert depth.bucket_depth([], cadence_ns=_CADENCE_NS) == []


# -- schema is the persistence half of the no-bias line --

def test_full_book_snapshot_keys_match_schema_exactly():
    full = _clean(recv_ns=_R0, update_id=1,
                  bids=[(100.0 - i, 10.0 + i) for i in range(1, 21)],
                  asks=[(101.0 + i, 10.0 + i) for i in range(1, 21)])
    s = _one(depth.bucket_depth([full], cadence_ns=_CADENCE_NS))
    assert set(s.keys()) == set(store.DEPTH_SNAPSHOT_SCHEMA.names)


def test_schema_carries_no_engineered_signal():
    forbidden = ("ratio", "imbalance", "zscore", "z_score", "rank", "norm",
                 "threshold", "thresh", "flag", "signal", "vwap", "mid",
                 "micro", "slope", "pressure", "ofi", "skew", "pct")
    for name in store.DEPTH_SNAPSHOT_SCHEMA.names:
        assert not any(tok in name for tok in forbidden), name
        assert not name.startswith("bid_l1_") and not name.startswith("ask_l1_")
        assert name not in ("bid_total_qty_mean", "ask_total_qty_mean")  # recoverable


# -- reader: capture depth_state -> clean float rows, forward-only --

def test_reader_parses_levels_and_keys_on_arrival(tmp_path):
    w = capture_store.depth_state_writer(str(tmp_path))
    w.append({"recv_ts_ns": _R0, "s": "ETHUSDT", "update_id": 7, "valid": True,
              "b": [["50.0", "1.5"], ["49.0", "2.0"]], "a": [["51.0", "3.0"]]})
    w.flush_all()
    rows = reader.read_new_depth_state(str(tmp_path))
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "ETHUSDT" and r["update_id"] == 7
    assert r["event_time_ms"] == _R0 // 1_000_000             # arrival, forward-only
    assert r["bids"] == [(50.0, 1.5), (49.0, 2.0)] and r["asks"] == [(51.0, 3.0)]


# -- round-trip incl. nulls + registry wiring --

def test_round_trip_write_read_preserves_nulls(tmp_path):
    snaps = depth.bucket_depth([_S1, _S2], cadence_ns=_CADENCE_NS)
    store.write_snapshots(str(tmp_path), sources.DEPTH.dataset,
                          store.DEPTH_SNAPSHOT_SCHEMA, snaps)
    back = store.read_snapshots(str(tmp_path), sources.DEPTH.dataset)
    assert len(back) == 1
    assert back[0]["bid_l2_qty_mean"] == 3.5
    assert back[0]["bid_l20_qty_mean"] is None               # thin book null survives round-trip


def test_depth_spec_wiring():
    # depth is deferred OUT of the runner SOURCES (KI-159), but its SourceSpec stays
    # defined (so the primitive still works and re-wiring is one line).
    spec = sources.DEPTH
    assert spec.event_time_key == "event_time_ms"
    assert spec.read_fn is reader.read_new_depth_state
    assert spec.bucket_fn is depth.bucket_depth
    assert spec.count_fn(depth.bucket_depth([_S1, _S2], cadence_ns=_CADENCE_NS)[0]) == 2


# -- pipeline end-to-end (generic run_once over the depth spec) --

_HUGE_NOW = (_T0_MS + 10 * 86_400_000) * 1_000_000   # 10 days past T0 -> window settled


def _ds_capture_row(recv_ns, update_id, bids, asks, symbol="BTCUSDT"):
    return {"recv_ts_ns": recv_ns, "s": symbol, "update_id": update_id, "valid": True,
            "b": [[str(p), str(q)] for p, q in bids], "a": [[str(p), str(q)] for p, q in asks]}


def _write_capture(root, rows):
    w = capture_store.depth_state_writer(str(root))
    for r in rows:
        w.append(r)
    w.flush_all()


def _run(tmp_path, now_ns=_HUGE_NOW):
    return pipeline.run_once(sources.DEPTH, capture_root=str(tmp_path / "capture"),
                             store_root=str(tmp_path / "brain"),
                             registry_path=str(tmp_path / "brain" / "registry.sqlite"), now_ns=now_ns)


def test_pipeline_end_to_end_round_trip(tmp_path):
    _write_capture(tmp_path / "capture", [
        _ds_capture_row(_R0, 101, [(100.0, 1.0), (99.0, 2.0)], [(101.0, 1.0)]),
        _ds_capture_row(_R0 + _5S, 105, [(100.0, 2.0), (99.0, 5.0)], [(101.0, 3.0)]),
    ])
    assert _run(tmp_path)["snapshots_written"] == 1
    (snap,) = store.read_snapshots(str(tmp_path / "brain"), "depth")
    assert snap["sample_count"] == 2 and snap["update_id_last"] == 105
    assert snap["bid_l2_qty_mean"] == 3.5            # L2 qty 2 -> 5
    assert snap["bid_total_qty_max"] == 7.0          # per-sample totals 3, 7


def test_pipeline_resume_no_double_count(tmp_path):
    _write_capture(tmp_path / "capture",
                   [_ds_capture_row(_R0, 101, [(100.0, 1.0)], [(101.0, 1.0)])])
    assert _run(tmp_path)["snapshots_written"] == 1
    assert _run(tmp_path)["snapshots_written"] == 0
    assert len(store.read_snapshots(str(tmp_path / "brain"), "depth")) == 1
