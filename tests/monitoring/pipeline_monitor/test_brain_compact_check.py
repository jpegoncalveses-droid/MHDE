"""Brain-store compactor freshness check (KI-165).

The check turns an absent / stale success-heartbeat into a RED continuous-monitor step, so a
SIGKILL-unhandleable OOM-kill (or nonzero exit) of the hourly compactor surfaces on Telegram
within a few hours instead of failing silently for days.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from monitoring.pipeline_monitor.checks import brain as B
from monitoring.pipeline_monitor.core import Status


def _now() -> datetime:
    return datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def _hb(hours_ago: float) -> dict:
    ns = int((_now() - timedelta(hours=hours_ago)).timestamp() * 1_000_000_000)
    return {"last_success_ns": ns, "sealed_compacted": 3}


def test_fresh_heartbeat_is_green():
    sr = B.evaluate(_now(), _hb(1.0), stale_red_hours=3.0)
    assert sr.status is Status.GREEN
    assert "1.0h ago" in sr.detail


def test_stale_heartbeat_is_red():
    sr = B.evaluate(_now(), _hb(5.0), stale_red_hours=3.0)
    assert sr.status is Status.RED
    assert "5.0h ago" in sr.detail and "KI-165" in sr.detail


def test_just_over_threshold_is_red():
    sr = B.evaluate(_now(), _hb(3.01), stale_red_hours=3.0)
    assert sr.status is Status.RED


def test_missing_heartbeat_is_red():
    sr = B.evaluate(_now(), None, stale_red_hours=3.0)
    assert sr.status is Status.RED
    assert "never completed" in sr.detail


def test_malformed_heartbeat_is_red():
    sr = B.evaluate(_now(), {"last_success_ns": "nope"}, stale_red_hours=3.0)
    assert sr.status is Status.RED
    assert "malformed" in sr.detail


def test_check_reads_missing_file_as_red(tmp_path):
    sr = B.check_brain_compact_freshness(
        _now(), heartbeat_path=str(tmp_path / "absent.json"), stale_red_hours=3.0)
    assert sr.status is Status.RED


def test_check_reads_fresh_file_as_green(tmp_path):
    p = tmp_path / "hb.json"
    p.write_text(json.dumps(_hb(0.5)))
    sr = B.check_brain_compact_freshness(
        _now(), heartbeat_path=str(p), stale_red_hours=3.0)
    assert sr.status is Status.GREEN


def test_check_reads_corrupt_file_as_red(tmp_path):
    p = tmp_path / "hb.json"
    p.write_text("{ not json")
    sr = B.check_brain_compact_freshness(
        _now(), heartbeat_path=str(p), stale_red_hours=3.0)
    assert sr.status is Status.RED
