"""Regression tests — one test per documented bug in KNOWN_ISSUES.md.

Each test should fail without its fix and pass with it. New regression
tests are added here when a new KI is closed.

KI → test mapping:

| KI       | Test                                                                           |
|----------|--------------------------------------------------------------------------------|
| KI-001   | tests/regression/test_schema_consistency.py::test_nginx_review_returns_404    |
| KI-002   | (no test — was a documentation drift, not a code regression)                  |
| KI-003   | (open — Session 6+ when promotion gate lands)                                 |
| KI-004   | tests/regression/test_cli_registry.py::test_models_saved_gitignored           |
| KI-005   | tests/fx/test_labels.py::test_compute_labels_empty_db                         |
| KI-006   | tests/equity/test_ml_features.py::test_compute_features_empty_universe        |
| KI-007   | tests/equity/test_ml_evaluate.py::test_print_no_folds                         |
| KI-101   | tests/regression/test_systemd_units.py::test_retrain_timers_staggered         |
| KI-102   | tests/regression/test_systemd_units.py::test_equity_predict_includes_features |
| KI-103   | tests/integration/test_crypto_pipeline.py::test_crypto_pipeline_end_to_end    |
|          | tests/crypto/test_predict.py::test_fill_outcomes_5d_horizon_hits_threshold    |
| KI-104   | tests/equity/test_ml_predict.py::test_fill_outcomes_uses_trading_days_not_calendar |
|          | tests/integration/test_equity_pipeline.py::test_equity_pipeline_end_to_end    |
| KI-105   | tests/regression/test_dashboard_structure.py::test_no_module_level_connection |
| KI-106   | tests/regression/test_systemd_units.py::test_user_level_units_no_user_group   |
| KI-107   | tests/integration/test_failure_modes.py::test_fx_pipeline_warns_but_runs_when_stale |
| KI-108   | tests/regression/test_systemd_units.py::test_crypto_predict_chain             |
| KI-109   | tests/regression/test_systemd_units.py::test_health_check_unit_deployed       |
| KI-110   | tests/integration/test_fx_pipeline.py::test_fx_position_aware_alert_suppression |
| KI-111   | tests/integration/test_failure_modes.py::test_storage_db_retries_on_lock_error |
| KI-112   | tests/regression/test_systemd_units.py::test_repo_vs_deployed_unit_parity     |
| KI-113   | tests/regression/test_dashboard_structure.py::test_outcome_columns_in_predictions |
| KI-116   | tests/regression/test_legacy_isolation.py::test_no_active_code_imports_legacy |
| KI-117   | tests/regression/test_schema_consistency.py::test_models_saved_path_exists    |

Plus structural regressions called out in HARDENING_PLAN.md Session 5:

- Schema migration test: test_schema_consistency.py
- CLI registry test: test_cli_registry.py
- Service file test: test_systemd_units.py
- Timer schedule test: test_systemd_units.py
- Legacy isolation test: test_legacy_isolation.py
"""
