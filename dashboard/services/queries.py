from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import duckdb


def _connect() -> duckdb.DuckDBPyConnection:
    import os
    db_path = os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb")
    return duckdb.connect(db_path, read_only=True)


def get_latest_run_id(conn: duckdb.DuckDBPyConnection) -> str | None:
    rows = conn.execute(
        "SELECT run_id FROM scores ORDER BY created_at DESC LIMIT 1"
    ).fetchall()
    return rows[0][0] if rows else None


def get_overview_stats(conn: duckdb.DuckDBPyConnection) -> dict:
    run_id = get_latest_run_id(conn)
    universe = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE is_active = true"
    ).fetchone()[0]

    candidates_scored = 0
    tier_counts: dict[str, int] = {}
    if run_id:
        candidates_scored = conn.execute(
            "SELECT COUNT(*) FROM scores WHERE run_id = ?", [run_id]
        ).fetchone()[0]
        tiers = conn.execute(
            "SELECT tier, COUNT(*) FROM scores WHERE run_id = ? GROUP BY tier", [run_id]
        ).fetchall()
        tier_counts = dict(tiers)

    source_fails = conn.execute(
        "SELECT COUNT(*) FROM source_runs WHERE status = 'error'"
    ).fetchone()[0]

    alerts_sent = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE status = 'sent'"
    ).fetchone()[0]

    health_warns = conn.execute(
        "SELECT COUNT(*) FROM health_checks WHERE status IN ('warn', 'fail')"
    ).fetchone()[0]

    feature_coverage = conn.execute(
        """
        SELECT
            COUNT(CASE WHEN feature_value IS NOT NULL THEN 1 END) * 100.0 / COUNT(*)
        FROM features
        WHERE run_id = ?
        """,
        [run_id or ""],
    ).fetchone()[0] if run_id else None

    return {
        "run_id": run_id,
        "universe_size": universe,
        "candidates_scored": candidates_scored,
        "tier_a": tier_counts.get("A", 0),
        "tier_b": tier_counts.get("B", 0),
        "tier_c": tier_counts.get("C", 0),
        "rejected": tier_counts.get("Reject", 0),
        "source_failures": source_fails,
        "alerts_sent": alerts_sent,
        "health_warnings": health_warns,
        "feature_coverage_pct": feature_coverage,
    }


def get_candidates(
    conn: duckdb.DuckDBPyConnection,
    run_id: str | None = None,
    tier: str | None = None,
    min_score: float = 0,
    max_score: float = 100,
    search: str | None = None,
) -> list[dict]:
    if not run_id:
        run_id = get_latest_run_id(conn)
    if not run_id:
        return []

    query = """
        SELECT s.ticker, c.company_name, s.tier, s.total_score, s.cheap_score,
               s.quality_score, s.catalyst_score, s.momentum_score, s.sentiment_score,
               s.risk_penalty, s.confidence, s.why_ranked, s.missing_data_json, s.run_id,
               s.as_of_date
        FROM scores s
        LEFT JOIN companies c ON s.ticker = c.ticker
        WHERE s.run_id = ?
          AND s.total_score >= ? AND s.total_score <= ?
    """
    params: list = [run_id, min_score, max_score]

    if tier:
        query += " AND s.tier = ?"
        params.append(tier)
    if search:
        query += " AND (s.ticker ILIKE ? OR c.company_name ILIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])

    query += " ORDER BY s.total_score DESC"
    rows = conn.execute(query, params).fetchall()
    cols = [
        "ticker", "company_name", "tier", "total_score", "cheap_score",
        "quality_score", "catalyst_score", "momentum_score", "sentiment_score",
        "risk_penalty", "confidence", "why_ranked", "missing_data_json", "run_id", "as_of_date",
    ]
    result = []
    for r in rows:
        d = dict(zip(cols, r))
        if d.get("missing_data_json"):
            try:
                d["missing_data"] = json.loads(d["missing_data_json"])
            except Exception:
                d["missing_data"] = []
        result.append(d)
    return result


def get_candidate_detail(
    conn: duckdb.DuckDBPyConnection, ticker: str, run_id: str | None = None
) -> dict:
    if not run_id:
        run_id = get_latest_run_id(conn)

    score_row = conn.execute(
        """
        SELECT s.*, c.company_name, c.sector, c.industry, c.cik
        FROM scores s LEFT JOIN companies c ON s.ticker = c.ticker
        WHERE s.ticker = ? AND s.run_id = ?
        """,
        [ticker, run_id],
    ).fetchone()

    hyp = conn.execute(
        """
        SELECT thesis, why_now, cheap_evidence_json, quality_evidence_json,
               catalyst_evidence_json, risks_json, missing_evidence_json, status
        FROM hypotheses WHERE ticker = ? AND run_id = ?
        """,
        [ticker, run_id],
    ).fetchone()

    llm = conn.execute(
        """
        SELECT output_json, provider, model, status, error_message
        FROM llm_runs WHERE ticker = ? AND run_id = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        [ticker, run_id],
    ).fetchone()

    features_rows = conn.execute(
        """
        SELECT feature_group, feature_name, feature_value, feature_score, confidence
        FROM features WHERE ticker = ? AND run_id = ?
        ORDER BY feature_group, feature_name
        """,
        [ticker, run_id],
    ).fetchall()

    prices = conn.execute(
        """
        SELECT trade_date, close, volume FROM prices_daily
        WHERE ticker = ? ORDER BY trade_date DESC LIMIT 90
        """,
        [ticker],
    ).fetchall()

    outcome = conn.execute(
        """
        SELECT forward_return_20d, forward_return_60d, max_drawdown_20d,
               max_runup_20d, review_status, review_notes
        FROM candidate_outcomes WHERE ticker = ? AND run_id = ?
        """,
        [ticker, run_id],
    ).fetchone()

    def parse_json(s):
        if not s:
            return []
        try:
            return json.loads(s)
        except Exception:
            return []

    detail: dict = {}
    if score_row:
        cols = [d[0] for d in conn.description]
        detail.update(dict(zip(cols, score_row)))

    if hyp:
        detail["thesis"] = hyp[0]
        detail["why_now"] = hyp[1]
        detail["cheap_evidence"] = parse_json(hyp[2])
        detail["quality_evidence"] = parse_json(hyp[3])
        detail["catalyst_evidence"] = parse_json(hyp[4])
        detail["risks"] = parse_json(hyp[5])
        detail["missing_evidence"] = parse_json(hyp[6])
        detail["hypothesis_status"] = hyp[7]

    if llm:
        try:
            llm_data = json.loads(llm[0]) if llm[0] else {}
        except Exception:
            llm_data = {}
        detail["llm_thesis"] = llm_data.get("thesis", "")
        detail["llm_confidence"] = llm_data.get("confidence", "")
        detail["llm_action"] = llm_data.get("recommended_action", "")
        detail["llm_provider"] = llm[1]
        detail["llm_model"] = llm[2]
        detail["llm_status"] = llm[3]
        detail["llm_error"] = llm[4]

    detail["features"] = [
        {"group": r[0], "name": r[1], "value": r[2], "score": r[3], "confidence": r[4]}
        for r in features_rows
    ]
    detail["prices"] = [
        {"date": r[0], "close": r[1], "volume": r[2]} for r in reversed(prices)
    ]

    if outcome:
        detail["outcome"] = {
            "forward_return_20d": outcome[0],
            "forward_return_60d": outcome[1],
            "max_drawdown_20d": outcome[2],
            "max_runup_20d": outcome[3],
            "review_status": outcome[4],
            "review_notes": outcome[5],
        }

    return detail


def get_source_health(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT source_name, status, COUNT(*) as runs,
               SUM(records_inserted) as total_inserted,
               MAX(finished_at) as last_run,
               COUNT(CASE WHEN status = 'error' THEN 1 END) as errors
        FROM source_runs
        GROUP BY source_name, status
        ORDER BY source_name, status
        """
    ).fetchall()
    cols = ["source_name", "status", "runs", "total_inserted", "last_run", "errors"]
    return [dict(zip(cols, r)) for r in rows]


def get_llm_runs(conn: duckdb.DuckDBPyConnection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """
        SELECT llm_run_id, ticker, provider, model, job_type, prompt_version,
               estimated_tokens, estimated_cost, status, error_message, created_at
        FROM llm_runs ORDER BY created_at DESC LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [
        "llm_run_id", "ticker", "provider", "model", "job_type", "prompt_version",
        "estimated_tokens", "estimated_cost", "status", "error_message", "created_at",
    ]
    return [dict(zip(cols, r)) for r in rows]


def get_outcomes(conn: duckdb.DuckDBPyConnection, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        """
        SELECT candidate_id, ticker, as_of_date, tier, total_score, reference_price,
               forward_return_20d, forward_return_60d, max_drawdown_20d, max_runup_20d,
               hit_10pct_before_down_10pct, review_status, review_notes
        FROM candidate_outcomes
        ORDER BY as_of_date DESC, total_score DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [
        "candidate_id", "ticker", "as_of_date", "tier", "total_score", "reference_price",
        "forward_return_20d", "forward_return_60d", "max_drawdown_20d", "max_runup_20d",
        "hit_10pct_before_down_10pct", "review_status", "review_notes",
    ]
    return [dict(zip(cols, r)) for r in rows]


def get_health_checks(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT check_name, status, severity, message, created_at
        FROM health_checks ORDER BY created_at DESC LIMIT 50
        """
    ).fetchall()
    cols = ["check_name", "status", "severity", "message", "created_at"]
    return [dict(zip(cols, r)) for r in rows]


def get_backtest_runs(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT backtest_run_id, as_of_date, tickers_tested, hit_rate, avg_return,
               warning, status, created_at
        FROM backtest_runs ORDER BY created_at DESC LIMIT 20
        """
    ).fetchall()
    cols = [
        "backtest_run_id", "as_of_date", "tickers_tested", "hit_rate",
        "avg_return", "warning", "status", "created_at",
    ]
    return [dict(zip(cols, r)) for r in rows]


def get_alerts(conn: duckdb.DuckDBPyConnection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """
        SELECT alert_id, ticker, channel, alert_type, status, message, sent_at, error_message
        FROM alerts ORDER BY created_at DESC LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = ["alert_id", "ticker", "channel", "alert_type", "status", "message", "sent_at", "error_message"]
    return [dict(zip(cols, r)) for r in rows]


def get_hypotheses(conn: duckdb.DuckDBPyConnection, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        """
        SELECT hypothesis_id, ticker, company_name, tier, total_score, thesis,
               why_now, confidence, status, review_status, created_at
        FROM hypotheses
        ORDER BY created_at DESC, total_score DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [
        "hypothesis_id", "ticker", "company_name", "tier", "total_score",
        "thesis", "why_now", "confidence", "status", "review_status", "created_at",
    ]
    return [dict(zip(cols, r)) for r in rows]
