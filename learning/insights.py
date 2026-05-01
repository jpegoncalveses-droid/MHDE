from __future__ import annotations

import duckdb

from learning.experiments import propose_experiment


def generate_insights(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """
    Inspect outcome and review data and emit a list of human-readable insight dicts.
    Each insight may carry an optional suggested experiment (not auto-applied).
    """
    insights: list[dict] = []
    _check_a_tier_quality(conn, insights)
    _check_catalyst_score_overstatement(conn, insights)
    _check_llm_overstatement(conn, insights)
    _check_peer_context_gap(conn, insights)
    _check_over_strict_rejection(conn, insights)
    _check_source_failures(conn, insights)
    _check_stale_data_pattern(conn, insights)
    return insights


def _check_a_tier_quality(conn: duckdb.DuckDBPyConnection, insights: list[dict]) -> None:
    row = conn.execute(
        """
        SELECT COUNT(*) as n,
               AVG(r.usefulness_score) as avg_useful,
               AVG(o.forward_return_20d) as avg_ret
        FROM candidate_outcomes o
        JOIN candidate_reviews r ON o.run_id = r.run_id AND o.ticker = r.ticker
        WHERE o.tier = 'A' AND r.usefulness_score IS NOT NULL
        """
    ).fetchone()
    if not row or row[0] < 3:
        return
    n, avg_useful, avg_ret = row
    if avg_useful is not None and avg_useful < 2.5:
        insights.append({
            "category": "scorecard_weakness",
            "message": f"A-tier candidates have low avg usefulness score ({avg_useful:.1f}/5 over {n} reviewed). "
                       "Scorecard may be over-ranking weak candidates.",
            "severity": "high",
            "suggested_experiment": {
                "hypothesis": "Tighten A-tier threshold to reduce false promotions",
                "proposed_change": {"tier_a_min_score": 80, "tier_a_min_catalyst": 60},
                "affected_components": ["scoring/tiers.py"],
                "expected_effect": "Fewer A-tier candidates, higher average quality",
            },
        })


def _check_catalyst_score_overstatement(conn: duckdb.DuckDBPyConnection, insights: list[dict]) -> None:
    row = conn.execute(
        """
        SELECT COUNT(*) as n
        FROM candidate_reviews
        WHERE false_positive_reason = 'weak_catalyst'
        """
    ).fetchone()
    if not row or row[0] < 3:
        return
    insights.append({
        "category": "catalyst_rules",
        "message": f"{row[0]} candidates marked as 'weak_catalyst'. Consider tightening catalyst scoring rules.",
        "severity": "medium",
        "suggested_experiment": {
            "hypothesis": "Raise minimum catalyst evidence count before awarding high catalyst_score",
            "proposed_change": {"min_catalyst_signals": 2},
            "affected_components": ["features/catalyst.py", "scoring/scorecard.py"],
            "expected_effect": "Reduce weak-catalyst false positives",
        },
    })


def _check_llm_overstatement(conn: duckdb.DuckDBPyConnection, insights: list[dict]) -> None:
    row = conn.execute(
        """
        SELECT COUNT(*) as n
        FROM candidate_reviews
        WHERE false_positive_reason = 'llm_overstated_case'
        """
    ).fetchone()
    if not row or row[0] < 2:
        return
    insights.append({
        "category": "llm_quality",
        "message": f"{row[0]} candidates flagged for LLM overstating the case. "
                   "Consider adding a critique step or lowering default LLM confidence.",
        "severity": "medium",
        "suggested_experiment": {
            "hypothesis": "Add thesis_critique LLM pass before finalizing hypothesis confidence",
            "proposed_change": {"enable_thesis_critique": True},
            "affected_components": ["llm/runner.py", "llm/prompts/thesis_critique.md"],
            "expected_effect": "More calibrated LLM output, fewer overstatement flags",
        },
    })


def _check_peer_context_gap(conn: duckdb.DuckDBPyConnection, insights: list[dict]) -> None:
    row = conn.execute(
        """
        SELECT COUNT(*) as n
        FROM candidate_reviews
        WHERE false_positive_reason = 'missing_peer_context'
           OR missing_evidence ILIKE '%peer%'
           OR missing_evidence ILIKE '%sector%'
           OR missing_evidence ILIKE '%comparable%'
        """
    ).fetchone()
    if not row or row[0] < 2:
        return
    insights.append({
        "category": "feature_gap",
        "message": f"{row[0]} reviews mention missing peer/sector context. "
                   "Consider adding peer comparison feature.",
        "severity": "medium",
        "suggested_experiment": {
            "hypothesis": "Add peer_context feature group comparing ticker vs sector median",
            "proposed_change": {"add_feature_group": "peer_context"},
            "affected_components": ["features/"],
            "expected_effect": "Richer context, fewer missing_peer_context flags",
        },
    })


def _check_over_strict_rejection(conn: duckdb.DuckDBPyConnection, insights: list[dict]) -> None:
    row = conn.execute(
        """
        SELECT COUNT(*) as n, AVG(o.forward_return_20d) as avg_ret
        FROM candidate_outcomes o
        JOIN rejections r ON o.ticker = r.ticker
        WHERE o.forward_return_20d > 0.10
        """
    ).fetchone()
    if not row or row[0] < 3:
        return
    insights.append({
        "category": "rejection_rule",
        "message": f"{row[0]} rejected candidates later had >10% return. Rejection rules may be too strict.",
        "severity": "low",
        "suggested_experiment": {
            "hypothesis": "Review rejection threshold — Reject floor may be too high",
            "proposed_change": {"tier_reject_max_score": 40},
            "affected_components": ["scoring/tiers.py"],
            "expected_effect": "Rescue borderline candidates with real upside",
        },
    })


def _check_source_failures(conn: duckdb.DuckDBPyConnection, insights: list[dict]) -> None:
    rows = conn.execute(
        """
        SELECT source_name,
               SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as error_rate
        FROM source_runs
        GROUP BY source_name
        HAVING error_rate > 30
        ORDER BY error_rate DESC
        """
    ).fetchall()
    for source_name, error_rate in rows:
        insights.append({
            "category": "source_reliability",
            "message": f"Source '{source_name}' has {error_rate:.0f}% error rate. "
                       "Investigate before trusting features derived from it.",
            "severity": "high",
            "suggested_experiment": None,
        })


def _check_stale_data_pattern(conn: duckdb.DuckDBPyConnection, insights: list[dict]) -> None:
    row = conn.execute(
        """
        SELECT COUNT(*) as n
        FROM candidate_reviews
        WHERE false_positive_reason IN ('bad_data', 'stale_data')
        """
    ).fetchone()
    if not row or row[0] < 2:
        return
    insights.append({
        "category": "data_quality",
        "message": f"{row[0]} candidates failed due to bad or stale data. "
                   "Address source data quality before changing score weights.",
        "severity": "high",
        "suggested_experiment": {
            "hypothesis": "Increase stale_data penalty in risk_score when fundamentals > 90 days old",
            "proposed_change": {"stale_data_threshold_days": 90, "stale_data_penalty": 15},
            "affected_components": ["features/risk.py"],
            "expected_effect": "Higher risk penalty for stale-data candidates, fewer bad_data false positives",
        },
    })


def propose_insights_as_experiments(
    conn: duckdb.DuckDBPyConnection,
    insights: list[dict],
    run_ids: list[str] | None = None,
) -> list[str]:
    experiment_ids = []
    for ins in insights:
        exp = ins.get("suggested_experiment")
        if not exp:
            continue
        eid = propose_experiment(
            conn,
            hypothesis=exp["hypothesis"],
            proposed_change=exp["proposed_change"],
            affected_components=exp["affected_components"],
            expected_effect=exp["expected_effect"],
            based_on_run_ids=run_ids,
        )
        experiment_ids.append(eid)
    return experiment_ids
