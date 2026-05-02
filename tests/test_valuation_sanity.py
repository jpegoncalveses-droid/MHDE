"""Valuation denominator sanity checks — experiment c129101b591b43a4."""
from __future__ import annotations

import uuid
from datetime import date

import pytest

from storage.db import get_connection, init_schema
from features.valuation import compute_valuation


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _price(conn, ticker, close):
    conn.execute(
        """INSERT INTO prices_daily (id, ticker, trade_date, close, source)
           VALUES (?, ?, ?, ?, 'test')""",
        [uuid.uuid4().hex[:16], ticker, date.today().isoformat(), close],
    )


def _fund(conn, ticker, concept, value):
    conn.execute(
        """INSERT INTO fundamentals_raw (id, ticker, concept, value, as_of_date)
           VALUES (?, ?, ?, ?, ?)""",
        [uuid.uuid4().hex[:16], ticker, concept, value, date.today().isoformat()],
    )


def _feat(features, name):
    return next((f for f in features if f["feature_name"] == name), None)


# ── Shares sanity ─────────────────────────────────────────────────────────────

def test_implausibly_small_shares_nulls_ps(conn):
    """CHTR-like: 100 shares reported → P/S ≈ 0 → null."""
    _price(conn, "CHTR", 173.0)
    _fund(conn, "CHTR", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding", 100)
    _fund(conn, "CHTR", "us-gaap/Revenues", 50_000_000_000)
    feats = compute_valuation(conn, "run1", "CHTR", date.today())
    ps = _feat(feats, "ps_proxy")
    assert ps["feature_score"] is None, f"Expected null P/S for implausible shares, got {ps}"
    assert ps["feature_value"] is None


def test_implausibly_small_shares_nulls_pb(conn):
    """CHTR-like: 100 shares → market cap near zero → P/B ≈ 0 → null."""
    _price(conn, "CHTR", 173.0)
    _fund(conn, "CHTR", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding", 100)
    _fund(conn, "CHTR", "us-gaap/StockholdersEquity", 10_000_000_000)
    feats = compute_valuation(conn, "run1", "CHTR", date.today())
    pb = _feat(feats, "pb_ratio")
    assert pb["feature_score"] is None, f"Expected null P/B for implausible shares, got {pb}"


def test_zero_shares_nulls_ps_and_pb(conn):
    """shares = 0 → null P/S and P/B (existing guard, regression test)."""
    _price(conn, "ZERO", 100.0)
    _fund(conn, "ZERO", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding", 0)
    _fund(conn, "ZERO", "us-gaap/Revenues", 1_000_000_000)
    _fund(conn, "ZERO", "us-gaap/StockholdersEquity", 500_000_000)
    feats = compute_valuation(conn, "run1", "ZERO", date.today())
    assert _feat(feats, "ps_proxy")["feature_score"] is None
    assert _feat(feats, "pb_ratio")["feature_score"] is None


# ── P/S bounds ────────────────────────────────────────────────────────────────

def test_ps_above_upper_bound_is_nulled(conn):
    """P/S = 200 (> 100) → null."""
    _price(conn, "XYZ", 100.0)
    _fund(conn, "XYZ", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding", 1_000_000_000)
    _fund(conn, "XYZ", "us-gaap/Revenues", 500_000)  # P/S = 100 * 1B / 500K = 200,000
    feats = compute_valuation(conn, "run1", "XYZ", date.today())
    ps = _feat(feats, "ps_proxy")
    assert ps["feature_score"] is None, f"P/S > 100 should be null, got {ps}"


# ── P/E bounds ────────────────────────────────────────────────────────────────

def test_implausible_pe_is_nulled(conn):
    """P/E = 200 (> 150) → null."""
    _price(conn, "PEHIGH", 100.0)
    _fund(conn, "PEHIGH", "us-gaap/EarningsPerShareDiluted", 0.50)  # P/E = 200
    feats = compute_valuation(conn, "run1", "PEHIGH", date.today())
    pe = _feat(feats, "pe_ratio")
    assert pe["feature_score"] is None, f"P/E = 200 should be null, got {pe}"
    assert pe["feature_value"] is None


# ── P/B bounds ────────────────────────────────────────────────────────────────

def test_negative_equity_nulls_pb(conn):
    """Equity < 0 → P/B null (existing guard, regression test)."""
    _price(conn, "NEG", 100.0)
    _fund(conn, "NEG", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding", 100_000_000)
    _fund(conn, "NEG", "us-gaap/StockholdersEquity", -5_000_000_000)
    feats = compute_valuation(conn, "run1", "NEG", date.today())
    pb = _feat(feats, "pb_ratio")
    assert pb["feature_score"] is None


def test_pb_above_upper_bound_is_nulled(conn):
    """P/B = 60 (> 50) → null."""
    _price(conn, "PBHIGH", 60.0)
    _fund(conn, "PBHIGH", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding", 100_000_000)
    _fund(conn, "PBHIGH", "us-gaap/StockholdersEquity", 100_000_000)  # BVPS = 1.0 → P/B = 60
    feats = compute_valuation(conn, "run1", "PBHIGH", date.today())
    pb = _feat(feats, "pb_ratio")
    assert pb["feature_score"] is None, f"P/B = 60 should be null, got {pb}"


# ── Valid ratios pass ─────────────────────────────────────────────────────────

def test_valid_ratios_pass_sanity_checks(conn):
    """Normal company: P/S=2, P/E=15, P/B=1.5 → all ratios scored."""
    _price(conn, "NORM", 30.0)
    _fund(conn, "NORM", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding", 100_000_000)
    _fund(conn, "NORM", "us-gaap/Revenues", 1_500_000_000)   # P/S = 30*100M/1.5B = 2.0
    _fund(conn, "NORM", "us-gaap/EarningsPerShareDiluted", 2.0)  # P/E = 30/2 = 15
    _fund(conn, "NORM", "us-gaap/StockholdersEquity", 2_000_000_000)  # BVPS=20, P/B=1.5
    feats = compute_valuation(conn, "run1", "NORM", date.today())
    assert _feat(feats, "ps_proxy")["feature_score"] is not None, "P/S=2 should pass"
    assert _feat(feats, "pe_ratio")["feature_score"] is not None, "P/E=15 should pass"
    assert _feat(feats, "pb_ratio")["feature_score"] is not None, "P/B=1.5 should pass"


# ── Governance: apply writes decision log ─────────────────────────────────────

def test_applied_experiment_writes_decision_log(conn, tmp_path):
    """apply_experiment() must write an entry to the decision log."""
    from unittest.mock import patch
    from pathlib import Path
    from learning.experiments import propose_experiment, approve_experiment, apply_experiment

    exp_id = propose_experiment(
        conn,
        hypothesis="Sanity-check hypothesis for test",
        proposed_change={"test": True},
        affected_components=["features/valuation.py"],
        expected_effect="Test effect only",
    )
    approve_experiment(conn, exp_id, "test_user")

    mock_log = tmp_path / "decision_log.md"
    with patch("learning.experiments._DECISION_LOG", mock_log):
        apply_experiment(conn, exp_id, "test_user", notes="test apply")

    assert mock_log.exists(), "Decision log file should be created on apply"
    content = mock_log.read_text()
    assert exp_id in content
    assert "Sanity-check hypothesis for test" in content
