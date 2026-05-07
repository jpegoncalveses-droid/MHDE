"""CLI registry regression tests.

Every CLI command wired to a systemd ExecStart must be invokable
(at least as `--help`). Plus KI-004: model artifacts gitignored.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _exec_starts_for_main_py() -> list[str]:
    """Return all `main.py <subcommand...>` invocations across systemd
    units in the repo (system-level files in systemd/ + the
    daily-analysis shell wrapper).
    """
    cmds: list[str] = []
    for unit in (REPO / "systemd").glob("*.service"):
        for line in unit.read_text().splitlines():
            if line.startswith("ExecStart="):
                cmd = line.split("=", 1)[1].strip()
                if "main.py" in cmd:
                    # Strip the python interpreter prefix
                    parts = cmd.split("main.py", 1)[1].strip().split()
                    if parts:
                        cmds.append(" ".join(parts))

    # Also: the daily-analysis wrapper invokes main.py multiple times.
    daily = REPO / ".claude" / "local_scripts" / "run_mhde_daily_analysis.sh"
    if daily.exists():
        for line in daily.read_text().splitlines():
            m = re.search(r'main\.py\s+([\w\-\s]+?)(?:\s*\\\s*$|\s*$|\s+--)', line)
            if m:
                cmds.append(m.group(1).strip())
    return cmds


def test_systemd_main_commands_invokable():
    """For each `main.py X Y` in any systemd ExecStart, `main.py X Y --help`
    must exit 0. Catches CLI removals / renames that break production
    timers."""
    failures: list[tuple[str, str]] = []
    for cmd in set(_exec_starts_for_main_py()):
        full = ["venv/bin/python", "main.py"] + cmd.split() + ["--help"]
        result = subprocess.run(full, cwd=REPO, capture_output=True, text=True,
                                timeout=20)
        if result.returncode != 0:
            failures.append((cmd, result.stderr.strip()[:200]))
    assert not failures, (
        f"main.py commands wired to systemd ExecStart fail --help: {failures}"
    )


def test_main_py_help_works():
    """The base `main.py --help` must work — sanity that click is wired."""
    result = subprocess.run(
        ["venv/bin/python", "main.py", "--help"],
        cwd=REPO, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert "MHDE" in result.stdout or "Usage" in result.stdout


# KI-004: trained-model artifacts gitignored ------------------------------------


def test_models_saved_gitignored():
    """KI-004: any *.joblib under models/saved/ must be ignored by git.
    On-disk binaries should not be tracked."""
    # On-disk joblibs:
    on_disk = list((REPO / "models" / "saved").rglob("*.joblib"))
    assert on_disk, "expected at least one trained joblib on disk"

    # None of them should be tracked:
    result = subprocess.run(
        ["git", "ls-files", "models/saved/"],
        cwd=REPO, capture_output=True, text=True,
    )
    tracked_joblibs = [line for line in result.stdout.splitlines()
                        if line.endswith(".joblib")]
    assert not tracked_joblibs, (
        f"KI-004: joblib files should be gitignored. "
        f"Tracked: {tracked_joblibs}"
    )

    # And every on-disk joblib should be ignored by check-ignore:
    not_ignored: list[str] = []
    for p in on_disk:
        rel = p.relative_to(REPO)
        check = subprocess.run(
            ["git", "check-ignore", "-q", str(rel)],
            cwd=REPO,
        )
        if check.returncode != 0:
            not_ignored.append(str(rel))
    assert not not_ignored, (
        f"KI-004: these joblib files are not gitignored: {not_ignored}"
    )
