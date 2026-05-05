"""Score all crypto universe symbols and write predictions to crypto_ml_predictions.

Adaptive threshold: show predictions above 0.60, fallback to 0.50 if too few.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import duckdb
import joblib
import numpy as np
import pandas as pd

from crypto.schema import create_all_tables
from crypto.config import FEATURE_COLS

logger = logging.getLogger("mhde.crypto.predict")

HIGH_THRESHOLD = 0.60
LOW_THRESHOLD = 0.50
MAX_PREDICTIONS = 15
MIN_HIGH_CONFIDENCE = 3


def _get_active_models(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute("""
        SELECT model_id, horizon, target_threshold, model_path
        FROM crypto_ml_model_runs WHERE is_active = true
    """).fetchall()
    return [{"model_id": r[0], "horizon": r[1], "threshold": r[2], "path": r[3]} for r in rows]


def _load_features_for_date(conn: duckdb.DuckDBPyConnection, prediction_date: date) -> pd.DataFrame:
    feature_select = ", ".join(f"f.{c}" for c in FEATURE_COLS)
    query = f"""
        SELECT f.symbol, {feature_select}
        FROM crypto_ml_features f
        WHERE f.trade_date = '{prediction_date.isoformat()}'
    """
    return conn.execute(query).fetchdf()


def _bucket_market_cap(mc_log):
    if pd.isna(mc_log) or mc_log is None:
        return "unknown"
    if mc_log > 11:
        return "major"
    if mc_log > 10:
        return "large_alt"
    return "mid_alt"


def score_universe(conn: duckdb.DuckDBPyConnection, prediction_date: date | None = None) -> dict:
    create_all_tables(conn)

    models = _get_active_models(conn)
    if not models:
        logger.error("No active crypto models. Run `python main.py crypto train` first.")
        return {"predictions": [], "regime": "unknown", "date": None}

    if prediction_date is None:
        prediction_date = conn.execute(
            "SELECT MAX(trade_date) FROM crypto_ml_features"
        ).fetchone()[0]

    logger.info("Scoring crypto universe for %s with %d models", prediction_date, len(models))

    features_df = _load_features_for_date(conn, prediction_date)
    if features_df.empty:
        logger.error("No features for %s", prediction_date)
        return {"predictions": [], "regime": "unknown", "date": prediction_date}

    all_predictions = []
    for model_cfg in models:
        bundle = joblib.load(model_cfg["path"])
        model = bundle["model"]
        platt = bundle["platt"]
        medians = bundle["medians"]

        X = features_df[FEATURE_COLS].copy()
        for col in FEATURE_COLS:
            X[col] = X[col].fillna(medians.get(col, 0))

        raw_probs = model.predict_proba(X)[:, 1].reshape(-1, 1)
        cal_probs = platt.predict_proba(raw_probs)[:, 1]

        for idx, (_, row) in enumerate(features_df.iterrows()):
            prob = float(cal_probs[idx])
            if prob < LOW_THRESHOLD:
                continue
            all_predictions.append({
                "symbol": row["symbol"],
                "prediction_date": prediction_date,
                "model_id": model_cfg["model_id"],
                "horizon": model_cfg["horizon"],
                "predicted_probability": prob,
                "prediction_threshold": model_cfg["threshold"],
                "market_cap_bucket": _bucket_market_cap(row.get("market_cap_log")),
            })

    # Adaptive thresholding per horizon
    final_preds = []
    for model_cfg in models:
        hz = model_cfg["horizon"]
        hz_preds = [p for p in all_predictions if p["horizon"] == hz]
        n_high = sum(1 for p in hz_preds if p["predicted_probability"] >= HIGH_THRESHOLD)

        if n_high >= MIN_HIGH_CONFIDENCE:
            hz_preds = [p for p in hz_preds if p["predicted_probability"] >= HIGH_THRESHOLD]
            for p in hz_preds:
                p["confidence"] = "high"
        else:
            for p in hz_preds:
                p["confidence"] = "high" if p["predicted_probability"] >= HIGH_THRESHOLD else "lower"

        hz_preds.sort(key=lambda p: p["predicted_probability"], reverse=True)
        final_preds.extend(hz_preds[:MAX_PREDICTIONS])

    # Write to DB
    conn.execute("DELETE FROM crypto_ml_predictions WHERE prediction_date = ?", [prediction_date])
    for p in final_preds:
        conn.execute("""
            INSERT INTO crypto_ml_predictions (
                symbol, prediction_date, model_id, horizon,
                predicted_probability, prediction_threshold, market_cap_bucket
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (symbol, prediction_date, model_id, horizon) DO UPDATE SET
                predicted_probability = excluded.predicted_probability
        """, [p["symbol"], p["prediction_date"], p["model_id"], p["horizon"],
              p["predicted_probability"], p["prediction_threshold"], p["market_cap_bucket"]])

    # Regime analysis
    total_universe = len(features_df)
    n_above_60 = sum(1 for p in final_preds if p["predicted_probability"] >= 0.60)
    pct = n_above_60 / total_universe * 100 if total_universe > 0 else 0

    if pct > 30:
        regime = "high_activity"
    elif pct > 10:
        regime = "normal"
    else:
        regime = "low_activity"

    result = {
        "predictions": final_preds,
        "date": prediction_date,
        "regime": regime,
        "n_predictions": len(final_preds),
        "n_universe": total_universe,
    }

    logger.info("Predictions: %d (regime: %s)", len(final_preds), regime)
    return result


def fill_outcomes(conn: duckdb.DuckDBPyConnection):
    """Fill actual returns for predictions where the horizon has elapsed."""
    conn.execute("""
        UPDATE crypto_ml_predictions p SET
            actual_max_return = sub.max_ret,
            actual_max_drawdown = sub.min_ret,
            actual_hit = sub.max_ret >= p.prediction_threshold,
            outcome_filled_at = CURRENT_TIMESTAMP
        FROM (
            SELECT
                pred.symbol,
                pred.prediction_date,
                pred.model_id,
                pred.horizon,
                (MAX(pr.close) / entry.close) - 1 AS max_ret,
                (MIN(pr.close) / entry.close) - 1 AS min_ret
            FROM crypto_ml_predictions pred
            JOIN crypto_prices_daily entry ON pred.symbol = entry.symbol AND pred.prediction_date = entry.trade_date
            JOIN crypto_prices_daily pr ON pred.symbol = pr.symbol
                AND pr.trade_date > pred.prediction_date
                AND pr.trade_date <= pred.prediction_date + CASE pred.horizon
                    WHEN '1d' THEN INTERVAL '3 days'
                    WHEN '3d' THEN INTERVAL '5 days'
                    WHEN '5d' THEN INTERVAL '10 days'
                    WHEN '10d' THEN INTERVAL '16 days'
                    ELSE INTERVAL '20 days'
                END
            WHERE pred.outcome_filled_at IS NULL
              AND pred.prediction_date + CASE pred.horizon
                    WHEN '1d' THEN INTERVAL '3 days'
                    WHEN '3d' THEN INTERVAL '5 days'
                    WHEN '5d' THEN INTERVAL '10 days'
                    WHEN '10d' THEN INTERVAL '16 days'
                    ELSE INTERVAL '20 days'
                END <= CURRENT_DATE
            GROUP BY pred.symbol, pred.prediction_date, pred.model_id, pred.horizon, entry.close
        ) sub
        WHERE p.symbol = sub.symbol
          AND p.prediction_date = sub.prediction_date
          AND p.model_id = sub.model_id
          AND p.horizon = sub.horizon
    """)

    filled = conn.execute(
        "SELECT COUNT(*) FROM crypto_ml_predictions WHERE outcome_filled_at IS NOT NULL"
    ).fetchone()[0]
    logger.info("Outcomes filled: %d total", filled)


def print_predictions(result: dict):
    preds = result.get("predictions", [])
    if not preds:
        print("No predictions above threshold.")
        return

    print(f"\n{'='*70}")
    print(f"CRYPTO PREDICTIONS — {result['date']}")
    print(f"Regime: {result['regime'].upper()} | {result['n_predictions']} predictions from {result['n_universe']} coins")
    print(f"{'='*70}")
    print(f"  {'Symbol':<12} {'Hz':>3} {'Prob':>6} {'Conf':>6} {'Bucket':>10}")
    print(f"  {'-'*45}")

    for p in sorted(preds, key=lambda x: (-x["predicted_probability"])):
        print(f"  {p['symbol']:<12} {p['horizon']:>3} {p['predicted_probability']:>5.1%} "
              f"{p.get('confidence', '?'):>6} {p['market_cap_bucket']:>10}")
