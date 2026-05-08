"""Tests for crypto/execution/backtest/costs.py.

Covers funding_payments_during_hold thoroughly (per request before
moving on to policies.py): empty window, straddled timestamps, signed
long-vs-short convention, half-open boundary semantics, and multi-row
sums. Plus light coverage of slippage tier classification and the
TradeCosts.total bundling.

This test module imports nothing from equity / FX modules and uses no
DuckDB — costs.py is intentionally I/O free.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from crypto.execution.backtest.costs import (
    MAKER_FEE,
    SLIPPAGE_TIER_1,
    SLIPPAGE_TIER_2,
    SLIPPAGE_TIER_3,
    TAKER_FEE,
    TradeCosts,
    classify_slippage_tier,
    compute_trade_costs,
    funding_payments_during_hold,
    get_missing_funding_warnings,
    reset_missing_funding_warnings,
    slippage_per_side,
)


@pytest.fixture(autouse=True)
def _reset_funding_warning_counter():
    """Each test starts with a zeroed counter so they're independent."""
    reset_missing_funding_warnings()
    yield


def _rates(rows: list[tuple[datetime, float]]) -> pd.DataFrame:
    """Build a `crypto_funding_rates`-shaped DataFrame for a single coin."""
    return pd.DataFrame(rows, columns=["funding_time", "funding_rate"])


# ──────────────────────────────────────────────────────────────────────
# (a) no funding rows in window
# ──────────────────────────────────────────────────────────────────────


def test_funding_window_with_no_rows_returns_zero():
    rates = _rates([
        (datetime(2026, 5, 1, 0), 0.0001),
        (datetime(2026, 5, 5, 0), 0.0002),
    ])
    # Hold window sits between the two funding ticks; no rows fall inside.
    result = funding_payments_during_hold(
        datetime(2026, 5, 2, 0),
        datetime(2026, 5, 4, 0),
        rates,
    )
    assert result == 0.0


def test_funding_empty_dataframe_returns_zero():
    rates = pd.DataFrame(columns=["funding_time", "funding_rate"])
    result = funding_payments_during_hold(
        datetime(2026, 5, 1, 0),
        datetime(2026, 5, 2, 0),
        rates,
    )
    assert result == 0.0


def test_funding_zero_length_window_returns_zero():
    """exit_dt <= entry_dt → no payments. (e.g. flat trade closed instantly.)"""
    rates = _rates([(datetime(2026, 5, 1, 0), 0.0005)])
    assert funding_payments_during_hold(
        datetime(2026, 5, 1, 0), datetime(2026, 5, 1, 0), rates
    ) == 0.0
    assert funding_payments_during_hold(
        datetime(2026, 5, 1, 0), datetime(2026, 4, 30, 0), rates
    ) == 0.0


# ──────────────────────────────────────────────────────────────────────
# (b) entry/exit straddling a funding timestamp
# ──────────────────────────────────────────────────────────────────────


def test_funding_straddles_single_timestamp():
    """Hold from 07:00 to 09:00 includes the 08:00 funding tick — and only that."""
    rates = _rates([
        (datetime(2026, 5, 1, 0), 0.0001),
        (datetime(2026, 5, 1, 8), 0.0002),
        (datetime(2026, 5, 1, 16), 0.0003),
    ])
    result = funding_payments_during_hold(
        datetime(2026, 5, 1, 7),
        datetime(2026, 5, 1, 9),
        rates,
    )
    assert result == pytest.approx(0.0002)


def test_funding_straddles_multiple_timestamps():
    """A 24h hold over the standard cadence includes 3 funding ticks."""
    rates = _rates([
        (datetime(2026, 5, 1, 0), 0.0001),
        (datetime(2026, 5, 1, 8), 0.0002),
        (datetime(2026, 5, 1, 16), 0.0003),
        (datetime(2026, 5, 2, 0), 0.0004),
    ])
    result = funding_payments_during_hold(
        datetime(2026, 5, 1, 0),
        datetime(2026, 5, 2, 0),
        rates,
    )
    # 0.0001 + 0.0002 + 0.0003 = 0.0006 (4th tick is on the right boundary
    # and excluded by the half-open window).
    assert result == pytest.approx(0.0006)


def test_funding_window_is_left_inclusive_right_exclusive():
    """Boundary semantics: entry_dt is included, exit_dt is excluded."""
    rates = _rates([(datetime(2026, 5, 1, 8), 0.0002)])
    # Tick at the entry boundary → included.
    assert funding_payments_during_hold(
        datetime(2026, 5, 1, 8),
        datetime(2026, 5, 1, 9),
        rates,
    ) == pytest.approx(0.0002)
    # Tick at the exit boundary → excluded.
    assert funding_payments_during_hold(
        datetime(2026, 5, 1, 7),
        datetime(2026, 5, 1, 8),
        rates,
    ) == 0.0


# ──────────────────────────────────────────────────────────────────────
# (c) signed convention — long pays positive, receives negative
# ──────────────────────────────────────────────────────────────────────


def test_funding_long_pays_positive_rate():
    """Per SPEC.md: positive funding rate means the long pays the short.
    The function returns the cost as a positive fraction of notional.
    """
    rates = _rates([(datetime(2026, 5, 1, 0), 0.0005)])
    result = funding_payments_during_hold(
        datetime(2026, 5, 1, 0),
        datetime(2026, 5, 1, 1),
        rates,
    )
    assert result == pytest.approx(0.0005)
    assert result > 0  # cost (paid)


def test_funding_long_receives_negative_rate():
    """Negative funding rate → long receives. Returned value is negative."""
    rates = _rates([(datetime(2026, 5, 1, 0), -0.0003)])
    result = funding_payments_during_hold(
        datetime(2026, 5, 1, 0),
        datetime(2026, 5, 1, 1),
        rates,
    )
    assert result == pytest.approx(-0.0003)
    assert result < 0  # received (credit)


def test_funding_mixed_signs_net():
    """Net of a paying tick and a receiving tick equals their algebraic sum."""
    rates = _rates([
        (datetime(2026, 5, 1, 0), 0.0010),
        (datetime(2026, 5, 1, 8), -0.0004),
        (datetime(2026, 5, 1, 16), 0.0001),
    ])
    result = funding_payments_during_hold(
        datetime(2026, 5, 1, 0),
        datetime(2026, 5, 2, 0),
        rates,
    )
    assert result == pytest.approx(0.0007)


# ──────────────────────────────────────────────────────────────────────
# Light coverage of the rest of the public surface
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rank, expected_tier",
    [(1, 1), (10, 1), (11, 2), (30, 2), (31, 3), (1000, 3), (None, 3)],
)
def test_classify_slippage_tier(rank, expected_tier):
    assert classify_slippage_tier(rank) == expected_tier


def test_slippage_per_side_values():
    assert slippage_per_side(1) == SLIPPAGE_TIER_1
    assert slippage_per_side(2) == SLIPPAGE_TIER_2
    assert slippage_per_side(3) == SLIPPAGE_TIER_3


def test_slippage_per_side_rejects_unknown_tier():
    with pytest.raises(ValueError):
        slippage_per_side(4)


def test_trade_costs_total_sums_components():
    tc = TradeCosts(
        entry_fee=MAKER_FEE,
        exit_fee=TAKER_FEE,
        entry_slippage=SLIPPAGE_TIER_2,
        exit_slippage=SLIPPAGE_TIER_2,
        funding=0.0003,
    )
    assert tc.fee_total == pytest.approx(MAKER_FEE + TAKER_FEE)
    assert tc.slippage_total == pytest.approx(2 * SLIPPAGE_TIER_2)
    assert tc.total == pytest.approx(MAKER_FEE + TAKER_FEE + 2 * SLIPPAGE_TIER_2 + 0.0003)


def test_compute_trade_costs_with_no_funding_data():
    tc = compute_trade_costs(
        volume_rank=5,
        entry_dt=datetime(2026, 5, 1, 0),
        exit_dt=datetime(2026, 5, 5, 0),
        funding_rates=None,
    )
    assert tc.entry_slippage == SLIPPAGE_TIER_1
    assert tc.exit_slippage == SLIPPAGE_TIER_1
    assert tc.funding == 0.0
    # Round-trip cost without funding ≈ 0.07% + 0.02% × 2 = 0.11%
    assert tc.total == pytest.approx(MAKER_FEE + TAKER_FEE + 2 * SLIPPAGE_TIER_1)


def test_compute_trade_costs_unknown_rank_falls_to_tier_3():
    tc = compute_trade_costs(
        volume_rank=None,
        entry_dt=datetime(2026, 5, 1, 0),
        exit_dt=datetime(2026, 5, 5, 0),
        funding_rates=None,
    )
    assert tc.entry_slippage == SLIPPAGE_TIER_3
    assert tc.exit_slippage == SLIPPAGE_TIER_3


# ──────────────────────────────────────────────────────────────────────
# Missing-funding-row warning + counter
# ──────────────────────────────────────────────────────────────────────


def test_warning_increments_on_long_hold_with_no_rows(caplog):
    """Hold of >= 1 day with zero rows in window triggers exactly one warning."""
    rates = _rates([(datetime(2026, 5, 10, 0), 0.0001)])  # outside window
    with caplog.at_level("WARNING", logger="crypto.execution.backtest.costs"):
        result = funding_payments_during_hold(
            datetime(2026, 5, 1, 0),
            datetime(2026, 5, 3, 0),  # 2-day hold
            rates,
        )
    assert result == 0.0
    assert get_missing_funding_warnings() == 1
    assert any("No funding rows" in r.message for r in caplog.records)


def test_warning_not_emitted_for_short_hold(caplog):
    """Holds shorter than 1 day are normal (slot between 8h ticks); silent."""
    rates = pd.DataFrame(columns=["funding_time", "funding_rate"])
    with caplog.at_level("WARNING", logger="crypto.execution.backtest.costs"):
        funding_payments_during_hold(
            datetime(2026, 5, 1, 0),
            datetime(2026, 5, 1, 6),  # 6 hours
            rates,
        )
    assert get_missing_funding_warnings() == 0
    assert not any("No funding rows" in r.message for r in caplog.records)


def test_warning_not_emitted_when_rows_are_present(caplog):
    """Even if rows happen to sum to zero, that's data, not a gap."""
    rates = _rates([
        (datetime(2026, 5, 1, 0), 0.0001),
        (datetime(2026, 5, 1, 8), -0.0001),
    ])
    with caplog.at_level("WARNING", logger="crypto.execution.backtest.costs"):
        result = funding_payments_during_hold(
            datetime(2026, 5, 1, 0),
            datetime(2026, 5, 2, 0),
            rates,
        )
    assert result == pytest.approx(0.0)
    assert get_missing_funding_warnings() == 0
    assert not any("No funding rows" in r.message for r in caplog.records)


def test_warning_increments_when_dataframe_is_empty():
    """Empty input + long hold is also a data gap."""
    rates = pd.DataFrame(columns=["funding_time", "funding_rate"])
    funding_payments_during_hold(
        datetime(2026, 5, 1, 0),
        datetime(2026, 5, 5, 0),
        rates,
    )
    assert get_missing_funding_warnings() == 1


def test_warning_counter_accumulates_across_calls():
    rates = pd.DataFrame(columns=["funding_time", "funding_rate"])
    for _ in range(3):
        funding_payments_during_hold(
            datetime(2026, 5, 1, 0),
            datetime(2026, 5, 5, 0),
            rates,
        )
    assert get_missing_funding_warnings() == 3


def test_reset_zeros_the_counter():
    rates = pd.DataFrame(columns=["funding_time", "funding_rate"])
    funding_payments_during_hold(
        datetime(2026, 5, 1, 0),
        datetime(2026, 5, 5, 0),
        rates,
    )
    assert get_missing_funding_warnings() == 1
    reset_missing_funding_warnings()
    assert get_missing_funding_warnings() == 0
