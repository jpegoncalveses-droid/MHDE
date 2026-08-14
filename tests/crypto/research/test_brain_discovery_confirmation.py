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
# with fresh_count < M is DEMOTED to CONFIRMING (evidence shrank, not failed); it
# re-promotes when the count returns and the edge still clears the gauntlet.

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
