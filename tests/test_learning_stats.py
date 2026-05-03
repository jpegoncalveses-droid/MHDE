"""Tests for dashboard learning stats service."""
from __future__ import annotations

import csv
import pytest


def _write_rows_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def test_learning_stats_zeros_when_no_files(tmp_path):
    from dashboard.services.learning_stats import get_learning_stats
    stats = get_learning_stats(str(tmp_path))
    assert stats["total"] == 0
    assert stats["true_miss"] == 0
    assert stats["near_threshold"] == 0
    assert stats["report_date"] == ""
    assert stats["top_rc_group"] == ""


def test_learning_stats_reads_rows_csv(tmp_path):
    from dashboard.services.learning_stats import get_learning_stats
    _write_rows_csv(str(tmp_path / "prediction_vs_actual_rows.csv"), [
        {"classification": "true_miss",       "event_date": "2026-05-01", "ticker": "AAPL"},
        {"classification": "near_threshold",   "event_date": "2026-05-01", "ticker": "GOOGL"},
        {"classification": "scored_correct",   "event_date": "2026-05-01", "ticker": "MSFT"},
        {"classification": "unscored_mover",   "event_date": "2026-05-01", "ticker": "NVDA"},
    ])
    stats = get_learning_stats(str(tmp_path))
    assert stats["total"] == 4
    assert stats["true_miss"] == 1
    assert stats["near_threshold"] == 1
    assert stats["scored_correct"] == 1
    assert stats["unscored_mover"] == 1
    assert stats["report_date"] == "2026-05-01"


def test_learning_stats_reads_enriched_csv(tmp_path):
    from dashboard.services.learning_stats import get_learning_stats
    _write_rows_csv(str(tmp_path / "prediction_vs_actual_enriched_rows.csv"), [
        {"root_cause_group": "data_gap",    "ticker": "AAPL"},
        {"root_cause_group": "data_gap",    "ticker": "MSFT"},
        {"root_cause_group": "feature_gap", "ticker": "GOOGL"},
    ])
    stats = get_learning_stats(str(tmp_path))
    assert stats["rc_groups"]["data_gap"] == 2
    assert stats["rc_groups"]["feature_gap"] == 1
    assert stats["top_rc_group"] == "data_gap"


def test_learning_stats_top_rc_group_is_largest(tmp_path):
    from dashboard.services.learning_stats import get_learning_stats
    _write_rows_csv(str(tmp_path / "prediction_vs_actual_enriched_rows.csv"), [
        {"root_cause_group": "scoring_gap", "ticker": "A"},
        {"root_cause_group": "scoring_gap", "ticker": "B"},
        {"root_cause_group": "scoring_gap", "ticker": "C"},
        {"root_cause_group": "data_gap",    "ticker": "D"},
    ])
    stats = get_learning_stats(str(tmp_path))
    assert stats["top_rc_group"] == "scoring_gap"


def test_learning_stats_handles_partial_files(tmp_path):
    """Rows CSV present but enriched CSV absent — no crash, rc_groups all zero."""
    from dashboard.services.learning_stats import get_learning_stats
    _write_rows_csv(str(tmp_path / "prediction_vs_actual_rows.csv"), [
        {"classification": "true_miss", "event_date": "2026-05-01", "ticker": "AAPL"},
    ])
    stats = get_learning_stats(str(tmp_path))
    assert stats["total"] == 1
    assert stats["true_miss"] == 1
    assert all(v == 0 for v in stats["rc_groups"].values())
    assert stats["top_rc_group"] == ""
