"""Tests for crypto.exports.write_active_spec.

Schema-conformance against INTERFACE.md §2 + integration with
synthetic crypto_backtest_summary / crypto_backtest_runs /
crypto_backtest_trades rows so simulate_portfolio runs end-to-end.
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import patch

import pytest

from crypto.exports import write_active_spec, spec_config
from crypto.exports.hashing import compute_spec_hash
from crypto.execution.backtest.harness import ensure_backtest_tables


def _seed_phase1b_winner(conn):
    """Insert the rows write_active_spec needs.

    A small set of trades (10) is enough for simulate_portfolio to
    return a non-degenerate PortfolioResult.
    """
    ensure_backtest_tables(conn)
    run_id = spec_config.PHASE1B_WINNER_RUN_ID
    conn.execute(
        "INSERT INTO crypto_backtest_runs ("
        "  run_id, horizon, exit_policy, selection_rule, parameters,"
        "  date_start, date_end, n_trades"
        ") VALUES (?, '10d', 'D', 'top_n', "
        "  '{\"policy_params\":{\"trail_pct\":0.3},\"selection_params\":{\"n\":6}}',"
        "  DATE '2025-04-05', DATE '2026-05-07', 10)",
        [run_id],
    )
    conn.execute(
        "INSERT INTO crypto_backtest_summary ("
        "  run_id, net_pnl_total_pct, net_pnl_annualized_pct, sharpe_ratio,"
        "  max_drawdown_pct, hit_rate, profit_factor, avg_holding_days,"
        "  pct_exits_trailing"
        ") VALUES (?, 51.2, 47.0, 6.32, -0.17, 0.871, 3.13, 3.66, 0.87)",
        [run_id],
    )
    # Seed 10 staggered winners (entry on day i, exit on day i+1, all
    # +5%). Distinct close-days produce a monotonically-increasing
    # equity curve → finite positive Sharpe + zero drawdown, which the
    # backtest_expectations assertions below depend on.
    from datetime import timedelta
    for i in range(10):
        entry = date(2025, 4, 5) + timedelta(days=i)
        exit_d = entry + timedelta(days=1)
        conn.execute(
            "INSERT INTO crypto_backtest_trades ("
            "  run_id, trade_id, coin, entry_date, entry_price,"
            "  exit_date, exit_price, exit_reason, holding_days,"
            "  net_pnl_pct, probability_at_entry"
            ") VALUES (?, ?, 'BTCUSDT',"
            "  ?, 60000.0,"
            "  ?, 63000.0, 'trailing', 1,"
            "  0.05, 0.80)",
            [run_id, f"t{i}", entry, exit_d],
        )


def test_build_spec_includes_all_required_top_level_fields(temp_db):
    _seed_phase1b_winner(temp_db)
    spec = write_active_spec.build_spec(temp_db)
    required = {
        "spec_version", "spec_hash", "generated_at", "generated_by_mhde_commit",
        "phase_0_status", "phase_1b_winner", "sizing", "risk", "universe",
        "runtime", "backtest_expectations",
    }
    assert required <= set(spec.keys())


def test_build_spec_phase1b_winner_pulled_from_db(temp_db):
    _seed_phase1b_winner(temp_db)
    spec = write_active_spec.build_spec(temp_db)
    w = spec["phase_1b_winner"]
    assert w["run_id"] == spec_config.PHASE1B_WINNER_RUN_ID
    assert w["horizon_days"] == 10
    assert w["exit_policy"] == "D"
    assert w["selection_mode"] == "top_n"
    assert w["selection_n"] == 6
    assert w["trail_pct"] == 0.30
    assert w["activation_pct"] == 0.01


def test_build_spec_sizing_passes_validation(temp_db):
    _seed_phase1b_winner(temp_db)
    s = write_active_spec.build_spec(temp_db)["sizing"]
    assert s["deploy_pct"] + s["reserve_pct"] == 1.0
    assert s["leverage"] in (1.0, 2.0)


def test_build_spec_risk_values_match_config(temp_db):
    _seed_phase1b_winner(temp_db)
    r = write_active_spec.build_spec(temp_db)["risk"]
    assert r == spec_config.RISK


def test_build_spec_phase_0_status_defaults_to_interim(temp_db):
    """No phase0 verdict computed (no outcome-filled predictions) →
    evaluate_all returns INTERIM → lowercased to 'interim'."""
    _seed_phase1b_winner(temp_db)
    spec = write_active_spec.build_spec(temp_db)
    assert spec["phase_0_status"] == "interim"


def test_build_spec_hash_is_self_consistent(temp_db):
    _seed_phase1b_winner(temp_db)
    spec = write_active_spec.build_spec(temp_db)
    declared = spec["spec_hash"]
    recomputed = compute_spec_hash(spec)
    assert declared == recomputed


def test_build_spec_backtest_expectations_pulled_from_simulate_portfolio(temp_db):
    _seed_phase1b_winner(temp_db)
    spec = write_active_spec.build_spec(temp_db)
    e = spec["backtest_expectations"]
    # All seeded trades are winners (+5%) → portfolio Sharpe is finite
    # and positive, max_dd_pct == 0 (no drawdown), n_trades == 10.
    assert e["portfolio_sharpe"] > 0
    assert e["portfolio_max_dd_pct"] <= 0  # fraction (negative or zero)
    assert e["expected_hit_rate"] == pytest.approx(0.871)
    assert e["expected_n_trades_per_year"] >= 1
    assert e["divergence_alert_threshold_pct"] == 0.20


def test_build_spec_raises_when_winner_row_missing(temp_db):
    ensure_backtest_tables(temp_db)
    # No insert. PHASE1B_WINNER_RUN_ID has no rows.
    with pytest.raises(write_active_spec.ExportSpecError, match="not found"):
        write_active_spec.build_spec(temp_db)


def test_write_creates_file_with_valid_json_and_hash(temp_db, tmp_path):
    _seed_phase1b_winner(temp_db)
    out = tmp_path / "active_spec.json"
    write_active_spec.write(temp_db, output_path=out)
    payload = json.loads(out.read_text())
    assert payload["spec_hash"] == compute_spec_hash(payload)
    assert payload["spec_version"] == "1.0.0"


def test_write_dry_run_does_not_create_file(temp_db, tmp_path, capsys):
    _seed_phase1b_winner(temp_db)
    out = tmp_path / "active_spec.json"
    write_active_spec.write(temp_db, output_path=out, dry_run=True)
    assert not out.exists()
    captured = capsys.readouterr()
    assert "spec_hash" in captured.out
