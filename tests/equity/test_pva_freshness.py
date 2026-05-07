"""Tests for PvA freshness guard — detects stale prediction-vs-actual artifacts."""
from __future__ import annotations

import csv
import datetime
import json
import os

import duckdb
import pytest

from health.pva_freshness import check_pva_freshness, PvaFreshnessResult


def _make_prices_db(tmp_path, latest_date: str) -> str:
    db = str(tmp_path / "mhde.duckdb")
    conn = duckdb.connect(db)
    conn.execute("""
        CREATE TABLE prices_daily (
            id VARCHAR PRIMARY KEY,
            ticker VARCHAR,
            trade_date DATE,
            close DOUBLE
        )
    """)
    conn.execute(
        "INSERT INTO prices_daily VALUES ('t1', 'AAPL', ?, 100.0)",
        [latest_date],
    )
    conn.close()
    return db


def _write_pva_csv(tmp_path, rows: list[dict], filename="prediction_vs_actual_rows.csv") -> str:
    path = str(tmp_path / filename)
    if not rows:
        open(path, "w").close()
        return path
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


# ── PvaFreshnessResult dataclass ─────────────────────────────────────────────

def test_freshness_result_has_required_fields():
    import dataclasses
    fields = {f.name for f in dataclasses.fields(PvaFreshnessResult)}
    required = {"is_stale", "latest_price_date", "pva_max_event_date", "pva_artifact_mtime", "reason"}
    assert required <= fields


# ── Stale detection ───────────────────────────────────────────────────────────

def test_stale_when_prices_newer_than_pva(tmp_path):
    """Prices have 2026-05-01 but PvA coverage only through 2026-04-30 → stale."""
    db = _make_prices_db(tmp_path, "2026-05-01")
    pva = _write_pva_csv(tmp_path, [
        {"event_date": "2026-04-30", "ticker": "AAPL", "classification": "true_miss"},
        {"event_date": "2026-04-29", "ticker": "AAPL", "classification": "true_miss"},
    ])
    result = check_pva_freshness(db_path=db, pva_csv_path=pva)
    assert result.is_stale is True
    assert "2026-05-01" in result.reason or "stale" in result.reason.lower()


def test_not_stale_when_aligned(tmp_path):
    """Prices and PvA both cover through 2026-05-01 → not stale."""
    db = _make_prices_db(tmp_path, "2026-05-01")
    pva = _write_pva_csv(tmp_path, [
        {"event_date": "2026-05-01", "ticker": "AAPL", "classification": "true_miss"},
    ])
    result = check_pva_freshness(db_path=db, pva_csv_path=pva)
    assert result.is_stale is False


def test_stale_when_pva_missing(tmp_path):
    """PvA CSV does not exist → stale."""
    db = _make_prices_db(tmp_path, "2026-05-01")
    result = check_pva_freshness(db_path=db, pva_csv_path=str(tmp_path / "missing.csv"))
    assert result.is_stale is True
    assert "missing" in result.reason.lower() or "not found" in result.reason.lower()


def test_stale_when_pva_empty(tmp_path):
    """PvA CSV exists but is empty → stale."""
    db = _make_prices_db(tmp_path, "2026-05-01")
    pva = _write_pva_csv(tmp_path, [])
    result = check_pva_freshness(db_path=db, pva_csv_path=pva)
    assert result.is_stale is True


def test_not_stale_when_no_prices(tmp_path):
    """No prices in DB → cannot determine staleness → not stale (no false alarm)."""
    db_path = str(tmp_path / "empty.duckdb")
    conn = duckdb.connect(db_path)
    conn.execute("CREATE TABLE prices_daily (id VARCHAR, ticker VARCHAR, trade_date DATE, close DOUBLE)")
    conn.close()
    pva = _write_pva_csv(tmp_path, [{"event_date": "2026-05-01", "ticker": "A", "classification": "x"}])
    result = check_pva_freshness(db_path=db_path, pva_csv_path=pva)
    assert result.is_stale is False


def test_latest_price_date_captured(tmp_path):
    """Result contains the correct latest_price_date."""
    db = _make_prices_db(tmp_path, "2026-05-01")
    pva = _write_pva_csv(tmp_path, [{"event_date": "2026-04-28", "ticker": "A", "classification": "x"}])
    result = check_pva_freshness(db_path=db, pva_csv_path=pva)
    assert str(result.latest_price_date) == "2026-05-01"


def test_pva_max_event_date_captured(tmp_path):
    """Result contains the correct pva_max_event_date from the CSV."""
    db = _make_prices_db(tmp_path, "2026-05-01")
    pva = _write_pva_csv(tmp_path, [
        {"event_date": "2026-04-28", "ticker": "A", "classification": "x"},
        {"event_date": "2026-04-30", "ticker": "B", "classification": "x"},
    ])
    result = check_pva_freshness(db_path=db, pva_csv_path=pva)
    assert str(result.pva_max_event_date) == "2026-04-30"


def test_no_scoring_changes():
    """pva_freshness module must not modify scoring or introduce feature flags."""
    import inspect
    import health.pva_freshness as mod
    src = inspect.getsource(mod)
    for bad in ("feature_flag", "FeatureFlag", "openai", "anthropic"):
        assert bad.lower() not in src.lower(), f"Prohibited term '{bad}' in pva_freshness.py"
