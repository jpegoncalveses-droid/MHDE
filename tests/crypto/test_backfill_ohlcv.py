"""Tests for crypto OHLCV/funding/OI backfill — the partial-candle ingestion fix.

Background: the daily ``mhde-crypto-predict`` timer runs at 00:30 UTC. The old
``backfill_ohlcv`` fetched klines through ``date.today()`` — i.e. the *in-progress*
UTC day — and inserted with ``ON CONFLICT DO NOTHING`` while only ever advancing
``fetch_start`` past the last stored date. Result: a ~30-minute partial candle was
written for "today" and never corrected. These tests pin the three-part fix:
(1) never request today's UTC date, (2) UPSERT so re-writes self-correct,
(3) re-fetch a trailing window of completed days every run.
"""
from datetime import date, datetime, timedelta

import duckdb

from crypto.config import INGESTION_LAG_DAYS, REFETCH_WINDOW_DAYS
from crypto.ingestion import backfill_funding as funding_mod
from crypto.ingestion import backfill_ohlcv as ohlcv_mod
from crypto.ingestion import backfill_oi as oi_mod
from crypto.ingestion import binance_client
from crypto.schema import create_all_tables

FIXED_TODAY = date(2026, 5, 15)
LAST_COMPLETE = FIXED_TODAY - timedelta(days=INGESTION_LAG_DAYS)


class _FixedDate(date):
    @classmethod
    def today(cls):
        return FIXED_TODAY


def _conn_with_universe(symbols=("FOOUSDT",)):
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)
    for rank, sym in enumerate(symbols, 1):
        conn.execute(
            "INSERT INTO crypto_universe (symbol, base_asset, avg_daily_volume_30d, "
            "rank_by_volume, is_active, added_date) VALUES (?, ?, ?, ?, true, ?)",
            [sym, sym.replace("USDT", ""), 1e9, rank, FIXED_TODAY],
        )
    return conn


def _kline(d, open_=1.0, high=2.0, low=0.5, close=1.5, volume=100.0, trades=10, taker=50.0):
    return {"trade_date": d, "open": open_, "high": high, "low": low, "close": close,
            "volume": volume, "trades": trades, "taker_buy_volume": taker}


def _seed_prices(conn, symbol, dates):
    for d in dates:
        conn.execute(
            "INSERT INTO crypto_prices_daily (symbol, trade_date, open, high, low, close, "
            "volume, trades, taker_buy_volume) VALUES (?, ?, 1, 1, 1, 1, 1, 1, 1)", [symbol, d])


# --- Test 5: never requests today's UTC date ---
def test_backfill_ohlcv_never_requests_today(monkeypatch):
    monkeypatch.setattr(ohlcv_mod, "date", _FixedDate)
    captured = {}

    def fake_fetch(self, symbol, start_date=None, end_date=None, futures=True):
        captured["start_date"], captured["end_date"] = start_date, end_date
        return []

    monkeypatch.setattr(binance_client.BinanceClient, "fetch_daily_klines", fake_fetch)
    ohlcv_mod.backfill_ohlcv(_conn_with_universe())

    assert captured["end_date"] < FIXED_TODAY
    assert captured["end_date"] == FIXED_TODAY - timedelta(days=INGESTION_LAG_DAYS)


# --- Test 8: re-fetch window spans the trailing REFETCH_WINDOW_DAYS completed days ---
def test_backfill_ohlcv_refetch_window_spans_trailing_days(monkeypatch):
    monkeypatch.setattr(ohlcv_mod, "date", _FixedDate)
    conn = _conn_with_universe()
    _seed_prices(conn, "FOOUSDT", [LAST_COMPLETE - timedelta(days=off) for off in range(6, -1, -1)])
    captured = {}

    def fake_fetch(self, symbol, start_date=None, end_date=None, futures=True):
        captured["start_date"], captured["end_date"] = start_date, end_date
        return []

    monkeypatch.setattr(binance_client.BinanceClient, "fetch_daily_klines", fake_fetch)
    ohlcv_mod.backfill_ohlcv(conn)

    assert captured["end_date"] == LAST_COMPLETE
    assert captured["start_date"] == LAST_COMPLETE - timedelta(days=REFETCH_WINDOW_DAYS - 1)
    assert (captured["end_date"] - captured["start_date"]).days == REFETCH_WINDOW_DAYS - 1


# --- Test 6: UPSERT overwrites an existing row when re-run with new values for same PK ---
def test_backfill_ohlcv_upsert_overwrites_on_rerun(monkeypatch):
    monkeypatch.setattr(ohlcv_mod, "date", _FixedDate)
    conn = _conn_with_universe()
    d = LAST_COMPLETE

    monkeypatch.setattr(
        binance_client.BinanceClient, "fetch_daily_klines",
        lambda self, symbol, start_date=None, end_date=None, futures=True: [_kline(d, close=1.0, volume=10.0)])
    ohlcv_mod.backfill_ohlcv(conn)
    assert conn.execute(
        "SELECT close, volume FROM crypto_prices_daily WHERE symbol='FOOUSDT' AND trade_date=?", [d]
    ).fetchone() == (1.0, 10.0)

    monkeypatch.setattr(
        binance_client.BinanceClient, "fetch_daily_klines",
        lambda self, symbol, start_date=None, end_date=None, futures=True: [_kline(d, close=2.0, volume=999.0)])
    ohlcv_mod.backfill_ohlcv(conn)
    assert conn.execute(
        "SELECT close, volume FROM crypto_prices_daily WHERE symbol='FOOUSDT' AND trade_date=?", [d]
    ).fetchone() == (2.0, 999.0)


# --- Test 7: a frozen partial candle (the old bug) self-heals on the next run ---
def test_backfill_ohlcv_partial_row_self_heals(monkeypatch):
    monkeypatch.setattr(ohlcv_mod, "date", _FixedDate)
    conn = _conn_with_universe()
    d = LAST_COMPLETE
    # simulate the OLD bug: a frozen ~30-minute partial candle written by a 00:30 run
    conn.execute(
        "INSERT INTO crypto_prices_daily (symbol, trade_date, open, high, low, close, volume, "
        "trades, taker_buy_volume) VALUES ('FOOUSDT', ?, 0.70767, 0.75756, 0.70341, 0.75132, "
        "9481801, 117050, 4000000)", [d])

    full = _kline(d, open_=0.70767, high=0.79799, low=0.54230, close=0.56941,
                  volume=287295338.0, trades=3985138, taker=140000000.0)
    monkeypatch.setattr(
        binance_client.BinanceClient, "fetch_daily_klines",
        lambda self, symbol, start_date=None, end_date=None, futures=True: [full])
    ohlcv_mod.backfill_ohlcv(conn)

    assert conn.execute(
        "SELECT open, high, low, close, volume, trades, taker_buy_volume FROM crypto_prices_daily "
        "WHERE symbol='FOOUSDT' AND trade_date=?", [d]
    ).fetchone() == (0.70767, 0.79799, 0.54230, 0.56941, 287295338.0, 3985138, 140000000.0)


# --- Audited path: funding-rate backfill now UPSERTs (was DO NOTHING) ---
def test_backfill_funding_upsert_overwrites(monkeypatch):
    conn = _conn_with_universe()
    ft = datetime(2026, 5, 14, 8, 0, 0)

    monkeypatch.setattr(
        binance_client.BinanceClient, "fetch_funding_rates",
        lambda self, symbol, start_date=None, end_date=None:
        [{"symbol": "FOOUSDT", "funding_time": ft, "funding_rate": 0.0001, "mark_price": 100.0}])
    funding_mod.backfill_funding(conn)
    assert conn.execute(
        "SELECT funding_rate, mark_price FROM crypto_funding_rates WHERE symbol='FOOUSDT'"
    ).fetchone() == (0.0001, 100.0)

    monkeypatch.setattr(
        binance_client.BinanceClient, "fetch_funding_rates",
        lambda self, symbol, start_date=None, end_date=None:
        [{"symbol": "FOOUSDT", "funding_time": ft, "funding_rate": 0.0005, "mark_price": 222.0}])
    funding_mod.backfill_funding(conn)
    assert conn.execute(
        "SELECT funding_rate, mark_price FROM crypto_funding_rates WHERE symbol='FOOUSDT'"
    ).fetchone() == (0.0005, 222.0)


# --- Audited path: open-interest backfill now UPSERTs (was DO NOTHING) ---
def test_backfill_oi_upsert_overwrites(monkeypatch):
    conn = _conn_with_universe()
    d = date(2026, 5, 14)

    monkeypatch.setattr(
        binance_client.BinanceClient, "fetch_open_interest_hist",
        lambda self, symbol, period="1d", limit=30:
        [{"symbol": "FOOUSDT", "trade_date": d, "open_interest": 10.0, "open_interest_value": 1000.0}])
    oi_mod.backfill_open_interest(conn)
    assert conn.execute(
        "SELECT open_interest, open_interest_value FROM crypto_open_interest WHERE symbol='FOOUSDT'"
    ).fetchone() == (10.0, 1000.0)

    monkeypatch.setattr(
        binance_client.BinanceClient, "fetch_open_interest_hist",
        lambda self, symbol, period="1d", limit=30:
        [{"symbol": "FOOUSDT", "trade_date": d, "open_interest": 77.0, "open_interest_value": 7777.0}])
    oi_mod.backfill_open_interest(conn)
    assert conn.execute(
        "SELECT open_interest, open_interest_value FROM crypto_open_interest WHERE symbol='FOOUSDT'"
    ).fetchone() == (77.0, 7777.0)
