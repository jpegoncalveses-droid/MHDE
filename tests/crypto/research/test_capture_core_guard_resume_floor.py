"""Disk/inode guard RESUME-FLOOR latch fix.

Regression (2026-08-08, ~14h silent WS outage): a firehose write-halt resumed only
at the SOFT floor (50 GiB) / WARN fraction (0.80). After an ENOSPC recovery that
retention could lift only INTO the [critical, soft) band, the guard held ``halted``
forever — writes stayed dropped silently until an operator restart. The resume
decision is now decoupled from the prune/warn target: writes resume at a RESUME
floor just above CRITICAL, so retention/compaction self-recover writes with no
operator action. Prune-toward-soft and the warn-alert edge are UNCHANGED.
"""
from __future__ import annotations

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import disk_guard as dg

GIB = 1024 ** 3


# -- resume thresholds sit strictly inside the old dead bands ------------------

def test_disk_resume_floor_between_critical_and_soft():
    assert cfg.CAPTURE_DISK_RESUME_FLOOR_BYTES == 15 * GIB
    assert (cfg.CAPTURE_DISK_CRITICAL_FLOOR_BYTES
            < cfg.CAPTURE_DISK_RESUME_FLOOR_BYTES
            < cfg.CAPTURE_DISK_SOFT_FLOOR_BYTES)


def test_inode_resume_fraction_between_warn_and_critical():
    assert cfg.CAPTURE_INODE_RESUME_FRACTION == 0.88
    assert (cfg.CAPTURE_INODE_WARN_FRACTION
            < cfg.CAPTURE_INODE_RESUME_FRACTION
            < cfg.CAPTURE_INODE_CRITICAL_FRACTION)


# -- pure halt-state resumes at the RESUME floor, not at soft/warn -------------

def test_next_halt_state_resumes_at_resume_floor_inside_old_deadband():
    # free recovered to 29 (old [10,50) dead band) but >= resume 15 -> RESUME.
    assert dg.next_halt_state(29, resume=15, critical=10, halted=True) is False


def test_next_halt_state_holds_only_in_small_resume_band():
    # [critical, resume) = [10,15): hold the prior state (no flap).
    assert dg.next_halt_state(12, resume=15, critical=10, halted=True) is True
    assert dg.next_halt_state(12, resume=15, critical=10, halted=False) is False
    assert dg.next_halt_state(9, resume=15, critical=10, halted=False) is True   # < critical


def test_next_inode_halt_state_resumes_at_resume_fraction():
    # used fell to 0.85 (old [0.80,0.90) dead band) but < resume 0.88 -> RESUME.
    assert dg.next_inode_halt_state(0.85, resume=0.88, critical=0.90, halted=True) is False
    # in [0.88,0.90): hold prior state.
    assert dg.next_inode_halt_state(0.89, resume=0.88, critical=0.90, halted=True) is True


# -- DiskGuard.enforce self-recovers with NOTHING left to prune ----------------

def test_enforce_self_recovers_in_deadband_when_nothing_to_prune():
    # THE 2026-08-08 scenario: free 29 (GiB, symbolic), halted, retention already
    # took the old partitions (list_fn -> []). Old code stayed halted (needed 50).
    g = dg.DiskGuard(
        "/d", soft_floor=50, critical_floor=10, resume_floor=15,
        free_fn=lambda _r: 29, list_fn=lambda _r, _ds: [], prune_fn=lambda _p: 0,
    )
    g.halted = True
    assert g.enforce().halted is False


def test_enforce_still_prunes_toward_soft_after_writes_resume():
    # free 40 (< soft 50, >= resume 15): writes resume AND the guard still prunes
    # the oldest toward the soft headroom target (unchanged behaviour).
    parts = [dg.Partition(path="/d/date=2026-01-01", date="2026-01-01", size=4)]
    pruned: list[str] = []

    def fake_prune(paths):
        pruned.extend(paths)
        return 4

    g = dg.DiskGuard(
        "/d", soft_floor=50, critical_floor=10, resume_floor=15,
        free_fn=lambda _r: 40, list_fn=lambda _r, _ds: list(parts), prune_fn=fake_prune,
    )
    g.halted = True
    res = g.enforce()
    assert res.halted is False                       # 40 >= resume 15 -> resumed
    assert pruned == ["/d/date=2026-01-01"]          # still pruned toward soft


def test_inode_guard_resumes_in_old_deadband_but_still_warns():
    # used 0.85: below resume 0.88 -> writes resume; still >= warn 0.80 -> "warn".
    used = [0.95]
    sent: list[str] = []
    g = dg.InodeGuard(
        "/x", warn_fraction=0.80, resume_fraction=0.88, critical_fraction=0.90,
        used_fn=lambda _r: used[0], notify_fn=sent.append,
    )
    g.enforce()                                      # 0.95 -> halt
    assert g.halted is True
    used[0] = 0.85
    res = g.enforce()                                # 0.85 -> resume (< 0.88), warn tier
    assert res.halted is False
    assert res.state == "warn"
