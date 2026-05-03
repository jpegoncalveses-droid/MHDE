"""Tests for the feature flag registry."""
import pytest

from governance.feature_flags import (
    FeatureFlag,
    FeatureFlagRegistry,
    apply_shadow_adjustments,
    load_flags_from_config,
)


def test_all_flags_disabled_by_default():
    registry = FeatureFlagRegistry()
    for flag in FeatureFlag:
        assert not registry.is_enabled(flag), f"{flag} should be disabled by default"


def test_flag_can_be_enabled():
    registry = FeatureFlagRegistry()
    registry.enable(FeatureFlag.SCALED_CATALYST_ADJUSTMENT)
    assert registry.is_enabled(FeatureFlag.SCALED_CATALYST_ADJUSTMENT)


def test_flag_can_be_disabled_after_enabling():
    registry = FeatureFlagRegistry()
    registry.enable(FeatureFlag.SECTOR_MOMENTUM_BOOST)
    registry.disable(FeatureFlag.SECTOR_MOMENTUM_BOOST)
    assert not registry.is_enabled(FeatureFlag.SECTOR_MOMENTUM_BOOST)


def test_load_from_empty_config_all_disabled():
    registry = load_flags_from_config({})
    for flag in FeatureFlag:
        assert not registry.is_enabled(flag)


def test_load_from_config_false_values_all_disabled():
    cfg = {
        "feature_flags": {
            "scaled_catalyst_adjustment": False,
            "sector_momentum_boost": False,
            "earnings_surprise_boost": False,
            "news_contract_boost": False,
            "risk_haircut": False,
        }
    }
    registry = load_flags_from_config(cfg)
    for flag in FeatureFlag:
        assert not registry.is_enabled(flag)


def test_load_from_config_enables_specific_flag():
    cfg = {"feature_flags": {"scaled_catalyst_adjustment": True}}
    registry = load_flags_from_config(cfg)
    assert registry.is_enabled(FeatureFlag.SCALED_CATALYST_ADJUSTMENT)
    assert not registry.is_enabled(FeatureFlag.SECTOR_MOMENTUM_BOOST)


def test_production_score_unchanged_when_all_disabled():
    registry = FeatureFlagRegistry()
    result = apply_shadow_adjustments(
        base_score=42.5,
        registry=registry,
        catalyst_adjustment=5.0,
        sector_boost=3.0,
        earnings_boost=2.0,
    )
    assert result["production_score"] == 42.5
    assert result["shadow_score"] == 42.5
    assert result["adjustments"] == {}


def test_shadow_score_changes_when_flag_enabled():
    registry = FeatureFlagRegistry()
    registry.enable(FeatureFlag.SCALED_CATALYST_ADJUSTMENT)
    result = apply_shadow_adjustments(
        base_score=42.5,
        registry=registry,
        catalyst_adjustment=5.0,
    )
    assert result["production_score"] == 42.5
    assert abs(result["shadow_score"] - 47.5) < 0.001
    assert "catalyst" in result["adjustments"]


def test_shadow_score_clamped_to_100():
    registry = FeatureFlagRegistry()
    registry.enable(FeatureFlag.SCALED_CATALYST_ADJUSTMENT)
    registry.enable(FeatureFlag.SECTOR_MOMENTUM_BOOST)
    result = apply_shadow_adjustments(
        base_score=98.0,
        registry=registry,
        catalyst_adjustment=5.0,
        sector_boost=5.0,
    )
    assert result["shadow_score"] <= 100.0


def test_shadow_score_clamped_to_zero():
    registry = FeatureFlagRegistry()
    registry.enable(FeatureFlag.RISK_HAIRCUT)
    result = apply_shadow_adjustments(
        base_score=2.0,
        registry=registry,
        risk_haircut=10.0,
    )
    assert result["shadow_score"] >= 0.0


def test_feature_flag_enum_values():
    assert FeatureFlag.SCALED_CATALYST_ADJUSTMENT.value == "scaled_catalyst_adjustment"
    assert FeatureFlag.SECTOR_MOMENTUM_BOOST.value == "sector_momentum_boost"
    assert FeatureFlag.EARNINGS_SURPRISE_BOOST.value == "earnings_surprise_boost"
    assert FeatureFlag.NEWS_CONTRACT_BOOST.value == "news_contract_boost"
    assert FeatureFlag.RISK_HAIRCUT.value == "risk_haircut"


def test_enabled_flags_returns_list():
    registry = FeatureFlagRegistry()
    registry.enable(FeatureFlag.SCALED_CATALYST_ADJUSTMENT)
    flags = registry.enabled_flags()
    assert isinstance(flags, list)
    assert FeatureFlag.SCALED_CATALYST_ADJUSTMENT in flags
