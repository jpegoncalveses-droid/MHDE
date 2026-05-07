"""Review importer — loads completed review fields from a packet JSON into candidate_reviews."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

import duckdb

from learning.error_taxonomy import FALSE_POSITIVE_REASONS, REVIEW_STATUSES

logger = logging.getLogger("mhde.review")


def import_packet(conn: duckdb.DuckDBPyConnection, packet_path: str) -> dict:
    """
    Reads a review packet JSON and imports any non-pending reviews into candidate_reviews.

    Only candidates with review_status != 'pending' are imported.
    Skips candidates whose ticker+run_id is already in candidate_reviews.

    Returns a summary dict: {imported, skipped_pending, skipped_duplicate, failed, run_id}.
    """
    path = Path(packet_path)
    if not path.exists():
        raise FileNotFoundError(f"Review packet not found: {packet_path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    run_id = data.get("run_id")
    if not run_id:
        raise ValueError("Packet JSON missing 'run_id' field.")

    imported = skipped_pending = skipped_dup = failed = 0

    for section_candidates in data.get("sections", {}).values():
        for c in section_candidates:
            review = c.get("review", {})
            status = review.get("review_status", "pending")

            if status == "pending":
                skipped_pending += 1
                continue

            ticker = c.get("ticker")
            if not ticker:
                failed += 1
                continue

            # Dedup: skip if already reviewed for this run
            existing = conn.execute(
                "SELECT review_id FROM candidate_reviews WHERE run_id=? AND ticker=? LIMIT 1",
                [run_id, ticker],
            ).fetchone()
            if existing:
                skipped_dup += 1
                continue

            try:
                _validate_review(review)
                conn.execute(
                    """
                    INSERT INTO candidate_reviews
                        (review_id, run_id, ticker, review_status, usefulness_score,
                         thesis_quality_score, evidence_quality_score,
                         false_positive_reason, missed_risk, missing_evidence,
                         review_notes, reviewed_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        uuid.uuid4().hex[:16], run_id, ticker,
                        status,
                        review.get("usefulness_score"),
                        review.get("thesis_quality_score"),
                        review.get("evidence_quality_score"),
                        review.get("false_positive_reason"),
                        review.get("missed_risk"),
                        review.get("missing_evidence"),
                        review.get("review_notes"),
                        "import",
                        datetime.utcnow(),
                    ],
                )
                imported += 1
            except Exception as exc:
                logger.warning("Review import failed for %s: %s", ticker, exc)
                failed += 1

    logger.info(
        "Review import complete: %d imported, %d skipped (pending), %d skipped (dup), %d failed",
        imported, skipped_pending, skipped_dup, failed,
    )
    return {
        "run_id": run_id,
        "imported": imported,
        "skipped_pending": skipped_pending,
        "skipped_duplicate": skipped_dup,
        "failed": failed,
    }


def _validate_review(review: dict) -> None:
    status = review.get("review_status")
    if status and status not in REVIEW_STATUSES:
        raise ValueError(f"Invalid review_status: {status!r}")
    fp = review.get("false_positive_reason")
    if fp and fp not in FALSE_POSITIVE_REASONS:
        raise ValueError(f"Invalid false_positive_reason: {fp!r}")
    for field in ("usefulness_score", "thesis_quality_score", "evidence_quality_score"):
        val = review.get(field)
        if val is not None and val not in (1, 2, 3, 4, 5):
            raise ValueError(f"Invalid {field}: {val!r} (must be 1-5 or null)")
