"""Tests for the crypto dashboard trading-date relabel.

Operational context: crypto predictions are written with
prediction_date = MAX(crypto_ml_features.trade_date) — i.e. the
just-completed bar = T-1 calendar day. The engine then consumes those
predictions for entries on the FOLLOWING calendar day. From the
operator's POV the "trading date" is T-1 + 1 = today; the dashboard's
old "Prediction date" label conflated the feature-snapshot date with
the trading date and was confusing every morning at 00:30 UTC.

This branch (feat-dashboard-crypto-trading-date-relabel) keeps the
backend semantics unchanged and adds a presentation-layer translation:

  - crypto_feature_date_to_trading_date(prediction_date) =
    prediction_date + 1 calendar day (crypto trades 24/7; no
    trading-day-skip needed)
  - crypto_trading_date_to_feature_date(trading_date) =
    trading_date − 1 calendar day (inverse, used by the selectbox
    callback to translate the operator's pick back to the backend
    prediction_date)
  - format_crypto_trading_date_banner(prediction_date, predicted_at,
    today) returns the three-reference banner text shown below the
    selector.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from dashboard.services.maturity import (
    crypto_feature_date_to_trading_date,
    crypto_trading_date_to_feature_date,
    format_crypto_trading_date_banner,
)


# ──────────────────────────────────────────────────────────────────────
# T-1 mapping (purely arithmetic, 24/7 market)
# ──────────────────────────────────────────────────────────────────────


def test_feature_date_to_trading_date_adds_one_calendar_day():
    """Crypto T-1: prediction_date is the feature day; trading date is
    the next calendar day. No trading-day-skip — crypto is 24/7."""
    assert crypto_feature_date_to_trading_date(date(2026, 5, 14)) == date(2026, 5, 15)


def test_trading_date_to_feature_date_subtracts_one_calendar_day():
    """Inverse mapping — selector translates operator's trading-date
    pick back to the backend prediction_date for the DB query."""
    assert crypto_trading_date_to_feature_date(date(2026, 5, 15)) == date(2026, 5, 14)


def test_feature_to_trading_round_trip_is_identity():
    """Composing the two mappings must be a no-op (presentation layer
    only; no information loss)."""
    for d in (date(2026, 1, 1), date(2026, 5, 15), date(2026, 12, 31)):
        assert crypto_trading_date_to_feature_date(
            crypto_feature_date_to_trading_date(d)
        ) == d


def test_mappings_handle_year_boundary():
    """31 Dec → 1 Jan of the next year. No weekday / market-hours
    consideration applies — crypto trades through the new year."""
    assert crypto_feature_date_to_trading_date(date(2026, 12, 31)) == date(2027, 1, 1)
    assert crypto_trading_date_to_feature_date(date(2027, 1, 1)) == date(2026, 12, 31)


# ──────────────────────────────────────────────────────────────────────
# Three-reference banner
# ──────────────────────────────────────────────────────────────────────


def test_banner_shows_all_three_references_when_predicted_at_present():
    """The banner must surface trading date, features-as-of date, and
    the generation timestamp — they're all operationally meaningful
    but easily confused without explicit naming.
    """
    pred_date = date(2026, 5, 14)
    predicted_at = datetime(2026, 5, 15, 0, 30, 8, tzinfo=timezone.utc)
    today = date(2026, 5, 15)
    text = format_crypto_trading_date_banner(
        prediction_date=pred_date,
        predicted_at=predicted_at,
        today=today,
    )
    assert "2026-05-15" in text, "must name the trading date"
    assert "2026-05-14" in text, "must name the feature-as-of date"
    assert "00:30:08" in text, "must surface the generation time"


def test_banner_labels_trading_date_explicitly():
    """The operator-facing label is 'Trading date'; the feature date
    keeps its 'features as of' framing."""
    text = format_crypto_trading_date_banner(
        prediction_date=date(2026, 5, 14),
        predicted_at=datetime(2026, 5, 15, 0, 30, 0, tzinfo=timezone.utc),
        today=date(2026, 5, 15),
    )
    assert "Trading date" in text
    assert "features as of" in text.lower()


def test_banner_handles_null_predicted_at():
    """Pre-migration rows have NULL predicted_at (migration v11 added
    the column without backfill). Banner must still render — just
    label the generation time 'N/A' rather than crashing or omitting
    the line."""
    text = format_crypto_trading_date_banner(
        prediction_date=date(2026, 5, 14),
        predicted_at=None,
        today=date(2026, 5, 15),
    )
    assert "2026-05-15" in text  # trading date
    assert "2026-05-14" in text  # feature date
    assert "N/A" in text


def test_banner_handles_nan_predicted_at():
    """DuckDB → pandas NULL surfaces as pd.NaT or float NaN depending
    on the column type. The banner must treat both as missing."""
    import pandas as pd
    text = format_crypto_trading_date_banner(
        prediction_date=date(2026, 5, 14),
        predicted_at=pd.NaT,
        today=date(2026, 5, 15),
    )
    assert "N/A" in text


def test_banner_predicted_at_renders_utc_timezone():
    """Generation timestamps are stored UTC (CURRENT_TIMESTAMP in
    DuckDB). Surface that explicitly so the operator doesn't read it
    as local time."""
    text = format_crypto_trading_date_banner(
        prediction_date=date(2026, 5, 14),
        predicted_at=datetime(2026, 5, 15, 0, 30, 8, tzinfo=timezone.utc),
        today=date(2026, 5, 15),
    )
    assert "UTC" in text
