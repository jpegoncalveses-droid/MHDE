
## Python command policy

Use `venv/bin/python` directly instead of activating the virtual environment with `source venv/bin/activate`.

Preferred commands:
- `venv/bin/python main.py health 2>&1 | tail -20`
- `venv/bin/python main.py backtest smoke 2>&1 | tail -10`

## Dashboard query smoke test command policy

Do not use inline `python -c` blocks for dashboard query validation.

Use this command instead:

`MHDE_DASHBOARD_AUTH_ENABLED=false venv/bin/python .claude/local_scripts/test_dashboard_queries.py 2>&1`
