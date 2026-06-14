"""Phase 0 — root-filesystem inode guard tests.

(d) The inode guard WARNs (Telegram) at 80% used, goes CRITICAL + HALTS firehose
    writes at 90% (with hysteresis so it does not flap), alerts only on threshold
    TRANSITIONS (no spam), and the service drops incoming firehose data while the
    inode guard is halted. Capture must fail itself before it can starve the OS
    again — without ever opening DuckDB.
"""
from __future__ import annotations

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import disk_guard as dg
from crypto.research.capture_core import service as svc


# -- shipped thresholds --------------------------------------------------------

def test_shipped_inode_thresholds():
    assert cfg.CAPTURE_INODE_WARN_FRACTION == 0.80
    assert cfg.CAPTURE_INODE_CRITICAL_FRACTION == 0.90
    assert cfg.CAPTURE_INODE_WARN_FRACTION < cfg.CAPTURE_INODE_CRITICAL_FRACTION


# -- pure helpers --------------------------------------------------------------

def test_inode_used_fraction_math():
    assert dg.inode_used(total=100, avail=5) == 0.95     # 95% used
    assert dg.inode_used(total=100, avail=100) == 0.0
    assert dg.inode_used(total=0, avail=0) == 0.0        # empty fs -> not "full"


def test_inode_state_tiers():
    assert dg.inode_state(0.95, warn=0.80, critical=0.90) == "critical"
    assert dg.inode_state(0.90, warn=0.80, critical=0.90) == "critical"  # == critical
    assert dg.inode_state(0.85, warn=0.80, critical=0.90) == "warn"
    assert dg.inode_state(0.80, warn=0.80, critical=0.90) == "warn"      # == warn
    assert dg.inode_state(0.50, warn=0.80, critical=0.90) == "ok"


def test_inode_halt_hysteresis():
    assert dg.next_inode_halt_state(0.92, warn=0.80, critical=0.90, halted=False) is True
    assert dg.next_inode_halt_state(0.79, warn=0.80, critical=0.90, halted=True) is False
    # in the band: hold the prior state (no flapping)
    assert dg.next_inode_halt_state(0.85, warn=0.80, critical=0.90, halted=True) is True
    assert dg.next_inode_halt_state(0.85, warn=0.80, critical=0.90, halted=False) is False


# -- InodeGuard.enforce (injected usage + notifier) ----------------------------

def _guard(used_value, sink=None):
    sink = sink if sink is not None else []
    used = used_value if callable(used_value) else (lambda _r: used_value)
    g = dg.InodeGuard("/x", warn_fraction=0.80, critical_fraction=0.90,
                      used_fn=used, notify_fn=sink.append)
    return g, sink


def test_inode_guard_warns_via_notifier_at_threshold():
    g, sent = _guard(0.82)
    res = g.enforce()
    assert res.state == "warn" and res.halted is False
    assert len(sent) == 1 and "%" in sent[0]      # a Telegram WARN was emitted


def test_inode_guard_critical_halts_and_alerts():
    g, sent = _guard(0.95)
    res = g.enforce()
    assert res.state == "critical" and res.halted is True
    assert len(sent) == 1


def test_inode_guard_alerts_only_on_transition_no_spam():
    g, sent = _guard(0.95)
    g.enforce(); g.enforce(); g.enforce()
    assert len(sent) == 1                          # only the entering-critical edge


def test_inode_guard_recovers_below_warn():
    used = [0.95]
    g, _sent = _guard(lambda _r: used[0])
    g.enforce()
    assert g.halted is True
    used[0] = 0.50
    res = g.enforce()
    assert res.halted is False and res.state == "ok"


# -- service integration -------------------------------------------------------

class _FakeGuard:
    def __init__(self, halted=False):
        self.halted = halted

    def enforce(self):
        return None


def _agg_data():
    return {"e": "aggTrade", "E": 1, "a": 1, "s": "BTCUSDT", "p": "1.0",
            "q": "2.0", "f": 1, "l": 2, "T": 1, "m": False}


def test_firehose_write_dropped_when_inode_guard_halted(tmp_path):
    s = svc.CaptureService(root=str(tmp_path), client=None, enable_snapshots=False,
                           install_signals=False, disk_guard=_FakeGuard(halted=False),
                           inode_guard=_FakeGuard(halted=True))
    s._on_message("btcusdt@aggTrade", _agg_data(), recv_ns=10)
    s.flush_all()
    assert s.stats()["agg_rows"] == 0              # forward-only drop on inode halt


def test_firehose_write_kept_when_neither_guard_halted(tmp_path):
    s = svc.CaptureService(root=str(tmp_path), client=None, enable_snapshots=False,
                           install_signals=False, disk_guard=_FakeGuard(halted=False),
                           inode_guard=_FakeGuard(halted=False))
    s._on_message("btcusdt@aggTrade", _agg_data(), recv_ns=10)
    s.flush_all()
    assert s.stats()["agg_rows"] == 1


def test_default_inode_guard_constructed_when_enabled(tmp_path):
    s = svc.CaptureService(root=str(tmp_path), client=None, enable_snapshots=False,
                           install_signals=False)
    assert isinstance(s._inode_guard, dg.InodeGuard)
    s2 = svc.CaptureService(root=str(tmp_path), client=None, enable_snapshots=False,
                            install_signals=False, inode_guard_enabled=False)
    assert s2._inode_guard is None
