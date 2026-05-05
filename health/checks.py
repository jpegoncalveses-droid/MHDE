from __future__ import annotations

import logging
import uuid
from datetime import datetime

import duckdb

from health.data_quality import (
    check_feature_coverage,
    check_fundamental_data,
    check_price_data,
    check_score_distribution,
    check_universe_size,
)
from health.operational import (
    check_a_tier_candidates,
    check_backtest_coverage,
    check_candidate_reviews,
    check_email_configured,
    check_finra_data,
    check_llm_provider,
    check_score_distribution_quality,
    check_stub_sources,
    check_telegram_configured,
    check_universe_vs_config,
    check_xgboost_installed,
)
from health.ml_checks import (
    check_last_prediction,
    check_ml_tables_freshness,
    check_rolling_precision,
    check_trained_models,
)
from health.source_status import (
    check_llm_failures,
    check_notification_failures,
    check_source_runs,
)

logger = logging.getLogger("mhde.health")


def _db_reachable(conn: duckdb.DuckDBPyConnection, db_path: str) -> dict:
    try:
        conn.execute("SELECT 1").fetchone()
        return {"check_name": "database_reachable", "status": "pass", "severity": "low",
                "message": f"DuckDB at {db_path}"}
    except Exception as exc:
        return {"check_name": "database_reachable", "status": "fail", "severity": "critical",
                "message": str(exc)}


def _schema_exists(conn: duckdb.DuckDBPyConnection) -> dict:
    try:
        tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
        required = [
            "companies", "pipeline_runs", "source_runs", "filings", "fundamentals_raw",
            "fundamentals_features", "prices_daily", "macro_series", "short_interest",
            "events", "features", "scores", "hypotheses", "rejections", "candidate_outcomes",
            "candidate_reviews", "scorecard_experiments", "backtest_runs", "model_runs",
            "llm_runs", "alerts", "health_checks", "review_notes", "dashboard_actions",
        ]
        missing = [t for t in required if t not in tables]
        if missing:
            return {"check_name": "schema_exists", "status": "fail", "severity": "critical",
                    "message": f"Missing tables: {missing}"}
        return {"check_name": "schema_exists", "status": "pass", "severity": "low",
                "message": f"{len(tables)} tables present (all required tables exist)"}
    except Exception as exc:
        return {"check_name": "schema_exists", "status": "fail", "severity": "critical",
                "message": str(exc)}


def overall_status(results: list[dict]) -> str:
    statuses = {r["status"] for r in results}
    if "fail" in statuses:
        return "FAIL"
    if "warn" in statuses:
        return "PASS_WITH_WARNINGS"
    if "skip" in statuses and not ("pass" in statuses):
        return "SKIP"
    return "PASS"


def _persist_checks(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    results: list[dict],
) -> None:
    now = datetime.utcnow()
    for r in results:
        try:
            conn.execute(
                """
                INSERT INTO health_checks
                    (id, run_id, check_name, status, severity, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    uuid.uuid4().hex[:16],
                    run_id,
                    r["check_name"],
                    r["status"],
                    r.get("severity", "medium"),
                    r.get("message", ""),
                    now,
                ],
            )
        except Exception as exc:
            logger.debug("Could not persist health check %s: %s", r.get("check_name"), exc)


def run_all_checks(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    cfg: dict,
) -> list[dict]:
    db_path = cfg.get("db_path", "data/mhde.duckdb")
    results: list[dict] = []

    # Core infrastructure
    results.append(_db_reachable(conn, db_path))
    results.append(_schema_exists(conn))

    # Data quality
    results.append(check_universe_size(conn))
    results.append(check_universe_vs_config(conn, cfg))
    results.append(check_price_data(conn))
    results.append(check_fundamental_data(conn))
    results.append(check_feature_coverage(conn))
    results.append(check_score_distribution(conn))
    results.append(check_a_tier_candidates(conn))
    results.extend(check_score_distribution_quality(conn))
    results.append(check_finra_data(conn))

    # Source status
    results.extend(check_source_runs(conn))
    results.extend(check_stub_sources(conn))

    # LLM and notifications
    results.append(check_llm_provider(conn))
    results.append(check_llm_failures(conn))
    results.append(check_telegram_configured())
    results.append(check_email_configured())
    results.append(check_notification_failures(conn))

    # Learning loop maturity
    results.append(check_candidate_reviews(conn))
    results.append(check_backtest_coverage(conn))
    results.append(check_xgboost_installed())

    # ML prediction engine
    results.append(check_trained_models())
    results.append(check_last_prediction(conn))
    results.append(check_rolling_precision(conn))
    results.extend(check_ml_tables_freshness(conn))

    _persist_checks(conn, run_id, results)
    return results
