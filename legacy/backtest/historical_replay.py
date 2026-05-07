from __future__ import annotations

import logging
from datetime import date, timedelta

import duckdb

logger = logging.getLogger("mhde.backtest.replay")


def replay(
    conn: duckdb.DuckDBPyConnection,
    as_of_date: date | None = None,
    lookback_days: int = 90,
) -> list[dict]:
    if as_of_date is None:
        as_of_date = date.today()

    cutoff = as_of_date - timedelta(days=lookback_days)
    rows = conn.execute(
        """
        SELECT run_id, ticker, as_of_date, total_score, tier
        FROM scores
        WHERE as_of_date >= ? AND as_of_date <= ?
        ORDER BY as_of_date DESC, total_score DESC
        """,
        [cutoff, as_of_date],
    ).fetchall()

    if not rows:
        logger.warning(
            "No historical scores found between %s and %s. "
            "Run 'score' multiple times over several days to build history.",
            cutoff, as_of_date,
        )
        return []

    cols = ["run_id", "ticker", "as_of_date", "total_score", "tier"]
    return [dict(zip(cols, r)) for r in rows]
