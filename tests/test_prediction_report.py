"""Prediction-vs-actual report — TDD suite."""
from __future__ import annotations

import csv
import json
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest

from storage.db import get_connection, init_schema


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _event(conn, ticker, window_days, return_value=15.0, *,
           was_in_universe=True, was_scored=True,
           score=55.0, tier="C",
           universe_tier="extended",
           event_date=None):
    if event_date is None:
        event_date = (date.today() - timedelta(days=3)).isoformat()
    event_id = uuid.uuid4().hex[:16]
    conn.execute(
        """INSERT INTO missed_opportunity_events
           (event_id, ticker, event_date, event_type, return_value, window_days,
            was_in_universe, was_scored, score_before_event, tier_before_event,
            had_catalyst_evidence, investigation_status)
           VALUES (?, ?, ?, 'gain_test', ?, ?, ?, ?, ?, ?, false, 'pending')""",
        [event_id, ticker, event_date, return_value, window_days,
         was_in_universe, was_scored, score if was_scored else None,
         tier if was_scored else None],
    )
    if universe_tier and was_in_universe:
        conn.execute(
            """INSERT INTO companies (ticker, company_name, universe_tier, is_active)
               VALUES (?, ?, ?, true)
               ON CONFLICT (ticker) DO UPDATE SET universe_tier = excluded.universe_tier""",
            [ticker, ticker, universe_tier],
        )


def test_1d_spikes_rank_above_longer_moves(conn):
    """1-day spikes must rank above 20-day stale moves."""
    from missed.prediction_report import build_rows
    _event(conn, "SHORT", window_days=1, return_value=8.0, universe_tier="extended")
    _event(conn, "LONG", window_days=20, return_value=25.0, universe_tier="extended")
    rows = build_rows(conn)
    tickers = [r["ticker"] for r in rows]
    assert tickers.index("SHORT") < tickers.index("LONG"), (
        f"1d spike (SHORT) should outrank 20d move (LONG), got order: {tickers}"
    )


def test_primary_universe_ranks_above_extended(conn):
    """Primary-universe events must rank above extended-universe events at same window."""
    from missed.prediction_report import build_rows
    _event(conn, "EXT", window_days=1, return_value=10.0, universe_tier="extended")
    _event(conn, "PRIM", window_days=1, return_value=10.0, universe_tier="primary")
    rows = build_rows(conn)
    tickers = [r["ticker"] for r in rows]
    assert tickers.index("PRIM") < tickers.index("EXT"), (
        f"Primary (PRIM) should outrank extended (EXT), got order: {tickers}"
    )


def test_near_threshold_score_increases_priority(conn):
    """Near-threshold score (40–45) should rank above deep-reject at same window."""
    from missed.prediction_report import build_rows
    _event(conn, "NEAR", window_days=10, score=42.0, tier="Reject", universe_tier="extended")
    _event(conn, "DEEP", window_days=10, score=25.0, tier="Reject", universe_tier="extended")
    rows = build_rows(conn)
    tickers = [r["ticker"] for r in rows]
    assert tickers.index("NEAR") < tickers.index("DEEP"), (
        f"Near-threshold (NEAR) should outrank deep reject (DEEP), got order: {tickers}"
    )


def test_no_score_events_are_visible(conn):
    """Events with was_scored=False must appear in results with classification 'unscored_mover'."""
    from missed.prediction_report import build_rows
    _event(conn, "UNSCORE", window_days=5, was_scored=False)
    rows = build_rows(conn)
    found = [r for r in rows if r["ticker"] == "UNSCORE"]
    assert found, "UNSCORE event should appear in build_rows() results"
    assert found[0]["classification"] == "unscored_mover", (
        f"Expected 'unscored_mover', got '{found[0]['classification']}'"
    )


def test_universe_miss_classification(conn):
    """Events with was_in_universe=False must get classification 'universe_miss'."""
    from missed.prediction_report import build_rows
    _event(conn, "NOTINUNIV", window_days=5, was_in_universe=False, was_scored=False)
    rows = build_rows(conn)
    found = [r for r in rows if r["ticker"] == "NOTINUNIV"]
    assert found, "NOTINUNIV should appear"
    assert found[0]["classification"] == "universe_miss"


def test_report_contains_required_sections(tmp_path, conn):
    """Markdown report must contain all 7 required section headings."""
    from missed.prediction_report import generate_prediction_report
    _event(conn, "AAA", window_days=1)
    _event(conn, "BBB", window_days=10, universe_tier="primary")
    md_path, _, _ = generate_prediction_report(conn, output_dir=str(tmp_path))
    md = Path(md_path).read_text()
    required = [
        "# Prediction vs Actual Spike Report",
        "## Summary",
        "## 1-Day Spikes",
        "## 3d / 10d Spikes",
        "## Longer Windows (20d / 60d)",
        "## Out-of-Universe Spikes",
        "## Near-Threshold Scores",
        "## No-Score Events",
    ]
    for heading in required:
        assert heading in md, f"Missing section heading: {heading!r}"


def test_csv_contains_required_columns(tmp_path, conn):
    """CSV must contain all required columns."""
    from missed.prediction_report import generate_prediction_report, _CSV_COLS
    _event(conn, "CCC", window_days=5)
    _, csv_path, _ = generate_prediction_report(conn, output_dir=str(tmp_path))
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
    for col in _CSV_COLS:
        assert col in header, f"Missing CSV column: {col!r}"


def test_no_production_score_mutation(tmp_path, conn):
    """generate_prediction_report must not alter the scores table."""
    from missed.prediction_report import generate_prediction_report
    score_id = uuid.uuid4().hex[:16]
    run_id = uuid.uuid4().hex[:16]
    conn.execute(
        """INSERT INTO scores
           (id, run_id, ticker, as_of_date, total_score, tier)
           VALUES (?, ?, 'SCORE_TEST', CURRENT_DATE, 55.0, 'C')""",
        [score_id, run_id],
    )
    _event(conn, "SCORE_TEST", window_days=5)
    generate_prediction_report(conn, output_dir=str(tmp_path))
    row = conn.execute(
        "SELECT total_score FROM scores WHERE id = ?", [score_id]
    ).fetchone()
    assert row is not None and row[0] == 55.0, (
        f"Score was mutated: expected 55.0, got {row}"
    )
