"""Score current/specified hourly bar with all active FX models."""
from __future__ import annotations

import logging
from datetime import datetime

import duckdb
import joblib
import numpy as np
import pandas as pd

from fx.config import FEATURE_COLS, PIP_SIZE
from fx.schema import create_all_tables

logger = logging.getLogger("mhde.fx.predict")


def _get_active_models(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute("""
        SELECT model_id, direction, horizon, target_pips, model_path
        FROM fx_ml_model_runs WHERE is_active = true
    """).fetchall()
    return [{"model_id": r[0], "direction": r[1], "horizon": r[2],
             "target_pips": r[3], "path": r[4]} for r in rows]


def score_bar(conn: duckdb.DuckDBPyConnection, bar_datetime: datetime | None = None) -> dict:
    create_all_tables(conn)
    models = _get_active_models(conn)
    if not models:
        logger.error("No active FX models.")
        return {"predictions": {}, "datetime": None, "price": None}

    if bar_datetime is None:
        bar_datetime = conn.execute(
            "SELECT MAX(datetime_utc) FROM fx_ml_features"
        ).fetchone()[0]

    feature_select = ", ".join(FEATURE_COLS)
    row = conn.execute(f"""
        SELECT {feature_select} FROM fx_ml_features WHERE datetime_utc = ?
    """, [bar_datetime]).fetchdf()

    if row.empty:
        logger.error("No features for %s", bar_datetime)
        return {"predictions": {}, "datetime": bar_datetime, "price": None}

    predictions = {}
    for model_cfg in models:
        bundle = joblib.load(model_cfg["path"])
        model = bundle["model"]
        platt = bundle["platt"]
        medians = bundle["medians"]

        X = row[FEATURE_COLS].copy()
        for col in FEATURE_COLS:
            X[col] = X[col].fillna(medians.get(col, 0))

        raw_prob = model.predict_proba(X)[:, 1].reshape(-1, 1)
        cal_prob = float(platt.predict_proba(raw_prob)[:, 1][0])

        key = f"{model_cfg['direction']}_{model_cfg['horizon']}"
        predictions[key] = {
            "model_id": model_cfg["model_id"],
            "direction": model_cfg["direction"],
            "horizon": model_cfg["horizon"],
            "probability": cal_prob,
            "target_pips": model_cfg["target_pips"],
        }

        conn.execute("""
            INSERT INTO fx_ml_predictions (datetime_utc, model_id, direction, horizon,
                                           predicted_probability, prediction_threshold)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (datetime_utc, model_id, direction, horizon) DO UPDATE SET
                predicted_probability = excluded.predicted_probability
        """, [bar_datetime, model_cfg["model_id"], model_cfg["direction"],
              model_cfg["horizon"], cal_prob, model_cfg["target_pips"]])

    price_row = conn.execute(
        "SELECT gbpeur_close FROM fx_prices_hourly WHERE datetime_utc = ?", [bar_datetime]
    ).fetchone()
    price = float(price_row[0]) if price_row else None

    return {"predictions": predictions, "datetime": bar_datetime, "price": price}


def fill_outcomes(conn: duckdb.DuckDBPyConnection):
    """Fill actual pip moves for predictions where horizon has elapsed."""
    conn.execute(f"""
        UPDATE fx_ml_predictions p SET
            actual_max_pips = sub.max_pips,
            actual_hit = sub.max_pips >= p.prediction_threshold,
            outcome_filled_at = CURRENT_TIMESTAMP
        FROM (
            SELECT
                pred.datetime_utc,
                pred.model_id,
                pred.direction,
                pred.horizon,
                CASE pred.direction
                    WHEN 'up' THEN (MAX(pr.gbpeur_high) - entry.gbpeur_close) / {PIP_SIZE}
                    WHEN 'down' THEN (entry.gbpeur_close - MIN(pr.gbpeur_low)) / {PIP_SIZE}
                END AS max_pips
            FROM fx_ml_predictions pred
            JOIN fx_prices_hourly entry ON pred.datetime_utc = entry.datetime_utc
            JOIN fx_prices_hourly pr ON pr.datetime_utc > pred.datetime_utc
                AND pr.datetime_utc <= pred.datetime_utc + CASE pred.horizon
                    WHEN '24h' THEN INTERVAL '24 hours'
                    WHEN '48h' THEN INTERVAL '48 hours'
                END
            WHERE pred.outcome_filled_at IS NULL
              AND pred.datetime_utc + CASE pred.horizon
                    WHEN '24h' THEN INTERVAL '24 hours'
                    WHEN '48h' THEN INTERVAL '48 hours'
                END <= (SELECT MAX(datetime_utc) FROM fx_prices_hourly)
            GROUP BY pred.datetime_utc, pred.model_id, pred.direction, pred.horizon, entry.gbpeur_close
        ) sub
        WHERE p.datetime_utc = sub.datetime_utc
          AND p.model_id = sub.model_id
          AND p.direction = sub.direction
          AND p.horizon = sub.horizon
    """)

    filled = conn.execute(
        "SELECT COUNT(*) FROM fx_ml_predictions WHERE outcome_filled_at IS NOT NULL"
    ).fetchone()[0]
    logger.info("Outcomes filled: %d total", filled)
