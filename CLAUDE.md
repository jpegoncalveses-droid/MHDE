## Read first

Before doing any work in this repo — debugging, refactoring, adding features, or operational tasks — read these files:

1. `INFRASTRUCTURE.md` — VPS, services, reverse proxy, schedules, secrets locations.
2. `docs/mhde_codebase_inventory.md` — codebase architecture and module map.

These describe the deployment topology and the code layout. They override assumptions from training data.

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
