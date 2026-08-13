"""Discovery-pass protected window: the scheduled run pauses the three hourly compactor
timers on entry and restores them on ANY exit (ExecStopPost runs on oom-kill too), with a
transient failsafe so a user-manager death cannot leave compaction paused.

Basis (data/processed/stage1_breadth_cap_measurement.md §9-13): every uncontained failure
of the full pass was a HOST-level OOM while an hourly compactor or ambient burst ran
beside the ~11.5G-resident pass; the timer's first scheduled run died 4 minutes after the
hourly brain-compact fired. Pausing firehose+okx+brain compact timers removes ~23 min/h
of known co-tenant spikes from the pass's ~4h exposure window. HONESTY: this reduces
collision probability; it does not remove the unidentified ambient burst that killed
gate-6 under a partial pause.
"""
from __future__ import annotations

import configparser
import os
import pathlib
import stat
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "discovery_compact_window.sh"
SVC = REPO / "systemd" / "mhde-brain-discover.service"

_TIMERS = ("mhde-capture-firehose-compact.timer",
           "mhde-capture-okx-firehose-compact.timer",
           "mhde-brain-compact.timer")
_FAILSAFE = "mhde-discover-compact-failsafe"


def _parse(unit_path: pathlib.Path) -> configparser.ConfigParser:
    p = configparser.ConfigParser(strict=False, interpolation=None)
    p.read_string(unit_path.read_text())
    return p


def _run_with_mocks(tmp_path, action):
    log = tmp_path / "calls.log"
    mock = tmp_path / "mock.sh"
    mock.write_text(f'#!/bin/bash\necho "$0 $@" >> "{log}"\n')
    mock.chmod(mock.stat().st_mode | stat.S_IEXEC)
    sysctl = tmp_path / "systemctl"
    run = tmp_path / "systemd-run"
    for m in (sysctl, run):
        m.write_text(f'#!/bin/bash\necho "{m.name} $@" >> "{log}"\n')
        m.chmod(m.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, SYSTEMCTL=str(sysctl), SYSTEMD_RUN=str(run))
    res = subprocess.run(["/bin/bash", str(SCRIPT), action], env=env,
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return log.read_text() if log.exists() else ""


def test_script_exists_and_is_valid_bash():
    assert SCRIPT.exists()
    res = subprocess.run(["/bin/bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_pause_stops_all_three_timers_and_arms_failsafe(tmp_path):
    calls = _run_with_mocks(tmp_path, "pause")
    stop_line = next((ln for ln in calls.splitlines()
                      if "systemctl" in ln and " stop " in ln and _TIMERS[0] in ln), "")
    for t in _TIMERS:
        assert t in stop_line, f"{t} not stopped together: {calls}"
    fs = next((ln for ln in calls.splitlines() if "systemd-run" in ln), "")
    assert _FAILSAFE in fs and "--on-active" in fs, f"failsafe not armed: {calls}"
    for t in _TIMERS:
        assert t in fs, f"failsafe does not restart {t}: {calls}"


def test_resume_restarts_all_three_timers_and_cancels_failsafe(tmp_path):
    calls = _run_with_mocks(tmp_path, "resume")
    start_line = next((ln for ln in calls.splitlines()
                       if "systemctl" in ln and " start " in ln and _TIMERS[0] in ln), "")
    for t in _TIMERS:
        assert t in start_line, f"{t} not restarted together: {calls}"
    assert any(_FAILSAFE in ln and " stop " in ln for ln in calls.splitlines()), \
        f"failsafe not cancelled on resume: {calls}"


def test_unknown_action_fails():
    res = subprocess.run(["/bin/bash", str(SCRIPT), "bogus"], capture_output=True, text=True)
    assert res.returncode != 0


def test_discover_service_wires_pause_and_resume():
    svc = _parse(SVC)["Service"]
    pres = [v for k, v in _parse(SVC)["Service"].items() if k.startswith("execstartpre")]
    body = SVC.read_text()
    assert "discovery_compact_window.sh pause" in body
    assert "ExecStopPost" in body and "discovery_compact_window.sh resume" in body
    # the protections that made the pass fit stay intact
    assert svc["memorymax"] == "13G"
    assert svc["nice"] == "19"
