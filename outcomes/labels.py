from __future__ import annotations

import logging
from datetime import date

import duckdb

logger = logging.getLogger("mhde.outcomes.labels")


def compute_forward_returns(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    as_of_date: date,
) -> dict:
    rows = conn.execute(
        """
        SELECT trade_date, close FROM prices_daily
        WHERE ticker = ? AND trade_date >= ?
        ORDER BY trade_date ASC
        LIMIT 130
        """,
        [ticker, as_of_date],
    ).fetchall()

    if not rows or len(rows) < 2:
        return {}

    prices = [(r[0], r[1]) for r in rows]
    ref_price = prices[0][1]
    if not ref_price:
        return {}

    def ret_at(n: int) -> float | None:
        if len(prices) > n:
            return (prices[n][1] - ref_price) / ref_price
        return None

    def max_drawdown(n: int) -> float | None:
        window = [p[1] for p in prices[1:n+1] if p[1]]
        if not window:
            return None
        return min(p / ref_price - 1.0 for p in window)

    def max_runup(n: int) -> float | None:
        window = [p[1] for p in prices[1:n+1] if p[1]]
        if not window:
            return None
        return max(p / ref_price - 1.0 for p in window)

    result: dict = {}
    for days, key in [(1, "forward_return_1d"), (5, "forward_return_5d"),
                      (20, "forward_return_20d"), (60, "forward_return_60d"),
                      (120, "forward_return_120d")]:
        v = ret_at(days)
        if v is not None:
            result[key] = v

    for days, key in [(20, "max_drawdown_20d"), (60, "max_drawdown_60d")]:
        v = max_drawdown(days)
        if v is not None:
            result[key] = v

    for days, key in [(20, "max_runup_20d"), (60, "max_runup_60d")]:
        v = max_runup(days)
        if v is not None:
            result[key] = v

    # Hit-rate labels
    window_20 = [p[1] for p in prices[1:21] if p[1]]
    if window_20:
        returns_20 = [p / ref_price - 1.0 for p in window_20]
        highs = [r for r in returns_20 if r >= 0.10]
        lows = [r for r in returns_20 if r <= -0.10]
        result["hit_10pct_before_down_10pct"] = bool(highs) and (not lows or min(lows) > min(highs))
        highs_20 = [r for r in returns_20 if r >= 0.20]
        result["hit_20pct_before_down_10pct"] = bool(highs_20) and (not lows or min(lows) > min(highs_20))

    return result


def backfill_outcomes(conn: duckdb.DuckDBPyConnection) -> int:
    """Compute forward returns for all pending outcomes that now have enough price data."""
    from outcomes.tracker import update_forward_returns

    rows = conn.execute(
        """
        SELECT candidate_id, ticker, as_of_date
        FROM candidate_outcomes
        WHERE review_status = 'pending' AND forward_return_20d IS NULL
        """
    ).fetchall()

    updated = 0
    for candidate_id, ticker, as_of_date in rows:
        returns = compute_forward_returns(conn, ticker, as_of_date)
        if returns:
            update_forward_returns(conn, candidate_id, returns)
            updated += 1

    logger.info("Backfilled forward returns for %d candidates", updated)
    return updated
