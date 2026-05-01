from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

import duckdb

logger = logging.getLogger("mhde.hypotheses")


def log_rejections(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    ranked_scores: list[dict],
) -> int:
    count = 0
    for scores in ranked_scores:
        if scores.get("tier") != "Reject":
            continue
        ticker = scores["ticker"]
        reason = scores.get("why_rejected", "Below threshold")
        risk = scores.get("risk_penalty", 0)
        missing = scores.get("missing_data_json")

        try:
            conn.execute(
                """
                INSERT INTO rejections
                    (id, run_id, ticker, reason, risk_flags_json,
                     missing_data_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    uuid.uuid4().hex[:16], run_id, ticker, reason,
                    json.dumps({"risk_penalty": risk}),
                    missing if isinstance(missing, str) else json.dumps(missing or []),
                    datetime.utcnow(),
                ],
            )
            count += 1
        except Exception as exc:
            logger.debug("Rejection log failed for %s: %s", ticker, exc)

    logger.info("Logged %d rejections", count)
    return count
