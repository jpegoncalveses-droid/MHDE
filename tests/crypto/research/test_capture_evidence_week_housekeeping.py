"""Evidence-week housekeeping: protect the tape the analysis depends on.

Motivating incident (2026-08-27/28): a 5h hole in the brain tape (19:24→24:00) wiped ~5h
of the out-of-sample verdict window. Root cause chain, from the logs:

  1. brain-tick lags (KI-160): the dense cursors sat ~4.5h behind at 19:00.
  2. free space fell below the 50 GiB SOFT floor, so the byte guard pruned the OLDEST
     dense date-partitions at 00:00:13 — measured free during that episode: 43.7-48.5 GiB.
  3. only ~1-2 days of dense tape exist, so "oldest" was YESTERDAY — the very partitions
     the lagging cursor still needed.
  4. cursor MAROONED below surviving tape -> forced +5h jump, interval flagged to _gaps.

Two fixes here, plus detection:
  * the soft floor 50 -> 40 GiB. Free never went below 43.7 GiB in that episode, so a
    40 GiB floor would have prevented the prune, and therefore the gap, entirely.
  * the guard's partition scan is made race-tolerant: it walks directories the compactor
    is concurrently replacing-then-deleting, and a vanished entry raised FileNotFoundError
    out of enforce() (9 occurrences overnight), skipping that whole enforcement cycle.
  * a wall-clock discontinuity detector per shard, replacing the depth-derived gap signal
    that KI-164 retired (depth carried the only sequence numbers; restarts and stalls are
    now otherwise unflagged).
"""
from __future__ import annotations

import os
import pathlib

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import disk_guard as dg


def _mkpart(root, ds, sym, date, nbytes=1024):
    d = pathlib.Path(root, ds, f"symbol={sym}", f"date={date}")
    d.mkdir(parents=True, exist_ok=True)
    (d / "part.parquet").write_bytes(b"x" * nbytes)
    return d


# ---------------------------------------------------------------- soft floor

def test_soft_floor_lowered_to_40gib():
    """50 -> 40 GiB. Measured free during the 2026-08-28 00:00 prune that caused the
    5h tape hole was 43.7-48.5 GiB — below 50, above 40, so this prevents that prune."""
    assert cfg.CAPTURE_DISK_SOFT_FLOOR_BYTES == 40 * 1024 ** 3
    # ordering invariants must survive the change
    assert (cfg.CAPTURE_DISK_CRITICAL_FLOOR_BYTES
            < cfg.CAPTURE_DISK_RESUME_FLOOR_BYTES
            < cfg.CAPTURE_DISK_SOFT_FLOOR_BYTES)


def test_the_incident_free_level_no_longer_triggers_a_prune(tmp_path):
    """43.7 GiB free — the lowest reading in the incident — must NOT prune."""
    _mkpart(tmp_path, "aggTrade", "BTCUSDT", "2026-08-27")
    pruned_paths = []
    g = dg.DiskGuard(str(tmp_path),
                     free_fn=lambda _r: int(43.7 * 1024 ** 3),
                     prune_fn=lambda ps: pruned_paths.extend(ps) or 0,
                     active_date_fn=lambda: "2026-08-28")
    res = g.enforce()
    assert res.pruned == [] and pruned_paths == []


# ---------------------------------------------------------------- race tolerance

def test_partition_scan_tolerates_a_vanishing_directory(tmp_path):
    """The compactor replaces-then-deletes under the guard's feet. A vanished entry must
    be SKIPPED, not raise out of enforce() and skip the whole enforcement cycle."""
    keep = _mkpart(tmp_path, "aggTrade", "BTCUSDT", "2026-08-26")
    doomed = _mkpart(tmp_path, "aggTrade", "ETHUSDT", "2026-08-26")

    real_dir_size = dg._dir_size

    def _vanishing(path):
        if str(doomed) in str(path):
            import shutil
            shutil.rmtree(doomed, ignore_errors=True)
            raise FileNotFoundError(str(path))
        return real_dir_size(path)

    dg._dir_size = _vanishing
    try:
        parts = dg.list_firehose_partitions(str(tmp_path), ("aggTrade",), with_size=True)
    finally:
        dg._dir_size = real_dir_size
    paths = {p.path for p in parts}
    assert str(keep) in paths
    assert str(doomed) not in paths          # skipped, not fatal


def test_enforce_survives_a_vanishing_partition(tmp_path):
    """End-to-end: enforce() must complete (and still evaluate the halt state) when a
    partition disappears mid-scan."""
    _mkpart(tmp_path, "aggTrade", "BTCUSDT", "2026-08-26")
    gone = pathlib.Path(tmp_path, "aggTrade", "symbol=GONEUSDT", "date=2026-08-26")
    gone.mkdir(parents=True)
    real_dir_size = dg._dir_size

    def _vanishing(path):
        if "GONEUSDT" in str(path):
            os.rmdir(path)
            raise FileNotFoundError(str(path))
        return real_dir_size(path)

    dg._dir_size = _vanishing
    try:
        g = dg.DiskGuard(str(tmp_path), free_fn=lambda _r: 5 * 1024 ** 3,
                         prune_fn=lambda ps: 0, active_date_fn=lambda: "2026-08-28")
        res = g.enforce()                     # must not raise
    finally:
        dg._dir_size = real_dir_size
    assert res.halted is True                 # 5GiB < critical -> halt still evaluated


# ---------------------------------------------------------------- gap detection

def test_wall_clock_discontinuity_detector_flags_a_hole():
    """Replacement for the retired depth-derived gap signal: a wall-clock jump between
    consecutive observed messages is a capture hole and must be reported."""
    d = dg.WallClockGapDetector(threshold_s=cfg.CAPTURE_GAP_ALERT_THRESHOLD_S)
    base = 1_787_000_000_000_000_000
    assert d.observe(base) is None
    assert d.observe(base + 30 * 10**9) is None                    # 30s: normal
    gap = d.observe(base + 30 * 10**9 + 400 * 10**9)               # 400s: a hole
    assert gap is not None
    start_ns, end_ns, secs = gap
    assert start_ns == base + 30 * 10**9
    assert round(secs) == 400


def test_detector_does_not_flag_normal_cadence():
    d = dg.WallClockGapDetector(threshold_s=120.0)
    t = 1_787_000_000_000_000_000
    for _ in range(50):
        t += 5 * 10**9
        assert d.observe(t) is None


def test_detector_threshold_is_configured_and_sane():
    assert 60.0 <= cfg.CAPTURE_GAP_ALERT_THRESHOLD_S <= 900.0


def test_service_records_a_wall_clock_gap_to_the_manifest(tmp_path):
    """End-to-end: a silent period between messages must produce a manifest row, so the
    hole is visible to every downstream analysis (the property depth used to provide)."""
    import pyarrow.parquet as pq
    from crypto.research.capture_core import service as svc

    s = svc.CaptureService(root=str(tmp_path), client=None)
    base = 1_787_000_000_000_000_000
    agg = {"e": "aggTrade", "E": 1, "a": 1, "s": "BTCUSDT", "p": "1", "q": "1",
           "f": 1, "l": 1, "T": 1, "m": False}
    s._on_message("btcusdt@aggTrade", agg, recv_ns=base)
    s._on_message("btcusdt@aggTrade", agg, recv_ns=base + 30 * 10**9)          # normal
    s._on_message("btcusdt@aggTrade", agg, recv_ns=base + 700 * 10**9)         # hole
    s._gaps.flush_all()

    rows = []
    for fp in sorted(pathlib.Path(tmp_path, "_gaps").rglob("*.parquet")):
        rows.extend(pq.read_table(str(fp)).to_pylist())
    holes = [r for r in rows if r["reason"] == "wall_clock_gap"]
    assert len(holes) == 1
    assert holes[0]["stream"] == "wall_clock"
    assert round((holes[0]["gap_end_ms"] - holes[0]["gap_start_ms"]) / 1000) == 670
