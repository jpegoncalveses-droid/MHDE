"""Tests for Task 1.4: gate-and-persist logic in crypto/ml/train.py.

These tests exercise _persist_and_gate() directly — the private helper
extracted in Task 1.4 that wires the validation gate into the training
pipeline.  Full walk-forward training is NOT run here; the expensive ML
steps are tested elsewhere (Tasks 1.1/1.2/1.3).  The goal of this file
is to verify the gating + persistence + alert logic only.

Test cases
----------
1. test_promotion_proceeds_when_gate_passes
2. test_promotion_blocked_when_hit_rate_drops
3. test_first_model_promotes_without_baseline
4. test_structured_log_emitted_on_every_run
"""
from __future__ import annotations

import json
import logging
from datetime import date

import pytest

from crypto.ml.train import _persist_and_gate


# ──────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────


def _seed_old_model(conn, *, model_id: str, horizon: str, precision: float) -> None:
    """Insert a minimal active model row to serve as the prior baseline."""
    conn.execute(
        """
        INSERT INTO crypto_ml_model_runs (
            model_id, horizon, target_threshold,
            train_start, train_end, test_start, test_end,
            n_train_samples, n_test_samples, n_positive_train, n_positive_test,
            precision_at_threshold, recall_at_threshold, f1_score, auc_roc,
            base_rate, lift_over_base, feature_importance_json,
            model_path, is_active, promotion_status
        ) VALUES (?, ?, 0.10,
                  '2024-01-01', '2024-12-31', '2025-01-01', '2025-01-31',
                  1000, 100, 100, 10,
                  ?, 0.5, 0.4, 0.75,
                  0.15, 2.0, '{}',
                  '/tmp/old.joblib', true, 'promoted')
        """,
        [model_id, horizon, precision],
    )


def _make_fold_and_final(precision: float) -> tuple[dict, dict]:
    """Return a minimal (last_fold, final) pair for _persist_and_gate."""
    last_fold = {
        "train_end": "2024-12-31",
        "test_start": "2025-01-01",
        "test_end": "2025-01-31",
    }
    final = {
        "n_train": 500,
        "n_test": 100,
        "n_pos_train": 50,
        "n_pos_test": 10,
        "recall": 0.5,
        "f1": 0.4,
        "feature_importance": {},
    }
    return last_fold, final


# ──────────────────────────────────────────────────────────────────────
# 1. Promotion proceeds when gate passes
# ──────────────────────────────────────────────────────────────────────


def test_promotion_proceeds_when_gate_passes(temp_db, monkeypatch, tmp_path):
    """Old precision=0.50; new precision=0.50 → gate passes → new model promoted."""
    _seed_old_model(temp_db, model_id="old_model_10d", horizon="10d", precision=0.50)

    new_model_id = "new_model_10d_passes"
    last_fold, final = _make_fold_and_final(precision=0.50)

    alerts_sent = []
    monkeypatch.setattr(
        "crypto.ml.train.send_alert",
        lambda mr: alerts_sent.append(mr),
    )

    _persist_and_gate(
        conn=temp_db,
        model_id=new_model_id,
        horizon="10d",
        threshold=0.10,
        last_fold=last_fold,
        final=final,
        avg_auc=0.75,
        avg_lift=2.0,
        avg_precision=0.50,
        avg_base=0.15,
        model_path=str(tmp_path / "new.joblib"),
    )

    new_row = temp_db.execute(
        "SELECT is_active, promotion_status FROM crypto_ml_model_runs WHERE model_id = ?",
        [new_model_id],
    ).fetchone()
    assert new_row is not None, "new model row not inserted"
    assert new_row[0] is True, "new model should be is_active=true after gate pass"
    assert new_row[1] == "promoted", f"expected 'promoted', got {new_row[1]!r}"

    old_row = temp_db.execute(
        "SELECT is_active FROM crypto_ml_model_runs WHERE model_id = 'old_model_10d'",
    ).fetchone()
    assert old_row[0] is False, "old model should be is_active=false after new model promoted"

    assert len(alerts_sent) == 0, "no alert should fire on a successful promotion"


# ──────────────────────────────────────────────────────────────────────
# 2. Promotion blocked when hit rate drops
# ──────────────────────────────────────────────────────────────────────


def test_promotion_blocked_when_hit_rate_drops(temp_db, monkeypatch, tmp_path):
    """Old precision=0.50; new precision=0.30 (0.6× old, below 0.9× floor) → blocked."""
    _seed_old_model(temp_db, model_id="old_model_10d_strict", horizon="10d", precision=0.50)

    new_model_id = "new_model_10d_blocked"
    last_fold, final = _make_fold_and_final(precision=0.30)

    alerts_sent = []
    monkeypatch.setattr(
        "crypto.ml.train.send_alert",
        lambda mr: alerts_sent.append(mr),
    )

    _persist_and_gate(
        conn=temp_db,
        model_id=new_model_id,
        horizon="10d",
        threshold=0.10,
        last_fold=last_fold,
        final=final,
        avg_auc=0.65,
        avg_lift=1.5,
        avg_precision=0.30,
        avg_base=0.15,
        model_path=str(tmp_path / "blocked.joblib"),
    )

    new_row = temp_db.execute(
        "SELECT is_active, promotion_status FROM crypto_ml_model_runs WHERE model_id = ?",
        [new_model_id],
    ).fetchone()
    assert new_row is not None, "new model row not inserted"
    assert new_row[0] is False, "new model should remain is_active=false when blocked"
    assert new_row[1] == "promotion_blocked", f"expected 'promotion_blocked', got {new_row[1]!r}"

    old_row = temp_db.execute(
        "SELECT is_active FROM crypto_ml_model_runs WHERE model_id = 'old_model_10d_strict'",
    ).fetchone()
    assert old_row[0] is True, "old model must remain is_active=true when new model blocked"

    assert len(alerts_sent) == 1, "exactly one alert should fire on a blocked promotion"
    alert = alerts_sent[0]
    assert alert.severity == "critical", f"expected severity='critical', got {alert.severity!r}"
    assert new_model_id in alert.title, (
        f"alert title should contain model_id={new_model_id!r}, got {alert.title!r}"
    )


# ──────────────────────────────────────────────────────────────────────
# 3. First model promotes without baseline
# ──────────────────────────────────────────────────────────────────────


def test_first_model_promotes_without_baseline(temp_db, monkeypatch, tmp_path, caplog):
    """No prior active model → first_model_skip → new model promoted, no alert."""
    new_model_id = "new_model_10d_first"
    last_fold, final = _make_fold_and_final(precision=0.50)

    alerts_sent = []
    monkeypatch.setattr(
        "crypto.ml.train.send_alert",
        lambda mr: alerts_sent.append(mr),
    )

    with caplog.at_level(logging.INFO, logger="mhde.crypto.train"):
        _persist_and_gate(
            conn=temp_db,
            model_id=new_model_id,
            horizon="10d",
            threshold=0.10,
            last_fold=last_fold,
            final=final,
            avg_auc=0.75,
            avg_lift=2.0,
            avg_precision=0.50,
            avg_base=0.15,
            model_path=str(tmp_path / "first.joblib"),
        )

    new_row = temp_db.execute(
        "SELECT is_active, promotion_status FROM crypto_ml_model_runs WHERE model_id = ?",
        [new_model_id],
    ).fetchone()
    assert new_row is not None, "new model row not inserted"
    assert new_row[0] is True, "first model should be is_active=true"
    assert new_row[1] == "promoted", f"expected 'promoted', got {new_row[1]!r}"

    assert len(alerts_sent) == 0, "no alert should fire for first model"

    json_lines = [
        msg for msg in caplog.messages
        if msg.startswith("{") and '"event"' in msg
    ]
    assert len(json_lines) >= 1, "expected at least one structured JSON log line"
    log_obj = json.loads(json_lines[0])
    assert log_obj.get("reason") == "first_model_skip", (
        f"expected reason='first_model_skip', got {log_obj.get('reason')!r}"
    )


# ──────────────────────────────────────────────────────────────────────
# 4. Structured log emitted on every run (pass and fail)
# ──────────────────────────────────────────────────────────────────────


def test_structured_log_emitted_on_every_run(temp_db, monkeypatch, tmp_path, caplog):
    """One JSON line with event='retrain_validation' is emitted regardless of outcome."""
    monkeypatch.setattr("crypto.ml.train.send_alert", lambda mr: None)

    required_fields = {"event", "model_id", "horizon", "passed", "duration_sec",
                       "reason", "comparison"}

    for scenario_idx, (precision_old, precision_new, label) in enumerate([
        (0.50, 0.50, "pass"),
        (0.50, 0.30, "fail"),
    ]):
        model_id = f"log_test_model_{scenario_idx}"
        old_id = f"log_test_old_{scenario_idx}"

        _seed_old_model(temp_db, model_id=old_id, horizon="5d", precision=precision_old)

        last_fold, final = _make_fold_and_final(precision=precision_new)

        with caplog.at_level(logging.INFO, logger="mhde.crypto.train"):
            _persist_and_gate(
                conn=temp_db,
                model_id=model_id,
                horizon="5d",
                threshold=0.10,
                last_fold=last_fold,
                final=final,
                avg_auc=0.70,
                avg_lift=1.8,
                avg_precision=precision_new,
                avg_base=0.15,
                model_path=str(tmp_path / f"log_{scenario_idx}.joblib"),
            )

        json_lines = [
            msg for msg in caplog.messages
            if msg.startswith("{") and '"event": "retrain_validation"' in msg
            and f'"model_id": "{model_id}"' in msg
        ]
        assert len(json_lines) >= 1, (
            f"Expected structured log for scenario={label!r} (model_id={model_id!r})"
        )
        log_obj = json.loads(json_lines[0])
        missing = required_fields - set(log_obj.keys())
        assert not missing, (
            f"Structured log missing fields {missing} for scenario={label!r}"
        )
        assert log_obj["event"] == "retrain_validation"
        assert log_obj["model_id"] == model_id
        assert log_obj["horizon"] == "5d"
        assert isinstance(log_obj["passed"], bool)
        assert isinstance(log_obj["duration_sec"], float)

        if label == "pass":
            assert log_obj["passed"] is True
        else:
            assert log_obj["passed"] is False

        caplog.clear()
