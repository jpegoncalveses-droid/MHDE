"""Discovery-pass protected window: the scheduled run pauses the three hourly compactor
timers on entry and restores them on ANY exit (ExecStopPost runs on oom-kill too), with a
transient failsafe covering a failed/skipped ExecStopPost.

Basis (data/processed/stage1_breadth_cap_measurement.md §9-13): every uncontained failure
of the full pass was a HOST-level OOM while an hourly compactor or ambient burst ran
beside the ~11.5G-resident pass; the timer's first scheduled run died 4 minutes after the
hourly brain-compact fired. HONESTY: pausing removes ~23 min/h of known co-tenant spikes,
not the unidentified ambient burst class that killed gate-6 under a partial pause.
"""
from __future__ import annotations

import os
import pathlib
import re
import stat
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "discovery_compact_window.sh"
SVC = REPO / "systemd" / "mhde-brain-discover.service"

_TIMERS = ("mhde-capture-firehose-compact.timer",
           "mhde-capture-okx-firehose-compact.timer",
           "mhde-brain-compact.timer")
_FAILSAFE = "mhde-discover-compact-failsafe"


def _run_with_mocks(tmp_path, action, *, systemctl_body=None):
    log = tmp_path / "calls.log"
    default = f'#!/bin/bash\necho "$(basename "$0") $@" >> "{log}"\n'
    sysctl = tmp_path / "systemctl"
    sysctl.write_text(systemctl_body.replace("__LOG__", str(log)) if systemctl_body else default)
    run = tmp_path / "systemd-run"
    run.write_text(default)
    for m in (sysctl, run):
        m.chmod(m.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, SYSTEMCTL=str(sysctl), SYSTEMD_RUN=str(run))
    res = subprocess.run(["/bin/bash", str(SCRIPT), action], env=env,
                         capture_output=True, text=True, timeout=30)
    return res, (log.read_text() if log.exists() else "")


def test_script_exists_and_is_valid_bash():
    assert SCRIPT.exists()
    res = subprocess.run(["/bin/bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_timer_names_match_repo_units():
    # the script's timer list must name real tracked units — a typo silently no-ops
    for t in _TIMERS:
        assert (REPO / "systemd" / t).exists(), f"repo has no systemd/{t}"
        assert t in SCRIPT.read_text(), f"script does not manage {t}"


def test_pause_clears_stale_failsafe_then_stops_timers_then_arms():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        res, calls = _run_with_mocks(pathlib.Path(td), "pause")
    assert res.returncode == 0, res.stderr
    lines = calls.splitlines()
    # order: stale-failsafe clear BEFORE the arm; timers stopped together
    clear_i = next(i for i, ln in enumerate(lines)
                   if "systemctl" in ln and " stop " in ln and _FAILSAFE in ln)
    stop_i = next(i for i, ln in enumerate(lines)
                  if "systemctl" in ln and " stop " in ln and _TIMERS[0] in ln)
    arm_i = next(i for i, ln in enumerate(lines) if "systemd-run" in ln)
    assert clear_i < arm_i, f"stale failsafe not cleared before arming: {calls}"
    for t in _TIMERS:
        assert t in lines[stop_i], f"{t} not stopped together: {calls}"
    arm = lines[arm_i]
    assert _FAILSAFE in arm and "--on-active" in arm
    for t in _TIMERS:
        assert t in arm, f"failsafe does not restart {t}: {calls}"
    # stale-clear also removes a wedged transient SERVICE, not just the timer
    assert f"{_FAILSAFE}.service" in lines[clear_i] or any(
        f"{_FAILSAFE}.service" in ln for ln in lines[:arm_i]), calls


def _armed_guard_command(tmp_path):
    """Extract the exact command the failsafe would run (everything after `/bin/bash -c`)."""
    res, calls = _run_with_mocks(tmp_path, "pause")
    assert res.returncode == 0, res.stderr
    arm = next(ln for ln in calls.splitlines() if "systemd-run" in ln)
    return arm.split("/bin/bash -c ", 1)[1]


def _exec_guard(tmp_path, guard_cmd, active_state):
    """EXECUTE the armed guard with a mocked systemctl reporting `active_state`."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "guard_calls.log"
    mock = bindir / "systemctl"
    mock.write_text(
        '#!/bin/bash\n'
        f'echo "systemctl $@" >> "{log}"\n'
        'case "$*" in *"show -p ActiveState"*) echo "' + active_state + '";; esac\n')
    mock.chmod(mock.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    res = subprocess.run(["/bin/bash", "-c", guard_cmd], env=env,
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return log.read_text() if log.exists() else ""


def test_failsafe_guard_noops_while_pass_is_running(tmp_path):
    # BEHAVIORAL (re-review finding 1): a Type=oneshot pass reports "activating" for its
    # whole run — the armed guard must NOT start the compactors in that state. A guard
    # built on `is-active` (exit 3 for activating) fails this test.
    guard = _armed_guard_command(tmp_path)
    for busy in ("activating", "active", "deactivating"):
        calls = _exec_guard(tmp_path / busy, guard, busy)
        assert not any(" start " in ln for ln in calls.splitlines()), \
            f"guard restarted compactors while pass state={busy}: {calls}"


def test_failsafe_guard_restores_when_pass_is_gone(tmp_path):
    guard = _armed_guard_command(tmp_path)
    for gone in ("inactive", "failed"):
        calls = _exec_guard(tmp_path / gone, guard, gone)
        start = next((ln for ln in calls.splitlines() if " start " in ln), "")
        for t in _TIMERS:
            assert t in start, f"guard did not restore {t} when pass state={gone}: {calls}"


def test_resume_restarts_all_three_and_cancels_failsafe_only_on_success():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        res, calls = _run_with_mocks(pathlib.Path(td), "resume")
    assert res.returncode == 0, res.stderr
    lines = calls.splitlines()
    start_i = next(i for i, ln in enumerate(lines)
                   if "systemctl" in ln and " start " in ln and _TIMERS[0] in ln)
    for t in _TIMERS:
        assert t in lines[start_i], f"{t} not restarted together: {calls}"
    assert any(_FAILSAFE in ln and " stop " in ln for ln in lines), \
        f"failsafe not cancelled after successful restart: {calls}"


def test_resume_leaves_failsafe_armed_when_restart_fails():
    # F1 guard: if the timer restart fails, the failsafe must NOT be cancelled.
    import tempfile
    body = ('#!/bin/bash\necho "$(basename "$0") $@" >> "__LOG__"\n'
            'case "$*" in *" start "*) exit 1;; esac\n')
    with tempfile.TemporaryDirectory() as td:
        res, calls = _run_with_mocks(pathlib.Path(td), "resume", systemctl_body=body)
    assert res.returncode == 0                       # resume itself does not hard-fail
    assert "RESUME FAILED" in res.stderr             # ...but it SAYS so (F2)
    assert not any(_FAILSAFE in ln and " stop " in ln for ln in calls.splitlines()), \
        f"failsafe cancelled despite failed restart: {calls}"


def test_pause_failures_are_loud():
    # F2: a fully broken pause must not report silent success on stderr.
    import tempfile
    body = '#!/bin/bash\nexit 1\n'
    with tempfile.TemporaryDirectory() as td:
        res, _ = _run_with_mocks(pathlib.Path(td), "pause", systemctl_body=body)
    assert "PAUSE FAILED" in res.stderr


def test_unknown_action_exits_2():
    res = subprocess.run(["/bin/bash", str(SCRIPT), "bogus"], capture_output=True, text=True)
    assert res.returncode == 2


def test_discover_service_binds_pause_and_resume_to_the_right_directives():
    body = SVC.read_text()
    # F5: assert DIRECTIVE BINDING, not substrings — resume must live on ExecStopPost
    # (the only directive that runs after an oom-kill), pause on ExecStartPre.
    assert re.search(r"^ExecStartPre=.*discovery_compact_window\.sh pause\s*$", body, re.M), \
        "pause not bound to ExecStartPre"
    assert re.search(r"^ExecStopPost=.*discovery_compact_window\.sh resume\s*$", body, re.M), \
        "resume not bound to ExecStopPost"
    assert not re.search(r"^ExecStop(?!Post)=.*resume", body, re.M)
    # the pass's own protections stay intact
    assert re.search(r"^MemoryMax=13G\s*$", body, re.M)
    assert re.search(r"^Nice=19\s*$", body, re.M)
