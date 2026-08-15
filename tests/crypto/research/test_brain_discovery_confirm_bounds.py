"""Bounding the confirming set (operator dispatch 2026-08-15) — two mechanisms.

MECHANISM 1 — shared-compute confirmation: one walk builds row-aligned window/lift
arrays once and per-feature columns once (LRU-bounded), and every rule's fresh
set/count/edge derives from ITS OWN vectorized mask. Evidence is never shared — only
compute — so the three invariants hold structurally: (a) per-rule fresh_count and
forward_edge are individually correct; (b) cohort provenance untouched; (c) a ghost in a
real family fails on its own numbers.

MECHANISM 2 — pace-collapse expiry (the firing-rate-relative form of ADR-040's future
work): opportunity O = n_fires x elapsed / DISCOVERY_HISTORY (the rule's OWN in-sample
rate converts calendar to instances). Expire CONFIRMING iff O >= floor (=M, no premature
judgment) AND fresh_count < M-H (the resolving/hysteresis band is exempt — demotees are
structurally untouchable) AND fresh_count x PACE_FACTOR < O (resolution would need >Kx
its observed pace's opportunity — implausible). Terminal but retained.
"""
from __future__ import annotations

import math
import statistics

import pytest

from crypto.research.brain.discovery import config as dcfg
from crypto.research.brain.discovery import confirmation as C
from crypto.research.brain.discovery import rulestore as RS
from crypto.research.brain.discovery.rules import Condition, fires, make_rule
from crypto.research.brain.discovery.scoring import EntryResult

_W = 60_000_000_000
_H14 = dcfg.DISCOVERY_HISTORY_NS


def _seed(conn, conds, *, disc_w=0, now=1, n_fires=120):
    rule = make_rule([Condition(*c) for c in conds])
    res = EntryResult(rule=rule, edge=0.02, n_fires=n_fires, depth=len(conds),
                      null_bar=0.005, margin=0.015)
    rid = RS.upsert_entry(conn, res, score_horizon_min=60, breadth=5,
                          discovery_window_ns=disc_w, now_ns=now)
    return rule, rid


def _reference_fresh(rule, engineered, lifts, disc_w):
    """The pre-vectorization semantics, reimplemented: set-based fires + fmean/stdev."""
    fresh = [k for k in fires(rule, engineered) if k[1] > disc_w and k in lifts]
    vals = [lifts[k] for k in fresh]
    n = len(vals)
    if n == 0:
        return 0, None, None
    edge = statistics.fmean(vals)
    if n < 2:
        return n, edge, None
    sd = statistics.stdev(vals)
    if sd == 0:
        return n, edge, math.inf if edge > 0 else (-math.inf if edge < 0 else 0.0)
    return n, edge, edge / (sd / math.sqrt(n))


# -- mechanism 1: vectorized walk equivalence ---------------------------------

def _mixed_tape():
    """Dict tape exercising: absent features (some fv lack 'g.raw'), unlabeled keys,
    stale + fresh instances, two families, a rule whose feature is absent tape-wide."""
    import random
    rng = random.Random(11)
    eng, lifts = {}, {}
    for i in range(1, 301):
        key = (f"S{i % 9}", i * _W)
        fv = {"f.z": rng.uniform(-2, 2)}
        if i % 3:                                    # 'g.raw' sparsely absent
            fv["g.raw"] = rng.uniform(0, 2)
        eng[key] = fv
        if i % 7:                                    # some keys unlabeled
            lifts[key] = rng.gauss(0.01, 0.05)
    return eng, lifts


def test_vectorized_confirmation_matches_reference_per_rule(tmp_path):
    eng, lifts = _mixed_tape()
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        cases = [
            ([("f.z", ">", 0.0)], 40 * _W),
            ([("f.z", ">", 1.2), ("g.raw", "<", 1.5)], 100 * _W),
            ([("g.raw", ">", 0.3)], 0),
            ([("absent.feat", ">", 0.5)], 0),        # feature absent tape-wide -> never fires
        ]
        rids = []
        for conds, dw in cases:
            rule, rid = _seed(conn, conds, disc_w=dw)
            RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
            rids.append((rule, rid, dw))
        C.run_confirmation(conn, eng, lifts, m=30, z=2.0, now_ns=10)
        for rule, rid, dw in rids:
            n_ref, edge_ref, tstat_ref = _reference_fresh(rule, eng, lifts, dw)
            row = RS.get_rule(conn, rid)
            assert row["fresh_count"] == n_ref, rule.canonical_id
            if edge_ref is None:
                assert row["forward_edge"] is None
            else:
                assert row["forward_edge"] == pytest.approx(edge_ref, abs=1e-12)
            want = C.confirmation_decision(n_ref, edge_ref, tstat_ref,
                                           null_bar=0.005, M=30, z=2.0)
            got_state = row["state"]
            assert (want == "promote") == (got_state == RS.PROMOTED)
            assert (want == "reject") == (got_state == RS.REJECTED)
    finally:
        conn.close()


def test_ghost_in_a_real_family_fails_on_its_own_numbers(tmp_path):
    # INVARIANT (c): compute is shared, evidence is NOT. Member A fires where lifts are
    # strong; same-family member B (higher threshold) fires ONLY where lifts are noise.
    eng, lifts = {}, {}
    for i in range(1, 121):
        key = (f"S{i % 7}", i * _W)
        if i <= 60:
            eng[key] = {"f.z": 1.0}                  # A-only zone: strong edge
            lifts[key] = 0.05
        else:
            eng[key] = {"f.z": 3.0}                  # A+B zone: pure noise
            lifts[key] = 0.0001 if i % 2 else -0.0001
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        _, a = _seed(conn, [("f.z", ">", 0.5)])      # fires on all 120
        _, b = _seed(conn, [("f.z", ">", 2.0)])      # fires ONLY on the 60 noise keys
        for rid in (a, b):
            RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
        C.run_confirmation(conn, eng, lifts, m=30, z=2.0, now_ns=10)
        ra, rb = RS.get_rule(conn, a), RS.get_rule(conn, b)
        assert ra["state"] == RS.PROMOTED            # strong on its own 120
        assert rb["state"] == RS.REJECTED            # its own 60 are noise -> fails z/bar
        assert rb["fresh_count"] == 60               # B's OWN count, not A's 120
        assert rb["forward_edge"] == pytest.approx(0.0, abs=1e-6)
    finally:
        conn.close()


# -- mechanism 2: pace-collapse expiry ----------------------------------------

def _age_ns(o_instances, n_fires):
    """Elapsed ns giving opportunity O = o_instances at the rule's in-sample rate."""
    return int(o_instances * _H14 / n_fires)


def test_pace_collapsed_slow_resolver_expires(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        _, rid = _seed(conn, [("f.z", ">", 1.0)], n_fires=120, now=0)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=1)
        with conn:
            conn.execute("UPDATE rules SET fresh_count=4 WHERE rule_id=?", (rid,))
        now = _age_ns(60, 120)                       # O = 60 >= floor(30); 4*6=24 < 60
        n = RS.expire_slow_resolvers(conn, now_ns=now, m=30, hysteresis=5,
                                     history_ns=_H14, opportunity_floor=30, pace_factor=6)
        assert n == 1
        row = RS.get_rule(conn, rid)
        assert row["state"] == RS.EXPIRED
        # provenance retained
        assert row["family_key"] is not None and row["discovery_window_ns"] is not None
    finally:
        conn.close()


def test_young_rule_is_never_judged(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        _, rid = _seed(conn, [("f.z", ">", 1.0)], n_fires=120, now=0)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=1)
        with conn:
            conn.execute("UPDATE rules SET fresh_count=0 WHERE rule_id=?", (rid,))
        now = _age_ns(29, 120)                       # O = 29 < floor(30)
        assert RS.expire_slow_resolvers(conn, now_ns=now, m=30, hysteresis=5,
                                        history_ns=_H14, opportunity_floor=30,
                                        pace_factor=6) == 0
        assert RS.get_rule(conn, rid)["state"] == RS.CONFIRMING
    finally:
        conn.close()


def test_resolving_band_and_demotees_are_exempt(tmp_path):
    # fresh >= M-H (incl. hysteresis demotees re-entering with 25-29 evidence) is NEVER
    # expired regardless of opportunity — "their clock reflects that".
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        _, rid = _seed(conn, [("f.z", ">", 1.0)], n_fires=400, now=0)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=1)
        with conn:
            conn.execute("UPDATE rules SET fresh_count=25 WHERE rule_id=?", (rid,))
        now = _age_ns(10_000, 400)                   # enormous opportunity
        assert RS.expire_slow_resolvers(conn, now_ns=now, m=30, hysteresis=5,
                                        history_ns=_H14, opportunity_floor=30,
                                        pace_factor=6) == 0
        assert RS.get_rule(conn, rid)["state"] == RS.CONFIRMING
    finally:
        conn.close()


def test_on_pace_rule_is_held(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        _, rid = _seed(conn, [("f.z", ">", 1.0)], n_fires=120, now=0)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=1)
        with conn:
            conn.execute("UPDATE rules SET fresh_count=12 WHERE rule_id=?", (rid,))
        now = _age_ns(60, 120)                       # O = 60; 12*6=72 >= 60 -> on pace
        assert RS.expire_slow_resolvers(conn, now_ns=now, m=30, hysteresis=5,
                                        history_ns=_H14, opportunity_floor=30,
                                        pace_factor=6) == 0
        assert RS.get_rule(conn, rid)["state"] == RS.CONFIRMING
    finally:
        conn.close()


def test_expired_is_terminal_and_outside_all_walks(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        _, rid = _seed(conn, [("f", ">", 0.5)], n_fires=120, now=0)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=1)
        with conn:
            conn.execute("UPDATE rules SET fresh_count=0 WHERE rule_id=?", (rid,))
        RS.expire_slow_resolvers(conn, now_ns=_age_ns(60, 120), m=30, hysteresis=5,
                                 history_ns=_H14, opportunity_floor=30, pace_factor=6)
        assert RS.get_rule(conn, rid)["state"] == RS.EXPIRED
        eng = {(f"S{i % 7}", i * _W): {"f": 1.0} for i in range(1, 61)}
        C.run_confirmation(conn, eng, {k: 0.02 for k in eng}, m=30, z=2.0, now_ns=99)
        assert RS.get_rule(conn, rid)["state"] == RS.EXPIRED         # untouched
        with pytest.raises(ValueError):
            RS.set_state(conn, rid, RS.CONFIRMING, now_ns=100)       # terminal
    finally:
        conn.close()


def test_runner_wires_expiry_after_confirmation_with_config_defaults():
    import inspect

    from crypto.research.brain.discovery import runner as RN
    sig = inspect.signature(RN.run_discovery_pass)
    assert sig.parameters["expire_pace_factor"].default is dcfg.EXPIRE_PACE_FACTOR
    assert dcfg.EXPIRE_PACE_FACTOR == 6
