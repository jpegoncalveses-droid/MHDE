"""Compute ML labels (forward returns and binary hit/miss targets).

For each ticker-date in the ML universe, computes:
- Point-to-point forward returns (5d, 10d, 20d)
- Max forward return within window (best close)
- Max drawdown within window (worst close)
- Binary labels at various thresholds
"""
from __future__ import annotations

import logging

import duckdb

from ml.schema import create_all_tables

logger = logging.getLogger("mhde.ml.labels")

_UNIVERSE_FILTER = """
    SELECT ticker FROM companies
    WHERE market_cap >= 10000000000
      AND sector IS NOT NULL
      AND is_etf = false
      AND is_active = true
"""

_LABELS_QUERY = """
WITH universe AS ({universe_filter}),
prices AS (
    SELECT p.ticker, p.trade_date, p.adjusted_close,
           ROW_NUMBER() OVER (PARTITION BY p.ticker ORDER BY p.trade_date) AS rn
    FROM prices_daily p
    WHERE p.ticker IN (SELECT ticker FROM universe)
      AND p.adjusted_close > 0
),
forward_windows AS (
    SELECT
        a.ticker,
        a.trade_date,
        a.adjusted_close AS close_price,
        a.rn,
        -- 5-day forward
        (SELECT b.adjusted_close FROM prices b
         WHERE b.ticker = a.ticker AND b.rn = a.rn + 5) AS close_5d,
        (SELECT MAX(b.adjusted_close) FROM prices b
         WHERE b.ticker = a.ticker AND b.rn > a.rn AND b.rn <= a.rn + 5) AS max_close_5d,
        (SELECT MIN(b.adjusted_close) FROM prices b
         WHERE b.ticker = a.ticker AND b.rn > a.rn AND b.rn <= a.rn + 5) AS min_close_5d,
        -- 10-day forward
        (SELECT b.adjusted_close FROM prices b
         WHERE b.ticker = a.ticker AND b.rn = a.rn + 10) AS close_10d,
        (SELECT MAX(b.adjusted_close) FROM prices b
         WHERE b.ticker = a.ticker AND b.rn > a.rn AND b.rn <= a.rn + 10) AS max_close_10d,
        (SELECT MIN(b.adjusted_close) FROM prices b
         WHERE b.ticker = a.ticker AND b.rn > a.rn AND b.rn <= a.rn + 10) AS min_close_10d,
        -- 20-day forward
        (SELECT b.adjusted_close FROM prices b
         WHERE b.ticker = a.ticker AND b.rn = a.rn + 20) AS close_20d,
        (SELECT MAX(b.adjusted_close) FROM prices b
         WHERE b.ticker = a.ticker AND b.rn > a.rn AND b.rn <= a.rn + 20) AS max_close_20d,
        (SELECT MIN(b.adjusted_close) FROM prices b
         WHERE b.ticker = a.ticker AND b.rn > a.rn AND b.rn <= a.rn + 20) AS min_close_20d
    FROM prices a
)
SELECT
    ticker,
    trade_date,
    close_price,
    -- Point-to-point returns
    CASE WHEN close_5d IS NOT NULL THEN (close_5d / close_price) - 1 END AS fwd_return_5d,
    CASE WHEN close_10d IS NOT NULL THEN (close_10d / close_price) - 1 END AS fwd_return_10d,
    CASE WHEN close_20d IS NOT NULL THEN (close_20d / close_price) - 1 END AS fwd_return_20d,
    -- Max returns
    CASE WHEN max_close_5d IS NOT NULL THEN (max_close_5d / close_price) - 1 END AS fwd_max_return_5d,
    CASE WHEN max_close_10d IS NOT NULL THEN (max_close_10d / close_price) - 1 END AS fwd_max_return_10d,
    CASE WHEN max_close_20d IS NOT NULL THEN (max_close_20d / close_price) - 1 END AS fwd_max_return_20d,
    -- Max drawdowns
    CASE WHEN min_close_5d IS NOT NULL THEN (min_close_5d / close_price) - 1 END AS fwd_max_drawdown_5d,
    CASE WHEN min_close_10d IS NOT NULL THEN (min_close_10d / close_price) - 1 END AS fwd_max_drawdown_10d,
    CASE WHEN min_close_20d IS NOT NULL THEN (min_close_20d / close_price) - 1 END AS fwd_max_drawdown_20d,
    -- Binary labels
    CASE WHEN max_close_5d IS NOT NULL THEN (max_close_5d / close_price) - 1 >= 0.03 END AS label_5d_3pct,
    CASE WHEN max_close_5d IS NOT NULL THEN (max_close_5d / close_price) - 1 >= 0.05 END AS label_5d_5pct,
    CASE WHEN max_close_10d IS NOT NULL THEN (max_close_10d / close_price) - 1 >= 0.05 END AS label_10d_5pct,
    CASE WHEN max_close_10d IS NOT NULL THEN (max_close_10d / close_price) - 1 >= 0.08 END AS label_10d_8pct,
    CASE WHEN max_close_20d IS NOT NULL THEN (max_close_20d / close_price) - 1 >= 0.05 END AS label_20d_5pct,
    CASE WHEN max_close_20d IS NOT NULL THEN (max_close_20d / close_price) - 1 >= 0.08 END AS label_20d_8pct,
    CASE WHEN max_close_20d IS NOT NULL THEN (max_close_20d / close_price) - 1 >= 0.10 END AS label_20d_10pct,
    CASE WHEN max_close_20d IS NOT NULL THEN (max_close_20d / close_price) - 1 >= 0.15 END AS label_20d_15pct
FROM forward_windows
""".format(universe_filter=_UNIVERSE_FILTER)


def compute_labels(conn: duckdb.DuckDBPyConnection, batch_size: int = 50) -> int:
    """Compute and insert ML labels for all universe ticker-dates.

    Uses batched processing by ticker to manage memory.
    Returns total rows inserted.
    """
    create_all_tables(conn)

    tickers = [r[0] for r in conn.execute(_UNIVERSE_FILTER).fetchall()]
    logger.info("Computing labels for %d tickers", len(tickers))

    conn.execute("DELETE FROM ml_labels")

    total_inserted = 0
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        placeholders = ",".join(f"'{t}'" for t in batch)

        batch_query = """
        WITH prices AS (
            SELECT p.ticker, p.trade_date, p.adjusted_close,
                   ROW_NUMBER() OVER (PARTITION BY p.ticker ORDER BY p.trade_date) AS rn
            FROM prices_daily p
            WHERE p.ticker IN ({tickers})
              AND p.adjusted_close > 0
        ),
        forward_windows AS (
            SELECT
                a.ticker,
                a.trade_date,
                a.adjusted_close AS close_price,
                a.rn,
                (SELECT b.adjusted_close FROM prices b
                 WHERE b.ticker = a.ticker AND b.rn = a.rn + 5) AS close_5d,
                (SELECT MAX(b.adjusted_close) FROM prices b
                 WHERE b.ticker = a.ticker AND b.rn > a.rn AND b.rn <= a.rn + 5) AS max_close_5d,
                (SELECT MIN(b.adjusted_close) FROM prices b
                 WHERE b.ticker = a.ticker AND b.rn > a.rn AND b.rn <= a.rn + 5) AS min_close_5d,
                (SELECT b.adjusted_close FROM prices b
                 WHERE b.ticker = a.ticker AND b.rn = a.rn + 10) AS close_10d,
                (SELECT MAX(b.adjusted_close) FROM prices b
                 WHERE b.ticker = a.ticker AND b.rn > a.rn AND b.rn <= a.rn + 10) AS max_close_10d,
                (SELECT MIN(b.adjusted_close) FROM prices b
                 WHERE b.ticker = a.ticker AND b.rn > a.rn AND b.rn <= a.rn + 10) AS min_close_10d,
                (SELECT b.adjusted_close FROM prices b
                 WHERE b.ticker = a.ticker AND b.rn = a.rn + 20) AS close_20d,
                (SELECT MAX(b.adjusted_close) FROM prices b
                 WHERE b.ticker = a.ticker AND b.rn > a.rn AND b.rn <= a.rn + 20) AS max_close_20d,
                (SELECT MIN(b.adjusted_close) FROM prices b
                 WHERE b.ticker = a.ticker AND b.rn > a.rn AND b.rn <= a.rn + 20) AS min_close_20d
            FROM prices a
        )
        INSERT INTO ml_labels
        SELECT
            ticker, trade_date, close_price,
            CASE WHEN close_5d IS NOT NULL THEN (close_5d / close_price) - 1 END,
            CASE WHEN close_10d IS NOT NULL THEN (close_10d / close_price) - 1 END,
            CASE WHEN close_20d IS NOT NULL THEN (close_20d / close_price) - 1 END,
            CASE WHEN max_close_5d IS NOT NULL THEN (max_close_5d / close_price) - 1 END,
            CASE WHEN max_close_10d IS NOT NULL THEN (max_close_10d / close_price) - 1 END,
            CASE WHEN max_close_20d IS NOT NULL THEN (max_close_20d / close_price) - 1 END,
            CASE WHEN min_close_5d IS NOT NULL THEN (min_close_5d / close_price) - 1 END,
            CASE WHEN min_close_10d IS NOT NULL THEN (min_close_10d / close_price) - 1 END,
            CASE WHEN min_close_20d IS NOT NULL THEN (min_close_20d / close_price) - 1 END,
            CASE WHEN max_close_5d IS NOT NULL THEN (max_close_5d / close_price) - 1 >= 0.03 END,
            CASE WHEN max_close_5d IS NOT NULL THEN (max_close_5d / close_price) - 1 >= 0.05 END,
            CASE WHEN max_close_10d IS NOT NULL THEN (max_close_10d / close_price) - 1 >= 0.05 END,
            CASE WHEN max_close_10d IS NOT NULL THEN (max_close_10d / close_price) - 1 >= 0.08 END,
            CASE WHEN max_close_20d IS NOT NULL THEN (max_close_20d / close_price) - 1 >= 0.05 END,
            CASE WHEN max_close_20d IS NOT NULL THEN (max_close_20d / close_price) - 1 >= 0.08 END,
            CASE WHEN max_close_20d IS NOT NULL THEN (max_close_20d / close_price) - 1 >= 0.10 END,
            CASE WHEN max_close_20d IS NOT NULL THEN (max_close_20d / close_price) - 1 >= 0.15 END
        FROM forward_windows
        """.format(tickers=placeholders)

        conn.execute(batch_query)
        batch_count = conn.execute(
            f"SELECT COUNT(*) FROM ml_labels WHERE ticker IN ({placeholders})"
        ).fetchone()[0]
        total_inserted += batch_count

        if (i // batch_size + 1) % 2 == 0 or i + batch_size >= len(tickers):
            logger.info("  Processed %d/%d tickers (%d rows so far)",
                        min(i + batch_size, len(tickers)), len(tickers), total_inserted)

    logger.info("Labels complete: %d total rows", total_inserted)
    return total_inserted
