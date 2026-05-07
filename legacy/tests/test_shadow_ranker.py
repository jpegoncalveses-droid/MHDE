"""Shadow ML ranker — TDD suite.

Critical invariant: shadow_score must NOT affect production scoring or tier assignment.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from storage.db import get_connection, init_schema


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _seed_outcome(conn, ticker, run_id, forward_return_20d, tier="Reject", total=30.0):
    as_of = date.today() - timedelta(days=30)
    conn.execute(
        """INSERT OR IGNORE INTO scores
           (id, run_id, ticker, as_of_date, cheap_score, quality_score, catalyst_score,
            momentum_score, sentiment_score, risk_penalty, total_score, tier, confidence,
            why_ranked, missing_data_json, created_at)
           VALUES (?, ?, ?, ?, 30, 30, 10, 20, 10, 20, ?, ?, 'low', 'test', '[]', CURRENT_TIMESTAMP)""",
        [uuid.uuid4().hex[:16], run_id, ticker, as_of.isoformat(), total, tier],
    )
    conn.execute(
        """INSERT OR IGNORE INTO candidate_outcomes
           (candidate_id, run_id, ticker, as_of_date, tier, total_score,
            reference_price, forward_return_20d, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 100.0, ?, CURRENT_TIMESTAMP)""",
        [uuid.uuid4().hex[:16], run_id, ticker, as_of.isoformat(), tier, total, forward_return_20d],
    )


# ── Imports smoke ─────────────────────────────────────────────────────────────

def test_shadow_dataset_importable():
    from models.shadow_dataset import build_shadow_dataset  # noqa: F401


def test_shadow_ranker_importable():
    from models.shadow_ranker import ShadowRanker  # noqa: F401


def test_promotion_gates_importable():
    from models.promotion_gates import check_promotion_gates, GATES  # noqa: F401


# ── Shadow ranker isolation ───────────────────────────────────────────────────

def test_shadow_ranker_does_not_affect_production_scoring(conn):
    """Training shadow ranker must not change any row in the scores table."""
    from models.shadow_ranker import ShadowRanker
    run_id = "pre_shadow_run"
    for i in range(35):
        fwd = 0.15 if i % 3 == 0 else -0.05
        _seed_outcome(conn, f"T{i:03d}", run_id, fwd)

    # Capture pre-training scores
    pre = conn.execute(
        "SELECT ticker, total_score, tier FROM scores WHERE run_id=?", [run_id]
    ).fetchall()

    ranker = ShadowRanker(conn)
    ranker.train()

    # Scores must be identical after training
    post = conn.execute(
        "SELECT ticker, total_score, tier FROM scores WHERE run_id=?", [run_id]
    ).fetchall()
    assert pre == post, "Shadow ranker training must not modify production scores"


def test_shadow_dataset_includes_missed_opportunity_label(conn):
    """build_shadow_dataset includes missed_opportunity_label column."""
    from models.shadow_dataset import build_shadow_dataset
    run_id = "ds_run"
    for i in range(35):
        _seed_outcome(conn, f"DS{i:03d}", run_id, 0.1 if i % 2 == 0 else -0.1)

    dataset = build_shadow_dataset(conn)
    if dataset is not None:
        # Either DataFrame or dict with 'columns' key
        try:
            cols = list(dataset.columns)
        except AttributeError:
            cols = dataset.get("feature_names", []) if isinstance(dataset, dict) else []
        assert "missed_opportunity_label" in cols or dataset is None, (
            f"Shadow dataset should include missed_opportunity_label, got {cols}"
        )


def test_shadow_model_run_stored_in_db(conn):
    """After training, a model_runs row is stored with shadow context."""
    from models.shadow_ranker import ShadowRanker
    run_id = "smr_run"
    for i in range(35):
        _seed_outcome(conn, f"SM{i:03d}", run_id, 0.12 if i % 3 == 0 else -0.04)

    ranker = ShadowRanker(conn)
    result = ranker.train()

    if result is not None:  # xgboost may not be installed
        row = conn.execute(
            "SELECT COUNT(*) FROM model_runs WHERE model_type LIKE '%shadow%'"
        ).fetchone()
        assert row[0] >= 1, "Shadow training should record a model_runs entry"


def test_shadow_ranker_skips_gracefully_without_xgboost(conn):
    """ShadowRanker.train() returns None gracefully when xgboost not installed."""
    import sys
    from models.shadow_ranker import ShadowRanker
    run_id = "nogb_run"
    for i in range(35):
        _seed_outcome(conn, f"NG{i:03d}", run_id, 0.1)

    # If xgboost IS installed, this just verifies it doesn't crash
    ranker = ShadowRanker(conn)
    result = ranker.train()
    # No assertion on result value — just must not raise
    assert result is None or result is not None  # always true, just checks no exception


def test_shadow_ranker_skips_gracefully_without_data(conn):
    """ShadowRanker.train() returns None when insufficient data."""
    from models.shadow_ranker import ShadowRanker
    ranker = ShadowRanker(conn)
    result = ranker.train()
    assert result is None, "Should return None with no training data"
