"""Drift Fix 2a: WIRE the (already-built, dormant) brain-store compactor — the coverage
guard, the CLI verb, and the systemd unit/timer.

The compactor (compaction.py) + its registry parity oracle existed but nothing invoked it:
no CLI hook, no unit, never run — so the continuous runner's 1-part-per-(symbol,date)-per-
pass fan-out grew unbounded (227,880 markprice files in one 10.5h run; the label pass's
229k-file read). Wiring constraint (operator): NO partition is compacted without a VALID
parity check — a partition holding rows the registry has never heard of is UNVERIFIABLE
(the completeness oracle would vacuously pass on an empty roster) and must be SKIPPED and
surfaced, not silently merged. This guard automatically excludes the three bookkeeping-dead
as-of datasets (open_interest / premium_index / global_ls_account — see the flush-lag KI)
and any future bookkeeping regression.

CLI: ``main.py crypto brain-compact`` (family pattern: capture-asof-compact) — sealed
(date < today) whole-partition compaction THEN today's closed-hour compaction, both
subprocess-chunked, registry-parity-checked, coverage-guarded. Unit/timer: hourly at :36
(offset from capture's :06), BUILT-NOT-DEPLOYED, capture-family shared-host caps.
"""
from __future__ import annotations

import configparser
import pathlib
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from crypto.research.brain import compaction
from crypto.research.brain import config as cfg
from crypto.research.brain import registry
from crypto.research.brain import store

_DATASET = cfg.MARKPRICE_DATASET
_SCHEMA = store.MARKPRICE_SNAPSHOT_SCHEMA
_SEALED = "2026-06-17"
_NOW_MS = int(datetime(2026, 6, 19, 12, tzinfo=timezone.utc).timestamp() * 1000)
_NOW_NS = _NOW_MS * 1_000_000

REPO = pathlib.Path("/home/jpcg/MHDE")
SVC = REPO / "systemd" / "mhde-brain-compact.service"
TIMER = REPO / "systemd" / "mhde-brain-compact.timer"


def _ns(date_str, minute=0):
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1_000_000_000) + minute * 60_000_000_000


def _snap(symbol, window_start_ns, *, update_count=5):
    row = {name: 0 for name in _SCHEMA.names}
    row.update(symbol=symbol, window_start_ns=window_start_ns,
               window_end_ns=window_start_ns + 60_000_000_000,
               recv_ts_ns=window_start_ns, mark_close=100.0, update_count=update_count)
    return row


def _write_pass(root, symbol, window_start_ns):
    store.write_snapshots(str(root), _DATASET, _SCHEMA, [_snap(symbol, window_start_ns)])


def _part_dir(root, symbol, date=_SEALED):
    return pathlib.Path(root, _DATASET, f"symbol={symbol}", f"date={date}")


def _reg_path(root) -> str:
    p = str(pathlib.Path(root, "registry.sqlite"))
    conn = registry.connect(p)                       # ensure schema exists even when empty
    conn.close()
    return p


def _record(reg_path, symbol, minutes, *, date=_SEALED):
    conn = registry.connect(reg_path)
    registry.record_windows(conn, [
        {"dataset": _DATASET, "symbol": symbol, "window_start_ns": _ns(date, m),
         "window_end_ns": _ns(date, m) + 60_000_000_000, "recv_ts_ns": _ns(date, m),
         "n_events": 5}
        for m in minutes
    ], now_ns=_NOW_NS)
    conn.close()


# --- the coverage guard: no partition compacts without a verifiable roster --------------

def test_sealed_guard_skips_partition_with_empty_roster(tmp_path):
    _write_pass(tmp_path, "AAAUSDT", _ns(_SEALED, 1))
    _write_pass(tmp_path, "AAAUSDT", _ns(_SEALED, 2))
    reg = _reg_path(tmp_path)                        # empty roster: nothing recorded
    res = compaction.compact_partition(str(_part_dir(tmp_path, "AAAUSDT")),
                                       registry_path=reg, require_registry_coverage=True)
    assert res.out_path is None, "an unverifiable partition must not be merged"
    assert len(list(_part_dir(tmp_path, "AAAUSDT").glob("part-*.parquet"))) == 2, \
        "originals stay untouched"
    assert res.unverifiable_skipped, "the skip must be surfaced, never silent"


def test_sealed_guard_compacts_partition_with_roster(tmp_path):
    _write_pass(tmp_path, "AAAUSDT", _ns(_SEALED, 1))
    _write_pass(tmp_path, "AAAUSDT", _ns(_SEALED, 2))
    reg = _reg_path(tmp_path)
    _record(reg, "AAAUSDT", [1, 2])
    res = compaction.compact_partition(str(_part_dir(tmp_path, "AAAUSDT")),
                                       registry_path=reg, require_registry_coverage=True)
    assert res.out_path is not None
    assert res.registry_mismatches == [] and not res.unverifiable_skipped
    assert len(list(_part_dir(tmp_path, "AAAUSDT").glob("part-*.parquet"))) == 0


def test_sealed_guard_off_by_default_keeps_existing_behavior(tmp_path):
    _write_pass(tmp_path, "AAAUSDT", _ns(_SEALED, 1))
    _write_pass(tmp_path, "AAAUSDT", _ns(_SEALED, 2))
    reg = _reg_path(tmp_path)                        # empty roster, no guard -> old behavior
    res = compaction.compact_partition(str(_part_dir(tmp_path, "AAAUSDT")), registry_path=reg)
    assert res.out_path is not None and not res.unverifiable_skipped


def test_sealed_guard_requires_registry_path(tmp_path):
    with pytest.raises(ValueError):
        compaction.compact_partition(str(_part_dir(tmp_path, "AAAUSDT")),
                                     require_registry_coverage=True)


def test_closed_hours_guard_skips_uncovered_hour_and_keeps_parts(tmp_path):
    today = datetime.fromtimestamp(_NOW_MS / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    _write_pass(tmp_path, "AAAUSDT", _ns(today, 1))          # hour 0, closed by _NOW
    _write_pass(tmp_path, "AAAUSDT", _ns(today, 2))
    reg = _reg_path(tmp_path)                                # empty roster
    results = compaction.compact_partition_closed_hours(
        str(_part_dir(tmp_path, "AAAUSDT", today)), now_ns=_NOW_NS, registry_path=reg,
        require_registry_coverage=True)
    assert all(r.out_path is None for r in results), "uncovered hour must not merge"
    assert any(r.unverifiable_skipped for r in results)
    assert len(list(_part_dir(tmp_path, "AAAUSDT", today).glob("part-*.parquet"))) == 2, \
        "the uncovered hour's parts must NOT be deleted"


def test_closed_hours_guard_compacts_covered_hour(tmp_path):
    today = datetime.fromtimestamp(_NOW_MS / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    _write_pass(tmp_path, "AAAUSDT", _ns(today, 1))
    _write_pass(tmp_path, "AAAUSDT", _ns(today, 2))
    reg = _reg_path(tmp_path)
    _record(reg, "AAAUSDT", [1, 2], date=today)
    results = compaction.compact_partition_closed_hours(
        str(_part_dir(tmp_path, "AAAUSDT", today)), now_ns=_NOW_NS, registry_path=reg,
        require_registry_coverage=True)
    merged = [r for r in results if r.out_path is not None]
    assert len(merged) == 1 and not any(r.unverifiable_skipped for r in results)
    assert len(list(_part_dir(tmp_path, "AAAUSDT", today).glob("part-*.parquet"))) == 0


def test_spanning_part_mixed_coverage_duplicates_benignly_then_self_heals(tmp_path):
    # A single writer part SPANNING a covered hour h0 and an uncovered hour h1 (a catch-up
    # pass writes multi-hour parts; a bookkeeping regression covers only h0). h0 merges,
    # the part is kept whole for h1 -> h0's windows transiently live in BOTH files. Pin:
    # (a) no window-keyed reader double-counts (dedup by window_start_ns), and (b) once h1
    # gains coverage, a re-run consumes the part WITHOUT duplicating h0 in a new compact.
    today = datetime.fromtimestamp(_NOW_MS / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    part_dir = _part_dir(tmp_path, "AAAUSDT", today)
    store.write_snapshots(str(tmp_path), _DATASET, _SCHEMA, [
        _snap("AAAUSDT", _ns(today, 30)),        # hour 0 (covered)
        _snap("AAAUSDT", _ns(today, 90)),        # hour 1 (uncovered) -> SAME part file
    ])
    assert len(list(part_dir.glob("part-*.parquet"))) == 1
    reg = _reg_path(tmp_path)
    _record(reg, "AAAUSDT", [30], date=today)    # cover ONLY hour 0

    results = compaction.compact_partition_closed_hours(
        str(part_dir), now_ns=_NOW_NS, registry_path=reg, require_registry_coverage=True)
    merged = [r for r in results if r.out_path is not None]
    skipped = [r for r in results if r.unverifiable_skipped]
    assert len(merged) == 1 and len(skipped) == 1
    assert len(list(part_dir.glob("part-*.parquet"))) == 1, \
        "the spanning part must be KEPT whole for the uncovered hour"

    # (a) transient duplication is dedup-safe for every window-keyed reader
    rows = store.read_snapshots(str(tmp_path), _DATASET, "AAAUSDT")
    by_window = {int(r["window_start_ns"]): r for r in rows}   # the labels/engineered pattern
    assert len(by_window) == 2, "window-keyed consumers see each window exactly once"
    assert sum(1 for r in rows if int(r["window_start_ns"]) == _ns(today, 30)) == 2, \
        "the raw duplication exists (compact-h0 + kept part) — the dedup is what protects"

    # (b) self-heal: cover h1, re-run -> part consumed, h0 NOT re-merged into a second file
    _record(reg, "AAAUSDT", [90], date=today)
    results2 = compaction.compact_partition_closed_hours(
        str(part_dir), now_ns=_NOW_NS, registry_path=reg, require_registry_coverage=True)
    assert not any(r.unverifiable_skipped for r in results2)
    assert len(list(part_dir.glob("part-*.parquet"))) == 0, "the spanning part is consumed"
    rows = store.read_snapshots(str(tmp_path), _DATASET, "AAAUSDT")
    assert sorted(int(r["window_start_ns"]) for r in rows) == [_ns(today, 30), _ns(today, 90)], \
        "post-heal the store holds each window exactly once"


def test_unverifiable_marshalled_through_chunked_driver(tmp_path):
    _write_pass(tmp_path, "AAAUSDT", _ns(_SEALED, 1))
    _write_pass(tmp_path, "AAAUSDT", _ns(_SEALED, 2))
    _write_pass(tmp_path, "BBBUSDT", _ns(_SEALED, 1))
    _write_pass(tmp_path, "BBBUSDT", _ns(_SEALED, 2))
    reg = _reg_path(tmp_path)
    _record(reg, "BBBUSDT", [1, 2])                          # only BBB is verifiable
    report = compaction.compact_brain_chunked(
        str(tmp_path), datasets=[_DATASET], registry_path=reg, now_ms=_NOW_MS,
        require_registry_coverage=True,
        chunk_runner=compaction._inprocess_chunk_runner())
    assert report.partitions_compacted == 1
    assert len(report.unverifiable_skipped) == 1 and "AAAUSDT" in report.unverifiable_skipped[0], \
        "the PR #60 lesson: an unverifiable partition must be marshalled, never a silent 0"


# --- the CLI verb ------------------------------------------------------------------------

def test_cli_brain_compact_runs_both_modes_with_guard(monkeypatch):
    from click.testing import CliRunner
    import main as main_mod

    calls = {}

    def fake_sealed(root, **kw):
        calls["sealed"] = {"root": root, **kw}
        return compaction.BrainCompactionReport(partitions_scanned=3, partitions_compacted=2,
                                                files_before=10, files_after=2)

    def fake_hours(root, **kw):
        calls["hours"] = {"root": root, **kw}
        return compaction.BrainCompactionReport(partitions_scanned=5, partitions_compacted=4,
                                                files_before=50, files_after=8)

    monkeypatch.setattr(compaction, "compact_brain_chunked", fake_sealed)
    monkeypatch.setattr(compaction, "compact_brain_closed_hours_chunked", fake_hours)
    result = CliRunner().invoke(main_mod.cli, ["crypto", "brain-compact"])
    assert result.exit_code == 0, result.output

    from crypto.research.brain import sources
    for mode in ("sealed", "hours"):
        assert calls[mode]["root"] == cfg.BRAIN_STORE_ROOT
        assert calls[mode]["registry_path"] == cfg.BRAIN_REGISTRY_PATH
        assert calls[mode]["require_registry_coverage"] is True, \
            "the CLI must never compact without the parity-coverage guard"
        assert sorted(calls[mode]["datasets"]) == sorted(sources.SOURCES.keys()), \
            "scope = the 12 primitive datasets (labels excluded until it has an oracle)"
    assert "sealed" in result.output and "closed-hour" in result.output


def test_cli_brain_compact_surfaces_mismatches_and_unverifiable(monkeypatch):
    from click.testing import CliRunner
    import main as main_mod

    bad = compaction.BrainCompactionReport(partitions_scanned=1)
    bad.registry_mismatches.append("markprice/AAAUSDT/2026-06-17: window 42 MISSING")
    bad.unverifiable_skipped.append("markprice/BBBUSDT/2026-06-17: no registry coverage")
    monkeypatch.setattr(compaction, "compact_brain_chunked", lambda root, **kw: bad)
    monkeypatch.setattr(compaction, "compact_brain_closed_hours_chunked",
                        lambda root, **kw: compaction.BrainCompactionReport())
    result = CliRunner().invoke(main_mod.cli, ["crypto", "brain-compact"])
    assert result.exit_code == 0
    assert "MISSING" in result.output and "UNVERIFIABLE" in result.output.upper()


# --- systemd unit + timer (BUILT-NOT-DEPLOYED, capture-family caps) ----------------------

def _parse(unit_path: pathlib.Path) -> configparser.ConfigParser:
    p = configparser.ConfigParser(strict=False, interpolation=None)
    p.optionxform = str
    p.read(unit_path)
    return p


def test_units_exist():
    assert SVC.exists() and TIMER.exists()


def test_units_valid_syntax():
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze not available")
    for unit in (SVC, TIMER):
        proc = subprocess.run(["systemd-analyze", "verify", "--user", str(unit)],
                              capture_output=True, text=True, timeout=20)
        assert proc.returncode == 0, f"{unit}:\n{proc.stdout}{proc.stderr}"


def test_service_is_oneshot_invoking_brain_compact_with_family_caps():
    svc = _parse(SVC)
    assert svc.get("Service", "Type") == "oneshot"
    exec_start = svc.get("Service", "ExecStart")
    assert "brain-compact" in exec_start and "/home/jpcg/MHDE/venv/bin/python" in exec_start
    assert svc.get("Service", "WorkingDirectory") == "/home/jpcg/MHDE"
    assert svc.get("Service", "Nice") == "19"
    assert svc.get("Service", "IOSchedulingClass") == "idle"
    assert svc.get("Service", "MemoryMax") == "2G"        # KI-165: raised 1G->2G (backstop)
    assert svc.get("Service", "CPUWeight") == "20"
    assert svc.get("Service", "IOWeight") == "20"
    assert svc.get("Service", "OOMScoreAdjust") == "800"
    assert "BUILT-NOT-DEPLOYED" in SVC.read_text()


def test_timer_is_hourly_offset_from_capture_compactors():
    timer = _parse(TIMER)
    oncal = timer.get("Timer", "OnCalendar")
    assert ":36" in oncal, "hourly at :36 — offset from capture's :06 compactor window"
    assert timer.get("Timer", "Persistent") == "true"
