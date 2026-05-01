from __future__ import annotations

import os
import pytest

from storage.db import get_connection, init_schema
from health.checks import run_all_checks, overall_status
from health.operational import (
    check_llm_provider,
    check_telegram_configured,
    check_email_configured,
    check_stub_sources,
    check_candidate_reviews,
    check_backtest_coverage,
    check_xgboost_installed,
    check_finra_data,
    check_universe_vs_config,
    check_a_tier_candidates,
)


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


# ── Core checks ───────────────────────────────────────────────────────────────

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


def test_schema_check_detects_all_required_tables(conn):
    results = run_all_checks(conn, "run004", {})
    schema_check = next((r for r in results if r["check_name"] == "schema_exists"), None)
    assert schema_check is not None
    assert schema_check["status"] == "pass", schema_check["message"]


# ── overall_status ────────────────────────────────────────────────────────────

def test_overall_status_pass():
    results = [
        {"check_name": "x", "status": "pass"},
        {"check_name": "y", "status": "pass"},
    ]
    assert overall_status(results) == "PASS"


def test_overall_status_pass_with_warnings():
    results = [
        {"check_name": "x", "status": "pass"},
        {"check_name": "y", "status": "warn"},
    ]
    assert overall_status(results) == "PASS_WITH_WARNINGS"


def test_overall_status_fail_dominates():
    results = [
        {"check_name": "x", "status": "pass"},
        {"check_name": "y", "status": "warn"},
        {"check_name": "z", "status": "fail"},
    ]
    assert overall_status(results) == "FAIL"


# ── Operational warnings ──────────────────────────────────────────────────────

def test_llm_provider_warns_when_mock(conn):
    import uuid
    conn.execute(
        "INSERT INTO llm_runs (llm_run_id, ticker, provider, status) VALUES (?, ?, ?, ?)",
        [uuid.uuid4().hex[:16], "AAPL", "mock", "ok"]
    )
    result = check_llm_provider(conn)
    assert result["status"] == "warn"
    assert "mock" in result["message"].lower()


def test_llm_provider_pass_when_real(conn):
    import uuid
    conn.execute(
        "INSERT INTO llm_runs (llm_run_id, ticker, provider, status) VALUES (?, ?, ?, ?)",
        [uuid.uuid4().hex[:16], "AAPL", "openai", "ok"]
    )
    result = check_llm_provider(conn)
    assert result["status"] == "pass"


def test_telegram_warns_when_not_configured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    result = check_telegram_configured()
    assert result["status"] == "warn"


def test_email_warns_when_not_configured(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)
    result = check_email_configured()
    assert result["status"] == "warn"


def test_stub_sources_warn_when_absent(conn):
    results = check_stub_sources(conn)
    names = {r["check_name"] for r in results}
    assert "stub_fda" in names
    assert "stub_stocktwits" in names
    assert "stub_gdelt" in names
    for r in results:
        assert r["status"] == "warn"


def test_stub_sources_no_warn_when_present(conn):
    import uuid
    from datetime import datetime
    for src in ("fda", "stocktwits", "gdelt"):
        conn.execute(
            "INSERT INTO source_runs (id, run_id, source_name, status) VALUES (?, ?, ?, ?)",
            [uuid.uuid4().hex[:16], "r1", src, "stub"]
        )
    results = check_stub_sources(conn)
    assert results == []


def test_candidate_reviews_warns_when_empty(conn):
    result = check_candidate_reviews(conn)
    assert result["status"] == "warn"


def test_backtest_coverage_warns_when_insufficient(conn):
    import uuid
    conn.execute(
        "INSERT INTO backtest_runs (backtest_run_id, tickers_tested, warning, status) VALUES (?, ?, ?, ?)",
        [uuid.uuid4().hex[:16], 0, "insufficient history", "complete"]
    )
    result = check_backtest_coverage(conn)
    assert result["status"] == "warn"


def test_xgboost_warns_when_not_installed(monkeypatch):
    import sys
    # Remove xgboost from sys.modules and make find_spec return None
    import importlib.util as ilu
    original = ilu.find_spec
    monkeypatch.setattr(ilu, "find_spec", lambda name: None if name == "xgboost" else original(name))
    result = check_xgboost_installed()
    assert result["status"] == "warn"


def test_finra_warns_when_zero_records(conn):
    result = check_finra_data(conn)
    assert result["status"] == "warn"


def test_universe_vs_config_warns_when_below_max(conn):
    import uuid
    conn.execute(
        "INSERT INTO companies (ticker, company_name, is_active) VALUES (?, ?, true)",
        ["AAPL", "Apple Inc"]
    )
    cfg = {"universe": {"max_symbols": 500}}
    result = check_universe_vs_config(conn, cfg)
    assert result["status"] == "warn"
    assert "500" in result["message"]


def test_a_tier_warns_when_zero(conn):
    result = check_a_tier_candidates(conn)
    assert result["status"] == "warn"


def test_fresh_db_is_pass_with_warnings_not_fail(conn):
    results = run_all_checks(conn, "run_fresh", {})
    status = overall_status(results)
    # A fresh DB with no data should never be FAIL — only PASS_WITH_WARNINGS
    assert status != "FAIL", f"Unexpected FAIL on fresh DB. Failing checks: {[r for r in results if r['status'] == 'fail']}"
    assert status == "PASS_WITH_WARNINGS"
