"""Unit tests for the 6 production monitors in monitoring/.

Each test exercises one monitor's pure-logic path with the temp_db
fixture and asserts the MonitorResult shape. mock_telegram captures
any would-be Telegram sends so we never hit the real API.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest

from monitoring import alert as alert_mod
from monitoring.alert import MonitorResult


@pytest.fixture(autouse=True)
def force_dry_run(monkeypatch):
    monkeypatch.setenv("MONITORING_DRY_RUN", "true")


# ──────────────────────────────────────────────────────────────────────
# alert.py shape + dispatch behavior
# ──────────────────────────────────────────────────────────────────────


def test_monitor_result_to_telegram_text_includes_severity_prefix():
    r = MonitorResult(monitor="x", status="warn", severity="warn",
                       title="t", body="b")
    text = r.to_telegram_text()
    assert "[!] MHDE monitor: x" in text
    assert "t" in text
    assert "b" in text


def test_send_alert_skips_ok_results(mock_telegram):
    r = MonitorResult(monitor="x", status="ok", severity="info", title="t")
    sent = alert_mod.send_alert(r)
    assert sent is False
    assert mock_telegram == []


def test_send_alert_dry_run_does_not_call_telegram(mock_telegram, monkeypatch):
    monkeypatch.setenv("MONITORING_DRY_RUN", "true")
    r = MonitorResult(monitor="x", status="fail", severity="critical",
                       title="failure")
    sent = alert_mod.send_alert(r)
    assert sent is False  # dry-run suppresses
    # mock_telegram captures requests.post — none should be invoked
    assert mock_telegram == []


# ──────────────────────────────────────────────────────────────────────
# dashboard_consistency
# ──────────────────────────────────────────────────────────────────────


def test_dashboard_consistency_ok_on_empty_db(temp_db):
    """Empty DB has no candidate_outcomes — dashboard and direct query
    both return 0; no mismatch."""
    from monitoring import dashboard_consistency
    result = dashboard_consistency.run(conn=temp_db)
    assert result.monitor == "dashboard_consistency"
    assert result.status == "ok"


# ──────────────────────────────────────────────────────────────────────
# pipeline_execution
# ──────────────────────────────────────────────────────────────────────


def test_pipeline_execution_flags_empty_engines(temp_db):
    """All three prediction tables empty → flagged."""
    from monitoring import pipeline_execution
    result = pipeline_execution.run(conn=temp_db)
    assert result.status in ("warn", "fail")
    body = result.body.lower()
    for engine in ("equity", "crypto", "fx"):
        assert engine in body


def test_pipeline_execution_ok_when_fresh(temp_db):
    from monitoring import pipeline_execution
    today = date.today()
    # 30 rows for the last 14 days each → high baseline
    for d_offset in range(15):
        d = today - timedelta(days=d_offset)
        for i in range(30):
            temp_db.execute(
                "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
                "horizon, predicted_probability, prediction_threshold) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [f"T{i}", d, "m1", "20d", 0.6 + 0.01 * i, 0.10],
            )
            temp_db.execute(
                "INSERT INTO crypto_ml_predictions (symbol, prediction_date, model_id, "
                "horizon, predicted_probability, prediction_threshold) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [f"S{i}USDT", d, "m1", "5d", 0.6, 0.10],
            )
    # FX: hourly bars over the past 27 hours
    now = datetime.utcnow().replace(minute=5, second=0, microsecond=0)
    for h_offset in range(27):
        bar = now - timedelta(hours=h_offset)
        temp_db.execute(
            "INSERT INTO fx_ml_predictions (datetime_utc, model_id, direction, "
            "horizon, predicted_probability, prediction_threshold) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [bar, "fx_m1", "up", "24h", 0.6, 20],
        )

    result = pipeline_execution.run(conn=temp_db, now=datetime.utcnow().replace(tzinfo=timezone.utc))
    # All three engines have recent rows above threshold — ok.
    assert result.status == "ok", f"got {result.status}: {result.body}"


# ──────────────────────────────────────────────────────────────────────
# config_drift
# ──────────────────────────────────────────────────────────────────────


def test_config_drift_runs_without_crashing():
    """In a CI environment the deployed dirs may not exist — monitor
    must still return a structured result."""
    from monitoring import config_drift
    result = config_drift.run()
    assert result.monitor == "config_drift"
    assert result.status in ("ok", "warn")


# ──────────────────────────────────────────────────────────────────────
# model_performance
# ──────────────────────────────────────────────────────────────────────


def test_model_performance_skips_when_no_active_models(temp_db):
    from monitoring import model_performance
    result = model_performance.run(conn=temp_db)
    assert result.status == "ok"  # no models to fail


def test_model_performance_flags_degradation(temp_db):
    """High baseline + low rolling = ratio < 0.8 → warn."""
    from monitoring import model_performance
    # Active model with baseline 0.40 precision
    temp_db.execute(
        "INSERT INTO ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active, precision_at_threshold) "
        "VALUES ('m1', '20d', 0.10, '/tmp/x.joblib', true, 0.40)"
    )
    # Last 7 days: 10 predictions filled, 1 hit → precision 0.10 (well below 0.32 threshold).
    today = date.today()
    for i in range(10):
        d = today - timedelta(days=i + 1)
        temp_db.execute(
            "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
            "horizon, predicted_probability, prediction_threshold, "
            "actual_hit, outcome_filled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [f"T{i}", d, "m1", "20d", 0.7, 0.10,
             (i == 0), datetime.now(timezone.utc) - timedelta(days=i)],
        )
    result = model_performance.run(conn=temp_db)
    assert result.status == "warn"
    assert "ratio" in result.body or "baseline" in result.body


# ──────────────────────────────────────────────────────────────────────
# data_quality
# ──────────────────────────────────────────────────────────────────────


def test_data_quality_flags_empty_tables(temp_db):
    from monitoring import data_quality
    result = data_quality.run(conn=temp_db)
    assert result.status == "warn"
    # All three engines flagged as empty
    body_lower = result.body.lower()
    for engine in ("equity", "crypto", "fx"):
        assert engine in body_lower


# ──────────────────────────────────────────────────────────────────────
# smoke_test
# ──────────────────────────────────────────────────────────────────────


def test_smoke_test_fails_without_active_models(temp_db):
    """No active model rows → smoke fails."""
    from monitoring import smoke_test
    result = smoke_test.run(conn=temp_db)
    # DB opens fine, dashboard query works, but no active models for any
    # engine → fail.
    assert result.status == "fail"
    assert "no active model" in result.body.lower()


def test_smoke_test_flags_missing_joblib(temp_db, tmp_path):
    """Active model row pointing at a nonexistent joblib → fail."""
    from monitoring import smoke_test
    fake_path = tmp_path / "does_not_exist.joblib"
    temp_db.execute(
        "INSERT INTO ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) VALUES ('m1', '20d', 0.10, ?, true)",
        [str(fake_path)],
    )
    result = smoke_test.run(conn=temp_db)
    assert result.status == "fail"
    assert "missing" in result.body.lower() or "path" in result.body.lower()
