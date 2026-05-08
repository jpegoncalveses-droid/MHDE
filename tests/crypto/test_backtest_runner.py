"""Tests for crypto/execution/backtest/runner.py.

Covers all required cases from step-5 spec:
  - 2 fresh configs both complete; 2 runs + 2 summaries persisted
  - skip_existing default: re-run silently skips
  - skip_existing=False without force: collision → failed_collision
  - force=True: overwrites existing
  - One config fails: that run marked failed_runtime, others unaffected
  - GridConfig.run_id is deterministic across invocations
  - CLI: --grid base/sensitivity validation parses correctly
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from crypto.execution.backtest.harness import (
    ensure_backtest_tables,
    make_run_id,
)
from crypto.execution.backtest.runner import (
    DEFAULT_HORIZONS,
    DEFAULT_POLICIES,
    DEFAULT_SELECTION_RULES,
    GridConfig,
    GridResult,
    GridRunResult,
    base_grid_configs,
    run_grid,
    summarize_grid_result,
)


# ──────────────────────────────────────────────────────────────────────
# Fixtures: minimal walkfold + price data so run_backtest can complete
# ──────────────────────────────────────────────────────────────────────


def _seed_walkfold_minimal(conn, horizon: str = "5d") -> None:
    """Seed one walkfold model_run + 3 BTC predictions + 25 days of flat
    100.0 prices. With flat prices no policy hits TP/SL → trades exit
    via time stop. Sufficient for testing the grid orchestration."""
    conn.execute(
        """
        INSERT INTO crypto_ml_model_runs
            (model_id, horizon, target_threshold,
             train_start, train_end, test_start, test_end, is_active)
        VALUES (?, ?, 0.10,
                '2024-01-01', '2025-04-04',
                '2025-04-05', '2025-04-30', false)
        """,
        [f"crypto_{horizon}_walkfold_2025_04", horizon],
    )
    for d in [date(2025, 4, 5), date(2025, 4, 7), date(2025, 4, 13)]:
        conn.execute(
            """
            INSERT INTO crypto_ml_predictions
                (symbol, prediction_date, model_id, horizon,
                 predicted_probability, prediction_threshold, market_cap_bucket)
            VALUES ('BTCUSDT', ?, ?, ?, 0.7, 0.10, 'unknown')
            """,
            [d, f"crypto_{horizon}_walkfold_2025_04", horizon],
        )
    for offset in range(0, 25):
        d = date(2025, 4, 5) + timedelta(days=offset)
        conn.execute(
            """
            INSERT INTO crypto_prices_daily
                (symbol, trade_date, open, high, low, close,
                 volume, trades, taker_buy_volume, source)
            VALUES ('BTCUSDT', ?, 100.0, 100.5, 99.5, 100.0,
                    1000.0, 1, 100.0, 'test')
            """,
            [d],
        )


def _two_compatible_configs() -> list[GridConfig]:
    """Two configs at 5d horizon — different policies so the run_ids differ."""
    return [
        GridConfig(
            horizon="5d", policy="A", selection="top_n",
            selection_params={"n": 1}, policy_params={},
        ),
        GridConfig(
            horizon="5d", policy="B", selection="top_n",
            selection_params={"n": 1}, policy_params={},
        ),
    ]


def _table_count(conn, table: str, run_id: str | None = None) -> int:
    if run_id is None:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return int(conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", [run_id]
    ).fetchone()[0])


# ──────────────────────────────────────────────────────────────────────
# GridConfig + base_grid_configs
# ──────────────────────────────────────────────────────────────────────


def test_grid_config_run_id_is_deterministic_across_invocations():
    cfg1 = GridConfig(
        horizon="5d", policy="A", selection="top_n",
        selection_params={"n": 6}, policy_params={},
    )
    cfg2 = GridConfig(
        horizon="5d", policy="A", selection="top_n",
        selection_params={"n": 6}, policy_params={},
    )
    assert cfg1.run_id == cfg2.run_id
    # Different params → different run_id.
    cfg3 = GridConfig(
        horizon="5d", policy="A", selection="top_n",
        selection_params={"n": 7}, policy_params={},
    )
    assert cfg3.run_id != cfg1.run_id


def test_grid_config_run_id_matches_make_run_id():
    cfg = GridConfig(
        horizon="10d", policy="C", selection="threshold",
        selection_params={"threshold": 0.55}, policy_params={"atr_mult": 2.0},
    )
    assert cfg.run_id == make_run_id(
        horizon="10d", exit_policy_id="C", selection_rule="threshold",
        selection_params={"threshold": 0.55}, policy_params={"atr_mult": 2.0},
    )


def test_base_grid_configs_produces_2x5x2_matrix():
    configs = base_grid_configs()
    assert len(configs) == len(DEFAULT_HORIZONS) * len(DEFAULT_POLICIES) * len(
        DEFAULT_SELECTION_RULES
    )
    assert len(configs) == 20
    horizons = {c.horizon for c in configs}
    policies = {c.policy for c in configs}
    selections = {c.selection for c in configs}
    assert horizons == set(DEFAULT_HORIZONS)
    assert policies == set(DEFAULT_POLICIES)
    assert selections == set(DEFAULT_SELECTION_RULES)
    # All run_ids are distinct.
    run_ids = {c.run_id for c in configs}
    assert len(run_ids) == 20


def test_base_grid_configs_default_params():
    """Default top_n=6 and threshold=0.55 (per SPEC.md selection rules)."""
    configs = base_grid_configs()
    top_n_configs = [c for c in configs if c.selection == "top_n"]
    threshold_configs = [c for c in configs if c.selection == "threshold"]
    assert all(c.selection_params == {"n": 6} for c in top_n_configs)
    assert all(c.selection_params == {"threshold": 0.55}
               for c in threshold_configs)


# ──────────────────────────────────────────────────────────────────────
# run_grid — happy path
# ──────────────────────────────────────────────────────────────────────


def test_run_grid_two_fresh_configs_both_complete(temp_db):
    ensure_backtest_tables(temp_db)
    _seed_walkfold_minimal(temp_db, "5d")
    configs = _two_compatible_configs()
    result = run_grid(temp_db, configs)

    assert isinstance(result, GridResult)
    assert len(result.results) == 2
    assert result.n_completed == 2
    assert result.n_skipped == 0
    assert result.n_failed == 0

    for r in result.results:
        assert r.status == "completed"
        assert r.summary is not None
        assert r.error is None
        assert r.elapsed_seconds >= 0

    # Both run rows + summary rows persisted.
    for cfg in configs:
        assert _table_count(temp_db, "crypto_backtest_runs", cfg.run_id) == 1
        assert _table_count(
            temp_db, "crypto_backtest_summary", cfg.run_id
        ) == 1


def test_run_grid_skip_existing_default_silently_skips(temp_db):
    ensure_backtest_tables(temp_db)
    _seed_walkfold_minimal(temp_db, "5d")
    configs = _two_compatible_configs()
    # First run — populates both rows for both configs.
    run_grid(temp_db, configs)

    # Second run with default skip_existing=True → both skipped.
    result = run_grid(temp_db, configs)
    assert result.n_skipped == 2
    assert result.n_completed == 0
    assert result.n_failed == 0
    for r in result.results:
        assert r.status == "skipped_existing"
        assert r.summary is None


def test_run_grid_skip_existing_false_marks_collision_as_failure(temp_db):
    ensure_backtest_tables(temp_db)
    _seed_walkfold_minimal(temp_db, "5d")
    configs = _two_compatible_configs()
    run_grid(temp_db, configs)   # populate

    result = run_grid(temp_db, configs, skip_existing=False, force=False)
    assert result.n_failed == 2
    assert result.n_completed == 0
    assert result.n_skipped == 0
    for r in result.results:
        assert r.status == "failed_collision"
        assert r.error is not None
        assert "already exists" in r.error


def test_run_grid_force_true_overwrites_existing(temp_db):
    ensure_backtest_tables(temp_db)
    _seed_walkfold_minimal(temp_db, "5d")
    configs = _two_compatible_configs()
    run_grid(temp_db, configs)

    # Capture the original timestamps.
    pre_ts = {
        cfg.run_id: temp_db.execute(
            "SELECT run_timestamp FROM crypto_backtest_runs WHERE run_id = ?",
            [cfg.run_id],
        ).fetchone()[0]
        for cfg in configs
    }

    # Force re-run — must succeed and replace the rows.
    import time as _time
    _time.sleep(0.05)   # ensure CURRENT_TIMESTAMP advances on the rewrite
    result = run_grid(temp_db, configs, force=True)
    assert result.n_completed == 2
    assert result.n_failed == 0
    for cfg in configs:
        assert _table_count(temp_db, "crypto_backtest_runs", cfg.run_id) == 1
        post_ts = temp_db.execute(
            "SELECT run_timestamp FROM crypto_backtest_runs WHERE run_id = ?",
            [cfg.run_id],
        ).fetchone()[0]
        assert post_ts >= pre_ts[cfg.run_id]


# ──────────────────────────────────────────────────────────────────────
# run_grid — failure isolation
# ──────────────────────────────────────────────────────────────────────


def test_run_grid_one_config_fails_others_complete(temp_db):
    """A config with an unknown selection_rule causes _apply_selection
    to raise ValueError, which the runner catches as failed_runtime.
    The other config still completes."""
    ensure_backtest_tables(temp_db)
    _seed_walkfold_minimal(temp_db, "5d")
    configs = [
        GridConfig(
            horizon="5d", policy="A", selection="top_n",
            selection_params={"n": 1}, policy_params={},
        ),
        GridConfig(
            horizon="5d", policy="A", selection="bogus_rule",
            selection_params={}, policy_params={},
        ),
        GridConfig(
            horizon="5d", policy="B", selection="top_n",
            selection_params={"n": 1}, policy_params={},
        ),
    ]
    result = run_grid(temp_db, configs)
    assert result.n_completed == 2
    assert result.n_failed == 1
    statuses = [r.status for r in result.results]
    assert statuses[0] == "completed"
    assert statuses[1] == "failed_runtime"
    assert statuses[2] == "completed"
    assert "selection_rule" in result.results[1].error.lower() or \
           "bogus" in result.results[1].error.lower()
    # Failed config did NOT persist anything.
    assert _table_count(
        temp_db, "crypto_backtest_runs", configs[1].run_id
    ) == 0


# ──────────────────────────────────────────────────────────────────────
# run_grid — dry-run
# ──────────────────────────────────────────────────────────────────────


def test_run_grid_dry_run_writes_nothing(temp_db):
    ensure_backtest_tables(temp_db)
    _seed_walkfold_minimal(temp_db, "5d")
    configs = _two_compatible_configs()
    result = run_grid(temp_db, configs, dry_run=True)
    assert result.n_completed == 2
    assert _table_count(temp_db, "crypto_backtest_runs") == 0
    assert _table_count(temp_db, "crypto_backtest_trades") == 0
    assert _table_count(temp_db, "crypto_backtest_summary") == 0
    # Dry-run still attempts to compute summary? No — we skip persistence
    # in dry-run mode, so summary is None.
    for r in result.results:
        assert r.summary is None


# ──────────────────────────────────────────────────────────────────────
# summarize_grid_result formatter
# ──────────────────────────────────────────────────────────────────────


def test_summarize_grid_result_renders_status_counts():
    cfg = GridConfig(
        horizon="5d", policy="A", selection="top_n",
        selection_params={"n": 6}, policy_params={},
    )
    results = [
        GridRunResult(config=cfg, run_id=cfg.run_id, status="completed",
                       summary=None, error=None, elapsed_seconds=1.5),
        GridRunResult(config=cfg, run_id="other_run_id_skip",
                       status="skipped_existing",
                       summary=None, error=None, elapsed_seconds=0.0),
        GridRunResult(config=cfg, run_id="other_run_id_fail",
                       status="failed_runtime",
                       summary=None, error="boom",
                       elapsed_seconds=0.5),
    ]
    text = summarize_grid_result(GridResult(results=results))
    assert "completed        : 1" in text
    assert "skipped existing : 1" in text
    assert "failed           : 1" in text
    assert "other_run_id_fail" in text
    assert "boom" in text


# ──────────────────────────────────────────────────────────────────────
# CLI surface
# ──────────────────────────────────────────────────────────────────────


def test_cli_backtest_grid_help_lists_options():
    from click.testing import CliRunner
    from main import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["crypto", "backtest-grid", "--help"])
    assert result.exit_code == 0
    assert "--grid" in result.output
    assert "--force" in result.output
    assert "--dry-run" in result.output
    # Both halves of the toggle render.
    assert "--skip-existing" in result.output
    assert "--no-skip-existing" in result.output


def test_cli_backtest_grid_rejects_invalid_grid_value():
    from click.testing import CliRunner
    from main import cli
    runner = CliRunner()
    result = runner.invoke(
        cli, ["crypto", "backtest-grid", "--grid", "invalid"],
    )
    assert result.exit_code != 0
    assert "Invalid value for '--grid'" in result.output


def test_cli_backtest_grid_sensitivity_raises_not_implemented():
    """sensitivity grid is the next phase; CLI must signal that clearly."""
    from click.testing import CliRunner
    from main import cli
    runner = CliRunner()
    result = runner.invoke(
        cli, ["crypto", "backtest-grid", "--grid", "sensitivity"],
    )
    assert result.exit_code != 0
    # The exception bubbles up through click's runner.
    assert isinstance(result.exception, NotImplementedError)
