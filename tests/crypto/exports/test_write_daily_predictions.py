"""Tests for crypto.exports.write_daily_predictions.

Preflight (one gate, staleness-only after KI-129 correction; tolerance
widened to today-or-today-1 after KI-138 / the cap-at-today-1 ingestion
fix in commit 8f9d707):
  - staleness gate accepts MAX(trade_date) == export_date
  - staleness gate accepts MAX(trade_date) == export_date - 1 (the
    structural normal under cap-at-today-1 ingestion)
  - staleness gate still fails MAX(trade_date) == export_date - 2
  - warmup-window symbols silently absent (no error, just smaller n)

Schema integration:
  - n_predictions == count(active universe ∩ has-features-on-features-date)
  - ranks 1..N consecutive
  - probabilities sorted descending
  - all probabilities in [0, 1]
  - export_date == today UTC (the date the predictions drive trades)
  - features_as_of_date == MAX(trade_date) used for inference
  - symlink points at the dated file

Joblib bundle is mocked via monkeypatch (same pattern as
tests/crypto/test_predict.py).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
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


def test_preflight_fails_when_features_two_days_stale(temp_db):
    """MAX(trade_date) == export_date - 2 → ExportPreflightError('stale').

    today-1 is the structural normal under the cap-at-today-1 ingestion
    fix (commit 8f9d707), so the gate must only fire once features fall
    *behind* that — genuine pipeline staleness.
    """
    today = date(2026, 5, 10)
    two_days_ago = date(2026, 5, 8)
    _seed_universe(temp_db, ["BTCUSDT", "ETHUSDT"])
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, ["BTCUSDT", "ETHUSDT"], two_days_ago)

    with pytest.raises(wdp.ExportPreflightError, match="stale"):
        wdp.build_predictions(temp_db, prediction_date=today)


def test_preflight_accepts_features_one_day_old(temp_db, monkeypatch):
    """MAX(trade_date) == export_date - 1 → success (cap-at-today-1
    ingestion semantics; KI-138). Inference runs on yesterday's closed
    candles; the export still drives *today's* trades."""
    today = date(2026, 5, 10)
    yesterday = date(2026, 5, 9)
    _seed_universe(temp_db, ["BTCUSDT", "ETHUSDT"])
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, ["BTCUSDT", "ETHUSDT"], yesterday)
    _mock_joblib_load(monkeypatch, [0.7, 0.6])

    out = wdp.build_predictions(temp_db, prediction_date=today)
    assert out["n_predictions"] == 2
    assert out["export_date"] == today.isoformat()
    assert out["features_as_of_date"] == yesterday.isoformat()


def test_export_date_is_today_utc_and_features_as_of_is_yesterday(
    temp_db, monkeypatch
):
    """No prediction_date passed → export_date == today UTC; under the
    cap-at-today-1 ingestion fix features_as_of_date == today - 1."""
    today = date(2026, 5, 10)
    yesterday = date(2026, 5, 9)
    monkeypatch.setattr(wdp, "_today_utc", lambda: today)
    _seed_universe(temp_db, ["BTCUSDT", "ETHUSDT"])
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, ["BTCUSDT", "ETHUSDT"], yesterday)
    _mock_joblib_load(monkeypatch, [0.7, 0.6])

    out = wdp.build_predictions(temp_db)
    assert out["export_date"] == today.isoformat()
    assert out["features_as_of_date"] == yesterday.isoformat()


def test_features_as_of_date_equals_max_trade_date_when_same_day(
    temp_db, monkeypatch
):
    """When the day's candle is already ingested by export time
    (e.g. a manual late-UTC re-run), features_as_of_date == export_date."""
    today = date(2026, 5, 10)
    _seed_universe(temp_db, ["BTCUSDT", "ETHUSDT"])
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, ["BTCUSDT", "ETHUSDT"], today)
    _mock_joblib_load(monkeypatch, [0.7, 0.6])

    out = wdp.build_predictions(temp_db, prediction_date=today)
    assert out["export_date"] == today.isoformat()
    assert out["features_as_of_date"] == today.isoformat()


def test_warmup_symbols_silently_absent_from_output(temp_db, monkeypatch):
    """Universe symbol with no features (warmup window) is silently
    absent from the output. Pins KI-129 corrected semantics: stale
    pipeline ≠ warmup symbols."""
    today = date(2026, 5, 10)
    _seed_universe(temp_db, ["BTCUSDT", "ETHUSDT", "NEWCOIN1USDT"])
    _seed_active_10d_model(temp_db)
    # NEWCOIN1USDT has no features (warmup window simulation)
    _seed_features(temp_db, ["BTCUSDT", "ETHUSDT"], today)
    _mock_joblib_load(monkeypatch, [0.7, 0.5])

    out = wdp.build_predictions(temp_db, prediction_date=today)
    assert out["n_predictions"] == 2
    assert {p["symbol"] for p in out["predictions"]} == {"BTCUSDT", "ETHUSDT"}


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
    # _load_features ORDER BY symbol returns rows alphabetical:
    # BNBUSDT, BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT.
    # Per-symbol probs: BNB=0.75, BTC=0.50, ETH=0.91, SOL=0.30, XRP=0.10
    _mock_joblib_load(monkeypatch, [0.75, 0.50, 0.91, 0.30, 0.10])

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
    """Stale features (≥2 days behind) → ExportPreflightError → no file
    written, no symlink modified. Pre-existing symlink is intact."""
    day0 = date(2026, 5, 9)
    day2 = date(2026, 5, 11)  # two days ahead of the only features we have
    syms = ["BTCUSDT"]
    _seed_universe(temp_db, syms)
    _seed_active_10d_model(temp_db)
    _seed_features(temp_db, syms, day0)
    _mock_joblib_load(monkeypatch, [0.7])

    # Day 0: write that day's file (features == export_date)
    wdp.write(temp_db, prediction_date=day0, output_dir=tmp_path)
    day0_file = tmp_path / "predictions_2026-05-09.json"
    latest = tmp_path / "predictions_latest.json"
    assert day0_file.exists()
    assert latest.readlink() == Path("predictions_2026-05-09.json")

    # Day 2: try to write — features are now 2 days stale
    with pytest.raises(wdp.ExportPreflightError, match="stale"):
        wdp.write(temp_db, prediction_date=day2, output_dir=tmp_path)

    # Symlink unchanged, no day-2 file written
    assert latest.readlink() == Path("predictions_2026-05-09.json")
    assert not (tmp_path / "predictions_2026-05-11.json").exists()


def test_build_raises_when_no_active_10d_model(temp_db):
    today = date(2026, 5, 10)
    _seed_universe(temp_db, ["BTCUSDT"])
    _seed_features(temp_db, ["BTCUSDT"], today)
    # No active model

    with pytest.raises(wdp.ExportPreflightError, match="active 10d model"):
        wdp.build_predictions(temp_db, prediction_date=today)


# ──────────────────────────────────────────────────────────────────────
# Post-parabolic exclusion filter (option (b), wired into build_predictions)
# ──────────────────────────────────────────────────────────────────────


def _seed_features_overrides(conn, trade_date, per_symbol_overrides):
    """Seed crypto_ml_features rows. per_symbol_overrides maps
    symbol -> {feature_name: value}; unspecified features default to 0.0."""
    cols = ", ".join(FEATURE_COLS)
    placeholders = ", ".join(["?"] * len(FEATURE_COLS))
    for sym, overrides in per_symbol_overrides.items():
        vals = [overrides.get(c, 0.0) for c in FEATURE_COLS]
        conn.execute(
            f"INSERT INTO crypto_ml_features (symbol, trade_date, {cols}) "
            f"VALUES (?, ?, {placeholders})",
            [sym, trade_date] + vals,
        )


def test_postparabolic_symbol_dropped_from_output(temp_db, monkeypatch):
    today = date(2026, 5, 10)
    syms = ["AUSDT", "BUSDT"]  # alphabetical: AUSDT, BUSDT
    _seed_universe(temp_db, syms)
    _seed_active_10d_model(temp_db)
    _seed_features_overrides(temp_db, today, {
        "AUSDT": {},  # clean
        "BUSDT": {"drawdown_from_90d_high": -0.25, "return_60d": 3.0},  # post-parabolic
    })
    _mock_joblib_load(monkeypatch, [0.60, 0.95])  # AUSDT=0.60, BUSDT=0.95

    out = wdp.build_predictions(temp_db, prediction_date=today)

    assert out["n_predictions"] == 1
    assert {p["symbol"] for p in out["predictions"]} == {"AUSDT"}
    # the high-prob post-parabolic coin must not have leaked through
    assert "BUSDT" not in {p["symbol"] for p in out["predictions"]}


def test_postparabolic_exclusion_row_written(temp_db, monkeypatch):
    today = date(2026, 5, 10)
    syms = ["AUSDT", "BUSDT"]
    _seed_universe(temp_db, syms)
    _seed_active_10d_model(temp_db)
    _seed_features_overrides(temp_db, today, {
        "AUSDT": {},
        "BUSDT": {"drawdown_from_90d_high": -0.25, "return_60d": 3.0},
    })
    _mock_joblib_load(monkeypatch, [0.60, 0.95])

    wdp.build_predictions(temp_db, prediction_date=today)

    rows = temp_db.execute(
        "SELECT export_date, symbol, model_id, raw_probability, dd90, ret60, reason "
        "FROM crypto_signal_exclusions ORDER BY symbol"
    ).fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r[0] == today
    assert r[1] == "BUSDT"
    assert r[2] == "crypto_10d_test"
    assert r[3] == pytest.approx(0.95)
    assert r[4] == pytest.approx(-0.25)
    assert r[5] == pytest.approx(3.0)
    assert isinstance(r[6], str) and r[6]


def test_postparabolic_exclusion_upsert_idempotent(temp_db, monkeypatch):
    today = date(2026, 5, 10)
    _seed_universe(temp_db, ["BUSDT"])
    _seed_active_10d_model(temp_db)
    _seed_features_overrides(temp_db, today, {
        "BUSDT": {"drawdown_from_90d_high": -0.25, "return_60d": 3.0},
    })
    _mock_joblib_load(monkeypatch, [0.95])

    wdp.build_predictions(temp_db, prediction_date=today)
    wdp.build_predictions(temp_db, prediction_date=today)  # re-run

    n = temp_db.execute("SELECT COUNT(*) FROM crypto_signal_exclusions").fetchone()[0]
    assert n == 1  # UPSERT, not duplicate


def test_rerank_consecutive_after_exclusion(temp_db, monkeypatch):
    today = date(2026, 5, 10)
    syms = ["AUSDT", "BUSDT", "CUSDT", "DUSDT", "EUSDT"]  # alphabetical order
    _seed_universe(temp_db, syms)
    _seed_active_10d_model(temp_db)
    _seed_features_overrides(temp_db, today, {
        "AUSDT": {},
        "BUSDT": {},
        # CUSDT is post-parabolic and would otherwise be ranked #1 (highest prob)
        "CUSDT": {"drawdown_from_90d_high": -0.30, "return_60d": 4.0},
        "DUSDT": {},
        "EUSDT": {},
    })
    # probs in alphabetical row order: A=0.50, B=0.60, C=0.99, D=0.40, E=0.30
    _mock_joblib_load(monkeypatch, [0.50, 0.60, 0.99, 0.40, 0.30])

    out = wdp.build_predictions(temp_db, prediction_date=today)

    assert out["n_predictions"] == 4
    preds = out["predictions"]
    assert [p["symbol"] for p in preds] == ["BUSDT", "AUSDT", "DUSDT", "EUSDT"]
    assert [p["rank"] for p in preds] == [1, 2, 3, 4]  # consecutive, no gap
    probs = [p["probability"] for p in preds]
    assert probs == sorted(probs, reverse=True)


def test_all_excluded_empty_list_handled_gracefully(temp_db, monkeypatch):
    today = date(2026, 5, 10)
    syms = ["AUSDT", "BUSDT"]
    _seed_universe(temp_db, syms)
    _seed_active_10d_model(temp_db)
    _seed_features_overrides(temp_db, today, {
        "AUSDT": {"drawdown_from_90d_high": -0.25, "return_60d": 3.0},
        "BUSDT": {"drawdown_from_90d_high": -0.40, "return_60d": 5.0},
    })
    _mock_joblib_load(monkeypatch, [0.80, 0.90])

    out = wdp.build_predictions(temp_db, prediction_date=today)  # must NOT raise
    assert out["n_predictions"] == 0
    assert out["predictions"] == []
    # both got logged to the exclusions table
    assert temp_db.execute("SELECT COUNT(*) FROM crypto_signal_exclusions").fetchone()[0] == 2


def test_missing_features_not_excluded_fail_open(temp_db, monkeypatch):
    """A symbol with NULL drawdown_from_90d_high / return_60d (warmup) is
    NOT excluded — fail-open."""
    today = date(2026, 5, 10)
    _seed_universe(temp_db, ["AUSDT"])
    _seed_active_10d_model(temp_db)
    cols = ", ".join(FEATURE_COLS)
    placeholders = ", ".join(["?"] * len(FEATURE_COLS))
    # NULLs for dd90 / ret60 specifically
    idx_dd = FEATURE_COLS.index("drawdown_from_90d_high")
    idx_ret = FEATURE_COLS.index("return_60d")
    vals = [0.0] * len(FEATURE_COLS)
    vals[idx_dd] = None
    vals[idx_ret] = None
    temp_db.execute(
        f"INSERT INTO crypto_ml_features (symbol, trade_date, {cols}) "
        f"VALUES (?, ?, {placeholders})", ["AUSDT", today] + vals,
    )
    _mock_joblib_load(monkeypatch, [0.80])

    out = wdp.build_predictions(temp_db, prediction_date=today)
    assert out["n_predictions"] == 1
    assert temp_db.execute("SELECT COUNT(*) FROM crypto_signal_exclusions").fetchone()[0] == 0
