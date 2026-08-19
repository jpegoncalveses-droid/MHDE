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

STATE MACHINE (§8.3): ``discovered`` -> ``confirming`` -> ``promoted`` | ``rejected``,
plus the evidence-shrink DEMOTION edge ``promoted`` -> ``confirming`` (2026-08-14: a
promoted rule whose fresh recount falls below CONFIRM_M - CONFIRM_DEMOTE_HYSTERESIS
returns to confirming and re-promotes through the full gauntlet; the band [M-H, M)
holds promoted — ADR-041). Rejected is terminal
(fails the null, fails forward confirmation, or decays below the bar).
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
#: Terminal-but-RETAINED (pace-collapse expiry, ADR-042): a never-promoted confirming
#: rule whose forward firing pace has collapsed >= EXPIRE_PACE_FACTOR x vs its own
#: in-sample rate (window-capped opportunity — time-free once mature). The ROW persists —
#: family/cohort provenance survives for the graduation bar.
EXPIRED = "expired"

#: Allowed forward transitions (a same-state set via set_state is a no-op; anything else
#: not listed raises). Rejected is terminal.
_TRANSITIONS = {
    DISCOVERED: {CONFIRMING, REJECTED},
    CONFIRMING: {PROMOTED, REJECTED, EXPIRED},
    # PROMOTED -> CONFIRMING is the evidence-shrink DEMOTION (2026-08-14): fresh
    # recounts are non-monotonic, and a promoted rule whose count falls below
    # CONFIRM_M - CONFIRM_DEMOTE_HYSTERESIS returns to confirming (it did not FAIL;
    # its sample fell under the floor; the jitter band [M-H, M) holds — ADR-041).
    # It re-promotes through the full gauntlet when the count returns.
    PROMOTED: {REJECTED, CONFIRMING},
    REJECTED: set(),
    EXPIRED: set(),
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
    updated_at_ns       INTEGER NOT NULL,
    family_key          TEXT,
    exit_inherited_from TEXT,
    promoted_at_ns      INTEGER
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
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive schema migration (idempotent). family_key/exit_inherited_from/
    promoted_at_ns arrived 2026-08-14 (family hygiene); family_key is backfilled from
    entry_def for pre-migration rows."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(rules)").fetchall()}
    if "family_key" not in cols:
        conn.execute("ALTER TABLE rules ADD COLUMN family_key TEXT")
    if "exit_inherited_from" not in cols:
        conn.execute("ALTER TABLE rules ADD COLUMN exit_inherited_from TEXT")
    if "promoted_at_ns" not in cols:
        conn.execute("ALTER TABLE rules ADD COLUMN promoted_at_ns INTEGER")
    # promoted_at_ns backfill for rows promoted BEFORE the marker existed (operator-
    # approved 2026-08-14): their promotion time is best-approximated by updated_at_ns at
    # migration time. Only the STANDING promoted set is recoverable from the DB — rules
    # demoted/rejected before this migration keep NULL (their promoted-ever history lives
    # in SESSION_LOG). Idempotent: new promotions set the marker at set_state.
    conn.execute("UPDATE rules SET promoted_at_ns = updated_at_ns "
                 "WHERE state = 'promoted' AND promoted_at_ns IS NULL")
    for row in conn.execute(
            "SELECT rule_id, entry_def FROM rules WHERE family_key IS NULL").fetchall():
        conn.execute("UPDATE rules SET family_key=? WHERE rule_id=?",
                     (family_key_from_entry_def(row["entry_def"]), row["rule_id"]))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rules_family_key ON rules(family_key)")


# -- entry (de)serialisation ----------------------------------------------------

def family_key(rule) -> str:
    """The rule's FAMILY: its sorted (feature, op) composition, thresholds excluded.

    Rule IDENTITY (canonical_id) is unstable run-to-run — thresholds shift on the moving
    quantile grid, so cohorts re-mint ~1,000 near-duplicate identities per pass with
    ~zero id overlap. COMPOSITION is the stable notion the graduation bar evaluates.
    Ops are part of the family (direction is identity); features are unique within a
    rule (the search extends by distinct-feature atoms only)."""
    return "|".join(sorted(f"{c.feature}{c.op}" for c in rule.conditions))


def family_key_from_entry_def(entry_def: str) -> str:
    return "|".join(sorted(f"{d['feature']}{d['op']}" for d in json.loads(entry_def)))


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
                "forward_edge, reject_reason, discovery_window_ns, discovered_at_ns, "
                "updated_at_ns, family_key) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?, ?, ?)",
                (rule_id, entry_def, result.depth, score_horizon_min, result.edge,
                 result.null_bar, result.margin, result.n_fires, breadth, DISCOVERED,
                 discovery_window_ns, now_ns, now_ns, family_key(result.rule)))
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
        if state == PROMOTED:
            # promoted-EVER marker, set once: demotions/decays must not erase a family's
            # promotion history from the bar's cohort tracking.
            conn.execute("UPDATE rules SET promoted_at_ns=? "
                         "WHERE rule_id=? AND promoted_at_ns IS NULL", (now_ns, rule_id))


def update_forward(conn: sqlite3.Connection, rule_id: str, *, fresh_count: int,
                   forward_edge: Optional[float], now_ns: int) -> None:
    """Record forward-confirmation progress (fresh post-discovery instance count + edge)."""
    with conn:
        conn.execute("UPDATE rules SET fresh_count=?, forward_edge=?, updated_at_ns=? "
                     "WHERE rule_id=?", (fresh_count, forward_edge, now_ns, rule_id))


def family_exit_donor(conn: sqlite3.Connection, fam_key: str, *,
                      exclude: str) -> Optional[dict]:
    """The family's deterministic exit donor: the lowest rule_id holding an exit among
    LIVE members (confirming/promoted — a rejected rule's exit must not seed its family,
    review F4), or None if the family has no live discovered exit yet."""
    row = conn.execute(
        "SELECT rule_id, exit_def, exit_inherited_from FROM rules "
        "WHERE family_key=? AND exit_def IS NOT NULL AND rule_id != ? "
        "AND state IN (?, ?) ORDER BY rule_id LIMIT 1",
        (fam_key, exclude, CONFIRMING, PROMOTED)).fetchone()
    return dict(row) if row is not None else None


def inherit_exit(conn: sqlite3.Connection, rule_id: str, donor: dict, *,
                 now_ns: int) -> None:
    """Copy the family donor's exit onto ``rule_id`` with ROOT provenance — if the donor
    itself inherited, the recorded source is the ORIGINAL discovering rule, so provenance
    never forms unrecoverable chains (review F6). A new family member skips the exit
    search entirely (the capacity fix: exit work is per-FAMILY, not per-identity; the
    exit-null trade this makes is ADR-040)."""
    root = donor.get("exit_inherited_from") or donor["rule_id"]
    with conn:
        conn.execute("UPDATE rules SET exit_def=?, exit_inherited_from=?, updated_at_ns=? "
                     "WHERE rule_id=?", (donor["exit_def"], root, now_ns, rule_id))


def expire_slow_resolvers(conn: sqlite3.Connection, *, now_ns: int, m: int,
                          hysteresis: int, history_ns: int, opportunity_floor: int,
                          pace_factor: int) -> int:
    """Pace-collapse expiry (ADR-042, redesigned after the BLOCK review).

    Opportunity is WINDOW-CAPPED: O = n_fires x min(elapsed, history)/history — expected
    fresh instances at the rule's in-sample rate over a window NO LONGER than the rolling
    window fresh_count itself measures. For mature rules (elapsed >= history) the
    criterion is therefore the pure forward/in-sample RATE-COLLAPSE ratio
    (fresh x pace_factor < n_fires) — completely time-free; calendar enters only as the
    pro-rating of a rule's first 14 days. A rate-stable rule (fresh ~ n_fires) is held
    forever. (The un-capped form degenerated to a rate-scaled wall clock ~ pace x 14d —
    the shape the PR #91 review falsified.)

    Expire CONFIRMING iff ALL of:
      promoted_at_ns IS NULL           — once-promoted rules NEVER pace-expire: demotee
                                         protection is structural (the operator's
                                         non-negotiable), not arithmetic; they exit via
                                         re-promotion or decay-rejection only.
      O >= opportunity_floor           — never judged before M's worth of expected
                                         evidence (window-capped).
      fresh_count < m - hysteresis     — the resolving band [M-H, M) is exempt.
      fresh_count x pace_factor < O    — resolution implausible at observed pace.

    REAL casts (n_fires x ns overflows SQLite INTEGER). Bulk UPDATE with the source state
    pinned in WHERE — the CONFIRMING -> EXPIRED edge in _TRANSITIONS is honored by
    construction (this is the sole producer of EXPIRED). Terminal but retained."""
    with conn:
        cur = conn.execute(
            "UPDATE rules SET state=?, updated_at_ns=? WHERE state=? "
            "AND promoted_at_ns IS NULL "
            "AND fresh_count < ? "
            "AND CAST(n_fires AS REAL) * MIN(? - discovered_at_ns, ?) >= CAST(? AS REAL) * ? "
            "AND CAST(fresh_count AS REAL) * ? * ? < CAST(n_fires AS REAL) * MIN(? - discovered_at_ns, ?)",
            (EXPIRED, now_ns, CONFIRMING, m - hysteresis,
             now_ns, history_ns, opportunity_floor, history_ns,
             pace_factor, history_ns, now_ns, history_ns))
    return cur.rowcount


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
