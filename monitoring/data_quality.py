"""Monitor: per-engine ticker / symbol / bar coverage on the latest day.

For each engine we track a 14-day rolling expected count and alert if
the latest day's count drops below 80% of that. Catches Yahoo data
thinning, Binance outages, Dukascopy gaps.

Schedule: daily (after each ingestion firing — equity 23:15, crypto
00:30, FX hourly :05; daily firing covers all three).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from monitoring.alert import MonitorResult, send_alert

logger = logging.getLogger("mhde.monitoring.data_quality")

COVERAGE_FLOOR = 0.80  # alert if latest count < 80% of 14-day avg


def _coverage_check(conn, engine: str, table: str, date_col: str,
                    entity_col: str | None) -> dict[str, Any]:
    """Compare latest count to 14-day rolling average."""
    out: dict[str, Any] = {"engine": engine}

    row = conn.execute(f"SELECT MAX({date_col}) FROM {table}").fetchone()
    latest = row[0] if row else None
    if latest is None:
        out["status"] = "fail"
        out["reason"] = f"{table} is empty"
        return out
    out["latest"] = latest

    if entity_col:
        latest_count = conn.execute(
            f"SELECT COUNT(DISTINCT {entity_col}) FROM {table} WHERE {date_col} = ?",
            [latest],
        ).fetchone()[0]
        avg_row = conn.execute(f"""
            SELECT AVG(c) FROM (
                SELECT COUNT(DISTINCT {entity_col}) AS c FROM {table}
                WHERE {date_col} >= ? AND {date_col} < ?
                GROUP BY {date_col}
            )
        """, [latest, latest]).fetchone()
    else:
        # FX: no entity dimension, count rows per date.
        latest_count = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE DATE_TRUNC('day', {date_col}) = ?",
            [latest if isinstance(latest, datetime) else latest],
        ).fetchone()[0]
        avg_row = conn.execute(f"""
            SELECT AVG(c) FROM (
                SELECT COUNT(*) AS c FROM {table}
                WHERE {date_col} >= ? AND {date_col} < ?
                GROUP BY DATE_TRUNC('day', {date_col})
            )
        """, [latest, latest]).fetchone()

    out["n_latest"] = latest_count
    n_avg = float(avg_row[0]) if avg_row and avg_row[0] is not None else 0.0
    out["n_avg"] = round(n_avg, 1)

    if n_avg < 1:
        out["status"] = "skip"
        out["reason"] = "no historical baseline yet"
        return out

    ratio = latest_count / n_avg
    out["ratio"] = round(ratio, 2)
    if ratio < COVERAGE_FLOOR:
        out["status"] = "warn"
        out["reason"] = (
            f"{latest_count} {entity_col or 'rows'} on {latest} vs "
            f"14d avg {n_avg:.1f} (ratio={ratio:.2f}) < {COVERAGE_FLOOR:.0%}"
        )
    else:
        out["status"] = "ok"
    return out


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
        engines = [
            ("equity", "prices_daily", "trade_date", "ticker"),
            ("crypto", "crypto_prices_daily", "trade_date", "symbol"),
            ("fx", "fx_prices_hourly", "datetime_utc", None),
        ]
        per_engine: dict[str, Any] = {}
        problems: list[str] = []
        for engine, table, date_col, entity_col in engines:
            r = _coverage_check(conn, engine, table, date_col, entity_col)
            per_engine[engine] = r
            if r.get("status") in {"warn", "fail"}:
                problems.append(f"{engine}: {r.get('reason')}")

        finished = datetime.now(timezone.utc)
        if problems:
            return MonitorResult(
                monitor="data_quality",
                status="warn",
                severity="warn",
                title=f"Data coverage below {COVERAGE_FLOOR:.0%} floor",
                body="\n".join(f"- {p}" for p in problems),
                metrics={"per_engine": per_engine,
                         "floor": COVERAGE_FLOOR},
                started_at=started, finished_at=finished,
            )
        return MonitorResult(
            monitor="data_quality",
            status="ok",
            severity="info",
            title="all 3 engines within coverage floor",
            metrics={"per_engine": per_engine},
            started_at=started, finished_at=finished,
        )
    finally:
        if close_conn:
            conn.close()


def main() -> int:
    result = run()
    send_alert(result)
    return 0 if result.status == "ok" else 1
