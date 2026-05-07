"""Tests for the MHDE learning loop."""
from __future__ import annotations

import json
import pytest
import duckdb

from storage.db import init_schema
from learning.error_taxonomy import FALSE_POSITIVE_REASONS, REVIEW_STATUSES, EXPERIMENT_STATUSES
from learning.feedback import submit_review, get_reviews
from learning.experiments import (
    propose_experiment, mark_tested, approve_experiment,
    apply_experiment, reject_experiment, archive_experiment, get_experiments,
)
from learning.calibration import (
    outcome_by_tier,
    outcome_by_score_bucket,
    outcome_by_review_status,
    false_positive_reasons,
)
from learning.insights import generate_insights
from learning.summarize import write_learning_report, _INSUFFICIENT_MSG


@pytest.fixture()
def conn(tmp_path):
    db = duckdb.connect(str(tmp_path / "test.duckdb"))
    init_schema(db)
    yield db
    db.close()


# ── Schema ────────────────────────────────────────────────────────────────────

def test_candidate_reviews_table_exists(conn):
    result = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name = 'candidate_reviews'"
    ).fetchone()
    assert result is not None


def test_scorecard_experiments_table_exists(conn):
    result = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name = 'scorecard_experiments'"
    ).fetchone()
    assert result is not None


# ── Review status validation ──────────────────────────────────────────────────

def test_valid_review_statuses_accepted(conn):
    for status in REVIEW_STATUSES:
        rid = submit_review(conn, run_id="run1", ticker=f"T{status[:3].upper()}", review_status=status)
        assert rid is not None


def test_invalid_review_status_raises(conn):
    with pytest.raises(ValueError, match="Invalid review_status"):
        submit_review(conn, run_id="run1", ticker="AAPL", review_status="invented_status")


def test_usefulness_score_out_of_range_raises(conn):
    with pytest.raises(ValueError, match="usefulness_score"):
        submit_review(conn, run_id="run1", ticker="AAPL", review_status="useful", usefulness_score=6)


def test_usefulness_score_valid_range(conn):
    for score in (1, 2, 3, 4, 5):
        submit_review(conn, run_id=f"r{score}", ticker="AAPL", review_status="useful", usefulness_score=score)
    rows = get_reviews(conn)
    scores = {r["usefulness_score"] for r in rows}
    assert scores == {1, 2, 3, 4, 5}


# ── False-positive taxonomy ───────────────────────────────────────────────────

def test_false_positive_taxonomy_codes_complete():
    assert "bad_data" in FALSE_POSITIVE_REASONS
    assert "stale_data" in FALSE_POSITIVE_REASONS
    assert "llm_overstated_case" in FALSE_POSITIVE_REASONS
    assert "source_failure" in FALSE_POSITIVE_REASONS
    assert len(FALSE_POSITIVE_REASONS) == 15


def test_invalid_false_positive_reason_raises(conn):
    with pytest.raises(ValueError, match="Invalid false_positive_reason"):
        submit_review(
            conn, run_id="r1", ticker="AAPL", review_status="false_positive",
            false_positive_reason="not_a_real_reason"
        )


def test_valid_false_positive_reason_accepted(conn):
    rid = submit_review(
        conn, run_id="r1", ticker="AAPL", review_status="false_positive",
        false_positive_reason="bad_data"
    )
    assert rid is not None


# ── Learning summary with empty history ───────────────────────────────────────

def test_learning_summary_handles_empty_history(conn, tmp_path):
    path = write_learning_report(conn, tmp_path)
    assert path.exists()
    content = path.read_text()
    assert _INSUFFICIENT_MSG in content


def test_learning_summary_json_written(conn, tmp_path):
    write_learning_report(conn, tmp_path)
    json_files = list(tmp_path.glob("learning_report_*.json"))
    assert len(json_files) == 1
    data = json.loads(json_files[0].read_text())
    assert data["insufficient_data"] is True


# ── Learning summary with sample data ────────────────────────────────────────

def _seed_sample_data(conn):
    import uuid
    from datetime import date
    # Insert companies
    for ticker in ("AAPL", "TSLA", "JPM", "NVDA", "UBER"):
        conn.execute(
            "INSERT OR IGNORE INTO companies (ticker, company_name) VALUES (?, ?)",
            [ticker, f"{ticker} Inc"]
        )
    # Insert scores
    run_id = "testrun001"
    for i, ticker in enumerate(("AAPL", "TSLA", "JPM", "NVDA", "UBER"), 1):
        sid = uuid.uuid4().hex[:16]
        conn.execute(
            """INSERT INTO scores (id, run_id, ticker, as_of_date, cheap_score,
               quality_score, catalyst_score, momentum_score, sentiment_score,
               risk_penalty, total_score, tier)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [sid, run_id, ticker, date.today(), 60.0, 65.0, 55.0, 50.0, 50.0,
             20.0, 70.0 + i, "B"],
        )
    # Insert candidate_outcomes
    for i, ticker in enumerate(("AAPL", "TSLA", "JPM", "NVDA", "UBER"), 1):
        cid = uuid.uuid4().hex[:16]
        conn.execute(
            """INSERT INTO candidate_outcomes (candidate_id, run_id, ticker, as_of_date,
               tier, total_score, forward_return_20d, forward_return_60d, max_drawdown_20d)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [cid, run_id, ticker, date.today(), "B", 70.0 + i, 0.05 * i, 0.08 * i, -0.03 * i],
        )
    # Insert reviews
    for i, ticker in enumerate(("AAPL", "TSLA", "JPM", "NVDA", "UBER"), 1):
        rid = uuid.uuid4().hex[:16]
        conn.execute(
            """INSERT INTO candidate_reviews (review_id, run_id, ticker, review_status,
               usefulness_score, thesis_quality_score, evidence_quality_score)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [rid, run_id, ticker, "useful", 4, 3, 4],
        )
    return run_id


def test_learning_summary_computes_aggregates(conn, tmp_path):
    _seed_sample_data(conn)
    path = write_learning_report(conn, tmp_path)
    content = path.read_text()
    assert "Outcome by Tier" in content or "Insufficient" in content


def test_outcome_by_tier_returns_data(conn):
    _seed_sample_data(conn)
    rows = outcome_by_tier(conn)
    assert isinstance(rows, list)


def test_outcome_by_score_bucket_returns_data(conn):
    _seed_sample_data(conn)
    rows = outcome_by_score_bucket(conn)
    assert isinstance(rows, list)


def test_outcome_by_review_status_returns_data(conn):
    _seed_sample_data(conn)
    rows = outcome_by_review_status(conn)
    assert len(rows) > 0
    assert rows[0]["review_status"] == "useful"


def test_false_positive_aggregation(conn):
    _seed_sample_data(conn)
    submit_review(conn, run_id="r2", ticker="XYZ", review_status="false_positive",
                  false_positive_reason="bad_data")
    submit_review(conn, run_id="r3", ticker="ABC", review_status="false_positive",
                  false_positive_reason="bad_data")
    rows = false_positive_reasons(conn)
    reason_map = {r["reason"]: r["count"] for r in rows}
    assert reason_map.get("bad_data", 0) >= 2


# ── Scorecard experiments ─────────────────────────────────────────────────────

def test_experiment_creation(conn):
    eid = propose_experiment(
        conn,
        hypothesis="Reduce sentiment weight from 10% to 5%",
        proposed_change={"sentiment_weight": 0.05},
        affected_components=["scoring/scorecard.py"],
        expected_effect="Fewer sentiment-driven false positives",
    )
    assert eid is not None
    exps = get_experiments(conn)
    assert len(exps) == 1
    assert exps[0]["status"] == "proposed"


def test_experiments_not_auto_applied(conn):
    propose_experiment(
        conn,
        hypothesis="Test experiment",
        proposed_change={"test": True},
        affected_components=["scoring/scorecard.py"],
        expected_effect="Test",
    )
    exps = get_experiments(conn)
    for e in exps:
        assert e["status"] != "applied", "Experiments must not be auto-applied"
        assert e["applied_at"] is None, "applied_at must be NULL until human applies"


def test_approve_does_not_set_applied_at(conn):
    eid = propose_experiment(
        conn,
        hypothesis="Tighten A-tier threshold",
        proposed_change={"tier_a_min_score": 80},
        affected_components=["scoring/tiers.py"],
        expected_effect="Fewer A-tier candidates",
    )
    approve_experiment(conn, eid, approved_by="jp@example.com", review_notes="Looks good")
    exps = get_experiments(conn)
    assert exps[0]["status"] == "approved"
    assert exps[0]["approved_by"] == "jp@example.com"
    assert exps[0]["applied_at"] is None, "approve_experiment must not set applied_at"
    assert exps[0]["applied_by"] is None, "approve_experiment must not set applied_by"


def test_apply_experiment_sets_applied_at(conn, tmp_path, monkeypatch):
    import learning.experiments as exp_mod
    monkeypatch.setattr(exp_mod, "_DECISION_LOG", tmp_path / "decision_log.md")

    eid = propose_experiment(
        conn,
        hypothesis="Raise risk penalty weight",
        proposed_change={"risk_weight": 0.30},
        affected_components=["scoring/scorecard.py"],
        expected_effect="Fewer high-risk false positives",
    )
    approve_experiment(conn, eid, approved_by="jp@example.com")
    apply_experiment(conn, eid, applied_by="jp@example.com", notes="Reviewed and confirmed")

    exps = get_experiments(conn)
    e = exps[0]
    assert e["status"] == "applied"
    assert e["applied_at"] is not None
    assert e["applied_by"] == "jp@example.com"


def test_apply_experiment_requires_approved_status(conn):
    eid = propose_experiment(
        conn,
        hypothesis="Unapproved change",
        proposed_change={"x": 1},
        affected_components=[],
        expected_effect="N/A",
    )
    with pytest.raises(ValueError, match="must be 'approved'"):
        apply_experiment(conn, eid, applied_by="jp@example.com")


def test_apply_experiment_fails_if_only_proposed(conn, tmp_path, monkeypatch):
    import learning.experiments as exp_mod
    monkeypatch.setattr(exp_mod, "_DECISION_LOG", tmp_path / "decision_log.md")

    eid = propose_experiment(
        conn,
        hypothesis="Directly apply without approval",
        proposed_change={},
        affected_components=[],
        expected_effect="Should fail",
    )
    # Must not be appliable when status is 'proposed'
    with pytest.raises(ValueError):
        apply_experiment(conn, eid, applied_by="jp@example.com")


def test_apply_experiment_writes_decision_log(conn, tmp_path, monkeypatch):
    import learning.experiments as exp_mod
    log_path = tmp_path / "decision_log.md"
    monkeypatch.setattr(exp_mod, "_DECISION_LOG", log_path)

    eid = propose_experiment(
        conn,
        hypothesis="Add peer context requirement",
        proposed_change={"add_feature_group": "peer_context"},
        affected_components=["features/"],
        expected_effect="Richer context",
    )
    approve_experiment(conn, eid, approved_by="jp@example.com")
    apply_experiment(conn, eid, applied_by="jp@example.com", notes="Verified in staging")

    assert log_path.exists()
    content = log_path.read_text()
    assert "peer_context" in content
    assert "jp@example.com" in content
    assert "applied" in content.lower()


def test_approve_does_not_mutate_scoring_config(conn):
    eid = propose_experiment(
        conn,
        hypothesis="Change weights",
        proposed_change={"cheap_weight": 0.40},
        affected_components=["scoring/scorecard.py"],
        expected_effect="Higher weight on valuation",
    )
    approve_experiment(conn, eid, approved_by="jp@example.com")
    # Scoring config file must not have been touched
    from pathlib import Path
    cfg_path = Path("config/scoring.yaml")
    if cfg_path.exists():
        content = cfg_path.read_text()
        # The proposed change value should NOT have been written to the config
        assert "0.40" not in content or "cheap_weight" not in content


def test_experiment_mark_tested_step(conn):
    eid = propose_experiment(
        conn,
        hypothesis="Hypothesis to test",
        proposed_change={"x": 1},
        affected_components=[],
        expected_effect="TBD",
    )
    mark_tested(conn, eid, backtest_result={"hit_rate": 0.6}, backtest_notes="Smoke backtest only")
    exps = get_experiments(conn)
    assert exps[0]["status"] == "tested"
    assert exps[0]["backtest_notes"] == "Smoke backtest only"


def test_experiment_reject(conn):
    eid = propose_experiment(
        conn,
        hypothesis="Bad idea",
        proposed_change={"bad": True},
        affected_components=[],
        expected_effect="Unknown",
    )
    reject_experiment(conn, eid, review_notes="Not justified")
    exps = get_experiments(conn)
    assert exps[0]["status"] == "rejected"


def test_experiment_archive(conn):
    eid = propose_experiment(
        conn,
        hypothesis="Old idea",
        proposed_change={},
        affected_components=[],
        expected_effect="N/A",
    )
    archive_experiment(conn, eid, review_notes="No longer relevant")
    exps = get_experiments(conn)
    assert exps[0]["status"] == "archived"


# ── Insights ──────────────────────────────────────────────────────────────────

def test_insights_returns_list_on_empty_data(conn):
    insights = generate_insights(conn)
    assert isinstance(insights, list)


def test_insights_source_failure_flagged(conn):
    import uuid
    from datetime import datetime
    for _ in range(5):
        conn.execute(
            """INSERT INTO source_runs (id, run_id, source_name, status, started_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [uuid.uuid4().hex[:16], "run1", "bad_source", "error",
             datetime.utcnow(), datetime.utcnow()],
        )
    insights = generate_insights(conn)
    source_insights = [i for i in insights if i["category"] == "source_reliability"]
    assert len(source_insights) >= 1
    assert "bad_source" in source_insights[0]["message"]


# ── Dashboard queries ─────────────────────────────────────────────────────────

def test_dashboard_get_candidate_reviews(conn):
    from dashboard.services.queries import get_candidate_reviews
    submit_review(conn, run_id="r1", ticker="AAPL", review_status="useful", usefulness_score=4)
    rows = get_candidate_reviews(conn)
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAPL"


def test_dashboard_get_scorecard_experiments(conn):
    from dashboard.services.queries import get_scorecard_experiments
    propose_experiment(
        conn,
        hypothesis="Test query function",
        proposed_change={},
        affected_components=[],
        expected_effect="Verify dashboard query works",
    )
    rows = get_scorecard_experiments(conn)
    assert len(rows) == 1
    assert rows[0]["status"] == "proposed"


# ── CLI command ───────────────────────────────────────────────────────────────

def test_cli_learn_summarize(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_cli.duckdb")
    monkeypatch.setenv("MHDE_DB_PATH", db_path)
    # Pre-init the schema so the CLI doesn't fail on missing tables
    import duckdb as _ddb
    from storage.db import init_schema as _init
    _conn = _ddb.connect(db_path)
    _init(_conn)
    _conn.close()

    from click.testing import CliRunner
    from main import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["learn", "summarize", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Learning report written" in result.output
