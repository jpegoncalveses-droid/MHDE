"""TDD tests for ingestion.ingest_fred._SERIES contents.

ml/features.py:_load_yield_curve needs DGS2 to compute the yield-curve
feature; today _SERIES omits it (see finding1_cross_asset_ingestion_root_cause.md
section 1b). VIXCLS is added as a redundant VIX path.
"""
from __future__ import annotations


def test_series_includes_dgs2():
    """ml/features.py joins DGS10 with DGS2 — both must be in the scheduled pull."""
    from ingestion.ingest_fred import _SERIES

    assert "DGS2" in _SERIES, "DGS2 missing from FRED _SERIES — yield-curve feature blocked"


def test_series_includes_vixcls():
    """VIXCLS is the FRED-side VIX backup for the prices_daily VIX path."""
    from ingestion.ingest_fred import _SERIES

    assert "VIXCLS" in _SERIES, "VIXCLS missing from FRED _SERIES — no VIX backup path"


def test_existing_series_preserved():
    """Adding new series must not remove the ones already being pulled."""
    from ingestion.ingest_fred import _SERIES

    for required in ("FEDFUNDS", "DGS10", "CPIAUCSL", "UNRATE", "PAYEMS", "GDP"):
        assert required in _SERIES, f"Existing series {required} was removed"
