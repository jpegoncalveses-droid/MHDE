from __future__ import annotations

import logging
import smtplib
import uuid
from datetime import datetime
from email.mime.text import MIMEText

import duckdb

from notifications.templates import format_email_digest

logger = logging.getLogger("mhde.notifications.email")


class EmailNotifier:
    def __init__(self, cfg: dict, conn: duckdb.DuckDBPyConnection):
        self.conn = conn
        self.host = cfg.get("smtp_host") or ""
        self.port = int(cfg.get("smtp_port") or 587)
        self.username = cfg.get("smtp_username") or ""
        self.password = cfg.get("smtp_password") or ""
        self.from_addr = cfg.get("email_from") or ""
        self.to_addr = cfg.get("email_to") or ""
        notif_cfg = cfg.get("notifications", {}).get("email", {})
        self.enabled = notif_cfg.get("enabled", False) and bool(self.host) and bool(self.to_addr)

    def send_digest(self, run_summary: dict) -> bool:
        if not self.enabled:
            logger.debug("Email not configured — skipping digest")
            return False

        subject, body = format_email_digest(run_summary)
        try:
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = self.from_addr
            msg["To"] = self.to_addr

            with smtplib.SMTP(self.host, self.port, timeout=15) as server:
                server.starttls()
                if self.username and self.password:
                    server.login(self.username, self.password)
                server.sendmail(self.from_addr, [self.to_addr], msg.as_string())

            self._log_alert("digest", "sent", subject)
            logger.info("Email digest sent to %s", self.to_addr)
            return True
        except Exception as exc:
            logger.error("Email digest failed: %s", exc)
            self._log_alert("digest", "failed", subject, error=str(exc))
            return False

    def _log_alert(
        self, ticker: str, status: str, message: str, error: str | None = None
    ) -> None:
        try:
            self.conn.execute(
                """
                INSERT INTO alerts (alert_id, ticker, channel, alert_type, status,
                    dedupe_key, message, sent_at, error_message)
                VALUES (?, ?, 'email', 'digest', ?, ?, ?, ?, ?)
                """,
                [
                    uuid.uuid4().hex[:16], ticker, status,
                    f"{ticker}:email", message, datetime.utcnow(), error,
                ],
            )
        except Exception as exc:
            logger.debug("Could not log email alert: %s", exc)
