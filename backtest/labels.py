from __future__ import annotations

import logging
from datetime import date

import duckdb

logger = logging.getLogger("mhde.backtest.labels")


def compute_labels(
    conn: duckdb.DuckDBPyConnection,
    tickers: list[str],
    as_of_date: date,
    forward_days: int = 20,
) -> list[dict]:
    results = []
    insufficient = []

    for ticker in tickers:
        rows = conn.execute(
            """
            SELECT trade_date, close FROM prices_daily
            WHERE ticker = ? AND trade_date >= ?
            ORDER BY trade_date ASC
            LIMIT ?
            """,
            [ticker, as_of_date, forward_days + 5],
        ).fetchall()

        if len(rows) < forward_days:
            insufficient.append(ticker)
            continue

        ref = rows[0][1]
        fwd = rows[min(forward_days, len(rows) - 1)][1]
        if ref and fwd:
            results.append({
                "ticker": ticker,
                "as_of_date": as_of_date,
                "reference_price": ref,
                "forward_price": fwd,
                "forward_return": (fwd - ref) / ref,
                "forward_days": forward_days,
            })

    if insufficient:
        logger.warning(
            "Insufficient price history for %d tickers (need %d days)",
            len(insufficient), forward_days,
        )

    return results
