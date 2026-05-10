"""Daily morning health check — verifies each prediction engine produced
fresh output, that all predict services are not in failed state, and posts
a Telegram summary.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import duckdb

from pipelines.market_calendar import expected_equity_prediction_date

logger = logging.getLogger("mhde.health_check")

PREDICT_SERVICES = (
    "mhde-predict.service",
    "mhde-crypto-predict.service",
    "mhde-fx-predict.service",
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _today_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _check_equity(conn: duckdb.DuckDBPyConnection) -> CheckResult:
    """Equity predict runs at 00:15 UTC and writes prediction_date = latest
    closed market day. By 06:00 UTC we expect rows for the most recent
    weekday strictly before today (Fri on Sat/Sun/Mon mornings; Mon on
    Tue; etc.). See KI-128 / ADR-018 for the weekday gate.
    """
    expected = expected_equity_prediction_date(_today_utc())
    row = conn.execute(
        "SELECT COUNT(*) FROM ml_predictions WHERE prediction_date = ?",
        [expected],
    ).fetchone()
    n = row[0] if row else 0
    if n > 0:
        return CheckResult("equity", True, f"{n} predictions for {expected}")
    latest = conn.execute(
        "SELECT MAX(prediction_date) FROM ml_predictions"
    ).fetchone()[0]
    return CheckResult(
        "equity", False,
        f"no rows for expected={expected}; latest prediction_date={latest}",
    )


def _check_crypto(conn: duckdb.DuckDBPyConnection) -> CheckResult:
    """Crypto predict runs at 00:30 UTC. The latest prediction_date is the most
    recently closed daily candle, which at 06:00 UTC is yesterday's date.
    Accept yesterday or newer."""
    yesterday = (_today_utc() - timedelta(days=1)).date()
    row = conn.execute(
        "SELECT MAX(prediction_date), COUNT(*) FROM crypto_ml_predictions "
        "WHERE prediction_date >= ?",
        [yesterday],
    ).fetchone()
    latest, n = row[0], row[1]
    if n and n > 0 and latest is not None:
        return CheckResult("crypto", True, f"{n} predictions; latest prediction_date={latest}")
    overall_latest = conn.execute(
        "SELECT MAX(prediction_date) FROM crypto_ml_predictions"
    ).fetchone()[0]
    return CheckResult(
        "crypto", False,
        f"no rows >= {yesterday}; latest prediction_date={overall_latest}",
    )


def _check_fx(conn: duckdb.DuckDBPyConnection) -> CheckResult:
    """FX predict runs hourly. Expect prediction within the last 2 hours."""
    threshold = _today_utc() - timedelta(hours=2)
    threshold_naive = threshold.replace(tzinfo=None)
    row = conn.execute(
        "SELECT MAX(datetime_utc) FROM fx_ml_predictions"
    ).fetchone()
    latest = row[0] if row else None
    if latest is not None and latest >= threshold_naive:
        sig_row = conn.execute(
            "SELECT signal_type, datetime_utc FROM fx_signals "
            "ORDER BY datetime_utc DESC LIMIT 1"
        ).fetchone()
        latest_sig = f"{sig_row[0]} @ {sig_row[1]}" if sig_row else "no signal"
        return CheckResult("fx", True, f"latest bar {latest} UTC; latest signal: {latest_sig}")
    return CheckResult(
        "fx", False,
        f"latest prediction {latest} is older than 2h (threshold {threshold_naive})",
    )


def _check_services() -> CheckResult:
    """Verify none of the predict services are in failed state."""
    failed = []
    for unit in PREDICT_SERVICES:
        result = subprocess.run(
            ["systemctl", "is-failed", unit],
            capture_output=True, text=True, check=False,
        )
        state = result.stdout.strip()
        if result.returncode == 0:
            failed.append(f"{unit}={state}")
    if not failed:
        return CheckResult("services", True, f"{len(PREDICT_SERVICES)} services OK")
    return CheckResult("services", False, "; ".join(failed))


def _format_message(results: list[CheckResult]) -> tuple[bool, str]:
    all_ok = all(r.ok for r in results)
    by_name = {r.name: r for r in results}

    if all_ok:
        eq = by_name["equity"].detail
        cr = by_name["crypto"].detail
        fx = by_name["fx"].detail
        text = (
            "🟢 MHDE health check — all engines healthy\n"
            f"• Equity: {eq}\n"
            f"• Crypto: {cr}\n"
            f"• FX: {fx}\n"
            f"• Services: {by_name['services'].detail}"
        )
    else:
        lines = ["🔴 MHDE health check — FAILURES detected"]
        for r in results:
            mark = "✅" if r.ok else "❌"
            lines.append(f"{mark} {r.name}: {r.detail}")
        text = "\n".join(lines)
    return all_ok, text


def run_health_check(db_path: str | None = None) -> bool:
    """Run all checks, send Telegram summary, return True if all passed."""
    from storage.config import load_engine_config

    if db_path is None:
        cfg = load_engine_config()
        db_path = cfg["db_path"]

    results: list[CheckResult] = []

    conn = duckdb.connect(db_path, read_only=True)
    try:
        results.append(_check_equity(conn))
        results.append(_check_crypto(conn))
        results.append(_check_fx(conn))
    finally:
        conn.close()

    results.append(_check_services())

    all_ok, text = _format_message(results)
    logger.info("Health check %s", "PASSED" if all_ok else "FAILED")
    for r in results:
        logger.info("  %s: %s — %s", "OK" if r.ok else "FAIL", r.name, r.detail)

    from fx.bot.telegram_bot import send_message
    msg_id = send_message(text)
    if msg_id is None:
        logger.error("Telegram send failed — health check summary not delivered")
    else:
        logger.info("Telegram summary sent (msg_id=%s)", msg_id)

    return all_ok
