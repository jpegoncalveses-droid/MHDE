"""Tests for fx/data/compare_sources.py — Dukascopy ↔ TwelveData diff."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from fx.config import PIP_SIZE
from fx.data.compare_sources import compare_recent, format_report


def _seed_bar(conn, table: str, dt: datetime, close: float):
    conn.execute(
        f"INSERT INTO {table} "
        "(datetime_utc, date, weekday, hour_utc, gbpeur_open, gbpeur_high, "
        " gbpeur_low, gbpeur_close, tick_count, data_quality) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [dt, dt.date(), dt.strftime("%A"), dt.hour,
         close - 0.0005, close + 0.0010, close - 0.0010, close,
         100, "OK"],
    )


def test_compare_empty_db_returns_zero_matches(temp_db):
    now = datetime(2026, 5, 7, 18, 0, 0)
    result = compare_recent(temp_db, hours=24, threshold_pips=5, now_utc=now)
    assert result["matched"] == 0
    assert result["breaches"] == []
    # With zero matches the gate should NOT pass — there's nothing to compare.
    assert result["all_within_threshold"] is False


def test_compare_24_bars_within_1pip_passes(temp_db):
    """24 hourly bars in both tables, all within 1 pip → all pass, no breaches."""
    now = datetime(2026, 5, 7, 18, 0, 0)
    base = 1.158
    for h in range(24):
        bar_dt = now - timedelta(hours=h + 1)
        # Dukascopy and TwelveData differ by 0.5 pip (well under 5).
        _seed_bar(temp_db, "fx_prices_hourly", bar_dt, base + h * 0.0001)
        _seed_bar(temp_db, "fx_prices_hourly_twelvedata", bar_dt,
                   base + h * 0.0001 + 0.00005)

    result = compare_recent(temp_db, hours=24, threshold_pips=5, now_utc=now)
    assert result["matched"] == 24
    assert result["within_threshold"] == 24
    assert result["breaches"] == []
    assert result["all_within_threshold"] is True


def test_compare_breach_above_threshold_flagged(temp_db):
    """One bar diverges by 7 pips (above 5-pip threshold) → in breaches,
    all_within_threshold False."""
    now = datetime(2026, 5, 7, 18, 0, 0)
    base = 1.158
    breach_dt = now - timedelta(hours=3)

    for h in range(24):
        bar_dt = now - timedelta(hours=h + 1)
        _seed_bar(temp_db, "fx_prices_hourly", bar_dt, base)
        if bar_dt == breach_dt:
            # 7 pips off
            _seed_bar(temp_db, "fx_prices_hourly_twelvedata", bar_dt,
                       base + 7 * PIP_SIZE)
        else:
            _seed_bar(temp_db, "fx_prices_hourly_twelvedata", bar_dt, base)

    result = compare_recent(temp_db, hours=24, threshold_pips=5, now_utc=now)
    assert result["matched"] == 24
    assert len(result["breaches"]) == 1
    breach = result["breaches"][0]
    assert breach["datetime_utc"] == breach_dt
    assert breach["pip_diff"] == pytest.approx(-7.0, abs=0.01)
    assert result["all_within_threshold"] is False


def test_compare_missing_from_twelvedata(temp_db):
    """Dukascopy has bars TwelveData doesn't → counted in missing_from_twelvedata."""
    now = datetime(2026, 5, 7, 18, 0, 0)
    for h in range(5):
        bar_dt = now - timedelta(hours=h + 1)
        _seed_bar(temp_db, "fx_prices_hourly", bar_dt, 1.158)
        if h % 2 == 0:
            _seed_bar(temp_db, "fx_prices_hourly_twelvedata", bar_dt, 1.158)

    result = compare_recent(temp_db, hours=24, threshold_pips=5, now_utc=now)
    assert result["matched"] == 3   # h=0, 2, 4
    assert result["missing_from_twelvedata"] == 2  # h=1, 3
    assert result["missing_from_dukascopy"] == 0


def test_compare_missing_from_dukascopy(temp_db):
    now = datetime(2026, 5, 7, 18, 0, 0)
    for h in range(5):
        bar_dt = now - timedelta(hours=h + 1)
        _seed_bar(temp_db, "fx_prices_hourly_twelvedata", bar_dt, 1.158)
        if h % 2 == 0:
            _seed_bar(temp_db, "fx_prices_hourly", bar_dt, 1.158)

    result = compare_recent(temp_db, hours=24, threshold_pips=5, now_utc=now)
    assert result["matched"] == 3
    assert result["missing_from_dukascopy"] == 2
    assert result["missing_from_twelvedata"] == 0


def test_compare_window_excludes_old_bars(temp_db):
    """Bars outside the `hours` window are not counted."""
    now = datetime(2026, 5, 7, 18, 0, 0)
    inside = now - timedelta(hours=10)
    outside = now - timedelta(hours=50)
    _seed_bar(temp_db, "fx_prices_hourly", inside, 1.158)
    _seed_bar(temp_db, "fx_prices_hourly_twelvedata", inside, 1.158)
    _seed_bar(temp_db, "fx_prices_hourly", outside, 1.158)
    _seed_bar(temp_db, "fx_prices_hourly_twelvedata", outside, 1.158)

    result = compare_recent(temp_db, hours=24, threshold_pips=5, now_utc=now)
    assert result["matched"] == 1


# ──────────────────────────────────────────────────────────────────────
# format_report
# ──────────────────────────────────────────────────────────────────────


def test_format_report_pass_text(temp_db):
    now = datetime(2026, 5, 7, 18, 0, 0)
    base = 1.158
    for h in range(24):
        bar_dt = now - timedelta(hours=h + 1)
        _seed_bar(temp_db, "fx_prices_hourly", bar_dt, base)
        _seed_bar(temp_db, "fx_prices_hourly_twelvedata", bar_dt, base)
    result = compare_recent(temp_db, hours=24, threshold_pips=5, now_utc=now)
    text = format_report(result)
    assert "PASS" in text
    assert "24 matched bars" in text


def test_format_report_fail_text(temp_db):
    now = datetime(2026, 5, 7, 18, 0, 0)
    base = 1.158
    for h in range(3):
        bar_dt = now - timedelta(hours=h + 1)
        _seed_bar(temp_db, "fx_prices_hourly", bar_dt, base)
        _seed_bar(temp_db, "fx_prices_hourly_twelvedata", bar_dt,
                   base + 10 * PIP_SIZE)
    result = compare_recent(temp_db, hours=24, threshold_pips=5, now_utc=now)
    text = format_report(result)
    assert "FAIL" in text
    assert "exceed" in text


def test_format_report_no_overlap_text(temp_db):
    result = compare_recent(temp_db, hours=24, threshold_pips=5,
                             now_utc=datetime(2026, 5, 7, 18, 0, 0))
    text = format_report(result)
    assert "no overlapping" in text.lower()
