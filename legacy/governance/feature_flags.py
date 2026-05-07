"""Feature flag registry for shadow experiments.

All flags are disabled by default. To enable a flag for testing, set it to true
in config/settings.yaml under feature_flags. No flag must ever be enabled by
default — this is the safety invariant protecting production scoring.

Production scoring is only affected when a flag is explicitly enabled in config
and the corresponding code path reads the flag via FeatureFlagRegistry.is_enabled().
"""
from __future__ import annotations

from enum import Enum


class FeatureFlag(Enum):
    SCALED_CATALYST_ADJUSTMENT = "scaled_catalyst_adjustment"
    SECTOR_MOMENTUM_BOOST = "sector_momentum_boost"
    EARNINGS_SURPRISE_BOOST = "earnings_surprise_boost"
    NEWS_CONTRACT_BOOST = "news_contract_boost"
    RISK_HAIRCUT = "risk_haircut"


class FeatureFlagRegistry:
    """Holds the enabled/disabled state of all feature flags."""

    def __init__(self) -> None:
        self._enabled: set[FeatureFlag] = set()

    def enable(self, flag: FeatureFlag) -> None:
        self._enabled.add(flag)

    def disable(self, flag: FeatureFlag) -> None:
        self._enabled.discard(flag)

    def is_enabled(self, flag: FeatureFlag) -> bool:
        return flag in self._enabled

    def enabled_flags(self) -> list[FeatureFlag]:
        return sorted(self._enabled, key=lambda f: f.value)


def load_flags_from_config(cfg: dict) -> FeatureFlagRegistry:
    """Build a FeatureFlagRegistry from the 'feature_flags' section of config.

    All flags default to disabled. A flag is only enabled when explicitly set to
    true (boolean) in the config. Any non-true value (false, missing, "0") leaves
    the flag disabled.
    """
    registry = FeatureFlagRegistry()
    flags_cfg = cfg.get("feature_flags", {}) or {}
    for flag in FeatureFlag:
        if flags_cfg.get(flag.value) is True:
            registry.enable(flag)
    return registry


def apply_shadow_adjustments(
    base_score: float,
    registry: FeatureFlagRegistry,
    catalyst_adjustment: float = 0.0,
    sector_boost: float = 0.0,
    earnings_boost: float = 0.0,
    news_boost: float = 0.0,
    risk_haircut: float = 0.0,
) -> dict:
    """Apply enabled feature-flag adjustments to produce a shadow score.

    The production_score is NEVER changed. Only shadow_score reflects
    enabled experiment adjustments.

    Returns a dict with: production_score, shadow_score, adjustments (dict of applied boosts).
    """
    shadow = base_score
    adjustments: dict[str, float] = {}

    if registry.is_enabled(FeatureFlag.SCALED_CATALYST_ADJUSTMENT) and catalyst_adjustment:
        shadow += catalyst_adjustment
        adjustments["catalyst"] = catalyst_adjustment

    if registry.is_enabled(FeatureFlag.SECTOR_MOMENTUM_BOOST) and sector_boost:
        shadow += sector_boost
        adjustments["sector"] = sector_boost

    if registry.is_enabled(FeatureFlag.EARNINGS_SURPRISE_BOOST) and earnings_boost:
        shadow += earnings_boost
        adjustments["earnings"] = earnings_boost

    if registry.is_enabled(FeatureFlag.NEWS_CONTRACT_BOOST) and news_boost:
        shadow += news_boost
        adjustments["news"] = news_boost

    if registry.is_enabled(FeatureFlag.RISK_HAIRCUT) and risk_haircut:
        shadow -= risk_haircut
        adjustments["risk_haircut"] = -risk_haircut

    shadow = max(0.0, min(100.0, shadow))

    return {
        "production_score": base_score,
        "shadow_score": shadow,
        "adjustments": adjustments,
    }
