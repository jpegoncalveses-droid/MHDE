"""Tests for the PR-3 capture disk guard (two-tier, firehose-only)."""
from __future__ import annotations

import pathlib

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import disk_guard as dg
from crypto.research.capture_core import service as svc

GIB = 1024 ** 3


def _p(date, size, path=None):
    return dg.Partition(path=path or f"/d/date={date}", date=date, size=size)


# -- pure threshold helpers --

def test_disk_state_tiers():
    assert dg.disk_state(5, soft=30, critical=10) == "critical"
    assert dg.disk_state(10, soft=30, critical=10) == "soft"     # == critical -> soft band
    assert dg.disk_state(29, soft=30, critical=10) == "soft"
    assert dg.disk_state(30, soft=30, critical=10) == "ok"       # == soft -> ok


def test_next_halt_state_hysteresis():
    # below critical -> halt
    assert dg.next_halt_state(9, soft=30, critical=10, halted=False) is True
    # at/above soft -> resume
    assert dg.next_halt_state(30, soft=30, critical=10, halted=True) is False
    # in the band: hold the prior state (no flapping)
    assert dg.next_halt_state(20, soft=30, critical=10, halted=True) is True
    assert dg.next_halt_state(20, soft=30, critical=10, halted=False) is False


# -- oldest-first selection (pure) --

def test_select_oldest_first_until_deficit_met():
    parts = [_p("2026-01-03", 4), _p("2026-01-01", 4), _p("2026-01-02", 4)]
    chosen = dg.select_oldest_to_reclaim(parts, deficit=5)
    assert [c.date for c in chosen] == ["2026-01-01", "2026-01-02"]   # oldest first, stop at >=5


def test_select_returns_all_when_deficit_exceeds_total_and_none_when_satisfied():
    parts = [_p("2026-01-01", 2), _p("2026-01-02", 2)]
    assert len(dg.select_oldest_to_reclaim(parts, deficit=100)) == 2   # all available
    assert dg.select_oldest_to_reclaim(parts, deficit=0) == []         # nothing to do


# -- DiskGuard.enforce (injected free/list/prune) --

def _guard(free_value, parts, *, soft=30, critical=10, pruned_sink=None):
    pruned_sink = pruned_sink if pruned_sink is not None else []

    def fake_prune(paths):
        pruned_sink.extend(paths)
        by_path = {p.path: p.size for p in parts}
        return sum(by_path.get(p, 0) for p in paths)

    g = dg.DiskGuard(
        "/d", soft_floor=soft, critical_floor=critical,
        free_fn=lambda _root: free_value,
        list_fn=lambda _root, _ds: list(parts),
        prune_fn=fake_prune,
    )
    return g, pruned_sink


def test_enforce_prunes_oldest_and_recovers_without_halt():
    parts = [_p("2026-01-01", 4), _p("2026-01-02", 4), _p("2026-01-03", 4)]
    g, pruned = _guard(25, parts)        # 25 < soft 30, deficit 5
    res = g.enforce()
    assert pruned == ["/d/date=2026-01-01", "/d/date=2026-01-02"]  # 4+4 >= 5, oldest first
    assert res.free_after == 25 + 8 and res.halted is False and res.state == "ok"


def test_enforce_halts_when_cannot_recover_above_critical():
    parts = [_p("2026-01-01", 1), _p("2026-01-02", 1)]    # only 2 reclaimable
    g, pruned = _guard(5, parts)         # 5 < critical 10
    res = g.enforce()
    assert len(pruned) == 2 and res.free_after == 7        # still < critical
    assert res.halted is True and res.state == "critical"


def test_enforce_resumes_on_recovery_above_soft():
    g, _ = _guard(35, [])                # already above soft
    g.halted = True
    res = g.enforce()
    assert res.halted is False and res.state == "ok"


def test_enforce_never_prunes_non_firehose_datasets(tmp_path):
    # real tree: one firehose partition + one klines + one REST-series partition
    fire = pathlib.Path(tmp_path, "aggTrade", "symbol=BTCUSDT", "date=2026-01-01")
    kln = pathlib.Path(tmp_path, "klines_1h", "symbol=BTCUSDT", "date=2026-01-01")
    rest = pathlib.Path(tmp_path, "open_interest", "symbol=BTCUSDT", "date=2026-01-01")
    for d in (fire, kln, rest):
        d.mkdir(parents=True)
        (d / "part.parquet").write_bytes(b"x" * 1000)
    # free below soft so the guard must prune; default datasets = FIREHOSE only,
    # real list/prune, real statvfs would not report low so force free via free_fn.
    g = dg.DiskGuard(str(tmp_path), soft_floor=10 * GIB, critical_floor=1,
                     free_fn=lambda _r: 0)   # 0 free -> prune everything firehose
    g.enforce()
    assert not fire.exists()        # firehose partition pruned
    assert kln.exists()             # klines NEVER pruned
    assert rest.exists()            # REST present-state NEVER pruned


# -- service integration: firehose writes dropped during a CRITICAL halt --

class _FakeGuard:
    def __init__(self, halted):
        self.halted = halted
        self.enforced = 0

    def enforce(self):
        self.enforced += 1


def _agg_data():
    return {"e": "aggTrade", "E": 1, "a": 1, "s": "BTCUSDT", "p": "1.0",
            "q": "2.0", "f": 1, "l": 2, "T": 1, "m": False}


def _service(tmp_path, guard):
    return svc.CaptureService(root=str(tmp_path), client=None,
                              enable_snapshots=False, install_signals=False,
                              disk_guard=guard)


def test_firehose_write_dropped_when_guard_halted(tmp_path):
    s = _service(tmp_path, _FakeGuard(halted=True))
    s._on_message("btcusdt@aggTrade", _agg_data(), recv_ns=10)
    s.flush_all()
    assert s.stats()["agg_rows"] == 0          # forward-only drop


def test_firehose_write_kept_when_guard_not_halted(tmp_path):
    s = _service(tmp_path, _FakeGuard(halted=False))
    s._on_message("btcusdt@aggTrade", _agg_data(), recv_ns=10)
    s.flush_all()
    assert s.stats()["agg_rows"] == 1


def test_default_disk_guard_constructed_when_enabled(tmp_path):
    s = svc.CaptureService(root=str(tmp_path), client=None, enable_snapshots=False,
                           install_signals=False)
    assert isinstance(s._disk_guard, dg.DiskGuard)
    s2 = svc.CaptureService(root=str(tmp_path), client=None, enable_snapshots=False,
                            install_signals=False, disk_guard_enabled=False)
    assert s2._disk_guard is None
