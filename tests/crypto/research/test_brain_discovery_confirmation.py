"""Component 5 (§6.2) — forward confirmation on POST-DISCOVERY instances only.

The decisive property (§13c): a rule is judged forward ONLY on instances whose window
settled AFTER its discovery frontier — data that did not exist during the search, so the
gate cannot be gamed by the search. Promotion needs >= M fresh instances AND an edge that
stays positive, distinguishable from zero, and past the in-sample null bar.
"""
from __future__ import annotations

import pytest

from crypto.research.brain.discovery import confirmation as C
from crypto.research.brain.discovery import rulestore as RS
from crypto.research.brain.discovery.rules import Condition, fires, make_rule
from crypto.research.brain.discovery.scoring import EntryResult

_W = 60_000_000_000


def test_only_post_discovery_instances_are_fresh():
    rule = make_rule([Condition("f", ">", 0.5)])
    eng = {(f"S{i}", i * _W): {"f": 1.0} for i in range(10)}     # all fire
    lifts = {k: 0.01 for k in eng}
    fresh = C.fresh_instances(rule, eng, lifts, discovery_window_ns=4 * _W)
    assert set(fresh) == {k for k in eng if k[1] > 4 * _W}        # windows 5..9 only
    assert all(w > 4 * _W for _, w in fresh)


def test_confirmation_decision_wait_promote_reject():
    # < M -> wait
    assert C.confirmation_decision(5, 0.02, 9.0, null_bar=0.005, M=30, z=2.0) == "wait"
    # >= M, positive, significant, past bar -> promote
    assert C.confirmation_decision(40, 0.02, 9.0, null_bar=0.005, M=30, z=2.0) == "promote"
    # >= M but edge below the bar -> reject
    assert C.confirmation_decision(40, 0.004, 9.0, null_bar=0.005, M=30, z=2.0) == "reject"
    # >= M, positive & past bar but NOT distinguishable from zero -> reject
    assert C.confirmation_decision(40, 0.02, 0.4, null_bar=0.005, M=30, z=2.0) == "reject"
    # >= M, wrong sign -> reject
    assert C.confirmation_decision(40, -0.01, -3.0, null_bar=0.005, M=30, z=2.0) == "reject"


def _seed_rule(conn, conds=(("f", ">", 0.5),), null_bar=0.005, disc_w=4 * _W):
    rule = make_rule([Condition(*c) for c in conds])
    res = EntryResult(rule=rule, edge=0.02, n_fires=120, depth=1, null_bar=null_bar,
                      margin=0.02 - null_bar)
    rid = RS.upsert_entry(conn, res, score_horizon_min=60, breadth=5,
                          discovery_window_ns=disc_w, now_ns=1)
    return rule, rid


def test_run_confirmation_advances_discovered_to_confirming(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rule, rid = _seed_rule(conn)
        eng = {(f"S{i}", i * _W): {"f": 1.0} for i in range(10)}
        lifts = {k: 0.02 for k in eng}            # only windows 5..9 are fresh (5 < M)
        C.run_confirmation(conn, eng, lifts, m=30, z=2.0, now_ns=10)
        row = RS.get_rule(conn, rid)
        assert row["state"] == RS.CONFIRMING       # advanced, but too few fresh to decide
        assert row["fresh_count"] == 5
    finally:
        conn.close()


def test_run_confirmation_promotes_a_holding_edge(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rule, rid = _seed_rule(conn, disc_w=0)     # everything after window 0 is fresh
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
        eng = {(f"S{i % 7}", i * _W): {"f": 1.0} for i in range(1, 61)}  # 60 fresh fires
        lifts = {k: 0.02 for k in eng}             # strong, consistent positive edge
        C.run_confirmation(conn, eng, lifts, m=30, z=2.0, now_ns=10)
        assert RS.get_rule(conn, rid)["state"] == RS.PROMOTED
    finally:
        conn.close()


def test_run_confirmation_rejects_a_decayed_edge(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rule, rid = _seed_rule(conn, disc_w=0)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
        eng = {(f"S{i % 7}", i * _W): {"f": 1.0} for i in range(1, 61)}
        lifts = {k: -0.01 for k in eng}            # forward edge went negative -> reject
        C.run_confirmation(conn, eng, lifts, m=30, z=2.0, now_ns=10)
        assert RS.get_rule(conn, rid)["state"] == RS.REJECTED
    finally:
        conn.close()


def test_run_confirmation_demotes_a_decayed_promoted_rule(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rule, rid = _seed_rule(conn, disc_w=0)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
        RS.set_state(conn, rid, RS.PROMOTED, now_ns=3)
        eng = {(f"S{i % 7}", i * _W): {"f": 1.0} for i in range(1, 61)}
        lifts = {k: 0.0001 for k in eng}           # now below the null bar -> decay -> reject
        C.run_confirmation(conn, eng, lifts, m=30, z=2.0, now_ns=10)
        assert RS.get_rule(conn, rid)["state"] == RS.REJECTED
        assert "deca" in (RS.get_rule(conn, rid)["reject_reason"] or "").lower()
    finally:
        conn.close()


# -- Sub-M decay immunity fix (2026-08-14): evidence-shrink demotes, never exempts --------
# fresh recounts are NON-MONOTONIC (features re-derive each pass; instances cross rule
# thresholds both ways). The old _decayed gate required n >= M, so a promoted rule whose
# recount fell below CONFIRM_M became decay-IMMUNE — 30/110 live promoted rules sat at
# 24-29 fresh, un-demotable regardless of forward edge. New semantics: a promoted rule
# with fresh_count < M - CONFIRM_DEMOTE_HYSTERESIS is DEMOTED to CONFIRMING (evidence
# shrank, not failed); the band [M-H, M) HOLDS (decay-quiet, ADR-041); it re-promotes
# when the count returns and the edge still clears the gauntlet.

def test_promoted_rule_with_sub_m_fresh_demotes_to_confirming(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rule, rid = _seed_rule(conn, disc_w=4 * _W)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
        RS.set_state(conn, rid, RS.PROMOTED, now_ns=3)
        eng = {(f"S{i}", i * _W): {"f": 1.0} for i in range(10)}   # 5 fresh (< M=30)
        lifts = {k: 0.02 for k in eng}                             # edge is HEALTHY
        summary = C.run_confirmation(conn, eng, lifts, m=30, z=2.0, now_ns=10)
        row = RS.get_rule(conn, rid)
        assert row["state"] == RS.CONFIRMING                       # demoted, NOT rejected
        assert not row["reject_reason"]
        assert row["fresh_count"] == 5
        assert summary.get("demoted", 0) == 1
    finally:
        conn.close()


def test_promoted_rule_with_zero_fresh_demotes_to_confirming(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rule, rid = _seed_rule(conn, disc_w=100 * _W)              # nothing is fresh
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
        RS.set_state(conn, rid, RS.PROMOTED, now_ns=3)
        eng = {(f"S{i}", i * _W): {"f": 1.0} for i in range(10)}
        lifts = {k: 0.02 for k in eng}
        C.run_confirmation(conn, eng, lifts, m=30, z=2.0, now_ns=10)
        assert RS.get_rule(conn, rid)["state"] == RS.CONFIRMING
    finally:
        conn.close()


def test_demoted_rule_repromotes_when_evidence_returns(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rule, rid = _seed_rule(conn, disc_w=0)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
        RS.set_state(conn, rid, RS.PROMOTED, now_ns=3)
        few = {(f"S{i}", i * _W): {"f": 1.0} for i in range(1, 6)}   # 5 fresh -> demote
        C.run_confirmation(conn, few, {k: 0.02 for k in few}, m=30, z=2.0, now_ns=10)
        assert RS.get_rule(conn, rid)["state"] == RS.CONFIRMING
        many = {(f"S{i % 7}", i * _W): {"f": 1.0} for i in range(1, 61)}  # 60 fresh, healthy
        C.run_confirmation(conn, many, {k: 0.02 for k in many}, m=30, z=2.0, now_ns=11)
        assert RS.get_rule(conn, rid)["state"] == RS.PROMOTED       # re-earned
    finally:
        conn.close()


def test_promoted_rule_with_healthy_full_evidence_stays_promoted(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rule, rid = _seed_rule(conn, disc_w=0)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
        RS.set_state(conn, rid, RS.PROMOTED, now_ns=3)
        eng = {(f"S{i % 7}", i * _W): {"f": 1.0} for i in range(1, 61)}
        lifts = {k: 0.02 for k in eng}
        C.run_confirmation(conn, eng, lifts, m=30, z=2.0, now_ns=10)
        assert RS.get_rule(conn, rid)["state"] == RS.PROMOTED
    finally:
        conn.close()


def test_sub_m_demotes_even_with_a_terrible_edge(tmp_path):
    # It is the COUNT, not the edge, that selects the demotion branch: sub-M + awful edge
    # -> CONFIRMING (parked awaiting evidence), never rejected-by-decay (decay judgment
    # requires a decision-floor sample). Pins the branch selection against a future
    # "demote only if healthy else reject" refactor.
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rule, rid = _seed_rule(conn, disc_w=4 * _W)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
        RS.set_state(conn, rid, RS.PROMOTED, now_ns=3)
        eng = {(f"S{i}", i * _W): {"f": 1.0} for i in range(10)}   # 5 fresh (< M)
        lifts = {k: -0.9 for k in eng}                             # edge is TERRIBLE
        C.run_confirmation(conn, eng, lifts, m=30, z=2.0, now_ns=10)
        row = RS.get_rule(conn, rid)
        assert row["state"] == RS.CONFIRMING
        assert not row["reject_reason"]
    finally:
        conn.close()


def test_returning_demotee_faces_the_full_gauntlet_including_z(tmp_path):
    # DOCUMENTED ASYMMETRY (review F1): a rule that STAYS promoted faces the edge-only
    # decay check, but a demoted rule RETURNS through confirmation_decision — including
    # the tstat >= z significance test it originally passed at first promotion. The
    # fixture is DETERMINISTIC and isolates the z-gate as the SOLE failing clause:
    # lifts alternate 0.35 / -0.25 -> n=60, mean edge exactly 0.05 (10x the 0.005 bar,
    # positive, past the bar) with tstat ~1.28 < z=2.0. The z=-inf counter-assertion
    # proves the z-gate is load-bearing — delete the tstat clause and this test fails.
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rule, rid = _seed_rule(conn, disc_w=0)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
        RS.set_state(conn, rid, RS.PROMOTED, now_ns=3)
        few = {(f"S{i}", i * _W): {"f": 1.0} for i in range(1, 6)}
        C.run_confirmation(conn, few, {k: 0.02 for k in few}, m=30, z=2.0, now_ns=10)
        assert RS.get_rule(conn, rid)["state"] == RS.CONFIRMING       # demoted

        keys = [(f"S{i % 7}", i * _W) for i in range(1, 61)]
        many = {k: {"f": 1.0} for k in keys}
        vals = [0.05 + 0.30, 0.05 - 0.30] * 30                        # edge 0.05, tstat ~1.28
        noisy = dict(zip(keys, vals))
        n, edge, tstat = C.fresh_stats([noisy[k] for k in keys])
        assert n == 60 and edge == pytest.approx(0.05) and 1.2 < tstat < 1.4
        # the z-gate is the SOLE failing clause: with z disabled this promotes...
        assert C.confirmation_decision(n, edge, tstat, null_bar=0.005, M=30,
                                       z=float("-inf")) == "promote"
        # ...and through the real path with z=2.0 the returning rule is REJECTED.
        C.run_confirmation(conn, many, noisy, m=30, z=2.0, now_ns=11)
        row = RS.get_rule(conn, rid)
        assert row["state"] == RS.REJECTED
        assert row["reject_reason"] == "forward edge not confirmed"
    finally:
        conn.close()


# -- Demotion hysteresis (operator decision 2026-08-14, after PR #90's first-application
# analysis): demote at fresh < M-5, promote at >= M. 66 of the 75 demotion-eligible
# promoted rules sat in [M-5, M) — recount jitter, not evidence loss — so a symmetric
# threshold flaps ~88% of them every pass. The band [M-5, M) is a deliberate HOLD zone:
# no demotion, no decay judgment (decay needs a full n >= M sample), no promotion — a
# bounded quiet zone traded for flap prevention, pinned below.

def test_promoted_holds_in_hysteresis_band_even_with_bad_edge(tmp_path):
    from crypto.research.brain.discovery import config as dcfg
    assert dcfg.CONFIRM_DEMOTE_HYSTERESIS == 5
    # FRESH connection per case (review F3): the > threshold is one-sided, so sharing a
    # DB would silently re-judge earlier rules on later passes.
    for i, (n_fresh, edge) in enumerate(((25, 0.02), (29, 0.02), (27, -0.9))):
        conn = RS.connect(str(tmp_path / f"d{i}.sqlite"))
        try:
            rule, rid = _seed_rule(conn, disc_w=0)
            RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
            RS.set_state(conn, rid, RS.PROMOTED, now_ns=3)
            eng = {(f"S{j}", j * _W): {"f": 1.0} for j in range(1, n_fresh + 1)}
            lifts = {k: edge for k in eng}
            C.run_confirmation(conn, eng, lifts, m=30, z=2.0, hysteresis=5, now_ns=10)
            row = RS.get_rule(conn, rid)
            assert row["state"] == RS.PROMOTED, (n_fresh, edge)      # HOLD: no flap, no decay
            assert row["fresh_count"] == n_fresh
        finally:
            conn.close()


def test_hysteresis_must_be_smaller_than_m(tmp_path):
    # review F4: m is a to-be-retuned default; h >= m would make demotion dead code and
    # silently reopen the sub-M immunity — guarded with a hard error.
    import pytest as _pytest
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        with _pytest.raises(ValueError):
            C.run_confirmation(conn, {}, {}, m=5, z=2.0, hysteresis=5, now_ns=1)
        with _pytest.raises(ValueError):
            C.run_confirmation(conn, {}, {}, m=30, z=2.0, hysteresis=-1, now_ns=1)
    finally:
        conn.close()


def test_promoted_demotes_below_the_hysteresis_floor(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rule, rid = _seed_rule(conn, disc_w=0)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
        RS.set_state(conn, rid, RS.PROMOTED, now_ns=3)
        eng = {(f"S{i}", i * _W): {"f": 1.0} for i in range(1, 25)}   # 24 fresh < M-5
        lifts = {k: 0.02 for k in eng}
        summary = C.run_confirmation(conn, eng, lifts, m=30, z=2.0, now_ns=10)
        assert RS.get_rule(conn, rid)["state"] == RS.CONFIRMING
        assert summary["demoted"] == 1
    finally:
        conn.close()


def test_decay_and_promotion_thresholds_unchanged_by_hysteresis(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rule, rid = _seed_rule(conn, disc_w=0)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
        RS.set_state(conn, rid, RS.PROMOTED, now_ns=3)
        eng = {(f"S{i % 7}", i * _W): {"f": 1.0} for i in range(1, 61)}   # 60 fresh >= M
        C.run_confirmation(conn, eng, {k: 0.0001 for k in eng}, m=30, z=2.0, now_ns=10)
        assert RS.get_rule(conn, rid)["state"] == RS.REJECTED             # decay unchanged
    finally:
        conn.close()
