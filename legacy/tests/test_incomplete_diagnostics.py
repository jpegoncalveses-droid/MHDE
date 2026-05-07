"""Tests for Incomplete scoring diagnostics."""
import json
import os

import duckdb
import pytest

from scoring.incomplete_diagnostics import (
    IncompleteDiagnostic,
    IncompleteReason,
    diagnose_incomplete,
    write_diagnostics_csv,
)


def _make_mem_db(rows: list[tuple]) -> duckdb.DuckDBPyConnection:
    """Create an in-memory DuckDB with scores and companies tables pre-populated."""
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE scores (
            ticker VARCHAR,
            tier VARCHAR,
            missing_data_json VARCHAR,
            confidence DOUBLE,
            run_id VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE companies (
            ticker VARCHAR PRIMARY KEY,
            sector VARCHAR,
            last_financial_filing_date DATE
        )
    """)
    for ticker, tier, missing_json, confidence, sector, last_filing in rows:
        conn.execute(
            "INSERT INTO scores VALUES (?, ?, ?, ?, 'run1')",
            [ticker, tier, missing_json, confidence],
        )
        conn.execute(
            "INSERT INTO companies VALUES (?, ?, ?)",
            [ticker, sector, last_filing],
        )
    return conn


def test_reason_enum_values():
    assert IncompleteReason.MISSING_PRICES.value == "missing_prices"
    assert IncompleteReason.STALE_FUNDAMENTALS.value == "stale_fundamentals"
    assert IncompleteReason.IFRS_FILER.value == "ifrs_filer"
    assert IncompleteReason.LOW_FEATURE_COVERAGE.value == "low_feature_coverage"
    assert IncompleteReason.UNKNOWN.value == "unknown"


def test_diagnose_empty_when_no_incomplete(tmp_path):
    conn = _make_mem_db([])
    results = diagnose_incomplete(conn)
    assert results == []


def test_diagnose_detects_ifrs_filer():
    conn = _make_mem_db([
        ("AAAB", "Incomplete", '{"ifrs": true}', 0.2, "Financials", None),
    ])
    results = diagnose_incomplete(conn)
    assert len(results) == 1
    assert results[0].reason == IncompleteReason.IFRS_FILER.value


def test_diagnose_detects_missing_valuation():
    conn = _make_mem_db([
        ("AAAB", "Incomplete", '{"valuation": "missing pe_proxy"}', 0.4, "Technology", None),
    ])
    results = diagnose_incomplete(conn)
    assert len(results) == 1
    assert results[0].reason == IncompleteReason.MISSING_VALUATION_INPUTS.value


def test_diagnose_detects_stale_fundamentals():
    import datetime
    stale_date = (datetime.date.today() - datetime.timedelta(days=200)).isoformat()
    conn = _make_mem_db([
        ("AAAB", "Incomplete", "{}", 0.5, "Industrials", stale_date),
    ])
    results = diagnose_incomplete(conn)
    assert len(results) == 1
    assert results[0].reason == IncompleteReason.STALE_FUNDAMENTALS.value


def test_diagnose_detects_low_coverage():
    conn = _make_mem_db([
        ("AAAB", "Incomplete", "{}", 0.1, "Energy", None),
    ])
    results = diagnose_incomplete(conn)
    assert len(results) == 1
    assert results[0].reason == IncompleteReason.LOW_FEATURE_COVERAGE.value


def test_diagnose_unknown_when_no_signal():
    conn = _make_mem_db([
        ("AAAB", "Incomplete", "{}", 0.5, "Technology", None),
    ])
    results = diagnose_incomplete(conn)
    assert len(results) == 1
    assert results[0].reason == IncompleteReason.UNKNOWN.value


def test_write_diagnostics_csv(tmp_path):
    diags = [
        IncompleteDiagnostic(ticker="AAAB", reason="ifrs_filer", sector="Financials", detail="IFRS"),
        IncompleteDiagnostic(ticker="AAAC", reason="unknown", sector="Tech", detail=""),
    ]
    out = str(tmp_path / "diag.csv")
    write_diagnostics_csv(diags, out)
    assert os.path.exists(out)
    content = open(out).read()
    assert "AAAB" in content
    assert "ifrs_filer" in content
    assert "AAAC" in content


def test_write_diagnostics_csv_empty_does_not_create(tmp_path):
    out = str(tmp_path / "diag.csv")
    write_diagnostics_csv([], out)
    assert not os.path.exists(out)


def test_incomplete_diagnostic_dataclass():
    d = IncompleteDiagnostic(ticker="AAAB", reason="unknown", sector="Tech", detail="")
    assert d.ticker == "AAAB"
