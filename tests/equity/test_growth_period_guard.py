"""Period-alignment guard for revenue growth metrics."""
from __future__ import annotations

import uuid
from datetime import date

import pytest

from storage.db import get_connection, init_schema
from features.quality import compute_quality


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


def _fund(conn, ticker, concept, value, unit="USD", dt=None, form=None):
    conn.execute(
        "INSERT INTO fundamentals_raw (id, ticker, concept, value, unit, as_of_date, form) "
        "VALUES (?,?,?,?,?,?,?)",
        [uuid.uuid4().hex[:16], ticker, concept, value, unit,
         (dt or date.today()).isoformat(), form],
    )


def _feat(features, name):
    return next((f for f in features if f["feature_name"] == name), None)


# ── Form-based period detection ────────────────────────────────────────────────

def test_annual_vs_annual_growth_valid(conn):
    """Two 10-K rows → annual vs annual → growth computed normally."""
    _company(conn, "AAPL")
    _fund(conn, "AAPL", "us-gaap/Revenues", 400_000_000_000,
          dt=date(2024, 9, 28), form="10-K")
    _fund(conn, "AAPL", "us-gaap/Revenues", 385_000_000_000,
          dt=date(2023, 9, 30), form="10-K")

    feats = compute_quality(conn, "run1", "AAPL", date.today())
    rev = _feat(feats, "revenue_growth_yoy")
    assert rev["feature_score"] is not None, \
        f"Annual vs annual growth should compute, got {rev}"
    meta = rev.get("metadata") or {}
    assert meta.get("period_alignment_status") == "aligned"


def test_quarterly_vs_quarterly_growth_valid(conn):
    """Two 10-Q rows → quarterly vs quarterly → growth computed normally."""
    _company(conn, "MSFT")
    _fund(conn, "MSFT", "us-gaap/Revenues", 65_000_000_000,
          dt=date(2024, 12, 31), form="10-Q")
    _fund(conn, "MSFT", "us-gaap/Revenues", 62_000_000_000,
          dt=date(2024, 9, 30), form="10-Q")

    feats = compute_quality(conn, "run1", "MSFT", date.today())
    rev = _feat(feats, "revenue_growth_yoy")
    assert rev["feature_score"] is not None, \
        f"Quarterly vs quarterly growth should compute, got {rev}"
    meta = rev.get("metadata") or {}
    assert meta.get("period_alignment_status") == "aligned"


def test_annual_vs_quarterly_growth_nulled(conn):
    """10-K vs 10-Q → period mismatch → growth null."""
    _company(conn, "CFG")
    _fund(conn, "CFG", "us-gaap/Revenues", 8_247_000_000,
          dt=date(2025, 12, 31), form="10-K")
    _fund(conn, "CFG", "us-gaap/Revenues", 2_118_000_000,
          dt=date(2025, 9, 30), form="10-Q")

    feats = compute_quality(conn, "run1", "CFG", date.today())
    rev = _feat(feats, "revenue_growth_yoy")
    assert rev["feature_score"] is None, \
        f"Annual vs quarterly should null growth, got {rev}"
    meta = rev.get("metadata") or {}
    assert "period_mismatch" in meta.get("missing_reason", ""), \
        f"Should set missing_reason=period_mismatch, got {meta}"


def test_period_mismatch_metadata_fields(conn):
    """Mismatched periods include diagnostic metadata fields."""
    _company(conn, "BANK")
    _fund(conn, "BANK", "us-gaap/Revenues", 10_000_000_000,
          dt=date(2025, 12, 31), form="10-K")
    _fund(conn, "BANK", "us-gaap/Revenues", 2_500_000_000,
          dt=date(2025, 9, 30), form="10-Q")

    feats = compute_quality(conn, "run1", "BANK", date.today())
    rev = _feat(feats, "revenue_growth_yoy")
    meta = rev.get("metadata") or {}
    assert "current_period_end" in meta
    assert "prior_period_end" in meta
    assert "period_alignment_status" in meta
    assert meta["period_alignment_status"] == "mismatched"


# ── Gap-based fallback (no form column) ───────────────────────────────────────

def test_no_form_annual_gap_valid(conn):
    """No form, ~365 day gap → inferred annual vs annual → growth computed."""
    _company(conn, "ANON")
    _fund(conn, "ANON", "us-gaap/Revenues", 2_000_000_000, dt=date(2024, 12, 31))
    _fund(conn, "ANON", "us-gaap/Revenues", 1_800_000_000, dt=date(2023, 12, 31))

    feats = compute_quality(conn, "run1", "ANON", date.today())
    rev = _feat(feats, "revenue_growth_yoy")
    assert rev["feature_score"] is not None, \
        f"365-day gap with no form should be treated as annual, got {rev}"


def test_no_form_quarterly_gap_valid(conn):
    """No form, ~90 day gap → inferred quarterly → growth computed."""
    _company(conn, "QTRLY")
    _fund(conn, "QTRLY", "us-gaap/Revenues", 500_000_000, dt=date(2024, 12, 31))
    _fund(conn, "QTRLY", "us-gaap/Revenues", 480_000_000, dt=date(2024, 9, 30))

    feats = compute_quality(conn, "run1", "QTRLY", date.today())
    rev = _feat(feats, "revenue_growth_yoy")
    assert rev["feature_score"] is not None, \
        f"90-day gap with no form should be treated as quarterly, got {rev}"


def test_no_form_ambiguous_gap_nulled(conn):
    """No form, ambiguous gap (e.g. 274 days) → null with period_mismatch."""
    _company(conn, "AMBIG")
    _fund(conn, "AMBIG", "us-gaap/Revenues", 8_000_000_000, dt=date(2024, 12, 31))
    _fund(conn, "AMBIG", "us-gaap/Revenues", 2_000_000_000, dt=date(2024, 3, 31))

    feats = compute_quality(conn, "run1", "AMBIG", date.today())
    rev = _feat(feats, "revenue_growth_yoy")
    assert rev["feature_score"] is None, \
        f"274-day gap (no form) should null growth, got {rev}"
    meta = rev.get("metadata") or {}
    assert "period_mismatch" in meta.get("missing_reason", "")


# ── Contribution to quality score ─────────────────────────────────────────────

def test_period_mismatch_does_not_inflate_score(conn):
    """CFG-exact: annual vs quarterly → growth null, does not inflate quality."""
    _company(conn, "CFGX", "CITIZENS FINANCIAL GROUP INC")
    _fund(conn, "CFGX", "us-gaap/Revenues", 8_247_000_000,
          dt=date(2025, 12, 31), form="10-K")
    _fund(conn, "CFGX", "us-gaap/Revenues", 2_118_000_000,
          dt=date(2025, 9, 30), form="10-Q")
    _fund(conn, "CFGX", "us-gaap/NetIncomeLoss", 1_500_000_000)

    feats = compute_quality(conn, "run1", "CFGX", date.today())
    rev = _feat(feats, "revenue_growth_yoy")
    assert rev["feature_score"] is None, \
        "CFG-like 289% artifact should be nulled by period guard"


def test_valid_annual_growth_contributes_to_score(conn):
    """Valid annual comparison → growth score is computed and non-null."""
    _company(conn, "GROW")
    _fund(conn, "GROW", "us-gaap/Revenues", 2_000_000_000,
          dt=date(2024, 12, 31), form="10-K")
    _fund(conn, "GROW", "us-gaap/Revenues", 1_800_000_000,
          dt=date(2023, 12, 31), form="10-K")

    feats = compute_quality(conn, "run1", "GROW", date.today())
    rev = _feat(feats, "revenue_growth_yoy")
    assert rev["feature_score"] is not None, "Valid annual growth should score"
    assert abs(rev["feature_value"] - 11.11) < 0.1, \
        f"Expected ~11.1% growth, got {rev['feature_value']}"
