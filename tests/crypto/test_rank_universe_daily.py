"""Tests for `crypto rank-universe-daily` CLI / rank_universe_daily function.

Writes one row per symbol per day to crypto_universe_ranking_buffer for
the top 100 eligible USDT-perp pairs (so hysteresis can see the cutoff
neighborhood — ranks 1-100), with in_top_50 = (rank <= 50). Idempotent on
(symbol, ranking_date). Does NOT touch crypto_universe.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from crypto.ingestion import binance_client
from crypto.ingestion.rank_universe import rank_universe_daily
from crypto.schema import create_all_tables


def _setup(monkeypatch, symbols, avg30d):
    monkeypatch.setattr(
        binance_client.BinanceClient,
        "fetch_futures_exchange_info",
        lambda self: symbols,
    )
    monkeypatch.setattr(
        binance_client.BinanceClient,
        "fetch_30d_avg_quote_volume",
        lambda self, symbol: avg30d.get(symbol),
    )


def _mk_symbols(n):
    """n synthetic perp symbols ranked by trailing volume."""
    syms = [{"symbol": f"COIN{i:03d}USDT", "base_asset": f"COIN{i:03d}"} for i in range(n)]
    # COIN000 has highest volume; volume decreases monotonically
    avg30d = {s["symbol"]: float((n - i) * 1_000_000) for i, s in enumerate(syms)}
    return syms, avg30d


# ---------- A. Writes top 100 rows with in_top_50 flag ----------

def test_writes_top_100_rows_with_in_top_50_flag(monkeypatch):
    """With >100 eligible coins, top 100 get one row each on the ranking_date,
    in_top_50=True for ranks 1-50, False for ranks 51-100."""
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)

    symbols, avg30d = _mk_symbols(120)
    _setup(monkeypatch, symbols, avg30d)

    n_written = rank_universe_daily(conn, ranking_date=date(2026, 5, 16), top_n=100)

    assert n_written == 100

    rows = conn.execute(
        "SELECT symbol, ranking_date, rank_by_volume, in_top_50 "
        "FROM crypto_universe_ranking_buffer "
        "WHERE ranking_date = '2026-05-16' "
        "ORDER BY rank_by_volume"
    ).fetchall()
    assert len(rows) == 100

    # Ranks 1-50: in_top_50 = True
    for r in rows[:50]:
        assert r[3] is True, f"rank {r[2]} should be in_top_50=True; got {r[3]}"
    # Ranks 51-100: in_top_50 = False
    for r in rows[50:]:
        assert r[3] is False, f"rank {r[2]} should be in_top_50=False; got {r[3]}"

    # No write to crypto_universe — that table is the daily-rebuild target.
    universe_rows = conn.execute("SELECT COUNT(*) FROM crypto_universe").fetchone()
    assert universe_rows[0] == 0
    conn.close()


def test_avg_daily_volume_30d_persisted(monkeypatch):
    """The 30-day average is persisted alongside the rank for audit/debug."""
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)

    symbols = [
        {"symbol": "BTCUSDT", "base_asset": "BTC"},
        {"symbol": "ETHUSDT", "base_asset": "ETH"},
    ]
    avg30d = {"BTCUSDT": 5_000_000_000, "ETHUSDT": 3_000_000_000}
    _setup(monkeypatch, symbols, avg30d)

    rank_universe_daily(conn, ranking_date=date(2026, 5, 16))

    rows = conn.execute(
        "SELECT symbol, avg_daily_volume_30d FROM crypto_universe_ranking_buffer "
        "ORDER BY rank_by_volume"
    ).fetchall()
    assert rows[0] == ("BTCUSDT", 5_000_000_000)
    assert rows[1] == ("ETHUSDT", 3_000_000_000)
    conn.close()


# ---------- B. Idempotency ----------

def test_idempotent_overwrites_same_date(monkeypatch):
    """Re-running for the same ranking_date overwrites prior rows; no duplicates."""
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)

    symbols = [
        {"symbol": "BTCUSDT", "base_asset": "BTC"},
        {"symbol": "ETHUSDT", "base_asset": "ETH"},
        {"symbol": "SOLUSDT", "base_asset": "SOL"},
    ]
    _setup(monkeypatch, symbols, {"BTCUSDT": 5e9, "ETHUSDT": 3e9, "SOLUSDT": 2e9})

    rank_universe_daily(conn, ranking_date=date(2026, 5, 16))
    rank_universe_daily(conn, ranking_date=date(2026, 5, 16))  # same date again

    rows = conn.execute(
        "SELECT COUNT(*) FROM crypto_universe_ranking_buffer "
        "WHERE ranking_date = '2026-05-16'"
    ).fetchone()
    assert rows[0] == 3  # not 6

    # PK violation check via DISTINCT
    distinct = conn.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT symbol, ranking_date "
        "FROM crypto_universe_ranking_buffer)"
    ).fetchone()
    assert distinct[0] == 3
    conn.close()


def test_overwrite_uses_new_ranking(monkeypatch):
    """Second run for the same date with different inputs reflects the new
    ranking — not the old one — confirming DELETE-then-INSERT semantics."""
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)

    symbols = [
        {"symbol": "BTCUSDT", "base_asset": "BTC"},
        {"symbol": "ETHUSDT", "base_asset": "ETH"},
    ]
    # First run: BTC > ETH
    _setup(monkeypatch, symbols, {"BTCUSDT": 5e9, "ETHUSDT": 3e9})
    rank_universe_daily(conn, ranking_date=date(2026, 5, 16))

    # Second run, same date: ETH > BTC
    _setup(monkeypatch, symbols, {"BTCUSDT": 3e9, "ETHUSDT": 5e9})
    rank_universe_daily(conn, ranking_date=date(2026, 5, 16))

    top1 = conn.execute(
        "SELECT symbol FROM crypto_universe_ranking_buffer "
        "WHERE ranking_date = '2026-05-16' AND rank_by_volume = 1"
    ).fetchone()
    assert top1[0] == "ETHUSDT"
    conn.close()


# ---------- C. Exclusions ----------

def test_excludes_stablecoins_wrapped_and_non_ascii(monkeypatch, caplog):
    """Same exclusion set as build_universe: STABLECOIN_EXCLUDE, WRAPPED_EXCLUDE,
    and the safe-symbol guard (CJK / lowercase / hyphen → rejected)."""
    import logging
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)

    symbols = [
        {"symbol": "BTCUSDT", "base_asset": "BTC"},
        {"symbol": "USDCUSDT", "base_asset": "USDC"},        # stablecoin
        {"symbol": "WBTCUSDT", "base_asset": "WBTC"},        # wrapped
        {"symbol": "币安人生USDT", "base_asset": "币安人生"},   # CJK
        {"symbol": "ETHUSDT", "base_asset": "ETH"},
    ]
    avg30d = {
        "BTCUSDT": 5e9,
        "USDCUSDT": 9e9,        # would rank #1 if included
        "WBTCUSDT": 8e9,
        "币安人生USDT": 7e9,
        "ETHUSDT": 3e9,
    }
    _setup(monkeypatch, symbols, avg30d)

    with caplog.at_level(logging.WARNING, logger="mhde.crypto.universe"):
        rank_universe_daily(conn, ranking_date=date(2026, 5, 16))

    rows = conn.execute(
        "SELECT symbol FROM crypto_universe_ranking_buffer "
        "WHERE ranking_date = '2026-05-16'"
    ).fetchall()
    written = {r[0] for r in rows}

    assert written == {"BTCUSDT", "ETHUSDT"}
    # CJK rejection produced a warning
    assert any("币安人生USDT" in r.message for r in caplog.records)
    conn.close()


# ---------- D. --date override ----------

def test_ranking_date_override(monkeypatch):
    """Caller can specify a non-today ranking_date (used by backfill)."""
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)

    symbols = [{"symbol": "BTCUSDT", "base_asset": "BTC"}]
    _setup(monkeypatch, symbols, {"BTCUSDT": 5e9})

    rank_universe_daily(conn, ranking_date=date(2026, 5, 10))

    rows = conn.execute(
        "SELECT ranking_date FROM crypto_universe_ranking_buffer"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == date(2026, 5, 10)
    conn.close()


def test_default_ranking_date_is_today(monkeypatch):
    """If ranking_date is None, defaults to today (UTC date)."""
    from datetime import datetime, timezone
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)

    symbols = [{"symbol": "BTCUSDT", "base_asset": "BTC"}]
    _setup(monkeypatch, symbols, {"BTCUSDT": 5e9})

    today_utc = datetime.now(tz=timezone.utc).date()
    rank_universe_daily(conn)  # no date arg

    rows = conn.execute(
        "SELECT ranking_date FROM crypto_universe_ranking_buffer"
    ).fetchall()
    assert rows[0][0] == today_utc
    conn.close()


# ---------- E. Transactional safety ----------

def test_api_failure_midway_leaves_no_partial_state(monkeypatch):
    """If Binance raises mid-fetch, the DB has no rows for that date.
    Pre-existing rows for the same date are not touched until success
    (atomic replace, not pre-emptive delete)."""
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)

    # Seed: one prior good run for the same date.
    symbols = [{"symbol": "BTCUSDT", "base_asset": "BTC"},
               {"symbol": "ETHUSDT", "base_asset": "ETH"}]
    _setup(monkeypatch, symbols, {"BTCUSDT": 5e9, "ETHUSDT": 3e9})
    rank_universe_daily(conn, ranking_date=date(2026, 5, 16))

    before = conn.execute(
        "SELECT COUNT(*) FROM crypto_universe_ranking_buffer "
        "WHERE ranking_date = '2026-05-16'"
    ).fetchone()[0]
    assert before == 2

    # Now arrange a failing run for the SAME date.
    call_count = {"n": 0}

    def flaky(self, symbol):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise RuntimeError("Binance unavailable")
        return 5e9

    monkeypatch.setattr(
        binance_client.BinanceClient, "fetch_30d_avg_quote_volume", flaky
    )
    monkeypatch.setattr(
        binance_client.BinanceClient,
        "fetch_futures_exchange_info",
        lambda self: symbols,
    )

    with pytest.raises(RuntimeError, match="Binance unavailable"):
        rank_universe_daily(conn, ranking_date=date(2026, 5, 16))

    # The prior good rows must still be present — failed run did not partially
    # overwrite them.
    after = conn.execute(
        "SELECT symbol FROM crypto_universe_ranking_buffer "
        "WHERE ranking_date = '2026-05-16' ORDER BY symbol"
    ).fetchall()
    assert [r[0] for r in after] == ["BTCUSDT", "ETHUSDT"]
    conn.close()


# ---------- F. CLI smoke ----------

def test_cli_command_registered():
    """`crypto rank-universe-daily` is a registered click sub-command."""
    from click.testing import CliRunner
    from main import crypto

    runner = CliRunner()
    result = runner.invoke(crypto, ["rank-universe-daily", "--help"])
    assert result.exit_code == 0
    assert "rank-universe-daily" in result.output or "Compute" in result.output
