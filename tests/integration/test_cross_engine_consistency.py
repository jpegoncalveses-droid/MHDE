"""Integration tests that span multiple engines.

Validates the cross-cutting invariants: schema parity for prediction
tables, date-convention compatibility, and the health-check coverage of
all three engines.
"""
from __future__ import annotations


COMMON_PREDICTION_COLUMNS = {
    "model_id", "horizon", "predicted_probability", "prediction_threshold",
    "actual_hit", "outcome_filled_at",
}


def _columns(conn, table_name: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = ?", [table_name]
    ).fetchall()
    return {r[0] for r in rows}


def test_all_three_predictions_share_common_columns(temp_db):
    """ml_predictions, crypto_ml_predictions, fx_ml_predictions must
    share the columns that the dashboard / health checks rely on."""
    eq_cols = _columns(temp_db, "ml_predictions")
    cr_cols = _columns(temp_db, "crypto_ml_predictions")
    fx_cols = _columns(temp_db, "fx_ml_predictions")

    for table_name, cols in [("ml_predictions", eq_cols),
                              ("crypto_ml_predictions", cr_cols),
                              ("fx_ml_predictions", fx_cols)]:
        missing = COMMON_PREDICTION_COLUMNS - cols
        assert not missing, f"{table_name} missing common cols: {sorted(missing)}"


def test_each_engine_has_outcome_filling_column(temp_db):
    """outcome_filled_at exists on every engine's predictions table —
    the canonical signal for `was this prediction reconciled with reality?`"""
    for table in ("ml_predictions", "crypto_ml_predictions", "fx_ml_predictions"):
        assert "outcome_filled_at" in _columns(temp_db, table)


def test_engine_keys_distinguishable(temp_db):
    """Each engine has a distinct entity key:
       equity → ticker, crypto → symbol, fx → datetime_utc-only.
    """
    assert "ticker" in _columns(temp_db, "ml_predictions")
    assert "symbol" in _columns(temp_db, "crypto_ml_predictions")
    assert "ticker" not in _columns(temp_db, "fx_ml_predictions")
    assert "symbol" not in _columns(temp_db, "fx_ml_predictions")
    # FX is keyed only on time
    assert "datetime_utc" in _columns(temp_db, "fx_ml_predictions")


def test_each_engine_has_active_model_runs_table(temp_db):
    for table in ("ml_model_runs", "crypto_ml_model_runs", "fx_ml_model_runs"):
        cols = _columns(temp_db, table)
        assert "model_id" in cols
        assert "is_active" in cols
        assert "model_path" in cols


def test_freshness_check_covers_all_engines(temp_db):
    """pipelines.freshness.check_all returns reports for all 3 engines."""
    from pipelines.freshness import check_all
    reports = check_all(temp_db)
    assert set(reports.keys()) == {"equity", "crypto", "fx"}


def test_health_orchestrator_runs_on_all_engines_present(temp_db):
    """run_all_checks must complete (no exceptions) when all engine
    schemas are in place."""
    from health.checks import run_all_checks, overall_status
    results = run_all_checks(temp_db, "test_run", {})
    # Must produce some checks, all with valid status
    assert len(results) > 0
    for r in results:
        assert r["status"] in ("pass", "warn", "fail", "skip")
    # overall_status must return a valid label
    assert overall_status(results) in ("PASS", "PASS_WITH_WARNINGS", "FAIL", "SKIP")
