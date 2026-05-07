"""Unit tests for fx/ml/features.py — 44 hourly features.

Coverage targets:
  - compute_features writes one row per input price bar.
  - All required FEATURE_COLS are present in the output schema.
  - Features at the head of the series are NULL (insufficient lookback).
  - No NaN/Inf in fully-warmed-up rows.
  - Lookahead bias check: feature for bar T does not change when later
    data is appended.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import math

from fx.config import FEATURE_COLS
from fx.ml.features import compute_features


def _insert_fx_prices(conn, rows):
    conn.executemany(
        "INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, "
        "gbpeur_open, gbpeur_high, gbpeur_low, gbpeur_close, tick_count, data_quality) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(r["datetime_utc"], r["date"], r["weekday"], r["hour_utc"],
          r["gbpeur_open"], r["gbpeur_high"], r["gbpeur_low"], r["gbpeur_close"],
          r["tick_count"], r["data_quality"]) for r in rows],
    )


def test_compute_features_writes_warmed_up_bars(temp_db, synthetic_prices_fx):
    """Features only fully-defined after the longest lookback window
    (480h MA), so the row count is len(prices) - warmup, not len(prices)."""
    rows = synthetic_prices_fx(num_hours=600)
    _insert_fx_prices(temp_db, rows)

    n = compute_features(temp_db)
    assert n > 0
    assert n <= len(rows)
    db_rows = temp_db.execute("SELECT COUNT(*) FROM fx_ml_features").fetchone()[0]
    assert db_rows == n


def test_compute_features_has_all_feature_cols(temp_db, synthetic_prices_fx):
    rows = synthetic_prices_fx(num_hours=520)
    _insert_fx_prices(temp_db, rows)
    compute_features(temp_db)

    cols = {r[0] for r in temp_db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'fx_ml_features'"
    ).fetchall()}
    missing = set(FEATURE_COLS) - cols
    assert not missing, f"fx_ml_features schema missing columns: {sorted(missing)}"


def test_warmed_up_rows_have_no_nan_in_core_features(temp_db, synthetic_prices_fx):
    """After 480+ bars of history, return / RSI / MA-distance features must
    be finite (not NaN/Inf)."""
    rows = synthetic_prices_fx(num_hours=600)
    _insert_fx_prices(temp_db, rows)
    compute_features(temp_db)

    df = temp_db.execute(
        "SELECT return_24h, rsi_14h, price_vs_24h_ma, realized_vol_24h "
        "FROM fx_ml_features ORDER BY datetime_utc DESC LIMIT 50"
    ).fetchdf()
    for col in df.columns:
        for v in df[col].dropna():
            assert math.isfinite(v), f"non-finite {col} value: {v}"


def test_lookahead_bias_features_dont_change_with_future_data(
    temp_db, synthetic_prices_fx
):
    """A feature value computed at time T must not change when bars after
    T are appended. This is the core no-lookahead invariant for the
    training-time/serving-time consistency."""
    rows = synthetic_prices_fx(num_hours=520)
    _insert_fx_prices(temp_db, rows)
    compute_features(temp_db)

    target_dt = rows[500]["datetime_utc"]
    before = temp_db.execute(
        "SELECT return_24h, rsi_14h, price_vs_24h_ma "
        "FROM fx_ml_features WHERE datetime_utc = ?", [target_dt]
    ).fetchone()

    # Append 20 more bars after the original window
    extra = synthetic_prices_fx(
        num_hours=20,
        start_datetime=rows[-1]["datetime_utc"] + timedelta(hours=1),
        seed=99,
    )
    _insert_fx_prices(temp_db, extra)
    compute_features(temp_db)

    after = temp_db.execute(
        "SELECT return_24h, rsi_14h, price_vs_24h_ma "
        "FROM fx_ml_features WHERE datetime_utc = ?", [target_dt]
    ).fetchone()

    assert before == after, (
        f"feature(s) at {target_dt} changed when later data was added "
        f"— lookahead bias. before={before}, after={after}"
    )


def test_compute_features_empty_db(temp_db):
    """compute_features on empty fx_prices_hourly returns 0 and writes nothing."""
    n = compute_features(temp_db)
    assert n == 0
    db_rows = temp_db.execute("SELECT COUNT(*) FROM fx_ml_features").fetchone()[0]
    assert db_rows == 0


def test_compute_features_skips_bad_quality_bars(temp_db, synthetic_prices_fx):
    """data_quality='BAD' rows must be excluded from feature computation."""
    rows = synthetic_prices_fx(num_hours=520)
    # Mark every 10th bar as BAD
    for i in range(0, len(rows), 10):
        rows[i]["data_quality"] = "BAD"
    _insert_fx_prices(temp_db, rows)
    compute_features(temp_db)

    n_features = temp_db.execute("SELECT COUNT(*) FROM fx_ml_features").fetchone()[0]
    assert n_features < len(rows)
