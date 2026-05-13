"""End-to-end tests for monitoring/alert.py throttle + dedup.

State is persisted in the ``monitor_alert_state`` table (migration v10).
Tests run against the in-memory ``temp_db`` fixture and patch
``fx.bot.telegram_bot.send_message`` to count would-be Telegram sends.

The contract under test:
    * First alert-worthy result for a monitor always sends.
    * Identical payload (same title + body) + same severity within the
      heartbeat window is throttled.
    * Payload change sends.
    * Severity escalation/de-escalation sends.
    * Heartbeat re-send fires after ``heartbeat_hours`` of silence even
      when state is unchanged.
    * A transition from warn/critical back to OK emits one "RECOVERED"
      message and updates state to ``info``; subsequent OK runs are silent.
    * ``alert_throttle.enabled: false`` bypasses the throttle entirely.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from monitoring import alert as alert_mod
from monitoring.alert import MonitorResult


T0 = datetime(2026, 5, 14, 12, 0, 0)


@pytest.fixture(autouse=True)
def _not_dry_run(monkeypatch):
    """The throttle is orthogonal to dry-run, but we exercise the real
    send path in these tests so a missed throttle would manifest as an
    extra captured call."""
    monkeypatch.delenv("MONITORING_DRY_RUN", raising=False)


@pytest.fixture
def captured(monkeypatch):
    """Replace fx.bot.telegram_bot.send_message with a recorder. send_alert
    imports this lazily, so the patch must target the source module."""
    calls: list[str] = []

    def _fake_send(text, **kwargs):
        calls.append(text)
        return 12345  # message_id

    import fx.bot.telegram_bot as tb
    monkeypatch.setattr(tb, "send_message", _fake_send)
    return calls


@pytest.fixture(autouse=True)
def _default_throttle_config(monkeypatch):
    """Bypass config/monitoring.yaml: tests should drive throttle settings
    explicitly, not pick up whatever the live config file says."""
    monkeypatch.setattr(
        alert_mod, "_load_throttle_config",
        lambda: {"enabled": True, "cooldown_hours": 4, "heartbeat_hours": 24},
    )


def _warn_result(monitor="m", title="t", body="b"):
    return MonitorResult(monitor=monitor, status="warn", severity="warn",
                         title=title, body=body)


def _crit_result(monitor="m", title="t", body="b"):
    return MonitorResult(monitor=monitor, status="fail", severity="critical",
                         title=title, body=body)


def _ok_result(monitor="m", title="ok"):
    return MonitorResult(monitor=monitor, status="ok", severity="info",
                         title=title)


# ──────────────────────────────────────────────────────────────────────
# the rule list
# ──────────────────────────────────────────────────────────────────────


def test_first_send_always_goes_through(temp_db, captured):
    sent = alert_mod.send_alert(_warn_result(), conn=temp_db, now=T0)
    assert sent is True
    assert len(captured) == 1
    # State row exists for the monitor
    row = temp_db.execute(
        "SELECT last_severity FROM monitor_alert_state WHERE monitor = 'm'"
    ).fetchone()
    assert row is not None
    assert row[0] == "warn"


def test_identical_payload_within_window_is_throttled(temp_db, captured):
    alert_mod.send_alert(_warn_result(), conn=temp_db, now=T0)
    assert len(captured) == 1

    # Re-fire 15 min later — same title, same body, same severity.
    sent = alert_mod.send_alert(
        _warn_result(), conn=temp_db, now=T0 + timedelta(minutes=15)
    )
    assert sent is False
    assert len(captured) == 1  # no new Telegram call


def test_payload_change_sends(temp_db, captured):
    alert_mod.send_alert(_warn_result(title="A", body="x"), conn=temp_db, now=T0)
    sent = alert_mod.send_alert(
        _warn_result(title="A", body="y"),  # body changed
        conn=temp_db, now=T0 + timedelta(minutes=15),
    )
    assert sent is True
    assert len(captured) == 2


def test_severity_transition_sends(temp_db, captured):
    alert_mod.send_alert(_warn_result(), conn=temp_db, now=T0)
    sent = alert_mod.send_alert(
        _crit_result(title="t", body="b"),  # warn → critical
        conn=temp_db, now=T0 + timedelta(minutes=15),
    )
    assert sent is True
    assert len(captured) == 2


def test_heartbeat_resend_after_silence(temp_db, captured):
    alert_mod.send_alert(_warn_result(), conn=temp_db, now=T0)
    # 25h later — heartbeat_hours default is 24, identical payload.
    sent = alert_mod.send_alert(
        _warn_result(), conn=temp_db, now=T0 + timedelta(hours=25)
    )
    assert sent is True
    assert len(captured) == 2


def test_recovery_message_on_transition_to_ok(temp_db, captured):
    alert_mod.send_alert(_warn_result(), conn=temp_db, now=T0)
    # Next cycle: monitor returns OK.
    sent = alert_mod.send_alert(
        _ok_result(), conn=temp_db, now=T0 + timedelta(minutes=15)
    )
    assert sent is True
    assert len(captured) == 2
    assert "RECOVERED" in captured[1]
    assert "m" in captured[1]
    # State is now info; subsequent OK calls stay silent.
    again = alert_mod.send_alert(
        _ok_result(), conn=temp_db, now=T0 + timedelta(minutes=30)
    )
    assert again is False
    assert len(captured) == 2


def test_throttle_disabled_always_sends(temp_db, captured, monkeypatch):
    monkeypatch.setattr(
        alert_mod, "_load_throttle_config",
        lambda: {"enabled": False, "cooldown_hours": 4, "heartbeat_hours": 24},
    )
    for offset in (0, 5, 10):
        sent = alert_mod.send_alert(
            _warn_result(), conn=temp_db, now=T0 + timedelta(minutes=offset)
        )
        assert sent is True
    assert len(captured) == 3


def test_ok_result_with_no_prior_state_is_silent(temp_db, captured):
    """The first run after install on a healthy system: no alert, no row
    written, no recovery emitted."""
    sent = alert_mod.send_alert(_ok_result(), conn=temp_db, now=T0)
    assert sent is False
    assert captured == []
    row = temp_db.execute(
        "SELECT 1 FROM monitor_alert_state WHERE monitor = 'm'"
    ).fetchone()
    assert row is None


def test_dry_run_still_updates_state(temp_db, captured, monkeypatch):
    """MONITORING_DRY_RUN suppresses the real Telegram call but the state
    table must still update — otherwise the throttle would re-send every
    cycle whenever dry-run is set."""
    monkeypatch.setenv("MONITORING_DRY_RUN", "true")
    sent = alert_mod.send_alert(_warn_result(), conn=temp_db, now=T0)
    assert sent is False  # dry-run suppresses the send
    assert captured == []
    # But state was recorded — re-running is throttled.
    sent2 = alert_mod.send_alert(
        _warn_result(), conn=temp_db, now=T0 + timedelta(minutes=15)
    )
    assert sent2 is False
    assert captured == []  # still no real send
    row = temp_db.execute(
        "SELECT last_severity FROM monitor_alert_state WHERE monitor = 'm'"
    ).fetchone()
    assert row is not None and row[0] == "warn"
