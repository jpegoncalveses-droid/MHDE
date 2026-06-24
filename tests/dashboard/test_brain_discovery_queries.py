"""Component 9 (§10) — read-only brain-discovery dashboard query layer.

The Streamlit app itself has no unit test (§12); this pins the data layer it renders:
defensive empties before any discovery run, and correct shapes once populated.
"""
from __future__ import annotations

from crypto.research.brain import registry as brain_registry
from crypto.research.brain.discovery import rulestore as RS
from crypto.research.brain.discovery import tradelog as TL
from crypto.research.brain.discovery.rules import Condition, make_rule
from crypto.research.brain.discovery.scoring import EntryResult
from dashboard.services import brain_discovery_queries as Q

_W = 60_000_000_000


def test_all_queries_are_defensive_on_missing_stores(tmp_path):
    missing = str(tmp_path / "nope.sqlite")
    assert Q.liveness(registry_path=missing) == []
    assert Q.runs(path=missing) == [] and Q.rules(path=missing) == []
    assert Q.trades(path=missing) == [] and Q.aggregates(path=missing) == {}
    assert Q.equity("r1", path=missing) == []


def test_liveness_reads_registry_cursor(tmp_path):
    reg = str(tmp_path / "registry.sqlite")
    conn = brain_registry.connect(reg)
    brain_registry.advance(conn, "trades", new_recv_ts_ns=12345, now_ns=999)
    conn.close()
    rows = Q.liveness(registry_path=reg)
    assert rows and rows[0]["reader"] == "trades" and rows[0]["last_recv_ts_ns"] == 12345


def test_discovery_queries_once_populated(tmp_path):
    db = str(tmp_path / "discovery.sqlite")
    conn = RS.connect(db)
    TL.ensure_schema(conn)
    rule = make_rule([Condition("sig.raw", ">", 0.5)])
    res = EntryResult(rule=rule, edge=0.02, n_fires=40, depth=1, null_bar=0.005, margin=0.015)
    rid = RS.upsert_entry(conn, res, score_horizon_min=60, breadth=7,
                          discovery_window_ns=0, now_ns=1)
    RS.set_state(conn, rid, RS.CONFIRMING, now_ns=2)
    RS.set_state(conn, rid, RS.PROMOTED, now_ns=3)
    RS.record_run(conn, started_at_ns=1, frontier_ns=100, score_horizon_min=60,
                  funnel=[{"depth": 1, "n_candidates": 9000, "n_passed": 2}], n_survivors=2)
    TL.record_trades(conn, [{"rule_id": rid, "symbol": "BTCUSDT", "entry_window_ns": _W,
                             "exit_window_ns": 2 * _W, "holding_windows": 1, "exit_reason": "target",
                             "rt_return": 0.02, "rt_vol_normalized": 2.0, "coin_vol": 0.01}],
                     exit_def="{}", now_ns=4)
    conn.close()

    assert Q.runs(path=db)[0]["n_survivors"] == 2
    assert [r["rule_id"] for r in Q.rules("promoted", path=db)] == [rid]
    assert Q.trades(rid, path=db)[0]["symbol"] == "BTCUSDT"
    assert Q.aggregates(path=db)[rid]["n_trades"] == 1
    assert Q.equity(rid, path=db) == [(_W, 2.0)]
