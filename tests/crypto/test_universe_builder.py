"""Tests for crypto universe builder."""
import duckdb

from crypto.ingestion.universe_builder import build_universe
from crypto.ingestion import binance_client
from crypto.schema import create_all_tables


def test_build_universe_filters_and_ranks(monkeypatch):
    """Verify stablecoins excluded, ranking by volume, top N selected."""
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)

    mock_symbols = [
        {"symbol": "BTCUSDT", "base_asset": "BTC"},
        {"symbol": "ETHUSDT", "base_asset": "ETH"},
        {"symbol": "USDCUSDT", "base_asset": "USDC"},
        {"symbol": "SOLUSDT", "base_asset": "SOL"},
    ]
    mock_tickers = [
        {"symbol": "BTCUSDT", "quote_volume": 5_000_000_000},
        {"symbol": "ETHUSDT", "quote_volume": 3_000_000_000},
        {"symbol": "USDCUSDT", "quote_volume": 1_000_000_000},
        {"symbol": "SOLUSDT", "quote_volume": 2_000_000_000},
    ]

    monkeypatch.setattr(binance_client.BinanceClient, "fetch_futures_exchange_info", lambda self: mock_symbols)
    monkeypatch.setattr(binance_client.BinanceClient, "fetch_24hr_tickers", lambda self: mock_tickers)

    result = build_universe(conn, top_n=3)

    assert len(result) == 3
    assert "USDCUSDT" not in result
    assert result[0] == "BTCUSDT"
    assert result[1] == "ETHUSDT"
    assert result[2] == "SOLUSDT"

    rows = conn.execute("SELECT symbol, rank_by_volume FROM crypto_universe ORDER BY rank_by_volume").fetchall()
    assert len(rows) == 3
    assert rows[0][0] == "BTCUSDT"
    assert rows[0][1] == 1
    conn.close()
