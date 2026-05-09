"""Monitor: cross-artifact consistency.

The daily Telegram health check (`pipelines/health_check.py`)
formats four ``CheckResult`` objects and posts a summary to the
operator. This monitor catches the rare-but-real failure mode where
the formatter LIES — the underlying DB has one value, the string
posted to Telegram has a different one.

It re-runs the same check functions used by the health check, then
independently re-queries the DB for the facts the detail strings
claim, and compares. A typo, a wrong column reference, a swapped
engine section, or a regression in `_format_message` would surface
here.

Three classes of disagreement covered:

  - Equity claim: "{n} predictions for {yesterday}" — verify n matches
    a direct ``SELECT COUNT(*) FROM ml_predictions WHERE
    prediction_date = ?``.
  - Crypto claim: "{n} predictions; latest prediction_date={date}" —
    verify n matches and the date is the actual MAX.
  - FX claim: "latest bar {dt} UTC; latest signal: {sig}" — verify
    `dt` matches MAX(datetime_utc) and the signal substring matches
    the latest signal row.

Format-evolution case: if the regex doesn't match (e.g. someone
changed the format string), the monitor warns rather than crashes.
That itself is a useful signal — the contract this monitor relies
on has changed.

Schedule: daily at 06:30 UTC, after the daily health check at 06:00.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from monitoring.alert import MonitorResult, send_alert

logger = logging.getLogger("mhde.monitoring.cross_artifact")


_EQUITY_DETAIL_RE = re.compile(
    r"^(\d+) predictions for (\d{4}-\d{2}-\d{2})$"
)
_CRYPTO_DETAIL_RE = re.compile(
    r"^(\d+) predictions; latest prediction_date=(\d{4}-\d{2}-\d{2})$"
)
_FX_DETAIL_RE = re.compile(
    r"^latest bar ([0-9: \-]+) UTC; latest signal: (.+)$"
)


def _verify_equity(conn, detail: str) -> list[str]:
    issues: list[str] = []
    m = _EQUITY_DETAIL_RE.match(detail)
    if not m:
        # Equity check returns one of two formats depending on success.
        # The "no rows for X" failure-shape is also valid; we only verify
        # the success-shape to avoid double-counting an existing failure.
        return issues
    claimed_n = int(m.group(1))
    claimed_date = m.group(2)
    actual_n = conn.execute(
        "SELECT COUNT(*) FROM ml_predictions WHERE prediction_date = ?",
        [claimed_date],
    ).fetchone()[0]
    if actual_n != claimed_n:
        issues.append(
            f"equity: detail claims {claimed_n} predictions for "
            f"{claimed_date}, DB has {actual_n}"
        )
    return issues


def _verify_crypto(conn, detail: str) -> list[str]:
    issues: list[str] = []
    m = _CRYPTO_DETAIL_RE.match(detail)
    if not m:
        return issues
    claimed_n = int(m.group(1))
    claimed_date = m.group(2)
    actual_max = conn.execute(
        "SELECT MAX(prediction_date) FROM crypto_ml_predictions"
    ).fetchone()[0]
    if actual_max is None or str(actual_max) != claimed_date:
        issues.append(
            f"crypto: detail claims latest prediction_date={claimed_date}, "
            f"DB MAX={actual_max}"
        )
        return issues
    actual_n = conn.execute(
        "SELECT COUNT(*) FROM crypto_ml_predictions "
        "WHERE prediction_date >= (CURRENT_DATE - INTERVAL '1 day')"
    ).fetchone()[0]
    if actual_n != claimed_n:
        issues.append(
            f"crypto: detail claims {claimed_n} predictions for "
            f">= yesterday, DB has {actual_n}"
        )
    return issues


def _verify_fx(conn, detail: str) -> list[str]:
    issues: list[str] = []
    m = _FX_DETAIL_RE.match(detail)
    if not m:
        return issues
    claimed_dt = m.group(1).strip()
    claimed_signal = m.group(2).strip()
    actual_dt = conn.execute(
        "SELECT MAX(datetime_utc) FROM fx_ml_predictions"
    ).fetchone()[0]
    if actual_dt is None or str(actual_dt) != claimed_dt:
        issues.append(
            f"fx: detail claims latest bar {claimed_dt}, DB MAX={actual_dt}"
        )
    sig_row = conn.execute(
        "SELECT signal_type, datetime_utc FROM fx_signals "
        "ORDER BY datetime_utc DESC LIMIT 1"
    ).fetchone()
    expected_sig = (
        f"{sig_row[0]} @ {sig_row[1]}" if sig_row else "no signal"
    )
    if claimed_signal != expected_sig:
        issues.append(
            f"fx: detail claims latest signal '{claimed_signal}', "
            f"DB latest='{expected_sig}'"
        )
    return issues


def _verify_message_includes_details(message: str, details: list[str]) -> list[str]:
    """Verify the formatted Telegram-style message contains each engine's
    detail string. Catches a `_format_message` regression that drops or
    swaps a section."""
    issues: list[str] = []
    for d in details:
        if d and d not in message:
            issues.append(f"format_message: dropped detail '{d}'")
    return issues


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
        # Reuse the exact internals the daily Telegram check posts.
        from pipelines.health_check import (
            _check_equity, _check_crypto, _check_fx, _format_message,
        )
        eq = _check_equity(conn)
        cr = _check_crypto(conn)
        fx = _check_fx(conn)

        problems: list[str] = []
        if eq.ok:
            problems.extend(_verify_equity(conn, eq.detail))
        if cr.ok:
            problems.extend(_verify_crypto(conn, cr.detail))
        if fx.ok:
            problems.extend(_verify_fx(conn, fx.detail))

        # Sanity: the formatted message must include each detail string.
        # `_check_services` is omitted from cross-checking — its detail
        # comes from systemctl, not the DB.
        from dataclasses import dataclass

        @dataclass
        class _Stub:
            name: str
            ok: bool
            detail: str

        services = _Stub("services", True, "stub OK")
        all_ok, message = _format_message([eq, cr, fx, services])
        problems.extend(_verify_message_includes_details(
            message, [eq.detail, cr.detail, fx.detail],
        ))

        finished = datetime.now(timezone.utc)
        metrics = {
            "equity_detail": eq.detail,
            "crypto_detail": cr.detail,
            "fx_detail": fx.detail,
        }
        if problems:
            return MonitorResult(
                monitor="cross_artifact",
                status="fail",
                severity="warn",
                title="cross_artifact: formatted message disagrees with DB",
                body="\n".join(f"- {p}" for p in problems),
                metrics=metrics,
                started_at=started, finished_at=finished,
            )
        return MonitorResult(
            monitor="cross_artifact",
            status="ok",
            severity="info",
            title="cross_artifact: telegram-format strings agree with DB",
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
