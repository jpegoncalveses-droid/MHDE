from __future__ import annotations

import logging
from datetime import datetime, timedelta

import duckdb

logger = logging.getLogger("mhde.notifications.dedupe")


def is_duplicate(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    channel: str,
    window_days: int = 14,
) -> bool:
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    rows = conn.execute(
        """
        SELECT 1 FROM alerts
        WHERE ticker = ? AND channel = ? AND status = 'sent' AND sent_at >= ?
        LIMIT 1
        """,
        [ticker, channel, cutoff],
    ).fetchall()
    return len(rows) > 0
