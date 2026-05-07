"""Assertion helpers for MHDE tests.

Keep helpers narrow: each does one check, reads no production code
besides the database, and produces a useful failure message. Tests
that need richer assertions should compose these.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import duckdb


def assert_db_state(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    expected_rows: int,
    where: str | None = None,
) -> None:
    """Count rows in `table` (optionally with a WHERE clause) and assert
    the count matches `expected_rows`.

    Useful idioms:
        assert_db_state(conn, "ml_predictions", 50)
        assert_db_state(conn, "ml_predictions", 5,
                        where="prediction_date = CURRENT_DATE")
    """
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    actual = conn.execute(sql).fetchone()[0]
    msg = f"row count mismatch in {table}"
    if where:
        msg += f" (where {where})"
    msg += f": expected {expected_rows}, got {actual}"
    assert actual == expected_rows, msg


def assert_pipeline_completed_cleanly(
    conn: duckdb.DuckDBPyConnection,
    engine: str,
    prediction_date: date | datetime | None = None,
) -> None:
    """Assert the engine wrote prediction rows for the target date and
    none of them have NULL `predicted_probability` (the key signal).

    `engine` ∈ {"equity", "crypto", "fx"}.

    For equity / crypto, `prediction_date` is a `date` (defaults today).
    For fx, it's the bar `datetime` (defaults the most recent row).
    """
    table = {
        "equity": "ml_predictions",
        "crypto": "crypto_ml_predictions",
        "fx": "fx_ml_predictions",
    }[engine]

    if engine == "fx":
        if prediction_date is None:
            row = conn.execute(
                "SELECT MAX(datetime_utc) FROM fx_ml_predictions"
            ).fetchone()
            prediction_date = row[0] if row else None
        assert prediction_date is not None, "fx_ml_predictions is empty"
        n_rows = conn.execute(
            "SELECT COUNT(*) FROM fx_ml_predictions WHERE datetime_utc = ?",
            [prediction_date],
        ).fetchone()[0]
        n_null = conn.execute(
            "SELECT COUNT(*) FROM fx_ml_predictions "
            "WHERE datetime_utc = ? AND predicted_probability IS NULL",
            [prediction_date],
        ).fetchone()[0]
        date_col = "datetime_utc"
    else:
        prediction_date = prediction_date or date.today()
        n_rows = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE prediction_date = ?",
            [prediction_date],
        ).fetchone()[0]
        n_null = conn.execute(
            f"SELECT COUNT(*) FROM {table} "
            f"WHERE prediction_date = ? AND predicted_probability IS NULL",
            [prediction_date],
        ).fetchone()[0]
        date_col = "prediction_date"

    assert n_rows > 0, (
        f"{engine} pipeline wrote no predictions for {date_col}={prediction_date} "
        f"(table {table})"
    )
    assert n_null == 0, (
        f"{engine} pipeline wrote {n_null} NULL predicted_probability rows "
        f"for {date_col}={prediction_date} (table {table})"
    )


def assert_dashboard_renders(
    conn: duckdb.DuckDBPyConnection,
    page: str,
    *,
    expected_min_rows: int = 0,
    expected_keys: list[str] | None = None,
) -> Any:
    """Call the dashboard's data-layer function for `page` and assert
    the response is well-formed.

    `page` is one of the queries defined in `dashboard/services/queries.py`:
        "overview", "candidates", "candidate_detail", "source_health",
        "llm_runs", "outcomes", "health_checks", "backtest_runs",
        "alerts", "hypotheses", "candidate_reviews",
        "scorecard_experiments".

    This sidesteps Streamlit's runtime (no `streamlit.runtime.scriptrunner`
    needed for tests) and validates the data layer the dashboard
    actually consumes.

    Returns the query's raw output so tests can do further assertions.
    Raises AssertionError on shape mismatch.
    """
    from dashboard.services import queries as q

    page_to_fn = {
        "overview": q.get_overview_stats,
        "candidates": q.get_candidates,
        "candidate_detail": q.get_candidate_detail,
        "source_health": q.get_source_health,
        "llm_runs": q.get_llm_runs,
        "outcomes": q.get_outcomes,
        "health_checks": q.get_health_checks,
        "backtest_runs": q.get_backtest_runs,
        "alerts": q.get_alerts,
        "hypotheses": q.get_hypotheses,
        "candidate_reviews": q.get_candidate_reviews,
        "scorecard_experiments": q.get_scorecard_experiments,
    }
    if page not in page_to_fn:
        raise ValueError(
            f"unknown dashboard page {page!r}; known: {sorted(page_to_fn)}"
        )

    result = page_to_fn[page](conn)

    if isinstance(result, list):
        assert len(result) >= expected_min_rows, (
            f"page {page} returned {len(result)} rows, expected >= {expected_min_rows}"
        )
        if expected_keys and result:
            row_keys = set(result[0].keys()) if isinstance(result[0], dict) else set()
            missing = set(expected_keys) - row_keys
            assert not missing, (
                f"page {page} row missing expected keys: {sorted(missing)}"
            )
    elif isinstance(result, dict):
        if expected_keys:
            missing = set(expected_keys) - set(result.keys())
            assert not missing, (
                f"page {page} dict missing expected keys: {sorted(missing)}"
            )

    return result
