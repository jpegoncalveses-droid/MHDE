"""Regression: crypto pipeline_execution honors the T-1 semantic of
``prediction_date``.

Background. ``crypto_ml_predictions.prediction_date`` is the features
``trade_date`` (the last completed feature day), not the time the
scoring pipeline ran. The monitor compares ``now - midnight UTC of
prediction_date`` against ``RECENCY_BUDGET['crypto']``. Because
prediction_date is always *yesterday* (T-1 calendar day) at write
time, the minimum age right after a successful 00:30 UTC fire is
~24h 30m, and the maximum age right before the next fire is ~48h 30m.
The original budget of 27h (24h + 3h grace) was set as if
prediction_date incremented to *today*, which it does not. Result:
the monitor false-fired roughly 21 hours of every 24-hour cycle even
when the pipeline ran fine. Fix: budget is now ``2 days 3 hours``
(48h cycle + 3h grace), which still catches a one-day outage at
~03:30 UTC on day+2.

These three cases pin the post-fix budget:

  * ``test_crypto_ok_at_normal_afternoon`` — on-time fire yesterday;
    now is 14:00 UTC today; age ≈ 38h must pass. This is the case
    the original 27h budget got wrong.
  * ``test_crypto_ok_just_before_next_fire`` — on-time fire yesterday;
    now is just before the next 00:30 fire (~48h 29m age); must pass.
  * ``test_crypto_fails_when_two_consecutive_fires_missed`` —
    no fire on the last two scheduled days; latest is day-3 midnight;
    now is 04:00 UTC today (~52h age); must fail with a reason that
    references the budget.

If anyone tightens the crypto budget back below 51h without also
introducing a run-time column on ``crypto_ml_predictions``, the first
two tests fail. If anyone loosens it past the two-day cycle, the
third test fails. See ADR-029 and KI-141.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def force_dry_run(monkeypatch):
    monkeypatch.setenv("MONITORING_DRY_RUN", "true")


def _seed_active_models(conn):
    """Insert one active model per engine so n_active > 0 in each.

    Equity and FX get seeded so their pipelines do not dominate or
    short-circuit the result we want to inspect (crypto)."""
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


def _seed_crypto_predictions(conn, latest_pred_date: date, days: int = 15):
    """Seed ``days`` of crypto predictions ending at ``latest_pred_date``,
    30 rows/day, attributed to the active model.

    Equity and FX get a minimal active row each so their recency checks
    do not flag against this test's ``now``."""
    for d_offset in range(days):
        d = latest_pred_date - timedelta(days=d_offset)
        for i in range(30):
            conn.execute(
                "INSERT INTO crypto_ml_predictions (symbol, prediction_date, "
                "model_id, horizon, predicted_probability, prediction_threshold) "
                "VALUES (?, ?, 'cr', '5d', 0.6, 0.10)",
                [f"S{i}USDT", d],
            )


def _seed_other_engines_fresh(conn, now: datetime):
    """Make equity and FX recency_ok=True for the given ``now`` so they
    don't pollute the crypto-focused assertions."""
    eq_date = now.date() - timedelta(days=1)
    for i in range(30):
        conn.execute(
            "INSERT INTO ml_predictions (ticker, prediction_date, model_id, "
            "horizon, predicted_probability, prediction_threshold) "
            "VALUES (?, ?, 'eq', '20d', 0.6, 0.10)",
            [f"T{i}", eq_date],
        )
    fx_ts = now - timedelta(hours=1)
    for h in range(5):
        conn.execute(
            "INSERT INTO fx_ml_predictions (datetime_utc, model_id, direction, "
            "horizon, predicted_probability, prediction_threshold) "
            "VALUES (?, 'fx_m1', 'up', '24h', 0.6, 20)",
            [fx_ts - timedelta(hours=h)],
        )


def test_crypto_ok_at_normal_afternoon(temp_db):
    """Pipeline fired on time at 00:30 UTC today; latest prediction_date
    is yesterday (T-1). Now is 14:00 UTC today, age ≈ 38h.

    This is the failure mode that motivated the fix — the old 27h
    budget alerted from ~03:30 UTC onwards every day."""
    from monitoring import pipeline_execution

    _seed_active_models(temp_db)
    latest_pred_date = date(2026, 5, 13)  # T-1
    _seed_crypto_predictions(temp_db, latest_pred_date)
    now = datetime(2026, 5, 14, 14, 0, 0, tzinfo=timezone.utc)
    _seed_other_engines_fresh(temp_db, now)

    result = pipeline_execution.run(conn=temp_db, now=now)
    crypto = result.metrics["crypto"]
    assert crypto["recency_ok"] is True, (
        "crypto recency must pass at 14:00 UTC the day after an on-time "
        "fire — prediction_date is T-1 so age is ~38h, well inside the "
        f"2-day cycle. Got reason={crypto.get('reason')}. If this fails, "
        "the budget was tightened below 48h+grace; see ADR-029."
    )


def test_crypto_ok_just_before_next_fire(temp_db):
    """On-time fire yesterday at 00:30 UTC; latest prediction_date is
    yesterday. Now is 00:29 UTC tomorrow — i.e. the moment right
    before the next scheduled fire. Age ≈ 48h 29m. Must pass."""
    from monitoring import pipeline_execution

    _seed_active_models(temp_db)
    latest_pred_date = date(2026, 5, 13)
    _seed_crypto_predictions(temp_db, latest_pred_date)
    now = datetime(2026, 5, 15, 0, 29, 0, tzinfo=timezone.utc)
    _seed_other_engines_fresh(temp_db, now)

    result = pipeline_execution.run(conn=temp_db, now=now)
    crypto = result.metrics["crypto"]
    assert crypto["recency_ok"] is True, (
        "crypto recency must pass just before the next 00:30 fire — "
        "age ~48h 29m sits inside the 2d 3h budget. "
        f"reason={crypto.get('reason')}"
    )


def test_crypto_fails_when_two_consecutive_fires_missed(temp_db):
    """Pipeline missed both of the last two scheduled 00:30 fires;
    latest prediction_date is from three calendar days ago. Now is
    04:00 UTC today, age ≈ 52h. Must fail recency, and the reason
    must reference the configured budget."""
    from monitoring import pipeline_execution

    _seed_active_models(temp_db)
    latest_pred_date = date(2026, 5, 12)  # 2 missed fires
    _seed_crypto_predictions(temp_db, latest_pred_date)
    now = datetime(2026, 5, 14, 4, 0, 0, tzinfo=timezone.utc)
    _seed_other_engines_fresh(temp_db, now)

    result = pipeline_execution.run(conn=temp_db, now=now)
    crypto = result.metrics["crypto"]
    assert crypto["recency_ok"] is False, (
        "crypto recency must fail when the pipeline has missed two "
        "consecutive 00:30 fires; age ~52h exceeds the 2d 3h budget."
    )
    reason = crypto.get("reason", "")
    assert "threshold" in reason.lower(), (
        f"failure reason should reference the configured threshold; "
        f"got {reason!r}"
    )
