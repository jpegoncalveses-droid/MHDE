"""Shared alert dispatcher for the monitoring/ package.

Goal: every monitor returns a `MonitorResult` and uses one entry point
to fire Telegram. That makes severity / dedup / dry-run handling
consistent across all 6 monitors.

Telegram path bottoms out in `fx.bot.telegram_bot.send_message` (same
helper the FX bot uses). Set `MONITORING_DRY_RUN=true` to suppress
real sends during testing or manual one-off runs.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Literal, Optional

logger = logging.getLogger("mhde.monitoring.alert")


Severity = Literal["info", "warn", "critical"]


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
        """Format the result for Telegram. Severity prefix + title + body."""
        prefix = {
            "info": "[i] MHDE monitor",
            "warn": "[!] MHDE monitor",
            "critical": "[!!] MHDE monitor",
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


def send_alert(result: MonitorResult) -> bool:
    """Send `result` to Telegram if alert-worthy.

    Returns True if a message was actually sent, False if suppressed
    (dry-run, or status=ok). Always logs the payload at INFO level for
    later forensic reading.
    """
    if not result.is_alert_worthy():
        logger.info("monitor %s OK — no alert", result.monitor)
        return False

    payload = result.to_telegram_text()
    logger.warning("MONITOR ALERT — %s\n%s", result.monitor, payload)

    if _is_dry_run():
        logger.info("MONITORING_DRY_RUN=true — skipping real Telegram send")
        return False

    try:
        from fx.bot.telegram_bot import send_message
    except ImportError:
        logger.error("fx.bot.telegram_bot.send_message not importable")
        return False

    msg_id = send_message(payload)
    return msg_id is not None


def send_text(text: str) -> bool:
    """Send a pre-formatted plain-text message to Telegram unconditionally.

    Unlike :func:`send_alert` (which suppresses OK results), this always
    attempts to send — used by the pipeline monitor, which posts a status
    message every run (green or red). Still respects ``MONITORING_DRY_RUN``
    and always logs the payload at INFO level. Returns True if a message was
    actually sent.
    """
    logger.info("MONITOR MESSAGE\n%s", text)

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
