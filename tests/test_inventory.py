from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import pytest
import duckdb
from click.testing import CliRunner

from storage.db import get_connection, init_schema
from storage.inventory import (
    TABLE_CATALOG,
    FILE_CATALOG,
    get_db_table_stats,
    get_file_stats,
    build_inventory,
    write_markdown,
    write_csv,
)

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

KNOWN_TABLES = [
    "schema_version",
    "companies",
    "source_runs",
    "filings",
    "fundamentals_raw",
    "fundamentals_features",
    "prices_daily",
    "macro_series",
    "short_interest",
    "events",
    "features",
    "scores",
    "hypotheses",
    "rejections",
    "candidate_outcomes",
    "backtest_runs",
    "model_runs",
    "llm_runs",
    "alerts",
    "pipeline_runs",
    "review_notes",
    "dashboard_actions",
    "candidate_reviews",
    "scorecard_experiments",
    "missed_opportunity_events",
    "missed_opportunity_investigations",
    "missed_opportunity_root_causes",
    "promotion_gate_results",
    "health_checks",
]


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    c.execute("INSERT INTO companies (ticker, company_name) VALUES ('AAPL', 'Apple')")
    c.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES ('p1', 'AAPL', '2026-01-01', 100.0)"
    )
    c.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES ('p2', 'AAPL', '2026-03-01', 120.0)"
    )
    yield c
    c.close()


# ---------------------------------------------------------------------------
# TABLE_CATALOG tests
# ---------------------------------------------------------------------------


def test_table_catalog_covers_all_known_tables():
    for table in KNOWN_TABLES:
        assert table in TABLE_CATALOG, f"TABLE_CATALOG missing entry for: {table}"


def test_table_catalog_entries_have_required_keys():
    required_keys = {"description", "pk", "important_cols", "source", "freshness", "consumers"}
    for table, entry in TABLE_CATALOG.items():
        missing = required_keys - set(entry.keys())
        assert not missing, f"TABLE_CATALOG['{table}'] missing keys: {missing}"


# ---------------------------------------------------------------------------
# FILE_CATALOG tests
# ---------------------------------------------------------------------------


def test_file_catalog_entries_have_required_keys():
    required_keys = {"pattern", "format", "description", "consumers"}
    for i, entry in enumerate(FILE_CATALOG):
        missing = required_keys - set(entry.keys())
        assert not missing, f"FILE_CATALOG[{i}] missing keys: {missing}"


# ---------------------------------------------------------------------------
# get_db_table_stats tests
# ---------------------------------------------------------------------------


def test_get_db_table_stats_returns_list(conn):
    result = get_db_table_stats(conn)
    assert isinstance(result, list)
    assert len(result) > 0


def test_get_db_table_stats_row_count(conn):
    result = get_db_table_stats(conn)
    companies_rows = [r for r in result if r["table"] == "companies"]
    assert len(companies_rows) == 1
    assert companies_rows[0]["row_count"] == 1


def test_get_db_table_stats_date_range(conn):
    result = get_db_table_stats(conn)
    prices_rows = [r for r in result if r["table"] == "prices_daily"]
    assert len(prices_rows) == 1
    row = prices_rows[0]
    assert str(row["date_min"]) == "2026-01-01"
    assert str(row["date_max"]) == "2026-03-01"


def test_get_db_table_stats_keys(conn):
    result = get_db_table_stats(conn)
    required_keys = {"table", "row_count", "date_min", "date_max"}
    for row in result:
        missing = required_keys - set(row.keys())
        assert not missing, f"get_db_table_stats row missing keys: {missing}"


# ---------------------------------------------------------------------------
# get_file_stats tests
# ---------------------------------------------------------------------------


def test_get_file_stats_returns_list(tmp_path):
    jsonl_file = tmp_path / "daily_catalyst_queue_enriched.jsonl"
    jsonl_file.write_text('{"ticker": "AAPL"}\n')
    result = get_file_stats(base_dir=str(tmp_path))
    assert isinstance(result, list)


def test_get_file_stats_counts_lines(tmp_path):
    jsonl_file = tmp_path / "daily_catalyst_queue_enriched.jsonl"
    jsonl_file.write_text(
        '{"ticker": "AAPL"}\n{"ticker": "NVDA"}\n{"ticker": "MSFT"}\n'
    )
    result = get_file_stats(base_dir=str(tmp_path))
    matching = [r for r in result if "daily_catalyst_queue_enriched" in r["path"]]
    assert len(matching) == 1
    assert matching[0]["row_count"] == 3


def test_get_file_stats_csv_counts(tmp_path):
    csv_file = tmp_path / "daily_catalyst_queue.csv"
    csv_file.write_text("ticker,score\nAAPL,80\nNVDA,75\n")
    result = get_file_stats(base_dir=str(tmp_path))
    matching = [r for r in result if "daily_catalyst_queue.csv" in r["path"]]
    assert len(matching) == 1
    assert matching[0]["row_count"] == 2


# ---------------------------------------------------------------------------
# build_inventory tests
# ---------------------------------------------------------------------------


def test_build_inventory_returns_tuple(conn, tmp_path):
    result = build_inventory(conn, base_dir=str(tmp_path))
    assert isinstance(result, tuple)
    assert len(result) == 2
    tables, files = result
    assert isinstance(tables, list)
    assert isinstance(files, list)


def test_build_inventory_tables_have_catalog_metadata(conn, tmp_path):
    tables, _ = build_inventory(conn, base_dir=str(tmp_path))
    prices_entries = [t for t in tables if t["table"] == "prices_daily"]
    assert len(prices_entries) == 1
    entry = prices_entries[0]
    assert "description" in entry
    assert "consumers" in entry
    assert "source" in entry


# ---------------------------------------------------------------------------
# write_markdown tests
# ---------------------------------------------------------------------------


def test_write_markdown_creates_file(conn, tmp_path):
    tables, files = build_inventory(conn, base_dir=str(tmp_path))
    output_path = tmp_path / "inventory.md"
    write_markdown(tables, files, str(output_path))
    assert output_path.exists()
    content = output_path.read_text()
    assert "# MHDE Data Inventory" in content
    assert "prices_daily" in content
    assert "companies" in content


def test_write_markdown_contains_all_sections(conn, tmp_path):
    tables, files = build_inventory(conn, base_dir=str(tmp_path))
    output_path = tmp_path / "inventory.md"
    write_markdown(tables, files, str(output_path))
    content = output_path.read_text()
    assert "## Database Tables" in content
    assert "## Flat Files" in content


# ---------------------------------------------------------------------------
# write_csv tests
# ---------------------------------------------------------------------------


def test_write_csv_creates_file(conn, tmp_path):
    tables, files = build_inventory(conn, base_dir=str(tmp_path))
    output_path = tmp_path / "inventory.csv"
    write_csv(tables, files, str(output_path))
    assert output_path.exists()


def test_write_csv_has_header_and_rows(conn, tmp_path):
    tables, files = build_inventory(conn, base_dir=str(tmp_path))
    output_path = tmp_path / "inventory.csv"
    write_csv(tables, files, str(output_path))
    with open(str(output_path), newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert len(rows) > 0
    required_keys = {"asset_type", "name", "row_count"}
    for key in required_keys:
        assert key in reader.fieldnames, f"CSV missing column: {key}"


# ── CLI integration test ───────────────────────────────────────────────────────

from main import cli


def test_data_inventory_cli(tmp_path):
    runner = CliRunner()
    docs_out = str(tmp_path / "data_inventory.md")
    csv_out = str(tmp_path / "data_inventory_summary.csv")
    base_dir = str(tmp_path / "processed")
    os.makedirs(base_dir, exist_ok=True)

    result = runner.invoke(cli, [
        "data", "inventory",
        "--docs-out", docs_out,
        "--csv-out", csv_out,
        "--base-dir", base_dir,
    ])

    assert result.exit_code == 0, f"CLI failed: {result.output}\n{result.exception}"
    assert os.path.exists(docs_out), "Markdown output not created"
    assert os.path.exists(csv_out), "CSV output not created"

    content = Path(docs_out).read_text()
    assert "# MHDE Data Inventory" in content
    assert "prices_daily" in content

    with open(csv_out) as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    assert any(r["name"] == "prices_daily" for r in rows)
    assert any(r["asset_type"] == "db_table" for r in rows)
