from __future__ import annotations

import logging
import uuid
from datetime import date

import duckdb

logger = logging.getLogger("mhde.outcomes")


def create_outcome_record(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    ticker: str,
    as_of_date: date,
    tier: str,
    total_score: float,
    reference_price: float | None,
) -> str:
    candidate_id = uuid.uuid4().hex[:16]
    try:
        conn.execute(
            """
            INSERT INTO candidate_outcomes
                (candidate_id, run_id, ticker, as_of_date, tier, total_score, reference_price)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id, ticker) DO NOTHING
            """,
            [candidate_id, run_id, ticker, as_of_date, tier, total_score, reference_price],
        )
        logger.debug("Outcome record created for %s run=%s", ticker, run_id)
    except Exception as exc:
        logger.error("Could not create outcome record for %s: %s", ticker, exc)
    return candidate_id


def update_forward_returns(
    conn: duckdb.DuckDBPyConnection,
    candidate_id: str,
    returns: dict,
) -> None:
    fields = [
        "forward_return_1d", "forward_return_3d", "forward_return_5d",
        "forward_return_10d", "forward_return_20d",
        "forward_return_60d", "forward_return_120d",
        "max_drawdown_20d", "max_drawdown_60d",
        "max_runup_20d", "max_runup_60d",
        "hit_10pct_before_down_10pct", "hit_20pct_before_down_10pct",
    ]
    set_clause = ", ".join(f"{f} = ?" for f in fields if f in returns)
    values = [returns[f] for f in fields if f in returns]
    if not set_clause:
        return
    try:
        conn.execute(
            f"UPDATE candidate_outcomes SET {set_clause} WHERE candidate_id = ?",
            values + [candidate_id],
        )
    except Exception as exc:
        logger.error("Could not update forward returns for %s: %s", candidate_id, exc)


def populate_forward_returns(
    conn: duckdb.DuckDBPyConnection,
    as_of_date: str,
) -> int:
    """Bulk-populate forward returns for all candidates where data is now available.

    For each NULL return window, looks up the closing price N trading days after
    as_of_date and computes (close / reference_price) - 1. Skips rows where
    reference_price is NULL or zero. Does NOT overwrite existing non-NULL values.

    Returns the total number of (candidate, window) cells updated.
    """
    windows: dict[str, int] = {
        "forward_return_1d": 1,
        "forward_return_3d": 3,
        "forward_return_5d": 5,
        "forward_return_10d": 10,
        "forward_return_20d": 20,
        "forward_return_60d": 60,
    }
    total_updated = 0
    for col, days in windows.items():
        try:
            conn.execute(f"""
                UPDATE candidate_outcomes co
                SET {col} = (
                    SELECT (p.close - co.reference_price) / co.reference_price
                    FROM prices_daily p
                    WHERE p.ticker = co.ticker
                      AND p.trade_date >= (co.as_of_date + INTERVAL '{days} days')
                    ORDER BY p.trade_date ASC
                    LIMIT 1
                )
                WHERE co.{col} IS NULL
                  AND co.reference_price IS NOT NULL
                  AND co.reference_price > 0
                  AND co.as_of_date <= DATE '{as_of_date}' - INTERVAL '{days} days'
            """)
            updated = conn.execute(f"""
                SELECT COUNT(*)
                FROM candidate_outcomes
                WHERE {col} IS NOT NULL
                  AND as_of_date <= DATE '{as_of_date}' - INTERVAL '{days} days'
                  AND reference_price IS NOT NULL
                  AND reference_price > 0
            """).fetchone()[0]
            total_updated += updated
        except Exception as exc:
            logger.warning("populate_forward_returns: %s — %s", col, exc)
    return total_updated
