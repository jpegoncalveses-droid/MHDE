"""Sector/theme/sympathy attribution logic.

Classifies a price move as direct_catalyst, sector_sympathy, peer_cluster_move,
or unknown — without any ticker-specific special cases.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


class AttributionLabel(Enum):
    DIRECT_CATALYST = "direct_catalyst"
    SECTOR_SYMPATHY = "sector_sympathy"
    PEER_CLUSTER_MOVE = "peer_cluster_move"
    THEME_MOMENTUM = "theme_momentum"
    MACRO_OR_FACTOR = "macro_or_factor"
    UNKNOWN = "unknown"


def compute_relative_return(ticker_return: float, sector_return: float) -> float:
    """Ticker excess return vs sector."""
    return ticker_return - sector_return


def classify_move_attribution(
    ticker_return: float,
    sector_return: Optional[float],
    has_direct_catalyst: bool,
    peer_cluster_return: Optional[float] = None,
    direct_catalyst_threshold: float = 0.05,
    sympathy_sector_threshold: float = 0.02,
    sympathy_relative_threshold: float = 0.03,
) -> AttributionLabel:
    """
    Classify what drove a price move.

    Rules (in priority order):
    1. If has_direct_catalyst and |ticker_return| > direct_catalyst_threshold → DIRECT_CATALYST
    2. If sector_return is None → UNKNOWN (no data to attribute)
    3. If sector moved significantly and ticker's relative outperformance is small:
       - If peer_cluster_return is close → PEER_CLUSTER_MOVE
       - Else → SECTOR_SYMPATHY
    4. If has_direct_catalyst (but below size threshold) → DIRECT_CATALYST
    5. → UNKNOWN
    """
    if has_direct_catalyst and abs(ticker_return) > direct_catalyst_threshold:
        return AttributionLabel.DIRECT_CATALYST

    if sector_return is None:
        return AttributionLabel.UNKNOWN

    relative = compute_relative_return(ticker_return, sector_return)

    if abs(sector_return) > sympathy_sector_threshold and abs(relative) < sympathy_relative_threshold:
        if (
            peer_cluster_return is not None
            and abs(ticker_return - peer_cluster_return) < 0.01
        ):
            return AttributionLabel.PEER_CLUSTER_MOVE
        return AttributionLabel.SECTOR_SYMPATHY

    if has_direct_catalyst:
        return AttributionLabel.DIRECT_CATALYST

    return AttributionLabel.UNKNOWN
