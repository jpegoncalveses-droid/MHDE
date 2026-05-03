from __future__ import annotations

import csv
import json
import logging
import os
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

_GROUP_TO_COMPONENT = {
    "valuation": "cheap",
    "quality": "quality",
    "catalyst": "catalyst",
    "momentum": "momentum",
    "sentiment": "sentiment",
    "risk": "risk_penalty",
}


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

    # Single bulk query: average feature_score per ticker per group
    bulk_rows = conn.execute(
        """
        SELECT ticker, feature_group, AVG(feature_score) AS avg_score
        FROM features
        WHERE run_id = ? AND feature_score IS NOT NULL AND ticker IS NOT NULL
        GROUP BY ticker, feature_group
        """,
        [run_id],
    ).fetchall()

    # Index: ticker -> {component_key: avg_score}
    ticker_scores: dict[str, dict[str, float]] = {}
    for ticker, group, avg in bulk_rows:
        comp = _GROUP_TO_COMPONENT.get(group)
        if comp:
            ticker_scores.setdefault(ticker, {})[comp] = avg

    score_rows: list[list] = []

    for ticker in tickers:
        scores = ticker_scores.get(ticker, {})

        cheap = scores.get("cheap")
        quality = scores.get("quality")
        catalyst = scores.get("catalyst")
        momentum = scores.get("momentum")
        sentiment = scores.get("sentiment")
        risk_raw = scores.get("risk_penalty")

        component_scores = {
            "cheap": cheap,
            "quality": quality,
            "catalyst": catalyst,
            "momentum": momentum,
            "sentiment": sentiment,
        }

        # Unknown is not neutral. Only sum observed components.
        total = 0.0
        for key, score in component_scores.items():
            if score is not None:
                total += weights.get(key, 0.0) * score

        risk_s = risk_raw if risk_raw is not None else 50.0
        total -= weights.get("risk_penalty", 0.20) * risk_s
        total = max(0.0, min(100.0, total))

        coverage = _compute_coverage(component_scores, weights)
        confidence = _confidence_label(coverage)
        observed_count = sum(1 for v in component_scores.values() if v is not None)

        missing = [k for k, v in component_scores.items() if v is None]
        tier = assign_tier(total, catalyst, risk_s, coverage=coverage,
                           observed_count=observed_count)

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

        score_rows.append([
            uuid.uuid4().hex[:16], run_id, ticker, as_of,
            cheap, quality, catalyst,
            momentum, sentiment, risk_s,
            total, tier, confidence,
            why_ranked, why_rejected,
            json.dumps(missing) if missing else None,
            datetime.utcnow(),
        ])

    if score_rows:
        try:
            conn.executemany(
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
                score_rows,
            )
        except Exception as exc:
            logger.warning("Scores batch insert failed: %s", exc)
            return 0

    count = len(score_rows)
    logger.info("Scored %d tickers", count)
    return count


_SCORE_COMPONENTS_CSV = "latest_score_components.csv"
_SCORE_COMPONENTS_COLS = [
    "ticker", "total_score", "tier",
    "cheap_score", "quality_score", "catalyst_score",
    "momentum_score", "sentiment_score", "risk_penalty",
    "confidence",
]


def export_score_components(
    conn: duckdb.DuckDBPyConnection,
    output_dir: str,
) -> str:
    """Export component scores for all tickers in the latest run to CSV.

    Returns the path to the written CSV file.
    """
    try:
        rows = conn.execute(
            """
            SELECT ticker, total_score, tier,
                   cheap_score, quality_score, catalyst_score,
                   momentum_score, sentiment_score, risk_penalty,
                   confidence
            FROM scores
            ORDER BY total_score DESC
            """
        ).fetchall()
    except Exception as exc:
        logger.warning("export_score_components query failed: %s", exc)
        rows = []

    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, _SCORE_COMPONENTS_CSV)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_SCORE_COMPONENTS_COLS)
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(zip(_SCORE_COMPONENTS_COLS, row)))

    logger.info("Exported %d score component rows to %s", len(rows), path)
    return path
