"""Missed-opportunity attribution.

Proposes experiments when a pattern of root causes is statistically significant.
Experiments are proposed (status='proposed') and never auto-applied.
"""
from __future__ import annotations

import logging

import duckdb

from learning.experiments import propose_experiment

logger = logging.getLogger("mhde.missed.attribution")

_MIN_PATTERN_COUNT = 3

_EXPERIMENT_TEMPLATES: dict[str, dict] = {
    "threshold_too_strict": {
        "hypothesis": (
            "Multiple missed opportunities were scored above 38 but Rejected. "
            "The C-tier threshold (currently 45) may be too strict."
        ),
        "proposed_change": {
            "c_tier_min_score": 42,
            "action": "Lower C-tier threshold from 45 to 42 and re-evaluate miss rate",
        },
        "affected_components": ["scoring/tiers.py"],
        "expected_effect": "Reduce threshold_too_strict misses by capturing near-threshold candidates",
    },
    "missing_catalyst_source": {
        "hypothesis": (
            "Multiple missed opportunities had no catalyst signals despite filing activity. "
            "New catalyst sources or classification rules may be needed."
        ),
        "proposed_change": {
            "action": "Expand catalyst sources (StockTwits, GDELT, IR transcripts) or loosen 8-K materiality keywords",
        },
        "affected_components": ["features/catalyst.py", "ingestion/"],
        "expected_effect": "Reduce missing_catalyst_source misses by broadening catalyst detection",
    },
    "missing_fundamentals": {
        "hypothesis": (
            "Multiple missed opportunities lacked fundamental data before the move."
        ),
        "proposed_change": {
            "action": "Improve SEC XBRL ingestion frequency or add alternative fundamental data source",
        },
        "affected_components": ["ingestion/ingest_sec.py"],
        "expected_effect": "Reduce missing_fundamentals misses through better data coverage",
    },
    "data_quality_guard_too_strict": {
        "hypothesis": (
            "Multiple missed opportunities were blocked by data quality guards. "
            "Guards may be too conservative."
        ),
        "proposed_change": {
            "action": "Review staleness threshold and guard confidence logic",
        },
        "affected_components": ["features/quality.py"],
        "expected_effect": "Reduce guard-driven misses without compromising data quality",
    },
}

_NO_EXPERIMENT_CAUSES = {"truly_unpredictable", "other", "not_in_universe"}


def propose_experiments_from_misses(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """
    Analyze accumulated investigation root causes and propose experiments
    where a pattern has >= _MIN_PATTERN_COUNT occurrences.
    Returns list of proposal dicts (hypothesis_category, experiment_id).
    """
    counts = _count_root_causes(conn)
    proposals: list[dict] = []

    for cause, count in counts.items():
        if cause in _NO_EXPERIMENT_CAUSES:
            continue
        if count < _MIN_PATTERN_COUNT:
            continue
        template = _EXPERIMENT_TEMPLATES.get(cause)
        if not template:
            continue

        exp_id = propose_experiment(
            conn,
            hypothesis=template["hypothesis"],
            proposed_change=template["proposed_change"],
            affected_components=template["affected_components"],
            expected_effect=template["expected_effect"],
            based_on_run_ids=[],
        )
        proposals.append({
            "hypothesis_category": cause,
            "experiment_id": exp_id,
            "count": count,
        })
        logger.info("Proposed experiment %s for pattern: %s (n=%d)", exp_id, cause, count)

    return proposals


def _count_root_causes(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT primary_root_cause, COUNT(*) FROM missed_opportunity_investigations GROUP BY primary_root_cause"
    ).fetchall()
    return {r[0]: r[1] for r in rows if r[0]}
