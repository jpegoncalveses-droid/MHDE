"""Regression: pipeline_execution baseline composition.

Discipline session 2026-05-09 fix: ``monitoring/pipeline_execution.py``
filters BOTH the latest-day count and the 14-day rolling baseline
to predictions written by ``is_active=true`` model_ids in the
corresponding ``*_model_runs`` table. This is required for
correctness — the predictions tables also hold training /
walk-forward backtest rows that share the schema, and those
rows would otherwise inflate the baseline and produce false
positives.

This test is the fail-then-pass guard for that fix. It seeds a
predictions table with both active and inactive model rows for the
same dates, then asserts the monitor sees only the active count on
both sides of the comparison. If anyone removes the ``is_active``
filter from either the ``n_latest`` or the ``n_avg`` query, this
test fails.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def force_dry_run(monkeypatch):
    monkeypatch.setenv("MONITORING_DRY_RUN", "true")


def test_pipeline_execution_baseline_excludes_inactive_models(temp_db):
    from monitoring import pipeline_execution

    # Two crypto models, identical horizon/threshold:
    #   'active'   — writes 30 rows/day, is_active=true
    #   'backfill' — writes 96 rows/day, is_active=false (training/walkfold)
    temp_db.execute(
        "INSERT INTO crypto_ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) "
        "VALUES ('active', '5d', 0.10, '/tmp/active.joblib', true)"
    )
    temp_db.execute(
        "INSERT INTO crypto_ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) "
        "VALUES ('backfill', '5d', 0.10, '/tmp/backfill.joblib', false)"
    )

    today = date.today()
    for d_offset in range(15):
        d = today - timedelta(days=d_offset)
        # 30 rows/day from the active model
        for i in range(30):
            temp_db.execute(
                "INSERT INTO crypto_ml_predictions (symbol, prediction_date, "
                "model_id, horizon, predicted_probability, prediction_threshold) "
                "VALUES (?, ?, 'active', '5d', 0.6, 0.10)",
                [f"S{i}USDT", d],
            )
        # 96 rows/day from the inactive backfill — must NOT count toward
        # the baseline or the latest-day count.
        for i in range(96):
            temp_db.execute(
                "INSERT INTO crypto_ml_predictions (symbol, prediction_date, "
                "model_id, horizon, predicted_probability, prediction_threshold) "
                "VALUES (?, ?, 'backfill', '5d', 0.55, 0.10)",
                [f"BFS{i}USDT", d],
            )

    # Seed equity + fx active models so those engines don't dominate the
    # overall result; we inspect crypto metrics directly anyway.
    temp_db.execute(
        "INSERT INTO ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) VALUES ('m1', '20d', 0.10, '/tmp/x', true)"
    )
    temp_db.execute(
        "INSERT INTO fx_ml_model_runs (model_id, direction, horizon, "
        "target_pips, model_path, is_active) "
        "VALUES ('fx_m1', 'up', '24h', 20, '/tmp/x', true)"
    )

    result = pipeline_execution.run(
        conn=temp_db,
        now=datetime.utcnow().replace(tzinfo=timezone.utc),
    )

    crypto = result.metrics["crypto"]
    assert crypto["n_latest"] == 30, (
        "n_latest counted backfill rows — the latest-day count must filter "
        f"to is_active=true models. Got {crypto['n_latest']}, expected 30 "
        "(active-only). If this is 126, the WHERE m.is_active=true clause "
        "was removed from the n_latest query in _check_engine_pipeline."
    )
    assert crypto["n_avg"] == 30.0, (
        "14-day baseline counted backfill rows — the baseline query must "
        f"filter to is_active=true models. Got {crypto['n_avg']}, expected "
        "30.0 (active-only). If this is 126.0, the WHERE m.is_active=true "
        "clause was removed from the n_avg sub-query in "
        "_check_engine_pipeline."
    )
    assert crypto["count_ok"] is True, (
        "active-only baseline (30) should match active-only latest (30) "
        f"and pass the 50% gate; got count_ok={crypto['count_ok']} "
        f"reason={crypto.get('reason')}"
    )


def test_pipeline_execution_flags_engine_with_no_active_models(temp_db):
    """When a *_model_runs table has zero is_active=true rows, the
    monitor must flag that engine — not silently report 'ok'. This
    is the partner check to the baseline-composition test: filtering
    must not let an empty model_runs table sneak through as fresh.
    """
    from monitoring import pipeline_execution

    # No active equity or crypto or fx models, but plenty of prediction
    # rows attributed to an inactive backfill model_id.
    temp_db.execute(
        "INSERT INTO crypto_ml_model_runs (model_id, horizon, target_threshold, "
        "model_path, is_active) "
        "VALUES ('backfill', '5d', 0.10, '/tmp/x', false)"
    )
    today = date.today()
    for d_offset in range(15):
        d = today - timedelta(days=d_offset)
        for i in range(96):
            temp_db.execute(
                "INSERT INTO crypto_ml_predictions (symbol, prediction_date, "
                "model_id, horizon, predicted_probability, prediction_threshold) "
                "VALUES (?, ?, 'backfill', '5d', 0.55, 0.10)",
                [f"S{i}USDT", d],
            )

    result = pipeline_execution.run(
        conn=temp_db,
        now=datetime.utcnow().replace(tzinfo=timezone.utc),
    )
    assert result.status in ("warn", "fail"), (
        "monitor returned ok despite no engine having any active model"
    )
    assert "no is_active" in result.body or "no active" in result.body.lower(), (
        f"flag body should call out missing active models; got: {result.body}"
    )
