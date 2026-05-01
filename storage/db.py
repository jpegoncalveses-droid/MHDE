from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import duckdb

logger = logging.getLogger("mhde.storage")

_DEFAULT_DB = "data/mhde.duckdb"
_SCHEMA_FILE = Path(__file__).parent / "schema.sql"


def ensure_data_dir(db_path: str = _DEFAULT_DB) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)


def get_connection(db_path: str = _DEFAULT_DB) -> duckdb.DuckDBPyConnection:
    ensure_data_dir(db_path)
    conn = duckdb.connect(db_path)
    return conn


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
