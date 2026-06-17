"""Tests for the brain pipeline: one inert pass capture -> primitive -> store -> cursor.

Proves the vertical slice end to end against the REAL capture writer, plus the
two correctness properties of an isolated, resumable reader:
  * the settled-window watermark holds back windows that are not yet complete;
  * resume from the persisted cursor double-counts nothing and skips nothing.

Everything writes only to ``tmp_path``; the optional live-capture smoke test
reads the real tape READ-ONLY and still writes only to a temp store.
"""
from __future__ import annotations

import pathlib

import pytest

from crypto.research.capture_core import store as capture_store
from crypto.research.brain import config as cfg
from crypto.research.brain import pipeline, store, reader, trades


_T0_MS = 1_781_640_000_000          # 2026-06-16 20:00:00 UTC, a 60s boundary
_R0 = _T0_MS * 1_000_000            # window-0 recv base (ns), ~ event time
_R1 = (_T0_MS + 60_000) * 1_000_000  # window-1 recv base (ns)

_W0_END_NS = (_T0_MS + 60_000) * 1_000_000
_W1_END_NS = (_T0_MS + 120_000) * 1_000_000


def _agg_row(symbol, *, recv_ns, T_ms, p, q, m, a=1):
    return {
        "recv_ts_ns": recv_ns, "e": "aggTrade", "E": T_ms, "a": a, "s": symbol,
        "p": p, "q": q, "f": 1, "l": 1, "T": T_ms, "m": m,
    }


def _write_capture(root, rows):
    w = capture_store.aggtrade_writer(str(root))
    for r in rows:
        w.append(r)
    w.flush_all()


# Window-0 (older) and window-1 (newer) fixtures; recv order == event order.
_W0_ROWS = [
    _agg_row("BTCUSDT", recv_ns=_R0 + 1, T_ms=_T0_MS + 1_000, p="100", q="2", m=False),  # BUY
    _agg_row("BTCUSDT", recv_ns=_R0 + 2, T_ms=_T0_MS + 2_000, p="101", q="3", m=True),   # SELL
]
_W1_ROWS = [
    _agg_row("BTCUSDT", recv_ns=_R1 + 1, T_ms=_T0_MS + 61_000, p="102", q="5", m=True),   # SELL
    _agg_row("BTCUSDT", recv_ns=_R1 + 2, T_ms=_T0_MS + 62_000, p="103", q="1", m=False),  # BUY
]


def _run(tmp_path, now_ns):
    return pipeline.run_once(
        capture_root=str(tmp_path / "capture"),
        store_root=str(tmp_path / "brain"),
        registry_path=str(tmp_path / "brain" / "registry.sqlite"),
        now_ns=now_ns,
    )


def test_end_to_end_capture_to_store_round_trip(tmp_path):
    _write_capture(tmp_path / "capture", _W0_ROWS + _W1_ROWS)
    # now far past both windows -> both settled.
    now = _W1_END_NS + cfg.BRAIN_WATERMARK_NS
    summary = _run(tmp_path, now)
    assert summary["snapshots_written"] == 2

    snaps = {s["window_start_ns"]: s for s in store.read_snapshots(str(tmp_path / "brain"))}
    w0 = snaps[_T0_MS * 1_000_000]
    assert w0["taker_buy_vol"] == 2.0      # m=False side
    assert w0["taker_sell_vol"] == 3.0     # m=True side
    assert w0["price_open"] == 100.0 and w0["price_close"] == 101.0
    assert w0["trade_count"] == 2
    assert w0["recv_ts_ns"] == _R0 + 2     # provenance = max recv in window


def test_settled_window_watermark_holds_back_incomplete_window(tmp_path):
    _write_capture(tmp_path / "capture", _W0_ROWS + _W1_ROWS)
    # horizon == W0 end: W0 settled, W1 not.
    now = _W0_END_NS + cfg.BRAIN_WATERMARK_NS
    summary = _run(tmp_path, now)
    assert summary["snapshots_written"] == 1

    starts = {s["window_start_ns"] for s in store.read_snapshots(str(tmp_path / "brain"))}
    assert starts == {_T0_MS * 1_000_000}            # only W0
    assert _W0_END_NS not in starts                  # W1 (start == W0 end) held back


def test_resume_emits_held_back_window_with_no_double_count_no_gap(tmp_path):
    _write_capture(tmp_path / "capture", _W0_ROWS + _W1_ROWS)

    # Pass 1: only W0 settled.
    _run(tmp_path, _W0_END_NS + cfg.BRAIN_WATERMARK_NS)
    after_p1 = store.read_snapshots(str(tmp_path / "brain"))
    assert {s["window_start_ns"] for s in after_p1} == {_T0_MS * 1_000_000}

    # Pass 2: now W1 is settled too -> it emits, W0 is NOT re-emitted (no double
    # count) and W1 is NOT skipped (no gap).
    summary2 = _run(tmp_path, _W1_END_NS + cfg.BRAIN_WATERMARK_NS)
    assert summary2["snapshots_written"] == 1
    after_p2 = store.read_snapshots(str(tmp_path / "brain"))
    starts = sorted(s["window_start_ns"] for s in after_p2)
    assert starts == [_T0_MS * 1_000_000, _W0_END_NS]   # exactly W0 + W1, once each

    # Pass 3: identical now -> nothing new (idempotent, no double count).
    summary3 = _run(tmp_path, _W1_END_NS + cfg.BRAIN_WATERMARK_NS)
    assert summary3["snapshots_written"] == 0
    assert len(store.read_snapshots(str(tmp_path / "brain"))) == 2


def test_no_new_data_is_a_clean_noop(tmp_path):
    _write_capture(tmp_path / "capture", _W0_ROWS)
    now = _W0_END_NS + cfg.BRAIN_WATERMARK_NS
    _run(tmp_path, now)
    summary2 = _run(tmp_path, now)
    assert summary2["snapshots_written"] == 0
    assert summary2["rows_read"] == 0   # cursor advanced past W0 -> nothing new to read


def test_live_capture_read_only_smoke(tmp_path):
    """Optional: prove the pipe against REAL captured bytes, read-only.

    Copies ONE real aggTrade part file into a temp capture root (cheap, bounded
    to a single ``symbol=/date=`` partition), then runs it through the real
    reader -> primitive -> store -> round-trip. The live tape is only read (the
    copy), and every write lands under ``tmp_path``. Skipped when no live capture
    data is present (keeps the suite environment-agnostic).
    """
    import shutil

    live = pathlib.Path(cfg.CAPTURE_RAW_DIR, cfg.AGGTRADE_DATASET)
    sample = next(live.rglob("*.parquet"), None) if live.exists() else None
    if sample is None:
        pytest.skip("no live capture aggTrade data present")

    # Recreate just this file's partition path under a temp capture root.
    symbol_dir = sample.parent.parent.name   # symbol=<S>
    date_dir = sample.parent.name            # date=<YYYY-MM-DD>
    dest_dir = tmp_path / "capture" / cfg.AGGTRADE_DATASET / symbol_dir / date_dir
    dest_dir.mkdir(parents=True)
    shutil.copy2(sample, dest_dir / sample.name)

    rows = reader.read_new_aggtrades(str(tmp_path / "capture"))
    assert rows, "the sampled live partition should contain trades"
    # real venue data exercises the casts + clean names + taker flag.
    assert all(isinstance(r["price"], float) and isinstance(r["qty"], float) for r in rows)
    assert all(r["taker_buy"] is (not r["is_buyer_maker"]) for r in rows)

    snaps = trades.bucket_trades(rows, cadence_ns=cfg.BRAIN_BASE_CADENCE_NS)
    store.write_snapshots(str(tmp_path / "brain"), snaps)
    got = store.read_snapshots(str(tmp_path / "brain"))
    assert len(got) == len(snaps)
    # isolation: writes landed only under tmp_path.
    assert pathlib.Path(tmp_path, "brain", "trades").exists()
