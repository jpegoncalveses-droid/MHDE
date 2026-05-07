"""Missed-opportunity attribution — TDD suite."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from storage.db import get_connection, init_schema


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _seed_investigation(conn, ticker, event_date, root_cause, primary=True):
    """Insert a missed_opportunity_events + investigation pair."""
    event_id = uuid.uuid4().hex[:16]
    conn.execute(
        """INSERT INTO missed_opportunity_events
           (event_id, ticker, event_date, event_type, return_value, window_days,
            was_in_universe, was_scored, had_catalyst_evidence, investigation_status)
           VALUES (?, ?, ?, 'gain_5d_10pct', 15.0, 5, true, true, false, 'investigated')""",
        [event_id, ticker, event_date.isoformat()],
    )
    inv_id = uuid.uuid4().hex[:16]
    conn.execute(
        """INSERT INTO missed_opportunity_investigations
           (investigation_id, event_id, ticker, event_date, root_causes_json,
            primary_root_cause, text_enrichment_needed)
           VALUES (?, ?, ?, ?, ?, ?, false)""",
        [inv_id, event_id, ticker, event_date.isoformat(),
         f'["{root_cause}"]', root_cause],
    )
    return event_id, inv_id


# ── Imports smoke ─────────────────────────────────────────────────────────────

def test_attribution_importable():
    from missed.attribution import propose_experiments_from_misses  # noqa: F401


# ── Experiment proposal ───────────────────────────────────────────────────────

def test_propose_experiment_for_threshold_too_strict(conn):
    """5+ threshold_too_strict misses → experiment proposed to review thresholds."""
    from missed.attribution import propose_experiments_from_misses
    today = date.today()
    for i in range(5):
        _seed_investigation(conn, f"T{i}", today - timedelta(days=i+1), "threshold_too_strict")

    proposals = propose_experiments_from_misses(conn)
    cats = [p["hypothesis_category"] for p in proposals]
    assert "threshold_too_strict" in cats, (
        f"Expected threshold_too_strict proposal, got {cats}"
    )


def test_no_auto_apply_of_proposed_experiments(conn):
    """Proposed experiments are stored with status='proposed', not 'applied'."""
    from missed.attribution import propose_experiments_from_misses
    today = date.today()
    for i in range(5):
        _seed_investigation(conn, f"TS{i}", today - timedelta(days=i+1), "threshold_too_strict")

    propose_experiments_from_misses(conn)

    rows = conn.execute(
        "SELECT status FROM scorecard_experiments WHERE status = 'applied'"
    ).fetchall()
    assert len(rows) == 0, "Experiments must not be auto-applied"


def test_no_experiment_when_sample_too_small(conn):
    """Fewer than 3 misses with same root cause → no experiment proposed."""
    from missed.attribution import propose_experiments_from_misses
    today = date.today()
    _seed_investigation(conn, "FEW1", today - timedelta(days=1), "threshold_too_strict")
    _seed_investigation(conn, "FEW2", today - timedelta(days=2), "threshold_too_strict")

    proposals = propose_experiments_from_misses(conn)
    cats = [p["hypothesis_category"] for p in proposals]
    assert "threshold_too_strict" not in cats, (
        "2 misses should not trigger experiment (need >= 3)"
    )


def test_truly_unpredictable_does_not_propose_experiment(conn):
    """Only truly_unpredictable misses → no scoring experiment proposed."""
    from missed.attribution import propose_experiments_from_misses
    today = date.today()
    for i in range(5):
        _seed_investigation(conn, f"TU{i}", today - timedelta(days=i+1), "truly_unpredictable")

    proposals = propose_experiments_from_misses(conn)
    # Truly unpredictable → no actionable experiment
    assert len(proposals) == 0, (
        f"Truly unpredictable should not generate experiments, got {proposals}"
    )


def test_experiment_saved_to_scorecard_experiments(conn):
    """Proposed experiment from attribution appears in scorecard_experiments table."""
    from missed.attribution import propose_experiments_from_misses
    today = date.today()
    for i in range(4):
        _seed_investigation(conn, f"EXP{i}", today - timedelta(days=i+1), "missing_catalyst_source")

    propose_experiments_from_misses(conn)
    rows = conn.execute(
        "SELECT COUNT(*) FROM scorecard_experiments WHERE hypothesis LIKE '%catalyst%'"
    ).fetchone()
    assert rows[0] >= 1, "Experiment about catalyst source gap should be in DB"
