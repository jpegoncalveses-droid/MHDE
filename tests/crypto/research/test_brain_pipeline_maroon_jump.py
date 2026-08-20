"""Maroon-jump: a bounded pass whose cursor is stranded BELOW the oldest surviving
raw tape must jump forward to the tape's edge and flag the skipped interval as a
capture gap (KI-166: the 2026-08-15 frontier stall).

The trap: retention/disk-guard pruning deletes raw ``date=`` partitions out from
under a behind cursor. Every bounded read of ``(cursor, cursor+W]`` then finds no
files, and the quiet-gap skip advances only ``W - watermark - cadence`` (+150s) per
tick — slower than wall clock under real tick walls — while the surviving tape's
oldest edge moves forward a full day every midnight. The cursor can never re-enter
live tape and settlement (gated on the cursor, not wall clock) freezes silently.

The fix is deliberately conservative: the jump fires ONLY when the entire bounded
window provably lies below the oldest date partition that still holds data. A quiet
gap WITHIN surviving tape keeps the exact old semantics (pinned below), and the
skipped interval is appended to the capture ``_gaps`` manifest — flag-don't-drop,
the same channel the label builder already consumes (no-bias rule: missing tape is
a GAP, never a quiet window).
"""
from __future__ import annotations

import pathlib

import pyarrow.parquet as pq

from crypto.research.capture_core import store as capture_store
from crypto.research.brain import config as cfg
from crypto.research.brain import pipeline, registry, sources

_T0_MS = 1_781_640_000_000               # 2026-06-16 20:00:00 UTC, a 60s boundary
_T0_NS = _T0_MS * 1_000_000
_CAD_MS = 60_000
_W = cfg.BRAIN_MAX_TICK_WINDOW_NS
_DAY_NS = 86_400_000_000_000

#: 2026-06-18 00:00:00 UTC = T0 + 28h — the "surviving tape" day, two dates after T0.
_SURVIVOR_DAY_START_NS = _T0_NS + 28 * 3_600_000_000_000
_SURVIVOR_K = 28 * 60 + 10               # a window 10 min into 2026-06-18


def _agg(symbol, k, *, seq=0):
    t_ms = _T0_MS + k * _CAD_MS + 1_000
    recv = t_ms * 1_000_000 + seq
    return {"recv_ts_ns": recv, "e": "aggTrade", "E": t_ms, "a": 1 + seq, "s": symbol,
            "p": "100", "q": "2", "f": 1, "l": 1, "T": t_ms, "m": False}


def _write(root, rows):
    w = capture_store.aggtrade_writer(str(root))
    for r in rows:
        w.append(r)
    w.flush_all()


def _seed_cursor(registry_path, reader, value_ns, now_ns):
    conn = registry.connect(registry_path)
    try:
        registry.advance(conn, reader, new_recv_ts_ns=value_ns, now_ns=now_ns)
    finally:
        conn.close()


def _gap_rows(cap_root):
    base = pathlib.Path(cap_root, "_gaps")
    rows = []
    for fp in sorted(base.rglob("*.parquet")) if base.exists() else []:
        rows.extend(pq.read_table(str(fp)).to_pylist())
    return rows


def _run(cap, st, reg, now):
    return pipeline.run_pass(
        sources.TRADES, capture_root=str(cap), store_root=str(st),
        registry_path=reg, now_ns=now)


# --- the pin: a marooned cursor jumps to the surviving tape and flags the gap --

def test_marooned_cursor_jumps_to_surviving_tape_with_gap_flag(tmp_path):
    cap, st = tmp_path / "capture", tmp_path / "brain"
    reg = str(st / "registry.sqlite")
    # Raw tape: ONLY 2026-06-18 survives (retention ate everything older).
    _write(cap, [_agg("BTCUSDT", _SURVIVOR_K, seq=i) for i in range(3)])
    seed = _T0_NS - 1                                    # cursor stranded 28h below
    now = _T0_NS + 3 * _DAY_NS
    _seed_cursor(reg, sources.TRADES.reader_name, seed, now)

    summary = _run(cap, st, reg, now)

    assert summary["rows_read"] == 0
    # THE JUMP: cursor lands at the surviving day's edge, not +150s of quiet-skip.
    assert summary["cursor_after"] >= _SURVIVOR_DAY_START_NS - 1, (
        "marooned cursor must jump to the oldest surviving tape, not treadmill")
    assert summary["cursor_after"] < _agg("BTCUSDT", _SURVIVOR_K)["recv_ts_ns"], (
        "the jump must land BELOW the surviving rows (no data skipped)")
    # THE FLAG: exactly one gap record covering the jumped interval.
    gaps = _gap_rows(cap)
    assert len(gaps) == 1
    g = gaps[0]
    assert g["stream"] == "aggTrade"
    assert g["gap_start_ms"] == seed // 1_000_000
    assert g["gap_end_ms"] == summary["cursor_after"] // 1_000_000
    assert "maroon" in g["reason"]

    # A second pass from the jumped cursor: normal quiet-skip, NO second jump/flag.
    summary2 = _run(cap, st, reg, now)
    assert summary2["cursor_after"] > summary["cursor_after"]
    assert len(_gap_rows(cap)) == 1


def test_quiet_gap_within_surviving_tape_never_jumps(tmp_path):
    # Data at windows {0,1,2} and {13,14} of the SAME day: the >2W quiet gap between
    # them is genuine quiet tape, not a maroon — old semantics exactly, no gap flag.
    cap, st = tmp_path / "capture", tmp_path / "brain"
    reg = str(st / "registry.sqlite")
    _write(cap, ([_agg("ETHUSDT", k, seq=s) for k in (0, 1, 2) for s in range(2)]
                 + [_agg("ETHUSDT", k) for k in (13, 14)]))
    seed = _T0_NS - 1
    now = _T0_NS + 20 * 60 * 1_000_000_000
    _seed_cursor(reg, sources.TRADES.reader_name, seed, now)

    summary = _run(cap, st, reg, now)

    assert summary["cursor_after"] <= seed + _W          # bounded advance, no jump
    assert _gap_rows(cap) == []


def test_empty_stale_date_dir_is_not_a_jump_target(tmp_path):
    # An older date= dir that retention emptied (dir left, no parquet) must be
    # skipped: the jump goes to the oldest date that still HOLDS data.
    cap, st = tmp_path / "capture", tmp_path / "brain"
    reg = str(st / "registry.sqlite")
    _write(cap, [_agg("BTCUSDT", _SURVIVOR_K)])
    empty = pathlib.Path(cap, "aggTrade", "symbol=BTCUSDT", "date=2026-06-16")
    empty.mkdir(parents=True)
    seed = _T0_NS - 1
    now = _T0_NS + 3 * _DAY_NS
    _seed_cursor(reg, sources.TRADES.reader_name, seed, now)

    summary = _run(cap, st, reg, now)

    assert summary["cursor_after"] >= _SURVIVOR_DAY_START_NS - 1, (
        "an empty stale date dir must not capture the jump")


def test_absent_dataset_dir_is_a_plain_noop_pass(tmp_path):
    cap, st = tmp_path / "capture", tmp_path / "brain"
    reg = str(st / "registry.sqlite")
    seed = _T0_NS - 1
    now = _T0_NS + _DAY_NS
    _seed_cursor(reg, sources.TRADES.reader_name, seed, now)

    summary = _run(cap, st, reg, now)                    # no aggTrade dir at all

    assert summary["rows_read"] == 0
    assert _gap_rows(cap) == []


def test_cold_cursor_unbounded_backfill_never_jumps(tmp_path):
    # cursor == 0 is the deliberate unbounded-backfill path (read_ceiling None):
    # the maroon logic must not engage — the pass reads the tape directly.
    cap, st = tmp_path / "capture", tmp_path / "brain"
    reg = str(st / "registry.sqlite")
    _write(cap, [_agg("BTCUSDT", _SURVIVOR_K, seq=i) for i in range(2)])
    now = _T0_NS + 3 * _DAY_NS

    summary = _run(cap, st, reg, now)                    # cursor never seeded (0)

    assert summary["rows_read"] == 2
    assert _gap_rows(cap) == []


def test_markprice_maroon_gap_uses_the_label_consumed_stream_name():
    # labels._markprice_gap_intervals_ns filters stream.startswith("!markPrice@arr")
    # (labels.py:88) — the markPrice maroon gap must be visible to it. Other
    # datasets keep their capture dir name (the compaction-gap convention).
    assert pipeline._maroon_gap_stream("markPrice").startswith("!markPrice@arr")
    assert pipeline._maroon_gap_stream("aggTrade") == "aggTrade"
    assert pipeline._maroon_gap_stream("bookTicker") == "bookTicker"
