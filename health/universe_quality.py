"""Universe quality guard: marks companies with SEC reporting issues."""
from __future__ import annotations

import logging
from datetime import datetime

import duckdb

logger = logging.getLogger("mhde.health.universe_quality")


def mark_inactive_sec_reporter(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    reason: str,
) -> None:
    """Mark a company as an inactive SEC reporter with an exclusion reason."""
    conn.execute(
        """UPDATE companies
           SET active_sec_reporter = false,
               has_financial_reporting_forms = false,
               universe_exclusion_reason = ?,
               updated_at = ?
           WHERE ticker = ?""",
        [reason, datetime.utcnow(), ticker],
    )
    logger.info("Marked %s as inactive SEC reporter: %s", ticker, reason)


def get_active_universe_tickers(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """Return tickers that are active in the universe and have not been excluded."""
    rows = conn.execute(
        """SELECT ticker FROM companies
           WHERE is_active = true
             AND (active_sec_reporter IS NULL OR active_sec_reporter = true)"""
    ).fetchall()
    return [r[0] for r in rows]
