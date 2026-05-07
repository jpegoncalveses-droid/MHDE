"""Missed-opportunity investigator.

For each detected missed event, determine WHY the engine missed it.
Critical invariant: only use data with as_of_date/filing_date BEFORE the event_date.
No post-event data is used to explain pre-event prediction quality.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date, timedelta

import duckdb

from missed.labels import (
    NUMERIC_ROOT_CAUSES,
    TEXT_RELATED_ROOT_CAUSES,
    THRESHOLD_TOO_STRICT_MIN_SCORE,
)

logger = logging.getLogger("mhde.missed.investigator")


def investigate_event(conn: duckdb.DuckDBPyConnection, event: dict) -> dict:
    """
    Investigate a single missed-opportunity event.
    Returns investigation dict. Caller decides whether to persist.
    """
    ticker = event["ticker"]
    event_date = event["event_date"]
    if isinstance(event_date, str):
        event_date = date.fromisoformat(event_date)

    root_causes: list[str] = []

    # Check 1: universe
    if not event.get("was_in_universe"):
        root_causes.append("not_in_universe")

    # Check 2: price data before event
    if not _has_price_data_before(conn, ticker, event_date):
        root_causes.append("missing_price_data")

    # Check 3: fundamentals before event
    if not _has_fundamentals_before(conn, ticker, event_date):
        root_causes.append("missing_fundamentals")

    # Check 4: score exists but threshold was strict
    score_before = event.get("score_before_event")
    tier_before = event.get("tier_before_event")
    if (score_before is not None
            and score_before >= THRESHOLD_TOO_STRICT_MIN_SCORE
            and tier_before == "Reject"):
        root_causes.append("threshold_too_strict")

    # Check 5: classify catalyst root cause precisely
    has_filing = _has_filing_before(conn, ticker, event_date)
    catalyst_was_low = _catalyst_score_was_low(conn, ticker, event_date)
    if has_filing and catalyst_was_low:
        if _has_material_filing_before(conn, ticker, event_date):
            root_causes.append("text_evidence_available_not_classified")
            root_causes.append("needs_llm_text_enrichment")
        else:
            root_causes.append("routine_event_correctly_suppressed")
    elif not event.get("had_catalyst_evidence") and not has_filing:
        if event.get("was_scored"):
            root_causes.append("no_public_catalyst_source_found")
        else:
            root_causes.append("price_move_without_known_catalyst")

    # Fallback: if no specific cause found and not in universe
    if not root_causes:
        root_causes.append("truly_unpredictable")

    primary = root_causes[0]
    text_needed = any(rc in TEXT_RELATED_ROOT_CAUSES for rc in root_causes)
    text_reason = None
    if text_needed:
        text_reasons = [rc for rc in root_causes if rc in TEXT_RELATED_ROOT_CAUSES]
        text_reason = "; ".join(text_reasons)

    # Determine nvidia/openai enrichment status
    nvidia_status = "not_needed"
    openai_status = "not_needed"
    if text_needed:
        nvidia_status = "queued" if has_filing else "not_needed"

    return {
        "event_id": event["event_id"],
        "ticker": ticker,
        "event_date": event_date,
        "root_causes": root_causes,
        "primary_root_cause": primary,
        "text_enrichment_needed": text_needed,
        "text_enrichment_reason": text_reason,
        "text_evidence_available": has_filing,
        "nvidia_enrichment_status": nvidia_status,
        "nvidia_summary_id": None,
        "openai_critique_status": openai_status,
        "openai_critique_id": None,
        "summary": _build_summary(ticker, event_date, root_causes, score_before, tier_before),
        "experiment_proposed": False,
        "experiment_id": None,
    }


def persist_investigation(conn: duckdb.DuckDBPyConnection, inv: dict) -> None:
    """Write investigation to DB and update parent event status."""
    inv_id = uuid.uuid4().hex[:16]
    event_date = inv["event_date"]
    if isinstance(event_date, date):
        event_date = event_date.isoformat()

    conn.execute(
        """INSERT OR IGNORE INTO missed_opportunity_investigations
           (investigation_id, event_id, ticker, event_date, root_causes_json,
            primary_root_cause, text_enrichment_needed, text_enrichment_reason,
            text_evidence_available, nvidia_enrichment_status, nvidia_summary_id,
            openai_critique_status, openai_critique_id, summary,
            experiment_proposed, experiment_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [inv_id, inv["event_id"], inv["ticker"], event_date,
         json.dumps(inv["root_causes"]), inv["primary_root_cause"],
         inv["text_enrichment_needed"], inv.get("text_enrichment_reason"),
         inv.get("text_evidence_available"), inv["nvidia_enrichment_status"],
         inv.get("nvidia_summary_id"), inv["openai_critique_status"],
         inv.get("openai_critique_id"), inv.get("summary"),
         inv.get("experiment_proposed", False), inv.get("experiment_id")],
    )
    # Persist individual root cause rows
    for rc in inv["root_causes"]:
        rc_id = uuid.uuid4().hex[:16]
        conn.execute(
            """INSERT OR IGNORE INTO missed_opportunity_root_causes
               (rc_id, investigation_id, ticker, event_date, root_cause)
               VALUES (?, ?, ?, ?, ?)""",
            [rc_id, inv_id, inv["ticker"], event_date, rc],
        )
    conn.execute(
        "UPDATE missed_opportunity_events SET investigation_status='investigated' WHERE event_id=?",
        [inv["event_id"]],
    )


def investigate_all_pending(conn: duckdb.DuckDBPyConnection) -> int:
    """Investigate all events with investigation_status='pending'."""
    rows = conn.execute(
        """SELECT event_id, ticker, event_date, event_type, return_value, window_days,
                  was_in_universe, was_scored, score_before_event, tier_before_event,
                  was_rejected, was_incomplete, had_catalyst_evidence
           FROM missed_opportunity_events
           WHERE investigation_status = 'pending'
           ORDER BY event_date DESC"""
    ).fetchall()

    cols = ["event_id", "ticker", "event_date", "event_type", "return_value", "window_days",
            "was_in_universe", "was_scored", "score_before_event", "tier_before_event",
            "was_rejected", "was_incomplete", "had_catalyst_evidence"]
    investigated = 0
    for row in rows:
        event = dict(zip(cols, row))
        inv = investigate_event(conn, event)
        persist_investigation(conn, inv)
        investigated += 1
    return investigated


# ── Private helpers (as-of queries only — no hindsight) ──────────────────────

def _has_price_data_before(conn: duckdb.DuckDBPyConnection, ticker: str, event_date: date) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM prices_daily WHERE ticker=? AND trade_date < ?",
        [ticker, event_date.isoformat()],
    ).fetchone()
    return bool(row and row[0] >= 5)


def _has_fundamentals_before(conn: duckdb.DuckDBPyConnection, ticker: str, event_date: date) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM fundamentals_raw WHERE ticker=? AND as_of_date < ?",
        [ticker, event_date.isoformat()],
    ).fetchone()
    return bool(row and row[0] > 0)


def _has_filing_before(conn: duckdb.DuckDBPyConnection, ticker: str, event_date: date) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM filings WHERE ticker=? AND filing_date < ? AND filing_date >= ?",
        [ticker, event_date.isoformat(),
         (event_date - timedelta(days=60)).isoformat()],
    ).fetchone()
    return bool(row and row[0] > 0)


_ROUTINE_FORM_TYPES = frozenset([
    "4", "4/A",
    "SC 13G", "SC 13G/A", "SC 13D", "SC 13D/A",
    "DEF 14A", "DEFA14A", "PRE 14A",
    "ARS", "SD",
])


def _has_material_filing_before(
    conn: duckdb.DuckDBPyConnection, ticker: str, event_date: date
) -> bool:
    """True if any non-routine (material) filing exists in the 60 days before the event."""
    rows = conn.execute(
        "SELECT DISTINCT form_type FROM filings WHERE ticker=? AND filing_date < ? AND filing_date >= ?",
        [ticker, event_date.isoformat(), (event_date - timedelta(days=60)).isoformat()],
    ).fetchall()
    form_types = {r[0] for r in rows}
    return bool(form_types - _ROUTINE_FORM_TYPES)


def _catalyst_score_was_low(
    conn: duckdb.DuckDBPyConnection, ticker: str, event_date: date
) -> bool:
    row = conn.execute(
        """SELECT catalyst_score FROM scores
           WHERE ticker=? AND as_of_date < ?
           ORDER BY as_of_date DESC LIMIT 1""",
        [ticker, event_date.isoformat()],
    ).fetchone()
    if row and row[0] is not None:
        return row[0] < 20.0
    return True  # no catalyst score = effectively low


def _build_summary(
    ticker: str,
    event_date: date,
    root_causes: list[str],
    score_before: float | None,
    tier_before: str | None,
) -> str:
    score_str = f"{score_before:.1f}" if score_before is not None else "N/A"
    tier_str = tier_before or "N/A"
    return (
        f"{ticker} missed event on {event_date}: "
        f"root_causes={root_causes}, "
        f"score_before={score_str}, tier_before={tier_str}"
    )
