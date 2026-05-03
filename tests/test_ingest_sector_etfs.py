"""Tests for sector ETF ingestion."""
from unittest.mock import patch

import pytest

from ingestion.ingest_sector_etfs import (
    ETF_TO_SECTOR,
    SECTOR_ETFS,
    get_sector_returns,
    ingest_sector_etfs_to_db,
)


def test_sector_etfs_is_tuple():
    assert isinstance(SECTOR_ETFS, tuple)


def test_sector_etfs_count():
    assert len(SECTOR_ETFS) == 11


def test_required_etfs_present():
    for etf in ("XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU", "XLB", "XLRE", "XLC", "XLY"):
        assert etf in SECTOR_ETFS


def test_etf_to_sector_map_complete():
    assert len(ETF_TO_SECTOR) == 11
    for etf in SECTOR_ETFS:
        assert etf in ETF_TO_SECTOR


def test_get_sector_returns_no_key():
    returns = get_sector_returns(date="2026-01-01", api_key=None)
    assert isinstance(returns, dict)
    assert len(returns) == 0


def test_get_sector_returns_mocked():
    mock_bar = {"results": [{"o": 100.0, "c": 102.5}]}
    with patch("ingestion.ingest_sector_etfs._fetch_etf_return", return_value=0.025):
        returns = get_sector_returns(date="2026-01-01", api_key="fake-key")
    assert isinstance(returns, dict)
    # All 11 ETFs should have a return value
    assert len(returns) == 11
    assert all(isinstance(v, float) for v in returns.values())


def test_ingest_sector_etfs_no_key(tmp_path):
    count = ingest_sector_etfs_to_db(
        db_path=str(tmp_path / "test.duckdb"), date="2026-01-01", api_key=None
    )
    assert count == 0


def test_ingest_sector_etfs_to_db(tmp_path):
    import duckdb

    db_path = str(tmp_path / "test.duckdb")
    conn = duckdb.connect(db_path)
    conn.execute("""
        CREATE TABLE prices_daily (
            ticker VARCHAR,
            trade_date DATE,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE,
            adjusted_close DOUBLE,
            source VARCHAR,
            PRIMARY KEY (ticker, trade_date)
        )
    """)
    conn.close()

    with patch(
        "ingestion.ingest_sector_etfs.get_sector_returns",
        return_value={"XLK": 0.025, "XLF": -0.01},
    ):
        count = ingest_sector_etfs_to_db(db_path=db_path, date="2026-01-01", api_key="fake")

    assert count == 2
    conn2 = duckdb.connect(db_path)
    rows = conn2.execute("SELECT ticker, adjusted_close FROM prices_daily ORDER BY ticker").fetchall()
    conn2.close()
    assert len(rows) == 2
    assert rows[0] == ("XLF", -0.01)
    assert rows[1] == ("XLK", 0.025)
