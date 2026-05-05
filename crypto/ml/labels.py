"""Compute crypto ML labels (forward returns and binary targets).

For each symbol-date, computes forward returns and binary labels
using only data from crypto_prices_daily.
"""
from __future__ import annotations

import logging

import duckdb

from crypto.schema import create_all_tables

logger = logging.getLogger("mhde.crypto.labels")


def compute_labels(conn: duckdb.DuckDBPyConnection, batch_size: int = 50) -> int:
    create_all_tables(conn)

    symbols = [r[0] for r in conn.execute(
        "SELECT symbol FROM crypto_universe WHERE is_active = true ORDER BY rank_by_volume"
    ).fetchall()]

    if not symbols:
        logger.warning("No symbols in universe.")
        return 0

    conn.execute("DELETE FROM crypto_ml_labels")
    total = 0

    for batch_start in range(0, len(symbols), batch_size):
        batch = symbols[batch_start:batch_start + batch_size]
        placeholders = ",".join(f"'{s}'" for s in batch)

        query = f"""
        WITH prices AS (
            SELECT symbol, trade_date, close,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date) AS rn
            FROM crypto_prices_daily
            WHERE symbol IN ({placeholders})
              AND close > 0
        ),
        forward_windows AS (
            SELECT
                a.symbol,
                a.trade_date,
                a.close AS close_price,
                a.rn,
                (SELECT b.close FROM prices b WHERE b.symbol = a.symbol AND b.rn = a.rn + 1) AS close_1d,
                (SELECT MAX(b.close) FROM prices b WHERE b.symbol = a.symbol AND b.rn > a.rn AND b.rn <= a.rn + 1) AS max_1d,
                (SELECT MIN(b.close) FROM prices b WHERE b.symbol = a.symbol AND b.rn > a.rn AND b.rn <= a.rn + 1) AS min_1d,
                (SELECT b.close FROM prices b WHERE b.symbol = a.symbol AND b.rn = a.rn + 3) AS close_3d,
                (SELECT MAX(b.close) FROM prices b WHERE b.symbol = a.symbol AND b.rn > a.rn AND b.rn <= a.rn + 3) AS max_3d,
                (SELECT MIN(b.close) FROM prices b WHERE b.symbol = a.symbol AND b.rn > a.rn AND b.rn <= a.rn + 3) AS min_3d,
                (SELECT b.close FROM prices b WHERE b.symbol = a.symbol AND b.rn = a.rn + 5) AS close_5d,
                (SELECT MAX(b.close) FROM prices b WHERE b.symbol = a.symbol AND b.rn > a.rn AND b.rn <= a.rn + 5) AS max_5d,
                (SELECT MIN(b.close) FROM prices b WHERE b.symbol = a.symbol AND b.rn > a.rn AND b.rn <= a.rn + 5) AS min_5d,
                (SELECT b.close FROM prices b WHERE b.symbol = a.symbol AND b.rn = a.rn + 10) AS close_10d,
                (SELECT MAX(b.close) FROM prices b WHERE b.symbol = a.symbol AND b.rn > a.rn AND b.rn <= a.rn + 10) AS max_10d,
                (SELECT MIN(b.close) FROM prices b WHERE b.symbol = a.symbol AND b.rn > a.rn AND b.rn <= a.rn + 10) AS min_10d
            FROM prices a
        )
        INSERT INTO crypto_ml_labels
        SELECT
            symbol,
            trade_date,
            close_price,
            close_1d / close_price - 1 AS fwd_return_1d,
            close_3d / close_price - 1 AS fwd_return_3d,
            close_5d / close_price - 1 AS fwd_return_5d,
            close_10d / close_price - 1 AS fwd_return_10d,
            max_1d / close_price - 1 AS fwd_max_return_1d,
            max_3d / close_price - 1 AS fwd_max_return_3d,
            max_5d / close_price - 1 AS fwd_max_return_5d,
            max_10d / close_price - 1 AS fwd_max_return_10d,
            min_1d / close_price - 1 AS fwd_max_drawdown_1d,
            min_3d / close_price - 1 AS fwd_max_drawdown_3d,
            min_5d / close_price - 1 AS fwd_max_drawdown_5d,
            min_10d / close_price - 1 AS fwd_max_drawdown_10d,
            (max_1d / close_price - 1) >= 0.05 AS label_1d_5pct,
            (max_1d / close_price - 1) >= 0.03 AS label_1d_3pct,
            (max_3d / close_price - 1) >= 0.05 AS label_3d_5pct,
            (max_3d / close_price - 1) >= 0.10 AS label_3d_10pct,
            (max_5d / close_price - 1) >= 0.10 AS label_5d_10pct,
            (max_5d / close_price - 1) >= 0.15 AS label_5d_15pct,
            (max_10d / close_price - 1) >= 0.10 AS label_10d_10pct,
            (max_10d / close_price - 1) >= 0.15 AS label_10d_15pct,
            (max_10d / close_price - 1) >= 0.20 AS label_10d_20pct
        FROM forward_windows
        WHERE close_1d IS NOT NULL
        """

        conn.execute(query)
        batch_count = conn.execute(f"""
            SELECT COUNT(*) FROM crypto_ml_labels WHERE symbol IN ({placeholders})
        """).fetchone()[0]
        total += batch_count
        logger.info("  Batch %d-%d: %d labels", batch_start + 1, batch_start + len(batch), batch_count)

    logger.info("Total labels computed: %d", total)
    return total
