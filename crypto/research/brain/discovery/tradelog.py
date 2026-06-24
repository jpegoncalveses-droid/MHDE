"""§8.2 — simulated trade log for PROMOTED rules (NO real capital).

When a rule is promoted, its entry+exit round trips are simulated across the rule's firing
instances and recorded here for operator judgment: timestamp (entry/exit windows), coin,
the entry condition that fired (the rule id == the entry conjunction), the discovered exit
that closed it, the holding duration, and the risk-adjusted (vol-normalised) outcome. These
are SIMULATED round trips only — promotion does NOT wire to any executor; graduating a
promoted rule to the paper engine is a later, separate operator decision (§8.3).

Lives in the same discovery SQLite-WAL DB as the rule store; idempotent on
``(rule_id, symbol, entry_window_ns)`` so a re-run never double-logs a round trip.
``rule_aggregates`` / ``equity_points`` back the dashboard's aggregate-health level (§10.5).
"""
from __future__ import annotations

import sqlite3
from typing import Mapping, Optional, Sequence

from crypto.research.brain.discovery.exits import ExitRule, simulate_exit_detail

_SCHEMA = """
CREATE TABLE IF NOT EXISTS simulated_trades (
    trade_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id           TEXT    NOT NULL,
    symbol            TEXT    NOT NULL,
    entry_window_ns   INTEGER NOT NULL,
    exit_window_ns    INTEGER NOT NULL,
    holding_windows   INTEGER NOT NULL,
    exit_reason       TEXT    NOT NULL,
    exit_def          TEXT    NOT NULL,
    rt_return         REAL    NOT NULL,
    rt_vol_normalized REAL    NOT NULL,
    coin_vol          REAL,
    recorded_at_ns    INTEGER NOT NULL,
    UNIQUE (rule_id, symbol, entry_window_ns)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def build_trades(rule_id: str, exit_rule: ExitRule, instances: Sequence[tuple],
                 continuations: Mapping[tuple, Sequence], coin_vols: Mapping[tuple, float],
                 *, window_ns: int, now_ns: int) -> list[dict]:
    """Simulate the round trip for each firing instance the exit can resolve, returning
    trade-log records (resolved exit window + holding + reason + raw/vol-normalised return)."""
    out: list[dict] = []
    for k in instances:
        rt = simulate_exit_detail(exit_rule, continuations[k], coin_vols[k])
        if rt is None:
            continue
        symbol, entry_w = k
        out.append({
            "rule_id": rule_id, "symbol": symbol, "entry_window_ns": int(entry_w),
            "exit_window_ns": int(entry_w) + rt.exit_k * window_ns,
            "holding_windows": rt.exit_k, "exit_reason": rt.reason,
            "rt_return": rt.raw_return, "rt_vol_normalized": rt.vol_normalized,
            "coin_vol": coin_vols[k],
        })
    return out


def record_trades(conn: sqlite3.Connection, trades: Sequence[Mapping], *, exit_def: str,
                  now_ns: int) -> int:
    """INSERT OR IGNORE the round trips; returns the count newly inserted."""
    before = conn.total_changes
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO simulated_trades (rule_id, symbol, entry_window_ns, "
            "exit_window_ns, holding_windows, exit_reason, exit_def, rt_return, "
            "rt_vol_normalized, coin_vol, recorded_at_ns) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(t["rule_id"], t["symbol"], t["entry_window_ns"], t["exit_window_ns"],
              t["holding_windows"], t["exit_reason"], exit_def, t["rt_return"],
              t["rt_vol_normalized"], t.get("coin_vol"), now_ns) for t in trades])
    return conn.total_changes - before


def list_trades(conn: sqlite3.Connection, rule_id: Optional[str] = None,
                limit: int = 500) -> list[dict]:
    if rule_id is None:
        rows = conn.execute("SELECT * FROM simulated_trades ORDER BY entry_window_ns DESC "
                            "LIMIT ?", (limit,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM simulated_trades WHERE rule_id = ? "
                            "ORDER BY entry_window_ns DESC LIMIT ?", (rule_id, limit)).fetchall()
    return [dict(r) for r in rows]


def rule_aggregates(conn: sqlite3.Connection) -> dict:
    """Per promoted rule: n_trades, hit_rate (frac with positive return), and mean
    vol-normalised outcome — the aggregate-health summary (§10.5)."""
    rows = conn.execute(
        "SELECT rule_id, COUNT(*) AS n, "
        "       AVG(CASE WHEN rt_return > 0 THEN 1.0 ELSE 0.0 END) AS hit_rate, "
        "       AVG(rt_vol_normalized) AS mean_vn, SUM(rt_vol_normalized) AS sum_vn "
        "FROM simulated_trades GROUP BY rule_id").fetchall()
    return {r["rule_id"]: {"n_trades": r["n"], "hit_rate": r["hit_rate"],
                           "mean_vol_normalized": r["mean_vn"],
                           "sum_vol_normalized": r["sum_vn"]} for r in rows}


def equity_points(conn: sqlite3.Connection, rule_id: str) -> list[tuple]:
    """``[(entry_window_ns, cumulative_vol_normalized)]`` ordered by entry time — a simulated
    equity curve in vol-normalised units for the aggregate-health level."""
    rows = conn.execute("SELECT entry_window_ns, rt_vol_normalized FROM simulated_trades "
                        "WHERE rule_id = ? ORDER BY entry_window_ns ASC", (rule_id,)).fetchall()
    pts, cum = [], 0.0
    for r in rows:
        cum += r["rt_vol_normalized"]
        pts.append((int(r["entry_window_ns"]), cum))
    return pts
