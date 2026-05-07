from __future__ import annotations

import logging

import duckdb

logger = logging.getLogger("mhde.models.registry")


def get_latest_model_run(conn: duckdb.DuckDBPyConnection) -> dict | None:
    rows = conn.execute(
        """
        SELECT model_run_id, model_type, status, warning, metrics_json,
               feature_importance_json, created_at
        FROM model_runs
        ORDER BY created_at DESC
        LIMIT 1
        """
    ).fetchall()

    if not rows:
        return None

    import json
    r = rows[0]
    return {
        "model_run_id": r[0],
        "model_type": r[1],
        "status": r[2],
        "warning": r[3],
        "metrics": json.loads(r[4]) if r[4] else {},
        "feature_importance": json.loads(r[5]) if r[5] else {},
        "created_at": r[6],
    }
