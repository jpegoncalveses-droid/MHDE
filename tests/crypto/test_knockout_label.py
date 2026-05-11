"""Unit tests for crypto.ml.knockout_label.knockout_classify (the pure
triple-barrier classifier). See crypto/ml/KNOCKOUT_LABEL_SPEC.md.

Convention: tp is a positive fraction (0.10 → TP barrier = C·1.10), sl is
a negative fraction (-0.05 → SL barrier = C·0.95). TP test inclusive
(high >= barrier); SL test inclusive (low <= barrier). Same-bar both-touch
→ "sl" when sl_first (the pessimistic default).
"""
import math

from crypto.config import KNOCKOUT_SL, KNOCKOUT_TP
from crypto.ml.knockout_label import (
    OUTCOME_NEITHER,
    OUTCOME_SL,
    OUTCOME_TP,
    knockout_classify,
)

TP, SL = 0.10, -0.05  # local copies for clarity
C = 100.0


def kc(highs, lows, *, tp=TP, sl=SL, horizon=10, sl_first=True, entry=C):
    return knockout_classify(highs, lows, entry, tp, sl, horizon, sl_first=sl_first)


def test_tp_first_on_day1():
    assert kc([112.0], [99.0]) == (OUTCOME_TP, 1)


def test_sl_first_on_day1_by_order():
    # only the SL barrier is touched
    assert kc([105.0], [94.0]) == (OUTCOME_SL, 1)


def test_tp_on_a_later_bar():
    assert kc([105.0, 111.0], [98.0, 100.0]) == (OUTCOME_TP, 2)


def test_sl_on_a_later_bar():
    assert kc([105.0, 106.0], [98.0, 94.0]) == (OUTCOME_SL, 2)


def test_neither_within_horizon():
    assert kc([105.0, 106.0, 104.0], [98.0, 97.0, 99.0]) == (OUTCOME_NEITHER, None)


def test_exact_edge_tp_inclusive():
    # high exactly at C·(1+tp) → counts as a TP touch
    assert kc([C * (1 + TP)], [99.0]) == (OUTCOME_TP, 1)


def test_exact_edge_sl_inclusive():
    # low exactly at C·(1+sl) → counts as an SL touch
    assert kc([105.0], [C * (1 + SL)]) == (OUTCOME_SL, 1)


def test_same_bar_both_touch_returns_sl_when_sl_first():
    assert kc([115.0], [90.0], sl_first=True) == (OUTCOME_SL, 1)


def test_same_bar_both_touch_returns_tp_when_not_sl_first():
    assert kc([115.0], [90.0], sl_first=False) == (OUTCOME_TP, 1)


def test_gap_up_through_tp_day1():
    # day-1 gaps up: the whole bar is above the TP barrier → win on day 1
    assert kc([130.0], [120.0]) == (OUTCOME_TP, 1)


def test_gap_down_through_sl_day1():
    # day-1 gaps down through SL: high never reaches TP → loss on day 1
    assert kc([80.0], [70.0]) == (OUTCOME_SL, 1)


def test_partial_window_shorter_than_horizon_no_touch():
    # horizon=10 but only 3 forward bars available, no barrier hit
    assert kc([105.0, 104.0, 106.0], [98.0, 97.0, 99.0], horizon=10) == (OUTCOME_NEITHER, None)


def test_partial_window_shorter_than_horizon_with_touch():
    assert kc([105.0, 112.0], [98.0, 100.0], horizon=10) == (OUTCOME_TP, 2)


def test_empty_forward_window():
    assert kc([], []) == (OUTCOME_NEITHER, None)


def test_nonpositive_entry_close():
    assert kc([200.0], [50.0], entry=0.0) == (OUTCOME_NEITHER, None)
    assert kc([200.0], [50.0], entry=-5.0) == (OUTCOME_NEITHER, None)


def test_nan_bar_is_skipped_not_a_touch():
    # day-1 high/low NaN (bad data) → not a touch; day-2 hits TP
    assert kc([math.nan, 112.0], [math.nan, 100.0]) == (OUTCOME_TP, 2)


def test_horizon_caps_lookahead():
    # bar 7 would hit TP, but horizon=5 cuts the window first
    highs = [105.0] * 6 + [115.0] + [105.0] * 5
    lows = [98.0] * 12
    assert kc(highs, lows, horizon=5) == (OUTCOME_NEITHER, None)
    assert kc(highs, lows, horizon=10) == (OUTCOME_TP, 7)


def test_negative_sl_param_sets_lower_barrier():
    # sl=-0.05 → SL barrier at 95, not 105. A low of 96 must NOT trigger SL.
    assert kc([105.0], [96.0]) == (OUTCOME_NEITHER, None)
    assert kc([105.0], [95.0]) == (OUTCOME_SL, 1)


def test_config_constants_have_expected_signs():
    assert KNOCKOUT_TP == 0.10
    assert KNOCKOUT_SL == -0.05


def test_skyai_2026_05_11_post_crash_entry_is_a_loss_on_day1():
    """SKYAIUSDT hypothetical entry at the 2026-05-10 close (0.54185); the
    next bar (2026-05-11) was H 0.55680 / L 0.38260. The low pierces the
    -5% barrier (0.51476) on day 1 before the high reaches the +10% barrier
    (0.59604) — knockout = LOSS, day 1, for both horizons and both tiebreaks."""
    entry = 0.54185
    for horizon in (5, 10):
        for sl_first in (True, False):
            assert knockout_classify([0.55680], [0.38260], entry, KNOCKOUT_TP,
                                     KNOCKOUT_SL, horizon, sl_first=sl_first) == (OUTCOME_SL, 1)
