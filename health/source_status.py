from __future__ import annotations

import logging
from datetime import datetime, timedelta

import duckdb

logger = logging.getLogger("mhde.health")


def check_source_runs(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    try:
        rows = conn.execute(
            """
            SELECT source_name, status, MAX(started_at) as last_run,
                   SUM(records_inserted) as total_inserted
            FROM source_runs
            GROUP BY source_name, status
            ORDER BY source_name
            """
        ).fetchall()
    except Exception:
        return [{"check_name": "source_runs", "status": "skip", "severity": "low",
                 "message": "No source_runs data yet"}]

    if not rows:
        return [{"check_name": "source_runs", "status": "warn", "severity": "medium",
                 "message": "No source runs recorded. Run 'ingest all'."}]

    results = []
    for source_name, status, last_run, total in rows:
        check_name = f"source_{source_name}"
        if status in ("stub", "disabled"):
            results.append({"check_name": check_name, "status": "skip", "severity": "low",
                             "message": f"{status.upper()}: {source_name}"})
        elif status == "error":
            results.append({"check_name": check_name, "status": "warn", "severity": "medium",
                             "message": f"Last run errored. {total or 0} records total."})
        else:
            results.append({"check_name": check_name, "status": "pass", "severity": "low",
                             "message": f"{total or 0} records. Last: {last_run}"})
    return results


def check_llm_failures(conn: duckdb.DuckDBPyConnection) -> dict:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM llm_runs WHERE status = 'error'"
        ).fetchone()
        count = row[0] if row else 0
        if count > 5:
            return {"check_name": "llm_failures", "status": "warn", "severity": "medium",
                    "message": f"{count} LLM run failures recorded"}
        return {"check_name": "llm_failures", "status": "pass", "severity": "low",
                "message": f"{count} LLM failures"}
    except Exception:
        return {"check_name": "llm_failures", "status": "skip", "severity": "low",
                "message": "No LLM runs recorded yet"}


def check_notification_failures(conn: duckdb.DuckDBPyConnection) -> dict:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE status = 'error'"
        ).fetchone()
        count = row[0] if row else 0
        if count > 0:
            return {"check_name": "notification_failures", "status": "warn", "severity": "medium",
                    "message": f"{count} alert delivery failures"}
        return {"check_name": "notification_failures", "status": "pass", "severity": "low",
                "message": "No notification failures"}
    except Exception:
        return {"check_name": "notification_failures", "status": "skip", "severity": "low",
                "message": "No alerts recorded yet"}
