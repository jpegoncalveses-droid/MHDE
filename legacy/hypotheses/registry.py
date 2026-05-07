from __future__ import annotations

from datetime import datetime

import duckdb


def get_open_hypotheses(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM hypotheses WHERE status IN ('new', 'watch', 'research') ORDER BY total_score DESC"
    ).fetchall()
    cols = [d[0] for d in conn.description]
    return [dict(zip(cols, r)) for r in rows]


def update_status(
    conn: duckdb.DuckDBPyConnection,
    hypothesis_id: str,
    status: str,
) -> None:
    conn.execute(
        "UPDATE hypotheses SET status = ?, updated_at = ? WHERE hypothesis_id = ?",
        [status, datetime.utcnow(), hypothesis_id],
    )
