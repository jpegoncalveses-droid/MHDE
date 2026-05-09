"""Monitor: weekly Phase 0 calibration interim check.

Per ``docs/PATH_TO_LIVE_PLAN.md`` § "Phase 0: Live Calibration
Validation", the four pass criteria need to be met before Phase 4
(live trading). This monitor runs weekly to surface drift early
instead of discovering issues at week 6.

Three independent alert paths, all per-active-model:

  1. **Drift signal.** Any of:
       - lift over base rate < 1.5 over rolling 30d window
         (the operator's tighter "yellow flag" — distinct from the
         Phase 0 1.3 hard gate).
       - rolling precision / baseline < 0.85
         (also tighter than the 0.75 lower-band of the ±25%
         hit-rate criterion).
       - 3+ consecutive same-direction reliability buckets off
         > 10pp from midpoint (matches the formal Phase 0
         calibration-bucket criterion).
     Severity: warn.

  2. **Sample-rate slowdown.** When the linear-projected ETA to
     the 200-sample gate moves more than 7 days later vs the
     previous run's projection. Stored in ``phase0_milestones``
     (engine, model_id, milestone="last_eta_projection") so the
     comparison is week-over-week. Severity: warn.

  3. **Sample threshold reached.** When n_filled crosses 200 for
     the first time on a given model, the monitor writes
     ``phase0_milestones (engine, model_id, "200_reached")`` and
     fires a single info-severity alert telling the operator to
     run ``crypto phase0-report`` for the formal evaluation.
     Idempotent — only fires once per model.

Schedule: Sundays 06:00 UTC. Adds the 10th monitor to the stack.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from monitoring.alert import MonitorResult, send_alert

logger = logging.getLogger("mhde.monitoring.phase0_calibration")


# Drift thresholds — tighter than the Phase 0 hard gates so the
# weekly monitor catches drift before the formal evaluation.
LIFT_DRIFT_THRESHOLD = 1.5
PRECISION_RATIO_DRIFT_THRESHOLD = 0.85
ETA_SLIP_DAYS = 7.0


@dataclass
class _ModelObservation:
    """One model's per-axis findings for the weekly run."""
    model_id: str
    horizon: str
    n_filled: int
    drift_problems: list[str]
    eta_iso: Optional[str]
    eta_slip_days: Optional[float]
    sample_threshold_crossed: bool


def _last_eta_projection(conn, engine: str, model_id: str) -> Optional[str]:
    row = conn.execute(
        """
        SELECT detail FROM phase0_milestones
        WHERE engine = ? AND model_id = ? AND milestone = 'last_eta_projection'
        """,
        [engine, model_id],
    ).fetchone()
    return row[0] if row else None


def _record_milestone(
    conn, engine: str, model_id: str, milestone: str, *,
    detail: Optional[str] = None,
) -> None:
    """Idempotent upsert of a milestone marker."""
    now = datetime.now(timezone.utc)
    conn.execute(
        """
        INSERT INTO phase0_milestones
            (engine, model_id, milestone, fired_at, detail)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (engine, model_id, milestone) DO UPDATE SET
            fired_at = excluded.fired_at,
            detail = excluded.detail
        """,
        [engine, model_id, milestone, now, detail],
    )


def _has_milestone(conn, engine: str, model_id: str, milestone: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM phase0_milestones
        WHERE engine = ? AND model_id = ? AND milestone = ?
        """,
        [engine, model_id, milestone],
    ).fetchone()
    return row is not None


def _eta_slip_days(prev_iso: Optional[str], current_iso: Optional[str]) -> Optional[float]:
    """Days the new ETA is later than the previous one. Negative
    means earlier (good). None when either input missing."""
    from datetime import date
    if prev_iso is None or current_iso is None:
        return None
    try:
        prev = date.fromisoformat(prev_iso)
        curr = date.fromisoformat(current_iso)
    except ValueError:
        return None
    return (curr - prev).days


def _evaluate_one(conn, engine, model_id: str) -> _ModelObservation:
    """Run interim evaluation + projection + milestone logic for one
    model. Reads the four-criterion verdict from phase0_evaluate and
    layers the weekly drift / sample-rate signals on top."""
    from crypto.ml.phase0_evaluate import (
        check_calibration_buckets, check_hit_rate_tolerance,
        check_lift_window, project_sample_accumulation,
    )

    # The "drift" signals are tighter than the formal pass criteria.
    drift: list[str] = []
    lift = check_lift_window(conn, model_id, engine=engine)
    if lift.status != "skip" and lift.current_value is not None:
        if lift.current_value < LIFT_DRIFT_THRESHOLD:
            drift.append(
                f"lift {lift.current_value:.2f}× over base "
                f"(threshold {LIFT_DRIFT_THRESHOLD:.2f}×; "
                f"30d window, n={lift.sample_size})"
            )
    hit = check_hit_rate_tolerance(conn, model_id, engine=engine)
    if (hit.status != "skip" and hit.current_value is not None
            and hit.expected_value is not None and hit.expected_value > 0):
        ratio = hit.current_value / hit.expected_value
        if ratio < PRECISION_RATIO_DRIFT_THRESHOLD:
            drift.append(
                f"rolling precision {hit.current_value:.3f} / "
                f"baseline {hit.expected_value:.3f} = ratio {ratio:.2f} "
                f"(threshold {PRECISION_RATIO_DRIFT_THRESHOLD:.2f}; "
                f"n={hit.sample_size})"
            )
    cal = check_calibration_buckets(conn, model_id, engine=engine)
    if cal.status == "fail":
        drift.append(f"calibration drift: {cal.detail}")

    # Pull horizon for messaging
    runs_row = conn.execute(
        f"SELECT horizon FROM {engine.model_runs_table} WHERE model_id = ?",
        [model_id],
    ).fetchone()
    horizon = runs_row[0] if runs_row else "unknown"

    proj = project_sample_accumulation(conn, model_id, engine=engine)
    eta_iso = proj.eta
    prev_iso = _last_eta_projection(conn, engine.name, model_id)
    eta_slip = _eta_slip_days(prev_iso, eta_iso)

    # Threshold-crossing one-shot logic
    sample_crossed = (
        proj.n_filled_now >= proj.n_filled_threshold
        and not _has_milestone(conn, engine.name, model_id, "200_reached")
    )

    return _ModelObservation(
        model_id=model_id, horizon=horizon, n_filled=proj.n_filled_now,
        drift_problems=drift, eta_iso=eta_iso, eta_slip_days=eta_slip,
        sample_threshold_crossed=sample_crossed,
    )


def _format_alert_lines(obs: _ModelObservation) -> list[str]:
    lines: list[str] = []
    if obs.sample_threshold_crossed:
        lines.append(
            f"🎯  `{obs.model_id}` ({obs.horizon}): n_filled = "
            f"{obs.n_filled} ≥ 200. **Phase 0 sample threshold reached.** "
            f"Run `crypto phase0-report` for formal evaluation."
        )
    for d in obs.drift_problems:
        lines.append(f"⚠  `{obs.model_id}` ({obs.horizon}): {d}")
    if (obs.eta_slip_days is not None
            and obs.eta_slip_days > ETA_SLIP_DAYS):
        lines.append(
            f"📉  `{obs.model_id}` ({obs.horizon}): sample-rate slowdown — "
            f"projected gate ETA slipped by {obs.eta_slip_days:.0f} days "
            f"(now {obs.eta_iso}); fill rate dropping vs prior week."
        )
    return lines


def run(conn=None) -> MonitorResult:
    """Run the weekly Phase 0 monitor. Crypto-only today; the
    EngineConfig abstraction is in place so equity / FX can be added
    by extending the loop."""
    started = datetime.now(timezone.utc)

    close_conn = False
    if conn is None:
        from storage.config import load_engine_config
        import duckdb
        cfg = load_engine_config()
        # Read-write here (not read_only) because the monitor records
        # milestones into phase0_milestones.
        conn = duckdb.connect(cfg["db_path"], read_only=False)
        close_conn = True

    try:
        # Make sure the milestones table exists (fresh DB / first run).
        from crypto.ml.phase0_evaluate import CRYPTO
        from crypto.schema import SCHEMA_PHASE0_MILESTONES
        conn.execute(SCHEMA_PHASE0_MILESTONES)

        engine = CRYPTO

        rows = conn.execute(
            f"SELECT model_id FROM {engine.model_runs_table} "
            f"WHERE is_active = TRUE ORDER BY horizon"
        ).fetchall()
        observations = [_evaluate_one(conn, engine, r[0]) for r in rows]

        # Persist milestones BEFORE classifying — once per cycle, the
        # "200_reached" alert fires; subsequent runs see the marker and
        # don't re-fire. ETA snapshot rolls forward every run.
        for obs in observations:
            if obs.sample_threshold_crossed:
                _record_milestone(
                    conn, engine.name, obs.model_id, "200_reached",
                    detail=f"n_filled={obs.n_filled}",
                )
            if obs.eta_iso is not None:
                _record_milestone(
                    conn, engine.name, obs.model_id, "last_eta_projection",
                    detail=obs.eta_iso,
                )

        problems: list[str] = []
        info_only_problems: list[str] = []
        for obs in observations:
            for ln in _format_alert_lines(obs):
                if ln.startswith("🎯"):
                    info_only_problems.append(ln)
                else:
                    problems.append(ln)

        finished = datetime.now(timezone.utc)
        metrics = {
            "n_active_models": len(observations),
            "models": [
                {
                    "model_id": o.model_id,
                    "horizon": o.horizon,
                    "n_filled": o.n_filled,
                    "eta_iso": o.eta_iso,
                    "eta_slip_days": o.eta_slip_days,
                    "drift_problems": o.drift_problems,
                    "sample_threshold_crossed": o.sample_threshold_crossed,
                }
                for o in observations
            ],
        }

        if problems:
            return MonitorResult(
                monitor="phase0_calibration",
                status="warn", severity="warn",
                title="Phase 0 calibration weekly check — drift detected",
                body="\n".join(f"- {p}" for p in problems + info_only_problems),
                metrics=metrics,
                started_at=started, finished_at=finished,
            )
        if info_only_problems:
            # Sample threshold reached but no drift — info-severity alert
            # via warn-status so it routes through send_alert once.
            return MonitorResult(
                monitor="phase0_calibration",
                status="warn", severity="info",
                title="Phase 0 sample threshold reached",
                body="\n".join(f"- {p}" for p in info_only_problems),
                metrics=metrics,
                started_at=started, finished_at=finished,
            )
        return MonitorResult(
            monitor="phase0_calibration",
            status="ok", severity="info",
            title=f"Phase 0 interim — {len(observations)} model(s) tracking",
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
