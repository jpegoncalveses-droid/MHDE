from __future__ import annotations

import json
import uuid
from datetime import datetime

import duckdb

from learning.error_taxonomy import EXPERIMENT_STATUSES


def propose_experiment(
    conn: duckdb.DuckDBPyConnection,
    hypothesis: str,
    proposed_change: dict,
    affected_components: list[str],
    expected_effect: str,
    based_on_run_ids: list[str] | None = None,
) -> str:
    experiment_id = uuid.uuid4().hex[:16]
    now = datetime.utcnow()
    conn.execute(
        """
        INSERT INTO scorecard_experiments (
            experiment_id, based_on_run_ids, hypothesis,
            proposed_change_json, affected_components_json,
            expected_effect, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
        """,
        [
            experiment_id,
            json.dumps(based_on_run_ids or []),
            hypothesis,
            json.dumps(proposed_change),
            json.dumps(affected_components),
            expected_effect,
            now, now,
        ],
    )
    return experiment_id


def get_experiments(conn: duckdb.DuckDBPyConnection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        SELECT experiment_id, based_on_run_ids, hypothesis, proposed_change_json,
               affected_components_json, expected_effect, backtest_result_json,
               status, review_notes, approved_by, applied_at, created_at, updated_at
        FROM scorecard_experiments
        ORDER BY created_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [
        "experiment_id", "based_on_run_ids", "hypothesis", "proposed_change_json",
        "affected_components_json", "expected_effect", "backtest_result_json",
        "status", "review_notes", "approved_by", "applied_at", "created_at", "updated_at",
    ]
    result = []
    for r in rows:
        d = dict(zip(cols, r))
        for field in ("based_on_run_ids", "proposed_change_json", "affected_components_json", "backtest_result_json"):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except Exception:
                    pass
        result.append(d)
    return result


def approve_experiment(
    conn: duckdb.DuckDBPyConnection,
    experiment_id: str,
    approved_by: str,
    review_notes: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE scorecard_experiments
        SET status = 'approved', approved_by = ?, review_notes = ?, updated_at = ?
        WHERE experiment_id = ?
        """,
        [approved_by, review_notes, datetime.utcnow(), experiment_id],
    )


def reject_experiment(
    conn: duckdb.DuckDBPyConnection,
    experiment_id: str,
    review_notes: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE scorecard_experiments
        SET status = 'rejected', review_notes = ?, updated_at = ?
        WHERE experiment_id = ?
        """,
        [review_notes, datetime.utcnow(), experiment_id],
    )
