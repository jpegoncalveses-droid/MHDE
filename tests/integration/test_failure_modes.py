"""Integration tests for pipeline failure modes.

Each test sets up an explicit broken state and asserts the pipeline
either degrades gracefully (logs and skips) or returns a structured
error — never crashes silently.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import duckdb
import pytest

from pipelines.crypto_prediction_pipeline import run_crypto_prediction_pipeline
from pipelines.fx_prediction_pipeline import run_fx_prediction_pipeline
from pipelines.ml_prediction_pipeline import run_prediction_pipeline


# ──────────────────────────────────────────────────────────────────────
# Stale data — pipeline skips with `DATA STALE` log
# ──────────────────────────────────────────────────────────────────────


def test_equity_pipeline_skips_when_stale(temp_db, caplog):
    """Equity prices_daily latest = 30 trading days back → skip."""
    import logging
    caplog.set_level(logging.WARNING)
    stale = date.today() - timedelta(days=60)
    temp_db.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES (?, ?, ?, ?)",
        ["x", "AAPL", stale, 150.0],
    )
    out = run_prediction_pipeline(temp_db, prediction_date=stale,
                                   skip_features=True, skip_outcomes=True)
    assert out.get("skipped") == "stale_data"
    assert any("DATA STALE" in r.message for r in caplog.records)


def test_crypto_pipeline_skips_when_stale(temp_db, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    stale = date.today() - timedelta(days=10)
    temp_db.execute(
        "INSERT INTO crypto_prices_daily (symbol, trade_date, close) VALUES (?, ?, ?)",
        ["BTCUSDT", stale, 50_000.0],
    )
    out = run_crypto_prediction_pipeline(temp_db, prediction_date=stale,
                                          skip_features=True, skip_outcomes=True)
    assert out.get("skipped") == "stale_data"
    assert any("DATA STALE" in r.message for r in caplog.records)


def test_fx_pipeline_warns_but_runs_when_stale(temp_db, caplog):
    """FX is intentionally tolerant of stale bars — logs warning but
    continues. Confirms ADR-010 asymmetry."""
    import logging
    caplog.set_level(logging.WARNING)
    stale_dt = datetime.utcnow() - timedelta(hours=10)
    stale_dt = stale_dt.replace(minute=0, second=0, microsecond=0)
    temp_db.execute(
        "INSERT INTO fx_prices_hourly (datetime_utc, date, weekday, hour_utc, "
        "gbpeur_close, data_quality) VALUES (?, ?, ?, ?, ?, ?)",
        [stale_dt, stale_dt.date(), stale_dt.strftime("%A"), stale_dt.hour, 1.18, "OK"],
    )
    # No active models → score_bar returns empty predictions, but pipeline
    # should NOT short-circuit on freshness alone.
    out = run_fx_prediction_pipeline(temp_db, send_alerts=False, skip_outcomes=True)
    # Pipeline ran past the freshness check (didn't return skipped:stale_data).
    assert "skipped" not in out
    assert any("DATA STALE" in r.message for r in caplog.records)


# ──────────────────────────────────────────────────────────────────────
# Missing data
# ──────────────────────────────────────────────────────────────────────


def test_equity_pipeline_handles_empty_universe(temp_db):
    """No companies → freshness check fails first (empty prices_daily)."""
    out = run_prediction_pipeline(temp_db, skip_features=True, skip_outcomes=True)
    # Either skipped due to stale (empty) data, or returns a structural error.
    # Neither should be a crash.
    assert isinstance(out, dict)


def test_fx_predict_no_active_models(temp_db, synthetic_prices_fx):
    """score_bar with no active models returns empty predictions, no
    crash. The pipeline test exercises this through run_fx_prediction_pipeline."""
    from tests.integration._helpers import insert_fx_prices
    insert_fx_prices(temp_db, synthetic_prices_fx(num_hours=10))

    out = run_fx_prediction_pipeline(temp_db, send_alerts=False, skip_outcomes=True)
    assert out["predictions"] == {}


# ──────────────────────────────────────────────────────────────────────
# DB lock retry
# ──────────────────────────────────────────────────────────────────────


def test_storage_db_retries_on_lock_error(monkeypatch, tmp_path):
    """KI-111 regression: get_connection retries when DuckDB raises
    `Could not set lock`."""
    from storage import db as storage_db

    target = tmp_path / "x.duckdb"
    call_count = {"n": 0}
    real_connect = duckdb.connect

    def flaky_connect(path, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise duckdb.IOException("Could not set lock on file: blocked")
        return real_connect(str(path), **kwargs)

    monkeypatch.setattr(storage_db.duckdb, "connect", flaky_connect)
    # Disable the actual sleep so the test isn't slow.
    monkeypatch.setattr(storage_db.time, "sleep", lambda _: None)

    conn = storage_db.get_connection(str(target))
    assert call_count["n"] == 3  # retried twice, succeeded on third
    conn.close()


def test_storage_db_propagates_non_lock_errors(monkeypatch, tmp_path):
    """A non-lock IOException must NOT be swallowed by retry logic."""
    from storage import db as storage_db

    def always_fail(path, **kwargs):
        raise duckdb.IOException("Disk is full or something")

    monkeypatch.setattr(storage_db.duckdb, "connect", always_fail)
    monkeypatch.setattr(storage_db.time, "sleep", lambda _: None)

    with pytest.raises(duckdb.IOException, match="Disk is full"):
        storage_db.get_connection(str(tmp_path / "x.duckdb"))


# ──────────────────────────────────────────────────────────────────────
# Missing model file
# ──────────────────────────────────────────────────────────────────────


def test_equity_predict_with_nonexistent_model_path(temp_db, monkeypatch):
    """If ml_model_runs.is_active points at a missing file, the pipeline
    should raise (not fail silently) — we register a path that doesn't
    exist and expect joblib.load to raise."""
    from datetime import date as _date
    from ml import predict as predict_mod

    pred_date = _date.today()
    temp_db.execute(
        "INSERT INTO companies (ticker, company_name, sector, is_active, is_etf, "
        "market_cap) VALUES ('AAPL', 'Apple', 'Technology', true, false, 3e12)"
    )
    # Insert ml_features so score_universe progresses past the empty-features check.
    from ml.train import FEATURE_COLS
    cols = ", ".join(FEATURE_COLS)
    placeholders = ", ".join(["?"] * len(FEATURE_COLS))
    temp_db.execute(
        f"INSERT INTO ml_features (ticker, trade_date, {cols}) VALUES (?, ?, {placeholders})",
        ["AAPL", pred_date] + [0.0] * len(FEATURE_COLS),
    )
    temp_db.execute(
        "INSERT INTO ml_model_runs (model_id, horizon, target_threshold, model_path, is_active) "
        "VALUES ('m_missing', '20d', 0.10, '/tmp/this_file_does_not_exist.joblib', true)"
    )

    with pytest.raises((FileNotFoundError, OSError)):
        predict_mod.score_universe(temp_db, pred_date)
