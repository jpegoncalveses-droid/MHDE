"""Monitor: synthetic end-to-end probe of the dashboard's user-facing path.

Two distinct failure modes covered, hourly:

1. **Streamlit liveness.** HTTP GET on
   ``http://127.0.0.1:8501/_stcore/health``. If Streamlit is down,
   crashed, or unreachable behind nginx, this fires before the user
   notices.

2. **Helper non-raise + non-empty.** For each prediction tab —
   equity, crypto, FX — calls the same query helper the running
   Streamlit page calls (``dashboard.services.queries.get_*_predictions``)
   and asserts:
     - the call doesn't raise,
     - the returned DataFrame is non-empty (when predictions exist
       for the latest date),
     - the columns the user reads are not entirely NULL.

The deeper per-horizon column-completeness checks live in
``dashboard_consistency`` (every 6h). This monitor is the lightweight
hourly subset that catches "Streamlit unreachable" and "helper
raised" — failure modes ``dashboard_consistency`` doesn't even get
the chance to catch because it can't run if the helper module is
broken.

Why both: ``dashboard_consistency`` could be silent for 6 hours in a
real outage. ``dashboard_synthetic`` reduces that detection window
to 1 hour for the loud failures.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from monitoring.alert import MonitorResult, send_alert

logger = logging.getLogger("mhde.monitoring.dashboard_synthetic")


HEALTH_URL = "http://127.0.0.1:8501/_stcore/health"
HTTP_TIMEOUT_S = 5

# Columns the user actually reads on each prediction tab. The
# helper is allowed to surface other columns; we only assert the
# ones below are not entirely NULL.
_KEY_COLUMNS = {
    "equity": (
        "ticker", "horizon", "predicted_probability",
        "price_at_prediction", "maturity_date",
    ),
    "crypto": (
        "symbol", "horizon", "predicted_probability",
        "price_at_prediction", "maturity_date",
    ),
    "fx": (
        "datetime_utc", "direction", "horizon", "predicted_probability",
        "price_at_prediction", "maturity_datetime",
    ),
}


def _check_http_liveness() -> tuple[bool, str]:
    """Return (ok, detail). ok=False means Streamlit isn't serving."""
    try:
        import requests
    except ImportError:
        return False, "requests not importable"
    try:
        r = requests.get(HEALTH_URL, timeout=HTTP_TIMEOUT_S)
    except requests.RequestException as exc:
        return False, f"GET {HEALTH_URL}: {exc}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code} from {HEALTH_URL}"
    return True, f"HTTP 200 ({len(r.content)} bytes)"


def _check_engine(conn, engine: str) -> tuple[list[str], dict[str, Any]]:
    """Return (issues, metrics) for one engine."""
    from dashboard.services.queries import (
        get_crypto_predictions,
        get_equity_predictions,
        get_fx_recent_predictions,
    )
    issues: list[str] = []
    metrics: dict[str, Any] = {}

    try:
        if engine == "equity":
            latest = conn.execute(
                "SELECT MAX(prediction_date) FROM ml_predictions"
            ).fetchone()[0]
            if latest is None:
                metrics["latest"] = None
                metrics["rows"] = 0
                return [], metrics
            df = get_equity_predictions(conn, latest)
            metrics["latest"] = str(latest)
        elif engine == "crypto":
            latest = conn.execute(
                "SELECT MAX(prediction_date) FROM crypto_ml_predictions"
            ).fetchone()[0]
            if latest is None:
                metrics["latest"] = None
                metrics["rows"] = 0
                return [], metrics
            df = get_crypto_predictions(conn, latest)
            metrics["latest"] = str(latest)
        elif engine == "fx":
            df = get_fx_recent_predictions(conn, limit=10)
            if df.empty:
                metrics["latest"] = None
                metrics["rows"] = 0
                return [], metrics
            metrics["latest"] = str(df["datetime_utc"].max())
        else:
            return [f"unknown engine: {engine}"], metrics
    except Exception as exc:
        return [f"{engine}: helper raised: {type(exc).__name__}: {exc}"], metrics

    metrics["rows"] = len(df)

    for col in _KEY_COLUMNS[engine]:
        if col not in df.columns:
            issues.append(f"{engine}: column '{col}' missing from helper result")
            continue
        if df[col].isna().all():
            issues.append(
                f"{engine}: key column '{col}' is all-NULL across {len(df)} rows"
            )
    return issues, metrics


def run(conn=None, skip_http: bool = False) -> MonitorResult:
    """Run the synthetic E2E probe.

    `skip_http` is exposed for unit tests so they don't have to mock
    out network. Production calls always run the HTTP probe.
    """
    started = datetime.now(timezone.utc)

    close_conn = False
    if conn is None:
        from storage.config import load_engine_config
        import duckdb
        cfg = load_engine_config()
        conn = duckdb.connect(cfg["db_path"], read_only=True)
        close_conn = True

    try:
        problems: list[str] = []
        metrics: dict[str, Any] = {}

        # 1. HTTP liveness
        if not skip_http:
            ok, detail = _check_http_liveness()
            metrics["http"] = detail
            if not ok:
                problems.append(f"streamlit liveness: {detail}")

        # 2. Per-engine helpers
        for engine in ("equity", "crypto", "fx"):
            issues, m = _check_engine(conn, engine)
            problems.extend(issues)
            metrics[engine] = m

        finished = datetime.now(timezone.utc)
        if problems:
            return MonitorResult(
                monitor="dashboard_synthetic",
                status="fail",
                severity="warn",
                title="dashboard_synthetic: user-facing path degraded",
                body="\n".join(f"- {p}" for p in problems),
                metrics=metrics,
                started_at=started, finished_at=finished,
            )
        return MonitorResult(
            monitor="dashboard_synthetic",
            status="ok",
            severity="info",
            title="dashboard liveness + helpers ok",
            metrics=metrics,
            started_at=started, finished_at=finished,
        )
    finally:
        if close_conn:
            conn.close()


def main() -> int:
    result = run()
    send_alert(result)
    return 0 if result.status == "ok" else 1
