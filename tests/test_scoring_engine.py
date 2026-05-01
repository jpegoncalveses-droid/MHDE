from __future__ import annotations

import pytest

from scoring.tiers import assign_tier, _COVERAGE_THRESHOLD
from scoring.scorecard import compute_scores
from storage.db import get_connection, init_schema


# ── Tier assignment ───────────────────────────────────────────────────────────

def test_tier_a():
    assert assign_tier(80, 60, 30, coverage=0.90) == "A"


def test_tier_a_fails_if_low_catalyst():
    assert assign_tier(80, 40, 30, coverage=0.90) != "A"


def test_tier_a_fails_if_low_coverage():
    assert assign_tier(80, 60, 30, coverage=0.70) != "A"


def test_tier_b():
    assert assign_tier(65, 40, 30, coverage=0.80) == "B"


def test_tier_b_requires_coverage():
    # Below B coverage threshold
    assert assign_tier(65, 40, 30, coverage=0.50) == "Incomplete"


def test_tier_c():
    assert assign_tier(50, 40, 30, coverage=0.80) == "C"


def test_reject_low_score():
    assert assign_tier(30, 60, 30, coverage=0.80) == "Reject"


def test_reject_high_risk():
    assert assign_tier(70, 60, 80, coverage=0.90) == "Reject"


def test_incomplete_when_low_coverage():
    # coverage < _COVERAGE_THRESHOLD → Incomplete regardless of score
    assert assign_tier(70, 60, 30, coverage=_COVERAGE_THRESHOLD - 0.01) == "Incomplete"


def test_incomplete_no_data():
    assert assign_tier(0, 0, 50, coverage=0.0) == "Incomplete"


def test_high_risk_always_rejects():
    # Even low coverage + high risk → Reject (risk dominates)
    assert assign_tier(10, 0, 90, coverage=0.10) == "Reject"


# ── Scorecard compute_scores ──────────────────────────────────────────────────

@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def test_compute_scores_empty(conn):
    compute_scores(conn, "run001", [], {})
    count = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    assert count == 0


def test_compute_scores_with_full_features(conn):
    from datetime import date
    conn.execute("INSERT INTO companies (ticker, company_name) VALUES ('AAPL', 'Apple')")
    for group, name, value, score in [
        ("valuation", "ps_proxy", 50.0, 60.0),
        ("quality", "net_income_positive", 1.0, 70.0),
        ("catalyst", "catalyst_score", 50.0, 50.0),
        ("momentum", "return_20d", 0.05, 55.0),
        ("sentiment", "short_interest_proxy", 0.1, 50.0),
        ("risk", "risk_penalty", 20.0, 20.0),
    ]:
        conn.execute(
            """INSERT INTO features (id, run_id, ticker, as_of_date, feature_group, feature_name,
                feature_value, feature_score)
               VALUES (?, 'run001', 'AAPL', ?, ?, ?, ?, ?)""",
            [f"{group}_{name}", date.today(), group, name, value, score],
        )
    compute_scores(conn, "run001", ["AAPL"], {})
    rows = conn.execute("SELECT ticker, tier, total_score, confidence FROM scores WHERE run_id='run001'").fetchall()
    assert len(rows) == 1
    assert rows[0][2] >= 0
    assert rows[0][2] <= 100
    # Full data → confidence should be high
    assert rows[0][3] == "high"


def test_compute_scores_missing_data_gives_incomplete(conn):
    """A ticker with only quality data and no price/momentum/sentiment → Incomplete."""
    from datetime import date
    conn.execute("INSERT INTO companies (ticker, company_name) VALUES ('QUAL', 'Quality Corp')")
    # Only quality + catalyst + risk — no valuation, no momentum, no sentiment
    for group, name, value, score in [
        ("quality", "net_income_positive", 1000000.0, 80.0),
        ("quality", "revenue_growth_yoy", 15.0, 75.0),
        ("catalyst", "catalyst_score", 1.0, 20.0),
        ("risk", "risk_penalty", 25.0, 25.0),
    ]:
        conn.execute(
            """INSERT INTO features (id, run_id, ticker, as_of_date, feature_group, feature_name,
                feature_value, feature_score)
               VALUES (?, 'run002', 'QUAL', ?, ?, ?, ?, ?)""",
            [f"{group}_{name}", date.today(), group, name, value, score],
        )
    compute_scores(conn, "run002", ["QUAL"], {})
    row = conn.execute("SELECT tier, cheap_score, momentum_score, sentiment_score FROM scores WHERE run_id='run002' AND ticker='QUAL'").fetchone()
    assert row is not None
    assert row[0] == "Incomplete"
    # Component scores stay null when no data — no neutral defaults
    assert row[1] is None, "cheap_score should be null, not 50"
    assert row[2] is None, "momentum_score should be null, not 50"
    assert row[3] is None, "sentiment_score should be null, not 50"


def test_missing_components_not_defaulted_to_50(conn):
    """Core invariant: missing components must be stored as NULL, never 50."""
    from datetime import date
    conn.execute("INSERT INTO companies (ticker, company_name) VALUES ('MISS', 'Missing Corp')")
    # Insert only one component
    conn.execute(
        """INSERT INTO features (id, run_id, ticker, as_of_date, feature_group, feature_name,
            feature_value, feature_score)
           VALUES ('x1', 'run003', 'MISS', ?, 'quality', 'net_income_positive', 100.0, 70.0)""",
        [date.today()],
    )
    conn.execute(
        """INSERT INTO features (id, run_id, ticker, as_of_date, feature_group, feature_name,
            feature_value, feature_score)
           VALUES ('x2', 'run003', 'MISS', ?, 'risk', 'risk_penalty', 25.0, 25.0)""",
        [date.today()],
    )
    compute_scores(conn, "run003", ["MISS"], {})
    row = conn.execute(
        "SELECT cheap_score, catalyst_score, momentum_score, sentiment_score FROM scores WHERE run_id='run003'"
    ).fetchone()
    assert row[0] is None, "cheap_score must be NULL (not 50) when valuation data is missing"
    assert row[1] is None, "catalyst_score must be NULL (not 50) when no catalyst data present"
    assert row[2] is None, "momentum_score must be NULL when missing"
    assert row[3] is None, "sentiment_score must be NULL when missing"


def test_total_score_lower_without_neutral_defaults(conn):
    """Total score must be lower when components are missing vs. faked as 50."""
    from datetime import date
    conn.execute("INSERT INTO companies (ticker, company_name) VALUES ('CMP', 'Compare Corp')")
    # Only quality data
    for name, value, score in [
        ("net_income_positive", 1.0, 80.0),
        ("revenue_growth_yoy", 15.0, 75.0),
    ]:
        conn.execute(
            """INSERT INTO features (id, run_id, ticker, as_of_date, feature_group, feature_name,
                feature_value, feature_score)
               VALUES (?, 'run004', 'CMP', ?, 'quality', ?, ?, ?)""",
            [f"q_{name}", date.today(), name, value, score],
        )
    conn.execute(
        """INSERT INTO features (id, run_id, ticker, as_of_date, feature_group, feature_name,
            feature_value, feature_score)
           VALUES ('r1', 'run004', 'CMP', ?, 'risk', 'risk_penalty', 25.0, 25.0)""",
        [date.today()],
    )
    compute_scores(conn, "run004", ["CMP"], {})
    row = conn.execute("SELECT total_score FROM scores WHERE run_id='run004'").fetchone()
    # Max possible with only quality (avg ~77.5, weight 0.25) and risk (25, weight 0.20):
    # 0.25 * 77.5 - 0.20 * 25 = 19.375 - 5 = 14.375
    # Must be well below the old fake score of ~48
    assert row[0] < 30, f"Total score {row[0]} is too high for partial data — neutral defaults may be back"
