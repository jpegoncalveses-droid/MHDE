"""Tests for the seven as-of SourceSpecs: per-dataset NO-BIAS schema whitelist,
generic-pipeline end-to-end, and a live read-only smoke per series.
"""
from __future__ import annotations

import pathlib
import shutil

import pytest

from crypto.research.capture_core import store as capture_store, rest_series
from crypto.research.brain import config as cfg, pipeline, sources, store


_T0_MS = 1_781_640_000_000
_R0 = _T0_MS * 1_000_000
_W0_END_NS = (_T0_MS + 60_000) * 1_000_000
_HUGE_NOW = 2_000_000_000_000 * 1_000_000  # ~2e18 ns: past any real event time, fits int64

_PROV = ["recv_ts_ns", "symbol", "window_start_ns", "window_end_ns", "asof_event_time_ms"]
_LS = ["long_account", "short_account", "long_short_ratio"]
_EXPECTED = {
    "open_interest": _PROV + ["open_interest"],
    "premium_index": _PROV + ["mark_price", "index_price", "estimated_settle_price",
                              "last_funding_rate", "interest_rate", "next_funding_time"],
    "global_ls_account": _PROV + _LS,
    "top_ls_account": _PROV + _LS,
    "top_ls_position": _PROV + _LS,
    "taker_ls_ratio": _PROV + ["buy_sell_ratio", "buy_vol", "sell_vol"],
    "basis": _PROV + ["index_price", "futures_price", "basis", "basis_rate", "annualized_basis_rate"],
}
_ENGINEERED = ["imbalance", "zscore", "z_score", "rank", "norm", "threshold",
               "thresh", "flag", "signal", "vwap", "ofi", "cvd", "skew", "pct", "percent"]


def _run(tmp_path, spec, now_ns=_HUGE_NOW):
    return pipeline.run_once(
        spec, capture_root=str(tmp_path / "capture"), store_root=str(tmp_path / "brain"),
        registry_path=str(tmp_path / "brain" / "registry.sqlite"), now_ns=now_ns)


# -- registry + per-dataset no-bias schema --

def test_asof_registry_has_seven_independent_sources():
    assert len(sources.ASOF_SOURCES) == 7
    assert {s.dataset for s in sources.ASOF_SOURCES} == set(_EXPECTED)
    for s in sources.ASOF_SOURCES:
        assert s.dataset in sources.SOURCES
    assert len({s.reader_name for s in sources.ASOF_SOURCES}) == 7


@pytest.mark.parametrize("spec", sources.ASOF_SOURCES, ids=lambda s: s.dataset)
def test_no_bias_schema_is_native_fields_only(spec):
    assert list(spec.schema.names) == _EXPECTED[spec.dataset]
    # native ratios/rates are RAW and allowed; no ENGINEERED token may appear.
    for name in spec.schema.names:
        low = name.lower()
        for bad in _ENGINEERED:
            assert bad not in low, f"engineered token {bad!r} in {spec.dataset}.{name}"


def test_adversarial_computed_column_would_break_whitelist():
    # If a normalized/zscore column were added to a schema, the exact-name
    # whitelist comparison would fail (this is what guards each schema).
    bad = list(_EXPECTED["open_interest"]) + ["open_interest_zscore"]
    assert list(store.OPEN_INTEREST_SNAPSHOT_SCHEMA.names) != bad


# -- generic pipeline end-to-end (open_interest: simple; basis: pair + '') --

def _write_capture(root, dataset, schema, symbol_key, time_key, rows):
    w = capture_store.dataset_writer(str(root / "capture"), dataset, schema,
                                     symbol_key=symbol_key, time_key=time_key)
    for r in rows:
        w.append(r)
    w.flush_all()


def test_open_interest_end_to_end(tmp_path):
    _write_capture(tmp_path, "open_interest", rest_series.OPEN_INTEREST_SCHEMA, "s", "time", [
        {"recv_ts_ns": _R0 + 1, "s": "BTCUSDT", "openInterest": "98672.073", "time": _T0_MS + 1000},
    ])
    summary = _run(tmp_path, sources.OPEN_INTEREST)
    assert summary["snapshots_written"] == 1
    (snap,) = store.read_snapshots(str(tmp_path / "brain"), "open_interest")
    assert snap["open_interest"] == 98672.073
    assert snap["asof_event_time_ms"] == _T0_MS + 1000
    assert snap["window_start_ns"] == _T0_MS * 1_000_000


def test_premium_index_end_to_end_keeps_int_next_funding_time(tmp_path):
    nft = _T0_MS + 8 * 3600_000
    _write_capture(tmp_path, "premium_index", rest_series.PREMIUM_INDEX_SCHEMA, "s", "time", [
        {"recv_ts_ns": _R0 + 1, "s": "BTCUSDT", "markPrice": "63715.4", "indexPrice": "63744.5",
         "estimatedSettlePrice": "63918.1", "lastFundingRate": "0.00006033", "interestRate": "0.0001",
         "nextFundingTime": nft, "time": _T0_MS + 1000},
    ])
    summary = _run(tmp_path, sources.PREMIUM_INDEX)
    assert summary["snapshots_written"] == 1
    (snap,) = store.read_snapshots(str(tmp_path / "brain"), "premium_index")
    assert snap["last_funding_rate"] == 0.00006033
    assert snap["next_funding_time"] == nft and isinstance(snap["next_funding_time"], int)


def test_basis_end_to_end_pair_and_empty_string_null(tmp_path):
    _write_capture(tmp_path, "basis", rest_series.BASIS_SCHEMA, "pair", "timestamp", [
        {"recv_ts_ns": _R0 + 1, "pair": "BTCUSDT", "contractType": "PERPETUAL",
         "indexPrice": "63826.4", "futuresPrice": "63807.7", "basis": "-18.7",
         "basisRate": "-0.0003", "annualizedBasisRate": "", "timestamp": _T0_MS + 1000},
    ])
    summary = _run(tmp_path, sources.BASIS)
    assert summary["snapshots_written"] == 1
    (snap,) = store.read_snapshots(str(tmp_path / "brain"), "basis")
    assert snap["symbol"] == "BTCUSDT"             # from 'pair'
    assert snap["basis"] == -18.7
    assert snap["annualized_basis_rate"] is None    # '' -> null


def test_asof_resume_no_double_count(tmp_path):
    _write_capture(tmp_path, "open_interest", rest_series.OPEN_INTEREST_SCHEMA, "s", "time", [
        {"recv_ts_ns": _R0 + 1, "s": "BTCUSDT", "openInterest": "100", "time": _T0_MS + 1000},
    ])
    s1 = _run(tmp_path, sources.OPEN_INTEREST, _W0_END_NS + cfg.BRAIN_WATERMARK_NS)
    assert s1["snapshots_written"] == 1
    s2 = _run(tmp_path, sources.OPEN_INTEREST, _W0_END_NS + cfg.BRAIN_WATERMARK_NS)
    assert s2["snapshots_written"] == 0  # idempotent, no double count
    assert len(store.read_snapshots(str(tmp_path / "brain"), "open_interest")) == 1


# -- live read-only smoke, all seven series --

@pytest.mark.parametrize("spec", sources.ASOF_SOURCES, ids=lambda s: s.dataset)
def test_live_smoke_asof(tmp_path, spec):
    live = pathlib.Path(cfg.CAPTURE_RAW_DIR, spec.dataset)  # capture dir == dataset name
    sample = next(live.rglob("*.parquet"), None) if live.exists() else None
    if sample is None:
        pytest.skip(f"no live {spec.dataset} data present")
    dest = tmp_path / "capture" / spec.dataset / sample.parent.parent.name / sample.parent.name
    dest.mkdir(parents=True)
    shutil.copy2(sample, dest / sample.name)

    rows = spec.read_fn(str(tmp_path / "capture"))
    assert rows, f"sampled live {spec.dataset} partition should contain rows"
    snaps = spec.bucket_fn(rows, cadence_ns=cfg.BRAIN_BASE_CADENCE_NS)
    store.write_snapshots(str(tmp_path / "brain"), spec.dataset, spec.schema, snaps)
    assert len(store.read_snapshots(str(tmp_path / "brain"), spec.dataset)) == len(snaps)
