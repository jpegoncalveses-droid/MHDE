"""Read-only query layer for the brain discovery dashboard (§10).

Reads the brain's SQLite-WAL stores READ-ONLY: the substrate registry (liveness) and the
discovery DB (runs, rule store, trade log, aggregates). Every function is DEFENSIVE — a
missing DB or a not-yet-created table returns an empty result rather than raising, so the
page renders cleanly before the discovery batch has ever run. It NEVER mutates a store.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

from crypto.research.brain import config as brain_cfg
from crypto.research.brain.discovery import config as dcfg
from crypto.research.brain.discovery import rulestore as RS
from crypto.research.brain.discovery import tradelog as TL


def _ro(path: str) -> Optional[sqlite3.Connection]:
    if not path or not os.path.exists(path):
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# -- level 1: liveness (substrate registry) -----------------------------------

def liveness(registry_path: str = brain_cfg.BRAIN_REGISTRY_PATH) -> list[dict]:
    """Per-reader cursor position + last-advanced time (substrate health)."""
    conn = _ro(registry_path)
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT reader, last_recv_ts_ns, updated_at_ns "
                            "FROM reader_cursor ORDER BY reader").fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


# -- levels 2-5: discovery DB -------------------------------------------------

def _discovery(path: str) -> Optional[sqlite3.Connection]:
    return _ro(path)


def runs(path: str = dcfg.DISCOVERY_DB_PATH, limit: int = 50) -> list[dict]:
    """Level 2 — per-run funnel (candidates generated / passing the null / per depth)."""
    conn = _discovery(path)
    if conn is None:
        return []
    try:
        return RS.list_runs(conn, limit=limit)
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def rules(state: Optional[str] = None, path: str = dcfg.DISCOVERY_DB_PATH) -> list[dict]:
    """Level 3 — the rule store (filterable by state, sorted by in-sample edge)."""
    conn = _discovery(path)
    if conn is None:
        return []
    try:
        return RS.list_rules(conn, state=state)
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def trades(rule_id: Optional[str] = None, path: str = dcfg.DISCOVERY_DB_PATH,
           limit: int = 500) -> list[dict]:
    """Level 4 — simulated round trips for promoted rules."""
    conn = _discovery(path)
    if conn is None:
        return []
    try:
        return TL.list_trades(conn, rule_id=rule_id, limit=limit)
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def aggregates(path: str = dcfg.DISCOVERY_DB_PATH) -> dict:
    """Level 5 — per promoted rule: n_trades, hit_rate, mean vol-normalised outcome."""
    conn = _discovery(path)
    if conn is None:
        return {}
    try:
        return TL.rule_aggregates(conn)
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def equity(rule_id: str, path: str = dcfg.DISCOVERY_DB_PATH) -> list[tuple]:
    """Level 5 — a promoted rule's simulated equity curve (cumulative vol-normalised)."""
    conn = _discovery(path)
    if conn is None:
        return []
    try:
        return TL.equity_points(conn, rule_id)
    except sqlite3.Error:
        return []
    finally:
        conn.close()
