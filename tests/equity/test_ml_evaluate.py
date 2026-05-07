"""Unit tests for ml/evaluate.py — print_walk_forward_results."""
from __future__ import annotations

from ml.evaluate import print_walk_forward_results


def _fold(fold_n=1, lift=1.5, auc=0.65, base=0.10):
    return {
        "fold": fold_n,
        "test_start": "2024-01-01",
        "test_end": "2024-03-31",
        "base_rate": base,
        "precision_top_20": 0.20,
        "precision_at_threshold": base * lift,
        "recall_at_threshold": 0.50,
        "lift_over_base": lift,
        "auc_roc": auc,
        "optimal_threshold": 0.55,
        "n_flagged": 30,
    }


def test_print_no_folds(capsys):
    print_walk_forward_results([], "label_20d_10pct", "20d")
    out = capsys.readouterr().out
    assert "WALK-FORWARD RESULTS" in out


def test_print_passes_lift_criterion(capsys):
    results = [_fold(1, lift=1.6), _fold(2, lift=1.8)]
    print_walk_forward_results(results, "label_20d_10pct", "20d")
    out = capsys.readouterr().out
    assert "Lift > 1.3 consistently" in out
    assert "PASS" in out


def test_print_fails_lift_criterion(capsys):
    """All folds with lift <= 1.3 → FAIL marker."""
    results = [_fold(1, lift=1.0), _fold(2, lift=1.1)]
    print_walk_forward_results(results, "label_20d_10pct", "20d")
    out = capsys.readouterr().out
    assert "FAIL" in out


def test_print_partial_when_average_passes_but_not_all(capsys):
    results = [_fold(1, lift=1.0), _fold(2, lift=1.8)]
    print_walk_forward_results(results, "label_20d_10pct", "20d")
    out = capsys.readouterr().out
    assert "PARTIAL" in out


def test_print_includes_final_model_block(capsys):
    final = {
        "final_model": {
            "model_id": "m_20d_10pct_test",
            "model_path": "/tmp/m.joblib",
            "avg_precision": 0.20,
            "avg_recall": 0.45,
            "avg_auc": 0.66,
            "avg_lift": 1.6,
            "feature_importance": {f"feat_{i}": 0.2 - i * 0.01 for i in range(15)},
        }
    }
    results = [_fold(1, lift=1.5), final]
    print_walk_forward_results(results, "label_20d_10pct", "20d")
    out = capsys.readouterr().out
    assert "FINAL MODEL: m_20d_10pct_test" in out
    assert "TOP 10 FEATURES" in out
    assert "feat_0" in out
