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


# ──────────────────────────────────────────────────────────────────────
# sensitivity_grid_configs
# ──────────────────────────────────────────────────────────────────────


def _seed_base_run(conn, run_id: str, horizon: str, policy: str,
                   selection: str, sel_params: dict, pol_params: dict) -> None:
    """Seed a base run row in crypto_backtest_runs so the sensitivity
    factory can read its configuration. Summary not required for the
    factory's contract — only the runs row is read."""
    import json as _json
    conn.execute(
        """
        INSERT INTO crypto_backtest_runs
            (run_id, run_timestamp, horizon, exit_policy,
             selection_rule, parameters, date_start, date_end,
             n_trades, n_data_gap_exits, n_forward_fills,
             n_predictions_seen, n_skipped_duplicates,
             n_skipped_missing_atr, n_excluded_by_funding_floor,
             n_missing_funding_warnings)
        VALUES (?, '2026-05-08 00:00:00', ?, ?, ?, ?,
                '2025-04-05', '2026-05-08',
                100, 0, 0, 200, 50, 0, 0, 0)
        """,
        [run_id, horizon, policy, selection,
         _json.dumps({"selection_params": sel_params,
                      "policy_params": pol_params})],
    )


def test_sensitivity_grid_factory_is_deterministic(temp_db):
    """Same base_run_ids → identical config sequence (run_id-by-run_id)
    across two invocations."""
    from crypto.execution.backtest.runner import sensitivity_grid_configs
    ensure_backtest_tables(temp_db)
    _seed_base_run(
        temp_db, "backtest_5d_D_threshold_aaaaaaaa",
        horizon="5d", policy="D", selection="threshold",
        sel_params={"threshold": 0.55}, pol_params={},
    )

    out1 = sensitivity_grid_configs(temp_db, ["backtest_5d_D_threshold_aaaaaaaa"])
    out2 = sensitivity_grid_configs(temp_db, ["backtest_5d_D_threshold_aaaaaaaa"])
    assert [c.run_id for c in out1] == [c.run_id for c in out2]


def test_sensitivity_grid_factory_axis_coverage_per_winner(temp_db):
    """One base run → 11 emitted configs: 3 trail + 4 activation +
    4 selection. Each config varies exactly one axis from the base
    while holding the other two at the base values."""
    from crypto.execution.backtest.runner import (
        SENSITIVITY_TRAIL_PCT, SENSITIVITY_ACTIVATION_PCT,
        SENSITIVITY_THRESHOLD,
        sensitivity_grid_configs,
    )
    ensure_backtest_tables(temp_db)
    _seed_base_run(
        temp_db, "backtest_5d_D_threshold_bbbbbbbb",
        horizon="5d", policy="D", selection="threshold",
        sel_params={"threshold": 0.55}, pol_params={},
    )

    configs = sensitivity_grid_configs(
        temp_db, ["backtest_5d_D_threshold_bbbbbbbb"]
    )
    assert len(configs) == (
        len(SENSITIVITY_TRAIL_PCT)
        + len(SENSITIVITY_ACTIVATION_PCT)
        + len(SENSITIVITY_THRESHOLD)
    ) == 11

    # Sweep block 1 (trail): each config has trail_pct varied; selection
    # is the base threshold; activation_pct is unset (base default).
    trail_block = configs[: len(SENSITIVITY_TRAIL_PCT)]
    trail_values = []
    for c in trail_block:
        assert c.horizon == "5d" and c.policy == "D" and c.selection == "threshold"
        assert c.selection_params == {"threshold": 0.55}
        # activation_pct should NOT be set (we're not sweeping it)
        assert "activation_pct" not in c.policy_params
        trail_values.append(c.policy_params.get("trail_pct"))
    # The base value (0.50) when overlaid against empty pol_params with
    # default 0.50 is elided — that's the run_id-collision feature. So
    # the trail sweep yields {0.30, 0.70} as explicit overrides + one
    # config with empty policy_params (which represents trail=0.50).
    explicit = [v for v in trail_values if v is not None]
    elided   = [v for v in trail_values if v is None]
    assert sorted(explicit) == [0.30, 0.70]
    assert len(elided) == 1  # the base point

    # Sweep block 2 (activation): symmetrical reasoning
    act_block = configs[len(SENSITIVITY_TRAIL_PCT)
                        : len(SENSITIVITY_TRAIL_PCT)
                          + len(SENSITIVITY_ACTIVATION_PCT)]
    act_values = [c.policy_params.get("activation_pct") for c in act_block]
    explicit = [v for v in act_values if v is not None]
    elided   = [v for v in act_values if v is None]
    assert sorted(explicit) == [0.00, 0.02, 0.03]  # 0.01 is the elided base
    assert len(elided) == 1

    # Sweep block 3 (selection): threshold values
    sel_block = configs[-len(SENSITIVITY_THRESHOLD):]
    thresholds = sorted(c.selection_params["threshold"] for c in sel_block)
    assert thresholds == [0.50, 0.55, 0.60, 0.65]


def test_sensitivity_grid_factory_top_n_branch(temp_db):
    """Base with selection=top_n must sweep 'n', not 'threshold'."""
    from crypto.execution.backtest.runner import (
        SENSITIVITY_TOP_N, sensitivity_grid_configs,
    )
    ensure_backtest_tables(temp_db)
    _seed_base_run(
        temp_db, "backtest_10d_D_top_n_cccccccc",
        horizon="10d", policy="D", selection="top_n",
        sel_params={"n": 6}, pol_params={},
    )

    configs = sensitivity_grid_configs(
        temp_db, ["backtest_10d_D_top_n_cccccccc"]
    )
    sel_block = configs[-len(SENSITIVITY_TOP_N):]
    n_values = sorted(c.selection_params["n"] for c in sel_block)
    assert n_values == [5, 6, 7, 8]
    # And no 'threshold' key leaked into selection_params
    for c in sel_block:
        assert "threshold" not in c.selection_params


def test_sensitivity_grid_skip_existing_collapses_base_overlap(temp_db):
    """The base-point sensitivity configs (one per axis sweep, 3 total
    for one base) produce the SAME run_id as the stored base run.
    `_run_id_already_persisted` skips them. Net new run_ids per base
    after dedup = 11 - 3 + 1 = 9 unique configs, of which 1 (the base)
    is already persisted, leaving 8 new runs."""
    from crypto.execution.backtest.runner import (
        sensitivity_grid_configs, _run_id_already_persisted,
    )
    ensure_backtest_tables(temp_db)
    base_id = make_run_id(
        horizon="5d", exit_policy_id="D", selection_rule="threshold",
        selection_params={"threshold": 0.55}, policy_params={},
    )
    _seed_base_run(
        temp_db, base_id,
        horizon="5d", policy="D", selection="threshold",
        sel_params={"threshold": 0.55}, pol_params={},
    )

    configs = sensitivity_grid_configs(temp_db, [base_id])
    base_matches = [c for c in configs if c.run_id == base_id]
    # Base config appears exactly 3 times (once per axis sweep that
    # includes the base value).
    assert len(base_matches) == 3
    # And the runner's existence check confirms the base IS persisted.
    assert _run_id_already_persisted(temp_db, base_id) is True
    # Unique run_ids in the 11 emitted = 11 - 2 (base counted 3 times)
    # = 9. Of those 9, the base is already persisted, so net new = 8.
    unique_ids = {c.run_id for c in configs}
    assert len(unique_ids) == 9
    new_ids = [rid for rid in unique_ids if rid != base_id]
    assert len(new_ids) == 8


def test_cli_backtest_grid_sensitivity_requires_seed():
    """Without any base runs in the DB and without --top-run-ids,
    the CLI surfaces a clear error rather than running an empty grid."""
    from click.testing import CliRunner
    from main import cli
    runner = CliRunner()
    # Use a temp DB by env so we don't touch production.
    import os, tempfile
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "MHDE_DB_PATH": f"{tmp}/empty.duckdb"}
        # Initialize the DB with the backtest schema.
        import duckdb
        conn = duckdb.connect(env["MHDE_DB_PATH"])
        from crypto.schema import create_all_tables as _crypto
        from crypto.execution.backtest.harness import ensure_backtest_tables
        _crypto(conn)
        ensure_backtest_tables(conn)
        conn.close()
        result = runner.invoke(
            cli, ["crypto", "backtest-grid", "--grid", "sensitivity"],
            env=env,
        )
    assert result.exit_code != 0
    assert "no rows in crypto_backtest_summary" in result.output


# ──────────────────────────────────────────────────────────────────────
# KI-125: iterated-sweep guard
# ──────────────────────────────────────────────────────────────────────


def _setup_with_iterated_top(tmp, *, include_sensitivity_shape: bool):
    """Build a temp DB with one base-grid run plus optionally one
    sensitivity-shape run (non-empty policy_params). Return the env
    dict + the run_ids inserted."""
    import os
    env = {**os.environ, "MHDE_DB_PATH": f"{tmp}/iter.duckdb"}
    import duckdb
    conn = duckdb.connect(env["MHDE_DB_PATH"])
    from crypto.schema import create_all_tables as _crypto
    from crypto.execution.backtest.harness import ensure_backtest_tables
    _crypto(conn)
    ensure_backtest_tables(conn)

    # Insert a canonical base-grid run (Sharpe 1.0).
    base_id = make_run_id(
        horizon="5d", exit_policy_id="D", selection_rule="top_n",
        selection_params={"n": 6}, policy_params={},
    )
    conn.execute(
        """
        INSERT INTO crypto_backtest_runs
            (run_id, run_timestamp, horizon, exit_policy, selection_rule,
             parameters, date_start, date_end, n_trades,
             n_data_gap_exits, n_forward_fills, n_predictions_seen,
             n_skipped_duplicates, n_skipped_missing_atr,
             n_excluded_by_funding_floor, n_missing_funding_warnings)
        VALUES (?, '2026-05-08 00:00:00', '5d', 'D', 'top_n',
                '{"selection_params": {"n": 6}, "policy_params": {}}',
                '2025-04-05', '2026-05-08', 100, 0, 0, 200, 50, 0, 0, 0)
        """,
        [base_id],
    )
    conn.execute(
        "INSERT INTO crypto_backtest_summary (run_id, sharpe_ratio) VALUES (?, 1.0)",
        [base_id],
    )

    sensitivity_id = None
    if include_sensitivity_shape:
        # Higher Sharpe so it ends up in the auto-top-3.
        sensitivity_id = make_run_id(
            horizon="5d", exit_policy_id="D", selection_rule="top_n",
            selection_params={"n": 6}, policy_params={"trail_pct": 0.3},
        )
        conn.execute(
            """
            INSERT INTO crypto_backtest_runs
                (run_id, run_timestamp, horizon, exit_policy, selection_rule,
                 parameters, date_start, date_end, n_trades,
                 n_data_gap_exits, n_forward_fills, n_predictions_seen,
                 n_skipped_duplicates, n_skipped_missing_atr,
                 n_excluded_by_funding_floor, n_missing_funding_warnings)
            VALUES (?, '2026-05-09 11:09:00', '5d', 'D', 'top_n',
                    '{"selection_params": {"n": 6}, "policy_params": {"trail_pct": 0.3}}',
                    '2025-04-05', '2026-05-08', 100, 0, 0, 200, 50, 0, 0, 0)
            """,
            [sensitivity_id],
        )
        conn.execute(
            "INSERT INTO crypto_backtest_summary (run_id, sharpe_ratio) VALUES (?, 5.0)",
            [sensitivity_id],
        )
    conn.close()
    return env, base_id, sensitivity_id


def test_cli_sensitivity_guard_blocks_iterated_auto_top():
    """Auto top-3 from DB contains a sensitivity-shape run → CLI
    refuses with a KI-125 message and a clear bypass instruction."""
    from click.testing import CliRunner
    from main import cli
    import tempfile
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmp:
        env, _, sensitivity_id = _setup_with_iterated_top(
            tmp, include_sensitivity_shape=True
        )
        result = runner.invoke(
            cli, ["crypto", "backtest-grid", "--grid", "sensitivity"],
            env=env,
        )
    assert result.exit_code != 0
    assert "non-base-grid" in result.output
    assert "--allow-iterated" in result.output
    assert "KI-125" in result.output
    assert sensitivity_id in result.output


def test_cli_sensitivity_guard_explicit_iterated_top_run_ids_blocked():
    """Explicit --top-run-ids that includes a non-base-grid id is
    also blocked. The user being explicit doesn't waive the safety
    check; --allow-iterated is the sanctioned escape hatch."""
    from click.testing import CliRunner
    from main import cli
    import tempfile
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmp:
        env, _, sensitivity_id = _setup_with_iterated_top(
            tmp, include_sensitivity_shape=True
        )
        result = runner.invoke(
            cli, ["crypto", "backtest-grid", "--grid", "sensitivity",
                  "--top-run-ids", sensitivity_id],
            env=env,
        )
    assert result.exit_code != 0
    assert "--allow-iterated" in result.output


def test_cli_sensitivity_allow_iterated_proceeds_with_warning():
    """--allow-iterated bypasses the guard; the CLI still warns
    loudly and prints the iterated bases. We use --dry-run so no
    actual lifecycle runs (the guard logic happens before the
    grid runs)."""
    from click.testing import CliRunner
    from main import cli
    import tempfile
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmp:
        env, _, sensitivity_id = _setup_with_iterated_top(
            tmp, include_sensitivity_shape=True
        )
        result = runner.invoke(
            cli, ["crypto", "backtest-grid", "--grid", "sensitivity",
                  "--top-run-ids", sensitivity_id,
                  "--allow-iterated", "--dry-run"],
            env=env,
        )
    # Whether the dry-run grid runs to completion depends on whether
    # the test DB has predictions to score. We only assert the guard
    # was bypassed and the warning fired.
    assert "--allow-iterated" in result.output
    assert "sensitivity-shape" in result.output
    assert sensitivity_id in result.output


def test_cli_sensitivity_no_guard_when_top_is_base_grid_only():
    """When the auto-selected top is entirely from the canonical
    base grid, the guard does NOT fire — even without
    --allow-iterated."""
    from click.testing import CliRunner
    from main import cli
    import tempfile
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmp:
        env, base_id, _ = _setup_with_iterated_top(
            tmp, include_sensitivity_shape=False
        )
        # The grid runs the actual sweep, which will fail for other
        # reasons in a temp DB without seeded predictions. We only
        # assert the guard does NOT fire (no KI-125 message).
        result = runner.invoke(
            cli, ["crypto", "backtest-grid", "--grid", "sensitivity",
                  "--top-run-ids", base_id, "--dry-run"],
            env=env,
        )
    assert "non-base-grid" not in result.output
    assert "KI-125" not in result.output
