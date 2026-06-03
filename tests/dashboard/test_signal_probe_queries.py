"""Unit tests for the Signal Probe tab's read-only snapshot loader.

The loader reads the signal-probe research DuckDB (written by the collector,
the single writer). The Streamlit ``with tab_probe:`` block itself is not
unit-tested (consistent with the rest of ``dashboard/app.py``); only the pure
query/transform helper in ``dashboard.services.queries`` is exercised here.

A real on-disk DuckDB file is built in ``tmp_path`` — the loader's whole point
is the ATTACH-read-only + COPY-FROM-DATABASE + DETACH dance, which only means
anything against a file, not an in-memory handle.
"""
from __future__ import annotations

from datetime import datetime

import duckdb
import pandas as pd

from dashboard.services import queries as q


def _build_probe_db(path: str) -> None:
    conn = duckdb.connect(path)
    conn.execute(
        """
        CREATE TABLE signal_probe (
            ts TIMESTAMP, symbol VARCHAR, close DOUBLE, roc_1m DOUBLE,
            PRIMARY KEY (symbol, ts)
        )
        """
    )
    conn.executemany(
        "INSERT INTO signal_probe (ts, symbol, close, roc_1m) VALUES (?, ?, ?, ?)",
        [
            (datetime(2026, 6, 2, 22, 0), "BTCUSDT", 100.0, 0.5),
            (datetime(2026, 6, 2, 22, 0), "ETHUSDT", 50.0, None),
            (datetime(2026, 6, 2, 22, 1), "BTCUSDT", 101.0, 1.0),
        ],
    )
    conn.close()


def test_load_signal_probe_snapshot_returns_all_rows_ordered(tmp_path):
    db = str(tmp_path / "signal_probe.duckdb")
    _build_probe_db(db)

    df = q.load_signal_probe_snapshot(db)

    assert isinstance(df, pd.DataFrame)
    assert len(df) == 3
    assert list(df.columns) == ["ts", "symbol", "close", "roc_1m"]
    # Ordered ts DESC, then symbol ASC: latest minute first.
    assert df.iloc[0]["ts"] == pd.Timestamp("2026-06-02 22:01:00")
    assert df.iloc[0]["symbol"] == "BTCUSDT"
    # NULL features survive as NaN (not dropped).
    assert pd.isna(df[df["symbol"] == "ETHUSDT"]["roc_1m"].iloc[0])


def test_load_signal_probe_snapshot_does_not_lock_the_file(tmp_path):
    """After the snapshot read the file must be openable read-write — proving
    the loader DETACHed and never holds the collector out."""
    db = str(tmp_path / "signal_probe.duckdb")
    _build_probe_db(db)

    q.load_signal_probe_snapshot(db)

    # The collector (a writer) must still be able to open it.
    writer = duckdb.connect(db)  # read-write
    writer.execute(
        "INSERT INTO signal_probe (ts, symbol, close, roc_1m) VALUES (?, ?, ?, ?)",
        [datetime(2026, 6, 2, 22, 2), "BTCUSDT", 102.0, 1.0],
    )
    writer.close()


def test_signal_probe_db_path_env_override(monkeypatch):
    monkeypatch.delenv(q.SIGNAL_PROBE_DB_ENV, raising=False)
    assert q.signal_probe_db_path() == "data/research/signal_probe.duckdb"
    monkeypatch.setenv(q.SIGNAL_PROBE_DB_ENV, "/tmp/custom.duckdb")
    assert q.signal_probe_db_path() == "/tmp/custom.duckdb"
