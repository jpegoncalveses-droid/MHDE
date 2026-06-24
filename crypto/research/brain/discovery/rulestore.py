"""§8.1 + §8.3 — the rule store + state machine (the primary operator-inspectable output).

A MUTABLE store (rules change state and accumulate fresh-instance counts), so it is
SQLite-WAL — exactly the substrate registry's choice and for the same reason: readers
(the dashboard) never block the lone batch writer, sidestepping DuckDB single-writer
contention. A SEPARATE db file (``discovery.sqlite``, not the registry) keeps the evolving
discovery layer's writer isolated from the tick-loop's registry writer.

Each tracked rule records (§8.1): its full entry definition (the conjunction — conditions
carry their normalization + window in the engineered feature id, e.g. ``...z1440``), the
discovered exit (Stage 2, nullable until then), the in-sample risk-adjusted edge, the
permutation-null result (bar at its depth + the margin, +ve beat / -ve missed-or-decayed),
the forward-confirmation status (fresh post-discovery instance count + current forward
edge), its state, and depth / frequency / breadth.

STATE MACHINE (§8.3): ``discovered`` -> ``confirming`` -> ``promoted`` | ``rejected``.
Rejected is terminal (fails the null, fails forward confirmation, or decays below the bar).
Transitions are validated; an illegal jump raises.

SCOPING NOTE (for the reviewer): the store tracks NULL-SURVIVORS (the meaningful set the
operator inspects and that flow through the states). The per-run FUNNEL — candidates
generated / scorable / passed per depth, including how many MISSED — lands in
``discovery_runs`` (the dashboard's "discovery activity" level), not as millions of
per-missed-candidate rows. This honours §8.1's intent (every tracked rule carries its null
margin; the funnel incl. misses is visible) without an unbounded store.

Promotion writes ``promoted`` and the trade log begins (component 7); it does NOT wire to
any executor — the brain<->executor loop stays open by design (§8.3).
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional, Sequence

from crypto.research.brain.discovery.rules import Condition, make_rule
from crypto.research.brain.discovery.scoring import EntryResult

DISCOVERED = "discovered"
CONFIRMING = "confirming"
PROMOTED = "promoted"
REJECTED = "rejected"

#: Allowed forward transitions (a same-state set via set_state is a no-op; anything else
#: not listed raises). Rejected is terminal.
_TRANSITIONS = {
    DISCOVERED: {CONFIRMING, REJECTED},
    CONFIRMING: {PROMOTED, REJECTED},
    PROMOTED: {REJECTED},
    REJECTED: set(),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    rule_id             TEXT    PRIMARY KEY,
    entry_def           TEXT    NOT NULL,
    exit_def            TEXT,
    depth               INTEGER NOT NULL,
    score_horizon_min   INTEGER NOT NULL,
    insample_edge       REAL    NOT NULL,
    null_bar            REAL    NOT NULL,
    null_margin         REAL    NOT NULL,
    n_fires             INTEGER NOT NULL,
    breadth             INTEGER NOT NULL,
    state               TEXT    NOT NULL,
    fresh_count         INTEGER NOT NULL DEFAULT 0,
    forward_edge        REAL,
    reject_reason       TEXT,
    discovery_window_ns INTEGER NOT NULL,
    discovered_at_ns    INTEGER NOT NULL,
    updated_at_ns       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_runs (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at_ns     INTEGER NOT NULL,
    frontier_ns       INTEGER,
    score_horizon_min INTEGER NOT NULL,
    funnel            TEXT    NOT NULL,
    n_survivors       INTEGER NOT NULL,
    n_promoted        INTEGER NOT NULL DEFAULT 0,
    notes             TEXT
);
"""


def connect(path: str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the discovery store. Writable connections enable WAL + create the schema;
    read-only connections (the dashboard) open the existing file ``mode=ro``."""
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# -- entry (de)serialisation ----------------------------------------------------

def serialize_rule(rule) -> str:
    return json.dumps([{"feature": c.feature, "op": c.op, "threshold": c.threshold}
                       for c in rule.conditions])


def deserialize_rule(entry_def: str):
    return make_rule([Condition(d["feature"], d["op"], float(d["threshold"]))
                      for d in json.loads(entry_def)])


# -- writes ---------------------------------------------------------------------

def upsert_entry(conn: sqlite3.Connection, result: EntryResult, *, score_horizon_min: int,
                 breadth: int, discovery_window_ns: int, now_ns: int) -> str:
    """Insert a null-survivor as ``discovered`` (first sighting) or update its in-sample
    metrics in place (idempotent on ``rule_id``). State / fresh-count / exit are NOT
    touched on update — those advance only through the state-machine helpers."""
    rule_id = result.rule.canonical_id
    entry_def = serialize_rule(result.rule)
    with conn:
        exists = conn.execute("SELECT 1 FROM rules WHERE rule_id = ?", (rule_id,)).fetchone()
        if exists:
            conn.execute(
                "UPDATE rules SET insample_edge=?, null_bar=?, null_margin=?, n_fires=?, "
                "breadth=?, depth=?, score_horizon_min=?, updated_at_ns=? WHERE rule_id=?",
                (result.edge, result.null_bar, result.margin, result.n_fires, breadth,
                 result.depth, score_horizon_min, now_ns, rule_id))
        else:
            conn.execute(
                "INSERT INTO rules (rule_id, entry_def, exit_def, depth, score_horizon_min, "
                "insample_edge, null_bar, null_margin, n_fires, breadth, state, fresh_count, "
                "forward_edge, reject_reason, discovery_window_ns, discovered_at_ns, updated_at_ns) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?, ?)",
                (rule_id, entry_def, result.depth, score_horizon_min, result.edge,
                 result.null_bar, result.margin, result.n_fires, breadth, DISCOVERED,
                 discovery_window_ns, now_ns, now_ns))
    return rule_id


def set_state(conn: sqlite3.Connection, rule_id: str, state: str, *,
              reject_reason: Optional[str] = None, now_ns: int) -> None:
    """Advance a rule's state. Raises ``ValueError`` on an illegal transition."""
    row = conn.execute("SELECT state FROM rules WHERE rule_id = ?", (rule_id,)).fetchone()
    if row is None:
        raise KeyError(rule_id)
    old = row["state"]
    if state != old and state not in _TRANSITIONS[old]:
        raise ValueError(f"illegal transition {old} -> {state} for {rule_id}")
    with conn:
        conn.execute("UPDATE rules SET state=?, reject_reason=?, updated_at_ns=? WHERE rule_id=?",
                     (state, reject_reason, now_ns, rule_id))


def update_forward(conn: sqlite3.Connection, rule_id: str, *, fresh_count: int,
                   forward_edge: Optional[float], now_ns: int) -> None:
    """Record forward-confirmation progress (fresh post-discovery instance count + edge)."""
    with conn:
        conn.execute("UPDATE rules SET fresh_count=?, forward_edge=?, updated_at_ns=? "
                     "WHERE rule_id=?", (fresh_count, forward_edge, now_ns, rule_id))


def set_exit(conn: sqlite3.Connection, rule_id: str, exit_def: str, now_ns: int) -> None:
    with conn:
        conn.execute("UPDATE rules SET exit_def=?, updated_at_ns=? WHERE rule_id=?",
                     (exit_def, now_ns, rule_id))


def record_run(conn: sqlite3.Connection, *, started_at_ns: int, frontier_ns: Optional[int],
               score_horizon_min: int, funnel, n_survivors: int, n_promoted: int = 0,
               notes: Optional[str] = None) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO discovery_runs (started_at_ns, frontier_ns, score_horizon_min, "
            "funnel, n_survivors, n_promoted, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (started_at_ns, frontier_ns, score_horizon_min, json.dumps(funnel),
             n_survivors, n_promoted, notes))
        return int(cur.lastrowid)


# -- reads ----------------------------------------------------------------------

def get_rule(conn: sqlite3.Connection, rule_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM rules WHERE rule_id = ?", (rule_id,)).fetchone()
    return dict(row) if row is not None else None


def list_rules(conn: sqlite3.Connection, state: Optional[str] = None) -> list[dict]:
    if state is None:
        rows = conn.execute("SELECT * FROM rules ORDER BY insample_edge DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM rules WHERE state = ? ORDER BY insample_edge DESC",
                            (state,)).fetchall()
    return [dict(r) for r in rows]


def list_runs(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute("SELECT * FROM discovery_runs ORDER BY run_id DESC LIMIT ?",
                        (limit,)).fetchall()
    return [dict(r) for r in rows]
