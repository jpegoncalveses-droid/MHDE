"""Read prediction-vs-actual CSV artifacts and return a stats summary dict."""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

_ROWS_CSV = "prediction_vs_actual_rows.csv"
_ENRICHED_CSV = "prediction_vs_actual_enriched_rows.csv"

_CLASSIFICATIONS = [
    "true_miss", "near_threshold", "scored_missed", "scored_correct",
    "universe_miss", "unscored_mover",
]
_RC_GROUPS = ["data_gap", "scoring_gap", "feature_gap", "near_miss", "universe_gap", "unknown"]

_INCOMPLETE_SUBCAUSES = [
    "missing_cik",
    "missing_sec_companyfacts",
    "foreign_filer_or_adr",
    "stale_fundamentals",
    "recent_ipo_or_short_history",
    "sector_specific_model_gap",
    "polygon_fundamentals_missing",
    "ifrs_mapping_gap",
    "price_only_scored",
]


def get_learning_stats(output_dir: str = "data/processed") -> dict:
    base = Path(output_dir)
    clf_counts: dict[str, int] = {k: 0 for k in _CLASSIFICATIONS}
    rc_counts: dict[str, int] = {k: 0 for k in _RC_GROUPS}
    report_date = ""
    total = 0

    rows_path = base / _ROWS_CSV
    if rows_path.exists():
        with open(rows_path, newline="") as f:
            rows = list(csv.DictReader(f))
        total = len(rows)
        for r in rows:
            clf = r.get("classification", "")
            if clf in clf_counts:
                clf_counts[clf] += 1
        if rows:
            report_date = rows[0].get("event_date", "")

    incomplete_subcause_counts: dict[str, int] = {k: 0 for k in _INCOMPLETE_SUBCAUSES}

    enriched_path = base / _ENRICHED_CSV
    if enriched_path.exists():
        with open(enriched_path, newline="") as f:
            enriched = list(csv.DictReader(f))
        c = Counter(r.get("root_cause_group", "unknown") for r in enriched)
        for k in _RC_GROUPS:
            rc_counts[k] = c.get(k, 0)
        sc = Counter(
            r.get("enriched_root_cause", "")
            for r in enriched
            if r.get("enriched_root_cause", "") in _INCOMPLETE_SUBCAUSES
        )
        for k in _INCOMPLETE_SUBCAUSES:
            incomplete_subcause_counts[k] = sc.get(k, 0)

    top_rc = max(rc_counts, key=rc_counts.get) if any(rc_counts.values()) else ""

    return {
        "report_date": report_date,
        "total": total,
        **clf_counts,
        "rc_groups": rc_counts,
        "top_rc_group": top_rc,
        "incomplete_subcauses": incomplete_subcause_counts,
    }
