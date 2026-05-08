"""Pure helpers for the dashboard prediction tabs:

- Compute % move since prediction (filled vs pending)
- Compute time remaining until maturity (days for equity/crypto, hours for FX)
- Format both as user-facing strings ("+1.8%", "3d", "Past due")

Kept independent of any DB/Streamlit imports so they're trivial to test.
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
    if outcome_filled and actual_max_return is not None and not _isnan(actual_max_return):
        return actual_max_return * 100.0
    if (
        price_at_prediction is not None
        and current_price is not None
        and not _isnan(price_at_prediction)
        and not _isnan(current_price)
        and price_at_prediction > 0
    ):
        return (current_price / price_at_prediction - 1.0) * 100.0
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
        and actual_max_pips is not None and not _isnan(actual_max_pips)
        and price_at_prediction is not None and not _isnan(price_at_prediction)
        and price_at_prediction > 0
    ):
        sign = -1.0 if direction == "down" else 1.0
        return sign * (actual_max_pips * PIP_SIZE) / price_at_prediction * 100.0
    if (
        price_at_prediction is not None
        and current_price is not None
        and not _isnan(price_at_prediction)
        and not _isnan(current_price)
        and price_at_prediction > 0
    ):
        return (current_price / price_at_prediction - 1.0) * 100.0
    return None


def format_pct_move(value: Optional[float]) -> str:
    if value is None or _isnan(value):
        return ""
    return f"{value:+.2f}%"


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
    if outcome_filled or maturity is None:
        return None
    today = today or date.today()
    delta = (maturity - today).days
    return TimeRemaining(value=delta, unit="d", past_due=delta < 0)


def time_remaining_hours(
    maturity_dt: Optional[datetime],
    now_utc: Optional[datetime] = None,
    outcome_filled: bool = False,
) -> Optional[TimeRemaining]:
    """FX. Returns None for filled rows or missing maturity."""
    if outcome_filled or maturity_dt is None:
        return None
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


def _isnan(value: float) -> bool:
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return False
