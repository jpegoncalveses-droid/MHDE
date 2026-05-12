"""Continuous monitor (every 30 min): alert only on red, silent when green.

Two independent checks (no cascade):

* **FX hourly bar freshness** — the newest ``fx_prices_hourly`` bar is within
  the live 2-hour threshold (or, during the Fri 22:00 → Sun 22:00 UTC forex
  close, at/after the Friday 21:00 close-floor — KI-128).
* **Crypto engine timers** — the engine's per-minute ``monitor`` cycle is
  ticking and its daily ``entry`` phase ran today (read-only cross-repo read
  of the engine DuckDB — ADR-020). The engine ``reconcile`` timer is disabled
  pending RECONCILE-001, so it is not checked; flip ``CHECK_ENGINE_RECONCILE``
  when it is re-enabled.

If any check is RED a single Telegram message (showing all checks) is sent;
if all green nothing is sent. Exit status: 0 when silent, 1 when an alert
fired.

CLI: ``main.py monitor continuous``. Systemd: ``mhde-continuous-monitor.{service,timer}``.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from typing import Optional

from monitoring import alert
from monitoring.pipeline_monitor.checks import crypto as C
from monitoring.pipeline_monitor.checks import fx as F
from monitoring.pipeline_monitor.core import (
    PipelineResult,
    Status,
    StepResult,
    evaluate_steps,
    render_telegram_message,
)

logger = logging.getLogger("mhde.monitoring.pipeline_monitor.continuous")

#: the engine `monitor` phase fires every minute; flag if the last success is older than this.
ENGINE_MONITOR_STALE_RED_MIN = 15
#: by this UTC time the engine's daily `entry` phase (06:30 per active_spec) should have run.
ENGINE_ENTRY_CUTOFF_UTC = time(8, 0)
#: the engine `reconcile` timer is disabled pending RECONCILE-001 — flip to True when re-enabled.
CHECK_ENGINE_RECONCILE = False
#: grace window for the engine `reconcile` phase (runs ~23:00 UTC daily) once re-enabled.
ENGINE_RECONCILE_STALE_RED_HOURS = 26

CONT_FX_FRESHNESS = "FX hourly bar freshness"
CONT_ENGINE_MONITOR = "Crypto engine monitor timer"
CONT_ENGINE_ENTRY = "Crypto engine entry timer (ran today)"
CONT_ENGINE_RECONCILE = "Crypto engine reconcile timer"


def _open_mhde_db():
    import duckdb
    from storage.config import load_engine_config

    return duckdb.connect(load_engine_config()["db_path"], read_only=True)


def _naive(now: datetime) -> datetime:
    return now.replace(tzinfo=None) if now.tzinfo else now


# ── checks ────────────────────────────────────────────────────────────
def check_fx_freshness_step(mhde_conn, now: datetime) -> StepResult:
    r = F.check_bar_ingestion(mhde_conn, now)
    return StepResult(CONT_FX_FRESHNESS, r.status, r.detail)


def check_engine_monitor_timer(engine_conn, now: datetime) -> StepResult:
    if engine_conn is None:
        return StepResult(CONT_ENGINE_MONITOR, Status.RED, "engine DuckDB not reachable")
    last = engine_conn.execute(
        "SELECT MAX(started_at) FROM engine_runs WHERE phase = 'monitor' AND success = TRUE"
    ).fetchone()[0]
    if last is None:
        return StepResult(CONT_ENGINE_MONITOR, Status.RED, "no successful engine 'monitor' cycle ever recorded")
    age_min = (_naive(now) - last).total_seconds() / 60.0
    if age_min > ENGINE_MONITOR_STALE_RED_MIN:
        return StepResult(
            CONT_ENGINE_MONITOR, Status.RED,
            f"last engine 'monitor' cycle {age_min:.0f} min ago (> {ENGINE_MONITOR_STALE_RED_MIN} min) — engine looks down",
        )
    return StepResult(CONT_ENGINE_MONITOR, Status.GREEN, f"engine 'monitor' cycle {age_min:.1f} min ago")


def check_engine_entry_timer(engine_conn, now: datetime) -> StepResult:
    if engine_conn is None:
        return StepResult(CONT_ENGINE_ENTRY, Status.RED, "engine DuckDB not reachable")
    nn = _naive(now)
    if nn.time() < ENGINE_ENTRY_CUTOFF_UTC:
        return StepResult(
            CONT_ENGINE_ENTRY, Status.GREEN,
            f"entry not due yet (before {ENGINE_ENTRY_CUTOFF_UTC:%H:%M} UTC cutoff)",
        )
    midnight = datetime.combine(nn.date(), time.min)
    last = engine_conn.execute(
        "SELECT MAX(started_at) FROM engine_runs WHERE phase = 'entry' AND success = TRUE AND started_at >= ?",
        [midnight],
    ).fetchone()[0]
    if last is None:
        return StepResult(
            CONT_ENGINE_ENTRY, Status.RED,
            f"no successful engine 'entry' run today ({nn.date()}) — past the {ENGINE_ENTRY_CUTOFF_UTC:%H:%M} UTC cutoff",
        )
    return StepResult(CONT_ENGINE_ENTRY, Status.GREEN, f"engine 'entry' ran today at {last:%H:%M} UTC")


def check_engine_reconcile_timer(engine_conn, now: datetime) -> StepResult:
    if engine_conn is None:
        return StepResult(CONT_ENGINE_RECONCILE, Status.RED, "engine DuckDB not reachable")
    last = engine_conn.execute(
        "SELECT MAX(started_at) FROM engine_runs WHERE phase = 'reconcile' AND success = TRUE"
    ).fetchone()[0]
    if last is None:
        return StepResult(CONT_ENGINE_RECONCILE, Status.RED, "no successful engine 'reconcile' run ever recorded")
    age_h = (_naive(now) - last).total_seconds() / 3600.0
    if age_h > ENGINE_RECONCILE_STALE_RED_HOURS:
        return StepResult(
            CONT_ENGINE_RECONCILE, Status.RED,
            f"last engine 'reconcile' {age_h:.0f}h ago (> {ENGINE_RECONCILE_STALE_RED_HOURS}h)",
        )
    return StepResult(CONT_ENGINE_RECONCILE, Status.GREEN, f"engine 'reconcile' ran {age_h:.0f}h ago")


# ── runner ────────────────────────────────────────────────────────────
def run_continuous(*, mhde_conn=None, engine_conn=None, now: Optional[datetime] = None) -> PipelineResult:
    now = now or datetime.now(timezone.utc)

    close_mhde = False
    if mhde_conn is None:
        mhde_conn = _open_mhde_db()
        close_mhde = True

    close_engine = False
    if engine_conn is None:
        try:
            engine_conn = C.open_engine_db()
            close_engine = True
        except Exception as exc:  # noqa: BLE001
            logger.error("continuous monitor: engine DuckDB unavailable: %s", exc)
            engine_conn = None

    try:
        steps = [
            (CONT_FX_FRESHNESS, lambda: check_fx_freshness_step(mhde_conn, now)),
            (CONT_ENGINE_MONITOR, lambda: check_engine_monitor_timer(engine_conn, now)),
            (CONT_ENGINE_ENTRY, lambda: check_engine_entry_timer(engine_conn, now)),
        ]
        if CHECK_ENGINE_RECONCILE:
            steps.append((CONT_ENGINE_RECONCILE, lambda: check_engine_reconcile_timer(engine_conn, now)))
        results = evaluate_steps(steps, stop_on_red=False)
        return PipelineResult(pipeline="Continuous", as_of=now, steps=results)
    finally:
        if close_mhde:
            mhde_conn.close()
        if close_engine and engine_conn is not None:
            engine_conn.close()


def main() -> int:
    result = run_continuous()
    message = render_telegram_message(result)
    # Echo the rendered message to stdout so it lands in the systemd journal
    # and is visible on a manual / MONITORING_DRY_RUN=true invocation. The
    # Telegram send still only happens on red (silent-when-green by design).
    print(message)
    if result.has_red:
        alert.send_text(message)
        logger.warning("continuous monitor — RED, alert sent")
        return 1
    logger.info("continuous monitor — all green, no alert sent")
    return 0
