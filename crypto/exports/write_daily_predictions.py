"""Build and write data/exports/predictions_YYYY-MM-DD.json + symlink
per INTERFACE.md §3.

The exporter does its OWN inference on the active universe — it does
NOT read crypto_ml_predictions, which is filtered/capped by
score_universe()'s threshold logic. One preflight gate:

  1. Staleness — MAX(trade_date) FROM crypto_ml_features must be the
     export date OR the export date minus one day. The cap-at-today-1
     OHLCV ingestion fix (commit 8f9d707) means the daily pipeline only
     ever ingests fully-closed UTC days, so on a normal run
     MAX(trade_date) is structurally export_date - 1: today's export is
     driven by yesterday's closed candles and intended to drive *today's*
     trades. We still accept export_date itself for the rarer case where
     the day's candle is already ingested by export time (e.g. a manual
     late-UTC re-run). Anything older than export_date - 1 is genuine
     pipeline staleness and aborts the export. See KI-138.

The previous per-symbol coverage check was removed in favor of letting
build_predictions emit predictions for whatever active-universe
symbols have features on the features-as-of date. Newly-added universe
symbols are in their 60-day features warmup window and have no features
yet — that's normal, not a pipeline failure. See KI-129.

The JSON carries two dates: `export_date` (= today UTC — the trading
date the predictions drive, per INTERFACE.md §3.1) and the informational
`features_as_of_date` (= MAX(trade_date) used for inference; today-1 on
a normal cap-at-today-1 run). The engine validates only `export_date`
against today UTC; `features_as_of_date` is for downstream consumers /
debugging.

Failure raises ExportPreflightError; no output files are touched.
The engine handles the resulting stale predictions_latest.json per
INTERFACE.md §5.3.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import joblib
import numpy as np

from crypto.config import FEATURE_COLS
from crypto.exports._io import (
    EXPORTS_DIR, atomic_write_json, atomic_replace_symlink,
)
from crypto.ml.postparabolic_filter import should_exclude

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


def _check_freshness(conn, export_date: date) -> date:
    """Staleness gate. Returns the features-as-of date — MAX(trade_date)
    FROM crypto_ml_features — after validating it is either `export_date`
    or `export_date - 1`. Raises ExportPreflightError otherwise.

    `export_date - 1` is the structural normal: the cap-at-today-1 OHLCV
    ingestion fix (commit 8f9d707) only ever ingests fully-closed UTC
    days, so when the daily exporter runs at ~00:30 UTC the freshest
    features are for yesterday. We still accept `export_date` itself for
    the rarer case where the day's candle is already ingested by export
    time (a manual late-UTC re-run). Anything older than `export_date - 1`
    is genuine pipeline staleness. See KI-138.

    The per-symbol coverage check that this function used to do was
    removed in favor of letting build_predictions emit predictions for
    whatever active-universe symbols have features on the features-as-of
    date. Newly-added universe symbols are in their 60-day features
    warmup window and have no features yet — that's normal, not a
    pipeline failure. See KI-129.
    """
    max_row = conn.execute(
        "SELECT MAX(trade_date) FROM crypto_ml_features"
    ).fetchone()
    max_trade_date = max_row[0] if max_row else None
    if max_trade_date not in (export_date, export_date - timedelta(days=1)):
        raise ExportPreflightError(
            f"features stale: MAX(trade_date)={max_trade_date}, expected "
            f"{export_date} or {export_date - timedelta(days=1)} "
            f"(cap-at-today-1 ingestion). Check "
            f"mhde-crypto-predict.service status."
        )
    return max_trade_date


def _load_features(conn, features_as_of_date: date):
    """Load features for `features_as_of_date` restricted to active
    universe symbols. Symbols in the active universe but missing features
    (warmup window) are silently absent — see KI-129 / spec §5.5."""
    feature_select = ", ".join(f"f.{c}" for c in FEATURE_COLS)
    return conn.execute(
        f"""
        SELECT f.symbol, {feature_select}
        FROM crypto_ml_features f
        JOIN crypto_universe u ON u.symbol = f.symbol
        WHERE f.trade_date = ?
          AND u.is_active = true
        ORDER BY f.symbol
        """,
        [features_as_of_date],
    ).fetchdf()


def build_predictions(
    conn: duckdb.DuckDBPyConnection,
    prediction_date: date | None = None,
) -> dict:
    """Construct the predictions dict per INTERFACE.md §3.

    `prediction_date` is the *export date* — the trading date these
    predictions drive (default today UTC). Inference itself runs on the
    freshest features, which under the cap-at-today-1 ingestion fix is
    `prediction_date - 1`; that date is carried back to the caller as
    `features_as_of_date`. See KI-138.

    Steps:
      1. Resolve the export date (default today UTC).
      2. Resolve active 10d model.
      3. Run preflight gate (staleness only — see KI-129) → it returns
         the features-as-of date (export_date or export_date - 1).
      4. Load features for the features-as-of date (active universe ∩
         has-features-on-that-date).
      5. Run model + Platt calibration.
      6. Apply the post-parabolic exclusion filter — drop suppressed
         coins, record them in crypto_signal_exclusions, log each one.
      7. Sort descending over the survivors, assign ranks 1..N
         (consecutive). An all-excluded day yields an empty list (the
         engine then skips entry + alerts per INTERFACE.md §3.2 / §5.3).
    """
    if prediction_date is None:
        prediction_date = _today_utc()

    model_info = _resolve_active_10d_model(conn)
    features_as_of_date = _check_freshness(conn, prediction_date)

    features_df = _load_features(conn, features_as_of_date)
    if features_df.empty:
        raise ExportPreflightError(
            f"no predictable symbols for features date {features_as_of_date} "
            f"(export_date {prediction_date}): crypto_ml_features has rows "
            f"for the date but none match crypto_universe.is_active=true. "
            f"This indicates a universe or features-pipeline misconfiguration."
        )

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

    # ── Post-parabolic exclusion filter (option (b); see
    # crypto/ml/postparabolic_filter.py and POSTPARABOLIC_FILTER_SPEC.md) ──
    # dd90 / ret60 / ret5 are read from the *raw* feature row, NOT the
    # median-filled X, so a warmup-window symbol with NULL features fails
    # open per-input (Rule A and Rule B each evaluate independently against
    # their own inputs). A coin that trips either rule is dropped from the
    # export and recorded in crypto_signal_exclusions; the raw probability
    # is left untouched in crypto_ml_predictions (written separately by
    # score_universe). ADR-028 added Rule B (ret5 < -0.30).
    dd90s = features_df["drawdown_from_90d_high"].tolist()
    ret60s = features_df["return_60d"].tolist()
    ret5s = features_df["return_5d"].tolist()
    rows = []
    n_excluded = 0
    for sym, prob, dd90, ret60, ret5 in zip(
        features_df["symbol"].tolist(), cal.tolist(), dd90s, ret60s, ret5s
    ):
        excluded, reason = should_exclude(dd90, ret60, ret5)
        if not excluded:
            rows.append((sym, float(prob)))
            continue
        n_excluded += 1
        # ret5 is logged as -999.0 when NULL/NaN (warmup) so the printf
        # format never breaks; the column in crypto_signal_exclusions stays
        # NULL via the float-or-None coercion below.
        ret5_for_log = (
            float(ret5) if ret5 is not None and not (
                isinstance(ret5, float) and ret5 != ret5
            ) else -999.0
        )
        logger.warning(
            "postparabolic_exclude symbol=%s export_date=%s model_id=%s "
            "drawdown_from_90d_high=%.4f return_60d=%.4f return_5d=%.4f "
            "raw_probability=%.4f reason=%s",
            sym, prediction_date, model_info["model_id"],
            float(dd90) if dd90 is not None else -999.0,
            float(ret60) if ret60 is not None else -999.0,
            ret5_for_log,
            float(prob), reason,
        )

        def _to_db(v):
            if v is None:
                return None
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return None if f != f else f  # NaN → NULL

        conn.execute(
            """
            INSERT INTO crypto_signal_exclusions
                (export_date, symbol, model_id, raw_probability,
                 dd90, ret60, ret5, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (export_date, symbol, model_id) DO UPDATE SET
                raw_probability = excluded.raw_probability,
                dd90 = excluded.dd90,
                ret60 = excluded.ret60,
                ret5 = excluded.ret5,
                reason = excluded.reason
            """,
            [prediction_date, sym, model_info["model_id"], float(prob),
             _to_db(dd90), _to_db(ret60), _to_db(ret5), reason],
        )

    if not rows:
        logger.warning(
            "postparabolic_filter excluded all %d candidate(s) for %s — "
            "emitting an empty predictions list; the engine will skip entry "
            "today and alert (INTERFACE.md §3.2 / §5.3)",
            n_excluded, prediction_date,
        )

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
        # Informational: the MAX(trade_date) in crypto_ml_features used
        # for inference. Under the cap-at-today-1 ingestion fix this is
        # export_date - 1 on a normal run. The engine doesn't validate
        # this field; it's for downstream consumers / debugging (KI-138).
        "features_as_of_date": features_as_of_date.isoformat(),
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
