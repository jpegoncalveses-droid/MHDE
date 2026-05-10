"""Build and write data/exports/predictions_YYYY-MM-DD.json + symlink
per INTERFACE.md §3.

The exporter does its OWN inference on the full active universe — it
does NOT read crypto_ml_predictions, which is filtered/capped by
score_universe()'s threshold logic. Two preflight gates:

  1. Staleness — MAX(trade_date) FROM crypto_ml_features must equal
     prediction_date (strict today-only).
  2. Coverage — every active universe symbol must have a feature row
     for prediction_date.

Failure raises ExportPreflightError; no output files are touched.
The engine handles the resulting stale predictions_latest.json per
INTERFACE.md §5.3.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import joblib
import numpy as np

from crypto.config import FEATURE_COLS
from crypto.exports._io import (
    EXPORTS_DIR, atomic_write_json, atomic_replace_symlink,
)

logger = logging.getLogger("mhde.exports.predictions")


class ExportPreflightError(Exception):
    """Raised when preflight gates fail.

    Caller (CLI) should log the message and exit non-zero. No output
    files have been touched at the point this is raised.
    """


def _today_utc() -> date:
    return datetime.now(tz=timezone.utc).date()


def _resolve_active_10d_model(conn) -> dict:
    rows = conn.execute(
        """
        SELECT model_id, horizon, model_path
        FROM crypto_ml_model_runs
        WHERE is_active = true
          AND horizon = '10d'
          AND model_id NOT LIKE 'crypto_%_walkfold_%'
        """
    ).fetchall()
    if len(rows) == 0:
        raise ExportPreflightError("no active 10d model in crypto_ml_model_runs")
    if len(rows) > 1:
        ids = ", ".join(r[0] for r in rows)
        raise ExportPreflightError(
            f"more than one active 10d model: {ids}"
        )
    model_id, horizon, model_path = rows[0]
    return {"model_id": model_id, "horizon": horizon, "model_path": model_path}


def _check_freshness_and_coverage(conn, prediction_date: date) -> list[str]:
    """Run the two preflight gates. Returns the list of active-universe
    symbols (in deterministic order) on success; raises
    ExportPreflightError otherwise."""
    max_row = conn.execute(
        "SELECT MAX(trade_date) FROM crypto_ml_features"
    ).fetchone()
    max_trade_date = max_row[0] if max_row else None
    if max_trade_date != prediction_date:
        raise ExportPreflightError(
            f"features stale: MAX(trade_date)={max_trade_date}, "
            f"expected {prediction_date}. Check "
            f"mhde-crypto-predict.service status."
        )

    rows = conn.execute(
        """
        SELECT u.symbol
        FROM crypto_universe u
        LEFT JOIN crypto_ml_features f
          ON f.symbol = u.symbol AND f.trade_date = ?
        WHERE u.is_active = true AND f.symbol IS NULL
        ORDER BY u.symbol
        """,
        [prediction_date],
    ).fetchall()
    missing = [r[0] for r in rows]
    if missing:
        raise ExportPreflightError(
            f"missing features for {len(missing)} active universe "
            f"symbol(s) on {prediction_date}: {', '.join(missing)}"
        )

    symbols = conn.execute(
        "SELECT symbol FROM crypto_universe WHERE is_active = true ORDER BY symbol"
    ).fetchall()
    return [r[0] for r in symbols]


def _load_features(conn, symbols, prediction_date: date):
    feature_select = ", ".join(f"f.{c}" for c in FEATURE_COLS)
    placeholders = ", ".join(["?"] * len(symbols))
    return conn.execute(
        f"""
        SELECT f.symbol, {feature_select}
        FROM crypto_ml_features f
        WHERE f.trade_date = ?
          AND f.symbol IN ({placeholders})
        """,
        [prediction_date] + list(symbols),
    ).fetchdf()


def build_predictions(
    conn: duckdb.DuckDBPyConnection,
    prediction_date: date | None = None,
) -> dict:
    """Construct the predictions dict per INTERFACE.md §3.

    Steps:
      1. Resolve prediction_date (default today UTC).
      2. Resolve active 10d model.
      3. Run preflight gates (staleness + 100% coverage).
      4. Load features, run model + Platt calibration.
      5. Sort descending, assign ranks 1..N.
    """
    if prediction_date is None:
        prediction_date = _today_utc()

    model_info = _resolve_active_10d_model(conn)
    symbols = _check_freshness_and_coverage(conn, prediction_date)

    features_df = _load_features(conn, symbols, prediction_date)

    bundle = joblib.load(model_info["model_path"])
    model = bundle["model"]
    platt = bundle["platt"]
    medians = bundle.get("medians", {}) or {}

    X = features_df[FEATURE_COLS].copy()
    for col in FEATURE_COLS:
        X[col] = X[col].fillna(medians.get(col, 0))

    raw = model.predict_proba(X)[:, 1].reshape(-1, 1)
    cal = platt.predict_proba(raw)[:, 1]

    # midnight UTC of the prediction date — deterministic per
    # INTERFACE.md §3.1 ("when MHDE generated this prediction"). The
    # actual wall-clock varies by timer fire time; engine cares about
    # the date.
    predicted_at = datetime.combine(
        prediction_date, datetime.min.time(), tzinfo=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = []
    for sym, prob in zip(features_df["symbol"].tolist(), cal.tolist()):
        rows.append((sym, float(prob)))
    rows.sort(key=lambda x: x[1], reverse=True)
    predictions = [
        {
            "symbol": sym,
            "probability": prob,
            "rank": idx + 1,
            "predicted_at": predicted_at,
        }
        for idx, (sym, prob) in enumerate(rows)
    ]

    return {
        "export_date": prediction_date.isoformat(),
        "generated_at": datetime.now(tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "model_id": model_info["model_id"],
        "horizon_days": int(model_info["horizon"].rstrip("d")),
        "n_predictions": len(predictions),
        "predictions": predictions,
    }


def write(
    conn: duckdb.DuckDBPyConnection,
    prediction_date: date | None = None,
    output_dir: Path = EXPORTS_DIR,
    dry_run: bool = False,
) -> dict:
    """Build + atomically write the dated file + replace symlink.

    On preflight failure: raises ExportPreflightError before any file
    is touched.
    """
    payload = build_predictions(conn, prediction_date)
    output_dir = Path(output_dir)
    if dry_run:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return payload
    dated_name = f"predictions_{payload['export_date']}.json"
    dated_path = output_dir / dated_name
    latest_path = output_dir / "predictions_latest.json"
    atomic_write_json(dated_path, payload)
    atomic_replace_symlink(latest_path, dated_name)
    logger.info(
        "wrote %s (n=%d) and updated symlink %s",
        dated_path, payload["n_predictions"], latest_path,
    )
    return payload
