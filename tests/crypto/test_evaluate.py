"""Unit tests for crypto/ml/evaluate.py — print_walk_forward_results."""
from __future__ import annotations

from crypto.ml.evaluate import print_walk_forward_results


def test_print_walk_forward_results_no_valid_folds(capsys):
    print_walk_forward_results([{"error": "model failed"}], "label_5d_10pct", "5d")
    out = capsys.readouterr().out
    assert "No successful folds" in out


def test_print_walk_forward_results_empty(capsys):
    print_walk_forward_results([], "label_5d_10pct", "5d")
    out = capsys.readouterr().out
    assert "No successful folds" in out


def test_print_walk_forward_results_passes_threshold(capsys):
    results = [
        {"fold": 1, "n_train": 1000, "n_test": 200, "base_rate": 0.10,
         "precision_top": 0.18, "auc_roc": 0.65, "lift": 1.8},
        {"fold": 2, "n_train": 1200, "n_test": 200, "base_rate": 0.10,
         "precision_top": 0.20, "auc_roc": 0.66, "lift": 2.0},
    ]
    print_walk_forward_results(results, "label_5d_10pct", "5d")
    out = capsys.readouterr().out
    assert "PASS" in out
    assert "5d" in out
    assert "label_5d_10pct" in out


def test_print_walk_forward_results_fails_threshold(capsys):
    """avg_lift <= 1.3 → NEEDS REVIEW marker."""
    results = [
        {"fold": 1, "n_train": 1000, "n_test": 200, "base_rate": 0.10,
         "precision_top": 0.11, "auc_roc": 0.51, "lift": 1.1},
    ]
    print_walk_forward_results(results, "label_5d_10pct", "5d")
    out = capsys.readouterr().out
    assert "NEEDS REVIEW" in out


def test_print_walk_forward_results_includes_feature_importance(capsys):
    """If the last fold has feature_importance, top-10 are printed."""
    results = [
        {"fold": 1, "n_train": 1000, "n_test": 200, "base_rate": 0.10,
         "precision_top": 0.20, "auc_roc": 0.70, "lift": 2.0,
         "feature_importance": {f"feat_{i}": 0.1 - i * 0.005 for i in range(15)}},
    ]
    print_walk_forward_results(results, "label_10d_15pct", "10d")
    out = capsys.readouterr().out
    assert "Top 10 features" in out
    # Top feature has highest importance (feat_0)
    assert "feat_0" in out
