"""Unit tests for monitoring/alert_state_store.py — the JSON sidecar that
replaces the DuckDB ``monitor_alert_state`` table (KI-150 part 2/3).

Each monitor's alert-throttle state must persist across process invocations
*without* requiring a writable DuckDB connection. The store must be safe
against concurrent writers via fcntl.flock and atomic enough that a
crashed write doesn't corrupt the file.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from multiprocessing import Process

import pytest

from monitoring import alert_state_store as ass


T0 = datetime(2026, 5, 14, 12, 0, 0)


def test_load_state_missing_file_returns_none(tmp_path):
    p = tmp_path / "missing.json"
    assert ass.load_state("any_monitor", path=p) is None


def test_load_state_unknown_monitor_returns_none(tmp_path):
    p = tmp_path / "state.json"
    ass.save_state("other", "sha1", "warn", T0, path=p)
    assert ass.load_state("missing", path=p) is None


def test_save_then_load_round_trips_all_fields(tmp_path):
    p = tmp_path / "state.json"
    ass.save_state("m1", "abc123", "warn", T0, path=p)
    state = ass.load_state("m1", path=p)
    assert state is not None
    assert state["last_payload_sha"] == "abc123"
    assert state["last_severity"] == "warn"
    assert state["last_sent_at"] == T0


def test_save_overwrites_existing_monitor_entry(tmp_path):
    p = tmp_path / "state.json"
    ass.save_state("m1", "sha_old", "warn", T0, path=p)
    ass.save_state("m1", "sha_new", "critical", T0 + timedelta(hours=1), path=p)
    state = ass.load_state("m1", path=p)
    assert state["last_payload_sha"] == "sha_new"
    assert state["last_severity"] == "critical"


def test_save_preserves_other_monitors_entries(tmp_path):
    p = tmp_path / "state.json"
    ass.save_state("m1", "sha1", "warn", T0, path=p)
    ass.save_state("m2", "sha2", "critical", T0 + timedelta(hours=2), path=p)
    assert ass.load_state("m1", path=p)["last_payload_sha"] == "sha1"
    assert ass.load_state("m2", path=p)["last_payload_sha"] == "sha2"


def test_corrupt_json_file_returns_none_for_load(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("not valid json {{{")
    assert ass.load_state("m1", path=p) is None


def test_path_env_override_takes_precedence(tmp_path, monkeypatch):
    override = tmp_path / "via_env.json"
    monkeypatch.setenv("MHDE_MONITOR_ALERT_STATE_PATH", str(override))
    ass.save_state("m1", "sha1", "warn", T0)  # no explicit path
    assert override.exists()
    assert ass.load_state("m1")["last_payload_sha"] == "sha1"


def _writer_proc(path_str: str, monitor: str, sha: str, sent_at_iso: str) -> None:
    """Worker for the concurrent-writer test."""
    from monitoring import alert_state_store as _ass
    from datetime import datetime as _dt
    _ass.save_state(monitor, sha, "warn", _dt.fromisoformat(sent_at_iso),
                    path=__import__("pathlib").Path(path_str))


def test_concurrent_writers_do_not_corrupt_file(tmp_path):
    """Two processes hammer save_state for distinct monitors. The file
    must end up with both entries present and parseable.
    """
    p = tmp_path / "state.json"
    procs = []
    for i in range(8):
        for monitor in ("m_a", "m_b"):
            sha = f"sha_{monitor}_{i}"
            sent_at = (T0 + timedelta(seconds=i)).isoformat()
            procs.append(Process(target=_writer_proc,
                                 args=(str(p), monitor, sha, sent_at)))
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=10)
    # File must parse and contain BOTH monitor entries — fcntl.flock
    # serialization protects the read-modify-write race.
    contents = p.read_text()
    data = json.loads(contents)
    assert "m_a" in data and "m_b" in data, (
        f"Both monitor entries must survive; got {data!r}"
    )
