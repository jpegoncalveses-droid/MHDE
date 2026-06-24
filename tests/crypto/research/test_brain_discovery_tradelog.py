"""Component 7 (§8.2) — simulated trade log for promoted rules (NO real capital)."""
from __future__ import annotations

import pytest

from crypto.research.brain.discovery import rulestore as RS
from crypto.research.brain.discovery import tradelog as TL
from crypto.research.brain.discovery.exits import ExitRule, exit_to_json

_W = 60_000_000_000


def _bar(h, l, c):
    return {"rel_high": h, "rel_low": l, "rel_close": c}


def _conn(tmp_path):
    conn = RS.connect(str(tmp_path / "discovery.sqlite"))
    TL.ensure_schema(conn)
    return conn


def test_ensure_schema_creates_table(tmp_path):
    conn = _conn(tmp_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "simulated_trades" in tables
    finally:
        conn.close()


def test_build_trades_captures_holding_exit_and_outcome():
    er = ExitRule(favorable_vol_mult=2.0, adverse_vol_mult=None, time_cap_min=5)
    inst = [("BTCUSDT", 10 * _W)]
    conts = {("BTCUSDT", 10 * _W): [_bar(1.005, 0.999, 1.004), _bar(1.03, 1.0, 1.02)]}
    vols = {("BTCUSDT", 10 * _W): 0.01}
    trades = TL.build_trades("ruleX", er, inst, conts, vols, window_ns=_W, now_ns=1)
    t = trades[0]
    assert t["symbol"] == "BTCUSDT" and t["holding_windows"] == 2
    assert t["exit_window_ns"] == 10 * _W + 2 * _W and t["exit_reason"] == "target"
    assert t["rt_vol_normalized"] == pytest.approx(2.0)
    assert t["rt_return"] == pytest.approx(0.02)


def test_record_trades_idempotent_and_list(tmp_path):
    conn = _conn(tmp_path)
    try:
        trades = [{"rule_id": "r1", "symbol": "BTCUSDT", "entry_window_ns": _W,
                   "exit_window_ns": 3 * _W, "holding_windows": 2, "exit_reason": "target",
                   "rt_return": 0.02, "rt_vol_normalized": 2.0, "coin_vol": 0.01}]
        TL.record_trades(conn, trades, exit_def='{"time_cap_min":5}', now_ns=1)
        TL.record_trades(conn, trades, exit_def='{"time_cap_min":5}', now_ns=2)  # re-record
        rows = TL.list_trades(conn, rule_id="r1")
        assert len(rows) == 1                          # idempotent on (rule, symbol, entry)
        assert rows[0]["rt_vol_normalized"] == pytest.approx(2.0)
    finally:
        conn.close()


def test_rule_aggregates_and_equity(tmp_path):
    conn = _conn(tmp_path)
    try:
        trades = [
            {"rule_id": "r1", "symbol": "A", "entry_window_ns": 1 * _W, "exit_window_ns": 2 * _W,
             "holding_windows": 1, "exit_reason": "target", "rt_return": 0.01,
             "rt_vol_normalized": 1.0, "coin_vol": 0.01},
            {"rule_id": "r1", "symbol": "A", "entry_window_ns": 3 * _W, "exit_window_ns": 4 * _W,
             "holding_windows": 1, "exit_reason": "stop", "rt_return": -0.005,
             "rt_vol_normalized": -0.5, "coin_vol": 0.01},
            {"rule_id": "r1", "symbol": "B", "entry_window_ns": 2 * _W, "exit_window_ns": 5 * _W,
             "holding_windows": 3, "exit_reason": "target", "rt_return": 0.02,
             "rt_vol_normalized": 2.0, "coin_vol": 0.01},
        ]
        TL.record_trades(conn, trades, exit_def="{}", now_ns=1)
        agg = TL.rule_aggregates(conn)["r1"]
        assert agg["n_trades"] == 3
        assert agg["hit_rate"] == pytest.approx(2 / 3)              # 2 of 3 positive
        assert agg["mean_vol_normalized"] == pytest.approx((1.0 - 0.5 + 2.0) / 3)
        eq = TL.equity_points(conn, "r1")
        # ordered by entry time 1W(+1.0), 2W(+2.0), 3W(-0.5) -> cumulative
        assert [p[1] for p in eq] == pytest.approx([1.0, 3.0, 2.5])
    finally:
        conn.close()
