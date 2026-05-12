"""Outcome-based checks for the equity (ML) pipeline.

The equity monitor fires daily at ~01:00 UTC, after ``mhde-predict.service``
(00:15 UTC) has chained ``ml backfill-features`` → ``ml predict``, which read
the ``prices_daily`` rows that the previous evening's ``mhde-daily-analysis``
(23:15 Mon-Fri) ingested. So at monitor time the latest equity ``trade_date``
and ``prediction_date`` are the most recent *weekday strictly before today* —
``pipelines.market_calendar.expected_equity_prediction_date(now)`` — which
weekend-rolls correctly (Mon → Fri, Sat → Fri, etc.).

The "dashboard data refresh" step checks the mtime of the daily-analysis
output file the dashboard/candidate-review surface feeds off; a generous
multi-day window absorbs weekends and market holidays (when the 23:15 path
does not run at all). A longer outage is caught by the daily window plus the
existing health-check / pipeline-execution monitors.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from monitoring.pipeline_monitor.core import Status, StepResult
from pipelines.market_calendar import expected_equity_prediction_date

# ── step display names ────────────────────────────────────────────────
EQUITY_INGESTION = "Equity data ingestion (prices_daily)"
EQUITY_FEATURES = "Feature pipeline (ml_features)"
EQUITY_PREDICTIONS = "Model predictions (ml_predictions)"
EQUITY_DASHBOARD = "Dashboard data refresh"

#: daily-analysis (Mon-Fri 23:15) artifact the candidate-review dashboard reads.
DEFAULT_DASHBOARD_MARKER = Path("data/processed/prediction_vs_actual_rows.csv")
#: how stale that artifact may be before we flag — wide enough to span a
#: Friday→Tuesday weekend plus a market holiday.
DASHBOARD_MAX_STALE_DAYS = 4


def _expected_date(now: datetime):
    now_aware = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return expected_equity_prediction_date(now_aware)


# ──────────────────────────────────────────────────────────────────────
# 1. Equity data ingestion
# ──────────────────────────────────────────────────────────────────────
def check_data_ingestion(conn, now: datetime) -> StepResult:
    expected = _expected_date(now)
    latest = conn.execute("SELECT MAX(trade_date) FROM prices_daily").fetchone()[0]
    if latest is None:
        return StepResult(EQUITY_INGESTION, Status.RED, "prices_daily is empty")
    n = conn.execute("SELECT COUNT(*) FROM prices_daily WHERE trade_date = ?", [latest]).fetchone()[0]
    if latest >= expected:
        return StepResult(EQUITY_INGESTION, Status.GREEN, f"MAX(trade_date)={latest}, {n} tickers")
    return StepResult(
        EQUITY_INGESTION, Status.RED,
        f"MAX(trade_date)={latest} — expected >= {expected} (latest closed market day)",
    )


# ──────────────────────────────────────────────────────────────────────
# 2. Feature pipeline
# ──────────────────────────────────────────────────────────────────────
def check_feature_pipeline(conn, now: datetime) -> StepResult:
    expected = _expected_date(now)
    latest = conn.execute("SELECT MAX(trade_date) FROM ml_features").fetchone()[0]
    if latest is None:
        return StepResult(EQUITY_FEATURES, Status.RED, "ml_features is empty")
    n = conn.execute("SELECT COUNT(*) FROM ml_features WHERE trade_date = ?", [latest]).fetchone()[0]
    if latest >= expected and n > 0:
        return StepResult(EQUITY_FEATURES, Status.GREEN, f"{n} tickers @ trade_date={latest}")
    return StepResult(
        EQUITY_FEATURES, Status.RED,
        f"MAX(trade_date)={latest} ({n} rows) — expected features for {expected}",
    )


# ──────────────────────────────────────────────────────────────────────
# 3. Model predictions
# ──────────────────────────────────────────────────────────────────────
def check_model_predictions(conn, now: datetime) -> StepResult:
    expected = _expected_date(now)
    n_active = conn.execute("SELECT COUNT(*) FROM ml_model_runs WHERE is_active = TRUE").fetchone()[0]
    if n_active == 0:
        return StepResult(EQUITY_PREDICTIONS, Status.RED, "no active model in ml_model_runs")
    latest = conn.execute(
        "SELECT MAX(p.prediction_date) FROM ml_predictions p "
        "JOIN ml_model_runs m ON p.model_id = m.model_id WHERE m.is_active = TRUE"
    ).fetchone()[0]
    if latest is None:
        return StepResult(EQUITY_PREDICTIONS, Status.RED, "no equity predictions written by an active model")
    n = conn.execute(
        "SELECT COUNT(*) FROM ml_predictions p JOIN ml_model_runs m ON p.model_id = m.model_id "
        "WHERE m.is_active = TRUE AND p.prediction_date = ?",
        [latest],
    ).fetchone()[0]
    if latest >= expected and n > 0:
        return StepResult(EQUITY_PREDICTIONS, Status.GREEN, f"{n} predictions @ prediction_date={latest}")
    return StepResult(
        EQUITY_PREDICTIONS, Status.RED,
        f"latest active-model prediction_date={latest} ({n} rows) — expected {expected}",
    )


# ──────────────────────────────────────────────────────────────────────
# 4. Dashboard data refresh
# ──────────────────────────────────────────────────────────────────────
def check_dashboard_refresh(now: datetime, marker_path: Optional[Path] = None) -> StepResult:
    now_aware = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    marker = Path(marker_path) if marker_path else DEFAULT_DASHBOARD_MARKER
    if not marker.exists():
        return StepResult(EQUITY_DASHBOARD, Status.RED, f"{marker} does not exist (daily-analysis never ran?)")
    mtime = datetime.fromtimestamp(marker.stat().st_mtime, tz=timezone.utc)
    age_days = (now_aware - mtime).total_seconds() / 86400.0
    if age_days <= DASHBOARD_MAX_STALE_DAYS:
        return StepResult(EQUITY_DASHBOARD, Status.GREEN, f"{marker.name} updated {mtime:%Y-%m-%d %H:%M} UTC")
    return StepResult(
        EQUITY_DASHBOARD, Status.RED,
        f"{marker.name} last updated {mtime:%Y-%m-%d %H:%M} UTC ({age_days:.1f}d ago, "
        f"> {DASHBOARD_MAX_STALE_DAYS}d) — daily-analysis output is stale",
    )
