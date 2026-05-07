"""Candidate lifecycle classification: episode detection, outcome, phase, actionability."""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Optional

import duckdb

logger = logging.getLogger("mhde.outcomes.candidate_lifecycle")

VALIDATION_THRESHOLD = 0.10   # +10% from event_price = validated
FAILURE_THRESHOLD = -0.05     # -5% from event_price = failed
DEFAULT_WINDOW_DAYS = 126     # ~6 months trading days
PRE_EVENT_LOOKBACK = 20       # trading days to scan for episode start
EPISODE_MOVE_THRESHOLD = 0.05 # 5% gain in any 5-day pre-event window


@dataclass
class CandidateLifecycle:
    ticker: str
    event_date: Optional[datetime.date]
    episode_start_date: Optional[datetime.date]
    signal_date: Optional[datetime.date]
    latest_price_date: Optional[datetime.date]

    event_price: Optional[float]
    episode_start_price: Optional[float]
    signal_price: Optional[float]
    latest_price: Optional[float]

    return_since_event: Optional[float]          # decimal, e.g. 0.294 = +29.4%
    return_since_episode_start: Optional[float]
    max_runup_since_event: Optional[float]
    max_drawdown_since_event: Optional[float]
    return_from_peak: Optional[float]            # latest vs max price since event

    expected_window_days: int
    validation_threshold: float

    outcome_status: str    # validated | failed | expired | validated_then_faded | pending | inconclusive | insufficient_data
    current_phase: str     # initial_event_reaction | continuation | post_event_fade | recovery_attempt | extended_move | no_price_confirmation | insufficient_data
    current_actionability: str  # high_priority | watch | context | investigate | expired | avoid | insufficient_data
    explanation: str


def _ret(cur: float, base: float) -> float:
    return (cur - base) / base


def detect_episode_start(
    prices_before_event: list[tuple],
    event_date: datetime.date,
) -> datetime.date:
    """Detect pre-event accumulation start from a list of (date, close, volume) tuples.

    Scans for a ≥5% gain in any 5-day window ending before event_date.
    Returns the start of the earliest such window, or event_date if none found.
    """
    pre = [(d, c, v) for d, c, v in prices_before_event if d < event_date]
    pre.sort(key=lambda x: x[0])
    if len(pre) < 5:
        return event_date

    episode_start = event_date
    for i in range(len(pre) - 4):
        window = pre[i:i + 5]
        base = window[0][1]
        top = window[-1][1]
        if base and base > 0 and (top - base) / base >= EPISODE_MOVE_THRESHOLD:
            episode_start = window[0][0]
            break  # take earliest qualifying window

    return episode_start


def _fetch_prices(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    from_date: datetime.date,
    to_date: datetime.date,
) -> list[tuple[datetime.date, float, Optional[int]]]:
    """Return [(trade_date, close, volume)] ordered by date ascending."""
    rows = conn.execute(
        """
        SELECT trade_date, COALESCE(adjusted_close, close) AS price, volume
        FROM prices_daily
        WHERE ticker = ? AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date ASC
        """,
        [ticker, from_date, to_date],
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _insufficient() -> CandidateLifecycle:
    return CandidateLifecycle(
        ticker="", event_date=None, episode_start_date=None,
        signal_date=None, latest_price_date=None,
        event_price=None, episode_start_price=None,
        signal_price=None, latest_price=None,
        return_since_event=None, return_since_episode_start=None,
        max_runup_since_event=None, max_drawdown_since_event=None,
        return_from_peak=None,
        expected_window_days=DEFAULT_WINDOW_DAYS,
        validation_threshold=VALIDATION_THRESHOLD,
        outcome_status="insufficient_data",
        current_phase="insufficient_data",
        current_actionability="insufficient_data",
        explanation="No event date or price data available.",
    )


def compute_lifecycle(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    event_date: str = "",
    signal_date: str = "",
    as_of: str = "",
    catalyst_type: str = "",
    expected_window_days: int = DEFAULT_WINDOW_DAYS,
) -> CandidateLifecycle:
    """Compute full CandidateLifecycle for a ticker given its event and signal dates."""
    lc = _insufficient()
    lc.ticker = ticker

    if not event_date:
        return lc

    try:
        evt = datetime.date.fromisoformat(event_date)
    except (ValueError, TypeError):
        return lc

    as_of_date = datetime.date.fromisoformat(as_of) if as_of else datetime.date.today()
    sig = datetime.date.fromisoformat(signal_date) if signal_date else None

    # Fetch prices: 25 trading days before event through as_of
    lookback_start = evt - datetime.timedelta(days=PRE_EVENT_LOOKBACK + 7)
    all_prices = _fetch_prices(conn, ticker, lookback_start, as_of_date)

    if not all_prices:
        return lc

    # Split into pre-event and post-event
    pre_event = [(d, c, v) for d, c, v in all_prices if d < evt]
    post_event = [(d, c, v) for d, c, v in all_prices if d >= evt]

    if not post_event:
        return lc

    # Event anchor: first trading day on/after event_date
    event_row = post_event[0]
    event_price = event_row[1]

    # Latest price
    latest_row = post_event[-1]
    latest_price = latest_row[1]
    latest_price_date = latest_row[0]

    # Signal price
    signal_price = None
    if sig:
        sig_matches = [(d, c, v) for d, c, v in all_prices if d >= sig]
        if sig_matches:
            signal_price = sig_matches[0][1]

    # Episode start detection
    episode_start = detect_episode_start(pre_event, evt)
    episode_start_price: Optional[float] = None
    if episode_start < evt:
        ep_matches = [(d, c, v) for d, c, v in pre_event if d == episode_start]
        if ep_matches:
            episode_start_price = ep_matches[0][1]
    if episode_start == evt:
        episode_start_price = event_price

    # Compute returns from event_price
    post_closes = [c for _, c, _ in post_event if c is not None]
    return_since_event = _ret(latest_price, event_price) if event_price else None
    max_runup = max((_ret(c, event_price) for c in post_closes), default=None)
    max_drawdown = min((_ret(c, event_price) for c in post_closes), default=None)
    peak_price = max(post_closes) if post_closes else None
    return_from_peak = _ret(latest_price, peak_price) if peak_price else None

    return_since_episode_start = None
    if episode_start_price:
        return_since_episode_start = _ret(latest_price, episode_start_price)

    # Days since event
    days_elapsed = (latest_price_date - evt).days

    # ── Outcome classification ──────────────────────────────────────────────
    if return_since_event is None:
        outcome = "insufficient_data"
    elif (max_runup is not None and max_runup >= VALIDATION_THRESHOLD
          and return_since_event is not None
          and return_since_event < max_runup / 2):
        outcome = "validated_then_faded"
    elif return_since_event >= VALIDATION_THRESHOLD:
        outcome = "validated"
    elif return_since_event <= FAILURE_THRESHOLD:
        if days_elapsed > expected_window_days:
            outcome = "failed"
        else:
            outcome = "failed"
    elif days_elapsed > expected_window_days:
        outcome = "expired"
    elif abs(return_since_event) < 0.03:
        outcome = "inconclusive"
    else:
        outcome = "pending"

    # ── Phase detection ─────────────────────────────────────────────────────
    if return_since_event is None:
        phase = "insufficient_data"
    elif latest_price < event_price:
        if max_runup is not None and max_runup > 0.02:
            phase = "recovery_attempt" if return_since_event > max_drawdown else "post_event_fade"
        else:
            phase = "post_event_fade"
    elif days_elapsed <= 5:
        phase = "initial_event_reaction"
    elif days_elapsed > expected_window_days:
        phase = "extended_move" if return_since_event > 0 else "post_event_fade"
    elif days_elapsed > 20:
        phase = "continuation" if return_since_event > 0 else "post_event_fade"
    elif abs(return_since_event) < 0.03:
        phase = "no_price_confirmation"
    else:
        phase = "continuation"

    # ── Actionability ───────────────────────────────────────────────────────
    if outcome == "insufficient_data":
        actionability = "insufficient_data"
    elif outcome == "validated":
        actionability = "context"
    elif outcome == "validated_then_faded":
        actionability = "investigate"
    elif outcome == "failed":
        actionability = "avoid"
    elif outcome == "expired":
        actionability = "expired"
    elif phase == "initial_event_reaction":
        actionability = "high_priority"
    elif phase == "continuation":
        actionability = "watch"
    elif phase == "post_event_fade":
        actionability = "investigate"
    elif phase == "no_price_confirmation":
        actionability = "watch"
    else:
        actionability = "watch"

    # ── Explanation ─────────────────────────────────────────────────────────
    ret_pct = f"{return_since_event * 100:.1f}%" if return_since_event is not None else "N/A"
    explanation = (
        f"{outcome}: {ret_pct} from event ${event_price:.2f} "
        f"({days_elapsed}d elapsed). Phase: {phase}."
    )

    return CandidateLifecycle(
        ticker=ticker,
        event_date=evt,
        episode_start_date=episode_start,
        signal_date=sig,
        latest_price_date=latest_price_date,
        event_price=event_price,
        episode_start_price=episode_start_price,
        signal_price=signal_price,
        latest_price=latest_price,
        return_since_event=return_since_event,
        return_since_episode_start=return_since_episode_start,
        max_runup_since_event=max_runup,
        max_drawdown_since_event=max_drawdown,
        return_from_peak=return_from_peak,
        expected_window_days=expected_window_days,
        validation_threshold=VALIDATION_THRESHOLD,
        outcome_status=outcome,
        current_phase=phase,
        current_actionability=actionability,
        explanation=explanation,
    )
