"""Tests for crypto/ml/phase0_report.py."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from crypto.ml.phase0_evaluate import (
    CRYPTO,
    CriterionResult,
    Phase0Verdict,
    ReliabilityBucket,
    SampleAccumulationProjection,
    evaluate_all,
    project_sample_accumulation,
)
from crypto.ml.phase0_report import (
    build_report,
    default_report_path,
    format_reliability_diagram,
    format_report,
    format_verdict,
    save_report,
)


def _bucket(low, high, n, hits):
    midpoint = (low + high) / 2
    if n == 0:
        return ReliabilityBucket(low=low, high=high, midpoint=midpoint,
                                 n=0, n_hits=0, actual_rate=None,
                                 deviation_pp=None)
    actual = hits / n
    return ReliabilityBucket(low=low, high=high, midpoint=midpoint,
                             n=n, n_hits=hits, actual_rate=actual,
                             deviation_pp=(actual - midpoint) * 100)


def _criterion(name, status, current=None, expected=None,
               lo=None, hi=None, sample=0, detail=""):
    return CriterionResult(
        name=name, status=status,
        current_value=current, expected_value=expected,
        threshold_lo=lo, threshold_hi=hi,
        sample_size=sample, detail=detail or f"{name} {status}",
    )


def _verdict_interim_below_sample():
    return Phase0Verdict(
        model_id="crypto_5d_ab428f75", horizon="5d", sample_size=32,
        criteria={
            "hit_rate_within_25pct": _criterion(
                "hit_rate_within_25pct", "pass", current=0.81, expected=0.52,
                lo=0.39, hi=0.65, sample=32,
                detail="live precision 0.81 vs baseline 0.52 (within band)",
            ),
            "lift_over_base": _criterion(
                "lift_over_base", "pass", current=3.76, expected=1.3,
                lo=1.3, sample=32, detail="lift 3.76× over base 0.216",
            ),
            "calibration_buckets": _criterion(
                "calibration_buckets", "pass", current=4.5, expected=0,
                lo=-10, hi=10, sample=32, detail="no run of ≥3 buckets",
            ),
            "minimum_sample": _criterion(
                "minimum_sample", "fail", current=32, expected=200,
                lo=200, sample=32, detail="32 vs threshold 200",
            ),
        },
        reliability=[
            _bucket(0.50, 0.55, 4, 2),
            _bucket(0.55, 0.60, 5, 3),
            _bucket(0.60, 0.65, 8, 5),
            _bucket(0.65, 0.70, 7, 6),
            _bucket(0.70, 0.75, 4, 4),
            _bucket(0.75, 0.80, 2, 2),
            _bucket(0.80, 0.85, 1, 1),
            _bucket(0.85, 0.90, 0, 0),
            _bucket(0.90, 0.95, 1, 1),
        ],
        overall="INTERIM",
    )


# ──────────────────────────────────────────────────────────────────────
# Reliability diagram rendering
# ──────────────────────────────────────────────────────────────────────


def test_format_reliability_diagram_empty():
    assert "no buckets" in format_reliability_diagram([])


def test_format_reliability_diagram_includes_legend_and_buckets():
    buckets = [
        _bucket(0.50, 0.55, 10, 5),    # 50%, midpoint 52.5%, dev -2.5pp
        _bucket(0.55, 0.60, 0, 0),     # empty
        _bucket(0.60, 0.65, 8, 4),     # 50%, midpoint 62.5%, dev -12.5pp
    ]
    text = format_reliability_diagram(buckets)
    # Header row + separator + 3 data rows + blank line + legend
    assert "0.50–0.55" in text
    assert "0.55–0.60" in text
    assert "0.60–0.65" in text
    assert "Legend" in text
    # Empty bucket gets dots ("·") in the bar — no '#' or '|'
    lines = text.splitlines()
    empty_row = next(ln for ln in lines if "0.55–0.60" in ln)
    assert "—" in empty_row  # empty observed
    # Negative deviation rendered with minus sign
    drift_row = next(ln for ln in lines if "0.60–0.65" in ln)
    assert "-12.5pp" in drift_row


def test_format_reliability_diagram_match_marker():
    """When observed and expected land in the same column, the bar
    shows 'X' instead of '#'."""
    # Bucket midpoint 0.525, observed 0.525 (10 hits / 20). With
    # bar_width=20, expected_pos = round(10.5) = 11, observed_pos = 11
    # — both at column 11 → 'X'.
    buckets = [_bucket(0.50, 0.55, 20, 10)]
    text = format_reliability_diagram(buckets, bar_width=20)
    assert "X" in text


# ──────────────────────────────────────────────────────────────────────
# Verdict block
# ──────────────────────────────────────────────────────────────────────


def test_format_verdict_interim_includes_metrics_and_disclaimer():
    v = _verdict_interim_below_sample()
    text = format_verdict(v)
    # INTERIM disclaimer present
    assert "interim" in text.lower()
    # All four criteria mentioned in the table
    assert "`hit_rate_within_25pct`" in text
    assert "`lift_over_base`" in text
    assert "`calibration_buckets`" in text
    assert "`minimum_sample`" in text
    # Reliability diagram included
    assert "Reliability diagram" in text


def test_format_verdict_pass_no_interim_disclaimer():
    """A PASS verdict shouldn't carry the INTERIM language."""
    v = _verdict_interim_below_sample()
    v.criteria["minimum_sample"] = _criterion(
        "minimum_sample", "pass", current=250, expected=200,
        lo=200, sample=250, detail="250 vs 200",
    )
    v.sample_size = 250
    object.__setattr__(v, "overall", "PASS")
    text = format_verdict(v)
    assert "interim" not in text.lower()


def test_format_verdict_includes_accumulation_when_provided():
    v = _verdict_interim_below_sample()
    proj = SampleAccumulationProjection(
        model_id=v.model_id, n_filled_now=32,
        n_filled_threshold=200, n_filled_last_7d=14,
        days_to_threshold=84.0, eta="2026-08-01",
    )
    text = format_verdict(v, accumulation=proj)
    assert "Sample accumulation" in text
    assert "2026-08-01" in text
    assert "14 fills" in text


# ──────────────────────────────────────────────────────────────────────
# Top-level report
# ──────────────────────────────────────────────────────────────────────


def test_format_report_empty():
    text = format_report([])
    assert "Phase 0 calibration report" in text
    assert "No active models" in text


def test_format_report_two_models_summary():
    v1 = _verdict_interim_below_sample()
    v2 = Phase0Verdict(
        model_id="crypto_10d_db171418", horizon="10d", sample_size=57,
        criteria=v1.criteria, reliability=v1.reliability,
        overall="INTERIM",
    )
    text = format_report([v1, v2], report_date=date(2026, 5, 9))
    assert "2026-05-09" in text
    assert "## Summary" in text
    # Both models in summary table
    assert "`crypto_5d_ab428f75`" in text
    assert "`crypto_10d_db171418`" in text


# ──────────────────────────────────────────────────────────────────────
# Save / default path
# ──────────────────────────────────────────────────────────────────────


def test_default_report_path_uses_today_when_no_date():
    p = default_report_path()
    assert p.parent.name == "reports"
    assert p.name.startswith("phase0_report_")
    assert p.name.endswith(".md")


def test_save_report_writes_file_and_creates_parent(tmp_path):
    target = tmp_path / "deep" / "nested" / "report.md"
    out = save_report("hello", path=target)
    assert out == target
    assert target.read_text() == "hello"


# ──────────────────────────────────────────────────────────────────────
# build_report end-to-end
# ──────────────────────────────────────────────────────────────────────


def test_build_report_renders_against_temp_db_with_no_active_models(temp_db):
    text = build_report(temp_db)
    assert "Phase 0 calibration report" in text
    assert "No active models" in text


def test_build_report_renders_against_temp_db_with_seeded_model(temp_db):
    """End-to-end smoke: seed one model + a handful of predictions and
    confirm the report renders without crashing."""
    temp_db.execute(
        """
        INSERT INTO crypto_ml_model_runs
            (model_id, horizon, target_threshold, base_rate,
             precision_at_threshold, lift_over_base,
             train_start, train_end, test_start, test_end, is_active)
        VALUES ('crypto_5d_test', '5d', 0.10, 0.30, 0.60, 2.0,
                '2024-01-01', '2025-04-04',
                '2025-04-05', '2025-04-30', true)
        """,
    )
    now = datetime.now(timezone.utc)
    today = now.date()
    for i in range(20):
        temp_db.execute(
            """
            INSERT INTO crypto_ml_predictions
                (symbol, prediction_date, model_id, horizon,
                 predicted_probability, prediction_threshold,
                 actual_hit, outcome_filled_at, market_cap_bucket)
            VALUES (?, ?, 'crypto_5d_test', '5d', 0.65, 0.10, ?, ?, 'unknown')
            """,
            [f"COIN{i:04d}USDT", today - timedelta(days=i % 14),
             i % 2 == 0, now - timedelta(days=3)],
        )

    text = build_report(temp_db)
    assert "Phase 0 calibration report" in text
    assert "crypto_5d_test" in text
    assert "INTERIM" in text     # 20 filled, well below 200
    assert "Reliability diagram" in text
