from __future__ import annotations

import uuid
from datetime import datetime

import duckdb

from learning.error_taxonomy import FALSE_POSITIVE_REASONS, REVIEW_STATUSES, SCORE_RANGE


def submit_review(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    ticker: str,
    review_status: str,
    usefulness_score: int | None = None,
    thesis_quality_score: int | None = None,
    evidence_quality_score: int | None = None,
    false_positive_reason: str | None = None,
    missed_risk: str | None = None,
    missing_evidence: str | None = None,
    review_notes: str | None = None,
    reviewed_by: str | None = None,
    candidate_id: str | None = None,
) -> str:
    if review_status not in REVIEW_STATUSES:
        raise ValueError(f"Invalid review_status: {review_status}. Must be one of {REVIEW_STATUSES}")
    if usefulness_score is not None and not (SCORE_RANGE[0] <= usefulness_score <= SCORE_RANGE[1]):
        raise ValueError(f"usefulness_score must be between {SCORE_RANGE[0]} and {SCORE_RANGE[1]}")
    if thesis_quality_score is not None and not (SCORE_RANGE[0] <= thesis_quality_score <= SCORE_RANGE[1]):
        raise ValueError(f"thesis_quality_score must be between {SCORE_RANGE[0]} and {SCORE_RANGE[1]}")
    if evidence_quality_score is not None and not (SCORE_RANGE[0] <= evidence_quality_score <= SCORE_RANGE[1]):
        raise ValueError(f"evidence_quality_score must be between {SCORE_RANGE[0]} and {SCORE_RANGE[1]}")
    if false_positive_reason is not None and false_positive_reason not in FALSE_POSITIVE_REASONS:
        raise ValueError(f"Invalid false_positive_reason: {false_positive_reason}")

    review_id = uuid.uuid4().hex[:16]
    now = datetime.utcnow()

    conn.execute(
        """
        INSERT INTO candidate_reviews (
            review_id, candidate_id, run_id, ticker, review_status,
            usefulness_score, thesis_quality_score, evidence_quality_score,
            false_positive_reason, missed_risk, missing_evidence,
            review_notes, reviewed_by, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (run_id, ticker) DO UPDATE SET
            review_status = excluded.review_status,
            usefulness_score = excluded.usefulness_score,
            thesis_quality_score = excluded.thesis_quality_score,
            evidence_quality_score = excluded.evidence_quality_score,
            false_positive_reason = excluded.false_positive_reason,
            missed_risk = excluded.missed_risk,
            missing_evidence = excluded.missing_evidence,
            review_notes = excluded.review_notes,
            reviewed_by = excluded.reviewed_by,
            updated_at = excluded.updated_at
        """,
        [
            review_id, candidate_id, run_id, ticker, review_status,
            usefulness_score, thesis_quality_score, evidence_quality_score,
            false_positive_reason, missed_risk, missing_evidence,
            review_notes, reviewed_by, now, now,
        ],
    )
    return review_id


def get_reviews(conn: duckdb.DuckDBPyConnection, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        """
        SELECT review_id, candidate_id, run_id, ticker, review_status,
               usefulness_score, thesis_quality_score, evidence_quality_score,
               false_positive_reason, missed_risk, missing_evidence,
               review_notes, reviewed_by, created_at, updated_at
        FROM candidate_reviews
        ORDER BY created_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [
        "review_id", "candidate_id", "run_id", "ticker", "review_status",
        "usefulness_score", "thesis_quality_score", "evidence_quality_score",
        "false_positive_reason", "missed_risk", "missing_evidence",
        "review_notes", "reviewed_by", "created_at", "updated_at",
    ]
    return [dict(zip(cols, r)) for r in rows]
