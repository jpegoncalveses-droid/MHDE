"""Build crypto universe from Binance perpetual futures by 30-day avg trading volume."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

import duckdb

from crypto.config import STABLECOIN_EXCLUDE, WRAPPED_EXCLUDE, UNIVERSE_SIZE
from crypto.ingestion.binance_client import BinanceClient
from crypto.schema import create_all_tables

logger = logging.getLogger("mhde.crypto.universe")


def _today_utc() -> date:
    """Indirection for tests; production returns UTC today."""
    return datetime.now(tz=timezone.utc).date()

# Reject anything that isn't uppercase ASCII letters/digits ending in USDT.
# Caught 2026-05-16 audit: a real Binance pair `币安人生USDT` (CJK) would
# otherwise be ranked into the top-50 and break downstream code that assumes
# uppercase-ASCII tickers (URL paths, model artifact filenames).
#
# The USDT suffix is hardcoded because this project is explicitly USDT-M
# Perpetual Futures only — see crypto/config.py STABLECOIN_EXCLUDE /
# WRAPPED_EXCLUDE (USDT-suffixed entries) and binance_client.py
# fetch_futures_exchange_info() filter on `quoteAsset == "USDT"`. Adding
# USDC-margined pairs would require: expanding this regex to allow USDC,
# extending fetch_futures_exchange_info() to include them, and confirming
# crypto/ml/features.py + crypto/ml/predict.py treat USDC-quoted prices
# consistently with the existing USDT-quoted feature pipeline.
_SAFE_SYMBOL_RE = re.compile(r"^[A-Z0-9]+USDT$")


def _is_safe_symbol(symbol: str) -> bool:
    return bool(_SAFE_SYMBOL_RE.match(symbol))


HYSTERESIS_DAYS = 7
LISTING_FLOOR_DAYS = 60


def build_universe(
    conn: duckdb.DuckDBPyConnection,
    dry_run: bool = False,
    hysteresis_days: int = HYSTERESIS_DAYS,
    listing_floor_days: int = LISTING_FLOOR_DAYS,
) -> dict:
    """Hysteresis-based daily rebuild of crypto_universe.

    Reads ``hysteresis_days`` most-recent ranking_dates from
    ``crypto_universe_ranking_buffer`` and applies:

    - ADD: candidate not currently active AND ALL ``hysteresis_days``
      most-recent ranking dates have ``in_top_50=TRUE`` AND coin has been
      listed on Binance perp for >= ``listing_floor_days``.
    - REMOVE: candidate currently active AND ALL ``hysteresis_days``
      most-recent ranking dates have ``in_top_50=FALSE``.
    - PENDING: same as ADD but coin's onboard_date is less than
      ``listing_floor_days`` ago; persisted to ``crypto_universe_pending``
      so the operator can see what's queued.
    - NO-OP: anything else (insufficient buffer history, mixed signal, etc.).

    Rank refresh: every currently-active symbol gets ``rank_by_volume`` and
    ``avg_daily_volume_30d`` refreshed from the most recent buffer date.

    If ``dry_run`` is True, returns the decision dict without modifying the DB.
    """
    create_all_tables(conn)
    today = _today_utc()

    # 1. Current active set
    current = {r[0] for r in conn.execute(
        "SELECT symbol FROM crypto_universe WHERE is_active = TRUE"
    ).fetchall()}

    # 2. Most recent N ranking_dates
    last_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT ranking_date FROM crypto_universe_ranking_buffer "
        "ORDER BY ranking_date DESC LIMIT ?", [hysteresis_days],
    ).fetchall()]
    if len(last_dates) < hysteresis_days:
        raise ValueError(
            f"crypto_universe_ranking_buffer has only {len(last_dates)} "
            f"distinct dates; need {hysteresis_days}. Run rank-universe-daily "
            f"(or backfill-universe-rankings) first."
        )
    latest_date = last_dates[0]

    # 3. Candidates: symbols in buffer for any of last N dates OR in current universe
    placeholders = ",".join("?" * len(last_dates))
    buffer_syms = {r[0] for r in conn.execute(
        f"SELECT DISTINCT symbol FROM crypto_universe_ranking_buffer "
        f"WHERE ranking_date IN ({placeholders})", last_dates,
    ).fetchall()}
    candidates = buffer_syms | current

    # 4. onboard_date lookup
    client = BinanceClient()
    perp_info = {s["symbol"]: s for s in client.fetch_futures_exchange_info()}

    # 5. Most-recent-date snapshot for rank refresh
    snapshot = {
        r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT symbol, rank_by_volume, avg_daily_volume_30d "
            "FROM crypto_universe_ranking_buffer WHERE ranking_date = ?",
            [latest_date],
        ).fetchall()
    }

    adds: list[dict] = []
    removes: list[dict] = []
    pendings: list[dict] = []
    no_ops: list[dict] = []

    for sym in sorted(candidates):
        is_active = sym in current
        history = conn.execute(
            f"SELECT in_top_50 FROM crypto_universe_ranking_buffer "
            f"WHERE symbol = ? AND ranking_date IN ({placeholders}) "
            f"ORDER BY ranking_date DESC",
            [sym, *last_dates],
        ).fetchall()
        if len(history) < hysteresis_days:
            no_ops.append({"symbol": sym, "reason": "insufficient_history",
                           "active": is_active, "rows": len(history)})
            continue

        in_top_seq = [h[0] for h in history]
        all_true = all(in_top_seq)
        all_false = not any(in_top_seq)
        # consecutive_top_50 from most recent date backwards
        consecutive_top_50 = 0
        for x in in_top_seq:
            if x:
                consecutive_top_50 += 1
            else:
                break

        if not is_active and all_true:
            info = perp_info.get(sym, {})
            base = info.get("base_asset", "?")
            onboard = info.get("onboard_date")
            if onboard is None:
                no_ops.append({"symbol": sym, "reason": "no_onboard_date",
                               "active": is_active})
                continue
            days_listed = (today - onboard).days
            if days_listed < listing_floor_days:
                eligible_after = onboard + timedelta(days=listing_floor_days)
                pendings.append({
                    "symbol": sym,
                    "days_listed": days_listed,
                    "eligible_after_date": eligible_after,
                    "consecutive_top_50": consecutive_top_50,
                })
                logger.info(
                    "PENDING: %s (%dd listed, eligible after %s, %dd in top-50)",
                    sym, days_listed, eligible_after, consecutive_top_50,
                )
            else:
                adds.append({"symbol": sym, "base": base,
                             "days_listed": days_listed})
        elif is_active and all_false:
            removes.append({"symbol": sym})
        else:
            no_ops.append({"symbol": sym, "reason": "no_signal",
                           "active": is_active,
                           "consecutive_top_50": consecutive_top_50})

    summary = {
        "adds": adds, "removes": removes, "pendings": pendings, "no_ops": no_ops,
        "kept_count": len(current) - len(removes),
        "latest_buffer_date": latest_date,
        "dry_run": dry_run,
    }

    if dry_run:
        return summary

    conn.execute("BEGIN")
    try:
        for a in adds:
            rank, qv = snapshot.get(a["symbol"], (None, None))
            conn.execute(
                """
                INSERT INTO crypto_universe
                    (symbol, base_asset, avg_daily_volume_30d, rank_by_volume,
                     is_active, added_date)
                VALUES (?, ?, ?, ?, TRUE, ?)
                ON CONFLICT (symbol) DO UPDATE SET
                    is_active = TRUE,
                    avg_daily_volume_30d = excluded.avg_daily_volume_30d,
                    rank_by_volume = excluded.rank_by_volume,
                    added_date = excluded.added_date,
                    removed_date = NULL
                """,
                [a["symbol"], a["base"], qv, rank, today],
            )
        for r in removes:
            conn.execute(
                "UPDATE crypto_universe SET is_active = FALSE, removed_date = ? "
                "WHERE symbol = ?",
                [today, r["symbol"]],
            )
        # Refresh rank for active symbols that appear in the most recent buffer
        for sym, (rank, qv) in snapshot.items():
            conn.execute(
                "UPDATE crypto_universe SET rank_by_volume = ?, "
                "avg_daily_volume_30d = ? WHERE symbol = ? AND is_active = TRUE",
                [rank, qv, sym],
            )
        # Persist pending list (truncate + insert)
        conn.execute("DELETE FROM crypto_universe_pending")
        for p in pendings:
            conn.execute(
                """
                INSERT INTO crypto_universe_pending
                    (symbol, days_listed, eligible_after_date,
                     consecutive_top_50, last_checked_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                [p["symbol"], p["days_listed"], p["eligible_after_date"],
                 p["consecutive_top_50"]],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    logger.info(
        "build_universe: %d adds, %d removes, %d pendings, %d no_ops (latest buffer date %s)",
        len(adds), len(removes), len(pendings), len(no_ops), latest_date,
    )
    return summary
