"""Tests for `crypto backfill-universe-rankings` CLI / backfill function.

Iterates a date range and, for each historical date D, computes the
30-day average quote volume in the window [D-30, D] (Binance kline
endTime convention) per eligible USDT-perp symbol. Persists top-N to
crypto_universe_ranking_buffer with the correct ranking_date.

Per-date transaction; per-date failure is logged and the loop continues.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import duckdb
import pytest

from crypto.ingestion import binance_client
from crypto.ingestion.binance_client import BinanceClient
from crypto.ingestion.rank_universe import backfill_universe_rankings
from crypto.schema import create_all_tables


def _setup_symbols(monkeypatch, symbols):
    monkeypatch.setattr(
        BinanceClient,
        "fetch_futures_exchange_info",
        lambda self: symbols,
    )


# ---------- A. The critical point-in-time test (user-flagged for strict TDD) ----------

def test_fetch_30d_avg_quote_volume_at_uses_endTime_for_historical_date(monkeypatch):
    """The new helper must set the Binance klines endTime to the end of the
    target historical date so the 30-day window is [end_date - 30d, end_date],
    not the current window through 'now'.

    This is the load-bearing assertion of point-in-time backfill: if endTime
    isn't set per date, every backfilled day would have today's 30-day average,
    which would make hysteresis behavior identical across all dates (useless)."""
    client = BinanceClient()
    captured = {}

    def fake_get(self, url, params=None):
        captured["url"] = url
        captured["params"] = params
        # Three 1d klines — quote-volume field [7] = 1M / 2M / 3M -> avg 2M.
        return [
            [0, "0", "0", "0", "0", "0", 0, "1000000", 0, "0", "0", "0"],
            [0, "0", "0", "0", "0", "0", 0, "2000000", 0, "0", "0", "0"],
            [0, "0", "0", "0", "0", "0", 0, "3000000", 0, "0", "0", "0"],
        ]

    monkeypatch.setattr(BinanceClient, "_get", fake_get)

    target = date(2026, 5, 10)
    avg = client.fetch_30d_avg_quote_volume_at("BTCUSDT", target)
    assert avg == 2_000_000.0

    # The end of `target` (UTC) is target + 1 day at 00:00 UTC, or close to it.
    expected_end_dt = datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc)
    expected_end_ms = int(expected_end_dt.timestamp() * 1000)

    params = captured["params"]
    assert params["symbol"] == "BTCUSDT"
    assert params["interval"] == "1d"
    # endTime must be within 1 day of expected (we allow either end-of-day
    # or start-of-next-day conventions).
    assert abs(params["endTime"] - expected_end_ms) <= 86_400_000, (
        f"endTime {params['endTime']} not within 1d of {expected_end_ms} "
        f"for end_date={target}"
    )
    # startTime must be ~30 days before endTime.
    span_days = (params["endTime"] - params["startTime"]) / 1000 / 86400
    assert 28 <= span_days <= 35, f"window {span_days}d not in [28, 35]"


def test_fetch_30d_avg_quote_volume_at_different_dates_send_different_endTimes(monkeypatch):
    """Two consecutive backfill dates must produce two different endTime
    values — proving the window slides per date, not stuck on 'now'."""
    client = BinanceClient()
    seen_end_times = []

    def fake_get(self, url, params=None):
        seen_end_times.append(params["endTime"])
        return [[0, "0", "0", "0", "0", "0", 0, "1000", 0, "0", "0", "0"]]

    monkeypatch.setattr(BinanceClient, "_get", fake_get)

    client.fetch_30d_avg_quote_volume_at("BTCUSDT", date(2026, 5, 10))
    client.fetch_30d_avg_quote_volume_at("BTCUSDT", date(2026, 5, 11))

    assert seen_end_times[0] != seen_end_times[1]
    # The later date's endTime should be ~1 day after the earlier one.
    diff_days = (seen_end_times[1] - seen_end_times[0]) / 1000 / 86400
    assert 0.5 <= diff_days <= 1.5


def test_fetch_30d_avg_quote_volume_at_returns_none_on_empty(monkeypatch):
    monkeypatch.setattr(BinanceClient, "_get", lambda self, url, params=None: [])
    avg = BinanceClient().fetch_30d_avg_quote_volume_at("DEADUSDT", date(2026, 5, 10))
    assert avg is None


# ---------- B. Backfill function ----------

def test_backfill_writes_one_set_per_date_in_range(monkeypatch):
    """Backfill from D1 to D3 writes 3 separate ranking_date sets — one per
    day — with the correct ranking_date stamped on each row."""
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)

    symbols = [
        {"symbol": "BTCUSDT", "base_asset": "BTC"},
        {"symbol": "ETHUSDT", "base_asset": "ETH"},
    ]
    _setup_symbols(monkeypatch, symbols)

    # Mock the point-in-time fetcher to return distinct values per date so
    # we can verify the ranking reflects per-date inputs.
    def fake_at(self, symbol, end_date, days=30):
        # Encode (symbol, day) into the volume so we can audit
        if symbol == "BTCUSDT":
            return 5_000_000_000 + end_date.day
        return 3_000_000_000 + end_date.day

    monkeypatch.setattr(BinanceClient, "fetch_30d_avg_quote_volume_at", fake_at)

    backfill_universe_rankings(conn, date(2026, 5, 10), date(2026, 5, 12), top_n=10)

    rows = conn.execute(
        "SELECT ranking_date, symbol, rank_by_volume, avg_daily_volume_30d "
        "FROM crypto_universe_ranking_buffer "
        "ORDER BY ranking_date, rank_by_volume"
    ).fetchall()
    # 3 dates * 2 symbols = 6 rows
    assert len(rows) == 6
    dates_seen = {r[0] for r in rows}
    assert dates_seen == {date(2026, 5, 10), date(2026, 5, 11), date(2026, 5, 12)}
    # For each date, BTC must be rank #1 (higher volume) and value reflects the day
    for d in sorted(dates_seen):
        btc = conn.execute(
            "SELECT rank_by_volume, avg_daily_volume_30d FROM crypto_universe_ranking_buffer "
            "WHERE ranking_date = ? AND symbol = 'BTCUSDT'", [d]
        ).fetchone()
        assert btc[0] == 1
        assert btc[1] == 5_000_000_000 + d.day
    conn.close()


def test_backfill_idempotent_overwrites_per_date(monkeypatch):
    """Running backfill twice over the same range overwrites each date's rows
    rather than duplicating or merging."""
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)

    symbols = [{"symbol": "BTCUSDT", "base_asset": "BTC"}]
    _setup_symbols(monkeypatch, symbols)
    monkeypatch.setattr(
        BinanceClient,
        "fetch_30d_avg_quote_volume_at",
        lambda self, s, d, days=30: 5e9,
    )

    backfill_universe_rankings(conn, date(2026, 5, 10), date(2026, 5, 12))
    backfill_universe_rankings(conn, date(2026, 5, 10), date(2026, 5, 12))

    n = conn.execute(
        "SELECT COUNT(*) FROM crypto_universe_ranking_buffer"
    ).fetchone()[0]
    # 3 days * 1 symbol = 3 rows (NOT 6)
    assert n == 3
    conn.close()


def test_backfill_continues_after_single_date_api_failure(monkeypatch, caplog):
    """If Binance fails on one date in the range, the backfill logs the error
    and continues. Other dates land normally; the failed date has zero rows."""
    import logging
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)

    symbols = [{"symbol": "BTCUSDT", "base_asset": "BTC"}]
    _setup_symbols(monkeypatch, symbols)

    def flaky(self, symbol, end_date, days=30):
        if end_date == date(2026, 5, 11):
            raise RuntimeError("Binance down on 5-11")
        return 5e9

    monkeypatch.setattr(BinanceClient, "fetch_30d_avg_quote_volume_at", flaky)

    with caplog.at_level(logging.ERROR, logger="mhde.crypto.universe"):
        result = backfill_universe_rankings(conn, date(2026, 5, 10), date(2026, 5, 12))

    rows = conn.execute(
        "SELECT ranking_date FROM crypto_universe_ranking_buffer ORDER BY ranking_date"
    ).fetchall()
    dates_written = {r[0] for r in rows}
    assert dates_written == {date(2026, 5, 10), date(2026, 5, 12)}   # NOT 5-11
    # Error was logged naming the date
    msgs = " ".join(r.message for r in caplog.records)
    assert "2026-05-11" in msgs
    # Return value flags the failed date
    assert result.get(date(2026, 5, 11), 0) == -1
    conn.close()


def test_backfill_respects_exclusions(monkeypatch):
    """Same exclusion set as rank_universe_daily: stablecoins, wrapped, symbol guard."""
    import logging
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)

    symbols = [
        {"symbol": "BTCUSDT", "base_asset": "BTC"},
        {"symbol": "USDCUSDT", "base_asset": "USDC"},
        {"symbol": "WBTCUSDT", "base_asset": "WBTC"},
        {"symbol": "币安人生USDT", "base_asset": "币安人生"},
        {"symbol": "ETHUSDT", "base_asset": "ETH"},
    ]
    _setup_symbols(monkeypatch, symbols)
    monkeypatch.setattr(
        BinanceClient,
        "fetch_30d_avg_quote_volume_at",
        lambda self, s, d, days=30: {
            "BTCUSDT": 5e9, "USDCUSDT": 9e9, "WBTCUSDT": 8e9,
            "币安人生USDT": 7e9, "ETHUSDT": 3e9,
        }.get(s),
    )

    backfill_universe_rankings(conn, date(2026, 5, 10), date(2026, 5, 10))

    rows = conn.execute("SELECT DISTINCT symbol FROM crypto_universe_ranking_buffer").fetchall()
    syms = {r[0] for r in rows}
    assert syms == {"BTCUSDT", "ETHUSDT"}
    conn.close()


def test_cli_backfill_command_registered():
    """`crypto backfill-universe-rankings` is a registered click sub-command."""
    from click.testing import CliRunner
    from main import crypto

    runner = CliRunner()
    result = runner.invoke(crypto, ["backfill-universe-rankings", "--help"])
    assert result.exit_code == 0
    assert "start-date" in result.output
    assert "end-date" in result.output
