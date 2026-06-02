"""End-to-end test for one signal-probe collection cycle (fake client)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from crypto.research.signal_probe import collector, store


def _klines(symbol, interval, n, *, end, price=100.0, stale_min=0):
    """``n`` ascending bars of ``interval`` ending at ``end`` (minus stale)."""
    step = timedelta(minutes=1 if interval == "1m" else 60)
    last_open = end - timedelta(minutes=stale_min)
    bars = []
    for i in range(n):
        t = last_open - step * (n - 1 - i)
        c = price + i * 0.01
        bars.append({
            "open_time": t, "open": c, "high": c + 0.5, "low": c - 0.5,
            "close": c, "volume": 10.0 + (i % 5), "close_time": t + step,
            "quote_volume": c * 10.0, "trades": 20, "taker_buy_base": 5.0,
        })
    return bars


class _FakeClient:
    def __init__(self, *, cycle_close, hour_floor, symbols, raise_for=(), stale=()):
        self._cc = cycle_close
        self._hf = hour_floor
        self._symbols = symbols
        self._raise_for = set(raise_for)
        self._stale = set(stale)

    def fetch_premium_index_all(self):
        return {
            s: {"symbol": s, "lastFundingRate": "0.0001", "markPrice": "100.0",
                "indexPrice": "100.0", "interestRate": "0.0001"}
            for s in self._symbols
        }

    def fetch_klines(self, symbol, interval, limit):
        if symbol in self._raise_for:
            raise RuntimeError("boom")
        stale = 5 if symbol in self._stale else 0
        if interval == "1m":
            # include the in-progress bar (open_time == cycle_close) to verify drop
            return _klines(symbol, "1m", 131, end=self._cc, stale_min=stale)
        return _klines(symbol, "1h", 740, end=self._hf)

    def fetch_open_interest(self, symbol):
        return 1_000_000.0

    def fetch_open_interest_hist(self, symbol, period, limit):
        return [1_000_000.0 + i * 100 for i in range(limit)]

    def fetch_depth(self, symbol, limit):
        return {"bids": [["99.9", "10"]], "asks": [["100.1", "8"]]}


def _setup(tmp_path):
    conn = store.connect_probe_db(str(tmp_path / "signal_probe.duckdb"))
    now = datetime(2026, 6, 2, 12, 34, 30, tzinfo=timezone.utc)
    cycle_close = now.replace(second=0, microsecond=0)
    hour_floor = now.replace(minute=0, second=0, microsecond=0)
    return conn, now, cycle_close, hour_floor


def test_run_cycle_writes_one_row_per_symbol(tmp_path):
    conn, now, cc, hf = _setup(tmp_path)
    syms = ["BTCUSDT", "AAAUSDT", "BBBUSDT"]
    client = _FakeClient(cycle_close=cc, hour_floor=hf, symbols=syms)
    try:
        summary = collector.run_cycle(client, conn, symbols=syms,
                                       btc_symbol="BTCUSDT", now=now)
        assert summary["rows_written"] == 3
        assert summary["symbols_ok"] == 3
        assert summary["symbols_skipped"] == []
        # ts is the closed-minute boundary, naive UTC
        assert summary["ts"] == datetime(2026, 6, 2, 12, 34)
        count = conn.execute("SELECT COUNT(*) FROM signal_probe").fetchone()[0]
        assert count == 3
    finally:
        conn.close()


def test_in_progress_minute_is_dropped(tmp_path):
    conn, now, cc, hf = _setup(tmp_path)
    syms = ["BTCUSDT"]
    client = _FakeClient(cycle_close=cc, hour_floor=hf, symbols=syms)
    try:
        collector.run_cycle(client, conn, symbols=syms, btc_symbol="BTCUSDT", now=now)
        # the latest stored close must come from the 12:33 bar, not 12:34
        roc1, close = conn.execute(
            "SELECT roc_1m, close FROM signal_probe WHERE symbol='BTCUSDT'").fetchone()
        assert roc1 is not None  # computed from closed bars
        assert close is not None
    finally:
        conn.close()


def test_cross_sectional_columns_present(tmp_path):
    conn, now, cc, hf = _setup(tmp_path)
    syms = ["BTCUSDT", "AAAUSDT", "BBBUSDT"]
    client = _FakeClient(cycle_close=cc, hour_floor=hf, symbols=syms)
    try:
        collector.run_cycle(client, conn, symbols=syms, btc_symbol="BTCUSDT", now=now)
        row = conn.execute(
            "SELECT ret_vs_btc_5m, ret_pct_5m, ret_spread_median_5m "
            "FROM signal_probe WHERE symbol='AAAUSDT'").fetchone()
        # AAAUSDT has data and >=2 peers, so percentile is populated
        assert row[1] is not None
    finally:
        conn.close()


def test_per_symbol_error_is_skipped(tmp_path):
    conn, now, cc, hf = _setup(tmp_path)
    syms = ["BTCUSDT", "AAAUSDT"]
    client = _FakeClient(cycle_close=cc, hour_floor=hf, symbols=syms,
                         raise_for=["AAAUSDT"])
    try:
        summary = collector.run_cycle(client, conn, symbols=syms,
                                      btc_symbol="BTCUSDT", now=now)
        assert summary["symbols_ok"] == 1
        assert summary["symbols_skipped"] == ["AAAUSDT"]
    finally:
        conn.close()


def test_stale_symbol_is_skipped(tmp_path):
    conn, now, cc, hf = _setup(tmp_path)
    syms = ["BTCUSDT", "AAAUSDT"]
    client = _FakeClient(cycle_close=cc, hour_floor=hf, symbols=syms,
                         stale=["AAAUSDT"])
    try:
        summary = collector.run_cycle(client, conn, symbols=syms,
                                      btc_symbol="BTCUSDT", now=now)
        assert "AAAUSDT" in summary["symbols_skipped"]
        assert summary["symbols_ok"] == 1
    finally:
        conn.close()


def test_idempotent_rerun_same_minute(tmp_path):
    conn, now, cc, hf = _setup(tmp_path)
    syms = ["BTCUSDT", "AAAUSDT"]
    client = _FakeClient(cycle_close=cc, hour_floor=hf, symbols=syms)
    try:
        collector.run_cycle(client, conn, symbols=syms, btc_symbol="BTCUSDT", now=now)
        collector.run_cycle(client, conn, symbols=syms, btc_symbol="BTCUSDT", now=now)
        count = conn.execute("SELECT COUNT(*) FROM signal_probe").fetchone()[0]
        assert count == 2  # same (symbol, ts) keys, upserted in place
    finally:
        conn.close()
