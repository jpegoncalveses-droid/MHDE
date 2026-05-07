from __future__ import annotations

import pytest

from storage.db import get_connection, init_schema
from notifications.dedupe import is_duplicate
from notifications.templates import format_telegram_alert, format_email_digest
from notifications.telegram import TelegramNotifier
from notifications.email import EmailNotifier


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def test_telegram_noop_without_token(conn):
    cfg = {"notifications": {"telegram": {"enabled": True}}}
    notifier = TelegramNotifier(cfg, conn)
    assert not notifier.enabled


def test_email_noop_without_smtp(conn):
    cfg = {"notifications": {"email": {"enabled": True}}}
    notifier = EmailNotifier(cfg, conn)
    assert not notifier.enabled


def test_dedupe_no_prior_alert(conn):
    assert not is_duplicate(conn, "AAPL", "telegram", 14)


def test_dedupe_detects_recent_alert(conn):
    from datetime import datetime
    conn.execute(
        """
        INSERT INTO alerts (alert_id, ticker, channel, alert_type, status, dedupe_key, sent_at)
        VALUES ('abc', 'AAPL', 'telegram', 'candidate', 'sent', 'AAPL:telegram', ?)
        """,
        [datetime.utcnow()],
    )
    assert is_duplicate(conn, "AAPL", "telegram", 14)


def test_format_telegram_alert():
    msg = format_telegram_alert({
        "ticker": "NVDA", "company_name": "NVIDIA", "tier": "A",
        "total_score": 82, "why_ranked": "Strong catalyst.",
    })
    assert "NVDA" in msg
    assert "A-Tier" in msg
    assert "research candidate" in msg.lower()


def test_format_email_digest():
    subject, body = format_email_digest({
        "run_id": "abc123",
        "candidates": [{"ticker": "TSLA", "tier": "B", "total_score": 65}],
        "sent": 1,
    })
    assert "TSLA" in body
    assert "abc123" in body
