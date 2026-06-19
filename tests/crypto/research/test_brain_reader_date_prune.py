"""Reader DATE-partition pruning — the time-twin of PR #53's symbol pruning.

PR #53 made a bounded read prune on the Hive ``symbol=`` partition. It still opened
EVERY ``date=`` partition of the selected symbols and buffered the full on-disk
history before the row-level ``recv_ts_ns > cursor`` filter trimmed it (the canary's
4.18 GiB, which grows with the tape). This completes the prune: a recv-cursor read
also prunes the ``date=`` partition, so a bounded read opens only
(selected symbols) x (in-range dates) and memory stops scaling with history length.

CORRECTNESS LANDMINE (why this is dataset-aware): the ``date=`` partition is keyed on
a capture-side time field that differs by dataset. For the recv-aligned firehose
(aggTrade / bookTicker / markPrice / forceOrder on event-time E ~ recv; depth_state on
recv itself) date(partition) tracks the recv cursor, so pruning is safe. But klines_1h
partitions on the bar ``openTime`` (a 90-day backfill writes 90-day-old partitions at
recv=now) and the ``/futures/data`` as-of series on a bucket ``timestamp`` (~35 min
behind recv) — pruning those by recv-date would SILENTLY DROP just-arrived rows. So
pruning is gated on an allowlist of recv-aligned datasets; klines/as-of opt out.

A 1-day margin absorbs the event-vs-recv skew (flush + clock + the UTC-midnight
straddle where a row's E is 23:59 on D-1 but its recv crosses into D); date is
day-granular, so the margin costs at most one extra partition and never drops a row.
"""
from __future__ import annotations

import logging
import pathlib

import pytest

from crypto.research.capture_core import store as capture_store
from crypto.research.capture_core import klines_store
from crypto.research.brain import reader
from crypto.research.brain import config as cfg

_DAY_MS = 86_400_000
_MS_TO_NS = 1_000_000
# 2026-06-16 00:00:00 UTC, in ms. day n -> + n days (n may be negative).
_D0_MS = 1_781_568_000_000

# A 438-byte truncated parquet (PAR1 header, no footer) — the DEXEUSDT shape that
# crashed the canary; here it stands in for "a fragment that, if OPENED, logs a skip".
_TRUNCATED = b"PAR1" + b"\x00" * 434


def _day_ms(n: int, *, h: int = 0, m: int = 0, s: int = 0) -> int:
    return _D0_MS + n * _DAY_MS + (h * 3600 + m * 60 + s) * 1000


def _write_bt(root, symbol, e_ms, recv_ns=None) -> int:
    """Append+flush one bookTicker row. ``E`` (ms) drives the date= partition; recv_ns
    (defaults to E in ns — live-WS aligned) drives the recv window. Returns recv_ns."""
    recv_ns = e_ms * _MS_TO_NS if recv_ns is None else recv_ns
    w = capture_store.bookticker_writer(str(root))
    w.append({"recv_ts_ns": recv_ns, "e": "bookTicker", "u": 1, "s": symbol,
              "b": "100.0", "B": "1.0", "a": "101.0", "A": "1.0", "T": e_ms, "E": e_ms})
    w.flush_all()
    return recv_ns


def _write_kline(root, symbol, open_time_ms, recv_ns) -> None:
    """Append+flush one klines_1h bar. ``openTime`` (ms) drives the date= partition;
    recv_ns is the (much later, for a backfill) arrival."""
    w = capture_store.dataset_writer(
        str(root), cfg.KLINES_CAPTURE_DATASET, klines_store.KLINES_1H_SCHEMA,
        symbol_key="s", time_key="openTime")
    w.append({"recv_ts_ns": recv_ns, "s": symbol, "openTime": open_time_ms,
              "open": "1.0", "high": "2.0", "low": "0.5", "close": "1.5", "volume": "10.0",
              "closeTime": open_time_ms + 3_600_000, "quoteVolume": "15.0", "trades": 3,
              "takerBuyBase": "4.0", "takerBuyQuote": "6.0"})
    w.flush_all()


def _drop_truncated(root, dataset, symbol, date_str) -> None:
    d = pathlib.Path(root, dataset, f"symbol={symbol}", f"date={date_str}")
    d.mkdir(parents=True, exist_ok=True)
    (d / "part-truncated.parquet").write_bytes(_TRUNCATED)


# T1 (RED driver) — a corrupt fragment in an OUT-OF-RANGE date is never opened --------

def test_corrupt_fragment_in_out_of_range_date_is_never_opened(tmp_path, caplog):
    sym = "AAAUSDT"
    # in-range good row on 06-20; an out-of-range date 06-17 (>= 2 days before the cursor
    # day, below the 1-day-margin lower bound date>=06-19) holding BOTH a good (real,
    # out-of-window) part AND a truncated part. The good part lets the dataset infer its
    # schema without touching the corrupt one; date-pruning then prunes the whole 06-17
    # partition, so the truncated part is never iterated -> no skip is ever logged.
    _write_bt(tmp_path, sym, _day_ms(4, h=12))           # 06-20, in window
    _write_bt(tmp_path, sym, _day_ms(1, h=12))           # 06-17, real but out-of-range/out-of-window
    _drop_truncated(tmp_path, "bookTicker", sym, "2026-06-17")
    cursor = _day_ms(4) * _MS_TO_NS                      # 06-20 00:00:00 (ns)
    with caplog.at_level(logging.WARNING):
        rows = reader.read_new_bookticker(str(tmp_path), after_recv_ts_ns=cursor, symbols=[sym])
    assert [r["symbol"] for r in rows] == [sym]          # only the in-range good row
    assert not any("part-truncated.parquet" in r.getMessage() for r in caplog.records), \
        "out-of-range date partition must be pruned (never opened) — no skip should be logged"


# T5 (positive control) — an IN-RANGE corrupt fragment IS opened, skipped, and logged -

def test_corrupt_fragment_in_in_range_date_is_skipped_and_logged(tmp_path, caplog):
    sym = "AAAUSDT"
    _write_bt(tmp_path, sym, _day_ms(4, h=12))           # good row 06-20
    _drop_truncated(tmp_path, "bookTicker", sym, "2026-06-20")   # corrupt, in range
    cursor = _day_ms(4) * _MS_TO_NS
    with caplog.at_level(logging.WARNING):
        rows = reader.read_new_bookticker(str(tmp_path), after_recv_ts_ns=cursor, symbols=[sym])
    assert [r["symbol"] for r in rows] == [sym]
    assert any("part-truncated.parquet" in r.getMessage() for r in caplog.records), \
        "an in-range corrupt fragment IS opened/skipped/logged — validates T1's no-warning proof"


# T6 (margin pin) — a cross-midnight straddle row is not dropped ----------------------

def test_cross_midnight_skew_row_is_not_dropped(tmp_path):
    sym = "AAAUSDT"
    # E at 06-19 23:59 (partition date=2026-06-19) but recv a moment later at 06-20 00:00:01.
    e_ms = _day_ms(3, h=23, m=59)
    recv = _day_ms(4, s=1) * _MS_TO_NS
    _write_bt(tmp_path, sym, e_ms, recv_ns=recv)
    cursor = _day_ms(4) * _MS_TO_NS                      # 06-20 00:00:00
    rows = reader.read_new_bookticker(str(tmp_path), after_recv_ts_ns=cursor, symbols=[sym])
    # recv > cursor (in window); its partition day (06-19) is one behind the recv day.
    # The 1-day margin keeps date>=06-19, so the row is NOT dropped (a no-margin prune would).
    assert [r["recv_ts_ns"] for r in rows] == [recv]


# T2 (boundary guard) — a read spanning two dates keeps the later date ----------------

def test_read_spanning_two_dates_keeps_the_later_date(tmp_path):
    sym = "AAAUSDT"
    r1 = _write_bt(tmp_path, sym, _day_ms(3, h=23))      # 06-19 23:00
    r2 = _write_bt(tmp_path, sym, _day_ms(4, h=1))       # 06-20 01:00
    cursor = _day_ms(3, h=22) * _MS_TO_NS                # 06-19 22:00 (before both)
    rows = reader.read_new_bookticker(str(tmp_path), after_recv_ts_ns=cursor, symbols=[sym])
    assert sorted(r["recv_ts_ns"] for r in rows) == sorted([r1, r2])   # both dates present


# T3 (correctness equality) — pruned read == full-window oracle -----------------------

def test_date_pruned_read_matches_full_window_oracle(tmp_path):
    sym = "AAAUSDT"
    out = _write_bt(tmp_path, sym, _day_ms(0, h=12))     # 06-16 (out of window AND pruned)
    in_a = _write_bt(tmp_path, sym, _day_ms(3, h=10))    # 06-19 (in window)
    in_b = _write_bt(tmp_path, sym, _day_ms(4, h=10))    # 06-20 (in window)
    cursor = _day_ms(2, h=12) * _MS_TO_NS                # 06-18 12:00
    oracle = sorted(r for r in (out, in_a, in_b) if r > cursor)   # recv>cursor, independent
    rows = reader.read_new_bookticker(str(tmp_path), after_recv_ts_ns=cursor, symbols=[sym])
    assert sorted(r["recv_ts_ns"] for r in rows) == oracle == sorted([in_a, in_b])
    # fixture genuinely spans >1 date partition, including an out-of-window earlier one:
    dates = sorted(p.name for p in
                   pathlib.Path(tmp_path, "bookTicker", f"symbol={sym}").glob("date=*"))
    assert dates == ["date=2026-06-16", "date=2026-06-19", "date=2026-06-20"]


# T4 (landmine guard) — klines backfill (old openTime, new recv) is NOT date-pruned ---

def test_klines_backfill_old_event_date_new_recv_is_not_date_pruned(tmp_path):
    sym = "AAAUSDT"
    # A backfilled bar: openTime ~90 days before the anchor (partition far in the past)
    # but recv = now (06-20). klines partitions on openTime, NOT recv.
    open_time = _day_ms(-90, h=12)
    recv = _day_ms(4, h=12) * _MS_TO_NS
    _write_kline(tmp_path, sym, open_time, recv)
    cursor = _day_ms(3) * _MS_TO_NS                       # 06-19 (recv > cursor)
    rows = reader.read_new_klines(str(tmp_path), after_recv_ts_ns=cursor, symbols=[sym])
    # If klines were date-pruned by the recv cursor, this just-arrived bar (old-openTime
    # partition, far below the lower bound) would be SILENTLY DROPPED. It must not be.
    assert [r["recv_ts_ns"] for r in rows] == [recv]
    assert rows[0]["event_time_ms"] == recv // _MS_TO_NS  # forward-only: arrival, not openTime


# T7 — date-pruning also applies on the full-universe (symbols=None) path -------------

def test_full_universe_read_still_prunes_out_of_range_dates(tmp_path, caplog):
    sym = "AAAUSDT"
    # Same shape as T1 but with NO symbol bound: the date prune is gated on dataset +
    # cursor, not on symbols, so a full-universe read still prunes old dates (bounding
    # the runner's full scan). 06-16 holds a real (out-of-window) part + a truncated one;
    # the no-skip-log proves the whole 06-16 partition was pruned, not opened.
    in_window = _write_bt(tmp_path, sym, _day_ms(4, h=12))    # 06-20, in window
    _write_bt(tmp_path, sym, _day_ms(0, h=12))                # 06-16, real but out-of-range
    _drop_truncated(tmp_path, "bookTicker", sym, "2026-06-16")
    cursor = _day_ms(4) * _MS_TO_NS                           # 06-20 00:00 -> lower_date 06-19
    with caplog.at_level(logging.WARNING):
        rows = reader.read_new_bookticker(str(tmp_path), after_recv_ts_ns=cursor)  # symbols=None
    assert [r["recv_ts_ns"] for r in rows] == [in_window]     # only the in-range row
    assert not any("part-truncated.parquet" in r.getMessage() for r in caplog.records), \
        "full-universe reads must still prune out-of-range dates (06-16 never opened)"
