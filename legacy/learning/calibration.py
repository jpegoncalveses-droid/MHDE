from __future__ import annotations

import duckdb


def outcome_by_tier(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT o.tier,
               COUNT(*) as count,
               AVG(o.forward_return_20d) as avg_return_20d,
               AVG(o.forward_return_60d) as avg_return_60d,
               AVG(o.max_drawdown_20d) as avg_drawdown_20d,
               SUM(CASE WHEN o.hit_10pct_before_down_10pct THEN 1 ELSE 0 END) as hit_rate_count
        FROM candidate_outcomes o
        WHERE o.tier IS NOT NULL
        GROUP BY o.tier
        ORDER BY o.tier
        """
    ).fetchall()
    cols = ["tier", "count", "avg_return_20d", "avg_return_60d", "avg_drawdown_20d", "hit_rate_count"]
    return [dict(zip(cols, r)) for r in rows]


def outcome_by_score_bucket(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            CASE
                WHEN o.total_score >= 80 THEN '80+'
                WHEN o.total_score >= 70 THEN '70-79'
                WHEN o.total_score >= 60 THEN '60-69'
                WHEN o.total_score >= 50 THEN '50-59'
                ELSE '<50'
            END as score_bucket,
            COUNT(*) as count,
            AVG(o.forward_return_20d) as avg_return_20d,
            AVG(o.forward_return_60d) as avg_return_60d
        FROM candidate_outcomes o
        WHERE o.total_score IS NOT NULL
        GROUP BY score_bucket
        ORDER BY score_bucket DESC
        """
    ).fetchall()
    cols = ["score_bucket", "count", "avg_return_20d", "avg_return_60d"]
    return [dict(zip(cols, r)) for r in rows]


def outcome_by_review_status(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT r.review_status,
               COUNT(*) as count,
               AVG(r.usefulness_score) as avg_usefulness,
               AVG(r.thesis_quality_score) as avg_thesis_quality,
               AVG(r.evidence_quality_score) as avg_evidence_quality
        FROM candidate_reviews r
        GROUP BY r.review_status
        ORDER BY count DESC
        """
    ).fetchall()
    cols = ["review_status", "count", "avg_usefulness", "avg_thesis_quality", "avg_evidence_quality"]
    return [dict(zip(cols, r)) for r in rows]


def false_positive_reasons(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT false_positive_reason, COUNT(*) as count
        FROM candidate_reviews
        WHERE false_positive_reason IS NOT NULL
        GROUP BY false_positive_reason
        ORDER BY count DESC
        """
    ).fetchall()
    cols = ["reason", "count"]
    return [dict(zip(cols, r)) for r in rows]


def score_component_vs_outcome(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            AVG(s.cheap_score) as avg_cheap,
            AVG(s.quality_score) as avg_quality,
            AVG(s.catalyst_score) as avg_catalyst,
            AVG(s.momentum_score) as avg_momentum,
            AVG(s.sentiment_score) as avg_sentiment,
            AVG(s.risk_penalty) as avg_risk,
            AVG(o.forward_return_20d) as avg_return_20d,
            COUNT(*) as count
        FROM scores s
        JOIN candidate_outcomes o ON s.run_id = o.run_id AND s.ticker = o.ticker
        WHERE o.forward_return_20d IS NOT NULL
        """
    ).fetchall()
    if not rows or rows[0][0] is None:
        return []
    cols = ["avg_cheap", "avg_quality", "avg_catalyst", "avg_momentum",
            "avg_sentiment", "avg_risk", "avg_return_20d", "count"]
    return [dict(zip(cols, rows[0]))]


def feature_coverage_vs_confidence(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            run_id,
            ticker,
            COUNT(*) as total_features,
            COUNT(CASE WHEN feature_value IS NOT NULL THEN 1 END) as non_null_features,
            COUNT(CASE WHEN feature_value IS NOT NULL THEN 1 END) * 100.0 / COUNT(*) as coverage_pct,
            COUNT(CASE WHEN confidence = 'high' THEN 1 END) as high_conf_features
        FROM features
        WHERE ticker IS NOT NULL
        GROUP BY run_id, ticker
        ORDER BY coverage_pct DESC
        LIMIT 50
        """
    ).fetchall()
    cols = ["run_id", "ticker", "total_features", "non_null_features", "coverage_pct", "high_conf_features"]
    return [dict(zip(cols, r)) for r in rows]


def source_failure_impact(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT source_name,
               COUNT(*) as total_runs,
               SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_count,
               SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as error_rate_pct,
               MAX(finished_at) as last_run
        FROM source_runs
        GROUP BY source_name
        ORDER BY error_rate_pct DESC
        """
    ).fetchall()
    cols = ["source_name", "total_runs", "error_count", "error_rate_pct", "last_run"]
    return [dict(zip(cols, r)) for r in rows]


def llm_confidence_vs_human_review(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            l.model,
            COUNT(*) as count,
            AVG(r.usefulness_score) as avg_usefulness,
            AVG(r.thesis_quality_score) as avg_thesis_quality
        FROM llm_runs l
        JOIN candidate_reviews r ON l.run_id = r.run_id AND l.ticker = r.ticker
        WHERE r.usefulness_score IS NOT NULL
        GROUP BY l.model
        ORDER BY avg_usefulness DESC
        """
    ).fetchall()
    cols = ["model", "count", "avg_usefulness", "avg_thesis_quality"]
    return [dict(zip(cols, r)) for r in rows]
