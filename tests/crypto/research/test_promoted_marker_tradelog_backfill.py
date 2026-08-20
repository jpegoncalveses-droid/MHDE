"""F5 (KI-166): recover promoted-ever markers from the trade log — the D5 hole.

Stage-4 writes ``simulated_trades`` ONLY for PROMOTED rules, so a trade row proves a
rule was once promoted. Rules demoted/rejected BEFORE the 2026-08-14 marker migration
kept ``promoted_at_ns`` NULL (the PR #93 backfill could only see the standing promoted
set) — leaving HIDDEN DEMOTEES without the structural expiry protection D4 promises
(3 confirming rows found unprotected on 2026-08-20). The ``_migrate`` backfill closes
the hole with the earliest recorded trade as the first-promotion approximation, the
same first-evidence spirit as the existing ``updated_at_ns`` backfill.
"""
from __future__ import annotations

from crypto.research.brain.discovery import config as dcfg
from crypto.research.brain.discovery import rulestore as RS
from crypto.research.brain.discovery import tradelog as TL
from crypto.research.brain.discovery.rules import Condition, make_rule
from crypto.research.brain.discovery.scoring import EntryResult

_H14 = dcfg.DISCOVERY_HISTORY_NS


def _seed(conn, feature, *, disc_w=0, n_fires=120, now=1):
    rule = make_rule([Condition(feature, ">", 1.0)])
    res = EntryResult(rule=rule, edge=0.02, n_fires=n_fires, depth=1,
                      null_bar=0.005, margin=0.015)
    return RS.upsert_entry(conn, res, score_horizon_min=60, breadth=5,
                           discovery_window_ns=disc_w, now_ns=now)


def _trade_row(conn, rule_id, *, recorded_at_ns, entry_window_ns=10):
    TL.ensure_schema(conn)
    with conn:
        conn.execute(
            "INSERT INTO simulated_trades (rule_id, symbol, entry_window_ns, "
            "exit_window_ns, holding_windows, exit_reason, exit_def, rt_return, "
            "rt_vol_normalized, coin_vol, recorded_at_ns) "
            "VALUES (?, 'BTCUSDT', ?, 20, 1, 'target', '{}', 0.01, 0.5, 0.02, ?)",
            (rule_id, entry_window_ns, recorded_at_ns))


def _hidden_demotee(conn, feature, *, recorded_at_ns, disc_w=0, n_fires=120):
    """A pre-2026-08-14-shaped row: promoted once (trade log proves it), demoted,
    marker NULL — exactly the D5 hole."""
    rid = _seed(conn, feature, disc_w=disc_w, n_fires=n_fires)
    RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
    RS.set_state(conn, rid, RS.PROMOTED, now_ns=3)
    RS.set_state(conn, rid, RS.CONFIRMING, now_ns=4)          # demotion
    _trade_row(conn, rid, recorded_at_ns=recorded_at_ns)
    with conn:                                                # simulate the pre-marker era
        conn.execute("UPDATE rules SET promoted_at_ns=NULL WHERE rule_id=?", (rid,))
    return rid


def test_migrate_recovers_promoted_ever_from_trade_log(tmp_path):
    db = str(tmp_path / "d.sqlite")
    conn = RS.connect(db)
    rid = _hidden_demotee(conn, "a.x", recorded_at_ns=42)
    conn.close()

    conn = RS.connect(db)                                     # reconnect -> _migrate
    try:
        assert RS.get_rule(conn, rid)["promoted_at_ns"] == 42
    finally:
        conn.close()


def test_earliest_trade_wins_and_rules_without_trades_stay_null(tmp_path):
    db = str(tmp_path / "d.sqlite")
    conn = RS.connect(db)
    rid = _hidden_demotee(conn, "a.x", recorded_at_ns=99)
    _trade_row(conn, rid, recorded_at_ns=7, entry_window_ns=11)   # earlier trade exists
    plain = _seed(conn, "b.y")
    RS.set_state(conn, plain, RS.CONFIRMING, now_ns=2)
    conn.close()

    conn = RS.connect(db)
    try:
        assert RS.get_rule(conn, rid)["promoted_at_ns"] == 7
        assert RS.get_rule(conn, plain)["promoted_at_ns"] is None
    finally:
        conn.close()


def test_existing_marker_is_never_overwritten(tmp_path):
    # set-once (D5): a rule that already carries its marker keeps it even when an
    # earlier trade row exists.
    db = str(tmp_path / "d.sqlite")
    conn = RS.connect(db)
    rid = _seed(conn, "a.x")
    RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
    RS.set_state(conn, rid, RS.PROMOTED, now_ns=3)            # marker := 3
    _trade_row(conn, rid, recorded_at_ns=1)
    conn.close()

    conn = RS.connect(db)
    try:
        assert RS.get_rule(conn, rid)["promoted_at_ns"] == 3
    finally:
        conn.close()


def test_backfilled_demotee_gains_structural_expiry_protection(tmp_path):
    # THE D4 PIN, end to end: before the backfill a hidden demotee is expirable;
    # after _migrate it carries the marker and pace-expiry can never take it, while
    # an identical never-promoted twin expires in the same call.
    db = str(tmp_path / "d.sqlite")
    conn = RS.connect(db)
    f0 = 5 * _H14
    demotee = _hidden_demotee(conn, "a.x", recorded_at_ns=42, disc_w=f0, n_fires=120)
    twin = _seed(conn, "b.y", disc_w=f0, n_fires=120)
    RS.set_state(conn, twin, RS.CONFIRMING, now_ns=2)
    with conn:
        conn.execute("UPDATE rules SET fresh_count=4 WHERE rule_id IN (?, ?)",
                     (demotee, twin))
    conn.close()

    conn = RS.connect(db)                                     # backfill happens here
    try:
        n = RS.expire_slow_resolvers(
            conn, now_ns=f0 + _H14, frontier_ns=f0 + _H14, m=30, hysteresis=5,
            history_ns=_H14, opportunity_floor=30, pace_factor=6)
        assert n == 1
        assert RS.get_rule(conn, demotee)["state"] == RS.CONFIRMING   # protected
        assert RS.get_rule(conn, twin)["state"] == RS.EXPIRED
    finally:
        conn.close()


def test_backfill_is_idempotent_across_reconnects(tmp_path):
    db = str(tmp_path / "d.sqlite")
    conn = RS.connect(db)
    rid = _hidden_demotee(conn, "a.x", recorded_at_ns=42)
    conn.close()
    for _ in range(3):
        conn = RS.connect(db)
        assert RS.get_rule(conn, rid)["promoted_at_ns"] == 42
        conn.close()
