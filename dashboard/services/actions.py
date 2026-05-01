from __future__ import annotations

import os

import duckdb


def _connect() -> duckdb.DuckDBPyConnection:
    db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")
    return duckdb.connect(db_path)


def update_hypothesis_status(hypothesis_id: str, status: str, note: str | None = None) -> bool:
    valid = {"new", "watch", "research", "rejected", "archived"}
    if status not in valid:
        return False
    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE hypotheses
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE hypothesis_id = ?
            """,
            [status, hypothesis_id],
        )
        return True
    except Exception:
        return False
    finally:
        conn.close()


def update_outcome_review(candidate_id: str, status: str, notes: str) -> bool:
    from outcomes.review import update_review_status
    conn = _connect()
    try:
        return update_review_status(conn, candidate_id, status, notes)
    finally:
        conn.close()
