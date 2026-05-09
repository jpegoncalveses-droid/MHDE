"""Phase 0 calibration evaluation — pure functions over a DuckDB connection.

Per ``docs/PATH_TO_LIVE_PLAN.md`` § "Phase 0: Live Calibration Validation",
the four pass criteria are:

  1. Top-N hit rate within ±25% of walk-forward expectation.
     ``check_hit_rate_tolerance``
  2. Lift over base rate > 1.3 over rolling 30-day window.
     ``check_lift_window``
  3. No systematic over- or under-confidence in calibration buckets.
     ``check_calibration_buckets`` (definition (a) absolute — see KI-126
     for the future relative-drift extension)
  4. Minimum sample: 200 predictions with elapsed horizons.
     ``check_minimum_sample``

The evaluator returns a structured per-criterion result so the report
renderer (``phase0_report.py``) and the weekly monitor
(``monitoring/phase0_calibration.py``) can consume the same data.

Crypto-only wiring today (``ENGINES = {"crypto": CRYPTO}``) but the
``EngineConfig`` abstraction makes equity / FX a one-config-block
extension when those workstreams reach Phase 0.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Optional

import duckdb

logger = logging.getLogger("mhde.crypto.phase0_evaluate")


# ──────────────────────────────────────────────────────────────────────
# Engine configuration
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EngineConfig:
    """Per-engine table + column names. Lets the evaluators stay
    engine-agnostic; today only ``crypto`` is wired."""
    name: str
    predictions_table: str
    model_runs_table: str
    date_col: str           # column on predictions table that orders rows
    bucket_col: str = "predicted_probability"
    label_col: str = "actual_hit"


CRYPTO = EngineConfig(
    name="crypto",
    predictions_table="crypto_ml_predictions",
    model_runs_table="crypto_ml_model_runs",
    date_col="prediction_date",
)

# To extend: add EQUITY (ml_predictions / ml_model_runs / prediction_date)
# and FX (fx_ml_predictions / fx_ml_model_runs / datetime_utc) and register
# them here. The criteria functions take engine as a kwarg.
ENGINES: dict[str, EngineConfig] = {"crypto": CRYPTO}


# ──────────────────────────────────────────────────────────────────────
# Result types
# ──────────────────────────────────────────────────────────────────────


@dataclass
class CriterionResult:
    """Outcome of one Phase 0 criterion check."""
    name: str
    status: Literal["pass", "fail", "skip"]
    current_value: Optional[float]
    expected_value: Optional[float]
    threshold_lo: Optional[float]
    threshold_hi: Optional[float]
    sample_size: int
    detail: str


@dataclass
class ReliabilityBucket:
    """One row of a reliability diagram."""
    low: float
    high: float
    midpoint: float
    n: int
    n_hits: int
    actual_rate: Optional[float]    # None when n == 0
    deviation_pp: Optional[float]   # (actual - midpoint) * 100, percentage points


@dataclass
class Phase0Verdict:
    """Aggregate per-model verdict — the building block both the
    CLI report and the monitor consume."""
    model_id: str
    horizon: str
    sample_size: int
    criteria: dict[str, CriterionResult]
    reliability: list[ReliabilityBucket]
    overall: Literal["PASS", "FAIL", "INTERIM"]


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


_DEFAULT_BUCKET_EDGES: tuple[float, ...] = (
    0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95,
)


def _filled_count(conn, engine: EngineConfig, model_id: str) -> tuple[int, int]:
    """Return (n_filled, n_hits) for one model. n_filled is the
    sample size the four criteria operate on."""
    row = conn.execute(
        f"""
        SELECT
            SUM(CASE WHEN {engine.label_col} IS NOT NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN {engine.label_col} = TRUE THEN 1 ELSE 0 END)
        FROM {engine.predictions_table}
        WHERE model_id = ? AND outcome_filled_at IS NOT NULL
        """,
        [model_id],
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def _baseline(
    conn, engine: EngineConfig, model_id: str
) -> tuple[Optional[float], Optional[float]]:
    """Return (precision_at_threshold, base_rate) for one model."""
    row = conn.execute(
        f"""
        SELECT precision_at_threshold, base_rate
        FROM {engine.model_runs_table}
        WHERE model_id = ?
        """,
        [model_id],
    ).fetchone()
    if not row:
        return None, None
    return row[0], row[1]


# ──────────────────────────────────────────────────────────────────────
# Criterion 1: hit rate within ±25%
# ──────────────────────────────────────────────────────────────────────


def check_hit_rate_tolerance(
    conn,
    model_id: str,
    *,
    engine: EngineConfig = CRYPTO,
    tolerance_pct: float = 0.25,
) -> CriterionResult:
    """Compare live rolling precision against the model's
    ``precision_at_threshold`` from training. Both directions flagged:
    under-performance AND over-performance both indicate calibration drift.

    Tolerance is relative: a baseline of 0.60 with tolerance_pct=0.25
    requires live precision ∈ [0.45, 0.75].
    """
    baseline, _ = _baseline(conn, engine, model_id)
    n_filled, n_hits = _filled_count(conn, engine, model_id)

    if baseline is None:
        return CriterionResult(
            name="hit_rate_within_25pct", status="skip",
            current_value=None, expected_value=None,
            threshold_lo=None, threshold_hi=None,
            sample_size=n_filled,
            detail=f"no precision_at_threshold for {model_id} in {engine.model_runs_table}",
        )
    if n_filled == 0:
        return CriterionResult(
            name="hit_rate_within_25pct", status="skip",
            current_value=None, expected_value=baseline,
            threshold_lo=baseline * (1 - tolerance_pct),
            threshold_hi=baseline * (1 + tolerance_pct),
            sample_size=0,
            detail="no filled outcomes yet",
        )
    rolling = n_hits / n_filled
    lo = baseline * (1 - tolerance_pct)
    hi = baseline * (1 + tolerance_pct)
    in_band = lo <= rolling <= hi
    return CriterionResult(
        name="hit_rate_within_25pct",
        status="pass" if in_band else "fail",
        current_value=rolling, expected_value=baseline,
        threshold_lo=lo, threshold_hi=hi,
        sample_size=n_filled,
        detail=(
            f"live precision {rolling:.3f} vs baseline {baseline:.3f} "
            f"(band [{lo:.3f}, {hi:.3f}]); "
            f"{'within' if in_band else 'OUTSIDE'} ±{tolerance_pct:.0%}"
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Criterion 2: lift over base rate > 1.3 over rolling 30 days
# ──────────────────────────────────────────────────────────────────────


def check_lift_window(
    conn,
    model_id: str,
    *,
    engine: EngineConfig = CRYPTO,
    window_days: int = 30,
    threshold: float = 1.3,
) -> CriterionResult:
    """Live lift = rolling_precision / base_rate over the last
    ``window_days`` of filled outcomes. Pass when ≥ ``threshold``."""
    _, base_rate = _baseline(conn, engine, model_id)
    if base_rate is None or base_rate <= 0:
        return CriterionResult(
            name="lift_over_base", status="skip",
            current_value=None, expected_value=None,
            threshold_lo=threshold, threshold_hi=None,
            sample_size=0,
            detail=(f"no base_rate for {model_id} in "
                    f"{engine.model_runs_table}"),
        )

    row = conn.execute(
        f"""
        SELECT
            SUM(CASE WHEN {engine.label_col} IS NOT NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN {engine.label_col} = TRUE THEN 1 ELSE 0 END)
        FROM {engine.predictions_table}
        WHERE model_id = ?
          AND outcome_filled_at IS NOT NULL
          AND outcome_filled_at >= CURRENT_TIMESTAMP - (INTERVAL '1 day' * ?)
        """,
        [model_id, window_days],
    ).fetchone()
    n, hits = int(row[0] or 0), int(row[1] or 0)

    if n == 0:
        return CriterionResult(
            name="lift_over_base", status="skip",
            current_value=None, expected_value=threshold,
            threshold_lo=threshold, threshold_hi=None,
            sample_size=0,
            detail=f"no filled outcomes in last {window_days}d window",
        )
    rolling = hits / n
    lift = rolling / base_rate
    return CriterionResult(
        name="lift_over_base",
        status="pass" if lift >= threshold else "fail",
        current_value=lift, expected_value=threshold,
        threshold_lo=threshold, threshold_hi=None,
        sample_size=n,
        detail=(
            f"rolling {window_days}d precision {rolling:.3f} / "
            f"base_rate {base_rate:.3f} = lift {lift:.2f}x "
            f"(threshold {threshold:.2f}x); "
            f"{'PASS' if lift >= threshold else 'BELOW'} threshold"
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Criterion 3: calibration buckets — absolute deviation (definition a)
# ──────────────────────────────────────────────────────────────────────


def compute_reliability_diagram(
    conn,
    model_id: str,
    *,
    engine: EngineConfig = CRYPTO,
    bucket_edges: tuple[float, ...] = _DEFAULT_BUCKET_EDGES,
) -> list[ReliabilityBucket]:
    """Bin filled predictions by ``predicted_probability`` and return
    one bucket per ``[edge_i, edge_{i+1})`` interval.

    Per Phase 0 spec the default edges are
    ``(0.50, 0.55, 0.60, ..., 0.95)``: 9 buckets covering the
    decision-relevant probability range.
    """
    if len(bucket_edges) < 2:
        raise ValueError("bucket_edges must have at least 2 entries")
    out: list[ReliabilityBucket] = []
    for i in range(len(bucket_edges) - 1):
        lo, hi = bucket_edges[i], bucket_edges[i + 1]
        # Last bucket is closed on the right so 0.95 lands in 0.90–0.95.
        right_op = "<=" if i == len(bucket_edges) - 2 else "<"
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS n,
                SUM(CASE WHEN {engine.label_col} = TRUE THEN 1 ELSE 0 END) AS hits
            FROM {engine.predictions_table}
            WHERE model_id = ?
              AND outcome_filled_at IS NOT NULL
              AND {engine.bucket_col} >= ?
              AND {engine.bucket_col} {right_op} ?
            """,
            [model_id, lo, hi],
        ).fetchone()
        n = int(row[0] or 0)
        hits = int(row[1] or 0)
        midpoint = (lo + hi) / 2.0
        if n == 0:
            actual = None
            dev = None
        else:
            actual = hits / n
            dev = (actual - midpoint) * 100.0
        out.append(ReliabilityBucket(
            low=lo, high=hi, midpoint=midpoint,
            n=n, n_hits=hits,
            actual_rate=actual, deviation_pp=dev,
        ))
    return out


def check_calibration_buckets(
    conn,
    model_id: str,
    *,
    engine: EngineConfig = CRYPTO,
    bucket_edges: tuple[float, ...] = _DEFAULT_BUCKET_EDGES,
    deviation_threshold_pp: float = 10.0,
    consecutive_required: int = 3,
) -> CriterionResult:
    """Definition (a) absolute drift detection: flag when ``≥
    consecutive_required`` adjacent buckets (with non-empty samples) are
    off the bucket midpoint by ``> deviation_threshold_pp`` in the same
    direction. Same direction = systematic miscalibration.

    Definition (b) relative — week-over-week comparison — is deferred to
    KI-126 once weekly snapshots have accumulated.
    """
    buckets = compute_reliability_diagram(
        conn, model_id, engine=engine, bucket_edges=bucket_edges,
    )
    populated = [b for b in buckets if b.n > 0 and b.deviation_pp is not None]
    n_filled = sum(b.n for b in buckets)

    if not populated:
        return CriterionResult(
            name="calibration_buckets", status="skip",
            current_value=None, expected_value=None,
            threshold_lo=-deviation_threshold_pp,
            threshold_hi=deviation_threshold_pp,
            sample_size=0,
            detail=f"no filled outcomes across {len(bucket_edges)-1} buckets",
        )

    # Detect runs of consecutive-direction over-threshold deviations.
    flagged_run: list[ReliabilityBucket] = []
    longest_run: list[ReliabilityBucket] = []
    current_dir: Optional[int] = None     # +1 over, -1 under, None reset
    current_run: list[ReliabilityBucket] = []
    for b in buckets:
        # A bucket with no data breaks the consecutive chain.
        if b.deviation_pp is None:
            current_dir = None
            current_run = []
            continue
        if abs(b.deviation_pp) <= deviation_threshold_pp:
            current_dir = None
            current_run = []
            continue
        bdir = 1 if b.deviation_pp > 0 else -1
        if current_dir is None or current_dir != bdir:
            current_dir = bdir
            current_run = [b]
        else:
            current_run.append(b)
        if len(current_run) > len(longest_run):
            longest_run = list(current_run)
        if len(current_run) >= consecutive_required:
            flagged_run = list(current_run)

    avg_abs_dev = sum(abs(b.deviation_pp) for b in populated) / len(populated)
    if flagged_run:
        worst = max(flagged_run, key=lambda b: abs(b.deviation_pp or 0.0))
        direction = "OVER" if (worst.deviation_pp or 0) > 0 else "UNDER"
        run_str = ", ".join(
            f"[{b.low:.2f}-{b.high:.2f}]={b.deviation_pp:+.1f}pp"
            for b in flagged_run
        )
        return CriterionResult(
            name="calibration_buckets", status="fail",
            current_value=avg_abs_dev,
            expected_value=0.0,
            threshold_lo=-deviation_threshold_pp,
            threshold_hi=deviation_threshold_pp,
            sample_size=n_filled,
            detail=(
                f"{len(flagged_run)} consecutive {direction}-confident buckets "
                f"off > {deviation_threshold_pp:.0f}pp: {run_str}"
            ),
        )
    return CriterionResult(
        name="calibration_buckets", status="pass",
        current_value=avg_abs_dev,
        expected_value=0.0,
        threshold_lo=-deviation_threshold_pp,
        threshold_hi=deviation_threshold_pp,
        sample_size=n_filled,
        detail=(
            f"no run of ≥{consecutive_required} consecutive same-direction "
            f"buckets off > {deviation_threshold_pp:.0f}pp "
            f"(avg|dev|={avg_abs_dev:.1f}pp; longest run={len(longest_run)})"
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Criterion 4: minimum sample
# ──────────────────────────────────────────────────────────────────────


def check_minimum_sample(
    conn,
    model_id: str,
    *,
    engine: EngineConfig = CRYPTO,
    threshold: int = 200,
) -> CriterionResult:
    """The 200-prediction floor before Phase 0 can emit a formal verdict."""
    n_filled, _ = _filled_count(conn, engine, model_id)
    return CriterionResult(
        name="minimum_sample",
        status="pass" if n_filled >= threshold else "fail",
        current_value=float(n_filled),
        expected_value=float(threshold),
        threshold_lo=float(threshold),
        threshold_hi=None,
        sample_size=n_filled,
        detail=(
            f"{n_filled} filled outcomes vs threshold {threshold}; "
            f"{'PASS' if n_filled >= threshold else 'INSUFFICIENT'}"
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Aggregator — produces the structured verdict the CLI + monitor consume
# ──────────────────────────────────────────────────────────────────────


def evaluate_model(
    conn,
    model_id: str,
    *,
    engine: EngineConfig = CRYPTO,
) -> Phase0Verdict:
    """Run all four criteria + reliability diagram for one model."""
    n_filled, _ = _filled_count(conn, engine, model_id)
    horizon_row = conn.execute(
        f"SELECT horizon FROM {engine.model_runs_table} WHERE model_id = ?",
        [model_id],
    ).fetchone()
    horizon = horizon_row[0] if horizon_row else "unknown"

    criteria = {
        "hit_rate_within_25pct": check_hit_rate_tolerance(
            conn, model_id, engine=engine,
        ),
        "lift_over_base": check_lift_window(conn, model_id, engine=engine),
        "calibration_buckets": check_calibration_buckets(
            conn, model_id, engine=engine,
        ),
        "minimum_sample": check_minimum_sample(conn, model_id, engine=engine),
    }
    reliability = compute_reliability_diagram(conn, model_id, engine=engine)

    # Verdict logic per design discussion:
    #   - sample < 200 → INTERIM (no pass/fail claim)
    #   - sample ≥ 200 + any criterion failed → FAIL
    #   - sample ≥ 200 + all pass → PASS
    sample_ok = criteria["minimum_sample"].status == "pass"
    other_crit = [
        c for k, c in criteria.items() if k != "minimum_sample"
    ]
    if not sample_ok:
        overall: Literal["PASS", "FAIL", "INTERIM"] = "INTERIM"
    elif any(c.status == "fail" for c in other_crit):
        overall = "FAIL"
    else:
        overall = "PASS"

    return Phase0Verdict(
        model_id=model_id, horizon=horizon, sample_size=n_filled,
        criteria=criteria, reliability=reliability, overall=overall,
    )


def evaluate_all(
    conn,
    *,
    engine: EngineConfig = CRYPTO,
    model_id: Optional[str] = None,
) -> list[Phase0Verdict]:
    """Run ``evaluate_model`` for one or all is_active=true models."""
    if model_id is not None:
        return [evaluate_model(conn, model_id, engine=engine)]
    rows = conn.execute(
        f"SELECT model_id FROM {engine.model_runs_table} "
        f"WHERE is_active = TRUE ORDER BY horizon"
    ).fetchall()
    return [evaluate_model(conn, r[0], engine=engine) for r in rows]


# ──────────────────────────────────────────────────────────────────────
# Sample-accumulation projection (used by the weekly monitor)
# ──────────────────────────────────────────────────────────────────────


@dataclass
class SampleAccumulationProjection:
    """Linear projection of when n_filled will reach the 200-sample
    Phase 0 gate, based on the rate of new fills over the last week."""
    model_id: str
    n_filled_now: int
    n_filled_threshold: int
    n_filled_last_7d: int
    days_to_threshold: Optional[float]    # None when at/past threshold
    eta: Optional[str]                     # ISO date string


def project_sample_accumulation(
    conn,
    model_id: str,
    *,
    engine: EngineConfig = CRYPTO,
    threshold: int = 200,
) -> SampleAccumulationProjection:
    """Linear projection from last-7d fill rate to the 200-sample gate."""
    from datetime import datetime, timedelta, timezone

    n_filled_now, _ = _filled_count(conn, engine, model_id)
    if n_filled_now >= threshold:
        return SampleAccumulationProjection(
            model_id=model_id,
            n_filled_now=n_filled_now,
            n_filled_threshold=threshold,
            n_filled_last_7d=0,
            days_to_threshold=None,
            eta=None,
        )
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM {engine.predictions_table}
        WHERE model_id = ?
          AND outcome_filled_at IS NOT NULL
          AND outcome_filled_at >= CURRENT_TIMESTAMP - INTERVAL '7 days'
        """,
        [model_id],
    ).fetchone()
    n_last_7d = int(row[0] or 0)
    if n_last_7d == 0:
        return SampleAccumulationProjection(
            model_id=model_id,
            n_filled_now=n_filled_now,
            n_filled_threshold=threshold,
            n_filled_last_7d=0,
            days_to_threshold=None,
            eta=None,
        )
    rate_per_day = n_last_7d / 7.0
    remaining = threshold - n_filled_now
    days = remaining / rate_per_day
    eta = (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()
    return SampleAccumulationProjection(
        model_id=model_id,
        n_filled_now=n_filled_now,
        n_filled_threshold=threshold,
        n_filled_last_7d=n_last_7d,
        days_to_threshold=days,
        eta=eta,
    )
