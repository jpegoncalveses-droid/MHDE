"""Tests for data coverage report generation."""
import csv
import datetime
import os

import duckdb
import pytest

from health.coverage_report import generate_coverage_report


def _make_db(tmp_path) -> str:
    db_path = str(tmp_path / "test.duckdb")
    conn = duckdb.connect(db_path)
    conn.execute("""
        CREATE TABLE companies (
            ticker VARCHAR PRIMARY KEY,
            is_active BOOLEAN DEFAULT true,
            market_cap DOUBLE,
            last_financial_filing_date DATE,
            last_seen_at TIMESTAMP,
            sector VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE prices_daily (
            ticker VARCHAR, trade_date DATE, close DOUBLE,
            PRIMARY KEY (ticker, trade_date)
        )
    """)
    today = str(datetime.date.today())
    conn.execute("INSERT INTO companies VALUES ('AAAB', true, 1e11, '2025-12-31', NULL, 'Tech')")
    conn.execute("INSERT INTO companies VALUES ('BBBB', true, NULL, NULL, NULL, 'Finance')")
    conn.execute("INSERT INTO companies VALUES ('CCCC', true, NULL, NULL, NULL, NULL)")
    conn.execute(f"INSERT INTO prices_daily VALUES ('AAAB', '{today}', 100.0)")
    conn.execute("INSERT INTO prices_daily VALUES ('BBBB', '2026-04-01', 90.0)")
    conn.close()
    return db_path


def test_generate_report_creates_files(tmp_path):
    db_path = _make_db(tmp_path)
    result = generate_coverage_report(db_path=db_path, output_dir=str(tmp_path))
    assert os.path.exists(result["md"])
    assert os.path.exists(result["csv"])


def test_report_md_contains_summary(tmp_path):
    db_path = _make_db(tmp_path)
    result = generate_coverage_report(db_path=db_path, output_dir=str(tmp_path))
    content = open(result["md"]).read()
    assert "Data Coverage" in content
    assert "AAAB" not in content  # MD shows summary counts, not per-ticker rows


def test_report_csv_has_all_tickers(tmp_path):
    db_path = _make_db(tmp_path)
    result = generate_coverage_report(db_path=db_path, output_dir=str(tmp_path))
    with open(result["csv"], newline="") as f:
        rows = list(csv.DictReader(f))
    tickers = [r["ticker"] for r in rows]
    assert "AAAB" in tickers
    assert "BBBB" in tickers
    assert "CCCC" in tickers


def test_report_summary_counts(tmp_path):
    db_path = _make_db(tmp_path)
    result = generate_coverage_report(db_path=db_path, output_dir=str(tmp_path))
    s = result["summary"]
    assert s["total"] == 3
    assert s["has_prices"] == 2
    assert s["has_fundamentals"] == 1
    assert s["has_market_cap"] == 1


def test_report_csv_freshness_labels(tmp_path):
    db_path = _make_db(tmp_path)
    result = generate_coverage_report(db_path=db_path, output_dir=str(tmp_path))
    with open(result["csv"], newline="") as f:
        rows = {r["ticker"]: r for r in csv.DictReader(f)}
    assert rows["CCCC"]["freshness_label"] == "missing"
    assert rows["BBBB"]["freshness_label"] == "stale"
