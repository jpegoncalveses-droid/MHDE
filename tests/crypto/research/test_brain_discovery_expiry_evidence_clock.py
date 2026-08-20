"""F4 (KI-166): pace-collapse expiry runs on the EVIDENCE clock, not the wall clock.

The 2026-08-20 incident: expire_slow_resolvers measured elapsed as
``now_ns − discovered_at_ns`` while ``fresh_count`` can only accrue when labels are
ingested. With the frontier frozen (KI-166 stall), wall-elapsed kept growing and
manufactured opportunity out of a dead pipe: 635 rules were expired across three
passes, of which ~all were artifacts (zero ingestible fresh by construction for
post-freeze mints). EXPIRED is terminal — a pipeline outage permanently killed rules.

The fix: elapsed = ``frontier_ns − discovery_window_ns`` — labeled tape actually
appended past the rule's own evidence boundary. A frozen frontier freezes
opportunity with it (D9: instance/evidence-relative, never calendar). ``now_ns``
remains only the updated_at stamp.

Plus the incident-scoped expiry hold: after recovery the frontier delta OVERCOUNTS
labeled exposure for pre-gap mints until the 5-day hole rolls out of the 14-day
window (~2026-09-03) — expiry stays held until the frontier passes
``EXPIRE_RESUME_FRONTIER_NS`` (operator-tunable; 0 = always active).
"""
from __future__ import annotations

import inspect

from crypto.research.brain.discovery import config as dcfg
from crypto.research.brain.discovery import rulestore as RS
from crypto.research.brain.discovery import runner as RN
from crypto.research.brain.discovery.rules import Condition, make_rule
from crypto.research.brain.discovery.scoring import EntryResult

_H14 = dcfg.DISCOVERY_HISTORY_NS


def _seed(conn, feature, *, disc_w, n_fires, fresh, now=1):
    rule = make_rule([Condition(feature, ">", 1.0)])
    res = EntryResult(rule=rule, edge=0.02, n_fires=n_fires, depth=1,
                      null_bar=0.005, margin=0.015)
    rid = RS.upsert_entry(conn, res, score_horizon_min=60, breadth=5,
                          discovery_window_ns=disc_w, now_ns=now)
    RS.set_state(conn, rid, RS.CONFIRMING, now_ns=now)
    with conn:
        conn.execute("UPDATE rules SET fresh_count=? WHERE rule_id=?", (fresh, rid))
    return rid


def _expire(conn, *, frontier_ns, now_ns):
    return RS.expire_slow_resolvers(
        conn, now_ns=now_ns, frontier_ns=frontier_ns, m=30, hysteresis=5,
        history_ns=_H14, opportunity_floor=30, pace_factor=6)


# --- THE STALL PIN (mutation-verified: reverting the clock to now-based fails) --

def test_frozen_frontier_never_manufactures_opportunity(tmp_path):
    # A rule minted AT the frozen frontier: zero labeled tape has ever been appended
    # past its boundary, so zero fresh was ever ingestible. Under the wall clock,
    # 10 days of elapsed would expire it (O huge, fresh 0). Under the evidence
    # clock, opportunity is 0 forever while the pipe is dead — held, at ANY wall age.
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        f0 = 5 * _H14                                 # the frozen frontier
        rid = _seed(conn, "f.z", disc_w=f0, n_fires=120, fresh=0)
        for wall_days in (10, 100):
            now = f0 + wall_days * 86_400_000_000_000
            assert _expire(conn, frontier_ns=f0, now_ns=now) == 0
        assert RS.get_rule(conn, rid)["state"] == RS.CONFIRMING
    finally:
        conn.close()


def test_advancing_frontier_restores_genuine_expiry(tmp_path):
    # Same rule shape, but the frontier HAS advanced a full window past its
    # boundary: rate-collapsed (fresh=4 vs n_fires=120) => expires. The clock is
    # evidence-relative, not lenient.
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        f0 = 5 * _H14
        rid = _seed(conn, "f.z", disc_w=f0, n_fires=120, fresh=4)
        assert _expire(conn, frontier_ns=f0 + _H14, now_ns=f0 + _H14) == 1
        assert RS.get_rule(conn, rid)["state"] == RS.EXPIRED
    finally:
        conn.close()


def test_wall_clock_plays_no_part_in_the_predicate(tmp_path):
    # Identical frontier, wildly different now_ns: identical outcome (now_ns is
    # only the updated_at stamp).
    outcomes = []
    for now_offset in (0, 400 * _H14):
        import sqlite3  # noqa: F401  (fresh DB per iteration)
        conn = RS.connect(str(tmp_path / f"d{now_offset}.sqlite"))
        try:
            f0 = 5 * _H14
            _seed(conn, "f.z", disc_w=f0, n_fires=120, fresh=4)
            outcomes.append(_expire(conn, frontier_ns=f0 + _H14 // 2,
                                    now_ns=f0 + _H14 // 2 + now_offset))
        finally:
            conn.close()
    assert outcomes[0] == outcomes[1]


# --- the incident-scoped expiry hold -------------------------------------------

def test_expiry_hold_helper_truth_table():
    assert RN._expiry_active(frontier_ns=100, resume_ns=0) is True
    assert RN._expiry_active(frontier_ns=100, resume_ns=100) is True
    assert RN._expiry_active(frontier_ns=99, resume_ns=100) is False


def test_runner_wires_evidence_clock_and_hold_with_config_defaults():
    sig = inspect.signature(RN.run_discovery_pass)
    assert (sig.parameters["expire_resume_frontier_ns"].default
            is dcfg.EXPIRE_RESUME_FRONTIER_NS)
    # the hold constant is the KI-166 gap-rollout boundary: 2026-09-03 00:00 UTC
    assert dcfg.EXPIRE_RESUME_FRONTIER_NS == 1_788_393_600_000_000_000
