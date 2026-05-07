from __future__ import annotations

import pytest
from datetime import date

from storage.db import get_connection, init_schema
from outcomes.tracker import create_outcome_record, update_forward_returns
from outcomes.review import get_pending_outcomes, update_review_status


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def test_create_outcome_record(conn):
    cid = create_outcome_record(conn, "run001", "AAPL", date.today(), "A", 82.0, 150.0)
    rows = conn.execute("SELECT tier, review_status FROM candidate_outcomes").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "A"
    assert rows[0][1] == "pending"


def test_create_outcome_record_dedup(conn):
    create_outcome_record(conn, "run001", "AAPL", date.today(), "A", 82.0, 150.0)
    create_outcome_record(conn, "run001", "AAPL", date.today(), "A", 82.0, 150.0)
    count = conn.execute("SELECT COUNT(*) FROM candidate_outcomes").fetchone()[0]
    assert count == 1


def test_update_forward_returns(conn):
    create_outcome_record(conn, "run001", "AAPL", date.today(), "A", 82.0, 150.0)
    candidate_id = conn.execute("SELECT candidate_id FROM candidate_outcomes").fetchone()[0]
    update_forward_returns(conn, candidate_id, {"forward_return_20d": 0.10})
    ret = conn.execute(
        "SELECT forward_return_20d FROM candidate_outcomes WHERE candidate_id = ?",
        [candidate_id],
    ).fetchone()[0]
    assert ret == pytest.approx(0.10)


def test_get_pending_outcomes(conn):
    create_outcome_record(conn, "run001", "TSLA", date.today(), "B", 65.0, None)
    pending = get_pending_outcomes(conn)
    assert len(pending) == 1
    assert pending[0]["ticker"] == "TSLA"


def test_update_review_status_valid(conn):
    create_outcome_record(conn, "run001", "NVDA", date.today(), "A", 80.0, None)
    cid = conn.execute("SELECT candidate_id FROM candidate_outcomes").fetchone()[0]
    ok = update_review_status(conn, cid, "validated", "Looks good")
    assert ok
    row = conn.execute("SELECT review_status FROM candidate_outcomes WHERE candidate_id = ?", [cid]).fetchone()
    assert row[0] == "validated"


def test_update_review_status_invalid(conn):
    create_outcome_record(conn, "run001", "TEST", date.today(), "C", 50.0, None)
    cid = conn.execute("SELECT candidate_id FROM candidate_outcomes").fetchone()[0]
    ok = update_review_status(conn, cid, "invalid_status")
    assert not ok


def test_update_forward_returns_3d_10d(conn):
    """forward_return_3d and forward_return_10d can be persisted via update_forward_returns."""
    create_outcome_record(conn, "run002", "NVDA", date.today(), "A", 85.0, 200.0)
    candidate_id = conn.execute(
        "SELECT candidate_id FROM candidate_outcomes WHERE ticker = 'NVDA'"
    ).fetchone()[0]
    update_forward_returns(conn, candidate_id, {
        "forward_return_3d": 0.09,
        "forward_return_10d": 0.14,
    })
    row = conn.execute(
        "SELECT forward_return_3d, forward_return_10d FROM candidate_outcomes WHERE candidate_id = ?",
        [candidate_id],
    ).fetchone()
    assert row[0] == pytest.approx(0.09)
    assert row[1] == pytest.approx(0.14)
