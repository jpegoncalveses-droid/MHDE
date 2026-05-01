from __future__ import annotations

import logging
import uuid
from datetime import datetime

import duckdb

from notifications.dedupe import is_duplicate
from notifications.templates import format_telegram_alert

logger = logging.getLogger("mhde.notifications.telegram")

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(self, cfg: dict, conn: duckdb.DuckDBPyConnection):
        self.conn = conn
        self.token = cfg.get("telegram_bot_token") or ""
        self.chat_id = cfg.get("telegram_chat_id") or ""
        notif_cfg = cfg.get("notifications", {}).get("telegram", {})
        self.enabled = notif_cfg.get("enabled", False) and bool(self.token) and bool(self.chat_id)
        self.dedup_days = cfg.get("notifications", {}).get("dedup_days", 14)
        self.min_tier = notif_cfg.get("min_tier", "A")

    def send_alert(self, hypothesis: dict) -> bool:
        ticker = hypothesis.get("ticker", "")

        if not self.enabled:
            logger.debug("Telegram not configured — skipping alert for %s", ticker)
            return False

        tier = hypothesis.get("tier", "D")
        if self.min_tier == "A" and tier != "A":
            return False

        if is_duplicate(self.conn, ticker, "telegram", self.dedup_days):
            logger.info("Dedup: skipping Telegram alert for %s (sent recently)", ticker)
            return False

        message = format_telegram_alert(hypothesis)
        try:
            import requests
            resp = requests.post(
                _TELEGRAM_API.format(token=self.token),
                json={"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"},
                timeout=10,
            )
            resp.raise_for_status()
            self._log_alert(ticker, "sent", message)
            logger.info("Telegram alert sent for %s", ticker)
            return True
        except Exception as exc:
            logger.error("Telegram alert failed for %s: %s", ticker, exc)
            self._log_alert(ticker, "failed", message, error=str(exc))
            return False

    def _log_alert(
        self, ticker: str, status: str, message: str, error: str | None = None
    ) -> None:
        try:
            self.conn.execute(
                """
                INSERT INTO alerts (alert_id, ticker, channel, alert_type, status,
                    dedupe_key, message, sent_at, error_message)
                VALUES (?, ?, 'telegram', 'candidate', ?, ?, ?, ?, ?)
                """,
                [
                    uuid.uuid4().hex[:16], ticker, status,
                    f"{ticker}:telegram", message, datetime.utcnow(), error,
                ],
            )
        except Exception as exc:
            logger.debug("Could not log alert: %s", exc)
