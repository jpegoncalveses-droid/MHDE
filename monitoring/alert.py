"""Shared alert dispatcher for the monitoring/ package.

Goal: every monitor returns a `MonitorResult` and uses one entry point
to fire Telegram. That makes severity / dedup / dry-run handling
consistent across all 6 monitors.

Telegram path bottoms out in `fx.bot.telegram_bot.send_message` (same
helper the FX bot uses). Set `MONITORING_DRY_RUN=true` to suppress
real sends during testing or manual one-off runs.

Throttle / dedup (since 2026-05-13):

    On the 15-min cadence the same warn-level alert was firing on
    every cycle. ``send_alert`` now persists the last-sent payload's
    severity and SHA into ``monitor_alert_state`` (migration v10) and
    suppresses identical re-sends, with three escape valves:

      * payload SHA changed → send (the underlying state actually
        moved)
      * severity changed → send (escalation / de-escalation matters,
        even if some lines coincidentally match)
      * heartbeat window elapsed → send (so a stuck red doesn't go
        unnoticed across days)

    A transition from warn/critical back to OK emits one
    ``RECOVERED`` message before state is updated to ``info``.

    State is updated regardless of ``MONITORING_DRY_RUN`` so the
    operator can verify the throttle deterministically in a dry-run
    invocation.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

logger = logging.getLogger("mhde.monitoring.alert")


Severity = Literal["info", "warn", "critical"]

_MONITORING_YAML = Path(__file__).resolve().parent.parent / "config" / "monitoring.yaml"

# Defaults — overridable by config/monitoring.yaml.
_DEFAULT_THROTTLE = {"enabled": True, "cooldown_hours": 4, "heartbeat_hours": 24}


@dataclass
class MonitorResult:
    """Standard return shape from every monitor's run()."""

    monitor: str                    # e.g. "dashboard_consistency"
    status: Literal["ok", "warn", "fail"]
    severity: Severity              # informs alert prefix and routing
    title: str                      # short, e.g. "ML predictions stale"
    body: str = ""                  # markdown-ish details for Telegram
    metrics: dict[str, Any] = field(default_factory=dict)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def is_alert_worthy(self) -> bool:
        return self.status in {"warn", "fail"}

    def to_telegram_text(self) -> str:
        """Format the result for Telegram. Severity prefix + title + body.

        The prefix is a spelled-out token (``INFO•``/``WARN•``/``CRITICAL•``)
        because the previous ``[!]``/``[!!]`` glyphs were too easy to misread
        on mobile — operators routinely interpreted ``[!]`` as "error" and
        the visual delta to ``[!!]`` critical was lost in tiny line-height.
        """
        prefix = {
            "info": "INFO • MHDE monitor",
            "warn": "WARN • MHDE monitor",
            "critical": "CRITICAL • MHDE monitor",
        }[self.severity]
        lines = [f"{prefix}: {self.monitor}", "", self.title]
        if self.body:
            lines.extend(["", self.body])
        if self.metrics:
            lines.extend(["", "Metrics:"])
            for k, v in self.metrics.items():
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)


def _is_dry_run() -> bool:
    return os.environ.get("MONITORING_DRY_RUN", "").lower() in {"1", "true", "yes"}


def _load_throttle_config() -> dict:
    """Read ``alert_throttle`` block from ``config/monitoring.yaml``."""
    try:
        import yaml
        if not _MONITORING_YAML.exists():
            return dict(_DEFAULT_THROTTLE)
        cfg = yaml.safe_load(_MONITORING_YAML.read_text()) or {}
    except Exception:
        logger.exception("alert: failed to load %s", _MONITORING_YAML)
        return dict(_DEFAULT_THROTTLE)
    merged = dict(_DEFAULT_THROTTLE)
    merged.update(cfg.get("alert_throttle") or {})
    return merged


def _open_default_conn():
    """Open a writable MHDE DuckDB connection using the project config."""
    import duckdb
    from storage.config import load_engine_config
    return duckdb.connect(load_engine_config()["db_path"])


def _payload_sha(result: MonitorResult) -> str:
    """Hash title + body only (metrics drift run-to-run and would defeat dedup)."""
    return hashlib.sha256(
        f"{result.title}\n{result.body}".encode("utf-8")
    ).hexdigest()


def _load_alert_state(conn, monitor: str) -> Optional[dict]:
    """Return the persisted state row for ``monitor`` or ``None``."""
    try:
        row = conn.execute(
            "SELECT last_payload_sha, last_severity, last_sent_at "
            "FROM monitor_alert_state WHERE monitor = ?",
            [monitor],
        ).fetchone()
    except Exception:
        # Table not migrated yet — fail open so a stale DB doesn't silence alerts.
        logger.exception("alert: monitor_alert_state read failed for %s", monitor)
        return None
    if row is None:
        return None
    return {
        "last_payload_sha": row[0],
        "last_severity": row[1],
        "last_sent_at": row[2],
    }


def _save_alert_state(conn, monitor: str, payload_sha: str,
                      severity: str, sent_at: datetime) -> None:
    try:
        conn.execute(
            """
            INSERT INTO monitor_alert_state
                (monitor, last_payload_sha, last_severity, last_sent_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (monitor) DO UPDATE SET
                last_payload_sha = excluded.last_payload_sha,
                last_severity    = excluded.last_severity,
                last_sent_at     = excluded.last_sent_at
            """,
            [monitor, payload_sha, severity, sent_at],
        )
    except Exception:
        logger.exception("alert: monitor_alert_state write failed for %s", monitor)


def _decide_send(state: Optional[dict], payload_sha: str, severity: str,
                 now: datetime, heartbeat_hours: float) -> tuple[bool, str]:
    """Pure decision: send or throttle, plus the reason for logs."""
    if state is None:
        return True, "first-send"
    if state["last_severity"] != severity:
        return True, "severity-change"
    if state["last_payload_sha"] != payload_sha:
        return True, "payload-change"
    last_sent = state["last_sent_at"]
    if last_sent is None:
        return True, "missing-last-sent"
    age_hours = (now - last_sent).total_seconds() / 3600.0
    if age_hours >= heartbeat_hours:
        return True, "heartbeat"
    return False, "throttled"


def _telegram_send(text: str) -> bool:
    """Bottom-of-stack Telegram dispatch. Honors MONITORING_DRY_RUN."""
    if _is_dry_run():
        logger.info("MONITORING_DRY_RUN=true — skipping real Telegram send")
        return False
    try:
        from fx.bot.telegram_bot import send_message
    except ImportError:
        logger.error("fx.bot.telegram_bot.send_message not importable")
        return False
    msg_id = send_message(text)
    return msg_id is not None


def send_alert(result: MonitorResult, conn=None, now: Optional[datetime] = None) -> bool:
    """Send ``result`` to Telegram if alert-worthy *and* not throttled.

    ``conn`` and ``now`` are injectable for tests; default to the project
    DuckDB and current UTC. State is updated even under ``MONITORING_DRY_RUN``
    so dry-run smoke tests can verify the throttle works.

    Returns ``True`` if a real Telegram send completed, ``False`` otherwise
    (OK status without prior alert, throttled, dry-run, or send failure).
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)

    throttle = _load_throttle_config()
    enabled = bool(throttle.get("enabled", True))
    heartbeat_hours = float(throttle.get("heartbeat_hours", 24))

    close_conn = False
    if conn is None:
        try:
            conn = _open_default_conn()
            close_conn = True
        except Exception:
            logger.exception("alert: could not open MHDE DB — bypassing throttle")
            conn = None

    try:
        state = _load_alert_state(conn, result.monitor) if conn is not None else None
        payload_sha = _payload_sha(result)

        if not result.is_alert_worthy():
            # OK status — emit recovery only on transition out of warn/critical.
            if state is not None and state.get("last_severity") in ("warn", "critical"):
                recovery_text = (
                    f"RECOVERED • MHDE monitor: {result.monitor}\n"
                    f"\nstatus: OK (was {state['last_severity']})"
                )
                logger.warning("MONITOR RECOVERED — %s", result.monitor)
                sent = _telegram_send(recovery_text)
                if conn is not None:
                    _save_alert_state(conn, result.monitor, payload_sha,
                                      "info", now)
                return sent
            logger.info("monitor %s OK — no alert", result.monitor)
            return False

        # Alert-worthy result. Decide throttle.
        if enabled and conn is not None:
            should_send, reason = _decide_send(
                state, payload_sha, result.severity, now, heartbeat_hours,
            )
            if not should_send:
                logger.info("monitor %s throttled (%s)", result.monitor, reason)
                return False
            logger.info("monitor %s sending (%s)", result.monitor, reason)

        payload = result.to_telegram_text()
        logger.warning("MONITOR ALERT — %s\n%s", result.monitor, payload)
        sent = _telegram_send(payload)
        if conn is not None:
            # Persist state on every send decision (incl. dry-run) so the
            # next cycle sees the new baseline. _telegram_send's return only
            # reflects network success; the throttle uses "we decided to
            # send" semantics.
            _save_alert_state(conn, result.monitor, payload_sha,
                              result.severity, now)
        return sent
    finally:
        if close_conn and conn is not None:
            conn.close()


def send_text(text: str) -> bool:
    """Send a pre-formatted plain-text message to Telegram unconditionally.

    Unlike :func:`send_alert` (which suppresses OK results and throttles),
    this always attempts to send — used by the pipeline monitor, which posts
    a status message every run (green or red). Still respects
    ``MONITORING_DRY_RUN`` and always logs the payload at INFO level.
    Returns True if a message was actually sent.
    """
    logger.info("MONITOR MESSAGE\n%s", text)
    return _telegram_send(text)
