"""Prediction-vs-actual spike report.

Compares MHDE scores before each detected move against what actually moved.
Shadow/diagnostic only — no production scores are written.
"""
from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import duckdb

logger = logging.getLogger("mhde.missed.prediction_report")

_NEAR_THRESHOLD_MIN = 40.0
_NEAR_THRESHOLD_MAX = 45.0

_WINDOW_URGENCY: dict[int, int] = {1: 5, 3: 4, 10: 3, 20: 2, 60: 1}

_REPORT_MD = "prediction_vs_actual_report.md"
_REPORT_CSV = "prediction_vs_actual_rows.csv"
_REPORT_JSONL = "missed_spike_investigations.jsonl"

_CSV_COLS = [
    "ticker", "event_date", "event_type", "return_value", "window_days",
    "classification", "priority_score", "universe_tier",
    "score_before_event", "tier_before_event",
    "had_catalyst_evidence", "was_in_universe", "was_scored",
    "root_cause_hint",
]

_QUERY = """
SELECT m.ticker, m.event_date, m.event_type, m.return_value, m.window_days,
       m.was_in_universe, m.was_scored, m.score_before_event,
       m.tier_before_event, m.had_catalyst_evidence,
       c.universe_tier
FROM missed_opportunity_events m
LEFT JOIN companies c ON m.ticker = c.ticker
WHERE m.event_date >= ?
ORDER BY m.event_date DESC
"""

_QUERY_COLS = [
    "ticker", "event_date", "event_type", "return_value", "window_days",
    "was_in_universe", "was_scored", "score_before_event",
    "tier_before_event", "had_catalyst_evidence", "universe_tier",
]


def classify_row(row: dict) -> str:
    """Assign one of 6 classification labels to a detected spike event."""
    if not row.get("was_in_universe"):
        return "universe_miss"
    if not row.get("was_scored"):
        return "unscored_mover"
    score = row.get("score_before_event")
    if score is not None and _NEAR_THRESHOLD_MIN <= score < _NEAR_THRESHOLD_MAX:
        return "near_threshold"
    tier = row.get("tier_before_event") or ""
    if tier in ("A", "B"):
        return "scored_correct"
    if tier == "C":
        return "scored_missed"
    return "true_miss"


def _root_cause_hint(classification: str, row: dict) -> str:
    if classification == "universe_miss":
        return "universe_gap"
    if classification == "unscored_mover":
        return "data_gap"
    if classification == "near_threshold":
        return "near_threshold"
    if classification == "true_miss":
        return "scoring_blind_spot"
    if classification == "scored_missed":
        return "catalyst_missed" if not row.get("had_catalyst_evidence") else "scoring_blind_spot"
    return "unknown"


def _priority_score(row: dict, classification: str) -> float:
    urgency = _WINDOW_URGENCY.get(int(row.get("window_days") or 0), 0)
    universe_bonus = (
        0.3 if row.get("universe_tier") == "primary" else
        0.1 if row.get("universe_tier") == "extended" else
        0.0
    )
    threshold_bonus = 0.2 if classification == "near_threshold" else 0.0
    return urgency + universe_bonus + threshold_bonus


def build_rows(
    conn: duckdb.DuckDBPyConnection,
    lookback_days: int = 90,
) -> list[dict]:
    """Query missed_opportunity_events + companies; return enriched, ranked rows."""
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    raw = conn.execute(_QUERY, [cutoff]).fetchall()

    result: list[dict] = []
    for r in raw:
        row = dict(zip(_QUERY_COLS, r))
        classification = classify_row(row)
        priority = _priority_score(row, classification)
        row["classification"] = classification
        row["priority_score"] = round(priority, 3)
        row["root_cause_hint"] = _root_cause_hint(classification, row)
        result.append(row)

    result.sort(key=lambda r: -r["priority_score"])
    return result
