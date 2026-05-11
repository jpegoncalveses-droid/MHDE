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
        INSERT INTO crypto_ml_labels (
            symbol, trade_date, close_price,
            fwd_return_1d, fwd_return_3d, fwd_return_5d, fwd_return_10d,
            fwd_max_return_1d, fwd_max_return_3d, fwd_max_return_5d, fwd_max_return_10d,
            fwd_max_drawdown_1d, fwd_max_drawdown_3d, fwd_max_drawdown_5d, fwd_max_drawdown_10d,
            label_1d_5pct, label_1d_3pct, label_3d_5pct, label_3d_10pct,
            label_5d_10pct, label_5d_15pct, label_10d_10pct, label_10d_15pct, label_10d_20pct
        )
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

    _compute_knockout_labels(conn)

    logger.info("Total labels computed: %d", total)
    return total


def _compute_knockout_labels(conn: duckdb.DuckDBPyConnection) -> int:
    """Second pass: forward-walk ``crypto_prices_daily`` bar by bar per symbol
    to populate the knockout (triple-barrier) columns. The close-based INSERT
    above can't express first-touch ordering. Updates only rows that already
    exist in ``crypto_ml_labels`` (i.e. have >= 1 forward bar). See
    ``crypto/ml/knockout_label.py`` / ``crypto/ml/KNOCKOUT_LABEL_SPEC.md``."""
    import pandas as pd  # local import — labels.py is otherwise pandas-free

    from crypto.config import KNOCKOUT_SL, KNOCKOUT_TP
    from crypto.ml.knockout_label import OUTCOME_TP, knockout_classify

    symbols = [r[0] for r in conn.execute(
        "SELECT symbol FROM crypto_universe WHERE is_active = true ORDER BY rank_by_volume"
    ).fetchall()]
    if not symbols:
        return 0
    placeholders = ",".join(f"'{s}'" for s in symbols)
    px = conn.execute(
        f"SELECT symbol, trade_date, high, low, close FROM crypto_prices_daily "
        f"WHERE symbol IN ({placeholders}) AND close > 0 ORDER BY symbol, trade_date"
    ).df()
    if px.empty:
        return 0

    horizons = (5, 10)
    out_rows: list[tuple] = []
    for sym, g in px.groupby("symbol", sort=False):
        g = g.sort_values("trade_date").reset_index(drop=True)
        highs = g["high"].tolist()
        lows = g["low"].tolist()
        closes = g["close"].tolist()
        dates = g["trade_date"].tolist()
        for i in range(len(g) - 1):  # need >= 1 forward bar (matches close_1d IS NOT NULL)
            c = closes[i]
            fh, fl = highs[i + 1:], lows[i + 1:]
            res = {}
            for n in horizons:
                outcome, day = knockout_classify(fh, fl, c, KNOCKOUT_TP, KNOCKOUT_SL, n, sl_first=True)
                res[n] = (outcome == OUTCOME_TP, outcome, day)
            out_rows.append((sym, dates[i],
                             res[5][0], res[5][1], res[5][2],
                             res[10][0], res[10][1], res[10][2]))
    if not out_rows:
        return 0

    upd = pd.DataFrame(out_rows, columns=[
        "symbol", "trade_date", "l5", "o5", "d5", "l10", "o10", "d10"])
    conn.register("_ko_updates", upd)
    try:
        conn.execute("""
            UPDATE crypto_ml_labels AS l
            SET label_5d_knockout       = u.l5,
                knockout_outcome_5d     = u.o5,
                knockout_resolve_day_5d = u.d5,
                label_10d_knockout      = u.l10,
                knockout_outcome_10d    = u.o10,
                knockout_resolve_day_10d = u.d10
            FROM _ko_updates AS u
            WHERE l.symbol = u.symbol AND l.trade_date = u.trade_date
        """)
    finally:
        conn.unregister("_ko_updates")
    logger.info("  Knockout labels: updated %d rows", len(out_rows))
    return len(out_rows)
