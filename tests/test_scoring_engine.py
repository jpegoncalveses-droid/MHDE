from __future__ import annotations

import pytest

from scoring.tiers import assign_tier
from scoring.scorecard import compute_scores
from storage.db import get_connection, init_schema


def test_tier_a():
    assert assign_tier(80, 60, 30, []) == "A"


def test_tier_a_fails_if_low_catalyst():
    assert assign_tier(80, 40, 30, []) != "A"


def test_tier_b():
    assert assign_tier(65, 40, 30, []) == "B"


def test_tier_c():
    assert assign_tier(50, 40, 30, []) == "C"


def test_reject_low_score():
    assert assign_tier(30, 60, 30, []) == "Reject"


def test_reject_high_risk():
    assert assign_tier(70, 60, 80, []) == "Reject"


def test_reject_no_data():
    assert assign_tier(70, 60, 30, ["missing_all"]) == "Reject"


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


def test_compute_scores_with_features(conn):
    from datetime import date
    conn.execute("INSERT INTO companies (ticker, company_name) VALUES ('AAPL', 'Apple')")
    # Insert minimal features
    for group, name, value, score in [
        ("valuation", "ps_proxy", 50.0, 60.0),
        ("quality", "net_income_positive", 1.0, 70.0),
        ("catalyst", "catalyst_score", 50.0, 50.0),
        ("momentum", "return_20d", 0.05, 55.0),
        ("sentiment", "short_interest_proxy", 0.1, 50.0),
        ("risk", "risk_penalty", 20.0, 20.0),
    ]:
        conn.execute(
            """
            INSERT INTO features (id, run_id, ticker, as_of_date, feature_group, feature_name,
                feature_value, feature_score)
            VALUES (?, 'run001', 'AAPL', ?, ?, ?, ?, ?)
            """,
            [f"{group}_{name}", date.today(), group, name, value, score],
        )
    compute_scores(conn, "run001", ["AAPL"], {})
    rows = conn.execute("SELECT ticker, tier, total_score FROM scores WHERE run_id = 'run001'").fetchall()
    assert len(rows) == 1
    assert rows[0][2] >= 0
    assert rows[0][2] <= 100
