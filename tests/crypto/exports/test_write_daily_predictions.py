"""Tests for crypto.exports.write_daily_predictions.

Preflight:
  - staleness gate (MAX(trade_date) < today UTC → error)
  - coverage gate (any active universe symbol missing → error)
  - happy path (full coverage, today UTC → success)

Schema integration:
  - n_predictions == count(active universe)
  - ranks 1..N consecutive
  - probabilities sorted descending
  - all probabilities in [0, 1]
  - export_date matches prediction_date
  - symlink points at the dated file

Joblib bundle is mocked via monkeypatch (same pattern as
tests/crypto/test_predict.py).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from crypto.config import FEATURE_COLS
from crypto.exports import write_daily_predictions as wdp


# ──────────────────────────────────────────────────────────────────────
# Fixture helpers
# ──────────────────────────────────────────────────────────────────────


def _seed_universe(conn, symbols):
    for sym in symbols:
        conn.execute(
            "INSERT INTO crypto_universe (symbol, base_asset, is_active, "
            "rank_by_volume) VALUES (?, ?, true, 1)",
            [sym, sym.removesuffix("USDT")],
        )


def _seed_active_10d_model(conn, model_path="/tmp/fake.joblib"):
    conn.execute(
        "INSERT INTO crypto_ml_model_runs ("
        "  model_id, horizon, target_threshold, model_path, is_active"
        ") VALUES ('crypto_10d_test', '10d', 0.10, ?, true)",
        [model_path],
    )


def _seed_features(conn, symbols, trade_date):
    cols = ", ".join(FEATURE_COLS)
    placeholders = ", ".join(["?"] * len(FEATURE_COLS))
    for sym in symbols:
        conn.execute(
            f"INSERT INTO crypto_ml_features (symbol, trade_date, {cols}) "
            f"VALUES (?, ?, {placeholders})",
            [sym, trade_date] + [0.0] * len(FEATURE_COLS),
        )


def _mock_joblib_load(monkeypatch, probs_per_call):
    """Replace joblib.load to return a bundle whose model returns the
    given probabilities (list of floats, in feature-row order)."""
    fake_model = MagicMock()
    arr = np.array([[1 - p, p] for p in probs_per_call])
    fake_model.predict_proba = lambda X: arr
    fake_platt = MagicMock()
    fake_platt.predict_proba = lambda raw: arr
    monkeypatch.setattr(
        wdp.joblib, "load",
        lambda path: {"model": fake_model, "platt": fake_platt, "medians": {}},
    )


# ──────────────────────────────────────────────────────────────────────
# Preflight tests
# ──────────────────────────────────────────────────────────────────────


def test_preflight_fails_when_features_stale(temp_db):
    """MAX(trade_date) = yesterday → ExportPreflightError('stale')."""
    today = date(2026, 5, 10)
    yesterday = date(2026, 5, 9)
    _seed_universe(temp_db, ["BTCUSDT", "ETHUSDT"])
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, ["BTCUSDT", "ETHUSDT"], yesterday)

    with pytest.raises(wdp.ExportPreflightError, match="stale"):
        wdp.build_predictions(temp_db, prediction_date=today)


def test_preflight_fails_when_features_missing_for_symbol(temp_db, monkeypatch):
    today = date(2026, 5, 10)
    _seed_universe(temp_db, ["BTCUSDT", "ETHUSDT"])
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, ["BTCUSDT"], today)  # ETHUSDT missing

    with pytest.raises(wdp.ExportPreflightError, match="ETHUSDT"):
        wdp.build_predictions(temp_db, prediction_date=today)


def test_preflight_passes_with_full_today_coverage(temp_db, monkeypatch):
    today = date(2026, 5, 10)
    _seed_universe(temp_db, ["BTCUSDT", "ETHUSDT"])
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, ["BTCUSDT", "ETHUSDT"], today)
    _mock_joblib_load(monkeypatch, [0.7, 0.6])

    out = wdp.build_predictions(temp_db, prediction_date=today)
    assert out["n_predictions"] == 2


# ──────────────────────────────────────────────────────────────────────
# Schema tests (full coverage, mocked model)
# ──────────────────────────────────────────────────────────────────────


def test_predictions_full_universe_ranked(temp_db, monkeypatch):
    today = date(2026, 5, 10)
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
    _seed_universe(temp_db, syms)
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, syms, today)
    _mock_joblib_load(monkeypatch, [0.50, 0.91, 0.30, 0.75, 0.10])

    out = wdp.build_predictions(temp_db, prediction_date=today)

    assert out["export_date"] == today.isoformat()
    assert out["n_predictions"] == 5
    assert out["model_id"] == "crypto_10d_test"
    assert out["horizon_days"] == 10

    preds = out["predictions"]
    assert len(preds) == 5
    # Sorted descending by probability
    probs = [p["probability"] for p in preds]
    assert probs == sorted(probs, reverse=True)
    # Ranks consecutive 1..5
    assert [p["rank"] for p in preds] == [1, 2, 3, 4, 5]
    # Top is ETHUSDT @ 0.91
    assert preds[0]["symbol"] == "ETHUSDT"
    assert preds[0]["probability"] == pytest.approx(0.91)
    # All probabilities in [0, 1]
    for p in preds:
        assert 0.0 <= p["probability"] <= 1.0


def test_predicted_at_is_utc_iso8601(temp_db, monkeypatch):
    today = date(2026, 5, 10)
    _seed_universe(temp_db, ["BTCUSDT"])
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, ["BTCUSDT"], today)
    _mock_joblib_load(monkeypatch, [0.7])

    out = wdp.build_predictions(temp_db, prediction_date=today)
    pa = out["predictions"][0]["predicted_at"]
    # Engine validation per INTERFACE.md §3.1: ISO 8601 UTC. Accept
    # both '...Z' and '+00:00' forms.
    assert "T" in pa
    assert pa.endswith("Z") or pa.endswith("+00:00")


def test_generated_at_is_utc_iso8601(temp_db, monkeypatch):
    today = date(2026, 5, 10)
    _seed_universe(temp_db, ["BTCUSDT"])
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, ["BTCUSDT"], today)
    _mock_joblib_load(monkeypatch, [0.7])

    out = wdp.build_predictions(temp_db, prediction_date=today)
    g = out["generated_at"]
    assert "T" in g and (g.endswith("Z") or g.endswith("+00:00"))


# ──────────────────────────────────────────────────────────────────────
# write() — dated file + symlink
# ──────────────────────────────────────────────────────────────────────


def test_write_creates_dated_file_and_symlink(temp_db, monkeypatch, tmp_path):
    today = date(2026, 5, 10)
    syms = ["BTCUSDT", "ETHUSDT"]
    _seed_universe(temp_db, syms)
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, syms, today)
    _mock_joblib_load(monkeypatch, [0.7, 0.5])

    wdp.write(temp_db, prediction_date=today, output_dir=tmp_path)

    dated = tmp_path / "predictions_2026-05-10.json"
    latest = tmp_path / "predictions_latest.json"
    assert dated.exists()
    assert latest.is_symlink()
    assert latest.readlink() == Path("predictions_2026-05-10.json")

    payload = json.loads(dated.read_text())
    assert payload["n_predictions"] == 2


def test_write_replaces_existing_symlink_silently(
    temp_db, monkeypatch, tmp_path
):
    today = date(2026, 5, 10)
    yesterday = date(2026, 5, 9)
    syms = ["BTCUSDT"]
    _seed_universe(temp_db, syms)
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, syms, yesterday)
    _mock_joblib_load(monkeypatch, [0.7])

    wdp.write(temp_db, prediction_date=yesterday, output_dir=tmp_path)
    yesterday_file = tmp_path / "predictions_2026-05-09.json"
    assert yesterday_file.exists()
    latest = tmp_path / "predictions_latest.json"
    assert latest.readlink() == Path("predictions_2026-05-09.json")

    # Today's run replaces the symlink
    _seed_features(temp_db, syms, today)
    _mock_joblib_load(monkeypatch, [0.8])
    wdp.write(temp_db, prediction_date=today, output_dir=tmp_path)

    assert latest.readlink() == Path("predictions_2026-05-10.json")
    # Yesterday's dated file is still there (the old dated file is NOT
    # deleted; only the symlink is replaced).
    assert yesterday_file.exists()


def test_write_dry_run_does_not_create_files(
    temp_db, monkeypatch, tmp_path, capsys
):
    today = date(2026, 5, 10)
    _seed_universe(temp_db, ["BTCUSDT"])
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, ["BTCUSDT"], today)
    _mock_joblib_load(monkeypatch, [0.7])

    wdp.write(temp_db, prediction_date=today, output_dir=tmp_path, dry_run=True)

    assert not (tmp_path / "predictions_2026-05-10.json").exists()
    assert not (tmp_path / "predictions_latest.json").exists()
    captured = capsys.readouterr()
    assert "BTCUSDT" in captured.out


def test_write_does_not_touch_files_on_preflight_failure(
    temp_db, monkeypatch, tmp_path
):
    """Stale features → ExportPreflightError → no file written, no
    symlink modified. Pre-existing symlink (yesterday's) is intact."""
    today = date(2026, 5, 10)
    yesterday = date(2026, 5, 9)
    syms = ["BTCUSDT"]
    _seed_universe(temp_db, syms)
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, syms, yesterday)
    _mock_joblib_load(monkeypatch, [0.7])

    # Day 1: write yesterday's file
    wdp.write(temp_db, prediction_date=yesterday, output_dir=tmp_path)
    yesterday_file = tmp_path / "predictions_2026-05-09.json"
    latest = tmp_path / "predictions_latest.json"
    assert yesterday_file.exists()
    assert latest.readlink() == Path("predictions_2026-05-09.json")

    # Day 2: try to write today's file but features are stale
    with pytest.raises(wdp.ExportPreflightError, match="stale"):
        wdp.write(temp_db, prediction_date=today, output_dir=tmp_path)

    # Symlink unchanged, no today-file written
    assert latest.readlink() == Path("predictions_2026-05-09.json")
    assert not (tmp_path / "predictions_2026-05-10.json").exists()


def test_build_raises_when_no_active_10d_model(temp_db):
    today = date(2026, 5, 10)
    _seed_universe(temp_db, ["BTCUSDT"])
    _seed_features(temp_db, ["BTCUSDT"], today)
    # No active model

    with pytest.raises(wdp.ExportPreflightError, match="active 10d model"):
        wdp.build_predictions(temp_db, prediction_date=today)
