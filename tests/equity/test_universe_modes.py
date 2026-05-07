"""Tests for universe mode constants."""
from universe.universe_builder import UNIVERSE_MODES, DEFAULT_UNIVERSE_MODE


def test_universe_modes_tuple_contains_expected_modes():
    assert "sp500" in UNIVERSE_MODES
    assert "us_large_cap" in UNIVERSE_MODES
    assert "extended" in UNIVERSE_MODES


def test_universe_modes_is_tuple():
    assert isinstance(UNIVERSE_MODES, tuple)


def test_default_universe_mode_is_sp500():
    assert DEFAULT_UNIVERSE_MODE == "sp500"


def test_default_universe_mode_in_modes():
    assert DEFAULT_UNIVERSE_MODE in UNIVERSE_MODES
