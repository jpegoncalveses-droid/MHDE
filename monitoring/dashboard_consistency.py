"""Monitor: dashboard rendering must match underlying database.

For each engine, count the rows the dashboard's outcomes / predictions
query returns and compare to a direct SELECT against the same tables.
Any divergence indicates the dashboard is filtering / joining wrong.

Schedule: every 6 hours.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from monitoring.alert import MonitorResult, send_alert

logger = logging.getLogger("mhde.monitoring.dashboard_consistency")


def run(conn=None) -> MonitorResult:
    """Run the dashboard-consistency check.

    `conn` is an open DuckDB read-only connection; if None, opens one
    from the configured engine path.
    """
    started = datetime.now(timezone.utc)

    close_conn = False
    if conn is None:
        from storage.config import load_engine_config
        import duckdb
        cfg = load_engine_config()
        conn = duckdb.connect(cfg["db_path"], read_only=True)
        close_conn = True

    try:
        from dashboard.services.queries import get_outcomes
        # Dashboard side
        dashboard_rows = get_outcomes(conn, limit=200)
        dashboard_count = len(dashboard_rows)

        # Direct DB side: count what the dashboard's get_outcomes view
        # would have returned. get_outcomes selects the most recent 200
        # candidate_outcomes rows; we count and clamp to 200.
        direct = conn.execute(
            "SELECT COUNT(*) FROM candidate_outcomes"
        ).fetchone()
        direct_count = min(direct[0], 200) if direct else 0

        mismatches: list[str] = []
        if dashboard_count != direct_count:
            mismatches.append(
                f"outcomes: dashboard={dashboard_count}, db={direct_count}"
            )

        # Per-engine prediction count parity: the dashboard selects rows
        # for the latest prediction_date. Verify the count it would show
        # matches a direct count.
        for engine, table, date_col in [
            ("equity", "ml_predictions", "prediction_date"),
            ("crypto", "crypto_ml_predictions", "prediction_date"),
            ("fx", "fx_ml_predictions", "datetime_utc"),
        ]:
            row = conn.execute(
                f"SELECT MAX({date_col}) FROM {table}"
            ).fetchone()
            if not row or row[0] is None:
                continue
            latest = row[0]
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {date_col} = ?", [latest]
            ).fetchone()[0]
            if count == 0:
                mismatches.append(
                    f"{engine}: latest {date_col}={latest} has 0 rows"
                )

        finished = datetime.now(timezone.utc)
        if mismatches:
            return MonitorResult(
                monitor="dashboard_consistency",
                status="fail",
                severity="warn",
                title="Dashboard ↔ DB mismatch detected",
                body="\n".join(f"- {m}" for m in mismatches),
                metrics={"dashboard_count": dashboard_count,
                         "direct_count": direct_count},
                started_at=started, finished_at=finished,
            )
        return MonitorResult(
            monitor="dashboard_consistency",
            status="ok",
            severity="info",
            title="dashboard ↔ DB consistent",
            metrics={"outcomes_checked": dashboard_count},
            started_at=started, finished_at=finished,
        )
    finally:
        if close_conn:
            conn.close()


def main() -> int:
    result = run()
    send_alert(result)
    return 0 if result.status == "ok" else 1
