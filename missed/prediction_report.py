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

from missed.root_cause_enrichment import enrich_rows, _ROOT_CAUSE_GROUPS

logger = logging.getLogger("mhde.missed.prediction_report")

_NEAR_THRESHOLD_MIN = 40.0
_NEAR_THRESHOLD_MAX = 45.0

_WINDOW_URGENCY: dict[int, int] = {1: 5, 3: 4, 5: 3, 10: 3, 20: 2, 60: 1, 252: 1}

_REPORT_MD = "prediction_vs_actual_report.md"
_REPORT_CSV = "prediction_vs_actual_rows.csv"
_REPORT_JSONL = "missed_spike_investigations.jsonl"

_CSV_COLS = [
    "ticker", "event_date", "event_type", "return_value", "window_days",
    "classification", "priority_score", "universe_tier",
    "score_before_event", "tier_before_event",
    "had_catalyst_evidence", "was_in_universe", "was_scored",
    "root_cause_hint", "score_join_method",
]

# Deduplicate scores so multiple runs on the same date don't fan out event rows.
# Join to the latest score on or before the event date; derive was_scored from
# actual data presence rather than the stored field (which may be stale/NULL).
_QUERY = """
WITH latest_scores AS (
    SELECT ticker, as_of_date, total_score, tier
    FROM scores
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY ticker, as_of_date
        ORDER BY created_at DESC
    ) = 1
)
SELECT m.ticker, m.event_date, m.event_type, m.return_value, m.window_days,
       m.was_in_universe,
       CASE WHEN s.total_score IS NOT NULL OR m.score_before_event IS NOT NULL
            THEN true ELSE false END AS was_scored,
       COALESCE(s.total_score, m.score_before_event)  AS score_before_event,
       COALESCE(s.tier,        m.tier_before_event)   AS tier_before_event,
       m.had_catalyst_evidence,
       c.universe_tier,
       CASE WHEN s.total_score IS NOT NULL    THEN 'scores_join'
            WHEN m.score_before_event IS NOT NULL THEN 'event_stored'
            ELSE 'none' END AS score_join_method
FROM missed_opportunity_events m
LEFT JOIN companies c ON m.ticker = c.ticker
LEFT JOIN latest_scores s ON s.ticker = m.ticker
    AND s.as_of_date = (
        SELECT MAX(s2.as_of_date) FROM latest_scores s2
        WHERE s2.ticker = m.ticker AND s2.as_of_date <= m.event_date
    )
WHERE m.event_date >= ?
ORDER BY m.event_date DESC
"""

_QUERY_COLS = [
    "ticker", "event_date", "event_type", "return_value", "window_days",
    "was_in_universe", "was_scored", "score_before_event",
    "tier_before_event", "had_catalyst_evidence", "universe_tier",
    "score_join_method",
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
    """Map classification label to a deterministic root cause hint string."""
    if classification == "universe_miss":
        return "universe_gap"
    if classification == "unscored_mover":
        return "data_gap"
    if classification == "near_threshold":
        return "near_threshold"
    if classification == "true_miss":
        if row.get("tier_before_event") == "Incomplete":
            return "data_gap"
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


def _section_lines(title: str, events: list[dict]) -> list[str]:
    lines = ["---", "", f"## {title}", ""]
    if events:
        lines += [
            "| Ticker | Return | Window | Score | Tier | Universe | Classification | Root Cause |",
            "|--------|--------|--------|-------|------|----------|----------------|------------|",
        ]
        for e in events:
            score = f"{e['score_before_event']:.1f}" if e.get("score_before_event") is not None else "—"
            tier = e.get("tier_before_event") or "—"
            ut = e.get("universe_tier") or "—"
            ret_str = f"+{e['return_value']:.1f}%" if e["return_value"] >= 0 else f"{e['return_value']:.1f}%"
            lines.append(
                f"| {e['ticker']} | {ret_str} | {e['window_days']}d"
                f" | {score} | {tier} | {ut} | `{e['classification']}` | {e['root_cause_hint']} |"
            )
    else:
        lines.append("_(no events in this window)_")
    lines.append("")
    return lines


def generate_prediction_report(
    conn: duckdb.DuckDBPyConnection,
    output_dir: str = "data/processed",
    *,
    lookback_days: int = 90,
) -> tuple[Path, Path, Path]:
    """Generate prediction-vs-actual report artifacts.

    Returns (md_path, csv_path, jsonl_path). Shadow-only — no scores written.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    rows = build_rows(conn, lookback_days=lookback_days)
    enriched_rows = enrich_rows(rows, conn)
    label_counts = Counter(r["classification"] for r in enriched_rows)
    join_counts = Counter(r.get("score_join_method", "none") for r in enriched_rows)
    n_with_score = len(enriched_rows) - join_counts.get("none", 0)

    lines: list[str] = [
        "# Prediction vs Actual Spike Report",
        "",
        f"Generated: {today} | Lookback: {lookback_days}d | Total events: {len(enriched_rows)}",
        "",
        "> **Shadow-only: no production scores were changed.**",
        "",
        "---",
        "",
        "## Summary",
        "",
        "| Classification | Count |",
        "|----------------|-------|",
    ]
    for label in ("scored_correct", "scored_missed", "near_threshold",
                  "true_miss", "unscored_mover", "universe_miss"):
        lines.append(f"| `{label}` | {label_counts.get(label, 0)} |")
    lines += [
        "",
        "| Score Join Diagnostics | Count |",
        "|------------------------|-------|",
        f"| Events with prior score | {n_with_score} |",
        f"| — via scores table join | {join_counts.get('scores_join', 0)} |",
        f"| — via event stored field | {join_counts.get('event_stored', 0)} |",
        f"| Events with no score data | {join_counts.get('none', 0)} |",
        "",
    ]

    lines += _section_lines("1-Day Spikes", [r for r in enriched_rows if r.get("window_days") == 1])
    lines += _section_lines("3d / 5d Spikes", [r for r in enriched_rows if r.get("window_days") in (3, 5)])
    lines += _section_lines("Longer Windows (10d / 20d / 60d)", [r for r in enriched_rows if r.get("window_days") in (10, 20, 60)])
    lines += _section_lines("52-Week Breakouts", [r for r in enriched_rows if r.get("window_days") == 252])
    lines += _section_lines("Out-of-Universe Spikes", [r for r in enriched_rows if r["classification"] == "universe_miss"])
    lines += _section_lines("Near-Threshold Scores", [r for r in enriched_rows if r["classification"] == "near_threshold"])
    lines += _section_lines("No-Score Events", [r for r in enriched_rows if r["classification"] in ("unscored_mover", "true_miss")])

    rc_counts = Counter(r.get("enriched_root_cause", "unknown") for r in enriched_rows)
    group_counts = Counter(r.get("root_cause_group", "unknown") for r in enriched_rows)

    lines += [
        "---",
        "",
        "## Root Cause Summary",
        "",
        "| Root Cause Group | Count |",
        "|------------------|-------|",
    ]
    for group in ("data_gap", "scoring_gap", "feature_gap", "near_miss", "universe_gap", "unknown"):
        lines.append(f"| `{group}` | {group_counts.get(group, 0)} |")
    lines += [
        "",
        "| Root Cause | Count |",
        "|------------|-------|",
    ]
    for label in _ROOT_CAUSE_GROUPS:
        count = rc_counts.get(label, 0)
        if count:
            lines.append(f"| `{label}` | {count} |")
    lines.append("")

    md_path = out / _REPORT_MD
    md_path.write_text("\n".join(lines) + "\n")

    csv_path = out / _REPORT_CSV
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_COLS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(enriched_rows)

    jsonl_path = out / _REPORT_JSONL
    with open(jsonl_path, "w") as f:
        for r in enriched_rows:
            f.write(json.dumps(r, default=str) + "\n")

    logger.info("Prediction-vs-actual report: %s (%d rows)", md_path, len(enriched_rows))
    return md_path, csv_path, jsonl_path
