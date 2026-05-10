"""Build and write data/exports/active_spec.json per INTERFACE.md §2.

Reads:
  - crypto_backtest_summary, crypto_backtest_runs, crypto_backtest_trades
    (Phase 1B winner — run_id from spec_config.PHASE1B_WINNER_RUN_ID)
  - crypto_ml_predictions (via phase0_evaluate.evaluate_all for verdict)

Does not write to DB. Atomic file write via _io.atomic_write_json.
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from crypto.exports import spec_config
from crypto.exports._io import EXPORTS_DIR, atomic_write_json
from crypto.exports.hashing import compute_spec_hash

logger = logging.getLogger("mhde.exports.spec")

ACTIVE_SPEC_PATH = EXPORTS_DIR / "active_spec.json"

# Phase0 model id — the active 10d model the engine cares about.
# Hardcoded because the export is Phase 1B-winner-specific.
PHASE0_MODEL_ID = "crypto_10d_db171418"


class ExportSpecError(Exception):
    """Raised when the spec cannot be built (missing winner row, etc.)."""


def _git_short_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd="/home/jpcg/MHDE", stderr=subprocess.DEVNULL,
        )
        return out.decode("utf-8").strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _phase_0_status(conn) -> str:
    """Lowercase the active 10d model's Phase0Verdict.overall.

    Returns one of "passed", "failed", "interim". Engine in live mode
    requires "passed".
    """
    from crypto.ml.phase0_evaluate import evaluate_all, CRYPTO
    verdicts = evaluate_all(conn, engine=CRYPTO, model_id=PHASE0_MODEL_ID)
    if not verdicts:
        return "interim"
    overall = verdicts[0].overall  # "PASS" | "FAIL" | "INTERIM"
    return {"PASS": "passed", "FAIL": "failed", "INTERIM": "interim"}[overall]


def _phase1b_winner_fields(conn) -> dict:
    run_id = spec_config.PHASE1B_WINNER_RUN_ID
    row = conn.execute(
        "SELECT horizon, exit_policy, selection_rule, parameters "
        "FROM crypto_backtest_runs WHERE run_id = ?",
        [run_id],
    ).fetchone()
    if row is None:
        raise ExportSpecError(
            f"Phase 1B winner run_id={run_id} not found in crypto_backtest_runs"
        )
    horizon, exit_policy, selection_rule, params_json = row
    params = json.loads(params_json) if params_json else {}
    policy_params = params.get("policy_params", {}) or {}
    selection_params = params.get("selection_params", {}) or {}

    horizon_days = int(horizon.rstrip("d"))
    winner = {
        "run_id": run_id,
        "horizon_days": horizon_days,
        "exit_policy": exit_policy,
        "selection_mode": selection_rule,
        "trail_pct": float(policy_params.get("trail_pct", 0.30)),
        "activation_pct": float(policy_params.get("activation_pct", 0.01)),
    }
    if selection_rule == "top_n":
        winner["selection_n"] = int(selection_params.get("n", 6))
    elif selection_rule == "threshold":
        winner["selection_threshold"] = float(selection_params.get("threshold", 0.55))
    return winner


def _backtest_expectations(conn) -> dict:
    """Map simulate_portfolio output + summary.hit_rate to the
    INTERFACE.md §2 backtest_expectations fields.

    Unit transforms (pinned by tests; see spec §5.4):
      - portfolio_sharpe         ← result.sharpe_ratio (passthrough)
      - portfolio_max_dd_pct     ← result.max_drawdown_pct / 100 (→ fraction)
      - expected_hit_rate        ← summary.hit_rate (passthrough fraction)
      - expected_annualized_return_pct ← result.annualized_return_pct (percentage)
      - expected_n_trades_per_year     ← round(n_trades_taken / span_days × 365)
    """
    from crypto.execution.backtest.report import simulate_portfolio
    run_id = spec_config.PHASE1B_WINNER_RUN_ID

    result = simulate_portfolio(
        conn, run_id=run_id,
        starting_capital=1000.0, max_positions=6,
        deploy_fraction=0.8, leverage=1.0,
    )
    hit_rate = conn.execute(
        "SELECT hit_rate FROM crypto_backtest_summary WHERE run_id = ?",
        [run_id],
    ).fetchone()
    if hit_rate is None:
        raise ExportSpecError(
            f"Phase 1B winner run_id={run_id} has no row in "
            f"crypto_backtest_summary"
        )
    hit_rate_value = float(hit_rate[0]) if hit_rate[0] is not None else 0.0
    n_trades_per_year = (
        round(result.n_trades_taken / result.span_days * 365)
        if result.span_days > 0 else 0
    )
    return {
        "portfolio_sharpe": float(result.sharpe_ratio),
        "portfolio_max_dd_pct": float(result.max_drawdown_pct) / 100.0,
        "expected_hit_rate": hit_rate_value,
        "expected_annualized_return_pct": float(result.annualized_return_pct),
        "expected_n_trades_per_year": n_trades_per_year,
        "divergence_alert_threshold_pct": spec_config.DIVERGENCE_ALERT_THRESHOLD_PCT,
    }


def build_spec(conn: duckdb.DuckDBPyConnection) -> dict:
    """Assemble the active_spec.json dict, hash-filled."""
    winner = _phase1b_winner_fields(conn)
    expectations = _backtest_expectations(conn)
    phase0 = _phase_0_status(conn)

    spec = {
        "spec_version": spec_config.SPEC_VERSION,
        "spec_hash": "",  # filled below
        "generated_at": datetime.now(tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "generated_by_mhde_commit": _git_short_sha(),
        "phase_0_status": phase0,
        "phase_1b_winner": winner,
        "sizing": dict(spec_config.SIZING),
        "risk": dict(spec_config.RISK),
        "universe": dict(spec_config.UNIVERSE),
        "runtime": dict(spec_config.RUNTIME),
        "backtest_expectations": expectations,
    }
    spec["spec_hash"] = compute_spec_hash(spec)
    return spec


def write(
    conn: duckdb.DuckDBPyConnection,
    output_path: Path = ACTIVE_SPEC_PATH,
    dry_run: bool = False,
) -> dict:
    spec = build_spec(conn)
    if dry_run:
        print(json.dumps(spec, indent=2, sort_keys=True))
        return spec
    atomic_write_json(output_path, spec)
    logger.info(
        "wrote %s (spec_hash=%s, version=%s)",
        output_path, spec["spec_hash"], spec["spec_version"],
    )
    return spec
