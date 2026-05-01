from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

import duckdb

from hypotheses.templates import build_thesis_text, build_why_now

logger = logging.getLogger("mhde.hypotheses")


def generate_hypotheses(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    ranked_scores: list[dict],
) -> list[dict]:
    hypotheses = []
    company_map = {
        r[0]: r[1]
        for r in conn.execute("SELECT ticker, company_name FROM companies").fetchall()
    }

    rank = 0
    for scores in ranked_scores:
        tier = scores.get("tier", "Reject")
        if tier == "Reject":
            continue

        ticker = scores["ticker"]
        company_name = company_map.get(ticker, ticker)
        rank += 1

        thesis = build_thesis_text({**scores, "ticker": ticker}, company_name)
        why_now = build_why_now(scores)

        cheap_ev = [f"Valuation score: {scores.get('cheap_score', 0):.0f}/100"]
        quality_ev = [f"Quality score: {scores.get('quality_score', 0):.0f}/100"]
        catalyst_ev = [f"Catalyst score: {scores.get('catalyst_score', 0):.0f}/100"]
        risks = [f"Risk penalty: {scores.get('risk_penalty', 0):.0f}/100"]
        missing = json.loads(scores.get("missing_data_json") or "[]") if isinstance(
            scores.get("missing_data_json"), str
        ) else []

        hyp = {
            "hypothesis_id": uuid.uuid4().hex[:16],
            "run_id": run_id,
            "ticker": ticker,
            "company_name": company_name,
            "rank": rank,
            "tier": tier,
            "total_score": scores.get("total_score"),
            "thesis": thesis,
            "why_now": why_now,
            "cheap_evidence_json": json.dumps(cheap_ev),
            "quality_evidence_json": json.dumps(quality_ev),
            "catalyst_evidence_json": json.dumps(catalyst_ev),
            "risks_json": json.dumps(risks),
            "missing_evidence_json": json.dumps(missing),
            "status": "new",
            "review_status": "pending",
        }
        hypotheses.append(hyp)

        now = datetime.utcnow()
        try:
            conn.execute(
                """
                INSERT INTO hypotheses
                    (hypothesis_id, run_id, ticker, company_name, rank, tier,
                     total_score, thesis, why_now,
                     cheap_evidence_json, quality_evidence_json, catalyst_evidence_json,
                     risks_json, missing_evidence_json, status, review_status,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    hyp["hypothesis_id"], run_id, ticker, company_name, rank, tier,
                    hyp["total_score"], thesis, why_now,
                    hyp["cheap_evidence_json"], hyp["quality_evidence_json"],
                    hyp["catalyst_evidence_json"], hyp["risks_json"],
                    hyp["missing_evidence_json"], "new", "pending", now, now,
                ],
            )
        except Exception as exc:
            logger.warning("Hypothesis insert failed for %s: %s", ticker, exc)

    logger.info("Generated %d hypotheses (A/B/C tier)", len(hypotheses))
    return hypotheses
