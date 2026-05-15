"""Pure helpers for the dashboard prediction tabs:

- Compute % move since prediction (filled vs pending)
- Compute time remaining until maturity (days for equity/crypto, hours for FX)
- Format both as user-facing strings ("+1.8%", "3d", "Past due")
- Estimate maturity_date for pending equity predictions (calendar
  approximation via business-day forward counting; the SQL
  trading-rows-forward JOIN can only resolve maturity once the future
  rows actually exist in prices_daily).

Kept independent of any DB/Streamlit imports so they're trivial to test.
Pandas NaT/NaN values flowing in from DuckDB DataFrames are treated as
missing alongside None.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

import numpy as np

from fx.config import PIP_SIZE


# ──────────────────────────────────────────────────────────────────────
# % move since prediction
# ──────────────────────────────────────────────────────────────────────

def pct_move_equity_or_crypto(
    actual_max_return: Optional[float],
    price_at_prediction: Optional[float],
    current_price: Optional[float],
    outcome_filled: bool,
) -> Optional[float]:
    """Equity / crypto: returns the % move since prediction, signed.

    Filled rows: realized max return × 100 (matches what fill_outcomes wrote).
    Pending rows: (current / price_at_prediction − 1) × 100.

    Returns None when no calculation is possible (e.g. no current price yet).
    """
    if outcome_filled and not _is_missing(actual_max_return):
        return float(actual_max_return) * 100.0
    if (
        not _is_missing(price_at_prediction)
        and not _is_missing(current_price)
        and float(price_at_prediction) > 0
    ):
        return (float(current_price) / float(price_at_prediction) - 1.0) * 100.0
    return None


def pct_move_fx(
    direction: Optional[str],
    actual_max_pips: Optional[float],
    price_at_prediction: Optional[float],
    current_price: Optional[float],
    outcome_filled: bool,
) -> Optional[float]:
    """FX: returns the % move since prediction, signed.

    For filled rows we project actual_max_pips back to a percentage:
        (max_pips × PIP_SIZE) / price_at_prediction × 100
    Sign comes from `direction` ('up' → +, 'down' → −) since actual_max_pips
    is stored unsigned by fx/ml/predict.py::fill_outcomes.

    For pending rows we use the standard close-to-close ratio.
    """
    if (
        outcome_filled
        and not _is_missing(actual_max_pips)
        and not _is_missing(price_at_prediction)
        and float(price_at_prediction) > 0
    ):
        sign = -1.0 if direction == "down" else 1.0
        return sign * (float(actual_max_pips) * PIP_SIZE) / float(price_at_prediction) * 100.0
    if (
        not _is_missing(price_at_prediction)
        and not _is_missing(current_price)
        and float(price_at_prediction) > 0
    ):
        return (float(current_price) / float(price_at_prediction) - 1.0) * 100.0
    return None


def format_pct_move(value: Optional[float]) -> str:
    if _is_missing(value):
        return ""
    return f"{float(value):+.2f}%"


# ──────────────────────────────────────────────────────────────────────
# Time remaining until maturity
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TimeRemaining:
    """Numeric time remaining; the `unit` is 'd' (days) or 'h' (hours)."""
    value: int
    unit: str   # 'd' or 'h'
    past_due: bool


def time_remaining_days(
    maturity: Optional[date],
    today: Optional[date] = None,
    outcome_filled: bool = False,
) -> Optional[TimeRemaining]:
    """Equity / crypto. Returns None for filled rows or missing maturity."""
    if outcome_filled or _is_missing(maturity):
        return None
    maturity = _to_date(maturity)
    today = today or date.today()
    delta = (maturity - today).days
    return TimeRemaining(value=delta, unit="d", past_due=delta < 0)


def time_remaining_hours(
    maturity_dt: Optional[datetime],
    now_utc: Optional[datetime] = None,
    outcome_filled: bool = False,
) -> Optional[TimeRemaining]:
    """FX. Returns None for filled rows or missing maturity."""
    if outcome_filled or _is_missing(maturity_dt):
        return None
    maturity_dt = _to_datetime(maturity_dt)
    now = now_utc or datetime.now(timezone.utc).replace(tzinfo=None)
    # Truncate towards zero, but keep negative integer for past-due.
    seconds = (maturity_dt - now).total_seconds()
    hours = int(seconds // 3600) if seconds >= 0 else -int((-seconds) // 3600 + (1 if (-seconds) % 3600 else 0))
    return TimeRemaining(value=hours, unit="h", past_due=hours < 0)


# ──────────────────────────────────────────────────────────────────────
# Estimated maturity date for pending equity predictions
# ──────────────────────────────────────────────────────────────────────


# NYSE market closures. Used by `numpy.busday_offset` so the estimate
# matches what `ml/predict.py:fill_outcomes` will eventually compute
# from the trading-rows-forward JOIN once those future rows exist.
# Extend this list when crossing into a new calendar year.
_NYSE_HOLIDAYS = np.array(
    [
        # 2024
        "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29",
        "2024-05-27", "2024-06-19", "2024-07-04", "2024-09-02",
        "2024-11-28", "2024-12-25",
        # 2025
        "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18",
        "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01",
        "2025-11-27", "2025-12-25",
        # 2026
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
        "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
        "2026-11-26", "2026-12-25",
        # 2027
        "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
        "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06",
        "2027-11-25", "2027-12-24",
    ],
    dtype="datetime64[D]",
)


# Equity horizons map to N trading days forward; mirrors
# ``ml/predict.py::fill_outcomes`` and the dashboard query's
# trading-rows-forward JOIN.
_EQUITY_HORIZON_TRADING_DAYS = {"5d": 5, "10d": 10, "20d": 20}


def estimate_equity_maturity_date(
    prediction_date: Optional[date], horizon: Optional[str]
) -> Optional[date]:
    """Estimate the trading-day maturity for a pending equity prediction.

    Returns ``prediction_date + N business days`` where N is the
    horizon's trading-day count (5/10/20), skipping NYSE holidays.
    Used as a fallback when the trading-rows-forward JOIN in
    `dashboard/services/queries.py:get_equity_predictions` returns
    NULL because the future rows don't exist yet in `prices_daily`.

    For matured predictions the JOIN's exact `mat.trade_date` is
    authoritative; this estimate is only consulted when that is NULL.
    """
    if _is_missing(prediction_date) or _is_missing(horizon):
        return None
    n = _EQUITY_HORIZON_TRADING_DAYS.get(horizon)
    if n is None:
        return None
    pd_date = _to_date(prediction_date)
    start = np.datetime64(pd_date, "D")
    out = np.busday_offset(
        start, n, roll="forward", holidays=_NYSE_HOLIDAYS
    )
    # numpy returns a datetime64[D]; convert back to a plain date.
    return out.astype("O")


def format_time_remaining(tr: Optional[TimeRemaining]) -> str:
    """Formatter consumed by Streamlit display.

    - filled / no maturity → "" (empty cell)
    - past due (maturity in the past, outcome still NULL) → "Past due"
    - future → "<n>d" or "<n>h"
    """
    if tr is None:
        return ""
    if tr.past_due:
        return "Past due"
    return f"{tr.value}{tr.unit}"


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────


def _is_missing(value) -> bool:
    """True for None, NaN, and pandas NaT.

    NaN and NaT both have the property `value != value`, so we can detect
    them without importing pandas. None is checked explicitly since None
    is equal to itself.
    """
    if value is None:
        return True
    try:
        return value != value
    except Exception:
        return False


def _to_date(value) -> date:
    """Coerce a pandas.Timestamp / datetime / date to a plain date."""
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "to_pydatetime"):  # pandas.Timestamp
        return value.to_pydatetime().date()
    return value


def _to_datetime(value) -> datetime:
    """Coerce a pandas.Timestamp / datetime to a tz-naive datetime."""
    if hasattr(value, "to_pydatetime"):  # pandas.Timestamp
        dt = value.to_pydatetime()
    else:
        dt = value
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


# ──────────────────────────────────────────────────────────────────────
# Equity T-2 honest banner (KI-149 follow-up, resumption queue Step 5)
# ──────────────────────────────────────────────────────────────────────

def format_equity_t2_banner(prediction_date: date, today: date) -> str:
    """Honest "predictions as of …" copy for the equity dashboard.

    The equity engine is on a T-2 cadence (Polygon free-tier delays
    current-day grouped-daily by ≥2 trading days; see
    ``docs/EQUITY_WORKSTREAM_PAUSED.md`` for the architectural decision).
    This helper labels the scoring date so the operator can tell at a
    glance:

      * whether the dashboard is showing the expected T-2 state, or
      * accidentally stale (>T-2 — usually means feature-pipeline or
        ingestion is behind), or
      * the unusual T-0 / T-1 case (paid-tier or backfill).

    Returns Streamlit-markdown text. The prediction date is bolded; the
    trading-day gap and the cadence label (T-0 / T-1 / T-2 / stale) are
    explicit so the message reads correctly even if the operator only
    glances at it.
    """
    from datetime import timedelta

    from pipelines.market_calendar import trading_days_between

    # Coerce common date-ish inputs (pandas.Timestamp, datetime) to date.
    p = _to_date(prediction_date)
    t = _to_date(today)

    # Elapsed trading days strictly after the prediction date. Mirrors
    # pipelines/freshness.py:78 (uses p + 1 day as inclusive start). For
    # p == t this returns 0; for p one trading day back it returns 1.
    gap = trading_days_between(p + timedelta(days=1), t) if p <= t else 0

    date_str = f"**{p.isoformat()}**"
    today_str = t.isoformat()

    if gap == 0:
        return (
            f"Predictions as of {date_str} — current (T-0 vs today "
            f"{today_str})."
        )
    if gap == 1:
        return (
            f"Predictions as of {date_str} — 1 trading day behind "
            f"today ({today_str})."
        )
    if gap == 2:
        return (
            f"Predictions as of {date_str} — 2 trading days behind "
            f"today ({today_str}). This is the expected **T-2 cadence** "
            "(Polygon free-tier delays current-day grouped-daily; see "
            "`docs/EQUITY_WORKSTREAM_PAUSED.md`)."
        )
    return (
        f"Predictions as of {date_str} — {gap} trading days behind "
        f"today ({today_str}). This is **stale** — check the equity "
        "ingestion / feature pipeline."
    )


# ──────────────────────────────────────────────────────────────────────
# Crypto: trading-date relabel
#
# The crypto pipeline writes prediction_date = MAX(crypto_ml_features.
# trade_date) = T-1 calendar day (the last completed daily bar). The
# trading engine consumes those predictions for entries on the
# FOLLOWING calendar day. From the operator's POV the meaningful
# "trading date" is prediction_date + 1, not prediction_date itself.
#
# These helpers translate at the presentation layer only — backend
# schema and column semantics are unchanged.
# ──────────────────────────────────────────────────────────────────────


def crypto_feature_date_to_trading_date(prediction_date: date) -> date:
    """Forward: feature-snapshot date → operator-facing trading date.

    Crypto is 24/7; the mapping is a plain +1 calendar day. No
    trading-day-skip logic (that's an equity-only concern).
    """
    from datetime import timedelta
    return _to_date(prediction_date) + timedelta(days=1)


def crypto_trading_date_to_feature_date(trading_date: date) -> date:
    """Inverse: operator's trading-date pick → backend prediction_date.

    Used by the dashboard selectbox callback to translate user input
    back to the column value DuckDB indexes.
    """
    from datetime import timedelta
    return _to_date(trading_date) - timedelta(days=1)


def format_crypto_exclusion_badge(
    reason,
    dd90,
    ret60,
    ret5,
) -> str:
    """Inline badge text for the per-row Status column on the crypto
    predictions table.

    Returns an empty string when ``reason`` is missing (no exclusion —
    the renderer falls back to pending / outcome display). Otherwise
    formats the rule name + the trigger value(s) that fired it:

        short_momentum                  → "EXCLUDED: short_momentum (ret5=-43.7%)"
        post_parabolic                  → "EXCLUDED: post_parabolic (dd90=-25.0%, ret60=+250.0%)"
        post_parabolic_and_short_momentum → "EXCLUDED: post_parabolic_and_short_momentum "
                                            "(dd90=-25.0%, ret60=+250.0%, ret5=-35.0%)"

    Filter rule definitions live in ``crypto/ml/postparabolic_filter.py``
    (ADR-021 + ADR-028).
    """
    if _is_missing(reason):
        return ""

    parts = []
    if _has(dd90):
        parts.append(f"dd90={float(dd90)*100:.1f}%")
    if _has(ret60):
        parts.append(f"ret60={float(ret60)*100:+.1f}%")
    if _has(ret5):
        parts.append(f"ret5={float(ret5)*100:+.1f}%")
    metrics = f" ({', '.join(parts)})" if parts else ""
    return f"EXCLUDED: {reason}{metrics}"


def _has(v) -> bool:
    return not _is_missing(v)


def format_crypto_predictions_summary(
    n_total: int,
    n_excluded: int,
    exclusion_reasons,
) -> str:
    """Summary line shown as the Crypto Predictions table's subheader.

    When no exclusions fired today: plain count form
    ``"Predictions (N coins)"`` — historical behaviour preserved so the
    operator's eye isn't drawn to a "0 filtered" non-event.

    When at least one exclusion fired: surface total / active /
    filtered counts plus the distinct rule names that fired so the
    operator can read the cohort at a glance without scrolling to the
    filtered rows. Reasons are listed in sorted order for stable
    rendering.
    """
    if n_excluded <= 0:
        return f"Predictions ({n_total} coins)"

    n_active = n_total - n_excluded
    reasons_sorted = sorted(set(exclusion_reasons or set()))
    reasons_str = ", ".join(reasons_sorted) if reasons_sorted else "filter"
    return (
        f"Predictions ({n_total} total — {n_active} active after "
        f"exclusions, {n_excluded} filtered: {reasons_str})"
    )


def format_crypto_trading_date_banner(
    prediction_date: date,
    predicted_at,
    today: date,
) -> str:
    """Three-reference banner shown under the crypto date selector.

    Surfaces (a) the trading date the operator picked, (b) the
    feature-snapshot date the model actually used, and (c) the
    timestamp the prediction row was written. (c) is `predicted_at`
    from `crypto_ml_predictions` (migration v11, KI-154). Pre-migration
    rows have NULL predicted_at and render as "N/A".

    All timestamps are UTC — labeled explicitly so the operator
    doesn't misread as local time.
    """
    p = _to_date(prediction_date)
    t = _to_date(today)
    trading_date = crypto_feature_date_to_trading_date(p)

    if _is_missing(predicted_at):
        generated_str = "N/A"
    else:
        ts = predicted_at
        # pandas.Timestamp / datetime both expose .strftime; normalize
        # to a stripped HH:MM:SS UTC label.
        if hasattr(ts, "tz_convert"):  # pandas.Timestamp
            try:
                ts = ts.tz_convert("UTC")
            except (TypeError, AttributeError):
                pass
        elif hasattr(ts, "astimezone"):
            try:
                from datetime import timezone as _tz
                ts = ts.astimezone(_tz.utc) if ts.tzinfo else ts
            except (TypeError, ValueError):
                pass
        generated_str = ts.strftime("%Y-%m-%d %H:%M:%S UTC")

    today_marker = (
        " (today)" if trading_date == t
        else f" (today is {t.isoformat()})"
    )
    return (
        f"**Trading date: {trading_date.isoformat()}**{today_marker} "
        f"— features as of **{p.isoformat()}** "
        f"— generated at **{generated_str}**. "
        "Predictions are produced at 00:30 UTC against the previous "
        "day's complete daily bar (T-1 cadence per ADR-029) and "
        "consumed by the trading engine at 00:45 UTC on the trading "
        "date shown."
    )
