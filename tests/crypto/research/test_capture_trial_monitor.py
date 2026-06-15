"""ADR-039 §G-trial gap 2 — trial supervisor (monitor + anon watchdog).

Unit-tests the supervisor logic with mocked I/O: anon is read from the cgroup
``memory.stat`` ``anon`` field (NOT ``memory.current`` — the ADR-038 false-alarm lesson);
the watchdog aborts iff anon crosses the threshold and targets the configured scope; the
log parser counts gap + handshake-timeout lines; and a sample row carries all four
pass/fail signals with a timestamp. Launches nothing; no systemd/cpuset.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_MOD_PATH = (pathlib.Path(__file__).resolve().parents[3]
             / "scripts" / "capture_trial_monitor.py")
_spec = importlib.util.spec_from_file_location("capture_trial_monitor", _MOD_PATH)
ctm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ctm)


# -- (a) anon comes from the 'anon' field, NOT memory.current / page cache -----

def test_anon_read_from_anon_field_not_page_cache():
    # 'file' (page cache) is huge; 'anon' (real heap) is small. We must return anon.
    stat = "anon 1048576\nfile 999999999\ninactive_file 500000000\nslab 4096\n"
    assert ctm.parse_anon_bytes(stat) == 1048576

    # No 'anon' field -> raise, so a bad read can never silently fall back to a
    # page-cache / total value (which is exactly the memory.current trap).
    with pytest.raises(ValueError):
        ctm.parse_anon_bytes("file 123\nslab 4\n")


# -- (b) watchdog aborts iff anon > threshold, targeting the configured scope --

def test_watchdog_aborts_only_above_threshold_and_targets_scope():
    aborted = []
    mon = ctm.TrialMonitor(
        scope="mhde-trial.scope", log_path="L", out_path="O",
        anon_threshold_bytes=2 * 1024 ** 3,
        abort_fn=lambda s: aborted.append(s),
        read_memory_stat=lambda: "", read_proc_stat=lambda: "",
        read_log=lambda: "", write_row=lambda r: None)

    assert mon.check_watchdog(1 * 1024 ** 3) is False        # below -> no abort
    assert aborted == []
    assert mon.aborted is False

    assert mon.check_watchdog(3 * 1024 ** 3) is True          # above -> abort
    assert aborted == ["mhde-trial.scope"]                    # the configured scope
    assert mon.aborted is True

    # exactly at threshold is NOT over -> no abort
    assert ctm.TrialMonitor(
        scope="s", log_path="L", out_path="O", anon_threshold_bytes=2 * 1024 ** 3,
        abort_fn=lambda s: aborted.append(s)).check_watchdog(2 * 1024 ** 3) is False


# -- (c) log parsing extracts gap count and handshake-timeout count ------------

def test_log_parsing_counts_gaps_and_handshake_timeouts():
    log = "\n".join([
        "INFO start",
        "WARNING capture-core shard disconnected (TimeoutError: timed out during "
        "opening handshake); reconnecting",
        "WARNING capture-core shard disconnected (ConnectionResetError: x); reconnecting",
        "INFO heartbeat",
        "WARNING capture-core shard disconnected (TimeoutError: timed out during "
        "opening handshake); reconnecting",
    ])
    assert ctm.count_marker(log, ctm.DEFAULT_GAP_MARKER) == 3           # all reconnects
    assert ctm.count_marker(log, ctm.DEFAULT_HANDSHAKE_MARKER) == 2     # storm subset


# -- (d) a sample appends a timestamped row carrying all four signals ----------

def test_sample_appends_row_with_all_four_signals(tmp_path):
    out = tmp_path / "samples.jsonl"
    mem = "anon 1073741824\nfile 9999999999\n"               # 1.0 GiB anon (file ignored)
    # cpu0 chosen so util computes to a clean 75.0% against prev below.
    proc = "cpu  1 1 1 1 0 0 0 0\ncpu0 100 0 25 75 0 0 0 0\n"
    log = ("WARNING capture-core shard disconnected (TimeoutError: timed out during "
           "opening handshake); reconnecting\n")
    mon = ctm.TrialMonitor(
        scope="s.scope", log_path="L", out_path=str(out),
        read_memory_stat=lambda: mem, read_proc_stat=lambda: proc,
        read_log=lambda: log, clock=lambda: 1234.5)

    prev = {"cpu0": (50, 100)}                                # earlier /proc/stat snapshot
    row, cur = mon.sample(prev)

    assert row["ts"] == 1234.5                                # (timestamped)
    assert row["anon_bytes"] == 1073741824 and row["anon_gib"] == 1.0   # signal 1
    assert row["per_core_pct"]["cpu0"] == 75.0               # signal 2 (computed delta)
    assert row["gap_count"] == 1                              # signal 3
    assert row["handshake_timeout_count"] == 1               # signal 4
    assert cur == {"cpu0": (75, 200)}                         # cur snapshot fed forward

    # appended to the sample log as a JSON line carrying the same signals
    written = json.loads(out.read_text().splitlines()[0])
    assert written["anon_bytes"] == 1073741824
    assert written["gap_count"] == 1 and written["handshake_timeout_count"] == 1


# -- per-core util math: no time elapsed -> 0.0 (no div-by-zero) ---------------

def test_percpu_util_zero_when_no_time_elapsed():
    snap = {"cpu0": (100, 200)}
    assert ctm.percpu_util_pct(snap, snap) == {"cpu0": 0.0}
