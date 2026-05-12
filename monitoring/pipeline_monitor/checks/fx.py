"""Outcome-based checks for the FX (GBP/EUR) hourly pipeline.

The FX *daily* monitor fires once a day (mid-UTC-day) and snapshots the FX
pipeline's health: the latest hourly bar is fresh, and a signal was emitted
for it. Continuous bar-freshness is handled separately by the every-30-min
continuous monitor (``continuous_runner``); the daily message gives the
operator a once-a-day green confirmation in the same shape as the other
pipelines.

Freshness reuses :func:`pipelines.freshness.check_fx_freshness`, which already
handles the forex-closed window (Fri 22:00 → Sun 22:00 UTC, KI-128): during
the close the latest bar must be at or after the Friday 21:00 close-floor
rather than within the 2-hour live threshold.
"""
from __future__ import annotations

from datetime import datetime, timezone

from monitoring.pipeline_monitor.core import Status, StepResult
from pipelines.freshness import check_fx_freshness

# ── step display names ────────────────────────────────────────────────
FX_BAR_INGESTION = "FX bar ingestion (fx_prices_hourly)"
FX_SIGNAL_GENERATION = "Signal generation (fx_signals)"


# ──────────────────────────────────────────────────────────────────────
# 1. FX bar ingestion / freshness
# ──────────────────────────────────────────────────────────────────────
def check_bar_ingestion(conn, now: datetime) -> StepResult:
    # check_fx_freshness's contract is a naive-UTC `now`; convert at the boundary.
    now_naive = now.replace(tzinfo=None) if now.tzinfo else now
    report = check_fx_freshness(conn, now=now_naive)
    status = Status.GREEN if report.is_fresh else Status.RED
    return StepResult(FX_BAR_INGESTION, status, report.message)


# ──────────────────────────────────────────────────────────────────────
# 2. Signal generation for the latest bar
# ──────────────────────────────────────────────────────────────────────
def check_signal_generation(conn, now: datetime) -> StepResult:
    latest_bar = conn.execute("SELECT MAX(datetime_utc) FROM fx_prices_hourly").fetchone()[0]
    if latest_bar is None:
        return StepResult(FX_SIGNAL_GENERATION, Status.RED, "fx_prices_hourly is empty — no bar to score")
    row = conn.execute(
        "SELECT datetime_utc, signal_type FROM fx_signals ORDER BY datetime_utc DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return StepResult(FX_SIGNAL_GENERATION, Status.RED, "fx_signals is empty — no signal ever generated")
    latest_sig, sig_type = row
    if latest_sig >= latest_bar:
        return StepResult(FX_SIGNAL_GENERATION, Status.GREEN, f"signal {sig_type} @ {latest_sig}")
    lag_h = (latest_bar - latest_sig).total_seconds() / 3600.0
    return StepResult(
        FX_SIGNAL_GENERATION, Status.RED,
        f"latest signal @ {latest_sig} lags latest bar @ {latest_bar} by {lag_h:.0f}h — "
        "fx predict / signal generation did not run for the newest bar",
    )
