"""KI-166 un-expire one-off: stall artifacts return to CONFIRMING, genuine expiries stand.

All KI-166-era expirations happened while the frontier sat frozen at F_frozen —
the script recomputes the EVIDENCE-clock predicate at that frontier for every
EXPIRED row and flips only the rows the honest clock would NOT have expired.
Dry-run by default; --apply writes; idempotent (a second apply flips 0)."""
from __future__ import annotations

from crypto.research.brain.discovery import config as dcfg
from crypto.research.brain.discovery import rulestore as RS
from crypto.research.brain.discovery.rules import Condition, make_rule
from crypto.research.brain.discovery.scoring import EntryResult
from scripts.unexpire_ki166_stall_artifacts import unexpire

_H14 = dcfg.DISCOVERY_HISTORY_NS
_F_FROZEN = 5 * _H14


def _seed_expired(conn, feature, *, disc_w, n_fires, fresh):
    rule = make_rule([Condition(feature, ">", 1.0)])
    res = EntryResult(rule=rule, edge=0.02, n_fires=n_fires, depth=1,
                      null_bar=0.005, margin=0.015)
    rid = RS.upsert_entry(conn, res, score_horizon_min=60, breadth=5,
                          discovery_window_ns=disc_w, now_ns=1)
    RS.set_state(conn, rid, RS.CONFIRMING, now_ns=1)
    with conn:
        conn.execute("UPDATE rules SET state=?, fresh_count=? WHERE rule_id=?",
                     (RS.EXPIRED, fresh, rid))
    return rid


def _fixture(tmp_path):
    conn = RS.connect(str(tmp_path / "d.sqlite"))
    # ARTIFACT: minted at the frozen frontier — zero evidence opportunity ever.
    artifact = _seed_expired(conn, "a.x", disc_w=_F_FROZEN, n_fires=120, fresh=0)
    # GENUINE: a full labeled window existed before the freeze; rate collapsed.
    genuine = _seed_expired(conn, "b.y", disc_w=_F_FROZEN - _H14, n_fires=120, fresh=4)
    # BYSTANDER: a rejected rule must never be touched.
    rule = make_rule([Condition("c.z", ">", 1.0)])
    res = EntryResult(rule=rule, edge=0.02, n_fires=50, depth=1,
                      null_bar=0.005, margin=0.015)
    bystander = RS.upsert_entry(conn, res, score_horizon_min=60, breadth=5,
                                discovery_window_ns=0, now_ns=1)
    RS.set_state(conn, bystander, RS.CONFIRMING, now_ns=1)
    RS.set_state(conn, bystander, RS.REJECTED, reject_reason="x", now_ns=2)
    return conn, artifact, genuine, bystander


def test_dry_run_reports_but_writes_nothing(tmp_path):
    conn, artifact, genuine, _ = _fixture(tmp_path)
    try:
        report = unexpire(conn, frozen_frontier_ns=_F_FROZEN, m=30, hysteresis=5,
                          history_ns=_H14, opportunity_floor=30, pace_factor=6,
                          now_ns=99, apply=False)
        assert report["artifacts"] == 1 and report["genuine"] == 1
        assert RS.get_rule(conn, artifact)["state"] == RS.EXPIRED   # unchanged
    finally:
        conn.close()


def test_apply_flips_only_artifacts_and_is_idempotent(tmp_path):
    conn, artifact, genuine, bystander = _fixture(tmp_path)
    try:
        report = unexpire(conn, frozen_frontier_ns=_F_FROZEN, m=30, hysteresis=5,
                          history_ns=_H14, opportunity_floor=30, pace_factor=6,
                          now_ns=99, apply=True)
        assert report["flipped"] == 1
        assert RS.get_rule(conn, artifact)["state"] == RS.CONFIRMING
        assert RS.get_rule(conn, genuine)["state"] == RS.EXPIRED
        assert RS.get_rule(conn, bystander)["state"] == RS.REJECTED
        again = unexpire(conn, frozen_frontier_ns=_F_FROZEN, m=30, hysteresis=5,
                         history_ns=_H14, opportunity_floor=30, pace_factor=6,
                         now_ns=100, apply=True)
        assert again["flipped"] == 0
    finally:
        conn.close()
