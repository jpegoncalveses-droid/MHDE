"""Tests for crypto/execution/backtest/selection.py.

Covers the 10 cases requested before moving on to harness.py:

    1. Top N basic (5 predictions, n=3 → top 3)
    2. Top N with ties → deterministic alphabetical break
    3. Top N when n > prediction count → returns all available
    4. Top N with empty input → empty output, no crash
    5. Threshold basic (above kept, below excluded)
    6. Threshold all-below → empty output
    7. Threshold edge: probability == threshold → kept (>=)
    8. Threshold validation: outside [0, 1] → ValueError
    9. Top N validation: n <= 0 → ValueError
   10. Multi-day input → rank_in_day resets per date

Pure pandas; imports nothing from equity / FX / shared ml.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from crypto.execution.backtest.selection import (
    select_threshold,
    select_top_n,
)


_OUTPUT_COLUMNS = ["coin", "date", "probability", "rank_in_day"]


def _make(rows: list[tuple[str, date, float]]) -> pd.DataFrame:
    """Build a (coin, date, probability) input frame."""
    return pd.DataFrame(rows, columns=["coin", "date", "probability"])


# ──────────────────────────────────────────────────────────────────────
# Top N — case 1: basic
# ──────────────────────────────────────────────────────────────────────


def test_top_n_basic_keeps_top_three_of_five():
    d = date(2026, 5, 1)
    df = _make([
        ("BTC", d, 0.72),
        ("ETH", d, 0.65),
        ("SOL", d, 0.81),
        ("AVAX", d, 0.55),
        ("LINK", d, 0.49),
    ])
    out = select_top_n(df, n=3)
    assert list(out.columns) == _OUTPUT_COLUMNS
    assert len(out) == 3
    assert out["coin"].tolist() == ["SOL", "BTC", "ETH"]
    assert out["rank_in_day"].tolist() == [1, 2, 3]
    assert out["probability"].tolist() == [0.81, 0.72, 0.65]


# ──────────────────────────────────────────────────────────────────────
# Top N — case 2: ties broken alphabetically by coin
# ──────────────────────────────────────────────────────────────────────


def test_top_n_ties_broken_alphabetically():
    d = date(2026, 5, 1)
    df = _make([
        ("ZEC",  d, 0.70),
        ("ETH",  d, 0.70),
        ("BTC",  d, 0.70),
        ("AVAX", d, 0.55),
    ])
    out = select_top_n(df, n=3)
    # Three coins tied at 0.70 → output must be alphabetical: BTC, ETH, ZEC.
    assert out["coin"].tolist() == ["BTC", "ETH", "ZEC"]
    assert out["rank_in_day"].tolist() == [1, 2, 3]


def test_top_n_is_deterministic_across_input_orderings():
    """Shuffling input rows must not change the output order."""
    d = date(2026, 5, 1)
    rows = [
        ("BTC", d, 0.70), ("ETH", d, 0.70), ("ZEC", d, 0.70),
        ("AVAX", d, 0.55), ("SOL", d, 0.81),
    ]
    out_a = select_top_n(_make(rows), n=4)
    out_b = select_top_n(_make(list(reversed(rows))), n=4)
    pd.testing.assert_frame_equal(out_a, out_b)


# ──────────────────────────────────────────────────────────────────────
# Top N — case 3: n greater than predictions returns all
# ──────────────────────────────────────────────────────────────────────


def test_top_n_when_n_exceeds_input_returns_all():
    d = date(2026, 5, 1)
    df = _make([("BTC", d, 0.72), ("ETH", d, 0.65)])
    out = select_top_n(df, n=10)
    assert len(out) == 2
    assert out["coin"].tolist() == ["BTC", "ETH"]
    assert out["rank_in_day"].tolist() == [1, 2]


# ──────────────────────────────────────────────────────────────────────
# Top N — case 4: empty input
# ──────────────────────────────────────────────────────────────────────


def test_top_n_empty_input_returns_empty():
    df = pd.DataFrame(columns=["coin", "date", "probability"])
    out = select_top_n(df, n=3)
    assert out.empty
    assert list(out.columns) == _OUTPUT_COLUMNS


# ──────────────────────────────────────────────────────────────────────
# Threshold — case 5: basic
# ──────────────────────────────────────────────────────────────────────


def test_threshold_basic_keeps_above_excludes_below():
    d = date(2026, 5, 1)
    df = _make([
        ("BTC", d, 0.72),
        ("ETH", d, 0.50),
        ("SOL", d, 0.81),
        ("AVAX", d, 0.40),
    ])
    out = select_threshold(df, threshold=0.55)
    assert list(out.columns) == _OUTPUT_COLUMNS
    # 0.72 and 0.81 retained; sorted desc with rank reset.
    assert out["coin"].tolist() == ["SOL", "BTC"]
    assert out["rank_in_day"].tolist() == [1, 2]


# ──────────────────────────────────────────────────────────────────────
# Threshold — case 6: all below threshold
# ──────────────────────────────────────────────────────────────────────


def test_threshold_all_below_returns_empty():
    d = date(2026, 5, 1)
    df = _make([("BTC", d, 0.40), ("ETH", d, 0.30)])
    out = select_threshold(df, threshold=0.55)
    assert out.empty
    assert list(out.columns) == _OUTPUT_COLUMNS


# ──────────────────────────────────────────────────────────────────────
# Threshold — case 7: edge (probability exactly equal → included)
# ──────────────────────────────────────────────────────────────────────


def test_threshold_inclusive_keeps_exact_equal():
    """Comparison is `>=`, so a probability equal to the threshold is kept."""
    d = date(2026, 5, 1)
    df = _make([("BTC", d, 0.55), ("ETH", d, 0.5499)])
    out = select_threshold(df, threshold=0.55)
    assert out["coin"].tolist() == ["BTC"]
    assert out["probability"].tolist() == [0.55]


# ──────────────────────────────────────────────────────────────────────
# Validation — cases 8 and 9
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0, -1.0])
def test_threshold_outside_unit_interval_raises(bad):
    d = date(2026, 5, 1)
    df = _make([("BTC", d, 0.7)])
    with pytest.raises(ValueError, match="threshold"):
        select_threshold(df, threshold=bad)


def test_threshold_zero_and_one_are_accepted():
    d = date(2026, 5, 1)
    df = _make([("BTC", d, 0.7)])
    # Boundary values are valid.
    select_threshold(df, threshold=0.0)
    select_threshold(df, threshold=1.0)


@pytest.mark.parametrize("bad_n", [0, -1, -100])
def test_top_n_non_positive_raises(bad_n):
    d = date(2026, 5, 1)
    df = _make([("BTC", d, 0.7)])
    with pytest.raises(ValueError, match="n must be a positive integer"):
        select_top_n(df, n=bad_n)


def test_top_n_non_integer_raises():
    d = date(2026, 5, 1)
    df = _make([("BTC", d, 0.7)])
    with pytest.raises(ValueError, match="n must be a positive integer"):
        select_top_n(df, n=2.5)  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────
# Case 10: multi-day input — rank resets per date
# ──────────────────────────────────────────────────────────────────────


def test_top_n_multi_day_rank_resets_per_date():
    d1, d2 = date(2026, 5, 1), date(2026, 5, 2)
    df = _make([
        ("BTC",  d1, 0.72),
        ("ETH",  d1, 0.65),
        ("SOL",  d1, 0.81),
        ("BTC",  d2, 0.55),
        ("ETH",  d2, 0.78),
        ("AVAX", d2, 0.60),
    ])
    out = select_top_n(df, n=2)
    # Day 1: top 2 → SOL (0.81), BTC (0.72)
    # Day 2: top 2 → ETH (0.78), AVAX (0.60)
    assert len(out) == 4
    day1 = out[out["date"] == d1].reset_index(drop=True)
    day2 = out[out["date"] == d2].reset_index(drop=True)
    assert day1["coin"].tolist() == ["SOL", "BTC"]
    assert day1["rank_in_day"].tolist() == [1, 2]
    assert day2["coin"].tolist() == ["ETH", "AVAX"]
    assert day2["rank_in_day"].tolist() == [1, 2]


def test_threshold_multi_day_rank_resets_per_date():
    d1, d2 = date(2026, 5, 1), date(2026, 5, 2)
    df = _make([
        ("BTC",  d1, 0.72),
        ("ETH",  d1, 0.50),   # excluded
        ("SOL",  d1, 0.81),
        ("BTC",  d2, 0.78),
        ("ETH",  d2, 0.60),
        ("AVAX", d2, 0.40),   # excluded
    ])
    out = select_threshold(df, threshold=0.55)
    day1 = out[out["date"] == d1].reset_index(drop=True)
    day2 = out[out["date"] == d2].reset_index(drop=True)
    assert day1["coin"].tolist() == ["SOL", "BTC"]
    assert day1["rank_in_day"].tolist() == [1, 2]
    assert day2["coin"].tolist() == ["BTC", "ETH"]
    assert day2["rank_in_day"].tolist() == [1, 2]


# ──────────────────────────────────────────────────────────────────────
# Misc — input not mutated; extra columns ignored
# ──────────────────────────────────────────────────────────────────────


def test_input_dataframe_is_not_mutated():
    d = date(2026, 5, 1)
    df = _make([("BTC", d, 0.72), ("ETH", d, 0.65)])
    snapshot = df.copy()
    select_top_n(df, n=1)
    select_threshold(df, threshold=0.5)
    pd.testing.assert_frame_equal(df, snapshot)


def test_extra_input_columns_are_ignored_in_output():
    """Input may carry horizon, predicted_class, etc.; output keeps only the
    documented columns."""
    d = date(2026, 5, 1)
    df = pd.DataFrame(
        [
            ("BTC", d, 0.72, "5d", 1),
            ("ETH", d, 0.65, "5d", 1),
        ],
        columns=["coin", "date", "probability", "horizon", "predicted_class"],
    )
    out = select_top_n(df, n=2)
    assert list(out.columns) == _OUTPUT_COLUMNS
