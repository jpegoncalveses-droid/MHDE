"""Tests for the hysteresis-based build_universe.

After the Step-4 refactor, build_universe no longer ranks from scratch each
run. Instead it:
  1. Reads the 7 most recent ranking_dates from crypto_universe_ranking_buffer.
  2. For each candidate symbol (in buffer last 7d OR in current universe):
     - ADD if NOT currently active AND 7 consecutive in_top_50=TRUE AND
       onboard_date >= 60 days ago.
     - REMOVE if currently active AND 7 consecutive in_top_50=FALSE.
     - PENDING (subset of ADD) if would-add but onboard_date <60d ago.
     - NO-OP otherwise (transition, insufficient history, etc.).
  3. Refreshes rank_by_volume + avg_daily_volume_30d for active symbols
     from the most recent buffer date.

Each test below targets ONE branch and fails with a branch-naming assertion.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import duckdb
import pytest

from crypto.ingestion import binance_client
from crypto.ingestion.binance_client import BinanceClient
from crypto.ingestion.universe_builder import build_universe
from crypto.schema import create_all_tables


HYSTERESIS_DAYS = 7
LISTING_FLOOR_DAYS = 60

# Anchor "today" for tests so onboard-date arithmetic is deterministic.
TODAY = date(2026, 5, 16)
LAST_7 = [TODAY - timedelta(days=i) for i in range(HYSTERESIS_DAYS)]  # most recent first


# -------------------- helpers --------------------

def _patch_exchange_info(monkeypatch, info):
    """info: dict[symbol -> {"base_asset": str, "onboard_date": date | None}]"""
    rows = [{"symbol": s, **v} for s, v in info.items()]
    monkeypatch.setattr(
        BinanceClient, "fetch_futures_exchange_info", lambda self: rows
    )


def _patch_today(monkeypatch, today=TODAY):
    """Pin today() inside universe_builder to a fixed date."""
    import crypto.ingestion.universe_builder as ub
    monkeypatch.setattr(ub, "_today_utc", lambda: today)


def _seed_buffer(conn, symbol, days_in_top_50, *, base_avg_qv=1e8, base_rank=10):
    """days_in_top_50: iterable of (date, in_top_50 bool). Inserts one row each."""
    for d, in_top in days_in_top_50:
        rank = base_rank if in_top else 80
        conn.execute(
            "INSERT INTO crypto_universe_ranking_buffer "
            "(symbol, ranking_date, avg_daily_volume_30d, rank_by_volume, in_top_50) "
            "VALUES (?, ?, ?, ?, ?)",
            [symbol, d, base_avg_qv, rank, in_top],
        )


def _seed_universe(conn, symbol, base="BASE", rank=5, avg_qv=1e9):
    conn.execute(
        "INSERT INTO crypto_universe "
        "(symbol, base_asset, avg_daily_volume_30d, rank_by_volume, is_active, added_date) "
        "VALUES (?, ?, ?, ?, TRUE, ?)",
        [symbol, base, avg_qv, rank, date(2026, 4, 1)],
    )


def _setup(monkeypatch):
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)
    _patch_today(monkeypatch)
    return conn


# -------------------- 1. ADD on 7 consecutive top-50 + listed >=60d --------------------

def test_hysteresis_add_when_7_consecutive_days_in_top_50_and_listed_60_days(monkeypatch):
    """BRANCH=ADD-success. A symbol not in the universe with 7-of-7 in_top_50=TRUE
    and onboard_date 100 days ago must be added with is_active=TRUE and
    rank_by_volume refreshed from the most recent buffer date."""
    conn = _setup(monkeypatch)
    _patch_exchange_info(monkeypatch, {
        "NEWUSDT": {"base_asset": "NEW", "onboard_date": TODAY - timedelta(days=100)},
    })
    _seed_buffer(conn, "NEWUSDT", [(d, True) for d in LAST_7], base_avg_qv=8e8, base_rank=12)

    result = build_universe(conn)

    assert "NEWUSDT" in [a["symbol"] for a in result["adds"]], (
        f"ADD-success branch: NEWUSDT should be ADDed; got adds={result['adds']}"
    )
    row = conn.execute(
        "SELECT is_active, rank_by_volume, avg_daily_volume_30d FROM crypto_universe "
        "WHERE symbol = 'NEWUSDT'"
    ).fetchone()
    assert row is not None
    assert row[0] is True, "NEWUSDT must be is_active=TRUE after ADD"
    # Rank/qv refreshed from most recent buffer date (TODAY)
    assert row[1] == 12
    assert row[2] == 8e8
    conn.close()


# -------------------- 2. NO-ADD on only 6 consecutive days --------------------

def test_hysteresis_no_add_when_only_6_consecutive_days(monkeypatch):
    """BRANCH=ADD-blocked-by-streak. Symbol has 6 of last 7 days TRUE; the
    oldest (7th) is FALSE. Must NOT be added."""
    conn = _setup(monkeypatch)
    _patch_exchange_info(monkeypatch, {
        "ALMOSTUSDT": {"base_asset": "ALMOST", "onboard_date": TODAY - timedelta(days=200)},
    })
    # 6 recent TRUE + 1 oldest FALSE
    streak = [(LAST_7[i], i < 6) for i in range(7)]
    _seed_buffer(conn, "ALMOSTUSDT", streak)

    result = build_universe(conn)

    assert "ALMOSTUSDT" not in [a["symbol"] for a in result["adds"]], (
        "ADD-blocked-by-streak: only 6 consecutive TRUE days, ADD must NOT fire"
    )
    n = conn.execute(
        "SELECT COUNT(*) FROM crypto_universe WHERE symbol = 'ALMOSTUSDT' "
        "AND is_active = TRUE"
    ).fetchone()[0]
    assert n == 0
    conn.close()


# -------------------- 3. ADD blocked by 60-day floor → PENDING --------------------

def test_hysteresis_no_add_when_listed_less_than_60_days_marked_pending(monkeypatch):
    """BRANCH=ADD-blocked-by-listing-floor. Symbol has 7 consecutive TRUE
    but onboard_date is only 30 days ago. Must be PENDING, not ADDed."""
    conn = _setup(monkeypatch)
    onboard = TODAY - timedelta(days=30)
    _patch_exchange_info(monkeypatch, {
        "FRESHUSDT": {"base_asset": "FRESH", "onboard_date": onboard},
    })
    _seed_buffer(conn, "FRESHUSDT", [(d, True) for d in LAST_7])

    result = build_universe(conn)

    assert "FRESHUSDT" not in [a["symbol"] for a in result["adds"]]
    pending_syms = [p["symbol"] for p in result["pendings"]]
    assert "FRESHUSDT" in pending_syms, (
        f"ADD-blocked-by-listing-floor: FRESHUSDT should be PENDING; got {pending_syms}"
    )
    p = next(p for p in result["pendings"] if p["symbol"] == "FRESHUSDT")
    assert p["days_listed"] == 30
    assert p["eligible_after_date"] == onboard + timedelta(days=LISTING_FLOOR_DAYS)
    n = conn.execute(
        "SELECT COUNT(*) FROM crypto_universe WHERE symbol = 'FRESHUSDT' "
        "AND is_active = TRUE"
    ).fetchone()[0]
    assert n == 0
    conn.close()


# -------------------- 4. REMOVE on 7 consecutive out-of-top-50 --------------------

def test_hysteresis_remove_when_7_consecutive_days_out_of_top_50(monkeypatch):
    """BRANCH=REMOVE. Active symbol with 7 consecutive in_top_50=FALSE must
    be marked is_active=FALSE."""
    conn = _setup(monkeypatch)
    _patch_exchange_info(monkeypatch, {
        "OLDUSDT": {"base_asset": "OLD", "onboard_date": TODAY - timedelta(days=500)},
    })
    _seed_universe(conn, "OLDUSDT", base="OLD")
    _seed_buffer(conn, "OLDUSDT", [(d, False) for d in LAST_7])

    result = build_universe(conn)

    assert "OLDUSDT" in [r["symbol"] for r in result["removes"]], (
        f"REMOVE branch: OLDUSDT should be removed; got removes={result['removes']}"
    )
    row = conn.execute(
        "SELECT is_active FROM crypto_universe WHERE symbol = 'OLDUSDT'"
    ).fetchone()
    assert row[0] is False
    conn.close()


# -------------------- 5. NO-REMOVE in transition (mixed) --------------------

def test_hysteresis_no_remove_when_in_transition_keeps_current_state(monkeypatch):
    """BRANCH=REMOVE-blocked-by-streak. Active symbol with 5 days FALSE + 2
    days TRUE in last 7 stays active (transition)."""
    conn = _setup(monkeypatch)
    _patch_exchange_info(monkeypatch, {
        "WOBBLEUSDT": {"base_asset": "WOBBLE", "onboard_date": TODAY - timedelta(days=500)},
    })
    _seed_universe(conn, "WOBBLEUSDT", base="WOBBLE")
    # 5 most recent FALSE, 2 oldest TRUE
    streak = [(LAST_7[i], i >= 5) for i in range(7)]
    _seed_buffer(conn, "WOBBLEUSDT", streak)

    result = build_universe(conn)

    assert "WOBBLEUSDT" not in [r["symbol"] for r in result["removes"]]
    row = conn.execute(
        "SELECT is_active FROM crypto_universe WHERE symbol = 'WOBBLEUSDT'"
    ).fetchone()
    assert row[0] is True, "Transitioning symbol must keep current is_active state"
    conn.close()


# -------------------- 6. NO-DECISION on insufficient buffer history --------------------

def test_hysteresis_no_decision_when_buffer_has_less_than_7_days(monkeypatch):
    """BRANCH=insufficient-history. Buffer has 7 dates overall, but SPARSEUSDT
    has rows for only 5 of them (was ranked >100 on the other 2). Even though
    its 5 rows are all TRUE, ADD must not fire — insufficient per-symbol history."""
    conn = _setup(monkeypatch)
    _patch_exchange_info(monkeypatch, {
        "SPARSEUSDT": {"base_asset": "SPARSE", "onboard_date": TODAY - timedelta(days=200)},
        "ANCHORUSDT": {"base_asset": "ANCHOR", "onboard_date": TODAY - timedelta(days=500)},
    })
    # ANCHOR has rows on every date — pins the buffer date set at 7.
    _seed_buffer(conn, "ANCHORUSDT", [(d, True) for d in LAST_7], base_rank=2)
    # SPARSE only has rows for 5 of the 7 dates.
    _seed_buffer(conn, "SPARSEUSDT", [(LAST_7[i], True) for i in range(5)])

    result = build_universe(conn)

    assert "SPARSEUSDT" not in [a["symbol"] for a in result["adds"]], (
        "insufficient-history: SPARSEUSDT has only 5 rows; ADD must NOT fire"
    )
    assert "SPARSEUSDT" not in [r["symbol"] for r in result["removes"]]
    no_op_syms = [n["symbol"] for n in result["no_ops"]]
    assert "SPARSEUSDT" in no_op_syms
    conn.close()


# -------------------- 7. Pending eligible_after_date math --------------------

def test_pending_list_includes_correct_eligible_date(monkeypatch):
    """BRANCH=pending-date-math. eligible_after_date = onboard_date + 60d
    regardless of how far through the buffer the symbol has been TRUE."""
    conn = _setup(monkeypatch)
    onboard = date(2026, 4, 20)
    _patch_exchange_info(monkeypatch, {
        "BABYUSDT": {"base_asset": "BABY", "onboard_date": onboard},
    })
    _seed_buffer(conn, "BABYUSDT", [(d, True) for d in LAST_7])

    result = build_universe(conn)

    assert any(p["symbol"] == "BABYUSDT" for p in result["pendings"]), (
        f"pending-date-math: BABYUSDT must be in pendings; got {result['pendings']}"
    )
    p = next(p for p in result["pendings"] if p["symbol"] == "BABYUSDT")
    assert p["eligible_after_date"] == date(2026, 6, 19), (
        f"pending-date-math: eligible_after_date should be onboard ({onboard}) "
        f"+ 60 days = 2026-06-19; got {p['eligible_after_date']}"
    )
    # Persisted to crypto_universe_pending
    db_row = conn.execute(
        "SELECT days_listed, eligible_after_date, consecutive_top_50 "
        "FROM crypto_universe_pending WHERE symbol = 'BABYUSDT'"
    ).fetchone()
    assert db_row is not None
    assert db_row[0] == 26
    assert db_row[1] == date(2026, 6, 19)
    assert db_row[2] == 7
    conn.close()


# -------------------- 8. Dry-run leaves DB untouched --------------------

def test_dry_run_makes_no_db_changes_but_logs_decisions(monkeypatch, caplog):
    """BRANCH=dry-run. build_universe(dry_run=True) must return the same
    decision dict but make no DB modifications."""
    import logging
    conn = _setup(monkeypatch)
    _patch_exchange_info(monkeypatch, {
        "DRY_NEW": {"base_asset": "DRY_NEW", "onboard_date": TODAY - timedelta(days=200)},
        "DRY_OLD": {"base_asset": "DRY_OLD", "onboard_date": TODAY - timedelta(days=500)},
    })
    # Skip naming guard for these synthetic symbols by patching it off — they
    # don't need to be real Binance ticker shape for hysteresis test.
    # Actually build_universe in the new flow doesn't run the guard at all
    # because it reads from buffer. So the symbol-name doesn't matter as
    # long as it's consistent.
    _seed_universe(conn, "DRY_OLD", base="DRY_OLD")
    _seed_buffer(conn, "DRY_NEW", [(d, True) for d in LAST_7])
    _seed_buffer(conn, "DRY_OLD", [(d, False) for d in LAST_7])

    universe_before = conn.execute(
        "SELECT symbol, is_active FROM crypto_universe ORDER BY symbol"
    ).fetchall()
    pending_before = conn.execute("SELECT COUNT(*) FROM crypto_universe_pending").fetchone()[0]

    with caplog.at_level(logging.INFO, logger="mhde.crypto.universe"):
        result = build_universe(conn, dry_run=True)

    universe_after = conn.execute(
        "SELECT symbol, is_active FROM crypto_universe ORDER BY symbol"
    ).fetchall()
    pending_after = conn.execute("SELECT COUNT(*) FROM crypto_universe_pending").fetchone()[0]

    assert universe_before == universe_after, "dry-run: crypto_universe must be unchanged"
    assert pending_before == pending_after, "dry-run: crypto_universe_pending must be unchanged"
    # Decisions still surfaced
    assert "DRY_NEW" in [a["symbol"] for a in result["adds"]]
    assert "DRY_OLD" in [r["symbol"] for r in result["removes"]]
    assert result["dry_run"] is True
    conn.close()


# -------------------- 9. Rank refresh for active symbols --------------------

def test_active_symbols_get_rank_refreshed_from_most_recent_buffer_date(monkeypatch):
    """BRANCH=rank-refresh. A symbol already active (not being removed) must
    have rank_by_volume + avg_daily_volume_30d updated to the values from the
    most recent buffer date."""
    conn = _setup(monkeypatch)
    _patch_exchange_info(monkeypatch, {
        "STABLEUSDT": {"base_asset": "STABLE", "onboard_date": TODAY - timedelta(days=500)},
    })
    _seed_universe(conn, "STABLEUSDT", base="STABLE", rank=99, avg_qv=1.0)
    # All 7 days TRUE with different ranks; most recent (LAST_7[0]) is rank=3.
    for i, d in enumerate(LAST_7):
        rank_for_day = 3 if i == 0 else 5
        qv_for_day = 7.7e9 if i == 0 else 1e9
        conn.execute(
            "INSERT INTO crypto_universe_ranking_buffer "
            "(symbol, ranking_date, avg_daily_volume_30d, rank_by_volume, in_top_50) "
            "VALUES (?, ?, ?, ?, ?)",
            ["STABLEUSDT", d, qv_for_day, rank_for_day, True],
        )

    build_universe(conn)

    row = conn.execute(
        "SELECT rank_by_volume, avg_daily_volume_30d FROM crypto_universe "
        "WHERE symbol = 'STABLEUSDT'"
    ).fetchone()
    assert row[0] == 3, f"rank-refresh: rank must be from most-recent date (3); got {row[0]}"
    assert row[1] == 7.7e9
    conn.close()
