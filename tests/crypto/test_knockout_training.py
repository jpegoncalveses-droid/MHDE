"""Tests for knockout-label training (phase 2): the --label-kind option,
training on label_Nd_knockout, model_id encoding, label_kind persistence,
and the no-auto-promote path. See crypto/ml/KNOCKOUT_LABEL_SPEC.md.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from crypto.config import FEATURE_COLS
from crypto.ml.train import _persist_and_gate, train_walk_forward


# ──────────────────────────────────────────────────────────────────────
# Synthetic dataset (features + knockout labels) for a 2-fold walk-forward
# ──────────────────────────────────────────────────────────────────────


def _seed_features_and_knockout_labels(conn, *, symbols, start=date(2024, 1, 1),
                                       num_days=240, pos_rate=0.30, seed=7):
    rng = random.Random(seed)
    fcols = ", ".join(FEATURE_COLS)
    fph = ", ".join(["?"] * len(FEATURE_COLS))
    feat_rows, label_rows = [], []
    for sym in symbols:
        for i in range(num_days):
            d = start + timedelta(days=i)
            feat_rows.append([sym, d] + [rng.gauss(0, 1) for _ in FEATURE_COLS])
            l5 = rng.random() < pos_rate
            l10 = rng.random() < pos_rate
            label_rows.append([sym, d, 100.0,
                               l5, ("tp" if l5 else "sl"), (1 if l5 else None),
                               l10, ("tp" if l10 else "sl"), (1 if l10 else None)])
    conn.executemany(
        f"INSERT INTO crypto_ml_features (symbol, trade_date, {fcols}) VALUES (?, ?, {fph})",
        feat_rows,
    )
    conn.executemany(
        "INSERT INTO crypto_ml_labels (symbol, trade_date, close_price, "
        "label_5d_knockout, knockout_outcome_5d, knockout_resolve_day_5d, "
        "label_10d_knockout, knockout_outcome_10d, knockout_resolve_day_10d) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        label_rows,
    )


# ──────────────────────────────────────────────────────────────────────
# 1. CLI option wiring
# ──────────────────────────────────────────────────────────────────────


def test_train_cli_has_label_kind_option_default_legacy():
    from main import crypto_train_cmd  # the click Command

    opt = next((p for p in crypto_train_cmd.params if p.name == "label_kind"), None)
    assert opt is not None, "--label-kind option missing on `crypto train`"
    assert opt.default == "legacy"
    # Choice constrained to {legacy, knockout}
    assert set(getattr(opt.type, "choices", [])) == {"legacy", "knockout"}


# ──────────────────────────────────────────────────────────────────────
# 2-4. train_walk_forward(label_kind="knockout") — right label col, model_id
#      encoding, label_kind persisted, is_active stays false, no promote.
# ──────────────────────────────────────────────────────────────────────


def test_train_walk_forward_knockout_persists_correctly(temp_db, tmp_path, monkeypatch):
    import crypto.config as cfg
    monkeypatch.setattr(cfg, "MODELS_DIR", str(tmp_path))
    import crypto.ml.train as train_mod
    monkeypatch.setattr(train_mod, "MODELS_DIR", str(tmp_path))
    # validate_promotion must NOT be called on the no-auto-promote path
    called = {"validate": 0}
    monkeypatch.setattr(train_mod, "validate_promotion",
                        lambda *a, **k: called.__setitem__("validate", called["validate"] + 1) or _fail_if_called())

    _seed_features_and_knockout_labels(temp_db, symbols=[f"S{i}USDT" for i in range(8)])

    results = train_walk_forward(temp_db, label_col="label_10d_knockout", horizon="10d",
                                 threshold=0.10, label_kind="knockout", auto_promote=False)
    assert results and any("error" not in r for r in results)

    row = temp_db.execute(
        "SELECT model_id, horizon, is_active, promotion_status, label_kind, target_threshold "
        "FROM crypto_ml_model_runs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    model_id, hz, is_active, promo, label_kind, thr = row
    assert model_id.startswith("crypto_10d_knockout_")          # model_id encodes the label kind
    assert hz == "10d"
    assert is_active is False                                    # NOT auto-promoted
    assert promo == "pending"
    assert label_kind == "knockout"                              # crypto_ml_model_runs.label_kind populated
    assert called["validate"] == 0                               # validation gate not invoked

    # the joblib bundle records the label kind + knockout params
    import glob, joblib
    bundles = glob.glob(str(tmp_path / "*.joblib"))
    assert bundles
    b = joblib.load(sorted(bundles)[-1])
    assert b["label_col"] == "label_10d_knockout"               # trained on the right column
    assert b["label_kind"] == "knockout"
    assert b["knockout_tp"] == 0.10 and b["knockout_sl"] == -0.05


def _fail_if_called():
    raise AssertionError("validate_promotion must not be called on the no-auto-promote path")


def test_train_walk_forward_legacy_unchanged(temp_db, tmp_path, monkeypatch):
    """A legacy run still auto-gates (validate_promotion called) and the
    model row gets label_kind='legacy'."""
    import crypto.ml.train as train_mod
    monkeypatch.setattr(train_mod, "MODELS_DIR", str(tmp_path))

    class _GatePassed:
        passed = True
        reason = "bootstrap"
        duration_sec = 0.0
        comparison = {}
    monkeypatch.setattr(train_mod, "validate_promotion", lambda *a, **k: _GatePassed())

    # legacy label column on the labels table
    rng = random.Random(3)
    fcols = ", ".join(FEATURE_COLS)
    fph = ", ".join(["?"] * len(FEATURE_COLS))
    syms = [f"L{i}USDT" for i in range(8)]
    fr, lr = [], []
    for sym in syms:
        for i in range(240):
            d = date(2024, 1, 1) + timedelta(days=i)
            fr.append([sym, d] + [rng.gauss(0, 1) for _ in FEATURE_COLS])
            lr.append([sym, d, 100.0, rng.random() < 0.3])
    temp_db.executemany(f"INSERT INTO crypto_ml_features (symbol, trade_date, {fcols}) VALUES (?, ?, {fph})", fr)
    temp_db.executemany("INSERT INTO crypto_ml_labels (symbol, trade_date, close_price, label_10d_10pct) VALUES (?, ?, ?, ?)", lr)

    train_walk_forward(temp_db, label_col="label_10d_10pct", horizon="10d", threshold=0.10)  # defaults: legacy, auto_promote
    row = temp_db.execute(
        "SELECT model_id, label_kind, is_active, promotion_status FROM crypto_ml_model_runs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row[0].startswith("crypto_10d_") and "knockout" not in row[0]
    assert row[1] == "legacy"
    assert row[2] is True and row[3] == "promoted"   # gate passed -> promoted (existing behavior)


# ──────────────────────────────────────────────────────────────────────
# 5. _persist_and_gate(auto_promote=False) — INSERT only, no gate, no promote
# ──────────────────────────────────────────────────────────────────────


def test_persist_and_gate_no_auto_promote(temp_db, monkeypatch):
    import crypto.ml.train as train_mod
    monkeypatch.setattr(train_mod, "validate_promotion",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gate must not run")))
    last_fold = {"train_end": "2024-12-31", "test_start": "2025-01-01", "test_end": "2025-01-31"}
    final = {"n_train": 500, "n_test": 100, "n_pos_train": 50, "n_pos_test": 10,
             "recall": 0.5, "f1": 0.4, "feature_importance": {}}
    _persist_and_gate(conn=temp_db, model_id="crypto_5d_knockout_deadbeef", horizon="5d",
                      threshold=0.10, last_fold=last_fold, final=final,
                      avg_auc=0.7, avg_lift=2.0, avg_precision=0.45, avg_base=0.22,
                      model_path="/tmp/x.joblib", label_kind="knockout", auto_promote=False)
    row = temp_db.execute(
        "SELECT is_active, promotion_status, label_kind FROM crypto_ml_model_runs WHERE model_id='crypto_5d_knockout_deadbeef'"
    ).fetchone()
    assert row == (False, "pending", "knockout")


# ──────────────────────────────────────────────────────────────────────
# 6. Harness: model_id_like param selects the right walkfold set
# ──────────────────────────────────────────────────────────────────────


def test_harness_load_oos_predictions_model_id_like(temp_db):
    from crypto.execution.backtest.harness import load_oos_predictions, MIN_FUNDING_DATA_DATE

    for mid in ("crypto_10d_walkfold_2025_06", "crypto_10d_kowf_2025_06"):
        temp_db.execute(
            "INSERT INTO crypto_ml_predictions (symbol, prediction_date, model_id, horizon, "
            "predicted_probability, prediction_threshold, market_cap_bucket) "
            "VALUES ('BTCUSDT', ?, ?, '10d', 0.7, 0.10, 'unknown')",
            [MIN_FUNDING_DATA_DATE, mid],
        )
    # default pattern -> only the legacy walkfold row
    df_default = load_oos_predictions(temp_db, "10d")
    assert set(df_default["model_id"]) == {"crypto_10d_walkfold_2025_06"}
    # knockout pattern -> only the kowf row
    df_ko = load_oos_predictions(temp_db, "10d", model_id_like="crypto_%_kowf_%")
    assert set(df_ko["model_id"]) == {"crypto_10d_kowf_2025_06"}
