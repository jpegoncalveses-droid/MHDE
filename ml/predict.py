"""Score all universe tickers and write predictions to ml_predictions.

Adaptive threshold logic:
- Show all predictions above 0.60, capped at 20 per horizon
- If fewer than 5 above 0.60, lower to 0.50 (flagged as lower confidence)
- Rank by probability
- Include sector correlation grouping
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime

import duckdb
import joblib
import numpy as np
import pandas as pd

from ml.schema import create_all_tables
from ml.train import FEATURE_COLS

logger = logging.getLogger("mhde.ml.predict")

HIGH_THRESHOLD = 0.60
LOW_THRESHOLD = 0.50
MAX_PREDICTIONS = 20
MIN_HIGH_CONFIDENCE = 5

class StaleFeaturesError(RuntimeError):
    """Raised when ml_features.MAX(trade_date) lags prices_daily.MAX(trade_date).

    KI-149: predict's default auto-pick used to silently fall back to the
    latest ml_features row when current-day prices had landed but feature
    computation had not. The pipeline then advertised "today's predictions"
    while scoring a past date. Default behavior is now to raise instead;
    `allow_stale_features=True` downgrades to a WARNING.
    """

    def __init__(self, features_max, prices_max):
        self.features_max = features_max
        self.prices_max = prices_max
        super().__init__(
            f"ml_features is stale: latest features={features_max}, "
            f"latest prices={prices_max}. Refusing to silently score "
            f"{features_max}. Pass allow_stale_features=True to proceed."
        )


SECTOR_ETF_MAP = {
    "Information Technology": "XLK",
    "Financials": "XLF",
    "Energy": "XLE",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Consumer Staples": "XLP",
    "Utilities": "XLU",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
}


def _load_model(model_path: str) -> dict:
    """Load a saved model bundle."""
    return joblib.load(model_path)


def _get_active_models(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Get all active model configs from ml_model_runs."""
    rows = conn.execute("""
        SELECT model_id, horizon, target_threshold, model_path
        FROM ml_model_runs WHERE is_active = true
    """).fetchall()
    return [{"model_id": r[0], "horizon": r[1], "threshold": r[2], "path": r[3]} for r in rows]


def _load_features_for_date(conn: duckdb.DuckDBPyConnection, prediction_date: date) -> pd.DataFrame:
    """Load features for all universe tickers on the given date."""
    feature_select = ", ".join(f"f.{c}" for c in FEATURE_COLS)
    query = f"""
        SELECT f.ticker, {feature_select}, c.sector, c.market_cap
        FROM ml_features f
        JOIN companies c ON f.ticker = c.ticker
        WHERE f.trade_date = '{prediction_date.isoformat()}'
    """
    return conn.execute(query).fetchdf()


def _bucket_market_cap(mc):
    if pd.isna(mc) or mc is None:
        return "unknown"
    if mc > 200e9:
        return "mega"
    if mc > 50e9:
        return "large"
    if mc > 10e9:
        return "mid"
    return "small"


def score_universe(
    conn: duckdb.DuckDBPyConnection,
    prediction_date: date | None = None,
    allow_stale_features: bool = False,
) -> dict:
    """Score all universe tickers for a given date.

    When prediction_date is None, the latest ml_features.trade_date is auto-
    picked and compared against the latest prices_daily.trade_date — a
    divergence (features behind prices by ≥1 day) raises StaleFeaturesError
    unless allow_stale_features=True (KI-149).

    Returns dict with predictions, regime info, and correlation analysis.
    """
    create_all_tables(conn)

    if prediction_date is None:
        row = conn.execute("SELECT MAX(trade_date) FROM ml_features").fetchone()
        prediction_date = row[0]
        if prediction_date is None:
            logger.error("No features available")
            return {"status": "error", "message": "No features available"}

        prices_row = conn.execute(
            "SELECT MAX(trade_date) FROM prices_daily"
        ).fetchone()
        prices_max = prices_row[0] if prices_row else None
        if prices_max is not None and prediction_date < prices_max:
            if allow_stale_features:
                logger.warning(
                    "Features stale vs prices: ml_features=%s, prices_daily=%s. "
                    "Proceeding because allow_stale_features=True (KI-149).",
                    prediction_date, prices_max,
                )
            else:
                logger.warning(
                    "Features stale vs prices: ml_features=%s, prices_daily=%s. "
                    "Refusing to score silently (KI-149).",
                    prediction_date, prices_max,
                )
                raise StaleFeaturesError(prediction_date, prices_max)

    logger.info("Scoring universe for %s", prediction_date)

    models = _get_active_models(conn)
    if not models:
        logger.error("No active models found")
        return {"status": "error", "message": "No active models"}

    features_df = _load_features_for_date(conn, prediction_date)
    if features_df.empty:
        logger.error("No features for date %s", prediction_date)
        return {"status": "error", "message": f"No features for {prediction_date}"}

    logger.info("  Loaded features for %d tickers", len(features_df))

    all_predictions = []

    for model_cfg in models:
        bundle = _load_model(model_cfg["path"])
        model = bundle["model"]
        platt = bundle["platt"]
        medians = bundle["medians"]

        X = features_df[FEATURE_COLS].copy()
        X = X.fillna(pd.Series(medians))

        raw_probs = model.predict_proba(X)[:, 1].reshape(-1, 1)
        calibrated_probs = platt.predict_proba(raw_probs)[:, 1]

        # Adaptive threshold
        above_high = calibrated_probs >= HIGH_THRESHOLD
        n_above_high = above_high.sum()

        if n_above_high >= MIN_HIGH_CONFIDENCE:
            mask = above_high
            effective_threshold = HIGH_THRESHOLD
        else:
            mask = calibrated_probs >= LOW_THRESHOLD
            effective_threshold = LOW_THRESHOLD

        # Get indices, sort by probability, cap at MAX_PREDICTIONS
        candidate_idx = np.where(mask)[0]
        sorted_idx = candidate_idx[np.argsort(calibrated_probs[candidate_idx])[::-1]]
        final_idx = sorted_idx[:MAX_PREDICTIONS]

        horizon = model_cfg["horizon"]
        model_id = model_cfg["model_id"]

        for idx in final_idx:
            ticker = features_df.iloc[idx]["ticker"]
            sector = features_df.iloc[idx]["sector"]
            market_cap = features_df.iloc[idx]["market_cap"]
            prob = float(calibrated_probs[idx])
            confidence = "high" if prob >= HIGH_THRESHOLD else "lower"

            all_predictions.append({
                "ticker": ticker,
                "prediction_date": prediction_date,
                "model_id": model_id,
                "horizon": horizon,
                "predicted_probability": prob,
                "prediction_threshold": model_cfg["threshold"],
                "sector": sector,
                "market_cap_bucket": _bucket_market_cap(market_cap),
                "confidence": confidence,
            })

        logger.info("  %s: %d predictions (threshold=%.2f, %d above 0.60)",
                    horizon, len(final_idx), effective_threshold, n_above_high)

    # Write to database
    _write_predictions(conn, all_predictions)

    # Compute correlation/regime analysis
    regime = _compute_regime(all_predictions, calibrated_probs, features_df)

    return {
        "status": "ok",
        "prediction_date": prediction_date,
        "predictions": all_predictions,
        "regime": regime,
    }


def _write_predictions(conn: duckdb.DuckDBPyConnection, predictions: list[dict]):
    """Insert predictions into ml_predictions table."""
    if not predictions:
        return
    rows = []
    for p in predictions:
        rows.append([
            p["ticker"], p["prediction_date"], p["model_id"], p["horizon"],
            p["predicted_probability"], p["prediction_threshold"],
            p["sector"], p["market_cap_bucket"],
            None, None, None, None,  # actual fields filled later
        ])
    conn.executemany("""
        INSERT INTO ml_predictions (
            ticker, prediction_date, model_id, horizon,
            predicted_probability, prediction_threshold,
            sector, market_cap_bucket,
            actual_max_return, actual_max_drawdown, actual_hit, outcome_filled_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (ticker, prediction_date, model_id, horizon) DO UPDATE SET
            predicted_probability = EXCLUDED.predicted_probability
    """, rows)


def _compute_regime(predictions: list[dict], all_probs: np.ndarray, features_df: pd.DataFrame) -> dict:
    """Compute regime indicator and sector concentration."""
    n_above_60 = int((all_probs >= 0.60).sum())
    n_above_50 = int((all_probs >= 0.50).sum())
    total_universe = len(all_probs)

    # Regime classification
    pct_above_60 = n_above_60 / total_universe if total_universe > 0 else 0
    if pct_above_60 > 0.30:
        regime_label = "high_activity"
        regime_description = "Broad opportunity — many stocks showing signal. Higher correlation risk."
    elif pct_above_60 > 0.10:
        regime_label = "normal"
        regime_description = "Normal conditions — selective opportunities."
    else:
        regime_label = "low_activity"
        regime_description = "Few signals — market may be range-bound or signals are concentrated."

    # Sector concentration analysis
    sector_counts = {}
    for p in predictions:
        s = p.get("sector", "Unknown")
        sector_counts[s] = sector_counts.get(s, 0) + 1

    total_preds = len(predictions)
    sector_concentration = []
    for sector, count in sorted(sector_counts.items(), key=lambda x: -x[1]):
        pct = count / total_preds * 100 if total_preds > 0 else 0
        warning = pct > 30
        sector_concentration.append({
            "sector": sector,
            "count": count,
            "pct": pct,
            "correlated_risk": warning,
        })

    return {
        "label": regime_label,
        "description": regime_description,
        "n_above_60": n_above_60,
        "n_above_50": n_above_50,
        "total_universe": total_universe,
        "pct_above_60": pct_above_60 * 100,
        "sector_concentration": sector_concentration,
    }


def fill_outcomes(conn: duckdb.DuckDBPyConnection):
    """Fill actual outcomes for predictions where the horizon has elapsed."""
    logger.info("Filling prediction outcomes...")

    conn.execute("""
        UPDATE ml_predictions p SET
            actual_max_return = sub.max_ret,
            actual_max_drawdown = sub.max_dd,
            actual_hit = sub.max_ret >= p.prediction_threshold,
            outcome_filled_at = CURRENT_TIMESTAMP
        FROM (
            SELECT
                p2.ticker,
                p2.prediction_date,
                p2.model_id,
                p2.horizon,
                (MAX(pr.adjusted_close) / entry.adjusted_close) - 1 AS max_ret,
                (MIN(pr.adjusted_close) / entry.adjusted_close) - 1 AS max_dd
            FROM ml_predictions p2
            JOIN (
                SELECT ticker, trade_date, adjusted_close,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date) AS rn
                FROM prices_daily WHERE adjusted_close > 0
            ) entry ON entry.ticker = p2.ticker AND entry.trade_date = p2.prediction_date
            JOIN (
                SELECT ticker, trade_date, adjusted_close,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date) AS rn
                FROM prices_daily WHERE adjusted_close > 0
            ) pr ON pr.ticker = p2.ticker
                AND pr.rn > entry.rn
                AND pr.rn <= entry.rn + CASE p2.horizon
                    WHEN '5d' THEN 5
                    WHEN '10d' THEN 10
                    WHEN '20d' THEN 20
                    ELSE 20
                END
            WHERE p2.outcome_filled_at IS NULL
            GROUP BY p2.ticker, p2.prediction_date, p2.model_id, p2.horizon,
                     entry.adjusted_close, entry.rn
            HAVING COUNT(pr.rn) = CASE p2.horizon
                    WHEN '5d' THEN 5
                    WHEN '10d' THEN 10
                    WHEN '20d' THEN 20
                    ELSE 20
                END
        ) sub
        WHERE p.ticker = sub.ticker
          AND p.prediction_date = sub.prediction_date
          AND p.model_id = sub.model_id
          AND p.horizon = sub.horizon
    """)

    filled = conn.execute("""
        SELECT COUNT(*) FROM ml_predictions WHERE outcome_filled_at IS NOT NULL
    """).fetchone()[0]
    logger.info("  %d predictions have outcomes filled", filled)


def print_predictions(result: dict):
    """Print predictions to stdout in a readable format."""
    if result["status"] != "ok":
        print(f"ERROR: {result.get('message', 'unknown')}")
        return

    regime = result["regime"]
    predictions = result["predictions"]
    pred_date = result["prediction_date"]

    print(f"\n{'='*90}")
    print(f"ML PREDICTIONS FOR {pred_date}")
    print(f"{'='*90}")

    # Regime banner
    regime_icon = {"high_activity": "!!!", "normal": " . ", "low_activity": " _ "}
    print(f"\n  [{regime_icon.get(regime['label'], '?')}] REGIME: {regime['label'].upper()}")
    print(f"      {regime['description']}")
    print(f"      {regime['n_above_60']} of {regime['total_universe']} tickers above 0.60 "
          f"({regime['pct_above_60']:.1f}%)")

    # Sector concentration warnings
    concentrated = [s for s in regime["sector_concentration"] if s["correlated_risk"]]
    if concentrated:
        print(f"\n  *** CORRELATION WARNING ***")
        for s in concentrated:
            print(f"      {s['sector']}: {s['count']} predictions ({s['pct']:.0f}% of total)")

    # Group predictions by horizon
    by_horizon = {}
    for p in predictions:
        h = p["horizon"]
        if h not in by_horizon:
            by_horizon[h] = []
        by_horizon[h].append(p)

    for horizon in sorted(by_horizon.keys()):
        preds = sorted(by_horizon[horizon], key=lambda x: -x["predicted_probability"])
        print(f"\n  --- {horizon.upper()} HORIZON ({len(preds)} predictions) ---")
        print(f"  {'#':<3} {'Ticker':<7} {'Prob':>5} {'Conf':<6} {'Sector':<28} {'Cap':<6}")
        print(f"  {'-'*65}")
        for i, p in enumerate(preds, 1):
            conf_marker = " " if p["confidence"] == "high" else "*"
            print(f"  {i:<3} {p['ticker']:<7} {p['predicted_probability']:>4.0%}{conf_marker} "
                  f"{p['confidence']:<6} {(p['sector'] or 'Unknown'):<28} {p['market_cap_bucket']:<6}")

    # Sector breakdown table
    print(f"\n  SECTOR BREAKDOWN (all horizons):")
    print(f"  {'Sector':<28} | {'Count':>5} | {'% of Total':>9} | {'Risk'}")
    print(f"  {'-'*60}")
    for s in regime["sector_concentration"]:
        risk = "CORRELATED" if s["correlated_risk"] else ""
        print(f"  {s['sector']:<28} | {s['count']:>5} | {s['pct']:>8.0f}% | {risk}")
