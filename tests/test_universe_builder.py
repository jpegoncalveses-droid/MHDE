from __future__ import annotations

import yaml
import pytest
import duckdb

from storage.db import init_schema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_yaml(path, tickers):
    """Write a minimal sp500_tickers.yaml to path."""
    data = {
        "last_updated": "2026-05-03",
        "source": "test",
        "tickers": tickers,
    }
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False)


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    init_schema(c)
    yield c
    c.close()


def _cfg(max_symbols=20, fallback_tickers=None):
    return {
        "universe": {
            "max_symbols": max_symbols,
            "fallback_tickers": fallback_tickers or [],
            "exclude_etfs": True,
            "exclude_funds": True,
            "exclude_adrs": False,
        }
    }


def _sec_co(ticker, name="Corp"):
    return {"ticker": ticker, "cik": "0001234567", "company_name": f"{ticker} {name}"}


# ---------------------------------------------------------------------------
# Loader tests
# ---------------------------------------------------------------------------

def test_load_sp500_yaml_returns_list(tmp_path):
    from universe.sp500_loader import load_sp500_yaml
    f = tmp_path / "sp500.yaml"
    _write_yaml(f, [{"ticker": "AAPL", "company_name": "Apple Inc", "sector": "IT"}])
    result = load_sp500_yaml(f)
    assert result == [{"ticker": "AAPL", "company_name": "Apple Inc", "sector": "IT"}]


def test_load_sp500_yaml_missing_file_returns_empty(tmp_path):
    from universe.sp500_loader import load_sp500_yaml
    result = load_sp500_yaml(tmp_path / "nonexistent.yaml")
    assert result == []


# ---------------------------------------------------------------------------
# Builder behaviour tests (Task 4 will make these pass)
# ---------------------------------------------------------------------------

def test_yaml_tickers_load_as_primary(conn, monkeypatch):
    yaml_entries = [{"ticker": "FOO", "company_name": "Foo Inc", "sector": "Financials"}]
    sec = [_sec_co("BAR"), _sec_co("BAZ")]
    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: yaml_entries)
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: sec)
    from universe.universe_builder import build_universe
    build_universe(conn, _cfg())
    row = conn.execute(
        "SELECT universe_tier FROM companies WHERE ticker = 'FOO'"
    ).fetchone()
    assert row is not None and row[0] == "primary"


def test_config_fallback_tickers_preserved(conn, monkeypatch):
    yaml_entries = [{"ticker": "FOO", "company_name": "Foo Inc"}]
    sec = [_sec_co("BAR")]
    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: yaml_entries)
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: sec)
    from universe.universe_builder import build_universe
    build_universe(conn, _cfg(fallback_tickers=["BZZZ"]))
    row = conn.execute(
        "SELECT universe_tier FROM companies WHERE ticker = 'BZZZ'"
    ).fetchone()
    assert row is not None and row[0] == "primary"


def test_sec_fillers_load_as_extended(conn, monkeypatch):
    yaml_entries = [{"ticker": "FOO", "company_name": "Foo Inc"}]
    sec = [_sec_co("BAR"), _sec_co("BAZ")]
    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: yaml_entries)
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: sec)
    from universe.universe_builder import build_universe
    build_universe(conn, _cfg())
    row = conn.execute(
        "SELECT universe_tier FROM companies WHERE ticker = 'BAR'"
    ).fetchone()
    assert row is not None and row[0] == "extended"


def test_primary_tickers_not_truncated_by_max_symbols(conn, monkeypatch):
    """10 YAML primaries with max_symbols=5 — all 10 must survive."""
    yaml_entries = [
        {"ticker": f"Y{i:03d}", "company_name": f"Y{i} Inc"} for i in range(10)
    ]
    sec = [_sec_co(f"S{i:03d}") for i in range(5)]
    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: yaml_entries)
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: sec)
    from universe.universe_builder import build_universe
    build_universe(conn, _cfg(max_symbols=5))
    count = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE universe_tier = 'primary' AND is_active = true"
    ).fetchone()[0]
    assert count == 10


def test_sector_industry_populate_from_yaml(conn, monkeypatch):
    yaml_entries = [{
        "ticker": "FOO",
        "company_name": "Foo Inc",
        "sector": "Energy",
        "industry": "Oil & Gas Exploration & Production",
    }]
    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: yaml_entries)
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: [])
    from universe.universe_builder import build_universe
    build_universe(conn, _cfg())
    row = conn.execute(
        "SELECT sector, industry FROM companies WHERE ticker = 'FOO'"
    ).fetchone()
    assert row is not None
    assert row[0] == "Energy"
    assert row[1] == "Oil & Gas Exploration & Production"


def test_duplicate_tickers_deduped(conn, monkeypatch):
    yaml_entries = [
        {"ticker": "FOO", "company_name": "Foo Inc"},
        {"ticker": "FOO", "company_name": "Foo Duplicate"},
    ]
    sec = [_sec_co("FOO")]
    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: yaml_entries)
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: sec)
    from universe.universe_builder import build_universe
    build_universe(conn, _cfg())
    count = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE ticker = 'FOO'"
    ).fetchone()[0]
    assert count == 1


def test_removed_primary_ticker_deactivated(conn, monkeypatch):
    """FOO is primary on run 1. YAML is emptied on run 2. FOO must become inactive."""
    from universe.universe_builder import build_universe

    # Run 1: FOO in YAML
    monkeypatch.setattr(
        "universe.universe_builder.load_sp500_yaml",
        lambda p: [{"ticker": "FOO", "company_name": "Foo Inc"}],
    )
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: [])
    build_universe(conn, _cfg())
    assert conn.execute(
        "SELECT is_active FROM companies WHERE ticker = 'FOO'"
    ).fetchone()[0] is True

    # Run 2: YAML empty, config fallback empty
    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: [])
    build_universe(conn, _cfg())
    assert conn.execute(
        "SELECT is_active FROM companies WHERE ticker = 'FOO'"
    ).fetchone()[0] is False


def test_dot_ticker_bypasses_filter(conn, monkeypatch):
    """BRK.B is dropped by filter_non_equities (dot in ticker) but must be inserted
    when it comes from the YAML primary list, preserving its CIK."""
    yaml_entries = [{
        "ticker": "BRK.B",
        "company_name": "Berkshire Hathaway Inc",
        "sector": "Financials",
        "industry": "Multi-Sector Holdings",
        "cik": "0001067983",
    }]
    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: yaml_entries)
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: [])
    from universe.universe_builder import build_universe
    build_universe(conn, _cfg())
    row = conn.execute(
        "SELECT ticker, universe_tier, cik FROM companies WHERE ticker = 'BRK.B'"
    ).fetchone()
    assert row is not None, "BRK.B must be in companies table"
    assert row[1] == "primary"
    assert row[2] == "0001067983"
