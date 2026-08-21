"""Option 2 admission migration (operator-approved one-off; ADR-043).

Applies the runner's admission predicate to the STANDING confirming set, one time:

  * exemptions stay confirming UNSTAMPED (they hold no seat): promoted-ever demotees
    (the lane) and resolving-band rows (fresh >= M-H — about to resolve; cutting
    them would waste accrued evidence);
  * per family: seat <= k COHORT-DISTINCT floor-passing (n_fires >= M) variants,
    best in-sample null_margin first (tiebreak rule_id), stamping
    ``n_fires_at_admission``;
  * everything else confirming -> BENCHED (terminal-but-retained; full provenance;
    donor-eligible). CONFIRMING->BENCHED is not a state-machine edge — this bulk
    UPDATE carries the same sanctioned one-off authority as the PR #93 backfill and
    the KI-166 un-expire.

Cohort = ``minted_run_id`` (backfilled by ``_migrate`` at connect; the dry-run also
COALESCEs through the timestamp join so a read-only run needs no write). Dry-run by
default; idempotent (a second apply changes nothing: seats are stamped confirming,
losers are benched and never re-enter).

Usage (from the repo root, AFTER the admission code is merged):
    venv/bin/python scripts/migrate_option2_admission.py            # dry-run
    venv/bin/python scripts/migrate_option2_admission.py --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from collections import defaultdict

DEFAULT_DB = "data/research/brain/discovery.sqlite"


def _plan(conn: sqlite3.Connection, *, m: int, hysteresis: int, k: int) -> dict:
    """Classify every confirming row: seats / band-exempt / demotee-exempt / bench."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(rules)").fetchall()}
    # A read-only DRY-RUN against a pre-ADR-043 DB (columns not yet migrated) falls
    # back to the pure timestamp join; --apply requires the migrated schema.
    cohort = ("COALESCE(minted_run_id, (SELECT d.run_id FROM discovery_runs d "
              "WHERE d.started_at_ns = rules.discovered_at_ns))"
              if "minted_run_id" in cols else
              "(SELECT d.run_id FROM discovery_runs d "
              "WHERE d.started_at_ns = rules.discovered_at_ns)")
    stamp = ("n_fires_at_admission" if "n_fires_at_admission" in cols
             else "NULL AS n_fires_at_admission")
    rows = conn.execute(
        f"SELECT rule_id, family_key, n_fires, null_margin, fresh_count, "
        f"promoted_at_ns, {stamp}, {cohort} AS cohort "
        "FROM rules WHERE state = 'confirming'").fetchall()
    exempt_demotees, exempt_band, families = [], [], defaultdict(list)
    for r in rows:
        if r["promoted_at_ns"] is not None:
            exempt_demotees.append(r["rule_id"])
        elif (r["fresh_count"] or 0) >= m - hysteresis:
            exempt_band.append(r["rule_id"])
        else:
            families[r["family_key"]].append(r)
    seats, benched = [], []
    for fam, members in families.items():
        elig = sorted((r for r in members if r["n_fires"] >= m),
                      key=lambda r: (-(r["null_margin"] or 0.0), r["rule_id"]))
        cohorts_used, n_seated, seated_ids = set(), 0, set()
        for r in elig:
            if n_seated >= k or r["cohort"] in cohorts_used:
                continue
            cohorts_used.add(r["cohort"])
            seated_ids.add(r["rule_id"])
            seats.append(r["rule_id"])
            n_seated += 1
        benched.extend(r["rule_id"] for r in members if r["rule_id"] not in seated_ids)
    return {"seats": seats, "band": exempt_band, "demotees": exempt_demotees,
            "bench": benched}


def migrate(conn: sqlite3.Connection, *, m: int, hysteresis: int, k: int,
            now_ns: int, apply: bool) -> dict:
    plan = _plan(conn, m=m, hysteresis=hysteresis, k=k)
    flipped = 0
    if apply:
        with conn:
            for rid in plan["seats"]:
                conn.execute(
                    "UPDATE rules SET n_fires_at_admission = n_fires, updated_at_ns=? "
                    "WHERE rule_id=? AND state='confirming' "
                    "AND n_fires_at_admission IS NULL", (now_ns, rid))
            for rid in plan["bench"]:
                cur = conn.execute(
                    "UPDATE rules SET state='benched', updated_at_ns=? "
                    "WHERE rule_id=? AND state='confirming'", (now_ns, rid))
                flipped += cur.rowcount
    return {"kept_seats": len(plan["seats"]), "exempt_band": len(plan["band"]),
            "exempt_demotees": len(plan["demotees"]), "benched": len(plan["bench"]),
            "confirming_after": (len(plan["seats"]) + len(plan["band"])
                                 + len(plan["demotees"])),
            "flipped": flipped}


def main() -> int:
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from crypto.research.brain.discovery import config as dcfg

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true",
                    help="write the seats/benchings (default: dry-run report only)")
    args = ap.parse_args()
    if args.apply:
        conn = sqlite3.connect(args.db)
    else:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = migrate(conn, m=dcfg.CONFIRM_M,
                         hysteresis=dcfg.CONFIRM_DEMOTE_HYSTERESIS,
                         k=dcfg.ADMIT_MAX_PER_FAMILY,
                         now_ns=time.time_ns(), apply=args.apply)
    finally:
        conn.close()
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] seats={report['kept_seats']} band={report['exempt_band']} "
          f"demotees={report['exempt_demotees']} benched={report['benched']} "
          f"confirming_after={report['confirming_after']} flipped={report['flipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
