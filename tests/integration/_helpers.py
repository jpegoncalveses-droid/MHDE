"""Helpers shared across integration tests.

Keep these focused on integration-test plumbing (model factories,
DB seeding, joblib bundle creation). Anything generic enough to share
with unit tests goes in `tests/conftest.py`.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import xgboost as xgb
from sklearn.linear_model import LogisticRegression


def train_tiny_model(
    feature_cols: list[str],
    out_path: Path,
    seed: int = 42,
    n_train: int = 200,
    positive_rate: float = 0.85,
) -> Path:
    """Train a small XGBoost on synthetic feature/label data and save
    a bundle compatible with `{ml,crypto/ml,fx/ml}/predict.py`.

    The training labels are biased positive (`positive_rate=0.85`) so the
    model produces probabilities above 0.50 — predict.py filters anything
    below LOW_THRESHOLD=0.50 out of the result, and integration tests
    need at least some predictions to land in the table.

    The model is intentionally fit on noise (random feature values);
    integration tests assert structural completeness of the pipeline
    (predictions written, outcomes filled, schema parity), not model
    quality.

    Returns the path the bundle was saved to.
    """
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n_train, len(feature_cols)))
    y = (rng.random(n_train) < positive_rate).astype(int)

    model = xgb.XGBClassifier(
        n_estimators=10, max_depth=3, learning_rate=0.1,
        random_state=seed, eval_metric="logloss",
    )
    model.fit(X, y)

    raw_probs = model.predict_proba(X)[:, 1].reshape(-1, 1)
    platt = LogisticRegression(C=1e10, solver="lbfgs", max_iter=1000)
    platt.fit(raw_probs, y)

    bundle = {
        "model": model,
        "platt": platt,
        "medians": {col: 0.0 for col in feature_cols},
        "feature_cols": feature_cols,
        "threshold": 0.5,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, out_path)
    return out_path


def register_active_equity_model(
    conn: Any,
    model_path: Path,
    horizon: str = "20d",
    label_col: str = "label_20d_10pct",
    threshold: float = 0.10,
) -> str:
    """Insert a row in ml_model_runs marking this model active."""
    model_id = f"test_{horizon}_{label_col}"
    conn.execute(
        "INSERT INTO ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) VALUES (?, ?, ?, ?, ?)",
        [model_id, horizon, threshold, str(model_path), True],
    )
    return model_id


def register_active_crypto_model(
    conn: Any,
    model_path: Path,
    horizon: str = "5d",
    threshold: float = 0.10,
) -> str:
    model_id = f"test_crypto_{horizon}"
    conn.execute(
        "INSERT INTO crypto_ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) VALUES (?, ?, ?, ?, ?)",
        [model_id, horizon, threshold, str(model_path), True],
    )
    return model_id


def register_active_fx_model(
    conn: Any,
    model_path: Path,
    direction: str = "up",
    horizon: str = "24h",
    target_pips: float = 20,
) -> str:
    model_id = f"test_fx_{direction}_{horizon}"
    conn.execute(
        "INSERT INTO fx_ml_model_runs (model_id, direction, horizon, target_pips, "
        "model_path, is_active) VALUES (?, ?, ?, ?, ?, ?)",
        [model_id, direction, horizon, target_pips, str(model_path), True],
    )
    return model_id


def seed_active_company(
    conn: Any,
    ticker: str,
    sector: str = "Information Technology",
    market_cap: float = 100e9,
) -> None:
    conn.execute(
        "INSERT INTO companies (ticker, company_name, sector, is_active, is_etf, "
        "market_cap) VALUES (?, ?, ?, ?, ?, ?)",
        [ticker, f"{ticker} Inc", sector, True, False, market_cap],
    )


def seed_crypto_universe(conn: Any, symbols: list[str]) -> None:
    for i, sym in enumerate(symbols):
        conn.execute(
            "INSERT INTO crypto_universe (symbol, base_asset, avg_daily_volume_30d, "
            "rank_by_volume, is_active) VALUES (?, ?, ?, ?, ?)",
            [sym, sym.replace("USDT", ""), 1e9 - i * 1e6, i + 1, True],
        )


def insert_equity_prices(conn: Any, rows: list[dict]) -> None:
    conn.executemany(
        "INSERT INTO prices_daily (id, ticker, trade_date, open, high, low, close, "
        "volume, adjusted_close, source, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(r["id"], r["ticker"], r["trade_date"], r["open"], r["high"], r["low"],
          r["close"], r["volume"], r["adjusted_close"], r["source"], r["run_id"])
         for r in rows],
    )


def insert_crypto_prices(conn: Any, rows: list[dict]) -> None:
    conn.executemany(
        "INSERT INTO crypto_prices_daily (symbol, trade_date, open, high, low, close, "
        "volume, trades, taker_buy_volume, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(r["symbol"], r["trade_date"], r["open"], r["high"], r["low"], r["close"],
          r["volume"], r["trades"], r["taker_buy_volume"], r["source"]) for r in rows],
    )


def insert_fx_prices(conn: Any, rows: list[dict]) -> None:
    conn.executemany(
        "INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, "
        "gbpeur_open, gbpeur_high, gbpeur_low, gbpeur_close, tick_count, data_quality) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(r["datetime_utc"], r["date"], r["weekday"], r["hour_utc"],
          r["gbpeur_open"], r["gbpeur_high"], r["gbpeur_low"], r["gbpeur_close"],
          r["tick_count"], r["data_quality"]) for r in rows],
    )
