from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import duckdb

logger = logging.getLogger("mhde.storage")

_DEFAULT_DB = "data/mhde.duckdb"
_SCHEMA_FILE = Path(__file__).parent / "schema.sql"

# DuckDB allows one writer at a time. When a long-running writer (e.g.
# nightly daily-analysis) holds the lock, services that fire on hourly
# schedules can collide and fail immediately with "Could not set lock".
# Retry with backoff so the hourly services self-recover instead of
# entering systemd's failed state.
_LOCK_RETRY_DELAYS_SEC = (30, 60, 120)


def ensure_data_dir(db_path: str = _DEFAULT_DB) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)


def _connect_with_lock_retry(db_path: str) -> duckdb.DuckDBPyConnection:
    attempts = len(_LOCK_RETRY_DELAYS_SEC) + 1
    for attempt in range(attempts):
        try:
            return duckdb.connect(db_path)
        except duckdb.IOException as exc:
            if "Could not set lock" not in str(exc) or attempt == attempts - 1:
                raise
            wait = _LOCK_RETRY_DELAYS_SEC[attempt]
            logger.warning(
                "DuckDB write lock held; retrying in %ds (attempt %d/%d): %s",
                wait, attempt + 1, attempts, exc,
            )
            time.sleep(wait)
    raise RuntimeError("unreachable")


def get_connection(db_path: str = _DEFAULT_DB) -> duckdb.DuckDBPyConnection:
    ensure_data_dir(db_path)
    return _connect_with_lock_retry(db_path)


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    ddl = _SCHEMA_FILE.read_text()
    # Execute each statement separately (DuckDB executemany not needed for DDL)
    statements = [s.strip() for s in ddl.split(";") if s.strip()]
    for stmt in statements:
        try:
            conn.execute(stmt)
        except Exception as exc:
            logger.warning("Schema statement failed (may already exist): %s", exc)
    logger.debug("Schema initialized")


def get_table_names(conn: duckdb.DuckDBPyConnection) -> list[str]:
    rows = conn.execute("SHOW TABLES").fetchall()
    return [r[0] for r in rows]


def table_exists(conn: duckdb.DuckDBPyConnection, table: str) -> bool:
    return table in get_table_names(conn)


def row_count(conn: duckdb.DuckDBPyConnection, table: str) -> int:
    if not table_exists(conn, table):
        return 0
    result = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return result[0] if result else 0
