"""Component 4 (§8.1, §8.3) — rule store + state machine (SQLite-WAL)."""
from __future__ import annotations

import pytest

from crypto.research.brain.discovery import rulestore as RS
from crypto.research.brain.discovery.rules import Condition, make_rule
from crypto.research.brain.discovery.scoring import EntryResult


def _result(edge=0.02, depth=1, conds=(("f", ">", 1.0),)):
    rule = make_rule([Condition(*c) for c in conds])
    return EntryResult(rule=rule, edge=edge, n_fires=120, depth=depth,
                       null_bar=0.005, margin=edge - 0.005)


def _conn(tmp_path):
    return RS.connect(str(tmp_path / "discovery.sqlite"))


def test_connect_enables_wal_and_creates_tables(tmp_path):
    conn = _conn(tmp_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"rules", "discovery_runs"} <= tables
    finally:
        conn.close()


def test_upsert_inserts_discovered_and_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    try:
        rid = RS.upsert_entry(conn, _result(edge=0.02), score_horizon_min=60,
                              breadth=12, discovery_window_ns=1000, now_ns=1)
        row = RS.get_rule(conn, rid)
        assert row["state"] == RS.DISCOVERED and row["depth"] == 1
        assert row["insample_edge"] == pytest.approx(0.02) and row["breadth"] == 12
        assert row["fresh_count"] == 0 and row["exit_def"] is None
        # re-upsert (same rule, new in-sample edge) updates in place, no duplicate row
        RS.upsert_entry(conn, _result(edge=0.03), score_horizon_min=60, breadth=15,
                        discovery_window_ns=2000, now_ns=2)
        assert len(RS.list_rules(conn)) == 1
        assert RS.get_rule(conn, rid)["insample_edge"] == pytest.approx(0.03)
    finally:
        conn.close()


def test_state_machine_valid_and_invalid_transitions(tmp_path):
    conn = _conn(tmp_path)
    try:
        rid = RS.upsert_entry(conn, _result(), score_horizon_min=60, breadth=1,
                              discovery_window_ns=1, now_ns=1)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
        assert RS.get_rule(conn, rid)["state"] == RS.CONFIRMING
        RS.set_state(conn, rid, RS.PROMOTED, now_ns=3)
        assert RS.get_rule(conn, rid)["state"] == RS.PROMOTED
        RS.set_state(conn, rid, RS.REJECTED, reject_reason="decayed", now_ns=4)
        assert RS.get_rule(conn, rid)["reject_reason"] == "decayed"
        # rejected is terminal: any further transition is invalid
        with pytest.raises(ValueError):
            RS.set_state(conn, rid, RS.CONFIRMING, now_ns=5)
    finally:
        conn.close()


def test_invalid_skip_transition_rejected(tmp_path):
    conn = _conn(tmp_path)
    try:
        rid = RS.upsert_entry(conn, _result(), score_horizon_min=60, breadth=1,
                              discovery_window_ns=1, now_ns=1)
        with pytest.raises(ValueError):     # discovered -> promoted skips confirming
            RS.set_state(conn, rid, RS.PROMOTED, now_ns=2)
    finally:
        conn.close()


def test_update_forward_progress_and_list_by_state(tmp_path):
    conn = _conn(tmp_path)
    try:
        rid = RS.upsert_entry(conn, _result(), score_horizon_min=60, breadth=1,
                              discovery_window_ns=1, now_ns=1)
        RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
        RS.update_forward(conn, rid, fresh_count=18, forward_edge=0.014, now_ns=3)
        row = RS.get_rule(conn, rid)
        assert row["fresh_count"] == 18 and row["forward_edge"] == pytest.approx(0.014)
        assert [r["rule_id"] for r in RS.list_rules(conn, state=RS.CONFIRMING)] == [rid]
        assert RS.list_rules(conn, state=RS.PROMOTED) == []
    finally:
        conn.close()


def test_set_exit_and_serialize_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    try:
        res = _result(conds=(("a.z1440", ">", 1.5), ("b.raw", "<", 0.3)), depth=2)
        rid = RS.upsert_entry(conn, res, score_horizon_min=60, breadth=4,
                              discovery_window_ns=1, now_ns=1)
        # entry_def round-trips back to the SAME canonical rule
        assert RS.deserialize_rule(RS.get_rule(conn, rid)["entry_def"]) == res.rule
        RS.set_exit(conn, rid, '{"kind":"time_cap","cap_min":30}', now_ns=2)
        assert "time_cap" in RS.get_rule(conn, rid)["exit_def"]
    finally:
        conn.close()


def test_record_run_diagnostics(tmp_path):
    conn = _conn(tmp_path)
    try:
        run_id = RS.record_run(conn, started_at_ns=10, frontier_ns=999, score_horizon_min=60,
                               funnel=[{"depth": 1, "n_candidates": 5000, "n_passed": 3}],
                               n_survivors=3, notes="ok")
        runs = RS.list_runs(conn)
        assert len(runs) == 1 and runs[0]["run_id"] == run_id
        assert runs[0]["n_survivors"] == 3 and "5000" in runs[0]["funnel"]
    finally:
        conn.close()
