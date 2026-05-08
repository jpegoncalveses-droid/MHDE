"""Pure helpers for the dashboard prediction tabs:

- Compute % move since prediction (filled vs pending)
- Compute time remaining until maturity (days for equity/crypto, hours for FX)
- Format both as user-facing strings ("+1.8%", "3d", "Past due")

Kept independent of any DB/Streamlit imports so they're trivial to test.
Pandas NaT/NaN values flowing in from DuckDB DataFrames are treated as
missing alongside None.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

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
