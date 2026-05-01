from __future__ import annotations

import pytest

from storage.db import get_connection, init_schema
from health.checks import run_all_checks


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def test_health_checks_return_list(conn):
    results = run_all_checks(conn, "run001", {})
    assert isinstance(results, list)
    assert len(results) > 0


def test_health_checks_have_required_fields(conn):
    results = run_all_checks(conn, "run001", {})
    for r in results:
        assert "check_name" in r
        assert "status" in r
        assert r["status"] in ("pass", "warn", "fail", "skip")


def test_health_checks_persist_to_db(conn):
    run_all_checks(conn, "run002", {})
    count = conn.execute(
        "SELECT COUNT(*) FROM health_checks WHERE run_id = 'run002'"
    ).fetchone()[0]
    assert count > 0


def test_health_database_check_passes(conn):
    results = run_all_checks(conn, "run003", {})
    db_check = next((r for r in results if r["check_name"] == "database_reachable"), None)
    assert db_check is not None
    assert db_check["status"] == "pass"
