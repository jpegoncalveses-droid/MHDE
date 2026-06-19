"""Full-universe CHUNKED pass (`pipeline.run_pass`) — the first runner piece.

`run_once` over the full universe loads every symbol's in-range slice at once
(unbounded memory). `run_pass` processes the universe in symbol-batches of K, each
batch a bounded (symbol+date-pruned) read, so peak memory is one batch.

The load-bearing invariant is the per-source CURSOR. Every batch reads at the SAME
`cursor_before`; the cursor advances exactly ONCE after all batches, to the global
frontier `min(max_settled, min_pending-1)` over the union of all batches' rows.
Advancing mid-pass would make a later batch (different symbols) read past its own
unprocessed rows — a silent permanent gap, since `seen_windows` is per-symbol and
cannot protect a symbol set whose rows were skipped. Re-run safety: the cursor only
moves after the full pass, so a mid-pass crash re-does the pass; each completed
batch records its bookkeeping immediately (`registry.record_windows`), so the
`seen_windows` gate makes the re-write a no-op (no duplicate primitives).
"""
from __future__ import annotations

import dataclasses
from collections import Counter

import pytest

from crypto.research.capture_core import store as capture_store
from crypto.research.brain import config as cfg
from crypto.research.brain import pipeline, store, sources, registry

_T0_MS = 1_781_640_000_000               # 2026-06-16 20:00:00 UTC, a 60s boundary
_R0 = _T0_MS * 1_000_000                 # window-0 recv base (ns)
_R1 = (_T0_MS + 60_000) * 1_000_000      # window-1 recv base (ns)
_W0_END_NS = (_T0_MS + 60_000) * 1_000_000
_W1_END_NS = (_T0_MS + 120_000) * 1_000_000
_W0_START_NS = _T0_MS * 1_000_000

SYMS = ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"]


def _agg_row(symbol, *, recv_ns, T_ms, p="100", q="2", m=False, a=1):
    return {"recv_ts_ns": recv_ns, "e": "aggTrade", "E": T_ms, "a": a, "s": symbol,
            "p": p, "q": q, "f": 1, "l": 1, "T": T_ms, "m": m}


def _write_capture(root, rows):
    w = capture_store.aggtrade_writer(str(root))
    for r in rows:
        w.append(r)
    w.flush_all()


def _settled_rows():
    """One settled window-0 row per symbol (distinct recv so order is deterministic)."""
    return [_agg_row(s, recv_ns=_R0 + 10 + i, T_ms=_T0_MS + 1_000 + i) for i, s in enumerate(SYMS)]


def _spy_read(real, calls):
    def _f(capture_root, after_recv_ts_ns=0, symbols=None):
        calls.append(list(symbols) if symbols is not None else None)
        return real(capture_root, after_recv_ts_ns=after_recv_ts_ns, symbols=symbols)
    return _f


def _run_pass(tmp_path, *, now_ns, symbols=None, batch_size=2, spec=sources.TRADES, store_name="brain"):
    return pipeline.run_pass(
        spec, capture_root=str(tmp_path / "capture"),
        store_root=str(tmp_path / store_name),
        registry_path=str(tmp_path / store_name / "registry.sqlite"),
        now_ns=now_ns, symbols=symbols, batch_size=batch_size)


def _snap_counter(tmp_path, store_name="brain"):
    return Counter((s["symbol"], s["window_start_ns"])
                   for s in store.read_snapshots(str(tmp_path / store_name), "trades"))


# T1 — chunked pass is identical to the single full-universe read --------------------

def test_chunked_pass_identical_to_single_read(tmp_path):
    _write_capture(tmp_path / "capture", _settled_rows())
    now = _W0_END_NS + cfg.BRAIN_WATERMARK_NS
    # oracle: the existing single-read run_once over all symbols at once.
    pipeline.run_once(sources.TRADES, capture_root=str(tmp_path / "capture"),
                      store_root=str(tmp_path / "oracle"),
                      registry_path=str(tmp_path / "oracle" / "registry.sqlite"), now_ns=now)
    # chunked: run_pass with batch_size=2 (enumerated universe), fresh store + registry.
    _run_pass(tmp_path, now_ns=now, symbols=None, batch_size=2)

    def by_key(name):
        return {(s["symbol"], s["window_start_ns"]): s
                for s in store.read_snapshots(str(tmp_path / name), "trades")}
    oracle, chunk = by_key("oracle"), by_key("brain")
    assert set(chunk) == set(oracle) and len(chunk) == 4         # same windows, no missing
    assert all(chunk[k] == oracle[k] for k in oracle)            # identical snapshot VALUES
    # multiset (not set): a duplicated window would repeat in read_snapshots.
    assert all(v == 1 for v in _snap_counter(tmp_path).values())


# T2 — the pass reads one batch at a time, never all symbols at once ------------------

def test_pass_reads_one_batch_at_a_time_never_all(tmp_path):
    _write_capture(tmp_path / "capture", _settled_rows())
    now = _W0_END_NS + cfg.BRAIN_WATERMARK_NS
    calls = []
    spec = dataclasses.replace(sources.TRADES, read_fn=_spy_read(sources.TRADES.read_fn, calls))
    _run_pass(tmp_path, now_ns=now, symbols=SYMS, batch_size=2, spec=spec)

    assert len(calls) == 2                                       # ceil(4 / 2) batches
    assert all(c is not None and len(c) <= 2 for c in calls)     # each read <= K symbols
    assert not any(len(c) == 4 for c in calls)                   # never all at once
    assert sorted(s for c in calls for s in c) == sorted(SYMS)   # union covers all, no overlap


def test_single_batch_when_batch_size_exceeds_universe(tmp_path):
    _write_capture(tmp_path / "capture", _settled_rows())
    now = _W0_END_NS + cfg.BRAIN_WATERMARK_NS
    calls = []
    spec = dataclasses.replace(sources.TRADES, read_fn=_spy_read(sources.TRADES.read_fn, calls))
    _run_pass(tmp_path, now_ns=now, symbols=SYMS, batch_size=10, spec=spec)
    assert len(calls) == 1 and sorted(calls[0]) == sorted(SYMS)  # one batch holds the whole universe


# T3 — the source cursor advances exactly once, after the full pass ------------------

def test_cursor_advances_once_after_the_full_pass(tmp_path, monkeypatch):
    _write_capture(tmp_path / "capture", _settled_rows())
    now = _W0_END_NS + cfg.BRAIN_WATERMARK_NS
    advance_calls, record_calls = [], []
    real_advance, real_record = registry.advance, registry.record_windows

    def spy_advance(conn, reader, *, new_recv_ts_ns, bookkeeping=(), now_ns=0):
        advance_calls.append(list(bookkeeping))
        return real_advance(conn, reader, new_recv_ts_ns=new_recv_ts_ns,
                            bookkeeping=bookkeeping, now_ns=now_ns)

    def spy_record(conn, bookkeeping, *, now_ns=0):
        record_calls.append(len(list(bookkeeping)))
        return real_record(conn, bookkeeping, now_ns=now_ns)

    monkeypatch.setattr(registry, "advance", spy_advance)
    monkeypatch.setattr(registry, "record_windows", spy_record)
    _run_pass(tmp_path, now_ns=now, symbols=None, batch_size=2)

    assert len(advance_calls) == 1                  # cursor advanced ONCE (not per-batch)
    assert advance_calls[0] == []                   # the single advance carries NO bookkeeping
    assert len(record_calls) == 2                   # per-batch window recording (2 batches)


# T4a — re-running the pass is idempotent (no duplicate primitives) -------------------

def test_rerun_is_idempotent_no_duplicates(tmp_path):
    _write_capture(tmp_path / "capture", _settled_rows())
    now = _W0_END_NS + cfg.BRAIN_WATERMARK_NS
    _run_pass(tmp_path, now_ns=now, symbols=None, batch_size=2)
    first = _snap_counter(tmp_path)
    summary2 = _run_pass(tmp_path, now_ns=now, symbols=None, batch_size=2)
    second = _snap_counter(tmp_path)
    assert summary2["snapshots_written"] == 0
    assert second == first and all(v == 1 for v in second.values())


# T4b — a mid-pass crash leaves the cursor unchanged; re-run has no duplicates --------

def test_midpass_crash_leaves_cursor_unchanged_and_rerun_has_no_dup(tmp_path):
    _write_capture(tmp_path / "capture", _settled_rows())
    now = _W0_END_NS + cfg.BRAIN_WATERMARK_NS
    reg_path = str(tmp_path / "brain" / "registry.sqlite")

    state = {"n": 0}
    real = sources.TRADES.read_fn

    def flaky(capture_root, after_recv_ts_ns=0, symbols=None):
        state["n"] += 1
        if state["n"] == 2:                          # the SECOND batch of the FIRST pass
            raise RuntimeError("injected mid-pass failure")
        return real(capture_root, after_recv_ts_ns=after_recv_ts_ns, symbols=symbols)

    flaky_spec = dataclasses.replace(sources.TRADES, read_fn=flaky)
    with pytest.raises(RuntimeError):
        _run_pass(tmp_path, now_ns=now, symbols=SYMS, batch_size=2, spec=flaky_spec)

    conn = registry.connect(reg_path)
    cursor_after_fail = registry.get_cursor(conn, sources.TRADES.reader_name)
    conn.close()
    assert cursor_after_fail == 0                    # cursor NOT advanced mid-pass

    # batch 1 (AAA,BBB) wrote + recorded before the crash; re-run dedups it, writes batch 2.
    _run_pass(tmp_path, now_ns=now, symbols=SYMS, batch_size=2)
    final = _snap_counter(tmp_path)
    assert sorted(final) == sorted((s, _W0_START_NS) for s in SYMS)   # all four, once
    assert all(v == 1 for v in final.values())       # batch 1 not duplicated on re-run


# Enumeration reads the CAPTURE dir, not the brain store dataset (the depth_state/depth
# and aggTrade/trades trap) ----------------------------------------------------------

def test_enumerate_universe_uses_capture_dir_not_brain_dataset(tmp_path):
    for spec in (sources.DEPTH, sources.TRADES):
        assert spec.capture_dataset != spec.dataset           # the trap genuinely exists
        for sym in ("AAAUSDT", "1000PEPEUSDT"):               # digit-leading must survive
            (tmp_path / spec.capture_dataset / f"symbol={sym}" / "date=2026-06-19").mkdir(parents=True)
        # a decoy under the BRAIN store dataset name must NOT be enumerated.
        (tmp_path / spec.dataset / "symbol=DECOYUSDT" / "date=2026-06-19").mkdir(parents=True)
        assert pipeline._enumerate_universe(str(tmp_path), spec.capture_dataset) == \
            ["1000PEPEUSDT", "AAAUSDT"]                        # sorted, from capture dir, no decoy


# T5 — cross-batch frontier: a high settled recv must not skip a low pending recv -----

def test_cross_batch_frontier_protects_a_low_recv_pending_row(tmp_path):
    # AAA: a SETTLED window-0 row that arrived LATE (recv high). BBB: a PENDING window-1
    # row that arrived EARLY (recv low). Separate batches (batch_size=1). A naive
    # cursor = max_settled (AAA's high recv) would skip BBB's low-recv pending row
    # forever (different symbol -> no seen_windows backstop). The global
    # min(max_settled, min_pending-1) must hold the cursor below BBB's recv.
    aaa_recv = _R0 + 100
    bbb_recv = _R0 + 50
    _write_capture(tmp_path / "capture", [
        _agg_row("AAAUSDT", recv_ns=aaa_recv, T_ms=_T0_MS + 1_000),    # window 0 -> settled
        _agg_row("BBBUSDT", recv_ns=bbb_recv, T_ms=_T0_MS + 61_000),   # window 1 -> pending
    ])
    now1 = _W0_END_NS + cfg.BRAIN_WATERMARK_NS          # W0 settled, W1 pending
    summary = _run_pass(tmp_path, now_ns=now1, symbols=["AAAUSDT", "BBBUSDT"], batch_size=1)

    reg_path = str(tmp_path / "brain" / "registry.sqlite")
    conn = registry.connect(reg_path)
    cursor = registry.get_cursor(conn, sources.TRADES.reader_name)
    conn.close()
    assert cursor == bbb_recv - 1                       # min(aaa_recv, bbb_recv-1) == bbb_recv-1
    starts = _snap_counter(tmp_path)
    assert ("AAAUSDT", _W0_START_NS) in starts          # AAA settled window written
    assert ("BBBUSDT", _W0_END_NS) not in starts        # BBB pending window held back (not lost)

    # Pass 2: now W1 is settled -> BBB's window emits, AAA is not re-written.
    now2 = _W1_END_NS + cfg.BRAIN_WATERMARK_NS
    _run_pass(tmp_path, now_ns=now2, symbols=["AAAUSDT", "BBBUSDT"], batch_size=1)
    final = _snap_counter(tmp_path)
    assert final[("BBBUSDT", _W0_END_NS)] == 1          # BBB window-1 emitted exactly once
    assert final[("AAAUSDT", _W0_START_NS)] == 1        # AAA not double-counted
