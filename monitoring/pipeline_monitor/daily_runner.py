"""Daily per-pipeline monitor.

Runs every step's outcome check for one pipeline (crypto / equity / fx), in
order, with the first RED short-circuiting the rest (the production pipelines
are strictly sequential), then posts **one** plain-text Telegram message:

    🟢/🔴 <Pipeline> Pipeline <date> <HH:MM UTC>
    🟢/🔴/⚪ <step> [— <detail>]
    ...

The message is sent every run, green or red, via ``monitoring.alert.send_text``
(``MONITORING_DRY_RUN=true`` suppresses the real send). Exit status: 0 if the
pipeline is green, 1 if any step is red — so ``systemctl status`` shows red
runs as failed.

CLI: ``main.py monitor {crypto,equity,fx}-pipeline``. Systemd:
``mhde-{crypto,equity,fx}-pipeline-monitor.{service,timer}``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from monitoring import alert
from monitoring.pipeline_monitor.checks import crypto as C
from monitoring.pipeline_monitor.checks import equity as E
from monitoring.pipeline_monitor.checks import fx as F
from monitoring.pipeline_monitor.core import (
    PipelineResult,
    evaluate_steps,
    render_telegram_message,
)

logger = logging.getLogger("mhde.monitoring.pipeline_monitor.daily")

DISPLAY_NAME = {"crypto": "Crypto", "equity": "Equity", "fx": "FX"}


def _open_mhde_db():
    import duckdb
    from storage.config import load_engine_config

    return duckdb.connect(load_engine_config()["db_path"], read_only=True)


# ── step wiring ───────────────────────────────────────────────────────
def _crypto_steps(mhde_conn, engine_conn, now: datetime, exports_dir, spec_path):
    return [
        (C.OHLCV_INGESTION, lambda: C.check_ohlcv_ingestion(mhde_conn, now)),
        (C.DATA_QUALITY_GUARD, lambda: C.check_data_quality_guard(mhde_conn, now)),
        (C.FUNDING_OI_INGESTION, lambda: C.check_funding_oi_ingestion(mhde_conn, now)),
        (C.FEATURE_PIPELINE, lambda: C.check_feature_pipeline(mhde_conn, now)),
        (C.MODEL_PREDICTIONS, lambda: C.check_model_predictions(mhde_conn, now)),
        (C.OUTCOME_TAGGING, lambda: C.check_outcome_tagging(mhde_conn, now)),
        (C.EXPORT_PREDICTIONS, lambda: C.check_export_predictions(now, exports_dir=exports_dir)),
        (C.ENGINE_INGEST, lambda: C.check_engine_ingest(engine_conn, now)),
        (C.ENGINE_POSITIONS, lambda: C.check_engine_positions(engine_conn, now, spec_path=spec_path)),
    ]


def _equity_steps(mhde_conn, now: datetime, dashboard_marker):
    return [
        (E.EQUITY_INGESTION, lambda: E.check_data_ingestion(mhde_conn, now)),
        (E.EQUITY_FEATURES, lambda: E.check_feature_pipeline(mhde_conn, now)),
        (E.EQUITY_PREDICTIONS, lambda: E.check_model_predictions(mhde_conn, now)),
        (E.EQUITY_DASHBOARD, lambda: E.check_dashboard_refresh(now, marker_path=dashboard_marker)),
    ]


def _fx_steps(mhde_conn, now: datetime):
    return [
        (F.FX_BAR_INGESTION, lambda: F.check_bar_ingestion(mhde_conn, now)),
        (F.FX_SIGNAL_GENERATION, lambda: F.check_signal_generation(mhde_conn, now)),
    ]


# ── runner ────────────────────────────────────────────────────────────
def run_pipeline(
    pipeline: str,
    *,
    mhde_conn=None,
    engine_conn=None,
    now: Optional[datetime] = None,
    exports_dir: Optional[Path] = None,
    spec_path: Optional[Path] = None,
    dashboard_marker: Optional[Path] = None,
) -> PipelineResult:
    """Evaluate every step of ``pipeline`` and return the aggregated result.

    Pass ``mhde_conn`` / ``engine_conn`` (the crypto pipeline only uses the
    engine conn) and the ``*_dir`` / ``*_path`` / ``*_marker`` overrides to
    inject test fixtures; otherwise real read-only connections are opened (and
    closed) here and the production paths are used. A failure to open the
    engine DB is logged and surfaces as RED on the engine steps rather than
    crashing.
    """
    now = now or datetime.now(timezone.utc)
    pipeline = pipeline.lower()
    if pipeline not in DISPLAY_NAME:
        raise ValueError(f"unknown pipeline {pipeline!r}; expected one of {sorted(DISPLAY_NAME)}")

    close_mhde = False
    if mhde_conn is None:
        mhde_conn = _open_mhde_db()
        close_mhde = True

    close_engine = False
    if pipeline == "crypto" and engine_conn is None:
        try:
            engine_conn = C.open_engine_db()
            close_engine = True
        except Exception as exc:  # noqa: BLE001
            logger.error("crypto pipeline monitor: engine DuckDB unavailable: %s", exc)
            engine_conn = None

    try:
        if pipeline == "crypto":
            steps = _crypto_steps(mhde_conn, engine_conn, now, exports_dir, spec_path)
        elif pipeline == "equity":
            steps = _equity_steps(mhde_conn, now, dashboard_marker)
        else:
            steps = _fx_steps(mhde_conn, now)
        results = evaluate_steps(steps, stop_on_red=True)
        return PipelineResult(pipeline=DISPLAY_NAME[pipeline], as_of=now, steps=results)
    finally:
        if close_mhde:
            mhde_conn.close()
        if close_engine and engine_conn is not None:
            engine_conn.close()


def main(pipeline: str) -> int:
    result = run_pipeline(pipeline)
    alert.send_text(render_telegram_message(result))
    logger.info("pipeline monitor %s — %s", result.pipeline, result.overall.name)
    return 0 if not result.has_red else 1
