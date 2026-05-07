"""Production monitors that fire Telegram alerts on detected anomalies.

Each module under `monitoring/` exposes a `run()` function returning a
`MonitorResult` (see monitoring/alert.py). The CLI `main.py monitor`
dispatches subcommands to these runners.

Schedules (per HARDENING_PLAN.md Session 6):
  dashboard_consistency  every 6h
  pipeline_execution     hourly
  config_drift           daily
  model_performance      daily
  data_quality           after each ingestion (≈ daily)
  smoke_test             hourly

Set `MONITORING_DRY_RUN=true` to suppress real Telegram alerts during
testing or manual runs. Alert payloads are still computed and logged.
"""
