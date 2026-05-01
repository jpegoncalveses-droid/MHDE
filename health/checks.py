from __future__ import annotations

import logging
import uuid
from datetime import datetime

import duckdb

from health.data_quality import (
    check_universe_size,
    check_price_data,
    check_fundamental_data,
    check_feature_coverage,
    check_score_distribution,
)
from health.source_status import (
    check_source_runs,
    check_llm_failures,
    check_notification_failures,
)

logger = logging.getLogger("mhde.health")


def _db_reachable(conn: duckdb.DuckDBPyConnection, db_path: str) -> dict:
    try:
        conn.execute("SELECT 1").fetchone()
        return {"check_name": "database_reachable", "status": "pass", "severity": "low",
                "message": f"DuckDB at {db_path}"}
    except Exception as exc:
        return {"check_name": "database_reachable", "status": "fail", "severity": "critical",
                "message": str(exc)}


def _schema_exists(conn: duckdb.DuckDBPyConnection) -> dict:
    try:
        tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
        required = ["companies", "features", "scores", "hypotheses"]
        missing = [t for t in required if t not in tables]
        if missing:
            return {"check_name": "schema_exists", "status": "fail", "severity": "critical",
                    "message": f"Missing tables: {missing}"}
        return {"check_name": "schema_exists", "status": "pass", "severity": "low",
                "message": f"{len(tables)} tables present"}
    except Exception as exc:
        return {"check_name": "schema_exists", "status": "fail", "severity": "critical",
                "message": str(exc)}


def _persist_checks(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    results: list[dict],
) -> None:
    now = datetime.utcnow()
    for r in results:
        try:
            conn.execute(
                """
                INSERT INTO health_checks
                    (id, run_id, check_name, status, severity, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    uuid.uuid4().hex[:16],
                    run_id,
                    r["check_name"],
                    r["status"],
                    r.get("severity", "medium"),
                    r.get("message", ""),
                    now,
                ],
            )
        except Exception as exc:
            logger.debug("Could not persist health check %s: %s", r.get("check_name"), exc)


def run_all_checks(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    cfg: dict,
) -> list[dict]:
    db_path = cfg.get("db_path", "data/mhde.duckdb")
    results: list[dict] = []

    results.append(_db_reachable(conn, db_path))
    results.append(_schema_exists(conn))
    results.append(check_universe_size(conn))
    results.append(check_price_data(conn))
    results.append(check_fundamental_data(conn))
    results.append(check_feature_coverage(conn))
    results.append(check_score_distribution(conn))
    results.extend(check_source_runs(conn))
    results.append(check_llm_failures(conn))
    results.append(check_notification_failures(conn))

    _persist_checks(conn, run_id, results)
    return results
