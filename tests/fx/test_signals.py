"""Unit tests for fx/ml/signals.py — generate_signal decision matrix.

Coverage targets:
  - BUY_GBP only when prob_up_24h >= BUY_THRESHOLD AND prob_down_24h < COUNTER_MAX
  - SELL_GBP only when prob_down_24h >= SELL_THRESHOLD AND prob_up_24h < COUNTER_MAX
  - WAIT (None) when neither condition is met
  - Confidence collisions (high up + high down) → WAIT, not both
  - Signal row written to fx_signals on non-WAIT
  - ON CONFLICT prevents duplicate rows when called twice with the same key
"""
from __future__ import annotations

from datetime import datetime

import pytest

from fx.config import (SIGNAL_BUY_THRESHOLD, SIGNAL_SELL_THRESHOLD,
                       SIGNAL_COUNTER_MAX)
from fx.ml.signals import generate_signal


def _preds(up24=0.5, down24=0.5, up48=0.5, down48=0.5):
    return {
        "up_24h": {"probability": up24},
        "down_24h": {"probability": down24},
        "up_48h": {"probability": up48},
        "down_48h": {"probability": down48},
    }


@pytest.fixture
def now():
    return datetime(2026, 5, 7, 14, 0, 0)


def test_buy_signal_when_up_high_down_low(temp_db, now):
    sig = generate_signal(
        _preds(up24=SIGNAL_BUY_THRESHOLD + 0.05, down24=SIGNAL_COUNTER_MAX - 0.05),
        now, 1.18, temp_db,
    )
    assert sig is not None
    assert sig["type"] == "BUY_GBP"


def test_sell_signal_when_down_high_up_low(temp_db, now):
    sig = generate_signal(
        _preds(up24=SIGNAL_COUNTER_MAX - 0.05, down24=SIGNAL_SELL_THRESHOLD + 0.05),
        now, 1.18, temp_db,
    )
    assert sig is not None
    assert sig["type"] == "SELL_GBP"


def test_wait_when_both_low(temp_db, now):
    sig = generate_signal(_preds(up24=0.4, down24=0.4), now, 1.18, temp_db)
    assert sig is None


def test_wait_when_both_high_collide(temp_db, now):
    """If both up and down probabilities are above their thresholds, the
    counter-suppression kicks in for both directions → WAIT."""
    sig = generate_signal(
        _preds(up24=SIGNAL_BUY_THRESHOLD + 0.05,
               down24=SIGNAL_SELL_THRESHOLD + 0.05),
        now, 1.18, temp_db,
    )
    assert sig is None


def test_wait_at_threshold_boundary(temp_db, now):
    """At exactly the BUY threshold + counter just below COUNTER_MAX,
    BUY_GBP fires (`>=` is the inclusive comparison in signals.py)."""
    sig = generate_signal(
        _preds(up24=SIGNAL_BUY_THRESHOLD, down24=SIGNAL_COUNTER_MAX - 0.001),
        now, 1.18, temp_db,
    )
    assert sig is not None
    assert sig["type"] == "BUY_GBP"


def test_signal_row_persisted(temp_db, now):
    sig = generate_signal(
        _preds(up24=SIGNAL_BUY_THRESHOLD + 0.05, down24=SIGNAL_COUNTER_MAX - 0.05),
        now, 1.18, temp_db,
    )
    rows = temp_db.execute(
        "SELECT signal_type, prob_up_24h, gbpeur_price FROM fx_signals "
        "WHERE datetime_utc = ?", [now]
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "BUY_GBP"


def test_signal_idempotent_on_same_key(temp_db, now):
    """Re-calling with the same (datetime, signal_type) doesn't duplicate."""
    preds = _preds(up24=SIGNAL_BUY_THRESHOLD + 0.05,
                   down24=SIGNAL_COUNTER_MAX - 0.05)
    generate_signal(preds, now, 1.18, temp_db)
    generate_signal(preds, now, 1.18, temp_db)
    n = temp_db.execute(
        "SELECT COUNT(*) FROM fx_signals WHERE datetime_utc = ?", [now]
    ).fetchone()[0]
    assert n == 1


def test_signal_carries_all_probabilities(temp_db, now):
    preds = _preds(up24=0.7, down24=0.2, up48=0.6, down48=0.3)
    sig = generate_signal(preds, now, 1.18, temp_db)
    assert sig["prob_up_24h"] == 0.7
    assert sig["prob_down_24h"] == 0.2
    assert sig["prob_up_48h"] == 0.6
    assert sig["prob_down_48h"] == 0.3


def test_missing_horizon_keys_treated_as_zero(temp_db, now):
    """If a horizon prediction is missing entirely, generate_signal reads
    it as probability=0 → WAIT."""
    sig = generate_signal({}, now, 1.18, temp_db)
    assert sig is None
