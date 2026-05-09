"""Selection rules for the crypto execution backtest.

Per ``crypto/execution/backtest/SPEC.md`` — Selection rules:

  - Top N (default N = 6)
  - Threshold (default p > 0.55)

Both functions take a per-day prediction batch (a long-format DataFrame
spanning one or more dates) and return the subset that should be opened
as trades on each day.

Determinism — same input always produces the same output. Probabilities
are sorted descending; coins tied on probability are broken alphabetically
ascending by symbol so the harness's downstream trade order is stable
across runs (important for reproducible backtests).

Scope — these functions output the **raw filtered list** per day. They
do not look at currently open positions or enforce a concurrent-position
cap. Skip-on-duplicate / position-limit logic lives in the harness layer
per the spec's "treat each prediction as an independent trade" stance
during Phase 1.

This module is pure pandas — no DuckDB, no equity / FX / shared-ML imports.
"""
from __future__ import annotations

import pandas as pd


_OUTPUT_COLUMNS: list[str] = ["coin", "date", "probability", "rank_in_day"]


def _sort_and_rank(predictions_df: pd.DataFrame) -> pd.DataFrame:
    """Sort by (date asc, probability desc, coin asc) and assign per-day rank.

    Used by both selection rules so the tie-breaking convention is identical.
    """
    sorted_df = predictions_df.sort_values(
        by=["date", "probability", "coin"],
        ascending=[True, False, True],
        kind="mergesort",  # stable, preserves prior ordering on equal keys
    ).reset_index(drop=True)
    sorted_df["rank_in_day"] = sorted_df.groupby("date").cumcount() + 1
    return sorted_df


def select_top_n(predictions_df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Keep the top ``n`` predictions per day by probability.

    Args:
        predictions_df: DataFrame with at minimum the columns
            ``["coin", "date", "probability"]``. May span multiple dates;
            extra columns are ignored.
        n: number of top predictions to keep per day. Must be ``> 0``.
            If a day has fewer than ``n`` predictions, all of them are
            kept (no padding).

    Returns:
        DataFrame with columns ``["coin", "date", "probability", "rank_in_day"]``,
        sorted by ``(date asc, rank_in_day asc)``. ``rank_in_day`` is
        1-indexed (1 = highest probability, broken alphabetically) and
        resets per date.

    Raises:
        ValueError: if ``n <= 0``.
    """
    if not isinstance(n, int) or n <= 0:
        raise ValueError(f"n must be a positive integer, got {n!r}")
    if predictions_df.empty:
        return pd.DataFrame(columns=_OUTPUT_COLUMNS)

    ranked = _sort_and_rank(predictions_df)
    return ranked.loc[ranked["rank_in_day"] <= n, _OUTPUT_COLUMNS].reset_index(
        drop=True
    )


def select_threshold(
    predictions_df: pd.DataFrame, threshold: float
) -> pd.DataFrame:
    """Keep predictions whose probability is at least ``threshold``.

    The comparison is inclusive (``probability >= threshold``) — a
    prediction whose probability is exactly equal to the threshold is
    retained.

    Args:
        predictions_df: DataFrame with at minimum the columns
            ``["coin", "date", "probability"]``.
        threshold: probability cutoff in ``[0, 1]``.

    Returns:
        DataFrame with columns ``["coin", "date", "probability", "rank_in_day"]``,
        sorted by ``(date asc, rank_in_day asc)``. ``rank_in_day`` reflects
        ordering within the retained set on each date and resets per date.

    Raises:
        ValueError: if ``threshold`` is outside ``[0, 1]``.
    """
    if not 0.0 <= float(threshold) <= 1.0:
        raise ValueError(f"threshold must be in [0, 1], got {threshold!r}")
    if predictions_df.empty:
        return pd.DataFrame(columns=_OUTPUT_COLUMNS)

    filtered = predictions_df.loc[predictions_df["probability"] >= threshold]
    if filtered.empty:
        return pd.DataFrame(columns=_OUTPUT_COLUMNS)

    ranked = _sort_and_rank(filtered)
    return ranked[_OUTPUT_COLUMNS].reset_index(drop=True)
