"""Fundamentals staleness guard for compute_quality() — Phase 3 TDD suite."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from storage.db import get_connection, init_schema
from features.quality import compute_quality
from features.valuation import compute_valuation


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _company(conn, ticker, name="TEST CORP"):
    conn.execute(
        "INSERT INTO companies (ticker, company_name) VALUES (?, ?) ON CONFLICT DO NOTHING",
        [ticker, name],
    )


def _fund(conn, ticker, concept, value, unit="USD", dt=None):
    conn.execute(
        "INSERT INTO fundamentals_raw (id, ticker, concept, value, unit, as_of_date)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [uuid.uuid4().hex[:16], ticker, concept, value, unit,
         (dt or date.today()).isoformat()],
    )


def _feat(features, name):
    return next((f for f in features if f["feature_name"] == name), None)


# ── Not stale ──────────────────────────────────────────────────────────────────

def test_fresh_fundamentals_confidence_unchanged(conn):
    """Fundamentals 30 days old → confidence is NOT forced to 'low' by staleness."""
    _company(conn, "AAPL")
    recent = date.today() - timedelta(days=30)
    _fund(conn, "AAPL", "us-gaap/NetIncomeLoss", 100_000_000, dt=recent)
    _fund(conn, "AAPL", "us-gaap/Revenues", 1_000_000_000, dt=recent)

    feats = compute_quality(conn, "run1", "AAPL", date.today())
    margin = _feat(feats, "net_margin")

    assert margin is not None
    assert margin["feature_score"] is not None
    # Should not have staleness metadata (data is fresh)
    meta = margin.get("metadata") or {}
    assert "stale_fundamentals_days" not in meta, (
        f"Fresh fundamentals should not have stale_fundamentals_days, got {meta}"
    )


# ── Stale boundary ────────────────────────────────────────────────────────────

def test_staleness_boundary_180_days_not_stale(conn):
    """as_of_date exactly 180 days ago is NOT stale (boundary is exclusive: > 180)."""
    _company(conn, "BNDRY")
    boundary = date.today() - timedelta(days=180)
    _fund(conn, "BNDRY", "us-gaap/NetIncomeLoss", 50_000_000, dt=boundary)
    _fund(conn, "BNDRY", "us-gaap/Revenues", 500_000_000, dt=boundary)

    feats = compute_quality(conn, "run1", "BNDRY", date.today())
    margin = _feat(feats, "net_margin")

    meta = margin.get("metadata") or {}
    assert "stale_fundamentals_days" not in meta, (
        f"180-day-old data should NOT be stale, got {meta}"
    )


def test_staleness_boundary_181_days_is_stale(conn):
    """as_of_date 181 days ago IS stale (> 180 days threshold)."""
    _company(conn, "STALE")
    stale_date = date.today() - timedelta(days=181)
    _fund(conn, "STALE", "us-gaap/NetIncomeLoss", 50_000_000, dt=stale_date)
    _fund(conn, "STALE", "us-gaap/Revenues", 500_000_000, dt=stale_date)

    feats = compute_quality(conn, "run1", "STALE", date.today())
    margin = _feat(feats, "net_margin")

    meta = margin.get("metadata") or {}
    assert "stale_fundamentals_days" in meta, (
        f"181-day-old data should be stale, got {meta}"
    )
    assert meta["stale_fundamentals_days"] >= 181


# ── Stale effects ─────────────────────────────────────────────────────────────

def test_stale_fundamentals_forces_low_confidence(conn):
    """When most recent as_of_date > 180 days ago, all quality features get confidence='low'."""
    _company(conn, "OLD")
    old_date = date.today() - timedelta(days=400)
    _fund(conn, "OLD", "us-gaap/NetIncomeLoss", 50_000_000, dt=old_date)
    _fund(conn, "OLD", "us-gaap/Revenues", 500_000_000, dt=old_date)

    feats = compute_quality(conn, "run1", "OLD", date.today())

    for f in feats:
        assert f["confidence"] == "low", (
            f"Feature '{f['feature_name']}' should have confidence='low' for stale data, "
            f"got '{f['confidence']}'"
        )


def test_stale_fundamentals_adds_staleness_metadata(conn):
    """Stale data adds stale_fundamentals_days to each feature's metadata."""
    _company(conn, "OLD2")
    old_date = date.today() - timedelta(days=200)
    _fund(conn, "OLD2", "us-gaap/NetIncomeLoss", 50_000_000, dt=old_date)
    _fund(conn, "OLD2", "us-gaap/Revenues", 500_000_000, dt=old_date)

    feats = compute_quality(conn, "run1", "OLD2", date.today())

    for f in feats:
        if f["feature_score"] is not None or f["feature_value"] is not None:
            meta = f.get("metadata") or {}
            assert "stale_fundamentals_days" in meta, (
                f"Feature '{f['feature_name']}' missing stale_fundamentals_days in metadata: {meta}"
            )
            assert meta["stale_fundamentals_days"] >= 181


def test_feature_score_not_changed_by_staleness(conn):
    """Staleness guard only annotates; it does NOT zero out feature_score values."""
    _company(conn, "SVAL")
    old_date = date.today() - timedelta(days=200)
    _fund(conn, "SVAL", "us-gaap/NetIncomeLoss", 50_000_000, dt=old_date)
    _fund(conn, "SVAL", "us-gaap/Revenues", 500_000_000, dt=old_date)

    feats = compute_quality(conn, "run1", "SVAL", date.today())
    margin = _feat(feats, "net_margin")

    # Score should still be computed (10% margin → score 55)
    assert margin["feature_score"] is not None, (
        "Staleness guard should not zero the feature_score — only annotate"
    )
    assert margin["feature_value"] is not None


# ── No fundamentals ───────────────────────────────────────────────────────────

def test_no_fundamentals_all_null(conn):
    """No fundamentals rows → all features null (existing behavior unchanged)."""
    _company(conn, "EMPTY")

    feats = compute_quality(conn, "run1", "EMPTY", date.today())

    for f in feats:
        assert f["feature_score"] is None, (
            f"Feature '{f['feature_name']}' should be null with no data, got {f['feature_score']}"
        )


# ── Isolation: valuation not affected ────────────────────────────────────────

def test_staleness_does_not_affect_non_quality_features(conn):
    """compute_valuation result is not modified by the quality staleness guard."""
    _company(conn, "VISO")
    old_date = date.today() - timedelta(days=300)
    _fund(conn, "VISO", "us-gaap/Revenues", 10_000_000_000, dt=old_date)
    _fund(conn, "VISO", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding",
          1_000_000_000, dt=old_date)
    # Seed a price
    conn.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close, source)"
        " VALUES (?, 'VISO', ?, 50.0, 'stooq')",
        [uuid.uuid4().hex[:16], date.today().isoformat()],
    )

    val_feats = compute_valuation(conn, "run1", "VISO", date.today())

    for f in val_feats:
        meta = f.get("metadata") or {}
        assert "stale_fundamentals_days" not in meta, (
            f"Valuation feature '{f['feature_name']}' should not have staleness metadata, "
            f"got {meta}"
        )
