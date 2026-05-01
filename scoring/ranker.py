from __future__ import annotations

import duckdb


def rank_tickers(conn: duckdb.DuckDBPyConnection, run_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT ticker, tier, total_score, cheap_score, quality_score,
               catalyst_score, momentum_score, sentiment_score, risk_penalty,
               why_ranked, why_rejected
        FROM scores
        WHERE run_id = ?
        ORDER BY total_score DESC
        """,
        [run_id],
    ).fetchall()

    cols = [
        "ticker", "tier", "total_score", "cheap_score", "quality_score",
        "catalyst_score", "momentum_score", "sentiment_score", "risk_penalty",
        "why_ranked", "why_rejected",
    ]
    return [dict(zip(cols, r)) for r in rows]
