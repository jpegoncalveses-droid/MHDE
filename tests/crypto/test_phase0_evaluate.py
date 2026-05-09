"""Tests for crypto/ml/phase0_evaluate.py.

Each criterion is exercised with synthetic data engineered to land on
both sides of its decision boundary. ``temp_db`` from conftest already
has all four crypto schemas applied.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from crypto.ml.phase0_evaluate import (
    CRYPTO,
    CriterionResult,
    Phase0Verdict,
    ReliabilityBucket,
    check_calibration_buckets,
    check_hit_rate_tolerance,
    check_lift_window,
    check_minimum_sample,
    compute_reliability_diagram,
    evaluate_all,
    evaluate_model,
    project_sample_accumulation,
)


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


def _seed_model(conn, *, model_id="crypto_5d_test", horizon="5d",
                target_threshold=0.10, base_rate=0.30,
                precision_at_threshold=0.60, lift_over_base=2.0,
                is_active=True) -> None:
    conn.execute(
        """
        INSERT INTO crypto_ml_model_runs
            (model_id, horizon, target_threshold, base_rate,
             precision_at_threshold, lift_over_base,
             train_start, train_end, test_start, test_end, is_active)
        VALUES (?, ?, ?, ?, ?, ?,
                '2024-01-01', '2025-04-04', '2025-04-05', '2025-04-30', ?)
        """,
        [model_id, horizon, target_threshold, base_rate,
         precision_at_threshold, lift_over_base, is_active],
    )


def _seed_predictions(
    conn,
    *,
    model_id="crypto_5d_test",
    horizon="5d",
    items: list[tuple[float, bool]],   # (predicted_probability, hit)
    fill_within_days: int = 5,         # outcome_filled_at age in days
) -> None:
    """Seed a list of (probability, hit) prediction outcomes. Each row
    gets a unique symbol + recent prediction_date so freshness windows
    pick them up."""
    now = datetime.now(timezone.utc)
    today = now.date()
    for i, (prob, hit) in enumerate(items):
        pd_ = today - timedelta(days=i % 21)  # spread across last 3 weeks
        filled_at = now - timedelta(days=fill_within_days)
        conn.execute(
            """
            INSERT INTO crypto_ml_predictions
                (symbol, prediction_date, model_id, horizon,
                 predicted_probability, prediction_threshold,
                 actual_hit, outcome_filled_at, market_cap_bucket)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unknown')
            """,
            [f"COIN{i:04d}USDT", pd_, model_id, horizon, prob, 0.10,
             hit, filled_at],
        )


# ──────────────────────────────────────────────────────────────────────
# Criterion 1 — hit rate within ±25%
# ──────────────────────────────────────────────────────────────────────


def test_hit_rate_pass_at_baseline(temp_db):
    _seed_model(temp_db, precision_at_threshold=0.60)
    # 60 hits out of 100 → live precision = 0.60 = baseline → pass
    items = [(0.7, True)] * 60 + [(0.7, False)] * 40
    _seed_predictions(temp_db, items=items)

    r = check_hit_rate_tolerance(temp_db, "crypto_5d_test")
    assert r.status == "pass"
    assert r.current_value == pytest.approx(0.60)
    assert r.expected_value == pytest.approx(0.60)
    assert r.threshold_lo == pytest.approx(0.45)
    assert r.threshold_hi == pytest.approx(0.75)
    assert r.sample_size == 100


def test_hit_rate_fail_under_performance(temp_db):
    _seed_model(temp_db, precision_at_threshold=0.60)
    # 30 hits / 100 → 0.30 — well below 0.45 lower band
    items = [(0.7, True)] * 30 + [(0.7, False)] * 70
    _seed_predictions(temp_db, items=items)

    r = check_hit_rate_tolerance(temp_db, "crypto_5d_test")
    assert r.status == "fail"
    assert "OUTSIDE" in r.detail


def test_hit_rate_fail_over_performance_also_flagged(temp_db):
    """Over-performance is calibration drift too — both directions count."""
    _seed_model(temp_db, precision_at_threshold=0.60)
    # 90 hits / 100 → 0.90 — above 0.75 upper band
    items = [(0.7, True)] * 90 + [(0.7, False)] * 10
    _seed_predictions(temp_db, items=items)

    r = check_hit_rate_tolerance(temp_db, "crypto_5d_test")
    assert r.status == "fail"
    assert r.current_value == pytest.approx(0.90)


def test_hit_rate_skip_no_baseline(temp_db):
    # No model_runs row at all → skip
    r = check_hit_rate_tolerance(temp_db, "crypto_no_such_model")
    assert r.status == "skip"
    assert "no precision_at_threshold" in r.detail


def test_hit_rate_skip_no_filled_outcomes(temp_db):
    _seed_model(temp_db)
    # No predictions seeded
    r = check_hit_rate_tolerance(temp_db, "crypto_5d_test")
    assert r.status == "skip"
    assert r.sample_size == 0


# ──────────────────────────────────────────────────────────────────────
# Criterion 2 — lift over base rate
# ──────────────────────────────────────────────────────────────────────


def test_lift_pass(temp_db):
    _seed_model(temp_db, base_rate=0.30)
    # 50 hits / 100 → 0.50 ; lift = 0.50 / 0.30 = 1.67 ≥ 1.3 → pass
    items = [(0.7, True)] * 50 + [(0.7, False)] * 50
    _seed_predictions(temp_db, items=items)

    r = check_lift_window(temp_db, "crypto_5d_test")
    assert r.status == "pass"
    assert r.current_value == pytest.approx(50/100 / 0.30, rel=1e-3)


def test_lift_fail(temp_db):
    _seed_model(temp_db, base_rate=0.30)
    # 30 hits / 100 → 0.30 ; lift = 1.00 < 1.3 → fail
    items = [(0.7, True)] * 30 + [(0.7, False)] * 70
    _seed_predictions(temp_db, items=items)

    r = check_lift_window(temp_db, "crypto_5d_test")
    assert r.status == "fail"
    assert r.current_value == pytest.approx(1.0)


def test_lift_skip_no_base_rate(temp_db):
    _seed_model(temp_db, base_rate=0.0)
    r = check_lift_window(temp_db, "crypto_5d_test")
    assert r.status == "skip"


# ──────────────────────────────────────────────────────────────────────
# Criterion 3 — calibration buckets (absolute drift)
# ──────────────────────────────────────────────────────────────────────


def test_calibration_pass_when_aligned(temp_db):
    """In-bucket hit rates within ±10pp of midpoint → pass."""
    _seed_model(temp_db)
    # Bucket 0.50–0.55 (midpoint 0.525) — 10 hits out of 20 → 0.50 → dev -2.5pp
    # Bucket 0.55–0.60 (midpoint 0.575) — 12 hits out of 20 → 0.60 → dev +2.5pp
    items = []
    items += [(0.52, True)] * 10 + [(0.52, False)] * 10
    items += [(0.57, True)] * 12 + [(0.57, False)] * 8
    _seed_predictions(temp_db, items=items)

    r = check_calibration_buckets(temp_db, "crypto_5d_test")
    assert r.status == "pass"


def test_calibration_fail_three_consecutive_under_confident(temp_db):
    """Three adjacent buckets with hit rate ~30pp BELOW midpoint → fail
    (systematic over-confidence — model says 70% likely, only 40% hit)."""
    _seed_model(temp_db)
    items = []
    # Bucket 0.65-0.70 mid 0.675 — 4/20=0.20 dev -47pp
    items += [(0.67, True)] * 4 + [(0.67, False)] * 16
    # Bucket 0.70-0.75 mid 0.725 — 5/20=0.25 dev -47pp
    items += [(0.72, True)] * 5 + [(0.72, False)] * 15
    # Bucket 0.75-0.80 mid 0.775 — 6/20=0.30 dev -47pp
    items += [(0.77, True)] * 6 + [(0.77, False)] * 14
    _seed_predictions(temp_db, items=items)

    r = check_calibration_buckets(temp_db, "crypto_5d_test")
    assert r.status == "fail"
    assert "consecutive" in r.detail
    assert "UNDER" in r.detail


def test_calibration_fail_three_consecutive_over_confident(temp_db):
    """Three adjacent buckets with hit rate ~30pp ABOVE midpoint → fail
    (model under-confident; could trade more aggressively)."""
    _seed_model(temp_db)
    items = []
    items += [(0.52, True)] * 18 + [(0.52, False)] * 2     # mid 0.525, 0.90 dev +37pp
    items += [(0.57, True)] * 18 + [(0.57, False)] * 2     # mid 0.575, 0.90 dev +32pp
    items += [(0.62, True)] * 18 + [(0.62, False)] * 2     # mid 0.625, 0.90 dev +27pp
    _seed_predictions(temp_db, items=items)

    r = check_calibration_buckets(temp_db, "crypto_5d_test")
    assert r.status == "fail"
    assert "OVER" in r.detail


def test_calibration_small_samples_per_bucket_break_chain(temp_db):
    """KI-127: 3+ consecutive buckets each with < 10 samples must NOT
    fire the drift detector even at extreme deviations. At n=3 in a
    bucket the CI half-width exceeds 30pp; observed-vs-expected
    differences are noise, not signal."""
    _seed_model(temp_db)
    items = []
    # Three adjacent buckets each with only 3 samples but extreme
    # 100% / 0% rates → would fire absent the min_samples guard.
    items += [(0.52, True)] * 3 + [(0.52, False)] * 0    # n=3 dev +47.5pp
    items += [(0.57, True)] * 3 + [(0.57, False)] * 0    # n=3 dev +42.5pp
    items += [(0.62, True)] * 3 + [(0.62, False)] * 0    # n=3 dev +37.5pp
    _seed_predictions(temp_db, items=items)

    r = check_calibration_buckets(temp_db, "crypto_5d_test")
    assert r.status == "pass"
    assert "0/3 buckets above min_samples_per_bucket" in r.detail


def test_calibration_can_lower_min_samples_per_bucket(temp_db):
    """The guard is a function parameter — synthetic tests / future
    research can lower it to exercise the chain detector with smaller
    populations."""
    _seed_model(temp_db)
    items = []
    items += [(0.52, True)] * 3
    items += [(0.57, True)] * 3
    items += [(0.62, True)] * 3
    _seed_predictions(temp_db, items=items)

    r = check_calibration_buckets(
        temp_db, "crypto_5d_test", min_samples_per_bucket=2,
    )
    assert r.status == "fail"
    assert "OVER" in r.detail


def test_calibration_pass_when_only_two_adjacent_drifts(temp_db):
    """Two adjacent off-midpoint buckets is below the 3-consecutive
    threshold; isolated noise rather than systematic drift."""
    _seed_model(temp_db)
    items = []
    items += [(0.52, True)] * 18 + [(0.52, False)] * 2     # +37pp
    items += [(0.57, True)] * 18 + [(0.57, False)] * 2     # +32pp
    # Third bucket back near midpoint
    items += [(0.62, True)] * 12 + [(0.62, False)] * 8     # +7pp — within band
    _seed_predictions(temp_db, items=items)

    r = check_calibration_buckets(temp_db, "crypto_5d_test")
    assert r.status == "pass"


def test_compute_reliability_diagram_buckets_match_spec(temp_db):
    """9 buckets covering 0.50–0.95 by default. Half-open [low, high)
    on every bucket except the last, which is closed on both ends so
    0.95 lands somewhere."""
    _seed_model(temp_db)
    # Probability 0.67 → bucket 0.65-0.70 (clearly inside, not on edge)
    _seed_predictions(temp_db, items=[(0.67, True)] * 5)

    diag = compute_reliability_diagram(temp_db, "crypto_5d_test")
    assert len(diag) == 9
    assert diag[0].low == pytest.approx(0.50)
    assert diag[0].high == pytest.approx(0.55)
    assert diag[-1].low == pytest.approx(0.90)
    assert diag[-1].high == pytest.approx(0.95)
    # Bucket 0.65-0.70 should hold all 5 entries
    in_bucket = [b for b in diag if b.low == 0.65 and b.high == 0.70][0]
    assert in_bucket.n == 5
    assert in_bucket.actual_rate == pytest.approx(1.0)


def test_compute_reliability_diagram_boundary_belongs_to_next_bucket(temp_db):
    """Probability exactly equal to a bucket edge falls into the
    higher-numbered bucket per the [low, high) convention. Pin this
    so future renames don't accidentally flip the convention."""
    _seed_model(temp_db)
    _seed_predictions(temp_db, items=[(0.70, True)] * 5)
    diag = compute_reliability_diagram(temp_db, "crypto_5d_test")
    in_low_bucket = [b for b in diag if b.low == 0.65 and b.high == 0.70][0]
    in_high_bucket = [b for b in diag if b.low == 0.70 and b.high == 0.75][0]
    assert in_low_bucket.n == 0
    assert in_high_bucket.n == 5


def test_compute_reliability_diagram_top_edge_inclusive(temp_db):
    """The very last bucket (e.g. 0.90-0.95) is closed on the right so
    a prediction at probability 0.95 still has somewhere to land."""
    _seed_model(temp_db)
    _seed_predictions(temp_db, items=[(0.95, True)] * 3)
    diag = compute_reliability_diagram(temp_db, "crypto_5d_test")
    last = [b for b in diag if b.low == 0.90 and b.high == 0.95][0]
    assert last.n == 3


# ──────────────────────────────────────────────────────────────────────
# Criterion 4 — minimum sample
# ──────────────────────────────────────────────────────────────────────


def test_minimum_sample_pass_at_threshold(temp_db):
    _seed_model(temp_db)
    _seed_predictions(temp_db, items=[(0.7, True)] * 200)
    r = check_minimum_sample(temp_db, "crypto_5d_test")
    assert r.status == "pass"
    assert r.current_value == 200.0


def test_minimum_sample_fail_below_threshold(temp_db):
    _seed_model(temp_db)
    _seed_predictions(temp_db, items=[(0.7, True)] * 50)
    r = check_minimum_sample(temp_db, "crypto_5d_test")
    assert r.status == "fail"
    assert r.current_value == 50.0


# ──────────────────────────────────────────────────────────────────────
# evaluate_model + evaluate_all + verdict logic
# ──────────────────────────────────────────────────────────────────────


def test_evaluate_model_interim_when_below_sample_threshold(temp_db):
    """Below 200 samples → INTERIM regardless of other criterion outcomes."""
    _seed_model(temp_db)
    _seed_predictions(temp_db, items=[(0.7, True)] * 50)
    v = evaluate_model(temp_db, "crypto_5d_test")
    assert isinstance(v, Phase0Verdict)
    assert v.overall == "INTERIM"
    assert len(v.criteria) == 4
    # All four criteria still computed
    assert v.criteria["hit_rate_within_25pct"].status in ("pass", "fail", "skip")
    assert v.criteria["lift_over_base"].status in ("pass", "fail", "skip")
    assert v.criteria["calibration_buckets"].status in ("pass", "fail", "skip")
    assert v.criteria["minimum_sample"].status == "fail"
    assert len(v.reliability) == 9


def test_evaluate_model_pass_when_all_four_pass_at_sample(temp_db):
    """200 hits-aligned predictions: hit rate ~baseline, lift > 1.3,
    calibration aligned, sample met → PASS."""
    _seed_model(temp_db, precision_at_threshold=0.60, base_rate=0.30)
    items = []
    # Distribute predictions across buckets so calibration passes.
    # 50 in 0.55-0.60, 30 hits → 0.60 hit rate, midpoint 0.575, dev +2.5pp
    items += [(0.57, True)] * 30 + [(0.57, False)] * 20
    # 50 in 0.60-0.65, 30 hits → 0.60, midpoint 0.625, dev -2.5pp
    items += [(0.62, True)] * 30 + [(0.62, False)] * 20
    # 100 more aligned: 60% hit rate at probability 0.625
    items += [(0.62, True)] * 60 + [(0.62, False)] * 40
    _seed_predictions(temp_db, items=items)

    v = evaluate_model(temp_db, "crypto_5d_test")
    assert v.overall == "PASS", f"got {v.overall}; criteria={v.criteria}"


def test_evaluate_model_fail_when_sample_met_but_criterion_fails(temp_db):
    _seed_model(temp_db, precision_at_threshold=0.60, base_rate=0.30)
    # 200 outcomes, 30 hits / 200 = 0.15 → way below baseline → fail
    items = [(0.7, True)] * 30 + [(0.7, False)] * 170
    _seed_predictions(temp_db, items=items)

    v = evaluate_model(temp_db, "crypto_5d_test")
    assert v.overall == "FAIL"
    assert v.criteria["hit_rate_within_25pct"].status == "fail"
    assert v.criteria["lift_over_base"].status == "fail"


def test_evaluate_all_picks_active_models(temp_db):
    _seed_model(temp_db, model_id="crypto_5d_one", horizon="5d", is_active=True)
    _seed_model(temp_db, model_id="crypto_10d_two", horizon="10d", is_active=True)
    _seed_model(temp_db, model_id="crypto_5d_old", horizon="5d", is_active=False)

    out = evaluate_all(temp_db)
    ids = {v.model_id for v in out}
    assert "crypto_5d_one" in ids
    assert "crypto_10d_two" in ids
    assert "crypto_5d_old" not in ids


def test_evaluate_all_with_explicit_model_id(temp_db):
    _seed_model(temp_db, model_id="crypto_5d_one", horizon="5d", is_active=True)
    _seed_model(temp_db, model_id="crypto_10d_two", horizon="10d", is_active=True)
    out = evaluate_all(temp_db, model_id="crypto_10d_two")
    assert len(out) == 1
    assert out[0].model_id == "crypto_10d_two"


# ──────────────────────────────────────────────────────────────────────
# Sample accumulation projection
# ──────────────────────────────────────────────────────────────────────


def test_project_sample_accumulation_zero_rate(temp_db):
    _seed_model(temp_db)
    proj = project_sample_accumulation(temp_db, "crypto_5d_test")
    assert proj.n_filled_now == 0
    assert proj.days_to_threshold is None
    assert proj.eta is None


def test_project_sample_accumulation_linear_extrapolation(temp_db):
    _seed_model(temp_db)
    # 14 fills in last 7d → 2/day → from 100 to 200 = 100 / 2 = 50d
    items = [(0.7, True)] * 100
    # Half before the 7d window, half within.
    now = datetime.now(timezone.utc)
    for i, (prob, hit) in enumerate(items):
        # Older items are filled > 7 days ago, newer ones inside.
        days_ago = 14 - (i % 14)
        filled_at = now - timedelta(days=days_ago)
        temp_db.execute(
            """
            INSERT INTO crypto_ml_predictions
                (symbol, prediction_date, model_id, horizon,
                 predicted_probability, prediction_threshold,
                 actual_hit, outcome_filled_at, market_cap_bucket)
            VALUES (?, ?, 'crypto_5d_test', '5d', ?, 0.10, ?, ?, 'unknown')
            """,
            [f"COIN{i:04d}USDT", date.today() - timedelta(days=i % 21),
             prob, hit, filled_at],
        )

    proj = project_sample_accumulation(temp_db, "crypto_5d_test")
    assert proj.n_filled_now == 100
    assert proj.n_filled_last_7d > 0
    assert proj.days_to_threshold is not None and proj.days_to_threshold > 0
    assert proj.eta is not None


def test_project_sample_accumulation_already_at_threshold(temp_db):
    _seed_model(temp_db)
    _seed_predictions(temp_db, items=[(0.7, True)] * 250)
    proj = project_sample_accumulation(temp_db, "crypto_5d_test")
    assert proj.n_filled_now == 250
    assert proj.days_to_threshold is None
    assert proj.eta is None
