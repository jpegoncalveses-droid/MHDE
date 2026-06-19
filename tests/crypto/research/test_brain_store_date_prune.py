"""`read_snapshots` date-partition pruning — the PR #55 reader pattern, store side.

`read_snapshots` already prunes by SYMBOL (path glob) but not by DATE: it `**`-globs every
`date=` partition and reads each file in full, so a forward read rescans a symbol's ENTIRE
history × fragments. This adds a cursor-driven date prune: given `after_recv_ts_ns`, skip
`date=` partitions older than its date (minus a 1-day margin for recv-vs-window skew) WITHOUT
opening their files. It's an OPTIMISATION (bounds history scanned), default-off and backward
compatible; the structural fix for fan-out is the compactor, not this.

Proof that the prune SKIPS files (not just filters rows): a corrupt part in an out-of-range
date that would raise `ArrowInvalid` if opened — a pruned read must succeed.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone

import pyarrow as pa

from crypto.research.brain import store

_SCHEMA = pa.schema([
    ("symbol", pa.string()),
    ("window_start_ns", pa.int64()),
    ("window_end_ns", pa.int64()),
    ("recv_ts_ns", pa.int64()),
    ("mark_close", pa.float64()),
])


def _ns(y, mo, d, h=12):
    return int(datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp() * 1_000_000_000)


def _snap(symbol, window_start_ns, recv_ts_ns=None):
    return {"symbol": symbol, "window_start_ns": window_start_ns,
            "window_end_ns": window_start_ns + 60_000_000_000,
            "recv_ts_ns": recv_ts_ns if recv_ts_ns is not None else window_start_ns,
            "mark_close": 100.0}


def _write(root, symbol, window_start_ns):
    store.write_snapshots(str(root), "markprice", _SCHEMA, [_snap(symbol, window_start_ns)])


def _poison(root, symbol, date):
    d = pathlib.Path(root, "markprice", f"symbol={symbol}", f"date={date}")
    d.mkdir(parents=True, exist_ok=True)
    (d / "part-poison.parquet").write_bytes(b"not a parquet at all")


# T1 (RED driver) — a corrupt part in an OUT-OF-RANGE date is never opened ----------

def test_corrupt_part_in_out_of_range_date_is_never_opened(tmp_path):
    _write(tmp_path, "BTCUSDT", _ns(2026, 6, 18))
    _poison(tmp_path, "BTCUSDT", "2026-01-01")           # would raise ArrowInvalid if opened
    rows = store.read_snapshots(str(tmp_path), "markprice", "BTCUSDT",
                                after_recv_ts_ns=_ns(2026, 6, 18) - 1)
    assert [r["window_start_ns"] for r in rows] == [_ns(2026, 6, 18)]   # old date pruned


# T2 (backward compat) — default after=0 reads every date ---------------------------

def test_after_zero_reads_all_dates(tmp_path):
    _write(tmp_path, "BTCUSDT", _ns(2026, 1, 1))
    _write(tmp_path, "BTCUSDT", _ns(2026, 6, 18))
    rows = store.read_snapshots(str(tmp_path), "markprice", "BTCUSDT")
    assert sorted(r["window_start_ns"] for r in rows) == [_ns(2026, 1, 1), _ns(2026, 6, 18)]


# T3 (margin) — the 1-day margin keeps the boundary date ----------------------------

def test_margin_keeps_the_boundary_date(tmp_path):
    # after in 06-18 -> lower_date = 06-17; the margin day MUST be kept (skew safety).
    _write(tmp_path, "BTCUSDT", _ns(2026, 6, 17))
    _write(tmp_path, "BTCUSDT", _ns(2026, 6, 18))
    rows = store.read_snapshots(str(tmp_path), "markprice", "BTCUSDT",
                                after_recv_ts_ns=_ns(2026, 6, 18))
    assert sorted(r["window_start_ns"] for r in rows) == [_ns(2026, 6, 17), _ns(2026, 6, 18)]


# T4 — a date below the margin IS pruned --------------------------------------------

def test_date_below_margin_is_pruned(tmp_path):
    _write(tmp_path, "BTCUSDT", _ns(2026, 6, 15))        # below 06-17 lower bound
    _write(tmp_path, "BTCUSDT", _ns(2026, 6, 18))
    rows = store.read_snapshots(str(tmp_path), "markprice", "BTCUSDT",
                                after_recv_ts_ns=_ns(2026, 6, 18))
    assert sorted(r["window_start_ns"] for r in rows) == [_ns(2026, 6, 18)]


# T5 — the all-symbols read (symbol=None) prunes too --------------------------------

def test_all_symbols_read_also_prunes(tmp_path):
    _write(tmp_path, "BTCUSDT", _ns(2026, 6, 18))
    _write(tmp_path, "ETHUSDT", _ns(2026, 6, 18))
    _poison(tmp_path, "BTCUSDT", "2026-01-01")
    rows = store.read_snapshots(str(tmp_path), "markprice",
                                after_recv_ts_ns=_ns(2026, 6, 18))
    assert sorted(r["symbol"] for r in rows) == ["BTCUSDT", "ETHUSDT"]


# T6 (oracle) — pruned read == full read restricted to the kept dates ---------------

def test_pruned_read_matches_full_window_oracle(tmp_path):
    for d in (1, 15, 17, 18, 19):
        _write(tmp_path, "BTCUSDT", _ns(2026, 6, d) if d != 1 else _ns(2026, 1, 1))
    after = _ns(2026, 6, 18)
    pruned = store.read_snapshots(str(tmp_path), "markprice", "BTCUSDT", after_recv_ts_ns=after)
    full = store.read_snapshots(str(tmp_path), "markprice", "BTCUSDT")
    lower = store._date_str_from_ns(after - store._DATE_PRUNE_MARGIN_NS)
    oracle = [r for r in full if store._date_str_from_ns(r["window_start_ns"]) >= lower]
    assert sorted(r["window_start_ns"] for r in pruned) == sorted(r["window_start_ns"] for r in oracle)
