"""Tests for the capture-core parquet store (per-stream writer + flush + gaps)."""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone

import pyarrow.parquet as pq

from crypto.research.capture_core import store


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


_DAY = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)


def _aggtrade_row(symbol="BTCUSDT", *, recv_ns=1, E=None, a=10,
                  p="100.5", q="2.0", f=1, l=2, T=None, m=False):
    e_ms = _ms(_DAY) if E is None else E
    return {"recv_ts_ns": recv_ns, "e": "aggTrade", "E": e_ms, "a": a, "s": symbol,
            "p": p, "q": q, "f": f, "l": l, "T": e_ms if T is None else T, "m": m}


def _read_all(root, dataset):
    files = sorted(pathlib.Path(root, dataset).rglob("*.parquet"))
    rows = []
    for fp in files:
        rows.extend(pq.read_table(str(fp)).to_pylist())
    return files, rows


# -- partitioning + round-trip --

def test_flush_all_writes_partitioned_parquet_and_round_trips(tmp_path):
    w = store.aggtrade_writer(str(tmp_path))
    w.append(_aggtrade_row("BTCUSDT", p="100.5"))
    w.append(_aggtrade_row("ETHUSDT", p="42.0"))
    w.flush_all()

    files, rows = _read_all(str(tmp_path), "aggTrade")
    parts = {fp.relative_to(tmp_path / "aggTrade").parts[:2] for fp in files}
    assert ("symbol=BTCUSDT", "date=2026-05-29") in parts
    assert ("symbol=ETHUSDT", "date=2026-05-29") in parts
    by_sym = {r["s"]: r for r in rows}
    assert by_sym["BTCUSDT"]["p"] == "100.5"  # price kept as venue string (lossless)
    assert by_sym["BTCUSDT"]["recv_ts_ns"] == 1
    assert by_sym["ETHUSDT"]["m"] is False
    assert w.rows_written == 2
    assert w.files_written == 2


def test_event_time_date_partition_splits_across_utc_midnight(tmp_path):
    w = store.aggtrade_writer(str(tmp_path))
    before = _ms(datetime(2026, 5, 29, 23, 59, 59, 999_000, tzinfo=timezone.utc))
    after = _ms(datetime(2026, 5, 30, 0, 0, 0, tzinfo=timezone.utc))
    w.append(_aggtrade_row("BTCUSDT", E=before))
    w.append(_aggtrade_row("BTCUSDT", E=after))
    w.flush_all()
    files, _ = _read_all(str(tmp_path), "aggTrade")
    dates = {fp.parent.name for fp in files}
    assert dates == {"date=2026-05-29", "date=2026-05-30"}


# -- flush triggers (earlier of size OR age) --

def test_flush_due_triggers_on_size(tmp_path):
    w = store.aggtrade_writer(str(tmp_path), flush_max_bytes=1, flush_interval_s=10_000)
    w.append(_aggtrade_row("BTCUSDT"))
    assert w.flush_due() == 1
    _, rows = _read_all(str(tmp_path), "aggTrade")
    assert len(rows) == 1


def test_flush_due_triggers_on_age_with_injected_clock(tmp_path):
    clock = [1000.0]
    w = store.aggtrade_writer(str(tmp_path), flush_max_bytes=10**12,
                              flush_interval_s=30.0, now_fn=lambda: clock[0])
    w.append(_aggtrade_row("BTCUSDT"))
    assert w.flush_due() == 0          # fresh + tiny -> not due
    clock[0] += 31.0
    assert w.flush_due() == 1          # aged past interval -> due
    _, rows = _read_all(str(tmp_path), "aggTrade")
    assert len(rows) == 1


def test_flush_all_on_empty_is_noop(tmp_path):
    w = store.aggtrade_writer(str(tmp_path))
    w.flush_all()
    assert w.rows_written == 0
    assert w.files_written == 0


# -- gap manifest --

def test_gap_writer_round_trips(tmp_path):
    g = store.gap_writer(str(tmp_path))
    start = _ms(datetime(2026, 5, 29, 10, 0, 0, tzinfo=timezone.utc))
    end = _ms(datetime(2026, 5, 29, 10, 0, 5, tzinfo=timezone.utc))
    g.append({"symbol": "BTCUSDT", "stream": "btcusdt@aggTrade",
              "gap_start_ms": start, "gap_end_ms": end,
              "reason": "reconnect", "recorded_recv_ts_ns": 1_000_000})
    g.flush_all()
    files, rows = _read_all(str(tmp_path), "_gaps")
    assert len(rows) == 1
    assert rows[0]["reason"] == "reconnect"
    assert rows[0]["symbol"] == "BTCUSDT"
    # gaps partition by date (derived from gap_start_ms); symbol stays a column
    assert files[0].parent.name == "date=2026-05-29"
