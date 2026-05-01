from __future__ import annotations

import logging

import duckdb

from storage.db import init_schema

logger = logging.getLogger("mhde.storage.migrations")

_CURRENT_VERSION = 2


def run_migrations(conn: duckdb.DuckDBPyConnection) -> None:
    init_schema(conn)
    try:
        result = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = result[0] if result and result[0] is not None else 0
    except Exception:
        current = 0

    if current < 1:
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (1, 'Initial schema') ON CONFLICT DO NOTHING"
        )
        logger.info("Applied migration v1: initial schema")

    if current < 2:
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (2, 'Learning loop: candidate_reviews + scorecard_experiments') ON CONFLICT DO NOTHING"
        )
        logger.info("Applied migration v2: learning loop tables")
