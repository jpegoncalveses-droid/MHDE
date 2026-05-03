from __future__ import annotations

import logging

import duckdb

from storage.db import init_schema

logger = logging.getLogger("mhde.storage.migrations")

_CURRENT_VERSION = 8


def run_migrations(conn: duckdb.DuckDBPyConnection) -> None:
    init_schema(conn)
    try:
        result = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = result[0] if result and result[0] is not None else 0
    except Exception:
        current = 0

    if current < 1:
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (1, 'Initial schema') ON CONFLICT DO NOTHING"
        )
        logger.info("Applied migration v1: initial schema")

    if current < 2:
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (2, 'Learning loop: candidate_reviews + scorecard_experiments') ON CONFLICT DO NOTHING"
        )
        logger.info("Applied migration v2: learning loop tables")

    if current < 4:
        # pipeline_runs, review_notes, dashboard_actions may already exist on fresh DBs
        for tbl in ("pipeline_runs", "review_notes", "dashboard_actions"):
            try:
                conn.execute(f"SELECT 1 FROM {tbl} LIMIT 1")
            except Exception:
                # Table doesn't exist — init_schema already ran, so it's there; this catches legacy DBs
                pass
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (4, 'Add pipeline_runs, review_notes, dashboard_actions') ON CONFLICT DO NOTHING"
        )
        logger.info("Applied migration v4: pipeline_runs, review_notes, dashboard_actions")

    if current < 3:
        # Add applied_by and backtest_notes to scorecard_experiments (may already exist on fresh DBs)
        for col, typedef in (("applied_by", "VARCHAR"), ("backtest_notes", "VARCHAR")):
            try:
                conn.execute(f"ALTER TABLE scorecard_experiments ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # column already exists
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (3, 'Add applied_by + backtest_notes to scorecard_experiments') ON CONFLICT DO NOTHING"
        )
        logger.info("Applied migration v3: scorecard_experiments governance columns")

    if current < 5:
        existing = {
            r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'companies'"
            ).fetchall()
        }
        for col, typedef in (
            ("active_sec_reporter", "BOOLEAN DEFAULT true"),
            ("last_financial_filing_date", "DATE"),
            ("has_financial_reporting_forms", "BOOLEAN DEFAULT true"),
            ("universe_exclusion_reason", "VARCHAR"),
        ):
            if col not in existing:
                conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {typedef}")
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (5, 'Add universe quality guard columns to companies') ON CONFLICT DO NOTHING"
        )
        logger.info("Applied migration v5: universe quality guard columns")

    if current < 6:
        existing_cols = {
            r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'candidate_outcomes'"
            ).fetchall()
        }
        for col in ("forward_return_3d", "forward_return_10d"):
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE candidate_outcomes ADD COLUMN {col} DOUBLE")
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (6, 'Add forward_return_3d and forward_return_10d to candidate_outcomes') ON CONFLICT DO NOTHING"
        )
        logger.info("Applied migration v6: forward_return_3d/10d on candidate_outcomes")

    if current < 7:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS earnings_estimates (
                ticker VARCHAR NOT NULL,
                fiscal_date DATE NOT NULL,
                reported_eps DOUBLE,
                estimated_eps DOUBLE,
                surprise_eps DOUBLE,
                surprise_pct DOUBLE,
                reported_revenue DOUBLE,
                estimated_revenue DOUBLE,
                revenue_surprise_pct DOUBLE,
                guidance_direction VARCHAR,
                source VARCHAR NOT NULL DEFAULT 'alpha_vantage',
                ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(ticker, fiscal_date, source)
            )
        """)
        conn.execute(
            "INSERT INTO schema_version (version, description) "
            "VALUES (7, 'Add earnings_estimates table') ON CONFLICT DO NOTHING"
        )
        logger.info("Applied migration v7: earnings_estimates table")

    if current < 8:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS move_episodes (
                episode_id VARCHAR PRIMARY KEY,
                ticker VARCHAR NOT NULL,
                start_date DATE NOT NULL,
                latest_date DATE NOT NULL,
                cumulative_return DOUBLE DEFAULT 0,
                max_1d_return DOUBLE,
                max_3d_return DOUBLE,
                max_5d_return DOUBLE,
                status VARCHAR DEFAULT 'active',
                parent_catalyst_event_id VARCHAR,
                attribution_type VARCHAR,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "INSERT INTO schema_version (version, description) "
            "VALUES (8, 'Add move_episodes table') ON CONFLICT DO NOTHING"
        )
        logger.info("Applied migration v8: move_episodes table")
