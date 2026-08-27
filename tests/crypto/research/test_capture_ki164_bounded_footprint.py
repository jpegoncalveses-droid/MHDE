"""KI-164 — bounded capture footprint: retire the depth family, enforce honest retention.

Inode history: 91% HALT 2026-08-21, 90% HALT 2026-08-26, both traced to writers with no
enforced ceiling. `depth_state` was emergency-deleted 2026-08-26 21:20 (2.97M files, 30% of
the filesystem's inodes) but its WRITER is live and regenerates at ~870k files/day, so the
deletion refills to the 90% halt in ~3-4 days. This is the durable close:

  A. retire the depth family by CONFIG KILL-SWITCH (readers untouched, Stage C revives by
     flag flip), plus a one-off script to sweep what is already on disk;
  B. nightly-enforced retention per CLASS on BOTH roots (dense 3d / as-of 21d), `_gaps` never;
  C. klines cut to 30d on the evidence that no consumer reads capture klines by deep date.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import maintenance
from crypto.research.capture_core import service as svc


def _touch(root, dataset, symbol, date):
    d = pathlib.Path(root, dataset, f"symbol={symbol}", f"date={date}")
    d.mkdir(parents=True, exist_ok=True)
    (d / "part.parquet").write_bytes(b"x")
    return d


def _gap_rows(root):
    """Gap manifest rows actually ON DISK (rows_written only counts FLUSHED rows, so a
    buffered-only assertion would pass trivially)."""
    import pyarrow.parquet as pq
    rows = []
    for fp in sorted(pathlib.Path(root, "_gaps").rglob("*.parquet")):
        rows.extend(pq.read_table(str(fp)).to_pylist())
    return rows


def _touch_gaps(root, date):
    d = pathlib.Path(root, "_gaps", f"date={date}")
    d.mkdir(parents=True, exist_ok=True)
    (d / "part.parquet").write_bytes(b"x")
    return d


def _day(offset):
    return (datetime.now(timezone.utc) + timedelta(days=offset)).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ A. kill-switches

def test_depth_family_writers_are_retired_by_default():
    assert cfg.DEPTH_ENABLED is False
    assert cfg.DEPTH_SNAPSHOT_ENABLED is False
    assert cfg.DEPTH_STATE_ENABLED is False


def test_depth_stream_is_not_subscribed_when_retired():
    """The raw diff is the bandwidth+inode source; retiring it must drop the SUBSCRIPTION,
    not merely the writer."""
    streams = svc.per_symbol_streams(["BTCUSDT", "ETHUSDT"])
    assert not [s for s in streams if "@depth@" in s]
    assert [s for s in streams if s.endswith("@aggTrade")]      # others untouched
    assert [s for s in streams if s.endswith("@bookTicker")]


def test_service_creates_no_depth_family_writers(tmp_path):
    s = svc.CaptureService(root=str(tmp_path), client=None)
    assert s._depth is None
    assert s._snapshot is None
    assert s._depth_state is None
    names = {getattr(w, "_dataset", None) for w in s._writers}
    assert "depth" not in names and "depth_snapshot" not in names and "depth_state" not in names
    assert {"aggTrade", "bookTicker", "forceOrder", "markPrice"} <= names


def test_retired_service_records_no_depth_gap_rows(tmp_path):
    """Gap-recorder entries for these streams stop with the writers: with no depth stream
    there is no sequence maintenance, so no `depth` rows reach the manifest."""
    s = svc.CaptureService(root=str(tmp_path), client=None)
    s._on_message("btcusdt@depth@100ms",
                  {"e": "depthUpdate", "E": 1, "T": 1, "s": "BTCUSDT",
                   "U": 1, "u": 2, "pu": 0, "b": [], "a": []}, recv_ns=1)
    s._gaps.flush_all()
    assert s._maintainers == {}
    assert _gap_rows(str(tmp_path)) == []


def test_connection_level_gap_recording_survives_retirement(tmp_path):
    """Only the depth-DERIVED `sequence_gap` goes away. Connection-level gaps (conn_manager
    `proactive_reconnect`) are independent of depth and must still be recorded."""
    s = svc.CaptureService(root=str(tmp_path), client=None)
    s._on_gap(["btcusdt@aggTrade", "!markPrice@arr"], "proactive_reconnect", 1000, 2000)
    s._gaps.flush_all()
    rows = _gap_rows(str(tmp_path))
    assert len(rows) == 2
    assert {r["stream"] for r in rows} == {"btcusdt@aggTrade", "!markPrice@arr"}
    assert {r["reason"] for r in rows} == {"proactive_reconnect"}


def test_depth_readers_are_untouched():
    """A2: the brain-side depth reader/primitive/SourceSpec stay defined (Stage C revives by
    flag flip), and DEPTH stays OUT of the wired runner set."""
    from crypto.research.brain import depth as brain_depth
    from crypto.research.brain import reader as brain_reader
    from crypto.research.brain.discovery import config  # noqa: F401
    from crypto.research.brain import sources

    assert callable(brain_reader.read_new_depth_state)
    assert callable(brain_depth.bucket_depth)
    assert sources.DEPTH is not None
    assert sources.DEPTH.dataset not in sources.SOURCES     # still deferred (KI-159)


# ------------------------------------------------------------------ B. retention policy

def test_retention_policy_is_per_class():
    p = cfg.CAPTURE_RETENTION_POLICY
    for d in ("aggTrade", "bookTicker", "markPrice", "forceOrder"):
        assert p[d] == 3, f"{d} must be dense/3d"
    for d in cfg.CAPTURE_ASOF_DATASETS:
        assert p[d] == 21, f"{d} must be as-of/21d"
    assert p[cfg.KLINES_DATASET] == 30


def test_gaps_are_never_expired():
    assert "_gaps" not in cfg.CAPTURE_RETENTION_POLICY


def test_retired_datasets_are_not_written_and_not_guard_prunable():
    """They keep a tight nightly ceiling (deploy-order safety, see the test below) but are
    out of the guard's prune set — the guard must never depend on a retired dataset."""
    for d in ("depth", "depth_snapshot", "depth_state"):
        assert d not in cfg.FIREHOSE_PRUNABLE_DATASETS
        assert cfg.CAPTURE_RETENTION_POLICY[d] == cfg.CAPTURE_RETIRED_RETENTION_DAYS == 1


def test_expire_by_policy_enforces_each_class(tmp_path):
    root = str(tmp_path)
    _touch(root, "aggTrade", "BTCUSDT", _day(-2))          # inside 3d
    _touch(root, "aggTrade", "BTCUSDT", _day(-9))          # beyond 3d
    _touch(root, "premium_index", "BTCUSDT", _day(-9))     # inside 21d
    _touch(root, "premium_index", "BTCUSDT", _day(-40))    # beyond 21d
    _touch(root, "klines_1h", "BTCUSDT", _day(-20))        # inside 30d
    _touch(root, "klines_1h", "BTCUSDT", _day(-60))        # beyond 30d
    gaps_old = _touch_gaps(root, _day(-70))                # never expired

    maintenance.expire_by_policy(root)

    assert pathlib.Path(root, "aggTrade", "symbol=BTCUSDT", f"date={_day(-2)}").exists()
    assert not pathlib.Path(root, "aggTrade", "symbol=BTCUSDT", f"date={_day(-9)}").exists()
    assert pathlib.Path(root, "premium_index", "symbol=BTCUSDT", f"date={_day(-9)}").exists()
    assert not pathlib.Path(root, "premium_index", "symbol=BTCUSDT", f"date={_day(-40)}").exists()
    assert pathlib.Path(root, "klines_1h", "symbol=BTCUSDT", f"date={_day(-20)}").exists()
    assert not pathlib.Path(root, "klines_1h", "symbol=BTCUSDT", f"date={_day(-60)}").exists()
    assert gaps_old.exists(), "_gaps must never be expired"


def test_expire_by_policy_never_removes_today(tmp_path):
    root = str(tmp_path)
    today = _touch(root, "aggTrade", "BTCUSDT", _day(0))
    maintenance.expire_by_policy(root)
    assert today.exists()


def test_expire_by_policy_is_root_agnostic_so_okx_gets_the_same_policy(tmp_path):
    """B3: the OKX unit runs the SAME CLI with --root, so one policy governs both roots."""
    okx = str(tmp_path / "capture_core_okx")
    _touch(okx, "aggTrade", "BTC-USDT-SWAP", _day(-9))
    _touch(okx, "premium_index", "BTC-USDT-SWAP", _day(-40))
    _touch(okx, "premium_index", "BTC-USDT-SWAP", _day(-9))
    maintenance.expire_by_policy(okx)
    assert not pathlib.Path(okx, "aggTrade", "symbol=BTC-USDT-SWAP", f"date={_day(-9)}").exists()
    assert not pathlib.Path(okx, "premium_index", "symbol=BTC-USDT-SWAP", f"date={_day(-40)}").exists()
    assert pathlib.Path(okx, "premium_index", "symbol=BTC-USDT-SWAP", f"date={_day(-9)}").exists()


# ------------------------------------------------------------------ C. klines

def test_klines_retention_is_30_days():
    assert cfg.KLINES_RETENTION_DAYS == 30


# ------------------------------------------------------------------ D. guard untouched

def test_disk_and_inode_guard_floors_are_unchanged():
    assert cfg.CAPTURE_DISK_SOFT_FLOOR_BYTES == 50 * 1024 ** 3
    assert cfg.CAPTURE_DISK_CRITICAL_FLOOR_BYTES == 10 * 1024 ** 3
    assert cfg.CAPTURE_DISK_RESUME_FLOOR_BYTES == 15 * 1024 ** 3
    assert cfg.CAPTURE_INODE_WARN_FRACTION == 0.80
    assert cfg.CAPTURE_INODE_CRITICAL_FRACTION == 0.90
    assert cfg.CAPTURE_INODE_RESUME_FRACTION == 0.88


# ------------------------------------------------------------------ A3. one-off sweep

def test_retire_script_dry_run_reports_but_deletes_nothing(tmp_path):
    from scripts import ki164_retire_depth_family as R

    root = str(tmp_path)
    d1 = _touch(root, "depth", "BTCUSDT", _day(-1))
    d2 = _touch(root, "depth_snapshot", "BTCUSDT", _day(-1))
    d3 = _touch(root, "depth_state", "BTCUSDT", _day(0))
    keep = _touch(root, "aggTrade", "BTCUSDT", _day(0))

    report = R.sweep([root], apply=False)
    assert report["depth"]["files"] == 1 and report["depth"]["bytes"] == 1
    assert report["depth_snapshot"]["files"] == 1
    assert report["depth_state"]["files"] == 1
    assert d1.exists() and d2.exists() and d3.exists(), "dry-run must delete nothing"
    assert keep.exists()


def test_retire_script_apply_deletes_only_the_depth_family_and_is_idempotent(tmp_path):
    from scripts import ki164_retire_depth_family as R

    root = str(tmp_path)
    _touch(root, "depth", "BTCUSDT", _day(-1))
    _touch(root, "depth_snapshot", "BTCUSDT", _day(-1))
    _touch(root, "depth_state", "BTCUSDT", _day(0))
    keep = _touch(root, "aggTrade", "BTCUSDT", _day(0))
    keep_gaps = _touch_gaps(root, _day(-1))

    R.sweep([root], apply=True)
    for d in ("depth", "depth_snapshot", "depth_state"):
        assert not pathlib.Path(root, d).exists(), f"{d} must be gone"
    assert keep.exists() and keep_gaps.exists()

    again = R.sweep([root], apply=True)                     # idempotent
    assert all(v["files"] == 0 for v in again.values())


# ------------------------------------------------------- regressions found in review

def test_stats_does_not_deref_retired_writers(tmp_path):
    """`stats()` is called from run()'s FINALLY. With the family retired the writers are
    None, so an ungated deref raises AttributeError on every clean shutdown — and because
    it is in the finally it REPLACES any real exception, permanently corrupting crash
    diagnostics on all 8 shards."""
    s = svc.CaptureService(root=str(tmp_path), client=None)
    st = s.stats()                                    # must not raise
    assert st["depth_rows"] == 0
    assert st["snapshot_rows"] == 0
    assert "agg_rows" in st


def test_retired_datasets_keep_a_nightly_ceiling_until_the_writers_are_off(tmp_path):
    """Deploy ordering: between merge and the operator's restart+sweep the writers are STILL
    LIVE (the running processes hold the old code). Dropping depth_state's old 2d expire in
    that window would leave it with NO ceiling at all — strictly worse than master. The
    retired datasets therefore keep the TIGHTEST ceiling until they are gone."""
    root = str(tmp_path)
    for ds in ("depth", "depth_snapshot", "depth_state"):
        _touch(root, ds, "BTCUSDT", _day(-3))
        _touch(root, ds, "BTCUSDT", _day(0))
    maintenance.expire_by_policy(root)
    for ds in ("depth", "depth_snapshot", "depth_state"):
        assert not pathlib.Path(root, ds, "symbol=BTCUSDT", f"date={_day(-3)}").exists(), \
            f"{ds} must still be bounded while its writer may be live"
        assert pathlib.Path(root, ds, "symbol=BTCUSDT", f"date={_day(0)}").exists()


def test_injected_snap_scheduler_still_wins(tmp_path):
    """The retirement gate must not silently discard a caller-injected scheduler — that
    inverts the injection contract the ctor docstring advertises."""
    class _Rec:
        def __init__(self):
            self.asked = []

        def request(self, sym):
            self.asked.append(sym)

    rec = _Rec()
    s = svc.CaptureService(root=str(tmp_path), client=None, snap_scheduler=rec)
    assert s._snap_sched is rec
