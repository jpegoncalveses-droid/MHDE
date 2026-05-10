"""KI-128 regression: pipeline_execution FX leg honors the forex-closed
window. Equity 75h budget already covers the weekend per ADR-015 and
is pinned here as a regression to ensure the refactor doesn't change it.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def force_dry_run(monkeypatch):
    monkeypatch.setenv("MONITORING_DRY_RUN", "true")


def _seed_active_models(conn):
    """Insert one active model per engine so n_active > 0 in each."""
    conn.execute(
        "INSERT INTO ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) VALUES ('eq', '20d', 0.10, '/tmp/x', true)"
    )
    conn.execute(
        "INSERT INTO crypto_ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) VALUES ('cr', '5d', 0.10, '/tmp/x', true)"
    )
    conn.execute(
        "INSERT INTO fx_ml_model_runs (model_id, direction, horizon, "
        "target_pips, model_path, is_active) "
        "VALUES ('fx_m1', 'up', '24h', 20, '/tmp/x', true)"
    )


def _seed_baseline_predictions(conn, eq_date: date, cr_date: date, fx_dt: datetime):
    """Seed enough baseline rows so the 14-day baseline doesn't trip
    the count check. We're testing recency, not row counts."""
    # Equity 14-day history: 30 rows/day for 14 days ending eq_date.
    for d_offset in range(15):
        d = eq_date - timedelta(days=d_offset)
        for i in range(30):
            conn.execute(
                "INSERT INTO ml_predictions (ticker, prediction_date, "
                "model_id, horizon, predicted_probability, prediction_threshold) "
                "VALUES (?, ?, 'eq', '20d', 0.6, 0.10)",
                [f"T{i}", d],
            )
    # Crypto 14-day history.
    for d_offset in range(15):
        d = cr_date - timedelta(days=d_offset)
        for i in range(30):
            conn.execute(
                "INSERT INTO crypto_ml_predictions (symbol, prediction_date, "
                "model_id, horizon, predicted_probability, prediction_threshold) "
                "VALUES (?, ?, 'cr', '5d', 0.6, 0.10)",
                [f"S{i}USDT", d],
            )
    # FX 14-day baseline of hourly rows ending at fx_dt. Use 5/day to
    # stay above the n_avg > 5 gate.
    for d_offset in range(15):
        for h in range(5):
            ts = fx_dt - timedelta(days=d_offset, hours=h)
            conn.execute(
                "INSERT INTO fx_ml_predictions (datetime_utc, model_id, "
                "direction, horizon, predicted_probability, prediction_threshold) "
                "VALUES (?, 'fx_m1', 'up', '24h', 0.6, 20)",
                [ts],
            )


def test_pipeline_execution_fx_ok_during_close_with_pre_close_bar(temp_db):
    """Sat 12:00 UTC, latest FX bar at Fri 21:00 UTC — must pass."""
    from monitoring import pipeline_execution

    _seed_active_models(temp_db)
    eq_date = date(2026, 5, 15)  # Fri
    cr_date = date(2026, 5, 16)  # Sat
    fx_dt = datetime(2026, 5, 15, 21, 0, 0)  # Fri 21:00 UTC
    _seed_baseline_predictions(temp_db, eq_date, cr_date, fx_dt)

    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)  # Sat
    result = pipeline_execution.run(conn=temp_db, now=now)
    assert result.metrics["fx"]["recency_ok"] is True, (
        f"fx recency must pass during forex close with pre-close bar; "
        f"reason={result.metrics['fx'].get('reason')}"
    )


def test_pipeline_execution_fx_fails_during_close_with_outage(temp_db):
    """Sat 12:00 UTC, latest FX bar at Wed 10:00 UTC — outage in flight."""
    from monitoring import pipeline_execution

    _seed_active_models(temp_db)
    eq_date = date(2026, 5, 15)
    cr_date = date(2026, 5, 16)
    fx_dt = datetime(2026, 5, 13, 10, 0, 0)  # Wed
    _seed_baseline_predictions(temp_db, eq_date, cr_date, fx_dt)

    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)
    result = pipeline_execution.run(conn=temp_db, now=now)
    assert result.metrics["fx"]["recency_ok"] is False
    reason = result.metrics["fx"].get("reason", "")
    assert "closed window" in reason.lower() or "floor" in reason.lower() or "predates" in reason.lower(), (
        f"reason should call out outage during closed window; got {reason!r}"
    )


def test_pipeline_execution_fx_fails_post_resume_with_stale_data(temp_db):
    """Sun 23:00 UTC — post-resume, 2h budget active. Stale Fri bar fails."""
    from monitoring import pipeline_execution

    _seed_active_models(temp_db)
    eq_date = date(2026, 5, 15)
    cr_date = date(2026, 5, 17)
    fx_dt = datetime(2026, 5, 15, 21, 0, 0)
    _seed_baseline_predictions(temp_db, eq_date, cr_date, fx_dt)

    now = datetime(2026, 5, 17, 23, 0, 0, tzinfo=timezone.utc)
    result = pipeline_execution.run(conn=temp_db, now=now)
    assert result.metrics["fx"]["recency_ok"] is False


def test_pipeline_execution_equity_ok_on_monday_morning(temp_db):
    """Pinned regression: ADR-015's 75h equity budget covers Mon morning
    when the latest prediction_date is Friday. Must remain unchanged
    by the KI-128 refactor."""
    from monitoring import pipeline_execution

    _seed_active_models(temp_db)
    eq_date = date(2026, 5, 15)  # Fri
    cr_date = date(2026, 5, 18)  # Mon
    fx_dt = datetime(2026, 5, 18, 1, 0, 0)  # Mon 01:00, before now
    _seed_baseline_predictions(temp_db, eq_date, cr_date, fx_dt)

    now = datetime(2026, 5, 18, 2, 0, 0, tzinfo=timezone.utc)  # Mon 02:00 (74h from Fri midnight)
    result = pipeline_execution.run(conn=temp_db, now=now)
    assert result.metrics["equity"]["recency_ok"] is True, (
        f"equity 75h budget should still cover Mon 02:00 with Fri data; "
        f"reason={result.metrics['equity'].get('reason')}"
    )
