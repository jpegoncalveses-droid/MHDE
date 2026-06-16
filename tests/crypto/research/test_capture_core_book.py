"""Tests for capture-core depth sequence maintenance (cursor-only, not a book).

Validates the Binance USDT-M local-book *maintenance* procedure: drop-stale,
first-applicable-event selection, pu/u continuity, and gap->resync. The
maintainer never stores or filters raw diffs (the service stores all raw
unconditionally); it only tracks the update-id cursor, detects sequence gaps,
and signals when a fresh REST snapshot is needed.
"""
from __future__ import annotations

from crypto.research.capture_core.book import DepthMaintainer


def _await_diffs(m, diffs):
    for U, u, pu, ts in diffs:
        m.on_diff(U, u, pu, ts)


def test_snapshot_drops_stale_and_syncs_on_first_applicable_event():
    m = DepthMaintainer("BTCUSDT")
    # buffered before snapshot
    m.on_diff(5, 10, 4, ts=1)      # stale: u=10 < lastUpdateId
    m.on_diff(11, 20, 10, ts=2)    # bridges lastUpdateId 15 (11 <= 16 <= 20)
    m.on_diff(21, 30, 20, ts=3)
    res = m.on_snapshot(last_update_id=15, ts=4)
    assert res.synced_now is True
    assert res.gap is None
    assert m.synced is True
    assert m.last_u == 30          # rest applied with pu-continuity


def test_continuity_pass_when_synced_advances_cursor():
    m = DepthMaintainer("BTCUSDT")
    m.on_diff(11, 20, 10, ts=2)
    m.on_snapshot(last_update_id=15, ts=3)   # synced at u=20
    res = m.on_diff(21, 30, 20, ts=4)        # pu=20 == last_u
    assert res.gap is None
    assert res.needs_snapshot is False
    assert m.last_u == 30


def test_continuity_break_requests_resync_then_emits_gap_on_resume():
    m = DepthMaintainer("BTCUSDT")
    m.on_diff(11, 20, 10, ts=2)
    m.on_diff(21, 30, 20, ts=3)
    m.on_snapshot(last_update_id=15, ts=4)   # synced; last_u=30, last-good ts=3
    assert m.synced and m.last_u == 30

    # a discontinuous event (pu=50 != last_u=30): lost updates -> resync
    res = m.on_diff(51, 60, 50, ts=10)
    assert res.needs_snapshot is True
    assert res.gap is None                   # gap not emitted until capture resumes
    assert m.synced is False

    # fresh snapshot + bridging diff re-syncs; gap spans last-good -> resume
    m.on_snapshot(last_update_id=60, ts=12)  # not yet bridged (need U<=61<=u)
    res2 = m.on_diff(61, 70, 60, ts=13)
    assert res2.synced_now is True
    assert res2.gap == (3, 13, "sequence_gap")   # start=last-good ts, end=resume ts
    assert m.synced is True and m.last_u == 70


def test_first_event_boundary_hole_requests_new_snapshot():
    m = DepthMaintainer("BTCUSDT")
    m.on_snapshot(last_update_id=100, ts=1)  # no diffs buffered yet -> wait
    # first diff starts past lastUpdateId+1 -> missed events between snap and diff
    res = m.on_diff(150, 160, 149, ts=2)
    assert res.needs_snapshot is True
    assert m.synced is False


def test_rest_application_break_reenters_resync_and_reports_not_synced():
    # The snapshot syncs at the first applicable event, but a later buffered
    # event breaks continuity -> we re-enter resync. synced_now must reflect the
    # FINAL state (not synced), and a fresh snapshot is needed.
    m = DepthMaintainer("BTCUSDT")
    m.on_diff(11, 20, 10, ts=2)
    m.on_diff(21, 30, 20, ts=3)
    m.on_diff(41, 50, 40, ts=4)              # pu=40 breaks after sync (last_u=30)
    res = m.on_snapshot(last_update_id=15, ts=5)
    assert res.needs_snapshot is True
    assert res.synced_now is False           # ended awaiting resync, not synced
    assert m.synced is False


def test_snapshot_then_later_diffs_sync_without_gap():
    # Snapshot arrives before any diff (normal seed order): wait, then sync.
    m = DepthMaintainer("ETHUSDT")
    res0 = m.on_snapshot(last_update_id=200, ts=1)
    assert res0.synced_now is False           # nothing to bridge yet
    res1 = m.on_diff(199, 205, 198, ts=2)     # 199 <= 201 <= 205 bridges it
    assert res1.synced_now is True
    assert res1.gap is None
    assert m.last_u == 205


# -- leak fix: the awaiting-snapshot buffer must be BOUNDED ---------------------

def test_unsynced_buffer_is_bounded_by_maxlen():
    # ROOT-CAUSE GUARD: a symbol whose seeding snapshot never lands keeps appending
    # one diff per message forever (the firehose memory leak). The buffer MUST cap.
    m = DepthMaintainer("BTCUSDT", buffer_maxlen=5)
    for i in range(50):                        # 50 diffs, never a snapshot -> unsynced
        m.on_diff(i * 10 + 1, i * 10 + 10, i * 10, ts=i)
    assert len(m._buffer) <= 5                  # today: 50 (unbounded) -> RED


def test_bounded_buffer_still_syncs_within_window():
    # The cap must NOT break the normal seed path: a snapshot arriving while the
    # buffered diffs still fit the window syncs exactly as before.
    m = DepthMaintainer("BTCUSDT", buffer_maxlen=5)
    m.on_diff(11, 20, 10, ts=2)                # bridges lastUpdateId 15
    m.on_diff(21, 30, 20, ts=3)
    res = m.on_snapshot(last_update_id=15, ts=4)
    assert res.synced_now is True
    assert m.synced is True and m.last_u == 30


def test_never_synced_book_requests_reseed_past_threshold():
    # Closes the re-request hole: a book stuck unsynced (snapshot never lands) must
    # itself raise needs_snapshot once it has buffered too long, so the service
    # re-queues a seed instead of silently buffering forever.
    m = DepthMaintainer("BTCUSDT", buffer_maxlen=100, reseed_threshold=3)
    results = [m.on_diff(i * 10 + 1, i * 10 + 10, i * 10, ts=i) for i in range(5)]
    assert results[0].needs_snapshot is False   # below threshold
    assert results[2].needs_snapshot is True    # at/over threshold -> re-request (today: never)
