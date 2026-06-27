"""Reader FORWARD-CEILING bound — the missing upper bound that makes a tick
constant-cost regardless of how far behind the cursor is.

The reader already bounds a read on the ``symbol=`` partition (PR #53) and the
``date=`` partition floor (PR #61). Both bound the read AGAINST the lower / older
side and against breadth. Neither bounds the FORWARD extent: the row filter was
``recv_ts_ns > cursor`` with no ceiling, so a cursor that had fallen N hours
behind materialized the entire ``(cursor, now]`` slice in one read — the OOM
death-spiral root cause.

These tests pin the new ``before_recv_ts_ns`` ceiling: a read returns only rows
in ``(cursor, ceiling]``. ``ceiling = cursor + W`` is computed by the pipeline;
here we drive the reader directly. ``before_recv_ts_ns=None`` keeps the old
unbounded behaviour (the deliberate from-zero full-backfill path).
"""
from __future__ import annotations

import pytest

from crypto.research.capture_core import store as capture_store
from crypto.research.brain import config as cfg
from crypto.research.brain import reader, sources

_MS_TO_NS = 1_000_000
# 2026-06-16 12:00:00 UTC (ms), a 60s boundary, mid-day (no UTC-midnight straddle).
_T0_MS = 1_781_611_200_000
_T0_NS = _T0_MS * _MS_TO_NS


def _write_bt(root, symbol, recv_ns) -> int:
    """Append+flush one bookTicker row with an explicit recv_ts_ns (and E aligned
    to it, so the date= partition matches recv). Returns recv_ns."""
    e_ms = recv_ns // _MS_TO_NS
    w = capture_store.bookticker_writer(str(root))
    w.append({"recv_ts_ns": recv_ns, "e": "bookTicker", "u": 1, "s": symbol,
              "b": "100.0", "B": "1.0", "a": "101.0", "A": "1.0", "T": e_ms, "E": e_ms})
    w.flush_all()
    return recv_ns


def _write_agg(root, symbol, recv_ns) -> int:
    e_ms = recv_ns // _MS_TO_NS
    w = capture_store.aggtrade_writer(str(root))
    w.append({"recv_ts_ns": recv_ns, "e": "aggTrade", "E": e_ms, "a": 1, "s": symbol,
              "p": "100", "q": "2", "f": 1, "l": 1, "T": e_ms, "m": False})
    w.flush_all()
    return recv_ns


# --- core: a bounded read returns only rows in (cursor, ceiling] ---------------

def test_bounded_read_excludes_rows_past_the_ceiling(tmp_path):
    sym = "AAAUSDT"
    W = cfg.BRAIN_MAX_TICK_WINDOW_NS
    cursor = _T0_NS
    inside = _write_bt(tmp_path, sym, cursor + 1)             # just past cursor -> IN
    at_ceiling = _write_bt(tmp_path, sym, cursor + W)         # exactly the ceiling -> IN (inclusive)
    just_past = _write_bt(tmp_path, sym, cursor + W + 1)      # one ns past ceiling -> OUT
    far_past = _write_bt(tmp_path, sym, cursor + 10 * W)      # far beyond -> OUT

    rows = reader.read_new_bookticker(
        str(tmp_path), after_recv_ts_ns=cursor,
        before_recv_ts_ns=cursor + W, symbols=[sym])
    got = sorted(r["recv_ts_ns"] for r in rows)
    assert got == [inside, at_ceiling], (
        "bounded read must return exactly (cursor, cursor+W] — never the rows past it")


def test_ceiling_none_is_the_old_unbounded_read(tmp_path):
    sym = "AAAUSDT"
    W = cfg.BRAIN_MAX_TICK_WINDOW_NS
    cursor = _T0_NS
    a = _write_bt(tmp_path, sym, cursor + 1)
    b = _write_bt(tmp_path, sym, cursor + 10 * W)             # far past any ceiling

    rows = reader.read_new_bookticker(
        str(tmp_path), after_recv_ts_ns=cursor, before_recv_ts_ns=None, symbols=[sym])
    assert sorted(r["recv_ts_ns"] for r in rows) == sorted([a, b]), (
        "before_recv_ts_ns=None must preserve the unbounded read (full-backfill path)")


def test_bounded_read_threads_through_aggtrade_reader(tmp_path):
    sym = "AAAUSDT"
    W = cfg.BRAIN_MAX_TICK_WINDOW_NS
    cursor = _T0_NS
    inside = _write_agg(tmp_path, sym, cursor + 1)
    _write_agg(tmp_path, sym, cursor + W + 1)                 # past ceiling -> OUT
    rows = reader.read_new_aggtrades(
        str(tmp_path), after_recv_ts_ns=cursor,
        before_recv_ts_ns=cursor + W, symbols=[sym])
    assert [r["recv_ts_ns"] for r in rows] == [inside]


# --- threading: every registered source's read_fn accepts the ceiling kwarg ----

@pytest.mark.parametrize("dataset", sorted(sources.SOURCES))
def test_every_source_read_fn_accepts_before_recv_ts_ns(tmp_path, dataset):
    """Every registered source read_fn (incl. the as-of / klines closures) must accept the
    ceiling kwarg, or the pipeline cannot bound them. An empty capture root -> [] proves the
    signature threads through without opening anything."""
    spec = sources.SOURCES[dataset]
    rows = spec.read_fn(str(tmp_path), after_recv_ts_ns=_T0_NS,
                        symbols=["AAAUSDT"], before_recv_ts_ns=_T0_NS + cfg.BRAIN_MAX_TICK_WINDOW_NS)
    assert rows == []
