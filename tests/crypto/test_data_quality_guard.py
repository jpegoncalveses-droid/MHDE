"""Tests for pipelines.data_quality_guard — the OHLCV plausibility /
volume-cliff guard that would have caught the 2026-05-07 partial-candle
ingestion bug immediately.

``check_ohlcv_plausibility(conn, target_date)`` is pure (reads only,
returns a ``QualityReport``); ``persist_report(conn, report)`` UPSERTs
the flagged rows into ``crypto_data_quality_reports``.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from crypto.config import (
    OHLCV_PLAUSIBILITY_WINDOW_DAYS as WIN,
    SYSTEMIC_FLAG_RATIO,
    SYSTEMIC_MIN_SYMBOLS,
    VOLUME_CLIFF_RATIO,
)
from pipelines.data_quality_guard import (
    QualityReport,
    check_ohlcv_plausibility,
    persist_report,
)

TARGET = date(2026, 5, 7)
# "normal" candle baseline
NORM = dict(open=100.0, high=102.0, low=98.0, close=100.5, volume=1000.0, trades=500)


def _seed(conn, symbols, *, n_days=WIN + 1, target_date=TARGET, overrides=None,
          short_window_symbols=None):
    """Seed `n_days` consecutive daily rows per symbol ending at `target_date`,
    with `NORM` values. `overrides` maps symbol -> {col: value} applied to that
    symbol's `target_date` row only. `short_window_symbols` maps symbol ->
    n_prior_days (seeds only that many days ending at target_date — warmup case)."""
    overrides = overrides or {}
    short_window_symbols = short_window_symbols or {}
    for sym in symbols:
        days = short_window_symbols.get(sym, n_days)
        for i in range(days):
            d = target_date - timedelta(days=days - 1 - i)
            row = dict(NORM)
            if d == target_date and sym in overrides:
                row.update(overrides[sym])
            conn.execute(
                "INSERT INTO crypto_prices_daily (symbol, trade_date, open, high, low, "
                "close, volume, trades, taker_buy_volume, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'test')",
                [sym, d, row["open"], row["high"], row["low"], row["close"],
                 row["volume"], row["trades"], row["volume"] / 2],
            )


# cliff override builders
def _vol_cliff(frac=0.02):
    return {"volume": NORM["volume"] * frac, "trades": NORM["trades"] * frac}


def _range_collapse(frac=0.04):
    half = NORM["close"] * (4.0 / 100.0) * frac / 2  # original range = 4.0
    return {"high": NORM["close"] + half, "low": NORM["close"] - half}


# ── per-symbol flags ──────────────────────────────────────────────────


def test_volume_cliff_flag_fires(temp_db):
    _seed(temp_db, ["BTCUSDT"], overrides={"BTCUSDT": {"volume": NORM["volume"] * 0.02}})
    rep = check_ohlcv_plausibility(temp_db, TARGET)
    flags = {(f.symbol, f.check_name) for f in rep.per_symbol_flags}
    assert ("BTCUSDT", "volume_cliff") in flags
    assert rep.n_flagged == 1
    assert rep.is_systemic is False
    assert rep.severity == "warn"


def test_range_collapse_flag_fires(temp_db):
    _seed(temp_db, ["BTCUSDT"], overrides={"BTCUSDT": _range_collapse(0.04)})
    rep = check_ohlcv_plausibility(temp_db, TARGET)
    assert ("BTCUSDT", "range_collapse") in {(f.symbol, f.check_name) for f in rep.per_symbol_flags}


def test_trade_count_cliff_flag_fires(temp_db):
    _seed(temp_db, ["BTCUSDT"], overrides={"BTCUSDT": {"trades": NORM["trades"] * 0.02}})
    rep = check_ohlcv_plausibility(temp_db, TARGET)
    assert ("BTCUSDT", "trade_count_cliff") in {(f.symbol, f.check_name) for f in rep.per_symbol_flags}


def test_no_flag_on_noisy_but_normal_data(temp_db):
    rng = random.Random(42)
    # 30 symbols, each day's metrics jittered ±40%; target day also within ±40%
    for sym in [f"SYM{i}USDT" for i in range(30)]:
        for i in range(WIN + 1):
            d = TARGET - timedelta(days=WIN - i)
            j = lambda x: x * rng.uniform(0.6, 1.4)
            temp_db.execute(
                "INSERT INTO crypto_prices_daily (symbol, trade_date, open, high, low, "
                "close, volume, trades, taker_buy_volume, source) "
                "VALUES (?, ?, 100, 102, 98, 100.5, ?, ?, ?, 'test')",
                [sym, d, j(NORM["volume"]), int(j(NORM["trades"])), j(NORM["volume"]) / 2],
            )
    rep = check_ohlcv_plausibility(temp_db, TARGET)
    assert rep.n_flagged == 0
    assert rep.is_systemic is False
    assert rep.severity == "ok"


def test_no_flag_when_just_above_threshold(temp_db):
    # volume at 1.5x the cliff ratio → must NOT flag (strict less-than)
    frac = VOLUME_CLIFF_RATIO * 1.5
    _seed(temp_db, ["BTCUSDT"], overrides={"BTCUSDT": {"volume": NORM["volume"] * frac,
                                                       "trades": NORM["trades"] * frac}})
    rep = check_ohlcv_plausibility(temp_db, TARGET)
    assert rep.n_flagged == 0


# ── systemic flag ─────────────────────────────────────────────────────


def test_systemic_flag_fires_when_ratio_exceeded(temp_db):
    syms = [f"SYM{i}USDT" for i in range(20)]
    # 14 of 20 (70% > SYSTEMIC_FLAG_RATIO) get a volume cliff on the target day
    bad = {s: _vol_cliff() for s in syms[:14]}
    _seed(temp_db, syms, overrides=bad)
    rep = check_ohlcv_plausibility(temp_db, TARGET)
    assert rep.n_evaluated == 20
    assert rep.n_flagged == 14
    assert rep.systemic_ratio == pytest.approx(14 / 20)
    assert rep.systemic_ratio > SYSTEMIC_FLAG_RATIO
    assert rep.is_systemic is True
    assert rep.severity == "critical"


def test_systemic_not_fired_on_isolated_single_symbol(temp_db):
    syms = [f"SYM{i}USDT" for i in range(20)]
    _seed(temp_db, syms, overrides={syms[0]: _vol_cliff()})
    rep = check_ohlcv_plausibility(temp_db, TARGET)
    assert rep.n_flagged == 1
    assert rep.systemic_ratio == pytest.approx(1 / 20)
    assert rep.is_systemic is False
    assert rep.severity == "warn"


def test_systemic_not_fired_when_too_few_symbols_evaluable(temp_db):
    # Fewer evaluable symbols than SYSTEMIC_MIN_SYMBOLS, even if 100% flagged
    syms = [f"SYM{i}USDT" for i in range(SYSTEMIC_MIN_SYMBOLS - 2)]
    _seed(temp_db, syms, overrides={s: _vol_cliff() for s in syms})
    rep = check_ohlcv_plausibility(temp_db, TARGET)
    assert rep.n_evaluated == len(syms)
    assert rep.n_flagged == len(syms)
    assert rep.is_systemic is False  # cold-start guard
    assert rep.severity == "warn"


# ── edge cases ────────────────────────────────────────────────────────


def test_warmup_symbols_skipped_not_flagged(temp_db):
    # WARMUP1 has only 5 prior days on the target date → not evaluable, not flagged
    _seed(temp_db, ["BTCUSDT"], short_window_symbols=None)
    _seed(temp_db, ["WARMUP1USDT"], short_window_symbols={"WARMUP1USDT": 5},
          overrides={"WARMUP1USDT": _vol_cliff()})
    rep = check_ohlcv_plausibility(temp_db, TARGET)
    assert "WARMUP1USDT" not in {f.symbol for f in rep.per_symbol_flags}
    assert rep.n_evaluated == 1  # only BTCUSDT
    assert rep.n_flagged == 0


def test_empty_universe_handled_gracefully(temp_db):
    # nothing seeded at all
    rep = check_ohlcv_plausibility(temp_db, TARGET)
    assert rep.n_symbols_on_date == 0
    assert rep.n_evaluated == 0
    assert rep.n_flagged == 0
    assert rep.is_systemic is False
    assert rep.severity == "ok"
    assert rep.to_rows() == []


def test_no_rows_on_target_date_but_history_exists(temp_db):
    # symbol has 21 days of history ending the day BEFORE the target → no row on target
    _seed(temp_db, ["BTCUSDT"], target_date=TARGET - timedelta(days=1))
    rep = check_ohlcv_plausibility(temp_db, TARGET)
    assert rep.n_symbols_on_date == 0
    assert rep.severity == "ok"


def test_severity_property():
    assert QualityReport(target_date=TARGET, n_symbols_on_date=0, n_evaluated=0,
                         n_flagged=0, systemic_ratio=0.0, is_systemic=False,
                         per_symbol_flags=[]).severity == "ok"
    r_warn = QualityReport(target_date=TARGET, n_symbols_on_date=5, n_evaluated=5,
                           n_flagged=1, systemic_ratio=0.2, is_systemic=False,
                           per_symbol_flags=[])
    assert r_warn.severity == "warn"
    r_crit = QualityReport(target_date=TARGET, n_symbols_on_date=20, n_evaluated=20,
                           n_flagged=15, systemic_ratio=0.75, is_systemic=True,
                           per_symbol_flags=[])
    assert r_crit.severity == "critical"


# ── persistence ───────────────────────────────────────────────────────


def test_persist_report_writes_flagged_rows_and_systemic_row(temp_db):
    syms = [f"SYM{i}USDT" for i in range(20)]
    _seed(temp_db, syms, overrides={s: _vol_cliff() for s in syms[:14]})
    rep = check_ohlcv_plausibility(temp_db, TARGET)
    n = persist_report(temp_db, rep)
    rows = temp_db.execute(
        "SELECT date, symbol, check_name, flagged, severity FROM crypto_data_quality_reports "
        "ORDER BY symbol, check_name"
    ).fetchall()
    assert n == len(rows)
    # 14 symbols × {volume_cliff, trade_count_cliff} (both trip on a vol cliff) + 1 systemic row
    assert n == 14 * 2 + 1
    assert all(r[3] for r in rows)  # every persisted row is flagged
    sysrows = [r for r in rows if r[2] == "systemic_corruption"]
    assert len(sysrows) == 1 and sysrows[0][4] == "critical"
    assert all(r[4] == "warn" for r in rows if r[2] != "systemic_corruption")


def test_persist_report_idempotent_upsert(temp_db):
    _seed(temp_db, ["BTCUSDT"], overrides={"BTCUSDT": _vol_cliff()})
    rep = check_ohlcv_plausibility(temp_db, TARGET)
    n1 = persist_report(temp_db, rep)
    n2 = persist_report(temp_db, rep)  # re-run same date
    assert n1 == n2
    total = temp_db.execute("SELECT COUNT(*) FROM crypto_data_quality_reports").fetchone()[0]
    assert total == n1  # no duplicates


def test_persist_clean_report_writes_nothing(temp_db):
    _seed(temp_db, ["BTCUSDT"])  # no overrides → clean
    rep = check_ohlcv_plausibility(temp_db, TARGET)
    assert persist_report(temp_db, rep) == 0
    assert temp_db.execute("SELECT COUNT(*) FROM crypto_data_quality_reports").fetchone()[0] == 0


# ── integration: the May 7-11 corruption ──────────────────────────────


def test_simulated_partial_candle_corruption_triggers_systemic(temp_db):
    """Reproduce the 2026-05-07 bug shape: every symbol's target-day candle is
    a ~30-min partial — ~2.5% of normal volume/trades, a tiny price range —
    while the prior 20 days are normal. The systemic flag must fire."""
    syms = [f"SYM{i}USDT" for i in range(50)]
    partial = {s: {**_vol_cliff(0.025), **_range_collapse(0.04)} for s in syms}
    _seed(temp_db, syms, overrides=partial)
    rep = check_ohlcv_plausibility(temp_db, TARGET)
    assert rep.n_evaluated == 50
    assert rep.n_flagged == 50
    assert rep.systemic_ratio == pytest.approx(1.0)
    assert rep.is_systemic is True
    assert rep.severity == "critical"
