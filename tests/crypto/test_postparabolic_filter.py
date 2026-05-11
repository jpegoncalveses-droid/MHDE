"""Tests for crypto.ml.postparabolic_filter.should_exclude.

Risk gate: exclude a coin if BOTH drawdown_from_90d_high < -0.20 AND
return_60d > +2.0 (strict). Fail-open on missing/NaN inputs. See
crypto/ml/POSTPARABOLIC_FILTER_SPEC.md.
"""
import math

from crypto.config import POSTPARABOLIC_DD90_THRESHOLD, POSTPARABOLIC_RET60_THRESHOLD
from crypto.ml.postparabolic_filter import should_exclude


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
