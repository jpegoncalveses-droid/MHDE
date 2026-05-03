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
