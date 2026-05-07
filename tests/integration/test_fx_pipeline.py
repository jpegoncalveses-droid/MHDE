"""Integration test: FX ML pipeline end-to-end.

The FX pipeline diverges from equity/crypto in three ways: hourly
cadence, single time-series (no symbol dimension), and a stateful
Telegram bot with alert suppression. This test exercises the full
score → generate_signal → send_signal_alert chain.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from fx.config import FEATURE_COLS, SIGNAL_BUY_THRESHOLD, SIGNAL_COUNTER_MAX
from fx.ml.features import compute_features
from fx.ml.labels import compute_labels
from fx.ml.predict import score_bar, fill_outcomes
from fx.ml.signals import generate_signal

from tests.integration._helpers import (
    insert_fx_prices,
    register_active_fx_model,
    train_tiny_model,
)


@pytest.fixture
def fx_pipeline_state(temp_db, synthetic_prices_fx, tmp_path):
    """600 hourly bars + 4 active FX models (up/down × 24h/48h)."""
    rows = synthetic_prices_fx(num_hours=600)
    insert_fx_prices(temp_db, rows)

    # Train one model per direction × horizon = 4 active models.
    for direction in ("up", "down"):
        for horizon in ("24h", "48h"):
            path = train_tiny_model(
                FEATURE_COLS,
                tmp_path / f"fx_{direction}_{horizon}.joblib",
                seed=hash((direction, horizon)) % 10000,
            )
            register_active_fx_model(temp_db, path, direction=direction,
                                      horizon=horizon, target_pips=20)
    return temp_db


def test_fx_pipeline_end_to_end(fx_pipeline_state):
    conn = fx_pipeline_state

    # Labels + features
    n_labels = compute_labels(conn)
    assert n_labels > 0
    n_features = compute_features(conn)
    assert n_features > 0

    # Score the latest bar
    latest = conn.execute(
        "SELECT MAX(datetime_utc) FROM fx_ml_features"
    ).fetchone()[0]
    out = score_bar(conn, latest)
    assert "predictions" in out
    # 4 active models, so all 4 predictions
    assert len(out["predictions"]) == 4
    for key in ("up_24h", "down_24h", "up_48h", "down_48h"):
        assert key in out["predictions"]

    # Predictions persisted
    pred_count = conn.execute(
        "SELECT COUNT(*) FROM fx_ml_predictions WHERE datetime_utc = ?", [latest]
    ).fetchone()[0]
    assert pred_count == 4

    # generate_signal — based on the model probabilities. The tiny model
    # was trained on biased noise so probabilities are typically high.
    sig = generate_signal(out["predictions"], latest, out["price"], conn)
    # sig may be None (WAIT) or a dict — either is structurally valid.
    if sig is not None:
        assert sig["type"] in ("BUY_GBP", "SELL_GBP")
        # Signal row persisted
        signal_count = conn.execute(
            "SELECT COUNT(*) FROM fx_signals WHERE datetime_utc = ?", [latest]
        ).fetchone()[0]
        assert signal_count == 1


def test_fx_fill_outcomes_uses_horizon_window(fx_pipeline_state):
    """KI-103-class regression for FX: outcome window matches horizon."""
    conn = fx_pipeline_state
    compute_labels(conn)
    compute_features(conn)

    # Insert a prediction 25h before the latest bar — 24h window has
    # elapsed but 48h has not.
    latest = conn.execute(
        "SELECT MAX(datetime_utc) FROM fx_prices_hourly"
    ).fetchone()[0]
    pred_dt = latest - timedelta(hours=25)
    # Need a price row at pred_dt for fill_outcomes' join
    row = conn.execute(
        "SELECT 1 FROM fx_prices_hourly WHERE datetime_utc = ?", [pred_dt]
    ).fetchone()
    if not row:
        pytest.skip("synthetic generator skipped pred_dt (FX weekend)")

    conn.execute(
        "INSERT INTO fx_ml_predictions (datetime_utc, model_id, direction, horizon, "
        "predicted_probability, prediction_threshold) VALUES (?, ?, ?, ?, ?, ?)",
        [pred_dt, "test_24h", "up", "24h", 0.7, 20],
    )
    conn.execute(
        "INSERT INTO fx_ml_predictions (datetime_utc, model_id, direction, horizon, "
        "predicted_probability, prediction_threshold) VALUES (?, ?, ?, ?, ?, ?)",
        [pred_dt, "test_48h", "up", "48h", 0.7, 20],
    )
    fill_outcomes(conn)

    # 24h window elapsed → outcome filled
    row24 = conn.execute(
        "SELECT outcome_filled_at FROM fx_ml_predictions "
        "WHERE datetime_utc = ? AND horizon = '24h'", [pred_dt]
    ).fetchone()
    assert row24[0] is not None

    # 48h window not yet elapsed → still NULL
    row48 = conn.execute(
        "SELECT outcome_filled_at FROM fx_ml_predictions "
        "WHERE datetime_utc = ? AND horizon = '48h'", [pred_dt]
    ).fetchone()
    assert row48[0] is None


def test_fx_position_aware_alert_suppression(temp_db, monkeypatch, mock_telegram):
    """KI-110 regression: if fx_position is 'HOLDING_GBP', BUY_GBP alerts
    must be suppressed."""
    from fx.bot import telegram_bot

    # Redirect the bot's _open_conn to return our temp_db (no .close()
    # so the connection stays alive across calls).
    class _NoCloseConn:
        def __init__(self, c): self._c = c
        def __getattr__(self, name): return getattr(self._c, name)
        def close(self): pass  # don't actually close — fixture owns it

    monkeypatch.setattr(
        telegram_bot, "_open_conn",
        lambda read_only=False: _NoCloseConn(temp_db),
    )
    # Stub send_message so we don't need real Telegram credentials.
    monkeypatch.setattr(telegram_bot, "send_message", lambda text: 12345)

    # Set position long → BUY_GBP should be suppressed.
    temp_db.execute(
        "INSERT INTO fx_position (position, entry_rate, entry_date) "
        "VALUES ('HOLDING_GBP', 1.18, '2026-05-01 00:00:00')"
    )
    # Need a corresponding fx_signals row for telegram_sent UPDATE.
    bar_dt = datetime(2026, 5, 7, 12, 0, 0)
    temp_db.execute(
        "INSERT INTO fx_signals (datetime_utc, signal_type, gbpeur_price, "
        "prob_up_24h, prob_down_24h, prob_up_48h, prob_down_48h) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [bar_dt, "BUY_GBP", 1.18, 0.75, 0.20, 0.70, 0.25],
    )

    sent = telegram_bot.send_signal_alert({
        "type": "BUY_GBP", "datetime": bar_dt, "price": 1.18,
        "prob_up_24h": 0.75, "prob_down_24h": 0.20,
        "prob_up_48h": 0.70, "prob_down_48h": 0.25,
    })
    assert sent is False, "BUY_GBP should be suppressed when already long_gbp"


def test_fx_alert_sent_when_position_compatible(temp_db, monkeypatch, mock_telegram):
    """SELL_GBP from a long_gbp position is actionable → alert fires."""
    from fx.bot import telegram_bot

    class _NoCloseConn:
        def __init__(self, c): self._c = c
        def __getattr__(self, name): return getattr(self._c, name)
        def close(self): pass

    monkeypatch.setattr(
        telegram_bot, "_open_conn",
        lambda read_only=False: _NoCloseConn(temp_db),
    )
    # Capture send_message calls. A non-None return means "sent".
    sent_messages: list[str] = []

    def _fake_send(text: str):
        sent_messages.append(text)
        return 99999

    monkeypatch.setattr(telegram_bot, "send_message", _fake_send)

    temp_db.execute(
        "INSERT INTO fx_position (position, entry_rate, entry_date) "
        "VALUES ('HOLDING_GBP', 1.18, '2026-05-01 00:00:00')"
    )
    bar_dt = datetime(2026, 5, 7, 12, 0, 0)
    temp_db.execute(
        "INSERT INTO fx_signals (datetime_utc, signal_type, gbpeur_price, "
        "prob_up_24h, prob_down_24h, prob_up_48h, prob_down_48h) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [bar_dt, "SELL_GBP", 1.18, 0.20, 0.75, 0.25, 0.70],
    )

    sent = telegram_bot.send_signal_alert({
        "type": "SELL_GBP", "datetime": bar_dt, "price": 1.18,
        "prob_up_24h": 0.20, "prob_down_24h": 0.75,
        "prob_up_48h": 0.25, "prob_down_48h": 0.70,
    })
    assert sent is True
    assert len(sent_messages) == 1
    assert "SELL_GBP" in sent_messages[0]
