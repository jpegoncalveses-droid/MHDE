"""Tests for the signal-probe research store (schema + idempotent UPSERT)."""
from __future__ import annotations

from datetime import datetime

import pytest

from crypto.research.signal_probe import store


def _conn(tmp_path):
    return store.connect_probe_db(str(tmp_path / "signal_probe.duckdb"))


def _row(symbol, ts, close):
    return {"symbol": symbol, "ts": ts, "close": close, "roc_1m": 0.01,
            "trades": 42, "depth_imbalance": None}


def test_schema_created_and_table_present(tmp_path):
    conn = _conn(tmp_path)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'signal_probe'").fetchall()}
        # spot-check a representative spread of declared columns
        for col in ("ts", "symbol", "roc_60m", "oi_change_1h",
                    "ret_pct_5m", "spread_bps", "predicted_funding_rate"):
            assert col in names
    finally:
        conn.close()


def test_upsert_writes_and_missing_keys_are_null(tmp_path):
    conn = _conn(tmp_path)
    try:
        ts = datetime(2026, 6, 2, 12, 34)
        n = store.upsert_rows(conn, [_row("BTCUSDT", ts, 100.0)])
        assert n == 1
        close, roc1, funding = conn.execute(
            "SELECT close, roc_1m, predicted_funding_rate FROM signal_probe "
            "WHERE symbol = 'BTCUSDT'").fetchone()
        assert close == pytest.approx(100.0)
        assert roc1 == pytest.approx(0.01)
        assert funding is None  # key absent -> NULL
    finally:
        conn.close()


def test_upsert_idempotent_on_symbol_ts(tmp_path):
    conn = _conn(tmp_path)
    try:
        ts = datetime(2026, 6, 2, 12, 34)
        store.upsert_rows(conn, [_row("BTCUSDT", ts, 100.0)])
        store.upsert_rows(conn, [_row("BTCUSDT", ts, 222.0)])  # same key, new value
        rows = conn.execute(
            "SELECT close FROM signal_probe WHERE symbol = 'BTCUSDT'").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == pytest.approx(222.0)  # overwritten
    finally:
        conn.close()


def test_upsert_empty_is_noop(tmp_path):
    conn = _conn(tmp_path)
    try:
        assert store.upsert_rows(conn, []) == 0
        assert conn.execute("SELECT COUNT(*) FROM signal_probe").fetchone()[0] == 0
    finally:
        conn.close()
