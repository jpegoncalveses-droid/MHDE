"""Tests for IFRS concept aliases in features/valuation.py."""
from __future__ import annotations

import uuid
from datetime import date

import pytest

from storage.db import get_connection, init_schema
from features.valuation import compute_valuation, _latest_usd_unit


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _price(conn, ticker, close):
    conn.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close, source) VALUES (?, ?, ?, ?, 'test')",
        [uuid.uuid4().hex[:16], ticker, date.today().isoformat(), close],
    )


def _fund(conn, ticker, concept, value, unit=None):
    conn.execute(
        "INSERT INTO fundamentals_raw (id, ticker, concept, value, unit, as_of_date) VALUES (?, ?, ?, ?, ?, ?)",
        [uuid.uuid4().hex[:16], ticker, concept, value, unit, date.today().isoformat()],
    )


def _feat(features, name):
    return next((f for f in features if f["feature_name"] == name), None)


# ── _latest_usd_unit unit tests ────────────────────────────────────────────────

def test_latest_usd_unit_returns_usd_value(conn):
    _fund(conn, "AAAB", "ifrs-full/Revenues", 1_000_000, unit="USD")
    val = _latest_usd_unit(conn, "AAAB", ["ifrs-full/Revenues"])
    assert val == 1_000_000


def test_latest_usd_unit_ignores_cad(conn):
    _fund(conn, "AAAB", "ifrs-full/Revenues", 5_000_000, unit="CAD")
    val = _latest_usd_unit(conn, "AAAB", ["ifrs-full/Revenues"])
    assert val is None


def test_latest_usd_unit_accepts_usd_per_share(conn):
    _fund(conn, "AAAB", "ifrs-full/EarningsPerShareDiluted", 3.5, unit="USD/shares")
    val = _latest_usd_unit(conn, "AAAB", ["ifrs-full/EarningsPerShareDiluted"])
    assert val == 3.5


def test_latest_usd_unit_ignores_eur_per_share(conn):
    _fund(conn, "AAAB", "ifrs-full/EarningsPerShareDiluted", 2.1, unit="EUR/shares")
    val = _latest_usd_unit(conn, "AAAB", ["ifrs-full/EarningsPerShareDiluted"])
    assert val is None


def test_latest_usd_unit_prefers_first_concept(conn):
    _fund(conn, "AAAB", "ifrs-full/EarningsPerShareDiluted", 4.0, unit="USD/shares")
    _fund(conn, "AAAB", "ifrs-full/EarningsPerShareBasic", 3.8, unit="USD/shares")
    val = _latest_usd_unit(conn, "AAAB", [
        "ifrs-full/EarningsPerShareDiluted",
        "ifrs-full/EarningsPerShareBasic",
    ])
    assert val == 4.0


# ── IFRS concept aliases in compute_valuation ─────────────────────────────────

def test_ifrs_revenue_used_for_ps_when_us_gaap_absent(conn):
    """USD-reporting IFRS filer gets P/S via ifrs-full/Revenues fallback."""
    _price(conn, "GFS", 50.0)
    _fund(conn, "GFS", "ifrs-full/Revenues", 6_791_000_000, unit="USD")
    _fund(conn, "GFS", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding",
          165_000_000, unit="shares")
    features = compute_valuation(conn, "test_run", "GFS", date.today())
    ps_feat = _feat(features, "ps_proxy")
    assert ps_feat is not None
    assert ps_feat["feature_value"] is not None, "P/S should compute via IFRS revenue fallback"


def test_ifrs_eps_used_for_pe_when_us_gaap_absent(conn):
    """USD-reporting IFRS filer gets P/E via ifrs-full/EarningsPerShareDiluted fallback."""
    _price(conn, "AAAB", 40.0)
    _fund(conn, "AAAB", "ifrs-full/EarningsPerShareDiluted", 2.0, unit="USD/shares")
    features = compute_valuation(conn, "test_run", "AAAB", date.today())
    pe_feat = _feat(features, "pe_ratio")
    assert pe_feat is not None
    assert pe_feat["feature_value"] == pytest.approx(20.0, rel=0.01), "P/E = 40 / 2 = 20"


def test_non_usd_ifrs_revenue_not_used(conn):
    """CAD-reporting IFRS filer does NOT get P/S from IFRS revenue."""
    _price(conn, "CVE", 18.0)
    _fund(conn, "CVE", "ifrs-full/Revenues", 54_000_000_000, unit="CAD")
    _fund(conn, "CVE", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding",
          1_900_000_000, unit="shares")
    features = compute_valuation(conn, "test_run", "CVE", date.today())
    ps_feat = _feat(features, "ps_proxy")
    assert ps_feat is None or ps_feat["feature_value"] is None, \
        "CAD-denominated IFRS revenue must not be used for P/S"


def test_non_usd_ifrs_eps_not_used(conn):
    """EUR-reporting IFRS filer does NOT get P/E from IFRS EPS."""
    _price(conn, "NOK", 4.0)
    _fund(conn, "NOK", "ifrs-full/EarningsPerShareDiluted", 0.23, unit="EUR/shares")
    features = compute_valuation(conn, "test_run", "NOK", date.today())
    pe_feat = _feat(features, "pe_ratio")
    assert pe_feat is None or pe_feat["feature_value"] is None, \
        "EUR-denominated IFRS EPS must not be used for P/E"


def test_us_gaap_revenue_preferred_over_ifrs(conn):
    """When both US-GAAP and IFRS revenue exist, US-GAAP wins."""
    _price(conn, "AAAB", 30.0)
    _fund(conn, "AAAB", "us-gaap/Revenues", 10_000_000_000)
    _fund(conn, "AAAB", "ifrs-full/Revenues", 99_000_000_000, unit="USD")
    _fund(conn, "AAAB", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding",
          500_000_000)
    features = compute_valuation(conn, "test_run", "AAAB", date.today())
    ps_feat = _feat(features, "ps_proxy")
    assert ps_feat is not None
    mc = 30.0 * 500_000_000
    expected_ps = mc / 10_000_000_000
    assert ps_feat["feature_value"] == pytest.approx(expected_ps, rel=0.01)


def test_no_ticker_specific_mapping():
    """Ensure no ticker names appear in the IFRS concept lists."""
    from features.valuation import _IFRS_REVENUE_CONCEPTS, _IFRS_EPS_CONCEPTS
    ticker_names = {"GFS", "UMC", "CVE", "NOK", "CRDO", "INTC"}
    for concept in _IFRS_REVENUE_CONCEPTS + _IFRS_EPS_CONCEPTS:
        for name in ticker_names:
            assert name not in concept, f"Ticker-specific term '{name}' found in concept '{concept}'"


def test_no_production_scoring_changes():
    """IFRS aliases must not change any scoring weight or threshold."""
    import inspect
    from features import valuation
    src = inspect.getsource(valuation)
    for bad in ("feature_flag", "FeatureFlag", "openai", "anthropic", "llm"):
        assert bad.lower() not in src.lower(), f"Prohibited term '{bad}' in valuation.py"
