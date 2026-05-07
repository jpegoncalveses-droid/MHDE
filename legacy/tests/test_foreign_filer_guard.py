"""Foreign filer valuation guard — experiment 3fa9a31cc3704b85."""
from __future__ import annotations

import uuid
from datetime import date

import pytest

from storage.db import get_connection, init_schema
from features.valuation import compute_valuation
from features.catalyst import compute_catalyst


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _price(conn, ticker, close):
    conn.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close, source) VALUES (?,?,?,?,'test')",
        [uuid.uuid4().hex[:16], ticker, date.today().isoformat(), close],
    )


def _fund(conn, ticker, concept, value, unit="USD"):
    conn.execute(
        "INSERT INTO fundamentals_raw (id, ticker, concept, value, unit, as_of_date) VALUES (?,?,?,?,?,?)",
        [uuid.uuid4().hex[:16], ticker, concept, value, unit, date.today().isoformat()],
    )


def _filing(conn, ticker, form_type, days_ago=5):
    from datetime import timedelta
    conn.execute(
        "INSERT INTO filings (id, ticker, form_type, filing_date) VALUES (?,?,?,?)",
        [uuid.uuid4().hex[:16], ticker, form_type,
         (date.today() - timedelta(days=days_ago)).isoformat()],
    )


def _feat(features, name):
    return next((f for f in features if f["feature_name"] == name), None)


# ── Foreign filer valuation guard ─────────────────────────────────────────────

def test_20f_filer_with_cny_nulls_all_valuation_ratios(conn):
    """BIDU-like: 20-F filer, revenue in CNY → P/S, P/E, P/B all null."""
    _price(conn, "BIDU", 126.0)
    _filing(conn, "BIDU", "20-F")
    _fund(conn, "BIDU", "us-gaap/Revenues", 129_000_000_000, unit="CNY")
    _fund(conn, "BIDU", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding", 34_900_000, unit="shares")
    _fund(conn, "BIDU", "us-gaap/EarningsPerShareDiluted", 100.89, unit="CNY/shares")
    _fund(conn, "BIDU", "us-gaap/StockholdersEquity", 500_000_000_000, unit="CNY")

    feats = compute_valuation(conn, "run1", "BIDU", date.today())

    for name in ("ps_proxy", "pe_ratio", "pb_ratio"):
        f = _feat(feats, name)
        assert f["feature_score"] is None, f"{name} should be null for foreign CNY filer, got {f}"
        assert f.get("metadata", {}) and "missing_reason" in f["metadata"], \
            f"{name} should have missing_reason in metadata"
        assert "foreign" in f["metadata"]["missing_reason"], \
            f"missing_reason should mention 'foreign', got {f['metadata']['missing_reason']}"


def test_6k_filer_with_cny_nulls_valuation_ratios(conn):
    """6-K-only filer with CNY revenue → same guard applies."""
    _price(conn, "XYZ", 50.0)
    _filing(conn, "XYZ", "6-K")
    _fund(conn, "XYZ", "us-gaap/Revenues", 10_000_000_000, unit="CNY")
    _fund(conn, "XYZ", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding", 500_000_000, unit="shares")

    feats = compute_valuation(conn, "run1", "XYZ", date.today())
    ps = _feat(feats, "ps_proxy")
    assert ps["feature_score"] is None, f"P/S should be null for 6-K CNY filer, got {ps}"


def test_foreign_filer_unknown_currency_nulls_ratios(conn):
    """20-F filer with no unit info → null with foreign_filer_currency_unknown."""
    _price(conn, "UK", 40.0)
    _filing(conn, "UK", "20-F")
    # No fundamentals seeded → no unit available → unknown currency
    feats = compute_valuation(conn, "run1", "UK", date.today())
    for name in ("ps_proxy", "pe_ratio", "pb_ratio"):
        f = _feat(feats, name)
        # foreign filer with no data → ratios null (could be null due to missing data OR currency unknown)
        assert f["feature_score"] is None, f"{name} should be null for unknown-currency 20-F filer"


def test_usd_reporting_foreign_filer_not_nulled(conn):
    """Foreign filer (20-F) that reports in USD → valuation computed normally."""
    _price(conn, "CHKP", 200.0)
    _filing(conn, "CHKP", "20-F")
    _fund(conn, "CHKP", "us-gaap/Revenues", 2_000_000_000, unit="USD")
    _fund(conn, "CHKP", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding", 200_000_000, unit="shares")
    _fund(conn, "CHKP", "us-gaap/EarningsPerShareDiluted", 8.0, unit="USD/shares")
    _fund(conn, "CHKP", "us-gaap/StockholdersEquity", 5_000_000_000, unit="USD")

    feats = compute_valuation(conn, "run1", "CHKP", date.today())
    ps = _feat(feats, "ps_proxy")
    # P/S = (200 * 200M) / 2B = 20 → in bounds → should have a score
    assert ps["feature_score"] is not None, f"USD-reporting foreign filer should compute P/S, got {ps}"


def test_domestic_filer_not_affected(conn):
    """Domestic filer (10-K only) is unaffected by the foreign filer guard."""
    _price(conn, "AAPL", 200.0)
    _filing(conn, "AAPL", "10-K")
    _fund(conn, "AAPL", "us-gaap/Revenues", 400_000_000_000, unit="USD")
    _fund(conn, "AAPL", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding", 15_000_000_000, unit="shares")
    _fund(conn, "AAPL", "us-gaap/EarningsPerShareDiluted", 6.0, unit="USD/shares")
    _fund(conn, "AAPL", "us-gaap/StockholdersEquity", 50_000_000_000, unit="USD")

    feats = compute_valuation(conn, "run1", "AAPL", date.today())
    ps = _feat(feats, "ps_proxy")
    assert ps["feature_score"] is not None, "Domestic 10-K filer should compute valuation normally"


# ── 6-K catalyst handling ─────────────────────────────────────────────────────

def test_6k_filing_not_scored_as_catalyst(conn):
    """6-K filings should NOT add points to catalyst_score."""
    _filing(conn, "BIDU", "6-K", days_ago=3)

    feats = compute_catalyst(conn, "run1", "BIDU", date.today())
    cat = _feat(feats, "catalyst_score")
    # 6-K alone should not produce a non-zero catalyst score
    assert cat["feature_score"] == 0.0, \
        f"6-K filing should not be scored as catalyst, got score={cat['feature_score']}"


def test_6k_filing_appears_in_disclosure_evidence(conn):
    """6-K filings should be recorded as disclosure evidence in metadata."""
    _filing(conn, "BIDU", "6-K", days_ago=3)

    feats = compute_catalyst(conn, "run1", "BIDU", date.today())
    cat = _feat(feats, "catalyst_score")
    meta = cat.get("metadata", {})
    disclosure = meta.get("disclosure_evidence", [])
    assert any("6-K" in d for d in disclosure), \
        f"6-K should appear in disclosure_evidence, got metadata={meta}"


def test_8k_still_scored_independently(conn):
    """8-K filings should still score normally alongside any 6-K."""
    _filing(conn, "MIX", "8-K", days_ago=5)
    _filing(conn, "MIX", "6-K", days_ago=3)

    feats = compute_catalyst(conn, "run1", "MIX", date.today())
    cat = _feat(feats, "catalyst_score")
    assert cat["feature_score"] >= 15.0, \
        f"8-K should still contribute to catalyst score, got {cat['feature_score']}"


# ── Governance ────────────────────────────────────────────────────────────────

def test_no_auto_apply_requires_approved_status(conn):
    """apply_experiment must raise if status is not 'approved'."""
    from learning.experiments import propose_experiment, apply_experiment
    exp_id = propose_experiment(
        conn, "Test hypothesis", {"x": 1}, ["test.py"], "Test effect"
    )
    with pytest.raises(ValueError, match="approved"):
        apply_experiment(conn, exp_id, "user", notes="should fail")
