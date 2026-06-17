"""Brain registry: a small SQLite-WAL DB holding the reader cursor and the
per-window snapshot bookkeeping.

SQLite-WAL (not DuckDB) is a deliberate, net-new choice for this repo: the
registry will be read concurrently by later consumers, and WAL lets readers run
without blocking the single writer — sidestepping the DuckDB single-writer
contention that ``mhde.duckdb`` and the engine both hit. The module exposes one
connect helper with the locking model documented here, mirroring the
``storage/db.py`` single-helper style rather than scattering ``sqlite3.connect``.

Restart-safety model (matches the house idiom: idempotent keyed sink + cursor):
  * ``reader_cursor`` is the monotonic high-water of processed ``recv_ts_ns`` —
    a restart resumes by reading rows with ``recv_ts_ns > cursor``.
  * ``snapshot_bookkeeping`` is keyed ``(dataset, symbol, window_start_ns)`` so a
    re-seen window is an idempotent no-op (INSERT OR IGNORE). Even if the cursor
    lagged a crash, a window is never double-counted.
  * :func:`advance` updates both in ONE transaction (atomic, no half-state).

This module owns only its registry file; it NEVER opens ``mhde.duckdb``, the
engine DB, or capture's store.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Iterable, Mapping

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reader_cursor (
    reader          TEXT    PRIMARY KEY,
    last_recv_ts_ns INTEGER NOT NULL,
    updated_at_ns   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_bookkeeping (
    dataset         TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    window_start_ns INTEGER NOT NULL,
    window_end_ns   INTEGER NOT NULL,
    recv_ts_ns      INTEGER NOT NULL,
    n_trades        INTEGER NOT NULL,
    written_at_ns   INTEGER NOT NULL,
    PRIMARY KEY (dataset, symbol, window_start_ns)
);
"""


def connect(path: str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the registry. Writable connections enable WAL and create the schema;
    read-only connections open the existing file ``mode=ro`` (no schema writes).
    """
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        return conn
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    # WAL: readers never block the lone writer. NORMAL is the durable-enough
    # WAL companion (a crash can lose only the last in-flight txn, which the
    # idempotent bookkeeping + cursor design tolerates).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def get_cursor(conn: sqlite3.Connection, reader: str) -> int:
    """Last processed ``recv_ts_ns`` for ``reader``; 0 if it has never advanced."""
    row = conn.execute(
        "SELECT last_recv_ts_ns FROM reader_cursor WHERE reader = ?", (reader,)
    ).fetchone()
    return int(row[0]) if row is not None else 0


def advance(
    conn: sqlite3.Connection,
    reader: str,
    *,
    new_recv_ts_ns: int,
    bookkeeping: Iterable[Mapping[str, object]] = (),
    now_ns: int = 0,
) -> None:
    """Atomically record ``bookkeeping`` windows and move the cursor forward.

    The cursor is a monotonic high-water: it never regresses below its current
    value. Bookkeeping rows are INSERT OR IGNORE on
    ``(dataset, symbol, window_start_ns)`` so re-seen windows are no-ops. Both
    happen in a single transaction.
    """
    rows = [
        (
            b["dataset"], b["symbol"], b["window_start_ns"], b["window_end_ns"],
            b["recv_ts_ns"], b["n_trades"], now_ns,
        )
        for b in bookkeeping
    ]
    with conn:  # one transaction; commits on success, rolls back on error
        if rows:
            conn.executemany(
                "INSERT OR IGNORE INTO snapshot_bookkeeping "
                "(dataset, symbol, window_start_ns, window_end_ns, recv_ts_ns, "
                " n_trades, written_at_ns) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        conn.execute(
            "INSERT INTO reader_cursor (reader, last_recv_ts_ns, updated_at_ns) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(reader) DO UPDATE SET "
            "  last_recv_ts_ns = MAX(last_recv_ts_ns, excluded.last_recv_ts_ns), "
            "  updated_at_ns = excluded.updated_at_ns",
            (reader, int(new_recv_ts_ns), int(now_ns)),
        )


def seen_windows(conn: sqlite3.Connection, dataset: str, symbol: str) -> set[int]:
    """The set of ``window_start_ns`` already recorded for ``(dataset, symbol)``."""
    return {
        int(r[0])
        for r in conn.execute(
            "SELECT window_start_ns FROM snapshot_bookkeeping "
            "WHERE dataset = ? AND symbol = ?",
            (dataset, symbol),
        )
    }
