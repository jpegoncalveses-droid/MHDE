"""KI-166 un-expire one-off (operator-approved, PR #93 precedent).

The 2026-08-20 passes expired 635 confirming rules on WALL-clock elapsed while the
frontier sat frozen at 2026-08-15 06:04 — opportunity manufactured by a dead pipe
(full RCA: data/processed/frontier_stall_rca.md). EXPIRED is terminal, so the
artifacts are permanently dead without this one-off.

This script recomputes the EVIDENCE-clock expiry predicate (F4 semantics:
elapsed = frozen_frontier − discovery_window_ns, window-capped) for every EXPIRED
row and flips back to CONFIRMING exactly the rows the honest clock would NOT have
expired. Rows that satisfy the evidence-clock predicate — genuinely rate-collapsed
on real labeled tape — STAND. Idempotent; dry-run by default.

The direct EXPIRED→CONFIRMING UPDATE deliberately bypasses ``set_state`` (the
state machine keeps EXPIRED terminal; this is a sanctioned incident one-off, the
same authority as the PR #93 promoted_at_ns backfill). ``updated_at_ns`` is
stamped; nothing else on the row is touched.

Usage:
    venv/bin/python scripts/unexpire_ki166_stall_artifacts.py            # dry-run
    venv/bin/python scripts/unexpire_ki166_stall_artifacts.py --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import time

#: The frontier every KI-166-era expiry ran against (markprice MAX(window_end_ns),
#: frozen 2026-08-15 06:04:00 UTC through all three expiring passes).
KI166_FROZEN_FRONTIER_NS = 1_786_773_840_000_000_000
DEFAULT_DB = "data/research/brain/discovery.sqlite"

_PREDICATE = (
    "promoted_at_ns IS NULL "
    "AND fresh_count < ? "
    "AND CAST(n_fires AS REAL) * MIN(? - discovery_window_ns, ?) >= CAST(? AS REAL) * ? "
    "AND CAST(fresh_count AS REAL) * ? * ? < CAST(n_fires AS REAL) * MIN(? - discovery_window_ns, ?)"
)


def _predicate_params(frozen_frontier_ns, m, hysteresis, history_ns,
                      opportunity_floor, pace_factor):
    return (m - hysteresis,
            frozen_frontier_ns, history_ns, opportunity_floor, history_ns,
            pace_factor, history_ns, frozen_frontier_ns, history_ns)


def unexpire(conn: sqlite3.Connection, *, frozen_frontier_ns: int, m: int,
             hysteresis: int, history_ns: int, opportunity_floor: int,
             pace_factor: int, now_ns: int, apply: bool) -> dict:
    """Classify every EXPIRED row under the evidence clock at ``frozen_frontier_ns``;
    with ``apply`` flip the artifacts back to CONFIRMING. Returns the counts."""
    params = _predicate_params(frozen_frontier_ns, m, hysteresis, history_ns,
                               opportunity_floor, pace_factor)
    total = conn.execute(
        "SELECT COUNT(*) FROM rules WHERE state='expired'").fetchone()[0]
    genuine = conn.execute(
        f"SELECT COUNT(*) FROM rules WHERE state='expired' AND {_PREDICATE}",
        params).fetchone()[0]
    artifacts = total - genuine
    flipped = 0
    if apply and artifacts:
        with conn:
            cur = conn.execute(
                f"UPDATE rules SET state='confirming', updated_at_ns=? "
                f"WHERE state='expired' AND NOT ({_PREDICATE})",
                (now_ns, *params))
            flipped = cur.rowcount
    return {"total_expired": total, "genuine": genuine,
            "artifacts": artifacts, "flipped": flipped}


def main() -> int:
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from crypto.research.brain.discovery import config as dcfg

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true",
                    help="write the flips (default: dry-run report only)")
    args = ap.parse_args()

    if args.apply:
        conn = sqlite3.connect(args.db)
    else:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        report = unexpire(
            conn, frozen_frontier_ns=KI166_FROZEN_FRONTIER_NS,
            m=dcfg.CONFIRM_M, hysteresis=dcfg.CONFIRM_DEMOTE_HYSTERESIS,
            history_ns=dcfg.DISCOVERY_HISTORY_NS, opportunity_floor=dcfg.CONFIRM_M,
            pace_factor=dcfg.EXPIRE_PACE_FACTOR,
            now_ns=time.time_ns(), apply=args.apply)
    finally:
        conn.close()
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] expired={report['total_expired']} "
          f"genuine(stand)={report['genuine']} artifacts={report['artifacts']} "
          f"flipped={report['flipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
