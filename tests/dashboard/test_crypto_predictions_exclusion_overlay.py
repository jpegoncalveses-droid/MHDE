"""Tests for the crypto predictions exclusion overlay.

The Crypto Predictions tab now surfaces filter exclusions per-row so
operators see which raw model outputs actually drive the engine.
Backend join: crypto_signal_exclusions joined to crypto_ml_predictions
on (symbol, model_id, export_date = prediction_date + 1 day). The
export-date / prediction-date mismatch is the same +1-day mapping the
trading-date-relabel branch shipped earlier today.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb
import pytest

from crypto.schema import create_all_tables
from dashboard.services import queries as q


@pytest.fixture
def crypto_db_with_predictions_and_exclusions():
    """In-memory engine DB seeded with two crypto predictions for
    prediction_date=2026-05-14, one of which has a matching exclusion
    row for the corresponding export_date=2026-05-15.
    """
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)

    # Two predictions for 2026-05-14, both 10d, same model_id.
    for sym, prob in [("SWARMSUSDT", 0.9065), ("TAGUSDT", 0.8956)]:
        conn.execute(
            "INSERT INTO crypto_ml_predictions "
            "(symbol, prediction_date, model_id, horizon, "
            " predicted_probability, prediction_threshold, market_cap_bucket) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [sym, date(2026, 5, 14), "crypto_10d_7760a3f6", "10d",
             prob, 0.10, "mid_alt"],
        )

    # Price rows at the prediction date + maturity so the join targets exist.
    for sym, p_pred, p_mat in [
        ("SWARMSUSDT", 0.01698, 0.013794),
        ("TAGUSDT", 0.001326, 0.001257),
    ]:
        conn.execute(
            "INSERT INTO crypto_prices_daily "
            "(symbol, trade_date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [sym, date(2026, 5, 14), p_pred, p_pred, p_pred, p_pred, 1000.0],
        )

    # Variant D exclusion of SWARMSUSDT — export_date = prediction_date + 1.
    conn.execute(
        "INSERT INTO crypto_signal_exclusions "
        "(export_date, symbol, model_id, raw_probability, "
        " dd90, ret60, ret5, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [date(2026, 5, 15), "SWARMSUSDT", "crypto_10d_7760a3f6",
         0.9065, -0.5920, 0.9808, -0.4372, "short_momentum"],
    )
    return conn


# ──────────────────────────────────────────────────────────────────────
# Query layer: LEFT JOIN crypto_signal_exclusions, expose is_excluded
# ──────────────────────────────────────────────────────────────────────


def test_get_crypto_predictions_exposes_is_excluded_column(
    crypto_db_with_predictions_and_exclusions,
):
    """The query must return is_excluded as a per-row boolean derived
    from the LEFT JOIN's hit/miss state."""
    df = q.get_crypto_predictions(
        crypto_db_with_predictions_and_exclusions, date(2026, 5, 14),
    )
    assert "is_excluded" in df.columns
    by_sym = {row["symbol"]: row for _, row in df.iterrows()}
    assert by_sym["SWARMSUSDT"]["is_excluded"] is True or \
           bool(by_sym["SWARMSUSDT"]["is_excluded"]) is True
    assert by_sym["TAGUSDT"]["is_excluded"] is False or \
           bool(by_sym["TAGUSDT"]["is_excluded"]) is False


def test_get_crypto_predictions_exposes_reason_and_trigger_value(
    crypto_db_with_predictions_and_exclusions,
):
    """Exclusion reason + the metric values needed to render the badge
    text (`short_momentum` with ret5, `post_parabolic` with dd90/ret60,
    or `post_parabolic_and_short_momentum` with all three).
    """
    df = q.get_crypto_predictions(
        crypto_db_with_predictions_and_exclusions, date(2026, 5, 14),
    )
    for col in ("exclusion_reason", "exclusion_dd90",
                "exclusion_ret60", "exclusion_ret5"):
        assert col in df.columns, f"missing column {col}; got {list(df.columns)}"

    sw = df[df["symbol"] == "SWARMSUSDT"].iloc[0]
    assert sw["exclusion_reason"] == "short_momentum"
    assert sw["exclusion_ret5"] == pytest.approx(-0.4372)

    tg = df[df["symbol"] == "TAGUSDT"].iloc[0]
    assert tg["exclusion_reason"] is None or _is_null(tg["exclusion_reason"])


def test_get_crypto_predictions_uses_export_date_eq_prediction_date_plus_one(
    crypto_db_with_predictions_and_exclusions,
):
    """The export_date in crypto_signal_exclusions is the TRADING date
    (prediction_date + 1 calendar day). The join must apply that
    mapping; an exclusion row dated to the prediction_date itself must
    NOT match.
    """
    conn = crypto_db_with_predictions_and_exclusions
    # Add a misdated exclusion row (export_date = prediction_date, not
    # prediction_date + 1) — must NOT light up is_excluded.
    conn.execute(
        "INSERT INTO crypto_signal_exclusions "
        "(export_date, symbol, model_id, raw_probability, ret5, reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [date(2026, 5, 14), "TAGUSDT", "crypto_10d_7760a3f6",
         0.8956, -0.05, "short_momentum"],
    )
    df = q.get_crypto_predictions(conn, date(2026, 5, 14))
    tg = df[df["symbol"] == "TAGUSDT"].iloc[0]
    assert bool(tg["is_excluded"]) is False, (
        f"export_date == prediction_date must NOT match the +1-day "
        f"trading-date join; got is_excluded={tg['is_excluded']}, "
        f"reason={tg.get('exclusion_reason')!r}"
    )


def test_get_crypto_predictions_preserves_model_rank_order(
    crypto_db_with_predictions_and_exclusions,
):
    """Excluded rows stay at their model-rank position — the operator
    needs to see "model says #1 but excluded" rather than the disagreement
    being hidden by reordering."""
    df = q.get_crypto_predictions(
        crypto_db_with_predictions_and_exclusions, date(2026, 5, 14),
    )
    # Sorted by predicted_probability DESC within horizon: SWARMS (0.9065)
    # before TAG (0.8956), regardless of exclusion.
    assert df.iloc[0]["symbol"] == "SWARMSUSDT"
    assert df.iloc[1]["symbol"] == "TAGUSDT"


def test_get_crypto_predictions_no_exclusions_returns_all_false(
    crypto_db_with_predictions_and_exclusions,
):
    """Different prediction_date with no matching exclusion row → every
    is_excluded=False; reason columns NaN/None."""
    conn = crypto_db_with_predictions_and_exclusions
    # Seed a separate prediction on a different date with no exclusion.
    conn.execute(
        "INSERT INTO crypto_ml_predictions "
        "(symbol, prediction_date, model_id, horizon, "
        " predicted_probability, prediction_threshold, market_cap_bucket) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ["BTCUSDT", date(2026, 5, 10), "crypto_10d_7760a3f6", "10d",
         0.70, 0.10, "mega"],
    )
    conn.execute(
        "INSERT INTO crypto_prices_daily "
        "(symbol, trade_date, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ["BTCUSDT", date(2026, 5, 10), 50000.0, 50000.0, 50000.0,
         50000.0, 1000.0],
    )
    df = q.get_crypto_predictions(conn, date(2026, 5, 10))
    assert len(df) == 1
    assert bool(df.iloc[0]["is_excluded"]) is False


# ──────────────────────────────────────────────────────────────────────
# Badge-text formatter
# ──────────────────────────────────────────────────────────────────────


def test_format_exclusion_badge_short_momentum():
    """Badge for short_momentum names the rule and the trigger value."""
    from dashboard.services.maturity import format_crypto_exclusion_badge
    text = format_crypto_exclusion_badge(
        reason="short_momentum",
        dd90=None, ret60=None, ret5=-0.4372,
    )
    assert "EXCLUDED" in text
    assert "short_momentum" in text
    assert "-43.7" in text or "-43.72" in text


def test_format_exclusion_badge_post_parabolic():
    """Badge for post_parabolic names the rule and BOTH trigger values."""
    from dashboard.services.maturity import format_crypto_exclusion_badge
    text = format_crypto_exclusion_badge(
        reason="post_parabolic",
        dd90=-0.25, ret60=2.5, ret5=None,
    )
    assert "EXCLUDED" in text
    assert "post_parabolic" in text
    assert "-25" in text or "-25.0" in text
    assert "250" in text or "+250" in text or "+2.5" in text


def test_format_exclusion_badge_both_rules():
    """Combined-rule badge mentions both and surfaces all three trigger
    values."""
    from dashboard.services.maturity import format_crypto_exclusion_badge
    text = format_crypto_exclusion_badge(
        reason="post_parabolic_and_short_momentum",
        dd90=-0.25, ret60=2.5, ret5=-0.35,
    )
    assert "EXCLUDED" in text
    assert "post_parabolic" in text or "BOTH" in text or "both" in text


def test_format_exclusion_badge_none_returns_empty():
    """When reason is None (no exclusion), the badge is empty — the
    renderer falls back to pending / outcome display."""
    from dashboard.services.maturity import format_crypto_exclusion_badge
    assert format_crypto_exclusion_badge(
        reason=None, dd90=None, ret60=None, ret5=None,
    ) == ""


# ──────────────────────────────────────────────────────────────────────
# Summary line formatter
# ──────────────────────────────────────────────────────────────────────


def test_format_predictions_summary_with_exclusions():
    """Summary names total, active, and filtered counts when any
    exclusion is present."""
    from dashboard.services.maturity import format_crypto_predictions_summary
    text = format_crypto_predictions_summary(
        n_total=30, n_excluded=1, exclusion_reasons={"short_momentum"},
    )
    assert "30" in text
    assert "29" in text and "active" in text.lower()
    assert "1" in text and "filtered" in text.lower()
    assert "short_momentum" in text


def test_format_predictions_summary_no_exclusions_is_unchanged():
    """When zero exclusions, summary is the historical "N coins" form
    (the operator's eye doesn't need a "0 filtered" call-out)."""
    from dashboard.services.maturity import format_crypto_predictions_summary
    text = format_crypto_predictions_summary(
        n_total=30, n_excluded=0, exclusion_reasons=set(),
    )
    # Plain count form; no "filtered" mention.
    assert "30" in text
    assert "filtered" not in text.lower()
    assert "excluded" not in text.lower()


def test_format_predictions_summary_multiple_distinct_reasons():
    """When multiple distinct rules fired today, list them all."""
    from dashboard.services.maturity import format_crypto_predictions_summary
    text = format_crypto_predictions_summary(
        n_total=44, n_excluded=4,
        exclusion_reasons={"short_momentum", "post_parabolic_and_short_momentum"},
    )
    assert "short_momentum" in text
    assert "post_parabolic_and_short_momentum" in text


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _is_null(v) -> bool:
    import pandas as pd
    return v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT
