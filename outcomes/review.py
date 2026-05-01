from __future__ import annotations

import logging

import duckdb

logger = logging.getLogger("mhde.outcomes.review")

_VALID_STATUSES = frozenset([
    "pending", "validated", "false_positive",
    "needs_more_time", "invalid_due_to_data_issue", "archived",
])


def get_pending_outcomes(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT candidate_id, ticker, as_of_date, tier, total_score,
               forward_return_20d, forward_return_60d, review_status
        FROM candidate_outcomes
        WHERE review_status = 'pending'
        ORDER BY as_of_date DESC
        """
    ).fetchall()
    cols = [
        "candidate_id", "ticker", "as_of_date", "tier", "total_score",
        "forward_return_20d", "forward_return_60d", "review_status",
    ]
    return [dict(zip(cols, r)) for r in rows]


def update_review_status(
    conn: duckdb.DuckDBPyConnection,
    candidate_id: str,
    status: str,
    notes: str | None = None,
) -> bool:
    if status not in _VALID_STATUSES:
        logger.error("Invalid review status: %s", status)
        return False
    try:
        conn.execute(
            """
            UPDATE candidate_outcomes
            SET review_status = ?, review_notes = ?, updated_at = CURRENT_TIMESTAMP
            WHERE candidate_id = ?
            """,
            [status, notes, candidate_id],
        )
        return True
    except Exception as exc:
        logger.error("Could not update review status: %s", exc)
        return False
