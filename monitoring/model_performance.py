"""Monitor: rolling 7-day precision per engine vs walk-forward baseline.

For each active model in `*_model_runs`, compute:
  - rolling 7d precision = hits / total over predictions whose
    outcome_filled_at lies within the last 7 days.
  - baseline = the model's `precision_at_threshold` from training.

Alert if the rolling precision drops below 0.8x of baseline (the
threshold called out in HARDENING_PLAN.md Session 6).

Schedule: daily.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from monitoring.alert import MonitorResult, send_alert

logger = logging.getLogger("mhde.monitoring.model_performance")

DEGRADATION_THRESHOLD = 0.8  # alert if rolling < 0.8 * baseline


def _check_engine(conn, engine: str, table_pred: str, table_runs: str,
                  date_col: str) -> dict[str, Any]:
    """Return dict for one engine with active model precision check.

    Note: ml_model_runs and crypto_ml_model_runs use `target_threshold`
    (probability cutoff for the binary label); fx_ml_model_runs uses
    `target_pips` (pip move threshold). We don't actually consume the
    target field — only the precision baseline — so we project NULL.
    """
    out: dict[str, Any] = {"engine": engine}

    target_col = "target_pips" if engine == "fx" else "target_threshold"
    active = conn.execute(f"""
        SELECT model_id, horizon, {target_col}, precision_at_threshold
        FROM {table_runs}
        WHERE is_active = true
    """).fetchall()
    if not active:
        out["status"] = "skip"
        out["reason"] = "no active model"
        return out

    rows: list[dict[str, Any]] = []
    for model_id, horizon, target, baseline in active:
        # Rolling precision over the last 7 days of filled outcomes.
        result = conn.execute(f"""
            SELECT
                COUNT(*) AS n_filled,
                SUM(CASE WHEN actual_hit THEN 1 ELSE 0 END) AS n_hit
            FROM {table_pred}
            WHERE model_id = ?
              AND outcome_filled_at IS NOT NULL
              AND outcome_filled_at >= CURRENT_TIMESTAMP - INTERVAL '7 days'
        """, [model_id]).fetchone()
        n_filled, n_hit = result if result else (0, 0)
        if not n_filled or n_filled < 5:
            rows.append({
                "model_id": model_id,
                "horizon": horizon,
                "n_filled": n_filled,
                "status": "skip",
                "reason": "n_filled<5 — not enough data yet",
            })
            continue
        rolling = n_hit / n_filled if n_filled else 0
        if baseline is None or baseline <= 0:
            rows.append({
                "model_id": model_id,
                "horizon": horizon,
                "n_filled": n_filled,
                "status": "skip",
                "reason": "baseline missing in *_model_runs",
            })
            continue
        if baseline >= 0.95:
            # FX train stores `precision_top10` (top-10 picks per fold)
            # in `precision_at_threshold`, which is ~0.99 by construction
            # and not comparable to a real rolling precision. Skip rather
            # than fire false alerts. Tracking as KI-011 follow-up — fix
            # the storage convention in fx/ml/train.py so this guard can
            # be removed.
            rows.append({
                "model_id": model_id,
                "horizon": horizon,
                "n_filled": n_filled,
                "rolling_precision": round(rolling, 3),
                "baseline": round(baseline, 3),
                "status": "skip",
                "reason": "baseline >= 0.95 (sentinel/non-comparable; KI-011)",
            })
            continue
        ratio = rolling / baseline
        rows.append({
            "model_id": model_id,
            "horizon": horizon,
            "n_filled": n_filled,
            "rolling_precision": round(rolling, 3),
            "baseline": round(baseline, 3),
            "ratio": round(ratio, 2),
            "status": "fail" if ratio < DEGRADATION_THRESHOLD else "ok",
        })
    out["status"] = "ok"
    out["models"] = rows
    return out


def run(conn=None) -> MonitorResult:
    started = datetime.now(timezone.utc)

    close_conn = False
    if conn is None:
        from storage.config import load_engine_config
        import duckdb
        cfg = load_engine_config()
        conn = duckdb.connect(cfg["db_path"], read_only=True)
        close_conn = True

    try:
        engines = [
            ("equity", "ml_predictions", "ml_model_runs", "prediction_date"),
            ("crypto", "crypto_ml_predictions", "crypto_ml_model_runs", "prediction_date"),
            ("fx", "fx_ml_predictions", "fx_ml_model_runs", "datetime_utc"),
        ]
        per_engine: dict[str, Any] = {}
        problems: list[str] = []
        for engine, pred, runs, dcol in engines:
            r = _check_engine(conn, engine, pred, runs, dcol)
            per_engine[engine] = r
            for m in r.get("models", []) or []:
                if m.get("status") == "fail":
                    problems.append(
                        f"{engine}/{m['model_id']} ({m['horizon']}): "
                        f"rolling={m['rolling_precision']:.3f} vs baseline="
                        f"{m['baseline']:.3f} (ratio={m['ratio']:.2f})"
                    )

        finished = datetime.now(timezone.utc)
        if problems:
            return MonitorResult(
                monitor="model_performance",
                status="warn",
                severity="warn",
                title=f"Model precision degraded (< {DEGRADATION_THRESHOLD:.0%} of baseline)",
                body="\n".join(f"- {p}" for p in problems),
                metrics={"per_engine": per_engine,
                         "threshold": DEGRADATION_THRESHOLD},
                started_at=started, finished_at=finished,
            )
        return MonitorResult(
            monitor="model_performance",
            status="ok",
            severity="info",
            title="all active models within baseline precision band",
            metrics={"per_engine": per_engine},
            started_at=started, finished_at=finished,
        )
    finally:
        if close_conn:
            conn.close()


def main() -> int:
    result = run()
    send_alert(result)
    return 0 if result.status == "ok" else 1
