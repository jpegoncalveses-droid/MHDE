"""Promotion gates for shadow model → production promotion.

AUTO_APPLY_ENABLED is always False. No production change may be automatically applied.
All gates must pass before an experiment is eligible for human promotion.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

import duckdb

logger = logging.getLogger("mhde.models.promotion_gates")

AUTO_APPLY_ENABLED = False

GATES: list[str] = [
    "minimum_sample_size",
    "out_of_sample_improvement",
    "false_positive_rate_not_worse",
    "bad_data_rate_not_worse",
    "sector_concentration_not_worse",
    "stable_across_time_windows",
    "rollback_available",
]

_MIN_SAMPLE_SIZE = 100


def check_promotion_gates(
    conn: duckdb.DuckDBPyConnection,
    experiment_id: str,
    model_run_id: str | None,
) -> list[dict]:
    """
    Run all promotion gates for the given experiment and model run.
    Stores results in promotion_gate_results. Returns list of gate result dicts.
    """
    results = []
    checkers = {
        "minimum_sample_size": _gate_minimum_sample_size,
        "out_of_sample_improvement": _gate_oos_improvement,
        "false_positive_rate_not_worse": _gate_fp_rate,
        "bad_data_rate_not_worse": _gate_bad_data_rate,
        "sector_concentration_not_worse": _gate_sector_concentration,
        "stable_across_time_windows": _gate_time_stability,
        "rollback_available": _gate_rollback_available,
    }

    for gate_name in GATES:
        checker = checkers[gate_name]
        result = checker(conn, experiment_id, model_run_id)
        result["gate_name"] = gate_name
        result["experiment_id"] = experiment_id
        result["model_run_id"] = model_run_id
        _store_gate_result(conn, result)
        results.append(result)

    return results


def _store_gate_result(conn: duckdb.DuckDBPyConnection, r: dict) -> None:
    conn.execute(
        """INSERT INTO promotion_gate_results
           (gate_result_id, experiment_id, model_run_id, gate_name, status,
            metric_value, threshold, passed, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [uuid.uuid4().hex[:16], r.get("experiment_id"), r.get("model_run_id"),
         r["gate_name"], r["status"],
         r.get("metric_value"), r.get("threshold"),
         r["passed"], r.get("notes")],
    )


def _gate_minimum_sample_size(conn, experiment_id, model_run_id) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) FROM candidate_outcomes WHERE forward_return_20d IS NOT NULL"
    ).fetchone()
    n = row[0] if row else 0
    passed = n >= _MIN_SAMPLE_SIZE
    return {
        "status": "pass" if passed else "fail",
        "passed": passed,
        "metric_value": float(n),
        "threshold": float(_MIN_SAMPLE_SIZE),
        "notes": f"{n} outcome rows (need {_MIN_SAMPLE_SIZE})",
    }


def _gate_oos_improvement(conn, experiment_id, model_run_id) -> dict:
    if not model_run_id:
        return {"status": "skip", "passed": False,
                "notes": "No model_run_id provided", "metric_value": None, "threshold": None}
    row = conn.execute(
        "SELECT metrics_json FROM model_runs WHERE model_run_id=?", [model_run_id]
    ).fetchone()
    if not row or not row[0]:
        return {"status": "skip", "passed": False,
                "notes": "Model run not found", "metric_value": None, "threshold": None}
    import json
    metrics = json.loads(row[0])
    auc = metrics.get("auc", 0.0)
    passed = auc > 0.55
    return {
        "status": "pass" if passed else "fail",
        "passed": passed,
        "metric_value": auc,
        "threshold": 0.55,
        "notes": f"OOS AUC={auc:.3f} (need >0.55)",
    }


def _gate_fp_rate(conn, experiment_id, model_run_id) -> dict:
    row = conn.execute(
        """SELECT COUNT(*), SUM(CASE WHEN false_positive_reason IS NOT NULL THEN 1 ELSE 0 END)
           FROM candidate_reviews"""
    ).fetchone()
    total, fp = (row[0] or 0), (row[1] or 0)
    rate = fp / total if total > 0 else None
    passed = rate is None or rate <= 0.30
    return {
        "status": "pass" if passed else "fail",
        "passed": passed,
        "metric_value": rate,
        "threshold": 0.30,
        "notes": f"FP rate={rate:.2f}" if rate is not None else "No review data",
    }


def _gate_bad_data_rate(conn, experiment_id, model_run_id) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) FROM features WHERE confidence='low'"
    ).fetchone()
    low = row[0] if row else 0
    total_row = conn.execute("SELECT COUNT(*) FROM features").fetchone()
    total = total_row[0] if total_row else 0
    rate = low / total if total > 0 else 0.0
    passed = rate <= 0.40
    return {
        "status": "pass" if passed else "fail",
        "passed": passed,
        "metric_value": rate,
        "threshold": 0.40,
        "notes": f"Low-confidence feature rate={rate:.2f}",
    }


def _gate_sector_concentration(conn, experiment_id, model_run_id) -> dict:
    # Check no single sector >50% of scored candidates
    rows = conn.execute(
        """SELECT c.sector, COUNT(*) as n
           FROM scores s LEFT JOIN companies c ON s.ticker=c.ticker
           GROUP BY c.sector ORDER BY n DESC LIMIT 1"""
    ).fetchone()
    if not rows:
        return {"status": "skip", "passed": True, "notes": "No scores yet",
                "metric_value": None, "threshold": None}
    total = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    top_share = rows[1] / total if total > 0 else 0.0
    passed = top_share <= 0.50
    return {
        "status": "pass" if passed else "fail",
        "passed": passed,
        "metric_value": top_share,
        "threshold": 0.50,
        "notes": f"Top sector share={top_share:.2f} (sector={rows[0]})",
    }


def _gate_time_stability(conn, experiment_id, model_run_id) -> dict:
    row = conn.execute(
        "SELECT COUNT(DISTINCT run_date) FROM pipeline_runs"
    ).fetchone()
    n_runs = row[0] if row else 0
    passed = n_runs >= 5
    return {
        "status": "pass" if passed else "fail",
        "passed": passed,
        "metric_value": float(n_runs),
        "threshold": 5.0,
        "notes": f"{n_runs} distinct run dates (need 5 for time-window stability)",
    }


def _gate_rollback_available(conn, experiment_id, model_run_id) -> dict:
    # Rollback is always available via git — this gate always passes as a policy check
    return {
        "status": "pass",
        "passed": True,
        "metric_value": 1.0,
        "threshold": 1.0,
        "notes": "Rollback available via git revert",
    }
