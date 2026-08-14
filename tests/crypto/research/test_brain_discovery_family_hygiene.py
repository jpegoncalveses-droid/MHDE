"""Family-aware rule-store hygiene (the capacity fix; serves the family-level bar).

Basis: rule IDENTITY is unstable run-to-run (~zero canonical-id overlap across cohorts —
thresholds shift on the moving quantile grid) while rule COMPOSITION is stable, so
~1,000 near-duplicate identities re-mint per pass, each spawning stage-2 exit work; the
pass lengthens toward cadence saturation (measured 3h40m -> 4h45m in two days).

(a) FAMILY KEY: sorted "feature<op>" composition — thresholds excluded (the unstable
    part), ops included (direction is identity), features unique within a rule (the
    search extends by distinct-feature atoms). Backfilled by migration; set on upsert.
(b) EXIT INHERITANCE: a new exit-less confirming/promoted rule whose family already
    holds a discovered exit INHERITS it (deterministic donor: lowest rule_id) instead of
    re-running the exit search — provenance in exit_inherited_from.
(c) STALE-CONFIRMING EXPIRY: confirming + fresh_count == 0 + age >= 48h (N=8 passes at
    the 6h cadence) -> EXPIRED — terminal but RETAINED, so family/cohort provenance
    survives for the bar's persistence counting.
(d) promoted_at_ns: promoted-EVER marker (first promotion), so demotions (PR #90) cannot
    erase a family's promotion history from cohort tracking.
"""
from __future__ import annotations

import pytest

from crypto.research.brain.discovery import config as dcfg
from crypto.research.brain.discovery import confirmation as C
from crypto.research.brain.discovery import rulestore as RS
from crypto.research.brain.discovery.rules import Condition, make_rule
from crypto.research.brain.discovery.scoring import EntryResult

_W = 60_000_000_000


def _mk(conds):
    return make_rule([Condition(*c) for c in conds])


def _seed(conn, conds, *, disc_w=4 * _W, now=1):
    rule = _mk(conds)
    res = EntryResult(rule=rule, edge=0.02, n_fires=120, depth=len(conds),
                      null_bar=0.005, margin=0.015)
    rid = RS.upsert_entry(conn, res, score_horizon_min=60, breadth=5,
                          discovery_window_ns=disc_w, now_ns=now)
    return rule, rid


# -- (a) family key -----------------------------------------------------------

def test_family_key_ignores_thresholds_and_order_keeps_ops():
    a = RS.family_key(_mk([("f.z", ">", 1.40), ("g.raw", "<", 1.0)]))
    b = RS.family_key(_mk([("g.raw", "<", 0.7), ("f.z", ">", 0.9)]))    # same composition
    c = RS.family_key(_mk([("f.z", "<", 1.40), ("g.raw", "<", 1.0)]))   # op flipped
    d = RS.family_key(_mk([("f.z", ">", 1.40)]))                        # subset
    assert a == b
    assert a != c and a != d


def test_upsert_sets_family_key_and_migration_backfills(tmp_path):
    db = str(tmp_path / "d.sqlite")
    conn = RS.connect(db)
    try:
        _, rid = _seed(conn, [("f.z", ">", 1.4), ("g.raw", "<", 1.0)])
        row = RS.get_rule(conn, rid)
        assert row["family_key"] == RS.family_key(_mk([("f.z", ">", 1.4), ("g.raw", "<", 1.0)]))
        # simulate a pre-migration row: null the key, reconnect -> backfilled
        with conn:
            conn.execute("UPDATE rules SET family_key=NULL WHERE rule_id=?", (rid,))
    finally:
        conn.close()
    conn = RS.connect(db)          # migration runs on connect
    try:
        assert RS.get_rule(conn, rid)["family_key"] is not None
    finally:
        conn.close()


def test_connect_is_idempotent_on_migrated_db(tmp_path):
    db = str(tmp_path / "d.sqlite")
    RS.connect(db).close()
    RS.connect(db).close()         # second connect must not raise on existing columns


# -- (b) exit inheritance -----------------------------------------------------

def test_family_exit_donor_is_deterministic_lowest_rule_id(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        _, r1 = _seed(conn, [("f.z", ">", 1.1)])
        _, r2 = _seed(conn, [("f.z", ">", 1.9)])       # same family, different threshold
        RS.set_exit(conn, r1, '{"e": 1}', 5)
        RS.set_exit(conn, r2, '{"e": 2}', 6)
        _, r3 = _seed(conn, [("f.z", ">", 1.5)])
        donor = RS.family_exit_donor(conn, RS.get_rule(conn, r3)["family_key"], exclude=r3)
        assert donor is not None
        assert donor["rule_id"] == min(r1, r2)          # deterministic: lowest rule_id
    finally:
        conn.close()


def test_inherit_exit_copies_def_and_records_provenance(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        _, r1 = _seed(conn, [("f.z", ">", 1.1)])
        RS.set_exit(conn, r1, '{"e": 1}', 5)
        _, r2 = _seed(conn, [("f.z", ">", 1.7)])
        donor = RS.family_exit_donor(conn, RS.get_rule(conn, r2)["family_key"], exclude=r2)
        RS.inherit_exit(conn, r2, donor, now_ns=9)
        row = RS.get_rule(conn, r2)
        assert row["exit_def"] == '{"e": 1}'
        assert row["exit_inherited_from"] == r1
        # a self-discovered exit keeps NULL provenance
        assert RS.get_rule(conn, r1)["exit_inherited_from"] is None
        # no donor outside the family
        _, r9 = _seed(conn, [("other.x", ">", 0.5)])
        assert RS.family_exit_donor(conn, RS.get_rule(conn, r9)["family_key"],
                                    exclude=r9) is None
    finally:
        conn.close()


# -- (c) stale-confirming expiry ----------------------------------------------

def test_expire_stale_confirming_terminal_but_retained(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        _, stale = _seed(conn, [("f.z", ">", 1.1)], now=0)
        _, fresh = _seed(conn, [("f.z", ">", 1.9)], now=0)
        _, young = _seed(conn, [("f.z", ">", 1.5)], now=0)
        for rid in (stale, fresh, young):
            RS.set_state(conn, rid, RS.CONFIRMING, now_ns=1)
        with conn:                                       # fresh evidence for one; young age for another
            conn.execute("UPDATE rules SET fresh_count=7 WHERE rule_id=?", (fresh,))
            conn.execute("UPDATE rules SET discovered_at_ns=? WHERE rule_id=?",
                         (dcfg.EXPIRE_STALE_CONFIRMING_NS, young))   # age < threshold at now
        now = dcfg.EXPIRE_STALE_CONFIRMING_NS + 10
        n = RS.expire_stale_confirming(conn, now_ns=now,
                                       stale_ns=dcfg.EXPIRE_STALE_CONFIRMING_NS)
        assert n == 1
        assert RS.get_rule(conn, stale)["state"] == RS.EXPIRED       # 0 fresh, old -> expired
        assert RS.get_rule(conn, fresh)["state"] == RS.CONFIRMING    # has fresh evidence
        assert RS.get_rule(conn, young)["state"] == RS.CONFIRMING    # too young
        # RETAINED: the row (cohort + family provenance) still exists
        assert RS.get_rule(conn, stale)["family_key"] is not None
        assert RS.get_rule(conn, stale)["discovery_window_ns"] is not None
    finally:
        conn.close()


def test_expired_rules_are_outside_the_confirmation_loop(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        _, rid = _seed(conn, [("f", ">", 0.5)], disc_w=0, now=0)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=1)
        RS.expire_stale_confirming(conn, now_ns=dcfg.EXPIRE_STALE_CONFIRMING_NS + 10,
                                   stale_ns=dcfg.EXPIRE_STALE_CONFIRMING_NS)
        assert RS.get_rule(conn, rid)["state"] == RS.EXPIRED
        eng = {(f"S{i % 7}", i * _W): {"f": 1.0} for i in range(1, 61)}
        C.run_confirmation(conn, eng, {k: 0.02 for k in eng}, m=30, z=2.0, now_ns=99)
        assert RS.get_rule(conn, rid)["state"] == RS.EXPIRED         # untouched
    finally:
        conn.close()


# -- (d) promoted-ever provenance ---------------------------------------------

def test_promoted_at_ns_set_once_on_first_promotion(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    try:
        _, rid = _seed(conn, [("f", ">", 0.5)])
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
        RS.set_state(conn, rid, RS.PROMOTED, now_ns=3)
        assert RS.get_rule(conn, rid)["promoted_at_ns"] == 3
        RS.set_state(conn, rid, RS.REJECTED, now_ns=5)               # decay later
        assert RS.get_rule(conn, rid)["promoted_at_ns"] == 3         # history preserved
    finally:
        conn.close()


# -- runner wiring: stage-2 inherits instead of re-searching; expiry in the pass ----------

def test_run_discovery_pass_inherits_family_exit_without_searching(tmp_path, monkeypatch):
    import random

    from crypto.research.brain.discovery import exits as X
    from crypto.research.brain.discovery import runner as RN
    from crypto.research.brain.discovery import tradelog as TL

    conn = RS.connect(str(tmp_path / "d.sqlite"))
    TL.ensure_schema(conn)
    try:
        # an established family member WITH a discovered exit...
        _, r1 = _seed(conn, [("a.raw", ">", 0.30)], disc_w=0, now=0)
        RS.set_state(conn, r1, RS.CONFIRMING, now_ns=1)
        RS.set_exit(conn, r1, X.exit_to_json(X.build_exit_grid((1.0,), (1.0,), (3,))[0]), 2)
        # ...and a new same-family identity (different threshold) lacking one
        _, r2 = _seed(conn, [("a.raw", ">", 0.55)], disc_w=0, now=3)
        RS.set_state(conn, r2, RS.CONFIRMING, now_ns=4)

        def _boom(*a, **k):
            raise AssertionError("exit search ran despite an available family donor")

        monkeypatch.setattr(RN.X, "discover_exit", _boom)
        monkeypatch.setattr(RN.S, "discover_entries", lambda *a, **k: ([], []))
        rng = random.Random(3)
        eng = {(f"S{i % 5}", i * _W): {"a.raw": rng.random()} for i in range(1, 200)}
        lifts = {k: 0.01 for k in eng}
        price_index = {s: {w: (100.0, 100.1, 99.9) for _, w in eng} for s in {k[0] for k in eng}}
        summary = RN.run_discovery_pass(
            conn, eng, lifts, price_index, RN.coin_volatilities(price_index),
            feature_ids=["a.raw"], frontier_ns=0, now_ns=10,
            n_bins=5, n_permutations=10, exit_grid=X.build_exit_grid((1.0,), (1.0,), (3,)))
        row = RS.get_rule(conn, r2)
        assert row["exit_def"] is not None
        assert row["exit_inherited_from"] == r1
        assert summary["exits_inherited"] == 1
        assert "expired" in summary
    finally:
        conn.close()
