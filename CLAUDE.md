## Read first

Before doing any work in this repo — debugging, refactoring, adding features, or operational tasks — read these files:

1. `ARCHITECTURE.md` — top-down system architecture; the three engines + daily-analysis path + dashboard.
2. `INFRASTRUCTURE.md` — VPS, services, reverse proxy, schedules, secrets locations.
3. `OPERATIONS.md` — runbook: manual pipeline invocations, recovery procedures, deploy steps.
4. `DATABASE_SCHEMA.md` — every table, columns, types, reader/writer modules.
5. `KNOWN_ISSUES.md` — open and resolved bug tracker.
6. `DECISIONS.md` — architecture decision records.
7. `HARDENING_PLAN.md` — multi-session roadmap; check the current session number before starting work.
8. `SESSION_LOG.md` — append-only log of what was done each session (read the most recent 3 entries at session start).

These describe the deployment topology and the code layout. They override assumptions from training data.

`legacy/` is **dormant code preserved for rollback safety**. Nothing under `legacy/` is imported by ACTIVE code. Don't refer to it for how production behaves; see `legacy/README.md` for what's there.

`docs/` (lowercase) contains pre-rebuild prose documentation. Some sections are still useful (data sources, scoring rationale) but the architectural sections were superseded by `ARCHITECTURE.md` in Session 1.

## Python command policy

Use `venv/bin/python` directly instead of activating the virtual environment with `source venv/bin/activate`.

Preferred commands:
- `venv/bin/python main.py health 2>&1 | tail -20`
- `venv/bin/python main.py backtest smoke 2>&1 | tail -10`

## Dashboard query smoke test command policy

Do not use inline `python -c` blocks for dashboard query validation.

Use this command instead:

`MHDE_DASHBOARD_AUTH_ENABLED=false venv/bin/python .claude/local_scripts/test_dashboard_queries.py 2>&1`

## Python pytest command policy

Do not use `source .venv/bin/activate && python ...`.

Use the virtual environment Python directly:

`.venv/bin/python -m pytest research/gbpeur_personal_fx/predictive/tests/test_forecast.py -v`

## DuckDB smoke test command policy

Do not use inline `python -c` blocks for DuckDB smoke tests.

Use this command instead:

`venv/bin/python .claude/local_scripts/test_duckdb_failed_alter.py`
