"""Option 2 family-level admission (ADR-043) — the bound on the confirming set.

Confirmation is the scarce resource and the FAMILY is the bar's unit: each pass mints
~1,000 near-duplicate identities (95-99% re-mints of families already under
confirmation) into a walk whose cost is linear in the standing set, and ~71% of them
have no exit path at all (sub-floor: can neither promote within the window nor
pace-expire) — un-admitted, the set grows without bound (measured 2026-08-20;
design: data/processed/family_admission_design.md).

Admission gates the DISCOVERED->CONFIRMING advance:

  * RESOLVABILITY FLOOR — only variants with ``n_fires >= M`` compete: a sub-floor
    rule cannot reach M fresh within the rolling window even at a perfectly stable
    rate, and window-capped opportunity ``O <= n_fires < M`` means it can never
    expire either. Benching it loses nothing the bar could ever measure. The floor
    IS M (no new constant), so it tracks any retune.
  * ONE PER FAMILY PER PASS — same-pass variants are threshold jitter of one signal;
    a second adds redundancy, not information. Selection is IN-SAMPLE ONLY:
    ``null_margin`` DESC (edge above the rule's own permutation-null bar,
    normalizing family-specific noise floors), tiebreak ``rule_id``. No
    out-of-sample state ever enters the decision (invariant a).
  * QUOTA k CONCURRENT SEATS PER FAMILY — a seat is a confirming row carrying the
    ``n_fires_at_admission`` stamp. Demotees (promoted-ever marker) and unstamped
    legacy/band-exempt rows hold no seat and never block one (invariant d). Seats
    from different passes are cohort-distinct by construction.
  * INSERT-ONLY DOMAIN (S9) — only rows in state DISCOVERED compete, i.e. identities
    minted (not re-minted) by a pass: a re-mint takes the UPDATE path and keeps its
    state, so a benched/terminal identity can never re-enter through admission
    (re-seating a re-mint would count its own fit window as forward evidence).

Losers are BENCHED: terminal-but-retained in v1, never walked, zero per-pass cost,
donor-eligible (S7) — the family's countable trial denominator.
"""
from __future__ import annotations

from crypto.research.brain.discovery import rulestore as RS


def run_admission(conn, *, m: int, k: int, now_ns: int) -> dict:
    """Decide every DISCOVERED row: stamp the admitted (the walk then advances them
    to CONFIRMING exactly as before) and bench the rest. Returns the counts."""
    rows = conn.execute(
        "SELECT rule_id, family_key, n_fires, null_margin FROM rules "
        "WHERE state = ? ORDER BY family_key, null_margin DESC, rule_id",
        (RS.DISCOVERED,)).fetchall()
    if not rows:
        return {"admitted": 0, "benched": 0}
    # A seat = a confirming row carrying the admission stamp AND never promoted:
    # a demotee keeps its stamp (S9 anti-drift still applies to it) but rides the
    # lane — it must not shrink its family's quota while it re-proves (S4).
    seats = dict(conn.execute(
        "SELECT family_key, COUNT(*) FROM rules "
        "WHERE state = ? AND n_fires_at_admission IS NOT NULL "
        "AND promoted_at_ns IS NULL GROUP BY family_key",
        (RS.CONFIRMING,)).fetchall())
    admitted, benched = [], []
    families_admitted = set()
    for r in rows:
        fam = r["family_key"]
        if (r["n_fires"] >= m
                and fam not in families_admitted
                and seats.get(fam, 0) < k):
            admitted.append(r["rule_id"])
            families_admitted.add(fam)
        else:
            benched.append(r["rule_id"])
    with conn:
        for rid in admitted:
            conn.execute(
                "UPDATE rules SET n_fires_at_admission = n_fires, updated_at_ns = ? "
                "WHERE rule_id = ? AND state = ?", (now_ns, rid, RS.DISCOVERED))
        for rid in benched:
            conn.execute(
                "UPDATE rules SET state = ?, updated_at_ns = ? "
                "WHERE rule_id = ? AND state = ?",
                (RS.BENCHED, now_ns, rid, RS.DISCOVERED))
    return {"admitted": len(admitted), "benched": len(benched)}
