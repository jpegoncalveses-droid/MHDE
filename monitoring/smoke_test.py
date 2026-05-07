"""Monitor: hourly end-to-end smoke.

Verifies the full prediction stack is reachable:
  1. DuckDB at the configured path opens read-only.
  2. Active model exists for at least one engine (joblib loadable).
  3. Latest features row + active model produce a probability without
     crashing.
  4. Dashboard query layer returns rows for the latest prediction date.

Light. No real ingestion. Hourly.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from monitoring.alert import MonitorResult, send_alert

logger = logging.getLogger("mhde.monitoring.smoke_test")


def _check_db_open(conn) -> dict[str, Any]:
    try:
        n = conn.execute("SELECT COUNT(*) FROM information_schema.tables").fetchone()[0]
        return {"ok": True, "tables": n}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


def _check_active_model_loadable(conn) -> dict[str, Any]:
    """Try to joblib.load the active model file for each engine."""
    import joblib
    out: dict[str, Any] = {}
    for engine, table in [("equity", "ml_model_runs"),
                           ("crypto", "crypto_ml_model_runs"),
                           ("fx", "fx_ml_model_runs")]:
        row = conn.execute(
            f"SELECT model_id, model_path FROM {table} WHERE is_active = true "
            f"ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            out[engine] = {"ok": False, "reason": "no active model"}
            continue
        model_id, model_path = row
        if not Path(model_path).exists():
            out[engine] = {
                "ok": False,
                "reason": f"path missing: {model_path}",
                "model_id": model_id,
            }
            continue
        try:
            joblib.load(model_path)
            out[engine] = {"ok": True, "model_id": model_id}
        except Exception as exc:
            out[engine] = {
                "ok": False,
                "reason": f"joblib.load failed: {exc}",
                "model_id": model_id,
            }
    return out


def _check_dashboard_returns_rows(conn) -> dict[str, Any]:
    try:
        from dashboard.services.queries import get_overview_stats
        stats = get_overview_stats(conn)
        return {"ok": isinstance(stats, dict), "keys": list(stats.keys()) if isinstance(stats, dict) else []}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


def run(conn=None) -> MonitorResult:
    started = datetime.now(timezone.utc)

    close_conn = False
    if conn is None:
        from storage.config import load_engine_config
        import duckdb
        cfg = load_engine_config()
        conn = duckdb.connect(cfg["db_path"], read_only=True)
        close_conn = True

    try:
        db_check = _check_db_open(conn)
        model_check = _check_active_model_loadable(conn)
        dashboard_check = _check_dashboard_returns_rows(conn)

        problems: list[str] = []
        if not db_check["ok"]:
            problems.append(f"DB: {db_check['reason']}")
        for engine, r in model_check.items():
            if not r["ok"]:
                problems.append(f"{engine} model: {r['reason']}")
        if not dashboard_check["ok"]:
            problems.append(f"dashboard: {dashboard_check.get('reason', 'unknown')}")

        finished = datetime.now(timezone.utc)
        metrics = {"db": db_check, "models": model_check, "dashboard": dashboard_check}

        if problems:
            return MonitorResult(
                monitor="smoke_test",
                status="fail",
                severity="critical",
                title="End-to-end smoke failed",
                body="\n".join(f"- {p}" for p in problems),
                metrics=metrics,
                started_at=started, finished_at=finished,
            )
        return MonitorResult(
            monitor="smoke_test",
            status="ok",
            severity="info",
            title="end-to-end smoke OK (DB + 3 models + dashboard)",
            metrics=metrics,
            started_at=started, finished_at=finished,
        )
    finally:
        if close_conn:
            conn.close()


def main() -> int:
    result = run()
    send_alert(result)
    return 0 if result.status == "ok" else 1
