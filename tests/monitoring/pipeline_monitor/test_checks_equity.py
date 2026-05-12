"""Unit tests for monitoring.pipeline_monitor.checks.equity."""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from monitoring.pipeline_monitor.core import Status
from monitoring.pipeline_monitor.checks import equity as E


# 2026-05-12 is a Tuesday → expected_equity_prediction_date = Mon 2026-05-11.
NOW = datetime(2026, 5, 12, 1, 0, 0, tzinfo=timezone.utc)
EXPECTED = NOW.date() - timedelta(days=1)  # Monday 2026-05-11


def _seed_prices(conn, trade_date, tickers=("AAPL", "MSFT", "NVDA")):
    for t in tickers:
        conn.execute(
            "INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES (?,?,?,?)",
            [f"{t}-{trade_date}", t, trade_date, 100.0],
        )


def _seed_features(conn, trade_date, tickers=("AAPL", "MSFT", "NVDA")):
    for t in tickers:
        conn.execute("INSERT INTO ml_features (ticker, trade_date, return_5d) VALUES (?,?,?)", [t, trade_date, 0.01])


def _seed_active_model(conn, model_id="eq1"):
    conn.execute(
        "INSERT INTO ml_model_runs (model_id, horizon, target_threshold, is_active) VALUES (?,?,?,?)",
        [model_id, "10d", 0.05, True],
    )


def _seed_prediction(conn, ticker, prediction_date, model_id="eq1", horizon="10d"):
    conn.execute(
        "INSERT INTO ml_predictions (ticker, prediction_date, model_id, horizon, predicted_probability, prediction_threshold) "
        "VALUES (?,?,?,?,?,?)",
        [ticker, prediction_date, model_id, horizon, 0.7, 0.05],
    )


@pytest.fixture
def seeded_db(temp_db):
    _seed_prices(temp_db, EXPECTED)
    _seed_features(temp_db, EXPECTED)
    _seed_active_model(temp_db)
    for t in ("AAPL", "MSFT", "NVDA"):
        _seed_prediction(temp_db, t, EXPECTED)
    return temp_db


# ── 1. ingestion ──────────────────────────────────────────────────────
def test_ingestion_green(seeded_db):
    r = E.check_data_ingestion(seeded_db, NOW)
    assert r.status is Status.GREEN and "2026-05-11" in r.detail


def test_ingestion_green_when_today(temp_db):
    _seed_prices(temp_db, NOW.date())
    assert E.check_data_ingestion(temp_db, NOW).status is Status.GREEN


def test_ingestion_red_when_empty(temp_db):
    r = E.check_data_ingestion(temp_db, NOW)
    assert r.status is Status.RED and "empty" in r.detail


def test_ingestion_red_when_stale(temp_db):
    _seed_prices(temp_db, NOW.date() - timedelta(days=5))
    r = E.check_data_ingestion(temp_db, NOW)
    assert r.status is Status.RED and "expected >=" in r.detail


# ── 2. features ───────────────────────────────────────────────────────
def test_features_green(seeded_db):
    assert E.check_feature_pipeline(seeded_db, NOW).status is Status.GREEN


def test_features_red_when_empty(temp_db):
    assert E.check_feature_pipeline(temp_db, NOW).status is Status.RED


def test_features_red_when_stale(temp_db):
    _seed_features(temp_db, NOW.date() - timedelta(days=6))
    assert E.check_feature_pipeline(temp_db, NOW).status is Status.RED


# ── 3. predictions ────────────────────────────────────────────────────
def test_predictions_green(seeded_db):
    r = E.check_model_predictions(seeded_db, NOW)
    assert r.status is Status.GREEN and "3 predictions" in r.detail


def test_predictions_red_when_no_active_model(temp_db):
    _seed_prediction(temp_db, "AAPL", EXPECTED)
    assert E.check_model_predictions(temp_db, NOW).status is Status.RED


def test_predictions_red_when_no_rows(temp_db):
    _seed_active_model(temp_db)
    assert E.check_model_predictions(temp_db, NOW).status is Status.RED


def test_predictions_red_when_stale(temp_db):
    _seed_active_model(temp_db)
    _seed_prediction(temp_db, "AAPL", NOW.date() - timedelta(days=6))
    assert E.check_model_predictions(temp_db, NOW).status is Status.RED


def test_predictions_ignores_inactive(temp_db):
    _seed_active_model(temp_db, "eq1")
    temp_db.execute(
        "INSERT INTO ml_model_runs (model_id, horizon, target_threshold, is_active) VALUES (?,?,?,?)",
        ["bt", "10d", 0.05, False],
    )
    _seed_prediction(temp_db, "AAPL", EXPECTED, model_id="eq1")
    _seed_prediction(temp_db, "ZZZ", NOW.date() - timedelta(days=99), model_id="bt")
    r = E.check_model_predictions(temp_db, NOW)
    assert r.status is Status.GREEN and "2026-05-11" in r.detail


# ── 4. dashboard refresh ──────────────────────────────────────────────
def test_dashboard_green(tmp_path):
    f = tmp_path / "prediction_vs_actual_rows.csv"
    f.write_text("a,b\n1,2\n")
    r = E.check_dashboard_refresh(NOW, marker_path=f)
    assert r.status is Status.GREEN and "updated" in r.detail


def test_dashboard_red_when_missing(tmp_path):
    r = E.check_dashboard_refresh(NOW, marker_path=tmp_path / "nope.csv")
    assert r.status is Status.RED and "does not exist" in r.detail


def test_dashboard_red_when_stale(tmp_path):
    f = tmp_path / "prediction_vs_actual_rows.csv"
    f.write_text("x\n")
    old = (NOW - timedelta(days=6)).timestamp()
    os.utime(f, (old, old))
    r = E.check_dashboard_refresh(NOW, marker_path=f)
    assert r.status is Status.RED and "stale" in r.detail


def test_dashboard_green_within_weekend_window(tmp_path):
    # 3 days old (e.g. Friday's run, monitored on Monday) → still green
    f = tmp_path / "prediction_vs_actual_rows.csv"
    f.write_text("x\n")
    old = (NOW - timedelta(days=3)).timestamp()
    os.utime(f, (old, old))
    assert E.check_dashboard_refresh(NOW, marker_path=f).status is Status.GREEN
