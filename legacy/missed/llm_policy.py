"""LLM activation policy for missed-opportunity investigations.

Principles:
- Deterministic first: numeric root causes do NOT trigger LLM.
- NVIDIA: text-related root causes (catalyst_not_classified, routine_filing_misclassified, etc.)
- OpenAI: limited to top MAX_OPENAI_EVENTS complex cases.
- Auto-enrichment requires 5+ text-related misses/week OR >30% text-related share.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import duckdb

from missed.labels import NUMERIC_ROOT_CAUSES, TEXT_RELATED_ROOT_CAUSES, FILING_RELATED_ROOT_CAUSES

logger = logging.getLogger("mhde.missed.llm_policy")

MAX_OPENAI_EVENTS = 10

_NVIDIA_ELIGIBLE_CAUSES = TEXT_RELATED_ROOT_CAUSES | FILING_RELATED_ROOT_CAUSES

_AUTO_ENRICHMENT_MIN_WEEKLY_TEXT = 5
_AUTO_ENRICHMENT_MIN_SHARE = 0.30


def is_text_enrichment_needed(conn: duckdb.DuckDBPyConnection, investigation_id: str) -> bool:
    """Return True only when root cause is text-related (not numeric/structural)."""
    row = conn.execute(
        "SELECT primary_root_cause FROM missed_opportunity_investigations WHERE investigation_id=?",
        [investigation_id],
    ).fetchone()
    if not row or not row[0]:
        return False
    return row[0] in TEXT_RELATED_ROOT_CAUSES


def get_nvidia_eligible_investigations(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """
    Return investigations eligible for NVIDIA enrichment:
    - text-related root cause AND had_catalyst_evidence=True (filing existed before move)
    - nvidia_enrichment_status = 'not_needed' or 'queued' (not yet completed)
    """
    rows = conn.execute(
        """SELECT i.investigation_id, i.ticker, i.event_date, i.primary_root_cause,
                  e.had_catalyst_evidence
           FROM missed_opportunity_investigations i
           JOIN missed_opportunity_events e ON i.event_id = e.event_id
           WHERE i.nvidia_enrichment_status IN ('not_needed', 'queued')
             AND e.had_catalyst_evidence = true
           ORDER BY i.created_at DESC"""
    ).fetchall()

    result = []
    for r in rows:
        inv_id, ticker, event_date, root_cause, had_cat = r
        if root_cause in _NVIDIA_ELIGIBLE_CAUSES:
            result.append({
                "investigation_id": inv_id,
                "ticker": ticker,
                "event_date": event_date,
                "primary_root_cause": root_cause,
            })
    return result


def get_openai_critique_candidates(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """
    Return at most MAX_OPENAI_EVENTS top complex investigations for OpenAI critique.
    Selects investigations where NVIDIA enrichment completed or text evidence is ambiguous.
    """
    rows = conn.execute(
        """SELECT investigation_id, ticker, event_date, primary_root_cause
           FROM missed_opportunity_investigations
           WHERE primary_root_cause IN ('catalyst_not_classified', 'routine_filing_misclassified')
             AND openai_critique_status = 'not_needed'
           ORDER BY created_at DESC
           LIMIT ?""",
        [MAX_OPENAI_EVENTS],
    ).fetchall()
    cols = ["investigation_id", "ticker", "event_date", "primary_root_cause"]
    return [dict(zip(cols, r)) for r in rows]


def should_enable_auto_enrichment(conn: duckdb.DuckDBPyConnection) -> bool:
    """
    Return True if auto NVIDIA enrichment should be enabled based on:
    - 5+ text-related misses this week, OR
    - >30% of this week's misses are text-related.
    """
    week_ago = (date.today() - timedelta(days=7)).isoformat()

    total_row = conn.execute(
        "SELECT COUNT(*) FROM missed_opportunity_investigations WHERE created_at >= ?",
        [week_ago],
    ).fetchone()
    total = total_row[0] if total_row else 0
    if total == 0:
        return False

    text_row = conn.execute(
        """SELECT COUNT(*) FROM missed_opportunity_investigations
           WHERE created_at >= ? AND primary_root_cause IN ({})""".format(
            ",".join(f"'{c}'" for c in TEXT_RELATED_ROOT_CAUSES)
        ),
        [week_ago],
    ).fetchone()
    text_count = text_row[0] if text_row else 0

    if text_count >= _AUTO_ENRICHMENT_MIN_WEEKLY_TEXT:
        return True
    # Share threshold only meaningful with a sufficient sample
    if total >= _AUTO_ENRICHMENT_MIN_WEEKLY_TEXT and text_count / total >= _AUTO_ENRICHMENT_MIN_SHARE:
        return True
    return False
