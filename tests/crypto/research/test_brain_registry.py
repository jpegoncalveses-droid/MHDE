"""Tests for the brain SQLite-WAL registry (reader cursor + snapshot bookkeeping).

The registry is the brain's own bookkeeping domain — SQLite-WAL (not DuckDB) so
later concurrent readers never contend with the writer. It holds the resumable
reader cursor (last processed recv_ts_ns) and an idempotent per-window
bookkeeping table; the two update atomically.
"""
from __future__ import annotations

from crypto.research.brain import registry


def _bk(symbol="BTCUSDT", *, window_start_ns=1000, n=3, recv=500):
    return {
        "dataset": "trades",
        "symbol": symbol,
        "window_start_ns": window_start_ns,
        "window_end_ns": window_start_ns + 60,
        "recv_ts_ns": recv,
        "n_trades": n,
    }


def test_connect_enables_wal_and_creates_tables(tmp_path):
    conn = registry.connect(str(tmp_path / "registry.sqlite"))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"reader_cursor", "snapshot_bookkeeping"} <= tables
    finally:
        conn.close()


def test_cursor_defaults_to_zero_when_absent(tmp_path):
    conn = registry.connect(str(tmp_path / "r.sqlite"))
    try:
        assert registry.get_cursor(conn, "trades") == 0
    finally:
        conn.close()


def test_advance_sets_cursor_and_is_monotonic(tmp_path):
    conn = registry.connect(str(tmp_path / "r.sqlite"))
    try:
        registry.advance(conn, "trades", new_recv_ts_ns=100, bookkeeping=[_bk()], now_ns=1)
        assert registry.get_cursor(conn, "trades") == 100
        # A lower value must NOT regress the cursor (monotonic high-water).
        registry.advance(conn, "trades", new_recv_ts_ns=50, bookkeeping=[], now_ns=2)
        assert registry.get_cursor(conn, "trades") == 100
        registry.advance(conn, "trades", new_recv_ts_ns=200, bookkeeping=[], now_ns=3)
        assert registry.get_cursor(conn, "trades") == 200
    finally:
        conn.close()


def test_bookkeeping_is_idempotent_on_window_pk(tmp_path):
    conn = registry.connect(str(tmp_path / "r.sqlite"))
    try:
        registry.advance(conn, "trades", new_recv_ts_ns=100,
                         bookkeeping=[_bk(window_start_ns=1000, n=3)], now_ns=1)
        # Re-recording the same (dataset, symbol, window_start) is ignored — one row.
        registry.advance(conn, "trades", new_recv_ts_ns=110,
                         bookkeeping=[_bk(window_start_ns=1000, n=99)], now_ns=2)
        rows = conn.execute(
            "SELECT n_trades FROM snapshot_bookkeeping WHERE window_start_ns=1000"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 3  # first write wins (INSERT OR IGNORE)
        assert registry.seen_windows(conn, "trades", "BTCUSDT") == {1000}
    finally:
        conn.close()


def test_seen_windows_is_scoped_by_symbol(tmp_path):
    conn = registry.connect(str(tmp_path / "r.sqlite"))
    try:
        registry.advance(conn, "trades", new_recv_ts_ns=100, bookkeeping=[
            _bk("BTCUSDT", window_start_ns=1000),
            _bk("BTCUSDT", window_start_ns=2000),
            _bk("ETHUSDT", window_start_ns=3000),
        ], now_ns=1)
        assert registry.seen_windows(conn, "trades", "BTCUSDT") == {1000, 2000}
        assert registry.seen_windows(conn, "trades", "ETHUSDT") == {3000}
    finally:
        conn.close()


def test_resume_from_persisted_cursor_on_a_fresh_connection(tmp_path):
    path = str(tmp_path / "r.sqlite")
    conn = registry.connect(path)
    registry.advance(conn, "trades", new_recv_ts_ns=777,
                     bookkeeping=[_bk(window_start_ns=1000)], now_ns=1)
    conn.close()
    conn2 = registry.connect(path)
    try:
        assert registry.get_cursor(conn2, "trades") == 777
        assert registry.seen_windows(conn2, "trades", "BTCUSDT") == {1000}
    finally:
        conn2.close()


def test_read_only_connection_can_read_cursor(tmp_path):
    path = str(tmp_path / "r.sqlite")
    conn = registry.connect(path)
    registry.advance(conn, "trades", new_recv_ts_ns=42, bookkeeping=[], now_ns=1)
    conn.close()
    ro = registry.connect(path, read_only=True)
    try:
        assert registry.get_cursor(ro, "trades") == 42
    finally:
        ro.close()
