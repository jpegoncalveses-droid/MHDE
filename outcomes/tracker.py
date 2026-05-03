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
