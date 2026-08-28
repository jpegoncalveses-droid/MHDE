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
#
# REVIEW CORRECTION. The first cut hooked a 300s wall-clock detector into `_on_message`.
# It could never fire: `conn_manager` abandons a silent socket at SOCKET_SILENCE_TIMEOUT_S
# = 60s and the unit's WatchdogSec=30 SIGABRTs the process at ~90s of silence, so 300s of
# in-process silence is unreachable — and shard-wide silence >60s is ALREADY recorded by
# conn_manager as `socket_silence`. Worse, it missed its own motivating case: the detector
# is per-CaptureService, so a restart resets it and the restart boundary stayed unflagged.
#
# The genuinely uncovered hole is the RESTART/DOWNTIME boundary, which survives only in
# the persisted heartbeat. That is what is detected now, and it is emitted on the stream
# name the label validity gate actually consumes.


def test_downtime_gap_detected_from_the_persisted_heartbeat(tmp_path):
    """The uncovered case: the process was DOWN. Nothing in-process can see that; only the
    heartbeat written before the stop survives it."""
    hb = tmp_path / "heartbeats"
    hb.mkdir()
    (hb / "shard-0.json").write_text('{"ts_ns": 1787000000000000000, "rows": 5}')
    gap = dg.detect_downtime_gap(str(hb), "shard-0",
                                 now_ns=1787000000000000000 + 900 * 10**9,
                                 threshold_s=cfg.CAPTURE_GAP_ALERT_THRESHOLD_S)
    assert gap is not None
    start_ns, end_ns, secs = gap
    assert start_ns == 1787000000000000000
    assert round(secs) == 900


def test_short_restart_is_not_flagged(tmp_path):
    """A clean handover is not a hole worth flagging (and the socket side is covered by
    conn_manager's reconnect gap)."""
    hb = tmp_path / "heartbeats"; hb.mkdir()
    (hb / "shard-0.json").write_text('{"ts_ns": 1787000000000000000}')
    assert dg.detect_downtime_gap(str(hb), "shard-0",
                                  now_ns=1787000000000000000 + 5 * 10**9,
                                  threshold_s=30.0) is None


def test_first_ever_start_has_no_heartbeat_and_no_gap(tmp_path):
    hb = tmp_path / "heartbeats"; hb.mkdir()
    assert dg.detect_downtime_gap(str(hb), "shard-0", now_ns=1787000000000000000,
                                  threshold_s=30.0) is None


def test_corrupt_heartbeat_is_not_fatal(tmp_path):
    hb = tmp_path / "heartbeats"; hb.mkdir()
    (hb / "shard-0.json").write_text("{not json")
    assert dg.detect_downtime_gap(str(hb), "shard-0", now_ns=1787000000000000000,
                                  threshold_s=30.0) is None


def test_threshold_brackets_a_clean_handover_and_a_real_outage():
    """The watchdog constraint that sank the first design does NOT apply here: a downtime
    gap spans a RESTART, so the process is not alive to be aborted. What matters is that
    the threshold sits above a clean handover (~13s measured 2026-08-27 22:10) and low
    enough to catch a genuine outage."""
    assert 20.0 <= cfg.CAPTURE_GAP_ALERT_THRESHOLD_S <= 120.0


def test_downtime_gap_uses_the_stream_the_label_gate_consumes():
    """A gap row the label builder ignores restores nothing. `labels.py` filters on
    MARK_GAP_STREAM_PREFIX; `pipeline.py` maps maroon gaps onto it for exactly this
    reason, and this must follow that precedent."""
    from crypto.research.brain import labels as brain_labels
    assert dg.DOWNTIME_GAP_STREAM.startswith(brain_labels.MARK_GAP_STREAM_PREFIX)
