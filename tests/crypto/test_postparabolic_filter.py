"""Tests for crypto.ml.postparabolic_filter.should_exclude.

Risk gate — a coin is excluded if EITHER rule fires (OR-combined):

  Rule A (post-parabolic, original): drawdown_from_90d_high < -0.20
                                     AND return_60d > +2.0 (strict).
  Rule B (short-window momentum, added 2026-05-14 — see ADR-028):
                                     return_5d < -0.30 (strict).

Each rule fails-open on its own missing/NaN inputs. See
crypto/ml/POSTPARABOLIC_FILTER_SPEC.md.
"""
import math

from crypto.config import (
    POSTPARABOLIC_DD90_THRESHOLD,
    POSTPARABOLIC_RET60_THRESHOLD,
    POSTPARABOLIC_RET5_THRESHOLD,
)
from crypto.ml.postparabolic_filter import (
    REASON_POST_PARABOLIC,
    REASON_SHORT_MOMENTUM,
    REASON_BOTH,
    should_exclude,
)


def test_both_conditions_met_excludes():
    excluded, reason = should_exclude(-0.25, 3.0)
    assert excluded is True
    assert isinstance(reason, str) and reason


def test_only_dd90_met_not_excluded():
    # deep drawdown but not up much over 60d
    assert should_exclude(-0.40, 0.5) == (False, None)


def test_only_ret60_met_not_excluded():
    # huge 60d run but near its 90d high
    assert should_exclude(-0.05, 5.0) == (False, None)


def test_exact_edge_dd90_not_excluded():
    # dd90 exactly at the threshold — strict less-than → not excluded
    assert should_exclude(POSTPARABOLIC_DD90_THRESHOLD, 3.0) == (False, None)


def test_exact_edge_ret60_not_excluded():
    # ret60 exactly at the threshold — strict greater-than → not excluded
    assert should_exclude(-0.30, POSTPARABOLIC_RET60_THRESHOLD) == (False, None)


def test_just_inside_both_thresholds_excludes():
    excluded, _ = should_exclude(POSTPARABOLIC_DD90_THRESHOLD - 1e-9,
                                 POSTPARABOLIC_RET60_THRESHOLD + 1e-9)
    assert excluded is True


def test_missing_dd90_fail_open():
    assert should_exclude(None, 3.0) == (False, None)


def test_missing_ret60_fail_open():
    assert should_exclude(-0.30, None) == (False, None)


def test_nan_inputs_fail_open():
    assert should_exclude(math.nan, 3.0) == (False, None)
    assert should_exclude(-0.30, math.nan) == (False, None)
    assert should_exclude(math.nan, math.nan) == (False, None)


def test_negative_ret60_not_excluded():
    # coin down on 60d as well — definitely not a post-parabolic case
    assert should_exclude(-0.50, -0.30) == (False, None)


def test_reason_is_stable_token():
    _, r1 = should_exclude(-0.25, 3.0)
    _, r2 = should_exclude(-0.99, 10.0)
    assert r1 == r2  # same canonical reason regardless of magnitudes


# ──────────────────────────────────────────────────────────────────────
# Rule B — short-window momentum (return_5d < -0.30). ADR-028.
# ──────────────────────────────────────────────────────────────────────


def test_ret5_below_threshold_alone_excludes():
    """ret5 < -0.30 with benign dd90/ret60 still excludes — the new
    rule fires independently of the post-parabolic gate."""
    excluded, reason = should_exclude(-0.05, 0.5, ret5=-0.35)
    assert excluded is True
    assert reason == REASON_SHORT_MOMENTUM


def test_ret5_exact_edge_not_excluded():
    """Strict less-than: ret5 == -0.30 does NOT fire."""
    excluded, reason = should_exclude(-0.05, 0.5, ret5=POSTPARABOLIC_RET5_THRESHOLD)
    assert (excluded, reason) == (False, None)


def test_ret5_just_below_threshold_excludes():
    excluded, _ = should_exclude(-0.05, 0.5, ret5=POSTPARABOLIC_RET5_THRESHOLD - 1e-9)
    assert excluded is True


def test_ret5_above_threshold_not_excluded():
    """ret5 = -0.20 (close to threshold but above) does NOT fire."""
    assert should_exclude(-0.05, 0.5, ret5=-0.20) == (False, None)


def test_missing_ret5_does_not_block_baseline():
    """ret5 None must not change baseline behavior — baseline still fires."""
    excluded, reason = should_exclude(-0.25, 3.0, ret5=None)
    assert excluded is True
    assert reason == REASON_POST_PARABOLIC


def test_missing_ret5_fails_open_for_short_momentum():
    """ret5 None + benign baseline → no exclusion (Rule B fails open
    independently of Rule A)."""
    assert should_exclude(-0.05, 0.5, ret5=None) == (False, None)


def test_nan_ret5_fails_open_for_short_momentum():
    assert should_exclude(-0.05, 0.5, ret5=math.nan) == (False, None)


def test_ret5_default_value_preserves_legacy_callers():
    """Callers that pass only (dd90, ret60) — i.e. the pre-ADR-028 signature —
    get the same answer they used to. ret5 defaults to None (fail-open)."""
    assert should_exclude(-0.25, 3.0) == (True, REASON_POST_PARABOLIC)
    assert should_exclude(-0.05, 0.5) == (False, None)


def test_both_rules_fire_reason_distinguishes():
    """SWARMSUSDT-class case — deep dd, parabolic ret60, AND ret5 < -0.30.
    The reason token must surface that BOTH rules contributed."""
    excluded, reason = should_exclude(-0.50, 2.5, ret5=-0.37)
    assert excluded is True
    assert reason == REASON_BOTH


def test_reason_short_momentum_stable():
    """Same canonical short-momentum token regardless of feature magnitudes."""
    _, r1 = should_exclude(-0.05, 0.5, ret5=-0.35)
    _, r2 = should_exclude(0.0, 0.0, ret5=-0.99)
    assert r1 == r2 == REASON_SHORT_MOMENTUM


def test_reason_tokens_are_distinct():
    """Three reason tokens must all be different (so they group cleanly in
    crypto_signal_exclusions / log aggregators)."""
    assert REASON_POST_PARABOLIC != REASON_SHORT_MOMENTUM
    assert REASON_POST_PARABOLIC != REASON_BOTH
    assert REASON_SHORT_MOMENTUM != REASON_BOTH


def test_swarmsusdt_live_incident_excluded():
    """Pin the SWARMSUSDT 2026-05-13 feature snapshot — the live incident
    that motivated ADR-028. dd90 = -0.4997, ret60 = +1.4714 (below the +2.0
    baseline gate, so Rule A does NOT fire), ret5 = -0.3680 (below -0.30,
    Rule B fires)."""
    excluded, reason = should_exclude(-0.4997, 1.4714, ret5=-0.3680)
    assert excluded is True
    assert reason == REASON_SHORT_MOMENTUM


def test_4usdt_live_incident_not_excluded():
    """Pin the 4USDT 2026-05-11 feature snapshot — confirmed to NOT be
    caught by the new filter (see backtest report). ret5 = -0.0116 is
    nowhere near -0.30 and ret60 = +0.60 is below the +2.0 baseline gate.
    The 4USDT-class failure pattern is a separate workstream."""
    assert should_exclude(-0.4354, 0.6000, ret5=-0.0116) == (False, None)
