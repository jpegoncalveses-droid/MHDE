"""ML Prediction Pipeline.

Orchestrates daily prediction run:
1. Compute features for latest date (if not already done)
2. Score all universe tickers
3. Fill outcomes for past predictions
4. Print results

Separate from daily_radar.py - runs independently.
"""
from __future__ import annotations

import logging
from datetime import date

import duckdb

logger = logging.getLogger("mhde.ml.pipeline")


def run_prediction_pipeline(
    conn: duckdb.DuckDBPyConnection,
    prediction_date: date | None = None,
    skip_features: bool = False,
    skip_outcomes: bool = False,
    allow_stale_features: bool = False,
) -> dict:
    """Run the full prediction pipeline.

    Args:
        conn: DuckDB connection
        prediction_date: Date to predict for (default: latest available)
        skip_features: If True, assume features already computed
        skip_outcomes: If True, skip filling historical outcomes
        allow_stale_features: If True, downgrade the KI-149 cross-check
            (ml_features behind prices_daily) from raise to WARNING.
    """
    from ml.predict import score_universe, fill_outcomes, print_predictions
    from pipelines.freshness import check_equity_freshness

    logger.info("Starting ML prediction pipeline")

    freshness = check_equity_freshness(conn)
    if not freshness.is_fresh:
        logger.warning("DATA STALE — skipping equity prediction. %s", freshness.message)
        return {"skipped": "stale_data", "freshness": freshness}
    logger.info("Freshness OK: %s", freshness.message)

    # Stage 1: Ensure features exist for prediction_date
    if not skip_features and prediction_date is not None:
        _ensure_features(conn, prediction_date)

    # Stage 2: Score universe
    result = score_universe(
        conn, prediction_date, allow_stale_features=allow_stale_features
    )

    # Stage 3: Fill outcomes for past predictions
    if not skip_outcomes:
        fill_outcomes(conn)

    # Stage 4: Print results
    print_predictions(result)

    # Stage 5: Print accuracy monitor
    if not skip_outcomes:
        _print_accuracy_monitor(conn)

    return result


def _ensure_features(conn: duckdb.DuckDBPyConnection, prediction_date: date):
    """Check if features exist for the prediction date, compute if not."""
    count = conn.execute(
        "SELECT COUNT(*) FROM ml_features WHERE trade_date = ?",
        [prediction_date]
    ).fetchone()[0]

    if count > 0:
        logger.info("  Features already exist for %s (%d rows)", prediction_date, count)
        return

    logger.info("  Computing features for %s...", prediction_date)
    from ml.features import compute_features
    compute_features(conn, batch_size=30)


def _print_accuracy_monitor(conn: duckdb.DuckDBPyConnection):
    """Print rolling accuracy stats for filled predictions."""
    stats = conn.execute("""
        SELECT
            horizon,
            COUNT(*) AS n_filled,
            SUM(CASE WHEN actual_hit THEN 1 ELSE 0 END) AS n_hit,
            AVG(actual_max_return) AS avg_max_return,
            AVG(actual_max_drawdown) AS avg_max_drawdown,
            AVG(predicted_probability) AS avg_prob
        FROM ml_predictions
        WHERE outcome_filled_at IS NOT NULL
        GROUP BY horizon
    """).fetchall()

    if not stats:
        return

    print(f"\n  {'='*70}")
    print(f"  HISTORICAL ACCURACY (predictions with outcomes filled)")
    print(f"  {'='*70}")
    print(f"  {'Horizon':<8} | {'N':>5} | {'Hits':>5} | {'Prec%':>6} | {'Avg MaxRet':>10} | {'Avg MaxDD':>9} | {'Avg Prob':>8}")
    print(f"  {'-'*65}")

    for row in stats:
        horizon, n, hits, avg_ret, avg_dd, avg_prob = row
        prec = hits / n * 100 if n > 0 else 0
        print(f"  {horizon:<8} | {n:>5} | {hits:>5} | {prec:>5.1f}% | "
              f"{avg_ret*100 if avg_ret else 0:>+9.2f}% | "
              f"{avg_dd*100 if avg_dd else 0:>+8.2f}% | {avg_prob:>7.1%}")
