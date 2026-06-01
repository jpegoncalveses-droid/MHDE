"""Tests for the intraday-klines research store + backfill driver.

The 1-minute klines live in a **separate research DB**
(``data/research/intraday.duckdb``), never in the production
``mhde.duckdb`` and never in ``crypto.schema.ALL_SCHEMAS``. The backfill
driver paginates through a Binance client (injected, so these tests never
touch the network), UPSERTs idempotently on ``(symbol, interval,
open_time)``, and skips + logs symbols the client can't return.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from crypto.execution.backtest import intraday_klines as ik


def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def _bars(symbol_start, n, *, step_min=1, price=100.0):
    """n consecutive 1-minute bars starting at ``symbol_start``."""
    rows = []
    t = symbol_start
    for i in range(n):
        rows.append({
            "open_time": t,
            "open": price + i,
            "high": price + i + 0.5,
            "low": price + i - 0.5,
            "close": price + i + 0.2,
            "volume": 10.0 + i,
        })
        t = t + timedelta(minutes=step_min)
    return rows


class _FakeClient:
    """Returns canned klines per symbol, or raises if the value is an Exception."""

    def __init__(self, data):
        self._data = data
        self.calls = []

    def fetch_klines(self, symbol, interval, start_dt=None, end_dt=None):
        self.calls.append((symbol, interval, start_dt, end_dt))
        v = self._data[symbol]
        if isinstance(v, Exception):
            raise v
        return v


def _mem_conn(tmp_path):
    db = tmp_path / "intraday.duckdb"
    return ik.connect_research_db(str(db))


# ── schema isolation ────────────────────────────────────────────────────


def test_intraday_table_not_in_all_schemas():
    from crypto import schema
    joined = "\n".join(schema.ALL_SCHEMAS)
    assert "crypto_klines_intraday" not in joined


def test_connect_creates_table(tmp_path):
    conn = _mem_conn(tmp_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info('crypto_klines_intraday')").fetchall()]
    assert cols == ["symbol", "interval", "open_time", "open", "high", "low", "close", "volume"]


# ── idempotent UPSERT ───────────────────────────────────────────────────


def test_upsert_inserts_rows(tmp_path):
    conn = _mem_conn(tmp_path)
    n = ik.upsert_klines(conn, "BTCUSDT", "1m", _bars(_utc(2026, 2, 7, 0, 45), 5))
    assert n == 5
    assert conn.execute("SELECT COUNT(*) FROM crypto_klines_intraday").fetchone()[0] == 5


def test_upsert_is_idempotent(tmp_path):
    conn = _mem_conn(tmp_path)
    bars = _bars(_utc(2026, 2, 7, 0, 45), 5)
    ik.upsert_klines(conn, "BTCUSDT", "1m", bars)
    ik.upsert_klines(conn, "BTCUSDT", "1m", bars)  # re-run
    assert conn.execute("SELECT COUNT(*) FROM crypto_klines_intraday").fetchone()[0] == 5


def test_upsert_updates_conflicting_row(tmp_path):
    conn = _mem_conn(tmp_path)
    t = _utc(2026, 2, 7, 0, 45)
    ik.upsert_klines(conn, "BTCUSDT", "1m", [
        {"open_time": t, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 9},
    ])
    ik.upsert_klines(conn, "BTCUSDT", "1m", [
        {"open_time": t, "open": 10, "high": 20, "low": 5, "close": 15, "volume": 90},
    ])
    row = conn.execute(
        "SELECT open, high, low, close, volume FROM crypto_klines_intraday"
    ).fetchone()
    assert row == (10.0, 20.0, 5.0, 15.0, 90.0)
    assert conn.execute("SELECT COUNT(*) FROM crypto_klines_intraday").fetchone()[0] == 1


# ── backfill driver ─────────────────────────────────────────────────────


def test_backfill_writes_known_symbols(tmp_path):
    conn = _mem_conn(tmp_path)
    client = _FakeClient({
        "BTCUSDT": _bars(_utc(2026, 2, 7, 0, 45), 3),
        "ETHUSDT": _bars(_utc(2026, 2, 7, 0, 45), 4),
    })
    summary = ik.backfill_intraday(
        client, conn, symbols=["BTCUSDT", "ETHUSDT"], interval="1m",
        start=_utc(2026, 2, 7, 0, 0), end=_utc(2026, 2, 7, 1, 0),
    )
    assert summary["rows_written"] == 7
    assert summary["symbols_ok"] == 2
    assert summary["symbols_skipped"] == []
    assert conn.execute("SELECT COUNT(*) FROM crypto_klines_intraday").fetchone()[0] == 7


def test_backfill_skips_unknown_symbol(tmp_path):
    conn = _mem_conn(tmp_path)
    client = _FakeClient({
        "BTCUSDT": _bars(_utc(2026, 2, 7, 0, 45), 3),
        "NOPEUSDT": ValueError("Invalid symbol"),
    })
    summary = ik.backfill_intraday(
        client, conn, symbols=["BTCUSDT", "NOPEUSDT"], interval="1m",
        start=_utc(2026, 2, 7, 0, 0), end=_utc(2026, 2, 7, 1, 0),
    )
    assert summary["symbols_ok"] == 1
    assert summary["symbols_skipped"] == ["NOPEUSDT"]
    assert summary["rows_written"] == 3  # BTC still written despite the skip


def test_backfill_counts_gaps(tmp_path):
    conn = _mem_conn(tmp_path)
    # 3 contiguous minutes, then a 2-minute hole, then 1 more bar → 1 gap.
    bars = _bars(_utc(2026, 2, 7, 0, 45), 3)
    bars.append({
        "open_time": _utc(2026, 2, 7, 0, 50), "open": 5, "high": 6,
        "low": 4, "close": 5.5, "volume": 3,
    })
    client = _FakeClient({"BTCUSDT": bars})
    summary = ik.backfill_intraday(
        client, conn, symbols=["BTCUSDT"], interval="1m",
        start=_utc(2026, 2, 7, 0, 0), end=_utc(2026, 2, 7, 1, 0),
    )
    assert summary["gaps"] == 1
    assert summary["rows_written"] == 4


def test_backfill_idempotent_across_runs(tmp_path):
    conn = _mem_conn(tmp_path)
    client = _FakeClient({"BTCUSDT": _bars(_utc(2026, 2, 7, 0, 45), 5)})
    kw = dict(symbols=["BTCUSDT"], interval="1m",
              start=_utc(2026, 2, 7, 0, 0), end=_utc(2026, 2, 7, 1, 0))
    ik.backfill_intraday(client, conn, **kw)
    ik.backfill_intraday(client, conn, **kw)  # second pass must not duplicate
    assert conn.execute("SELECT COUNT(*) FROM crypto_klines_intraday").fetchone()[0] == 5
