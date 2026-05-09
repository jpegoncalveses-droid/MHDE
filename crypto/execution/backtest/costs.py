"""Cost model for the crypto execution backtest.

Per ``crypto/execution/backtest/SPEC.md`` — Cost Model:

  Fees:
    - Entry: maker fee 0.02% (limit order; assume fill within 24h)
    - Exit:  taker fee 0.05% (market order for speed)
    - Round-trip baseline: 0.07% per trade

  Slippage (per side):
    - Tier 1 (BTC, ETH, SOL, top 10 by volume): 0.02%
    - Tier 2 (other top 30):                    0.05%
    - Tier 3 (rest of universe):                0.10%

  Funding:
    - Binance funding cadence varies per coin (8h is the historical
      standard; many newer pairs run on a 4h schedule and a few on a
      1h schedule). The cost model is cadence-agnostic — it sums the
      actual rows present in ``crypto_funding_rates`` over the hold
      window without assuming any particular tick interval.
    - Long position pays positive rates, receives negative rates.

All cost values are returned as **fractions of trade notional** (e.g.
``0.0007`` for ``0.07%``), so the harness can sum them directly into
the trade's net P&L percentage. The :class:`TradeCosts` dataclass keeps
each component separate per the spec's "diagnostic visibility" requirement.

This module is intentionally I/O free — it accepts pre-fetched pandas
DataFrames and pure scalars. The harness layer handles DB reads and
volume-rank computation; that keeps the cost logic deterministic and
unit-testable in isolation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Fee constants
# ──────────────────────────────────────────────────────────────────────

MAKER_FEE: float = 0.0002
TAKER_FEE: float = 0.0005
ROUND_TRIP_FEE_BASELINE: float = MAKER_FEE + TAKER_FEE  # 0.0007 = 0.07%

# ──────────────────────────────────────────────────────────────────────
# Slippage tiers (per side)
# ──────────────────────────────────────────────────────────────────────

SLIPPAGE_TIER_1: float = 0.0002   # ≤ rank 10
SLIPPAGE_TIER_2: float = 0.0005   # ≤ rank 30
SLIPPAGE_TIER_3: float = 0.0010   # rank > 30 or unknown

# ──────────────────────────────────────────────────────────────────────
# Missing-funding-row counter
# ──────────────────────────────────────────────────────────────────────
# Incremented by :func:`funding_payments_during_hold` whenever a hold of
# at least one day yields zero funding rows — the most likely cause is
# a data gap in ``crypto_funding_rates``. The harness reads this counter
# at the end of each run and surfaces it in the run output so silent
# data gaps don't get baked into the P&L numbers unnoticed.

_missing_funding_warnings: int = 0


def reset_missing_funding_warnings() -> None:
    """Zero the missing-funding counter. Call once per backtest run."""
    global _missing_funding_warnings
    _missing_funding_warnings = 0


def get_missing_funding_warnings() -> int:
    """Return the count of missing-funding warnings since last reset."""
    return _missing_funding_warnings


def classify_slippage_tier(volume_rank: Optional[int]) -> int:
    """Map a coin's volume rank to its slippage tier.

    ``volume_rank`` is 1-indexed (rank 1 = most-traded). ``None`` means
    rank could not be determined and falls into tier 3.
    """
    if volume_rank is None or volume_rank > 30:
        return 3
    if volume_rank <= 10:
        return 1
    return 2


def slippage_per_side(tier: int) -> float:
    """Slippage fraction for one side of the trade at the given tier."""
    if tier == 1:
        return SLIPPAGE_TIER_1
    if tier == 2:
        return SLIPPAGE_TIER_2
    if tier == 3:
        return SLIPPAGE_TIER_3
    raise ValueError(f"Unknown slippage tier: {tier!r} (expected 1, 2, or 3)")


@dataclass(frozen=True)
class TradeCosts:
    """All cost components for one round-trip trade, as fractions of notional.

    The harness multiplies any of these by 100 to obtain the percentage
    columns required by ``crypto_backtest_trades`` (see SPEC.md output schema).
    """

    entry_fee: float
    exit_fee: float
    entry_slippage: float
    exit_slippage: float
    funding: float          # net (positive = paid; negative = received)

    @property
    def fee_total(self) -> float:
        return self.entry_fee + self.exit_fee

    @property
    def slippage_total(self) -> float:
        return self.entry_slippage + self.exit_slippage

    @property
    def total(self) -> float:
        """Sum of all cost components — the value to subtract from gross P&L."""
        return self.fee_total + self.slippage_total + self.funding


def funding_payments_during_hold(
    entry_dt: datetime,
    exit_dt: datetime,
    funding_rates: pd.DataFrame,
) -> float:
    """Net funding cost (as fraction of notional) over ``[entry_dt, exit_dt)``.

    The function is **cadence-agnostic** — Binance funding ticks may
    arrive every 8h, 4h, or 1h depending on the perp contract, and
    individual coins occasionally change cadence. We don't assume any
    specific schedule; we just sum the rows that fall inside the hold
    window. The harness owns the upstream filtering of ``funding_rates``
    to a single ``symbol``.

    Args:
        entry_dt:  position open timestamp (tz-naive UTC).
        exit_dt:   position close timestamp (tz-naive UTC).
        funding_rates: DataFrame with columns ``["funding_time", "funding_rate"]``,
            pre-filtered to one coin. May be empty.

    Returns:
        Sum of ``funding_rate`` values whose ``funding_time`` falls in
        ``[entry_dt, exit_dt)`` (left-inclusive, right-exclusive). Positive
        = the long paid; negative = the long received funding.

    Side effect:
        If the hold window is at least one day long but no funding rows
        were found inside it, increments
        :func:`get_missing_funding_warnings` and emits a ``logging``
        warning. This catches silent data gaps without raising — the
        backtest still produces a result, but the harness can surface
        the count alongside the run summary.
    """
    if exit_dt <= entry_dt:
        return 0.0

    if funding_rates.empty:
        n_rows_in_window = 0
        total = 0.0
    else:
        mask = (
            (funding_rates["funding_time"] >= entry_dt)
            & (funding_rates["funding_time"] < exit_dt)
        )
        n_rows_in_window = int(mask.sum())
        total = float(funding_rates.loc[mask, "funding_rate"].sum())

    if n_rows_in_window == 0 and (exit_dt - entry_dt) >= timedelta(days=1):
        global _missing_funding_warnings
        _missing_funding_warnings += 1
        logger.warning(
            "No funding rows in [%s, %s) (hold=%s); assuming 0 funding cost. "
            "Possible data gap in crypto_funding_rates.",
            entry_dt, exit_dt, exit_dt - entry_dt,
        )

    return total


def compute_trade_costs(
    volume_rank: Optional[int],
    entry_dt: datetime,
    exit_dt: datetime,
    funding_rates: Optional[pd.DataFrame] = None,
) -> TradeCosts:
    """Bundle all cost components for one round-trip.

    Args:
        volume_rank: coin's average-daily-volume rank during the trade
            window; used to classify slippage tier.
        entry_dt / exit_dt: tz-naive UTC timestamps bounding the hold.
        funding_rates: pre-filtered DataFrame for the coin, or ``None``
            to skip funding (e.g. when the table has no rows for this coin).
    """
    tier = classify_slippage_tier(volume_rank)
    slip = slippage_per_side(tier)
    if funding_rates is not None and not funding_rates.empty:
        funding = funding_payments_during_hold(entry_dt, exit_dt, funding_rates)
    else:
        funding = 0.0
    return TradeCosts(
        entry_fee=MAKER_FEE,
        exit_fee=TAKER_FEE,
        entry_slippage=slip,
        exit_slippage=slip,
        funding=funding,
    )
