"""Tests for sector/theme/sympathy attribution logic."""
import pytest

from missed.sector_attribution import (
    AttributionLabel,
    classify_move_attribution,
    compute_relative_return,
)


def test_compute_relative_return():
    rel = compute_relative_return(ticker_return=0.08, sector_return=0.02)
    assert abs(rel - 0.06) < 1e-9


def test_compute_relative_return_negative():
    rel = compute_relative_return(ticker_return=-0.03, sector_return=-0.01)
    assert abs(rel - (-0.02)) < 1e-9


def test_direct_catalyst_large_move():
    label = classify_move_attribution(
        ticker_return=0.15,
        sector_return=0.02,
        has_direct_catalyst=True,
    )
    assert label == AttributionLabel.DIRECT_CATALYST


def test_sector_sympathy_detected():
    # Sector moves +5%, ticker moves +5.5%, small relative outperformance
    label = classify_move_attribution(
        ticker_return=0.055,
        sector_return=0.05,
        has_direct_catalyst=False,
    )
    assert label == AttributionLabel.SECTOR_SYMPATHY


def test_no_sector_data_gives_unknown():
    label = classify_move_attribution(
        ticker_return=0.10,
        sector_return=None,
        has_direct_catalyst=False,
    )
    assert label == AttributionLabel.UNKNOWN


def test_peer_cluster_move():
    label = classify_move_attribution(
        ticker_return=0.055,
        sector_return=0.05,
        has_direct_catalyst=False,
        peer_cluster_return=0.054,
    )
    assert label == AttributionLabel.PEER_CLUSTER_MOVE


def test_direct_catalyst_with_no_sector_data():
    label = classify_move_attribution(
        ticker_return=0.03,
        sector_return=None,
        has_direct_catalyst=True,
    )
    assert label == AttributionLabel.UNKNOWN  # sector_return=None → UNKNOWN before checking catalyst


def test_large_relative_outperformance_not_sympathy():
    # Ticker +12%, sector +2% → large relative → not sympathy
    label = classify_move_attribution(
        ticker_return=0.12,
        sector_return=0.02,
        has_direct_catalyst=False,
    )
    # relative = 0.10 > sympathy_relative_threshold(0.03) → UNKNOWN
    assert label == AttributionLabel.UNKNOWN


def test_attribution_label_enum_values():
    assert AttributionLabel.DIRECT_CATALYST.value == "direct_catalyst"
    assert AttributionLabel.SECTOR_SYMPATHY.value == "sector_sympathy"
    assert AttributionLabel.PEER_CLUSTER_MOVE.value == "peer_cluster_move"
    assert AttributionLabel.UNKNOWN.value == "unknown"
