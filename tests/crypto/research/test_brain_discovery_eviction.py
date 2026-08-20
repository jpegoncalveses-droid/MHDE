"""S8 mature-seat eviction (ADR-042 amendment; Option 2 admission round).

The measured dead zone: for a mature rule, promote/reject need fresh >= M while
pace-expiry needs fresh < M-H AND fresh*pace < O — any never-promoted confirming rule
whose forward rate sits stably in [n_fires/6, M) per window has NO exit, and under
scarce seats it burns a family slot forever (53% of floor-passing rules measured in
the zone). Eviction: once the rule is MATURE on the EVIDENCE clock (frontier −
discovery_window >= DISCOVERY_HISTORY — fresh_count is then the definitive
rolling-window measurement), evict iff fresh < M−H. The resolving band [M−H, M)
stays exempt (ADR-041); demotees are structurally exempt (promoted-ever marker);
immature rules keep the full ADR-042 pace predicate. Runs under the same KI-166
gap-rollout hold as expiry.
"""
from __future__ import annotations

import inspect

from crypto.research.brain.discovery import config as dcfg
from crypto.research.brain.discovery import rulestore as RS
from crypto.research.brain.discovery import runner as RN
from crypto.research.brain.discovery.rules import Condition, make_rule
from crypto.research.brain.discovery.scoring import EntryResult

_H14 = dcfg.DISCOVERY_HISTORY_NS


def _seed_confirming(conn, feature, *, disc_w, n_fires, fresh):
    rule = make_rule([Condition(feature, ">", 1.0)])
    res = EntryResult(rule=rule, edge=0.02, n_fires=n_fires, depth=1,
                      null_bar=0.005, margin=0.015)
    rid = RS.upsert_entry(conn, res, score_horizon_min=60, breadth=5,
                          discovery_window_ns=disc_w, now_ns=1)
    RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
    with conn:
        conn.execute("UPDATE rules SET fresh_count=? WHERE rule_id=?", (fresh, rid))
    return rid


def _evict(conn, *, frontier_ns, now_ns=99):
    return RS.evict_mature_unresolved(conn, now_ns=now_ns, frontier_ns=frontier_ns,
                                      m=30, hysteresis=5, history_ns=_H14)


def test_dead_zone_seat_is_evicted_once_mature(tmp_path):
    # THE DEAD-ZONE PIN: fresh=13, n_fires=25 — pace-expiry can NEVER take it
    # (sub-floor: O <= 25 < 30) and it can never resolve (rate < 30/window).
    # Without eviction this seat is permanent; with it, ordinary turnover.
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rid = _seed_confirming(conn, "a.x", disc_w=0, n_fires=25, fresh=13)
        pace = RS.expire_slow_resolvers(
            conn, now_ns=99, frontier_ns=2 * _H14, m=30, hysteresis=5,
            history_ns=_H14, opportunity_floor=30, pace_factor=6)
        assert pace == 0                                          # the zone, measured
        assert _evict(conn, frontier_ns=2 * _H14) == 1
        row = RS.get_rule(conn, rid)
        assert row["state"] == RS.EXPIRED
        assert "mature-evicted" in (row["reject_reason"] or "")
    finally:
        conn.close()


def test_resolving_band_is_exempt_at_any_maturity(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rid = _seed_confirming(conn, "a.x", disc_w=0, n_fires=25, fresh=25)
        assert _evict(conn, frontier_ns=50 * _H14) == 0
        assert RS.get_rule(conn, rid)["state"] == RS.CONFIRMING
    finally:
        conn.close()


def test_immature_rule_is_exempt_even_at_fresh_zero(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rid = _seed_confirming(conn, "a.x", disc_w=0, n_fires=25, fresh=0)
        assert _evict(conn, frontier_ns=_H14 - 1) == 0            # evidence-immature
        assert RS.get_rule(conn, rid)["state"] == RS.CONFIRMING
    finally:
        conn.close()


def test_demotee_is_never_evicted(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rid = _seed_confirming(conn, "a.x", disc_w=0, n_fires=25, fresh=13)
        with conn:
            conn.execute("UPDATE rules SET promoted_at_ns=5 WHERE rule_id=?", (rid,))
        assert _evict(conn, frontier_ns=50 * _H14) == 0
        assert RS.get_rule(conn, rid)["state"] == RS.CONFIRMING
    finally:
        conn.close()


def test_eviction_uses_the_evidence_clock_not_wall(tmp_path):
    # Frozen frontier => nothing matures, no matter the wall clock (KI-166 lesson).
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        f0 = 5 * _H14
        rid = _seed_confirming(conn, "a.x", disc_w=f0, n_fires=25, fresh=0)
        assert _evict(conn, frontier_ns=f0, now_ns=f0 + 100 * _H14) == 0
        assert RS.get_rule(conn, rid)["state"] == RS.CONFIRMING
    finally:
        conn.close()


def test_runner_wires_eviction_under_the_expiry_hold():
    src = inspect.getsource(RN.run_discovery_pass)
    assert "evict_mature_unresolved" in src
    # both retention steps sit behind the same KI-166 hold
    assert src.index("_expiry_active") < src.index("evict_mature_unresolved")
