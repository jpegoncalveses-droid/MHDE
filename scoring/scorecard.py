from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime

import duckdb

from scoring.tiers import assign_tier
from scoring.explanations import generate_why_ranked, generate_why_rejected

logger = logging.getLogger("mhde.scoring")

_DEFAULT_WEIGHTS = {
    "cheap": 0.30,
    "quality": 0.25,
    "catalyst": 0.25,
    "momentum": 0.10,
    "sentiment": 0.10,
    "risk_penalty": 0.20,
}

# Maps feature_group → score_dimension
_GROUP_MAP = {
    "valuation": "cheap",
    "quality": "quality",
    "catalyst": "catalyst",
    "momentum": "momentum",
    "sentiment": "sentiment",
    "risk": "risk",
}


def _avg_scores(conn: duckdb.DuckDBPyConnection, run_id: str, ticker: str, group: str) -> float | None:
    rows = conn.execute(
        """
        SELECT feature_score FROM features
        WHERE run_id = ? AND ticker = ? AND feature_group = ?
          AND feature_score IS NOT NULL
        """,
        [run_id, ticker, group],
    ).fetchall()
    if not rows:
        return None
    return sum(r[0] for r in rows) / len(rows)


def compute_scores(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    tickers: list[str],
    cfg: dict,
) -> int:
    weights = cfg.get("scoring", {}).get("weights", _DEFAULT_WEIGHTS)
    as_of = date.today()
    count = 0

    for ticker in tickers:
        cheap = _avg_scores(conn, run_id, ticker, "valuation")
        quality = _avg_scores(conn, run_id, ticker, "quality")
        catalyst = _avg_scores(conn, run_id, ticker, "catalyst")
        momentum = _avg_scores(conn, run_id, ticker, "momentum")
        sentiment = _avg_scores(conn, run_id, ticker, "sentiment")
        risk_raw = _avg_scores(conn, run_id, ticker, "risk")

        missing = []
        if cheap is None:
            missing.append("valuation")
        if quality is None:
            missing.append("quality")
        if catalyst is None:
            missing.append("catalyst")

        # Use 50 as neutral default for missing non-risk scores
        cheap_s = cheap if cheap is not None else 50.0
        quality_s = quality if quality is not None else 50.0
        catalyst_s = catalyst if catalyst is not None else 0.0
        momentum_s = momentum if momentum is not None else 50.0
        sentiment_s = sentiment if sentiment is not None else 50.0
        risk_s = risk_raw if risk_raw is not None else 50.0

        w = weights
        total = (
            w.get("cheap", 0.30) * cheap_s
            + w.get("quality", 0.25) * quality_s
            + w.get("catalyst", 0.25) * catalyst_s
            + w.get("momentum", 0.10) * momentum_s
            + w.get("sentiment", 0.10) * sentiment_s
            - w.get("risk_penalty", 0.20) * risk_s
        )
        total = max(0.0, min(100.0, total))

        insufficient = len(missing) >= 2
        tier = assign_tier(total, catalyst_s, risk_s, missing_fields=insufficient)

        score_data = {
            "cheap_score": cheap_s,
            "quality_score": quality_s,
            "catalyst_score": catalyst_s,
            "momentum_score": momentum_s,
            "sentiment_score": sentiment_s,
            "risk_penalty": risk_s,
            "total_score": total,
        }
        why_ranked = generate_why_ranked(score_data) if tier != "Reject" else ""
        why_rejected = generate_why_rejected(score_data, missing) if tier == "Reject" else ""

        try:
            conn.execute(
                """
                INSERT INTO scores
                    (id, run_id, ticker, as_of_date,
                     cheap_score, quality_score, catalyst_score,
                     momentum_score, sentiment_score, risk_penalty,
                     total_score, tier, confidence,
                     why_ranked, why_rejected, missing_data_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id, ticker) DO UPDATE SET
                    total_score = excluded.total_score,
                    tier = excluded.tier,
                    why_ranked = excluded.why_ranked,
                    why_rejected = excluded.why_rejected
                """,
                [
                    uuid.uuid4().hex[:16], run_id, ticker, as_of,
                    cheap_s, quality_s, catalyst_s,
                    momentum_s, sentiment_s, risk_s,
                    total, tier,
                    "low" if missing else "medium",
                    why_ranked, why_rejected,
                    json.dumps(missing) if missing else None,
                    datetime.utcnow(),
                ],
            )
            count += 1
        except Exception as exc:
            logger.warning("Score insert failed for %s: %s", ticker, exc)

    logger.info("Scored %d tickers", count)
    return count
