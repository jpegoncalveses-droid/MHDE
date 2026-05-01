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

_POSITIVE_COMPONENTS = ("cheap", "quality", "catalyst", "momentum", "sentiment")


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


def _compute_coverage(component_scores: dict[str, float | None], weights: dict) -> float:
    """Fraction of positive-weight component mass that is observed (non-null)."""
    total_positive = sum(weights.get(k, 0.0) for k in _POSITIVE_COMPONENTS)
    if total_positive == 0:
        return 0.0
    observed = sum(weights.get(k, 0.0) for k, v in component_scores.items() if v is not None)
    return observed / total_positive


def _confidence_label(coverage: float) -> str:
    if coverage >= 0.80:
        return "high"
    if coverage >= 0.50:
        return "medium"
    if coverage > 0:
        return "low"
    return "none"


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

        component_scores = {
            "cheap": cheap,
            "quality": quality,
            "catalyst": catalyst,
            "momentum": momentum,
            "sentiment": sentiment,
        }

        # Unknown is not neutral. Only sum observed components.
        # Missing components contribute 0 to the positive sum (they are simply absent).
        # Risk is the one component that defaults when absent (it is a penalty, not a signal).
        total = 0.0
        for key, score in component_scores.items():
            if score is not None:
                total += weights.get(key, 0.0) * score

        risk_s = risk_raw if risk_raw is not None else 50.0
        total -= weights.get("risk_penalty", 0.20) * risk_s
        total = max(0.0, min(100.0, total))

        # Coverage = fraction of positive-weight components with real data
        coverage = _compute_coverage(component_scores, weights)
        confidence = _confidence_label(coverage)

        missing = [k for k, v in component_scores.items() if v is None]
        tier = assign_tier(total, catalyst, risk_s, coverage=coverage)

        score_data = {
            "cheap_score": cheap,
            "quality_score": quality,
            "catalyst_score": catalyst,
            "momentum_score": momentum,
            "sentiment_score": sentiment,
            "risk_penalty": risk_s,
            "total_score": total,
            "coverage": coverage,
        }

        if tier not in ("Reject", "Incomplete"):
            why_ranked = generate_why_ranked(score_data)
            why_rejected = ""
        else:
            why_ranked = ""
            why_rejected = generate_why_rejected(score_data, missing, tier=tier)

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
                    confidence = excluded.confidence,
                    why_ranked = excluded.why_ranked,
                    why_rejected = excluded.why_rejected
                """,
                [
                    uuid.uuid4().hex[:16], run_id, ticker, as_of,
                    cheap, quality, catalyst,
                    momentum, sentiment, risk_s,
                    total, tier, confidence,
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
