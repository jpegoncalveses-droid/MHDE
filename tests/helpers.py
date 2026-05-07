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


def assert_dashboard_renders(page: str, expected_data: dict[str, Any]) -> None:
    """Verify a dashboard page renders with the expected data.

    Stub for Session 4 (integration tests). Streamlit's render path needs
    a runtime that pytest doesn't load by default; this helper is the
    placeholder so test code can already be written against it.

    Suggested implementation when filled in: call
    `dashboard.services.queries.<page_query>(conn)` and compare its
    return value to `expected_data` field-by-field. That sidesteps
    Streamlit and tests the data layer the dashboard actually consumes.
    """
    raise NotImplementedError(
        f"assert_dashboard_renders is a Session 4 deliverable. "
        f"page={page!r}, expected_data keys={list(expected_data.keys())}"
    )
