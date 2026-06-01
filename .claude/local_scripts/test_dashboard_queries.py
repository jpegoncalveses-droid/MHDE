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
    engine_db_path,
    get_paper_open_positions,
    get_paper_closed_trades,
    get_paper_failed_entries,
    get_paper_engine_runs_summary,
    get_paper_today_cohort,
    get_paper_position_snapshots,
    build_position_chart_frame,
    position_is_armed,
)

conn = duckdb.connect("data/mhde.duckdb", read_only=True)
errors = []

def check(label, fn):
    try:
        r = fn()
        count = len(r) if hasattr(r, "__len__") else r
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

# --- Paper-trading tab queries (read the engine DuckDB read-only) ---
print("\n=== Paper-trading queries (engine DB) ===\n")
_engine_path = engine_db_path()
if not os.path.exists(_engine_path):
    print(f"  SKIP paper-trading queries — engine DB not found at {_engine_path}")
else:
    eng = duckdb.connect(_engine_path, read_only=True)
    check("paper_engine_runs_summary", lambda: get_paper_engine_runs_summary(eng))
    check("paper_open_positions",
          lambda: get_paper_open_positions(eng, trail_pct=0.30, activation_pct=0.01))
    check("paper_closed_trades", lambda: get_paper_closed_trades(eng, limit=30))
    check("paper_failed_entries", lambda: get_paper_failed_entries(eng, limit=20))
    # Rebuilt positions view (paper-tab-overhaul): cohort + per-position charts.
    cohort = check("paper_today_cohort", lambda: get_paper_today_cohort(eng))
    if cohort is not None and len(cohort) > 0:
        _id = cohort.iloc[0]["id"]
        _snaps = check("paper_position_snapshots",
                       lambda: get_paper_position_snapshots(eng, _id, max_points=400))
        check("position_is_armed",
              lambda: position_is_armed(
                  entry_price=cohort.iloc[0]["entry_price"],
                  peak_price=cohort.iloc[0]["peak_price"],
                  activation_pct=0.01))
        if _snaps is not None:
            check("build_position_chart_frame",
                  lambda: build_position_chart_frame(
                      _snaps, entry_price=cohort.iloc[0]["entry_price"],
                      peak_price=cohort.iloc[0]["peak_price"],
                      trail_pct=0.30, activation_pct=0.01))
    eng.close()

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
