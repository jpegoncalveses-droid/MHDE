"""Tests for the generic source pipeline: the SOURCES registry + run_once(spec).

The same read -> primitive -> store -> cursor pass drives every source. These
tests exercise the generic plumbing (settled-window watermark, gap-free resume,
per-source dataset + cursor) for the new bookTicker / markPrice / forceOrder
sources, plus a live read-only smoke per source.
"""
from __future__ import annotations

import pathlib
import shutil

import pytest

from crypto.research.capture_core import store as capture_store
from crypto.research.brain import config as cfg, pipeline, sources, store


_T0_MS = 1_781_640_000_000
_W0_END_NS = (_T0_MS + 60_000) * 1_000_000
_W1_END_NS = (_T0_MS + 120_000) * 1_000_000
_R0 = _T0_MS * 1_000_000
_R1 = (_T0_MS + 60_000) * 1_000_000
_HUGE_NOW = 9_999_999_999_999 * 1_000_000  # far past any real event time -> all settled


def _run(tmp_path, spec, now_ns):
    return pipeline.run_once(
        spec,
        capture_root=str(tmp_path / "capture"),
        store_root=str(tmp_path / "brain"),
        registry_path=str(tmp_path / "brain" / "registry.sqlite"),
        now_ns=now_ns,
    )


# -- registry --

def test_sources_registry_has_four_independent_sources():
    assert set(sources.SOURCES) == {"trades", "bookticker", "markprice", "forceorder"}
    for name, spec in sources.SOURCES.items():
        assert spec.dataset == name
        assert callable(spec.read_fn) and callable(spec.bucket_fn)
        assert spec.schema is not None and callable(spec.count_fn)
    # each source has its OWN cursor name
    assert len({s.reader_name for s in sources.SOURCES.values()}) == 4


# -- bookTicker end-to-end + watermark + resume (E-bucketed source) --

def _bt(symbol, *, recv_ns, E_ms, b, B, a, A):
    return {"recv_ts_ns": recv_ns, "e": "bookTicker", "u": 1, "s": symbol,
            "b": b, "B": B, "a": a, "A": A, "T": E_ms, "E": E_ms}


def test_bookticker_end_to_end_and_resume(tmp_path):
    w = capture_store.bookticker_writer(str(tmp_path / "capture"))
    for r in [
        _bt("BTCUSDT", recv_ns=_R0 + 1, E_ms=_T0_MS + 1_000, b="100", B="3", a="101", A="2"),
        _bt("BTCUSDT", recv_ns=_R0 + 2, E_ms=_T0_MS + 2_000, b="102", B="4", a="103", A="1"),
        _bt("BTCUSDT", recv_ns=_R1 + 1, E_ms=_T0_MS + 61_000, b="110", B="5", a="111", A="1"),
    ]:
        w.append(r)
    w.flush_all()

    # Pass 1: only W0 settled.
    s1 = _run(tmp_path, sources.BOOKTICKER, _W0_END_NS + cfg.BRAIN_WATERMARK_NS)
    assert s1["snapshots_written"] == 1
    snaps = store.read_snapshots(str(tmp_path / "brain"), "bookticker")
    assert {s["window_start_ns"] for s in snaps} == {_T0_MS * 1_000_000}
    (w0,) = snaps
    assert w0["bid_open"] == 100.0 and w0["bid_close"] == 102.0 and w0["ask_high"] == 103.0
    assert w0["spread_last"] == 1.0 and w0["update_count"] == 2

    # Pass 2: W1 now settled -> emitted, W0 not re-emitted (no double count / no gap).
    s2 = _run(tmp_path, sources.BOOKTICKER, _W1_END_NS + cfg.BRAIN_WATERMARK_NS)
    assert s2["snapshots_written"] == 1
    starts = sorted(s["window_start_ns"] for s in store.read_snapshots(str(tmp_path / "brain"), "bookticker"))
    assert starts == [_T0_MS * 1_000_000, _W0_END_NS]

    # Pass 3: idempotent.
    s3 = _run(tmp_path, sources.BOOKTICKER, _W1_END_NS + cfg.BRAIN_WATERMARK_NS)
    assert s3["snapshots_written"] == 0


# -- markPrice end-to-end (confirms E-bucketing through the generic pipeline) --

def test_markprice_end_to_end(tmp_path):
    w = capture_store.markprice_writer(str(tmp_path / "capture"))
    nft = _T0_MS + 8 * 3600 * 1000
    for r in [
        {"recv_ts_ns": _R0 + 1, "e": "markPriceUpdate", "E": _T0_MS + 1_000, "s": "BTCUSDT",
         "p": "100.0", "i": "99.5", "P": "100.2", "r": "0.0001", "T": nft},
        {"recv_ts_ns": _R0 + 2, "e": "markPriceUpdate", "E": _T0_MS + 2_000, "s": "BTCUSDT",
         "p": "101.0", "i": "99.6", "P": "100.3", "r": "0.0002", "T": nft},
    ]:
        w.append(r)
    w.flush_all()
    summary = _run(tmp_path, sources.MARKPRICE, _W0_END_NS + cfg.BRAIN_WATERMARK_NS)
    assert summary["snapshots_written"] == 1
    (snap,) = store.read_snapshots(str(tmp_path / "brain"), "markprice")
    assert snap["mark_open"] == 100.0 and snap["mark_close"] == 101.0
    assert snap["funding_max"] == pytest.approx(0.0002)
    assert snap["next_funding_time_last"] == nft


# -- forceOrder end-to-end --

def test_forceorder_end_to_end(tmp_path):
    w = capture_store.forceorder_writer(str(tmp_path / "capture"))
    for r in [
        {"recv_ts_ns": _R0 + 1, "E": _T0_MS + 1_000, "s": "BTCUSDT", "S": "SELL", "o": "LIMIT",
         "f": "IOC", "q": "10", "p": "2.0", "ap": "2.0", "X": "FILLED", "l": "10", "z": "10", "T": _T0_MS + 1_000},
        {"recv_ts_ns": _R0 + 2, "E": _T0_MS + 2_000, "s": "BTCUSDT", "S": "BUY", "o": "LIMIT",
         "f": "IOC", "q": "3", "p": "5.0", "ap": "5.0", "X": "FILLED", "l": "3", "z": "3", "T": _T0_MS + 2_000},
    ]:
        w.append(r)
    w.flush_all()
    summary = _run(tmp_path, sources.FORCEORDER, _W0_END_NS + cfg.BRAIN_WATERMARK_NS)
    assert summary["snapshots_written"] == 1
    (snap,) = store.read_snapshots(str(tmp_path / "brain"), "forceorder")
    assert snap["liq_sell_vol"] == 10.0 and snap["liq_buy_vol"] == 3.0
    assert snap["liq_buy_quote_vol"] == 15.0 and snap["liq_sell_quote_vol"] == 20.0


# -- live read-only smoke, per source --

@pytest.mark.parametrize("spec_name,read_fn_name", [
    ("BOOKTICKER", "read_new_bookticker"),
    ("MARKPRICE", "read_new_markprice"),
])
def test_live_smoke_bookticker_markprice(tmp_path, spec_name, read_fn_name):
    spec = getattr(sources, spec_name)
    live = pathlib.Path(cfg.CAPTURE_RAW_DIR, _capture_dir(spec))
    sample = next(live.rglob("*.parquet"), None) if live.exists() else None
    if sample is None:
        pytest.skip(f"no live {spec.dataset} data present")
    dest = tmp_path / "capture" / _capture_dir(spec) / sample.parent.parent.name / sample.parent.name
    dest.mkdir(parents=True)
    shutil.copy2(sample, dest / sample.name)

    rows = spec.read_fn(str(tmp_path / "capture"))
    assert rows, "sampled live partition should contain rows"
    snaps = spec.bucket_fn(rows, cadence_ns=cfg.BRAIN_BASE_CADENCE_NS)
    store.write_snapshots(str(tmp_path / "brain"), spec.dataset, spec.schema, snaps)
    assert len(store.read_snapshots(str(tmp_path / "brain"), spec.dataset)) == len(snaps)


def test_live_smoke_forceorder_tolerates_sparsity(tmp_path):
    # Liquidations are extremely sparse; tolerate empty / skip-if-absent.
    spec = sources.FORCEORDER
    live = pathlib.Path(cfg.CAPTURE_RAW_DIR, cfg.FORCEORDER_CAPTURE_DATASET)
    sample = next(live.rglob("*.parquet"), None) if live.exists() else None
    if sample is None:
        pytest.skip("no live forceOrder data present")
    dest = tmp_path / "capture" / cfg.FORCEORDER_CAPTURE_DATASET / sample.parent.parent.name / sample.parent.name
    dest.mkdir(parents=True)
    shutil.copy2(sample, dest / sample.name)
    rows = spec.read_fn(str(tmp_path / "capture"))
    # Validate the raw side domain rather than a row count.
    assert all(r["side"] in {"BUY", "SELL"} for r in rows)
    snaps = spec.bucket_fn(rows, cadence_ns=cfg.BRAIN_BASE_CADENCE_NS)
    store.write_snapshots(str(tmp_path / "brain"), spec.dataset, spec.schema, snaps)
    assert len(store.read_snapshots(str(tmp_path / "brain"), spec.dataset)) == len(snaps)


def _capture_dir(spec):
    return {
        "bookticker": cfg.BOOKTICKER_CAPTURE_DATASET,
        "markprice": cfg.MARKPRICE_CAPTURE_DATASET,
        "forceorder": cfg.FORCEORDER_CAPTURE_DATASET,
        "trades": cfg.AGGTRADE_DATASET,
    }[spec.dataset]
