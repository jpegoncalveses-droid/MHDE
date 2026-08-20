"""Option 2 family-level admission (ADR-043; design: data/processed/family_admission_design.md).

Admission gates the DISCOVERED->CONFIRMING advance at the family level: at most ONE
newly-minted identity per family per pass, at most k=3 concurrent seats per family
(cohort-distinct by construction), selected by in-sample null_margin only, with the
resolvability floor n_fires >= M (a sub-floor rule can neither promote within the
14-day window nor pace-expire — admitting it buys an eternal walk slot and no possible
verdict). Everything else is BENCHED: recorded with full provenance, never walked,
terminal in v1. Demotees ride their own lane (PROMOTED->CONFIRMING is untouched and a
demotee never consumes a seat). Seats are identified by the n_fires_at_admission stamp
— which also closes the S9 drift leak (a later re-mint deflating n_fires can no longer
revoke expiry eligibility: opportunity is judged against the max).
"""
from __future__ import annotations

import inspect

from crypto.research.brain.discovery import admission as AD
from crypto.research.brain.discovery import config as dcfg
from crypto.research.brain.discovery import confirmation as C
from crypto.research.brain.discovery import rulestore as RS
from crypto.research.brain.discovery import runner as RN
from crypto.research.brain.discovery.rules import Condition, make_rule
from crypto.research.brain.discovery.scoring import EntryResult

_H14 = dcfg.DISCOVERY_HISTORY_NS
_W = 60_000_000_000


def _mint(conn, feature, thr, *, n_fires=120, margin=0.015, run_id=None, now=1):
    rule = make_rule([Condition(feature, ">", thr)])
    res = EntryResult(rule=rule, edge=0.02, n_fires=n_fires, depth=1,
                      null_bar=0.005, margin=margin)
    return RS.upsert_entry(conn, res, score_horizon_min=60, breadth=5,
                           discovery_window_ns=0, now_ns=now, minted_run_id=run_id)


def _admit(conn, *, m=30, k=3, now=9):
    return AD.run_admission(conn, m=m, k=k, now_ns=now)


def _seat(conn, feature, thr, *, run_id, n_fires=120, margin=0.015):
    """Mint + admit + advance to CONFIRMING — a stamped seat."""
    rid = _mint(conn, feature, thr, n_fires=n_fires, margin=margin, run_id=run_id)
    _admit(conn)
    RS.set_state(conn, rid, RS.CONFIRMING, now_ns=10)
    return rid


# -- floor, selection, one-per-pass ---------------------------------------------

def test_sub_floor_variant_is_benched_even_alone(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rid = _mint(conn, "f.z", 1.0, n_fires=29, run_id=1)      # below M=30
        s = _admit(conn)
        assert s == {"admitted": 0, "benched": 1}
        assert RS.get_rule(conn, rid)["state"] == AD.RS.BENCHED
    finally:
        conn.close()


def test_best_margin_wins_the_seat_and_losers_bench(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        lo = _mint(conn, "f.z", 1.0, margin=0.01, run_id=1)
        hi = _mint(conn, "f.z", 2.0, margin=0.09, run_id=1)      # same family, better margin
        s = _admit(conn)
        assert s == {"admitted": 1, "benched": 1}
        assert RS.get_rule(conn, hi)["state"] == RS.DISCOVERED    # walk advances it
        assert RS.get_rule(conn, hi)["n_fires_at_admission"] == 120
        assert RS.get_rule(conn, lo)["state"] == RS.BENCHED
        assert RS.get_rule(conn, lo)["n_fires_at_admission"] is None
    finally:
        conn.close()


def test_two_families_each_admit_one(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        _mint(conn, "f.z", 1.0, run_id=1)
        _mint(conn, "g.y", 1.0, run_id=1)
        assert _admit(conn) == {"admitted": 2, "benched": 0}
    finally:
        conn.close()


# -- quota k and the concurrent bound -------------------------------------------

def test_family_at_quota_benches_new_variants(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        for i, thr in enumerate((1.0, 2.0, 3.0)):
            _seat(conn, "f.z", thr, run_id=i + 1)
        late = _mint(conn, "f.z", 4.0, run_id=4)
        s = _admit(conn)
        assert s == {"admitted": 0, "benched": 1}
        assert RS.get_rule(conn, late)["state"] == RS.BENCHED
    finally:
        conn.close()


def test_family_below_quota_admits_and_seats_are_cohort_distinct(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        a = _seat(conn, "f.z", 1.0, run_id=1)
        b = _seat(conn, "f.z", 2.0, run_id=2)
        c = _mint(conn, "f.z", 3.0, run_id=3)
        assert _admit(conn) == {"admitted": 1, "benched": 0}
        runs = {RS.get_rule(conn, r)["minted_run_id"] for r in (a, b, c)}
        assert runs == {1, 2, 3}                                  # distinct cohorts
    finally:
        conn.close()


def test_resolved_seat_frees_the_slot(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        seats = [_seat(conn, "f.z", t, run_id=i + 1) for i, t in enumerate((1.0, 2.0, 3.0))]
        RS.set_state(conn, seats[0], RS.REJECTED, reject_reason="x", now_ns=20)
        nxt = _mint(conn, "f.z", 4.0, run_id=4)
        assert _admit(conn) == {"admitted": 1, "benched": 0}
        assert RS.get_rule(conn, nxt)["n_fires_at_admission"] is not None
    finally:
        conn.close()


# -- the demotee lane and unstamped confirming rows ------------------------------

def test_demotee_never_consumes_a_seat(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        demotee = _seat(conn, "f.z", 0.5, run_id=1)
        RS.set_state(conn, demotee, RS.PROMOTED, now_ns=20)
        RS.set_state(conn, demotee, RS.CONFIRMING, now_ns=21)     # demotion
        _seat(conn, "f.z", 1.0, run_id=2)
        _seat(conn, "f.z", 2.0, run_id=3)
        # family has 3 confirming rows, but the demotee holds no seat: one slot open
        nxt = _mint(conn, "f.z", 3.0, run_id=4)
        assert _admit(conn) == {"admitted": 1, "benched": 0}
        assert RS.get_rule(conn, nxt)["n_fires_at_admission"] is not None
    finally:
        conn.close()


def test_unstamped_confirming_rows_do_not_count_toward_quota(tmp_path):
    # Migration band-exemptions stay confirming WITHOUT the admission stamp — they
    # must not block their family's slots.
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        legacy = _mint(conn, "f.z", 0.5, run_id=1)
        _admit(conn)
        RS.set_state(conn, legacy, RS.CONFIRMING, now_ns=5)
        with conn:                                                # strip the stamp
            conn.execute("UPDATE rules SET n_fires_at_admission=NULL WHERE rule_id=?",
                         (legacy,))
        for i, thr in enumerate((1.0, 2.0)):
            _seat(conn, "f.z", thr, run_id=i + 2)
        nxt = _mint(conn, "f.z", 3.0, run_id=5)
        assert _admit(conn) == {"admitted": 1, "benched": 0}      # 2 seats + legacy ≠ 3 seats
    finally:
        conn.close()


# -- bench semantics -------------------------------------------------------------

def test_benched_is_terminal_and_outside_the_walk(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        lo = _mint(conn, "f.z", 1.0, margin=0.01, run_id=1)
        _mint(conn, "f.z", 2.0, margin=0.09, run_id=1)
        _admit(conn)
        assert RS.get_rule(conn, lo)["state"] == RS.BENCHED
        import pytest
        with pytest.raises(ValueError):
            RS.set_state(conn, lo, RS.CONFIRMING, now_ns=30)
        eng = {(f"S{i % 5}", i * _W): {"f.z": 3.0} for i in range(1, 61)}
        C.run_confirmation(conn, eng, {kk: 0.02 for kk in eng}, m=30, z=2.0, now_ns=40)
        row = RS.get_rule(conn, lo)
        assert row["state"] == RS.BENCHED and row["fresh_count"] == 0   # never walked
    finally:
        conn.close()


def test_benched_rule_is_exit_donor_eligible(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        lo = _mint(conn, "f.z", 1.0, margin=0.01, run_id=1)
        hi = _mint(conn, "f.z", 2.0, margin=0.09, run_id=1)
        _admit(conn)
        with conn:
            conn.execute("UPDATE rules SET exit_def='{\"x\":1}' WHERE rule_id=?", (lo,))
        fam = RS.get_rule(conn, hi)["family_key"]
        donor = RS.family_exit_donor(conn, fam, exclude=hi)
        assert donor is not None and donor["rule_id"] == lo
    finally:
        conn.close()


# -- minted_run_id + the S9 anti-drift stamp -------------------------------------

def test_minted_run_id_stamped_on_insert_and_immutable_on_remint(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rid = _mint(conn, "f.z", 1.0, run_id=7)
        assert RS.get_rule(conn, rid)["minted_run_id"] == 7
        _mint(conn, "f.z", 1.0, run_id=9)                         # same identity re-mint
        assert RS.get_rule(conn, rid)["minted_run_id"] == 7
    finally:
        conn.close()


def test_migrate_backfills_minted_run_id_from_timestamp_join(tmp_path):
    db = str(tmp_path / "d.sqlite")
    conn = RS.connect(db)
    run = RS.record_run(conn, started_at_ns=555, frontier_ns=0, score_horizon_min=60,
                        funnel=[], n_survivors=1)
    rid = _mint(conn, "f.z", 1.0, run_id=None, now=555)           # pre-column-era shape
    with conn:
        conn.execute("UPDATE rules SET minted_run_id=NULL WHERE rule_id=?", (rid,))
    conn.close()
    conn = RS.connect(db)                                         # _migrate backfill
    try:
        assert RS.get_rule(conn, rid)["minted_run_id"] == run
    finally:
        conn.close()


def test_expiry_opportunity_uses_admission_stamp_against_nfires_drift(tmp_path):
    # S9: a seat admitted at n_fires=40 later re-minted down to 12 must STAY
    # expirable — opportunity is judged against max(n_fires, stamp).
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        rid = _seat(conn, "f.z", 1.0, run_id=1, n_fires=40)
        with conn:
            conn.execute("UPDATE rules SET n_fires=12, fresh_count=2 WHERE rule_id=?",
                         (rid,))
        n = RS.expire_slow_resolvers(
            conn, now_ns=_H14, frontier_ns=_H14, m=30, hysteresis=5,
            history_ns=_H14, opportunity_floor=30, pace_factor=6)
        assert n == 1                                             # O from stamp(40) >= 30
        assert RS.get_rule(conn, rid)["state"] == RS.EXPIRED
    finally:
        conn.close()


# -- runner wiring ----------------------------------------------------------------

def test_runner_wires_admission_with_config_default():
    sig = inspect.signature(RN.run_discovery_pass)
    assert (sig.parameters["admit_max_per_family"].default
            is dcfg.ADMIT_MAX_PER_FAMILY)
    assert dcfg.ADMIT_MAX_PER_FAMILY == 3
