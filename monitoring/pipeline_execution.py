"""Monitor: each prediction pipeline ran in the last expected window
and produced a row count above a sensible floor.

Per ARCHITECTURE.md schedules:
  equity ML predict   → daily 00:15 UTC, scores T-1 (expect new ml_predictions rows daily)
  crypto ML predict   → daily 00:30 UTC      (expect new crypto_ml_predictions rows daily)
  fx     ML predict   → hourly :05           (expect new fx_ml_predictions rows every hour)
  daily-analysis      → Mon-Fri 23:15        (expect new pipeline_runs rows weekday)

For each pipeline we check:
  1. last write recency vs the schedule's grace window
  2. row count for the last write vs a 14-day rolling average

Below 50% of the rolling average → warn. Below 20% → fail.

Both the latest count and the trailing baseline filter to predictions
written by `is_active=true` model_ids in the corresponding *_model_runs
table. This is required for correctness: the predictions tables also
hold rows from training/walk-forward backtest paths, and including
those in the baseline inflates the rolling average and produces false
positives when only production scoring is active for the day. Fix
landed in the discipline session 2026-05-09 after the crypto pipeline
fired a false-positive warning for two consecutive days.

Schedule: hourly (FX pacing). Equity / crypto / daily-analysis only
flag once per day; the same hourly run handles all four.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from monitoring.alert import MonitorResult, send_alert
from pipelines.market_calendar import is_forex_closed, fx_close_floor

logger = logging.getLogger("mhde.monitoring.pipeline_execution")


# Grace windows after the scheduled firing before we consider the
# pipeline "missed". The monitor compares `now - latest_dt` against
# this budget, where `latest_dt` is `MAX(prediction_date)` interpreted
# as midnight UTC of that date. The budgets must therefore include
# both the schedule's natural lag AND the schema's prediction-date
# convention.
RECENCY_BUDGET = {
    # Equity: T-1 scoring at 00:15 UTC daily. prediction_date is
    # yesterday's trading date; on a Friday → Monday weekend the
    # latest stays at Friday until Tuesday's 00:15 fire (~72h gap
    # between fresh prediction_date values). 75h = 72h weekend roll +
    # 3h grace. Holiday-extended weekends may still warn — accepted.
    # See ADR-015 for rationale.
    "equity": timedelta(hours=75),
    # Crypto: 24/7 scoring at 00:30 UTC writes prediction_date = T-1
    # (the last completed features `trade_date`), not the run time.
    # Age right after a successful fire is therefore ~24h 30m, and
    # right before the next fire is ~48h 30m — the same cycle-on-T-1
    # pattern as equity, just without the weekend roll. 51h = 48h
    # cycle + 3h grace; this still catches a single missed fire
    # within ~3h after the second day's scheduled run. See ADR-029
    # and KI-141. The prior value of 27h assumed prediction_date
    # incremented to *today*, which it does not, and false-fired
    # ~21h of every 24h cycle.
    "crypto": timedelta(days=2, hours=3),
    # FX: hourly at :05; tight budget appropriate.
    "fx":     timedelta(hours=2),
}


def _check_engine_pipeline(
    conn,
    engine: str,
    table: str,
    date_col: str,
    model_runs_table: str,
    now: datetime,
) -> dict[str, Any]:
    """Return a dict of {recency_ok, count_ok, latest, n_latest, n_avg}.

    Both the latest count and the 14-day baseline filter to predictions
    written by ``is_active=true`` model_ids in ``model_runs_table``.
    Without that filter, the baseline includes training / walk-forward
    backtest rows that share the predictions table and produces false
    positives when only production scoring is active.
    """
    out: dict[str, Any] = {"recency_ok": True, "count_ok": True}

    n_active = conn.execute(
        f"SELECT COUNT(*) FROM {model_runs_table} WHERE is_active = true"
    ).fetchone()[0]
    if n_active == 0:
        out["recency_ok"] = False
        out["count_ok"] = False
        out["reason"] = f"{model_runs_table} has no is_active=true models"
        return out

    row = conn.execute(f"""
        SELECT MAX(p.{date_col})
        FROM {table} p
        JOIN {model_runs_table} m ON p.model_id = m.model_id
        WHERE m.is_active = true
    """).fetchone()
    latest = row[0] if row else None
    out["latest"] = latest

    if latest is None:
        out["recency_ok"] = False
        out["count_ok"] = False
        out["reason"] = f"{table} has no rows written by active models"
        return out

    # Recency check.
    if isinstance(latest, datetime):
        latest_dt = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
    else:  # date
        latest_dt = datetime.combine(latest, datetime.min.time(), tzinfo=timezone.utc)

    if engine == "fx" and is_forex_closed(now):
        floor = fx_close_floor(now)
        logger.info(
            "fx forex-closed window — asserting latest >= %s (KI-128)",
            floor.isoformat(),
        )
        if latest_dt < floor:
            out["recency_ok"] = False
            out["reason"] = (
                f"latest {date_col}={latest} predates forex-close floor "
                f"{floor.isoformat()} — outage during closed window"
            )
        # else: fx healthy during the close; skip the 2h budget.
    else:
        age = now - latest_dt
        if age > RECENCY_BUDGET[engine]:
            out["recency_ok"] = False
            out["reason"] = (
                f"latest {date_col}={latest} is {age} old, threshold "
                f"{RECENCY_BUDGET[engine]}"
            )

    # Row-count check vs 14-day rolling average. Both sides filter to
    # active model_ids so the baseline reflects production scoring only
    # (KI-118 lesson + monitor false-positive fix 2026-05-09).
    n_latest = conn.execute(f"""
        SELECT COUNT(*)
        FROM {table} p
        JOIN {model_runs_table} m ON p.model_id = m.model_id
        WHERE p.{date_col} = ? AND m.is_active = true
    """, [latest]).fetchone()[0]
    out["n_latest"] = n_latest

    avg_row = conn.execute(f"""
        SELECT AVG(c) FROM (
            SELECT COUNT(*) AS c FROM {table} p
            JOIN {model_runs_table} m ON p.model_id = m.model_id
            WHERE p.{date_col} >= ? AND p.{date_col} < ?
              AND m.is_active = true
            GROUP BY p.{date_col}
        )
    """, [latest_dt - timedelta(days=14), latest_dt]).fetchone()
    n_avg = float(avg_row[0]) if avg_row and avg_row[0] is not None else 0.0
    out["n_avg"] = round(n_avg, 1)

    if n_avg > 5:  # only judge once we have at least a small sample
        ratio = n_latest / n_avg
        out["ratio"] = round(ratio, 2)
        if ratio < 0.20:
            out["count_ok"] = False
            out["count_severity"] = "fail"
            out["reason"] = (
                f"{n_latest} rows vs 14d avg {n_avg:.1f} (ratio={ratio:.2f}) "
                f"— below 20% threshold"
            )
        elif ratio < 0.50:
            out["count_ok"] = False
            out["count_severity"] = "warn"
            out["reason"] = (
                f"{n_latest} rows vs 14d avg {n_avg:.1f} (ratio={ratio:.2f}) "
                f"— below 50% threshold"
            )
    return out


def run(conn=None, now: datetime | None = None) -> MonitorResult:
    started = datetime.now(timezone.utc)
    now = now or started

    close_conn = False
    if conn is None:
        from storage.config import load_engine_config
        import duckdb
        cfg = load_engine_config()
        conn = duckdb.connect(cfg["db_path"], read_only=True)
        close_conn = True

    try:
        engines = [
            ("equity", "ml_predictions", "prediction_date", "ml_model_runs"),
            ("crypto", "crypto_ml_predictions", "prediction_date", "crypto_ml_model_runs"),
            ("fx", "fx_ml_predictions", "datetime_utc", "fx_ml_model_runs"),
        ]
        problems: list[str] = []
        worst_severity = "info"
        metrics: dict[str, Any] = {}

        for engine, table, date_col, model_runs_table in engines:
            r = _check_engine_pipeline(conn, engine, table, date_col, model_runs_table, now)
            metrics[engine] = r
            if not r["recency_ok"] or not r["count_ok"]:
                worst_severity = "critical" if r.get("count_severity") == "fail" or not r["recency_ok"] else \
                                 ("warn" if worst_severity != "critical" else worst_severity)
                problems.append(f"{engine}: {r.get('reason', 'check failed')}")

        finished = datetime.now(timezone.utc)
        if problems:
            return MonitorResult(
                monitor="pipeline_execution",
                status="fail" if worst_severity == "critical" else "warn",
                severity=worst_severity,
                title="Pipeline execution monitor flagged",
                body="\n".join(f"- {p}" for p in problems),
                metrics={k: v for k, v in metrics.items()},
                started_at=started, finished_at=finished,
            )
        return MonitorResult(
            monitor="pipeline_execution",
            status="ok",
            severity="info",
            title="all 3 pipelines fresh and within count budget",
            metrics={k: v for k, v in metrics.items()},
            started_at=started, finished_at=finished,
        )
    finally:
        if close_conn:
            conn.close()


def main() -> int:
    result = run()
    send_alert(result)
    return 0 if result.status == "ok" else 1
