"""Regression: dashboard date-selector helper must return every distinct
prediction_date up to the requested limit.

A `SELECT DISTINCT col FROM t ORDER BY col DESC LIMIT N` pattern triggers
a DuckDB 1.5.2 TopN-with-distinct planner regression that silently returns
far fewer rows than the table actually contains. Use GROUP BY (or any other
shape that does not fuse DISTINCT with a small TopN on the same column) to
avoid it. These tests pin the helper that wraps that query so the bug can
never reach the dashboard tabs again.
"""
from __future__ import annotations

from datetime import date, timedelta

from dashboard.services.queries import get_distinct_prediction_dates


def _insert_equity_rows(conn, dates):
    for i, d in enumerate(dates):
        conn.execute(
            """
            INSERT INTO ml_predictions
                (ticker, prediction_date, model_id, horizon,
                 predicted_probability, prediction_threshold,
                 sector, market_cap_bucket)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [f"T{i}", d, "m1", "20d", 0.5, 0.5, "Tech", "large"],
        )


def _insert_crypto_rows(conn, dates):
    for i, d in enumerate(dates):
        conn.execute(
            """
            INSERT INTO crypto_ml_predictions
                (symbol, prediction_date, model_id, horizon,
                 predicted_probability, prediction_threshold,
                 market_cap_bucket)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [f"C{i}", d, "m1", "10d", 0.5, 0.5, "large"],
        )


def test_equity_returns_every_distinct_date_under_limit(temp_db):
    dates = [date(2026, 5, 10) - timedelta(days=i) for i in range(5)]
    _insert_equity_rows(temp_db, dates)

    result = get_distinct_prediction_dates(
        temp_db, "ml_predictions", "prediction_date", limit=30
    )

    assert len(result) == 5
    assert sorted(result, reverse=True) == sorted(dates, reverse=True)


def test_crypto_returns_every_distinct_date_under_limit(temp_db):
    dates = [date(2026, 5, 10) - timedelta(days=i) for i in range(5)]
    _insert_crypto_rows(temp_db, dates)

    result = get_distinct_prediction_dates(
        temp_db, "crypto_ml_predictions", "prediction_date", limit=30
    )

    assert len(result) == 5
    assert sorted(result, reverse=True) == sorted(dates, reverse=True)


def test_returns_most_recent_when_table_exceeds_limit(temp_db):
    # 35 distinct dates, limit 30 → 30 most-recent in DESC order.
    dates = [date(2026, 5, 10) - timedelta(days=i) for i in range(35)]
    _insert_crypto_rows(temp_db, dates)

    result = get_distinct_prediction_dates(
        temp_db, "crypto_ml_predictions", "prediction_date", limit=30
    )

    assert len(result) == 30
    assert result[0] == date(2026, 5, 10)
    assert result[-1] == date(2026, 5, 10) - timedelta(days=29)


def test_helper_uses_group_by_not_distinct_topn():
    """Source-level anti-pattern guard.

    The DuckDB 1.5.2 TopN-with-distinct regression is data-volume sensitive
    — fresh in-memory tables don't hit the planner path that triggers it,
    so a behavioural test can't reliably catch a regression to the broken
    SQL shape. Pin the SQL to ``GROUP BY`` at the source level: if anyone
    reverts to ``SELECT DISTINCT col ... ORDER BY col DESC LIMIT N`` the
    helper's executed SQL no longer contains ``GROUP BY`` and this test
    fires.
    """
    # Build the SQL the helper actually executes by intercepting the
    # execute call. Source inspection alone is unreliable because the
    # docstring also mentions GROUP BY.
    captured: dict = {}

    class _Capture:
        def execute(self, sql, params=None):
            captured["sql"] = sql

            class _R:
                def fetchall(self_inner):
                    return []
            return _R()

    get_distinct_prediction_dates(
        _Capture(),
        "crypto_ml_predictions",
        "prediction_date",
        limit=30,
    )

    sql = captured["sql"].upper()
    assert "GROUP BY" in sql, (
        f"helper must use GROUP BY (DuckDB 1.5.2 TopN+DISTINCT bug); got: {sql!r}"
    )
    assert "DISTINCT" not in sql, (
        f"helper must not use DISTINCT — triggers DuckDB 1.5.2 planner "
        f"regression on production-scale data; got: {sql!r}"
    )


def test_multiple_rows_per_date_collapse_to_one_entry(temp_db):
    # The bug case: many rows for the most-recent date can starve out
    # older dates if TopN gets fused with DISTINCT.
    d_today = date(2026, 5, 10)
    _insert_crypto_rows(temp_db, [d_today] * 0)  # noop, signature
    for i in range(40):
        temp_db.execute(
            """
            INSERT INTO crypto_ml_predictions
                (symbol, prediction_date, model_id, horizon,
                 predicted_probability, prediction_threshold,
                 market_cap_bucket)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [f"X{i}", d_today, "m1", "10d", 0.5, 0.5, "large"],
        )
    # Older dates with fewer rows each.
    older = [d_today - timedelta(days=i) for i in range(1, 6)]
    _insert_crypto_rows(temp_db, older)

    result = get_distinct_prediction_dates(
        temp_db, "crypto_ml_predictions", "prediction_date", limit=30
    )

    assert len(result) == 6
    assert result[0] == d_today
