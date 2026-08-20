"""Option 2 migration one-off: seat/bench the standing confirming set.

Applies the SAME predicate the runner's admission uses (one code path in spirit): per
family, seat <=k cohort-distinct floor-passing variants by in-sample margin (stamping
n_fires_at_admission); resolving-band rows (fresh >= M-H) and promoted-ever demotees
stay confirming UNSTAMPED (exempt — they hold no seat); everything else confirming ->
BENCHED via the sanctioned bulk UPDATE (CONFIRMING->BENCHED is not a machine edge; the
one-off carries PR #93/un-expire authority). Dry-run default; idempotent."""
from __future__ import annotations

from crypto.research.brain.discovery import config as dcfg
from crypto.research.brain.discovery import rulestore as RS
from crypto.research.brain.discovery.rules import Condition, make_rule
from crypto.research.brain.discovery.scoring import EntryResult
from scripts.migrate_option2_admission import migrate

_H14 = dcfg.DISCOVERY_HISTORY_NS


def _row(conn, feature, thr, *, run_started, n_fires=120, margin=0.015, fresh=4,
         promoted_ever=False):
    rule = make_rule([Condition(feature, ">", thr)])
    res = EntryResult(rule=rule, edge=0.02, n_fires=n_fires, depth=1,
                      null_bar=0.005, margin=margin)
    rid = RS.upsert_entry(conn, res, score_horizon_min=60, breadth=5,
                          discovery_window_ns=0, now_ns=run_started)
    RS.set_state(conn, rid, RS.CONFIRMING, now_ns=run_started + 1)
    with conn:
        conn.execute("UPDATE rules SET fresh_count=? WHERE rule_id=?", (fresh, rid))
        if promoted_ever:
            conn.execute("UPDATE rules SET promoted_at_ns=1 WHERE rule_id=?", (rid,))
    return rid


def _fixture(tmp_path):
    db = str(tmp_path / "d.sqlite")
    conn = RS.connect(db)
    for started in (100, 200, 300, 400, 500):
        RS.record_run(conn, started_at_ns=started, frontier_ns=0,
                      score_horizon_min=60, funnel=[], n_survivors=1)
    fam = {}
    # family f.z: 5 floor-passing variants across 4 cohorts (two share cohort 100),
    # margins ranked; 1 sub-floor; 1 band row; 1 demotee.
    fam["s1"] = _row(conn, "f.z", 1.0, run_started=100, margin=0.09)
    fam["s2"] = _row(conn, "f.z", 2.0, run_started=200, margin=0.08)
    fam["s3"] = _row(conn, "f.z", 3.0, run_started=300, margin=0.07)
    fam["dup_cohort"] = _row(conn, "f.z", 1.5, run_started=100, margin=0.085)
    fam["fourth"] = _row(conn, "f.z", 4.0, run_started=400, margin=0.06)
    fam["subfloor"] = _row(conn, "f.z", 5.0, run_started=500, n_fires=20, margin=0.99)
    fam["band"] = _row(conn, "f.z", 6.0, run_started=500, fresh=27)
    fam["demotee"] = _row(conn, "f.z", 7.0, run_started=500, promoted_ever=True)
    # family g.y: single floor-passing variant
    fam["solo"] = _row(conn, "g.y", 1.0, run_started=100)
    return db, conn, fam


def test_dry_run_reports_and_writes_nothing(tmp_path):
    db, conn, fam = _fixture(tmp_path)
    try:
        r = migrate(conn, m=30, hysteresis=5, k=3, now_ns=999, apply=False)
        assert r == {"kept_seats": 4, "exempt_band": 1, "exempt_demotees": 1,
                     "benched": 3, "confirming_after": 6, "flipped": 0}
        assert RS.get_rule(conn, fam["subfloor"])["state"] == RS.CONFIRMING  # untouched
    finally:
        conn.close()


def test_apply_seats_cohort_distinct_by_margin_and_benches_the_rest(tmp_path):
    db, conn, fam = _fixture(tmp_path)
    try:
        r = migrate(conn, m=30, hysteresis=5, k=3, now_ns=999, apply=True)
        assert r["flipped"] == 3
        # seats: s1 (cohort 100, best margin), s2 (200), s3 (300) — dup_cohort loses
        # its cohort to s1 despite outranking s2/s3? No: greedy by margin with
        # cohort-skip => s1, dup_cohort SKIPPED (cohort 100 used), s2, s3. fourth
        # (cohort 400) misses the k=3 cut.
        for rid in (fam["s1"], fam["s2"], fam["s3"], fam["solo"]):
            row = RS.get_rule(conn, rid)
            assert row["state"] == RS.CONFIRMING
            assert row["n_fires_at_admission"] is not None
        for rid in (fam["dup_cohort"], fam["fourth"], fam["subfloor"]):
            assert RS.get_rule(conn, rid)["state"] == RS.BENCHED
        for rid in (fam["band"], fam["demotee"]):
            row = RS.get_rule(conn, rid)
            assert row["state"] == RS.CONFIRMING
            assert row["n_fires_at_admission"] is None            # exempt, no seat
        again = migrate(conn, m=30, hysteresis=5, k=3, now_ns=1000, apply=True)
        assert again["flipped"] == 0                               # idempotent
    finally:
        conn.close()
