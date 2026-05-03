"""Smoke test all dashboard query functions against the live DuckDB."""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

os.environ["MHDE_DB_PATH"] = "data/mhde.duckdb"
os.environ["MHDE_DASHBOARD_AUTH_ENABLED"] = "false"

import duckdb
from dashboard.services.queries import (
    get_overview_stats,
    get_candidates,
    get_candidate_detail,
    get_source_health,
    get_outcomes,
    get_llm_runs,
    get_health_checks,
    get_hypotheses,
    get_alerts,
    get_backtest_runs,
    get_latest_run_id,
)

conn = duckdb.connect("data/mhde.duckdb", read_only=True)
errors = []

def check(label, fn):
    try:
        r = fn()
        count = len(r) if isinstance(r, (list, dict)) else r
        print(f"  OK  {label}: {count}")
        return r
    except Exception as e:
        errors.append((label, e))
        print(f"  ERR {label}: {e}")
        return None

print("\n=== Dashboard Query Smoke Test ===\n")
stats  = check("overview_stats",  lambda: get_overview_stats(conn))
cands  = check("candidates",      lambda: get_candidates(conn))
check("source_health",    lambda: get_source_health(conn))
check("outcomes",         lambda: get_outcomes(conn))
check("llm_runs",         lambda: get_llm_runs(conn))
check("health_checks",    lambda: get_health_checks(conn))
check("hypotheses",       lambda: get_hypotheses(conn))
check("alerts",           lambda: get_alerts(conn))
check("backtest_runs",    lambda: get_backtest_runs(conn))

run_id = get_latest_run_id(conn)
if cands and run_id:
    check("candidate_detail",
          lambda: get_candidate_detail(conn, cands[0]["ticker"], run_id))

conn.close()

print(f"\n{'='*40}")
if errors:
    print(f"FAILED — {len(errors)} error(s):")
    for label, exc in errors:
        print(f"  {label}: {exc}")
    sys.exit(1)

print(f"PASSED — all queries OK")
if stats:
    print(f"\nDB summary:")
    print(f"  Universe:         {stats.get('universe_size')}")
    print(f"  Candidates:       {stats.get('candidates_scored')}")
    print(f"  A/B/C/Reject:     {stats.get('tier_a')}/{stats.get('tier_b')}/{stats.get('tier_c')}/{stats.get('rejected')}")
    print(f"  Feature coverage: {stats.get('feature_coverage_pct', 'N/A'):.0f}%" if stats.get('feature_coverage_pct') else "  Feature coverage: N/A")
    print(f"  Latest run:       {stats.get('run_id')}")
sys.exit(0)
