from __future__ import annotations

import glob
import os
from datetime import date, timedelta

import duckdb


MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models", "saved")


def check_trained_models() -> dict:
    models = glob.glob(os.path.join(MODELS_DIR, "*.joblib"))
    if not models:
        return {"check_name": "ml_trained_models", "status": "fail", "severity": "critical",
                "message": "No trained models found in models/saved/"}
    horizons = {os.path.basename(m).split("_")[0] for m in models}
    return {"check_name": "ml_trained_models", "status": "pass", "severity": "low",
            "message": f"{len(models)} model(s) for horizons: {', '.join(sorted(horizons))}"}


def check_last_prediction(conn: duckdb.DuckDBPyConnection) -> dict:
    row = conn.execute("""
        SELECT MAX(prediction_date) FROM ml_predictions
    """).fetchone()
    if row is None or row[0] is None:
        return {"check_name": "ml_last_prediction", "status": "fail", "severity": "critical",
                "message": "No predictions found in ml_predictions"}
    last = row[0]
    if isinstance(last, str):
        last = date.fromisoformat(last)
    elif hasattr(last, "date"):
        last = last.date()
    age_days = (date.today() - last).days
    if age_days > 3:
        return {"check_name": "ml_last_prediction", "status": "warn", "severity": "medium",
                "message": f"Last prediction is {age_days} days old ({last})"}
    return {"check_name": "ml_last_prediction", "status": "pass", "severity": "low",
            "message": f"Last prediction: {last} ({age_days}d ago)"}


def check_rolling_precision(conn: duckdb.DuckDBPyConnection) -> dict:
    row = conn.execute("""
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN actual_hit THEN 1 ELSE 0 END) AS hits
        FROM ml_predictions
        WHERE outcome_filled_at IS NOT NULL
    """).fetchone()
    n, hits = row[0], row[1]
    if n == 0:
        return {"check_name": "ml_rolling_precision", "status": "skip", "severity": "low",
                "message": "No outcomes filled yet"}
    precision = hits / n
    status = "pass" if precision >= 0.50 else "warn"
    return {"check_name": "ml_rolling_precision", "status": status, "severity": "medium",
            "message": f"Precision: {precision:.0%} ({hits}/{n} hits)"}


def check_ml_tables_freshness(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    results = []
    for table in ("ml_features", "ml_labels"):
        row = conn.execute(f"SELECT MAX(trade_date) FROM {table}").fetchone()
        if row is None or row[0] is None:
            results.append({"check_name": f"{table}_freshness", "status": "fail",
                            "severity": "critical", "message": f"{table} is empty"})
            continue
        last = row[0]
        if isinstance(last, str):
            last = date.fromisoformat(last)
        elif hasattr(last, "date"):
            last = last.date()
        age_days = (date.today() - last).days
        if age_days > 7:
            status, sev = "warn", "medium"
        else:
            status, sev = "pass", "low"
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        results.append({"check_name": f"{table}_freshness", "status": status, "severity": sev,
                        "message": f"Latest: {last} ({age_days}d ago), {count:,} rows"})
    return results
