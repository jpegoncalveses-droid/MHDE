"""Missed-opportunity report generator — TDD suite."""
from __future__ import annotations

import json
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


def _seed_investigated_miss(conn, ticker, root_cause, truly_unpredictable=False):
    event_id = uuid.uuid4().hex[:16]
    conn.execute(
        """INSERT INTO missed_opportunity_events
           (event_id, ticker, event_date, event_type, return_value, window_days,
            was_in_universe, was_scored, had_catalyst_evidence, investigation_status)
           VALUES (?, ?, ?, 'gain_5d_10pct', 15.0, 5, true, true, false, 'investigated')""",
        [event_id, ticker, (date.today() - timedelta(days=3)).isoformat()],
    )
    inv_id = uuid.uuid4().hex[:16]
    rc = "truly_unpredictable" if truly_unpredictable else root_cause
    conn.execute(
        """INSERT INTO missed_opportunity_investigations
           (investigation_id, event_id, ticker, event_date, root_causes_json,
            primary_root_cause, text_enrichment_needed)
           VALUES (?, ?, ?, ?, ?, ?, false)""",
        [inv_id, event_id, ticker, (date.today() - timedelta(days=3)).isoformat(),
         f'["{rc}"]', rc],
    )


def test_report_importable():
    from missed.report import generate_report  # noqa: F401


def test_report_generates_markdown_file(tmp_path, conn):
    """generate_report creates a .md file."""
    from missed.report import generate_report
    _seed_investigated_miss(conn, "AAA", "threshold_too_strict")

    md_path, _ = generate_report(conn, output_dir=str(tmp_path))
    assert md_path.exists(), f"Expected markdown file at {md_path}"
    assert md_path.suffix == ".md"


def test_report_generates_json_file(tmp_path, conn):
    """generate_report creates a .json file."""
    from missed.report import generate_report
    _seed_investigated_miss(conn, "BBB", "missing_catalyst_source")

    _, json_path = generate_report(conn, output_dir=str(tmp_path))
    assert json_path.exists(), f"Expected JSON file at {json_path}"
    data = json.loads(json_path.read_text())
    assert "missed_events" in data or "root_cause_breakdown" in data


def test_report_includes_root_cause_breakdown(tmp_path, conn):
    """Report markdown includes a root cause breakdown section."""
    from missed.report import generate_report
    _seed_investigated_miss(conn, "CCC", "threshold_too_strict")
    _seed_investigated_miss(conn, "DDD", "missing_fundamentals")

    md_path, _ = generate_report(conn, output_dir=str(tmp_path))
    md = md_path.read_text()
    assert "threshold_too_strict" in md or "Root cause" in md, (
        "Report should contain root cause breakdown"
    )


def test_report_includes_truly_unpredictable_count(tmp_path, conn):
    """Report includes count of truly_unpredictable events."""
    from missed.report import generate_report
    _seed_investigated_miss(conn, "EEE", "threshold_too_strict")
    _seed_investigated_miss(conn, "FFF", "missing_fundamentals", truly_unpredictable=True)

    md_path, _ = generate_report(conn, output_dir=str(tmp_path))
    md = md_path.read_text()
    assert "truly_unpredictable" in md or "unpredictable" in md.lower(), (
        "Report should surface truly_unpredictable count"
    )
