# Data Inventory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Document every data asset (DB tables + data/processed/ files) in `docs/data_inventory.md` and `data/processed/data_inventory_summary.csv`, and expose a `main.py data inventory` CLI command that regenerates them on demand.

**Architecture:** A new `storage/inventory.py` module holds two static catalogs (TABLE_CATALOG for DB tables, FILE_CATALOG for flat-file patterns) plus live query functions that annotate each entry with row counts and date coverage from the running DuckDB instance. The CLI command calls `build_inventory()`, then `write_markdown()` and `write_csv()`. No scoring logic is touched.

**Tech Stack:** Python 3.11, DuckDB (via existing `storage.db`), Click (existing CLI), csv stdlib, pathlib.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `storage/inventory.py` | **Create** | Static catalog + live DB stats + file scan + write helpers |
| `tests/test_inventory.py` | **Create** | Unit tests for inventory module |
| `main.py` | **Modify** | Add `data` group + `inventory` subcommand |
| `docs/data_inventory.md` | **Generated** | Human-readable full inventory (output artifact) |
| `data/processed/data_inventory_summary.csv` | **Generated** | Machine-readable summary (output artifact) |

---

## Task 1: Write failing tests

**Files:**
- Create: `tests/test_inventory.py`

- [ ] **Step 1: Write the test file**

```python
# tests/test_inventory.py
from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path

import pytest

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


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    c.execute(
        "INSERT INTO companies (ticker, company_name) VALUES ('AAPL', 'Apple')"
    )
    c.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close) "
        "VALUES ('p1', 'AAPL', '2026-01-01', 100.0)"
    )
    c.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close) "
        "VALUES ('p2', 'AAPL', '2026-03-01', 120.0)"
    )
    yield c
    c.close()


# ── TABLE_CATALOG tests ────────────────────────────────────────────────────────

def test_table_catalog_covers_all_known_tables():
    required = [
        "companies", "prices_daily", "fundamentals_raw", "fundamentals_features",
        "filings", "short_interest", "events", "macro_series", "scores",
        "features", "hypotheses", "rejections", "missed_opportunity_events",
        "missed_opportunity_investigations", "missed_opportunity_root_causes",
        "llm_runs", "candidate_reviews", "candidate_outcomes",
        "pipeline_runs", "backtest_runs", "model_runs", "review_notes",
        "scorecard_experiments", "promotion_gate_results", "health_checks",
        "source_runs", "alerts", "dashboard_actions", "schema_version",
    ]
    for t in required:
        assert t in TABLE_CATALOG, f"TABLE_CATALOG missing entry for: {t}"


def test_table_catalog_entries_have_required_keys():
    required_keys = {"description", "pk", "important_cols", "source", "freshness", "consumers"}
    for table_name, meta in TABLE_CATALOG.items():
        missing = required_keys - set(meta.keys())
        assert not missing, f"{table_name} missing keys: {missing}"


def test_file_catalog_entries_have_required_keys():
    required_keys = {"pattern", "format", "description", "consumers"}
    for entry in FILE_CATALOG:
        missing = required_keys - set(entry.keys())
        assert not missing, f"FILE_CATALOG entry missing keys: {missing}"


# ── get_db_table_stats tests ───────────────────────────────────────────────────

def test_get_db_table_stats_returns_list(conn):
    stats = get_db_table_stats(conn)
    assert isinstance(stats, list)
    assert len(stats) > 0


def test_get_db_table_stats_row_count(conn):
    stats = get_db_table_stats(conn)
    companies = next(s for s in stats if s["table"] == "companies")
    assert companies["row_count"] == 1


def test_get_db_table_stats_date_range(conn):
    stats = get_db_table_stats(conn)
    prices = next(s for s in stats if s["table"] == "prices_daily")
    assert prices["date_min"] == "2026-01-01"
    assert prices["date_max"] == "2026-03-01"


def test_get_db_table_stats_keys(conn):
    stats = get_db_table_stats(conn)
    required = {"table", "row_count", "date_min", "date_max"}
    for row in stats:
        missing = required - set(row.keys())
        assert not missing, f"Row for {row['table']} missing keys: {missing}"


# ── get_file_stats tests ───────────────────────────────────────────────────────

def test_get_file_stats_returns_list(tmp_path):
    # Create a fake JSONL file
    f = tmp_path / "daily_catalyst_queue_enriched.jsonl"
    f.write_text('{"ticker": "AAPL"}\n{"ticker": "NVDA"}\n')

    stats = get_file_stats(base_dir=str(tmp_path))
    assert isinstance(stats, list)


def test_get_file_stats_counts_lines(tmp_path):
    f = tmp_path / "daily_catalyst_queue_enriched.jsonl"
    f.write_text('{"a": 1}\n{"b": 2}\n{"c": 3}\n')

    stats = get_file_stats(base_dir=str(tmp_path))
    match = next((s for s in stats if "daily_catalyst_queue_enriched" in s["path"]), None)
    assert match is not None
    assert match["row_count"] == 3


def test_get_file_stats_csv_counts(tmp_path):
    f = tmp_path / "daily_catalyst_queue.csv"
    f.write_text("ticker,score\nAAPL,80\nNVDA,75\n")

    stats = get_file_stats(base_dir=str(tmp_path))
    match = next((s for s in stats if "daily_catalyst_queue.csv" in s["path"]), None)
    assert match is not None
    assert match["row_count"] == 2  # header excluded


# ── build_inventory tests ──────────────────────────────────────────────────────

def test_build_inventory_returns_tuple(conn, tmp_path):
    tables, files = build_inventory(conn, base_dir=str(tmp_path))
    assert isinstance(tables, list)
    assert isinstance(files, list)


def test_build_inventory_tables_have_catalog_metadata(conn, tmp_path):
    tables, _ = build_inventory(conn, base_dir=str(tmp_path))
    prices = next(t for t in tables if t["table"] == "prices_daily")
    assert "description" in prices
    assert "consumers" in prices
    assert "source" in prices


# ── write_markdown tests ───────────────────────────────────────────────────────

def test_write_markdown_creates_file(conn, tmp_path):
    tables, files = build_inventory(conn, base_dir=str(tmp_path))
    out = tmp_path / "data_inventory.md"
    write_markdown(tables, files, str(out))
    assert out.exists()
    content = out.read_text()
    assert "# MHDE Data Inventory" in content
    assert "prices_daily" in content
    assert "companies" in content


def test_write_markdown_contains_all_sections(conn, tmp_path):
    tables, files = build_inventory(conn, base_dir=str(tmp_path))
    out = tmp_path / "data_inventory.md"
    write_markdown(tables, files, str(out))
    content = out.read_text()
    for section in ["## Database Tables", "## Flat Files"]:
        assert section in content, f"Missing section: {section}"


# ── write_csv tests ────────────────────────────────────────────────────────────

def test_write_csv_creates_file(conn, tmp_path):
    tables, files = build_inventory(conn, base_dir=str(tmp_path))
    out = tmp_path / "data_inventory_summary.csv"
    write_csv(tables, files, str(out))
    assert out.exists()


def test_write_csv_has_header_and_rows(conn, tmp_path):
    tables, files = build_inventory(conn, base_dir=str(tmp_path))
    out = tmp_path / "data_inventory_summary.csv"
    write_csv(tables, files, str(out))
    with open(out) as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    assert len(rows) > 0
    assert "asset_type" in rows[0]
    assert "name" in rows[0]
    assert "row_count" in rows[0]
```

- [ ] **Step 2: Run tests to confirm they fail (module not yet created)**

```bash
venv/bin/python -m pytest tests/test_inventory.py -v 2>&1 | tail -20
```

Expected: `ModuleNotFoundError: No module named 'storage.inventory'`

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/test_inventory.py
git commit -m "test: add failing tests for data inventory module"
```

---

## Task 2: Implement `storage/inventory.py`

**Files:**
- Create: `storage/inventory.py`

- [ ] **Step 1: Write the module**

```python
# storage/inventory.py
from __future__ import annotations

import csv
import glob
import os
from datetime import date
from pathlib import Path

import duckdb

# ── Static catalog: one entry per DB table ─────────────────────────────────────
# Keys: description, pk, important_cols, source, freshness, consumers

TABLE_CATALOG: dict[str, dict] = {
    "schema_version": {
        "description": "Tracks applied DB migration versions.",
        "pk": "version",
        "important_cols": ["version", "applied_at", "description"],
        "source": "internal",
        "freshness": "updated per migration",
        "consumers": ["storage/migrations.py"],
    },
    "companies": {
        "description": "Universe of tracked equities — the master ticker list.",
        "pk": "ticker",
        "important_cols": [
            "ticker", "cik", "company_name", "exchange", "sector", "industry",
            "is_active", "is_etf", "is_fund", "universe_tier", "market_cap",
            "active_sec_reporter", "last_financial_filing_date",
            "has_financial_reporting_forms", "universe_exclusion_reason",
        ],
        "source": "SEC company-tickers JSON (company_tickers.json)",
        "freshness": "rebuilt every pipeline run via build_universe()",
        "consumers": [
            "universe/universe_builder.py",
            "features/feature_builder.py",
            "ingestion/orchestrator.py",
            "scoring/scorecard.py",
            "main.py (score command)",
            "health/universe_quality.py",
        ],
    },
    "source_runs": {
        "description": "Audit log: one row per ingestor execution attempt.",
        "pk": "id",
        "important_cols": [
            "run_id", "source_name", "use_case", "status", "started_at",
            "finished_at", "records_inserted", "records_failed", "error_message",
        ],
        "source": "written by ingestion/base_ingestor.py",
        "freshness": "appended on every ingest run",
        "consumers": ["health/source_status.py", "ingestion/base_ingestor.py"],
    },
    "filings": {
        "description": "SEC EDGAR filing index — form types, dates, accession numbers, and doc URLs.",
        "pk": "id",
        "important_cols": [
            "ticker", "cik", "form_type", "accession_number",
            "filing_date", "report_date", "doc_url",
        ],
        "source": "SEC EDGAR company submissions API",
        "freshness": "refreshed per ticker on each SEC ingest run (incremental)",
        "consumers": [
            "features/catalyst.py",
            "features/risk.py",
            "features/filer_utils.py",
            "missed/catalyst_sampler.py",
            "missed/investigator.py",
            "review/packet_builder.py",
        ],
    },
    "fundamentals_raw": {
        "description": "Raw XBRL/IFRS concept-level financial facts from SEC EDGAR.",
        "pk": "id",
        "important_cols": [
            "ticker", "cik", "concept", "value", "unit",
            "as_of_date", "period_of_report", "form",
        ],
        "source": "SEC EDGAR XBRL company-facts API",
        "freshness": "refreshed per ticker on each SEC ingest run",
        "consumers": [
            "features/quality.py",
            "features/valuation.py",
            "features/industry_utils.py",
            "features/filer_utils.py",
        ],
    },
    "fundamentals_features": {
        "description": "Derived fundamental metrics (revenue, net income, margins, etc.) keyed by ticker+date.",
        "pk": "id",
        "important_cols": [
            "ticker", "as_of_date", "revenue", "net_income", "shares_outstanding",
            "revenue_growth_yoy", "net_margin", "dilution_rate",
            "pe_proxy", "ps_proxy", "data_freshness_days",
        ],
        "source": "derived by features/quality.py from fundamentals_raw",
        "freshness": "computed on each scoring run",
        "consumers": ["features/feature_builder.py", "scoring/scorecard.py"],
    },
    "prices_daily": {
        "description": "Daily OHLCV prices for all tracked tickers.",
        "pk": "id",
        "important_cols": [
            "ticker", "trade_date", "open", "high", "low",
            "close", "volume", "adjusted_close", "source",
        ],
        "source": "Polygon (primary), Stooq (gap-fill), Yahoo Finance (historical bootstrap)",
        "freshness": "updated daily; Polygon runs first, Stooq fills gaps, Yahoo bootstraps history",
        "consumers": [
            "features/momentum.py",
            "features/valuation.py",
            "features/risk.py",
            "pipelines/daily_radar.py",
            "backtest/historical_replay.py",
            "backtest/labels.py",
            "missed/detector.py",
        ],
    },
    "macro_series": {
        "description": "Macroeconomic time series (interest rates, inflation, etc.) from FRED.",
        "pk": "id",
        "important_cols": [
            "series_id", "series_name", "value", "as_of_date", "frequency", "source",
        ],
        "source": "FRED API",
        "freshness": "updated each ingest run",
        "consumers": ["features/macro.py"],
    },
    "short_interest": {
        "description": "FINRA biweekly short-interest reports by ticker.",
        "pk": "id",
        "important_cols": [
            "ticker", "settlement_date", "short_interest", "avg_daily_volume", "days_to_cover",
        ],
        "source": "FINRA short-interest CSV",
        "freshness": "biweekly; updated on each FINRA ingest run",
        "consumers": ["features/sentiment.py", "features/catalyst.py"],
    },
    "events": {
        "description": "Upcoming earnings, FDA dates, and IR calendar events.",
        "pk": "id",
        "important_cols": [
            "ticker", "event_type", "event_date", "title", "description",
            "source", "is_upcoming",
        ],
        "source": "Nasdaq Earnings calendar, FDA calendar, company IR pages",
        "freshness": "updated on each events ingest run",
        "consumers": ["features/catalyst.py", "review/packet_builder.py"],
    },
    "features": {
        "description": "EAV store of all computed feature values and scores per ticker per run.",
        "pk": "id",
        "important_cols": [
            "run_id", "ticker", "as_of_date", "feature_group",
            "feature_name", "feature_value", "feature_score",
            "source", "confidence",
        ],
        "source": "computed by features/ modules during scoring",
        "freshness": "written fresh per scoring run",
        "consumers": ["scoring/scorecard.py", "review/packet_builder.py"],
    },
    "scores": {
        "description": "Final composite scores and tier assignments per ticker per run.",
        "pk": "id",
        "important_cols": [
            "run_id", "ticker", "as_of_date", "cheap_score", "quality_score",
            "catalyst_score", "momentum_score", "sentiment_score",
            "risk_penalty", "total_score", "tier", "confidence",
            "why_ranked", "why_rejected",
        ],
        "source": "scoring/scorecard.py",
        "freshness": "written once per scoring run; not mutated afterward",
        "consumers": [
            "scoring/ranker.py",
            "review/packet_builder.py",
            "missed/catalyst_sampler.py",
            "missed/catalyst_queue.py",
            "missed/detector.py",
            "missed/investigator.py",
            "health/operational.py",
            "health/data_quality.py",
            "pipelines/weekly_review.py",
            "main.py (shadow command)",
        ],
    },
    "hypotheses": {
        "description": "LLM-generated investment theses for A/B-tier candidates.",
        "pk": "hypothesis_id",
        "important_cols": [
            "run_id", "ticker", "company_name", "rank", "tier", "total_score",
            "thesis", "why_now", "status", "review_status",
        ],
        "source": "llm/runner.py after scoring",
        "freshness": "created per scoring run for new candidates",
        "consumers": [
            "review/packet_builder.py",
            "pipelines/weekly_review.py",
            "main.py (brief command)",
        ],
    },
    "rejections": {
        "description": "Tickers rejected below scoring threshold with reason.",
        "pk": "id",
        "important_cols": [
            "run_id", "ticker", "reason", "risk_flags_json", "missing_data_json",
        ],
        "source": "scoring/scorecard.py",
        "freshness": "written per scoring run",
        "consumers": ["review/packet_builder.py"],
    },
    "candidate_outcomes": {
        "description": "Forward-looking price outcomes for scored candidates (backtest labels).",
        "pk": "candidate_id",
        "important_cols": [
            "run_id", "ticker", "as_of_date", "tier", "total_score",
            "reference_price", "forward_return_1d", "forward_return_5d",
            "forward_return_20d", "forward_return_60d",
            "max_drawdown_20d", "hit_10pct_before_down_10pct",
            "review_status",
        ],
        "source": "backtest/labels.py",
        "freshness": "computed during backtest run; review_status updated manually",
        "consumers": ["learning/summarize.py", "backtest/metrics.py"],
    },
    "backtest_runs": {
        "description": "Summary metrics for each backtest execution.",
        "pk": "backtest_run_id",
        "important_cols": [
            "run_id", "as_of_date", "lookback_days", "forward_days",
            "tickers_tested", "hit_rate", "avg_return", "metrics_json",
        ],
        "source": "backtest/smoke_test.py",
        "freshness": "one row per backtest run",
        "consumers": ["learning/summarize.py"],
    },
    "model_runs": {
        "description": "Experimental ML model training run metadata (XGBoost ranker).",
        "pk": "model_run_id",
        "important_cols": [
            "run_id", "model_type", "target",
            "train_start_date", "train_end_date",
            "metrics_json", "feature_importance_json",
        ],
        "source": "models/xgboost_ranker.py",
        "freshness": "one row per training run",
        "consumers": ["learning/summarize.py", "scoring/tiers.py (promotion gates)"],
    },
    "llm_runs": {
        "description": "Audit log of every LLM API call (provider, model, cost, IO hashes).",
        "pk": "llm_run_id",
        "important_cols": [
            "run_id", "ticker", "job_type", "provider", "model",
            "prompt_version", "input_hash", "output_hash",
            "estimated_tokens", "estimated_cost", "status",
        ],
        "source": "llm/runner.py, missed/catalyst_classifier.py",
        "freshness": "appended on each LLM call",
        "consumers": ["health/operational.py", "health/source_status.py"],
    },
    "alerts": {
        "description": "Outbound alert log (Telegram, email) with dedup keys.",
        "pk": "alert_id",
        "important_cols": [
            "run_id", "ticker", "channel", "alert_type",
            "status", "dedupe_key", "sent_at",
        ],
        "source": "notifications/telegram.py, notifications/email.py",
        "freshness": "appended when alerts are sent",
        "consumers": ["main.py (notify command)"],
    },
    "pipeline_runs": {
        "description": "Top-level summary of each full daily-radar pipeline execution.",
        "pk": "pipeline_run_id",
        "important_cols": [
            "run_id", "run_date", "pipeline_type", "universe_size",
            "sources_succeeded", "sources_failed", "candidates_scored",
            "tier_a", "tier_b", "tier_c", "rejected",
            "hypotheses_created", "alerts_sent", "status",
        ],
        "source": "pipelines/daily_radar.py",
        "freshness": "one row per pipeline run",
        "consumers": ["health/operational.py", "dashboard/app.py"],
    },
    "review_notes": {
        "description": "Free-text analyst notes attached to candidates or hypotheses.",
        "pk": "note_id",
        "important_cols": [
            "ticker", "run_id", "hypothesis_id", "note_type", "body", "author",
        ],
        "source": "dashboard/app.py, review/importer.py",
        "freshness": "appended on demand",
        "consumers": ["review/packet_builder.py", "dashboard/app.py"],
    },
    "dashboard_actions": {
        "description": "Audit log of user actions performed via the dashboard.",
        "pk": "action_id",
        "important_cols": [
            "action_type", "target_table", "target_id", "payload_json", "performed_by",
        ],
        "source": "dashboard/app.py",
        "freshness": "appended on each user action",
        "consumers": ["dashboard/app.py"],
    },
    "candidate_reviews": {
        "description": "Structured human review records for scored candidates (usefulness, quality, false-positive reason).",
        "pk": "review_id",
        "important_cols": [
            "candidate_id", "run_id", "ticker", "review_status",
            "usefulness_score", "thesis_quality_score", "evidence_quality_score",
            "false_positive_reason", "missed_risk", "review_notes",
        ],
        "source": "review/importer.py (imported from review packet JSON)",
        "freshness": "imported after manual review cycles",
        "consumers": [
            "learning/summarize.py",
            "health/operational.py",
        ],
    },
    "scorecard_experiments": {
        "description": "Proposed and tested changes to the scoring model, with backtest results.",
        "pk": "experiment_id",
        "important_cols": [
            "hypothesis", "proposed_change_json", "affected_components_json",
            "expected_effect", "backtest_result_json", "status",
            "approved_by", "applied_by", "applied_at",
        ],
        "source": "missed/attribution.py, manual entry via dashboard",
        "freshness": "updated when experiments are proposed, tested, or applied",
        "consumers": ["learning/summarize.py", "dashboard/app.py"],
    },
    "missed_opportunity_events": {
        "description": "Significant price moves (≥15% in 20d) that the system may have missed.",
        "pk": "event_id",
        "important_cols": [
            "ticker", "event_date", "event_type", "return_value",
            "window_days", "reference_price", "peak_price",
            "was_in_universe", "was_scored", "score_before_event",
            "tier_before_event", "was_rejected", "was_incomplete",
            "had_catalyst_evidence", "investigation_status",
        ],
        "source": "missed/detector.py (scans prices_daily)",
        "freshness": "populated by `missed detect`; clustered at 7-day window",
        "consumers": [
            "missed/investigator.py",
            "missed/catalyst_sampler.py",
            "missed/report.py",
        ],
    },
    "missed_opportunity_investigations": {
        "description": "Root-cause investigation records for each missed-opportunity event.",
        "pk": "investigation_id",
        "important_cols": [
            "event_id", "ticker", "event_date", "root_causes_json",
            "primary_root_cause", "text_enrichment_needed",
            "summary", "experiment_proposed",
        ],
        "source": "missed/investigator.py",
        "freshness": "written after `missed investigate`",
        "consumers": ["missed/report.py", "missed/attribution.py"],
    },
    "missed_opportunity_root_causes": {
        "description": "Individual root-cause tags per missed-opportunity investigation.",
        "pk": "rc_id",
        "important_cols": [
            "investigation_id", "ticker", "event_date", "root_cause", "confidence", "evidence",
        ],
        "source": "missed/investigator.py",
        "freshness": "written alongside investigations",
        "consumers": ["missed/report.py"],
    },
    "promotion_gate_results": {
        "description": "Pass/fail results for each model promotion gate check.",
        "pk": "gate_result_id",
        "important_cols": [
            "experiment_id", "model_run_id", "gate_name",
            "status", "metric_value", "threshold", "passed",
        ],
        "source": "scoring/tiers.py",
        "freshness": "written when model promotion gates are evaluated",
        "consumers": ["governance/scorecard_registry.py"],
    },
    "health_checks": {
        "description": "Time-series log of health check results (pass/warn/fail).",
        "pk": "id",
        "important_cols": [
            "run_id", "check_name", "status", "severity", "message",
        ],
        "source": "health/checks.py",
        "freshness": "appended on each `main.py health` run",
        "consumers": ["health/checks.py"],
    },
}

# ── Static catalog: flat-file patterns in data/processed/ ─────────────────────

FILE_CATALOG: list[dict] = [
    {
        "pattern": "daily_catalyst_queue_enriched.jsonl",
        "format": "JSONL",
        "description": (
            "Latest daily catalyst queue — one record per near-threshold ticker, "
            "with LLM enrichment fields (catalyst_type, materiality, sentiment, "
            "shadow score projection)."
        ),
        "consumers": [
            "main.py (missed shadow)",
            "missed/catalyst_queue.py",
            "review/server.py",
        ],
    },
    {
        "pattern": "daily_catalyst_queue.csv",
        "format": "CSV",
        "description": (
            "Tabular version of the daily catalyst queue for human review. "
            "Columns: ticker, event_date, filing_form_type, shadow_score, tier_move, etc."
        ),
        "consumers": ["review/server.py", "manual review workflow"],
    },
    {
        "pattern": "daily_catalyst_queue.md",
        "format": "Markdown",
        "description": "Human-readable formatted daily catalyst queue report.",
        "consumers": ["email digest", "review/server.py"],
    },
    {
        "pattern": "daily_catalyst_queue.html",
        "format": "HTML",
        "description": "HTML version of the daily catalyst queue, served by review server.",
        "consumers": ["review/server.py"],
    },
    {
        "pattern": "daily_catalyst_queue_cache.jsonl",
        "format": "JSONL",
        "description": (
            "LLM response cache for the daily catalyst queue — keyed by input hash. "
            "Avoids redundant API calls on re-runs."
        ),
        "consumers": ["missed/catalyst_classifier.py"],
    },
    {
        "pattern": "catalyst_queue_history/*/daily_catalyst_queue_enriched.jsonl",
        "format": "JSONL",
        "description": "Archived daily catalyst queue enriched JSONL, one folder per run date.",
        "consumers": ["review/server.py (history endpoint)"],
    },
    {
        "pattern": "catalyst_queue_history/*/daily_catalyst_queue.csv",
        "format": "CSV",
        "description": "Archived daily catalyst queue CSV, one folder per run date.",
        "consumers": ["review/server.py (history endpoint)"],
    },
    {
        "pattern": "catalyst_queue_history/*/manual_review.csv",
        "format": "CSV",
        "description": "Manual analyst review annotations for an archived queue run.",
        "consumers": ["review/server.py"],
    },
    {
        "pattern": "catalyst_queue_history/*/run_metadata.json",
        "format": "JSON",
        "description": "Run metadata (provider, n, score range, timestamp) for an archived queue run.",
        "consumers": ["review/server.py"],
    },
    {
        "pattern": "catalyst_llm_pilot_enriched.jsonl",
        "format": "JSONL",
        "description": (
            "LLM enrichment output from the catalyst pilot experiment — "
            "missed-opportunity events classified with catalyst_type and should_affect_score."
        ),
        "consumers": ["main.py (missed shadow)", "missed/catalyst_shadow_scorer.py"],
    },
    {
        "pattern": "catalyst_llm_pilot_sample.jsonl",
        "format": "JSONL",
        "description": "Input sample drawn from missed_opportunity_events for the pilot experiment.",
        "consumers": ["main.py (missed pilot --report-only)"],
    },
    {
        "pattern": "catalyst_llm_pilot_review.csv",
        "format": "CSV",
        "description": "Human-review CSV generated by the catalyst pilot report.",
        "consumers": ["manual review workflow"],
    },
    {
        "pattern": "catalyst_near_threshold_enriched.jsonl",
        "format": "JSONL",
        "description": "Near-threshold variant enriched JSONL from the pilot (score 40–44.9 Reject tickers).",
        "consumers": ["main.py (missed shadow)"],
    },
    {
        "pattern": "catalyst_shadow_score_rows.csv",
        "format": "CSV",
        "description": "Per-ticker shadow score projection rows from the shadow scoring experiment.",
        "consumers": ["main.py (missed shadow)"],
    },
    {
        "pattern": "catalyst_shadow_score_report.md",
        "format": "Markdown",
        "description": "Markdown summary of the shadow scoring experiment results.",
        "consumers": ["manual review"],
    },
]

# ── Date-column map: tables that have a natural date column for range queries ──

_DATE_COL: dict[str, str] = {
    "prices_daily": "trade_date",
    "fundamentals_raw": "as_of_date",
    "fundamentals_features": "as_of_date",
    "filings": "filing_date",
    "events": "event_date",
    "scores": "as_of_date",
    "macro_series": "as_of_date",
    "short_interest": "settlement_date",
    "candidate_outcomes": "as_of_date",
    "backtest_runs": "as_of_date",
    "missed_opportunity_events": "event_date",
    "missed_opportunity_investigations": "event_date",
    "missed_opportunity_root_causes": "event_date",
    "pipeline_runs": "run_date",
    "health_checks": "created_at",
    "source_runs": "started_at",
    "llm_runs": "created_at",
    "alerts": "sent_at",
}


def get_db_table_stats(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Return a list of dicts with live row count and date coverage per table."""
    try:
        tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
    except Exception:
        return []

    rows = []
    for tbl in sorted(tables):
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except Exception:
            count = 0

        date_min = date_max = None
        date_col = _DATE_COL.get(tbl)
        if date_col:
            try:
                result = conn.execute(
                    f"SELECT MIN({date_col}), MAX({date_col}) FROM {tbl}"
                ).fetchone()
                if result:
                    date_min = str(result[0]) if result[0] else None
                    date_max = str(result[1]) if result[1] else None
            except Exception:
                pass

        rows.append({
            "table": tbl,
            "row_count": count,
            "date_min": date_min,
            "date_max": date_max,
        })

    return rows


def _count_lines(path: str, has_header: bool = False) -> int:
    """Count non-empty lines in a text file. Subtract 1 if has_header."""
    try:
        with open(path) as fh:
            total = sum(1 for line in fh if line.strip())
        return max(0, total - (1 if has_header else 0))
    except Exception:
        return 0


def get_file_stats(base_dir: str = "data/processed") -> list[dict]:
    """Scan data/processed/ and return one dict per file with row count."""
    results = []
    base = Path(base_dir)
    if not base.exists():
        return []

    # Build a pattern→catalog lookup
    cat_lookup: dict[str, dict] = {}
    for entry in FILE_CATALOG:
        cat_lookup[entry["pattern"]] = entry

    # Walk all files
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in {".jsonl", ".csv", ".md", ".json", ".html"}:
            continue

        rel = str(path.relative_to(base))
        name = path.name
        fmt = path.suffix.lstrip(".").upper()

        # Match catalog entry by pattern (simple name match or fnmatch glob)
        catalog_entry = None
        for pat, entry in cat_lookup.items():
            import fnmatch
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
                catalog_entry = entry
                break

        row_count = 0
        if fmt == "JSONL":
            row_count = _count_lines(str(path))
        elif fmt == "CSV":
            row_count = _count_lines(str(path), has_header=True)

        results.append({
            "path": str(path),
            "rel_path": rel,
            "format": fmt,
            "size_bytes": path.stat().st_size,
            "row_count": row_count,
            "description": catalog_entry["description"] if catalog_entry else "",
            "consumers": catalog_entry["consumers"] if catalog_entry else [],
        })

    return results


def build_inventory(
    conn: duckdb.DuckDBPyConnection,
    base_dir: str = "data/processed",
) -> tuple[list[dict], list[dict]]:
    """Return (tables, files) — each a list of dicts with full metadata."""
    db_stats = {row["table"]: row for row in get_db_table_stats(conn)}

    tables = []
    for tbl, meta in TABLE_CATALOG.items():
        stat = db_stats.get(tbl, {})
        tables.append({
            "table": tbl,
            "description": meta["description"],
            "pk": meta["pk"],
            "important_cols": meta["important_cols"],
            "source": meta["source"],
            "freshness": meta["freshness"],
            "consumers": meta["consumers"],
            "row_count": stat.get("row_count", 0),
            "date_min": stat.get("date_min"),
            "date_max": stat.get("date_max"),
        })

    # Add any DB tables not in catalog (defensive)
    catalogued = set(TABLE_CATALOG.keys())
    for tbl, stat in db_stats.items():
        if tbl not in catalogued:
            tables.append({
                "table": tbl,
                "description": "(not in catalog)",
                "pk": "unknown",
                "important_cols": [],
                "source": "unknown",
                "freshness": "unknown",
                "consumers": [],
                "row_count": stat.get("row_count", 0),
                "date_min": stat.get("date_min"),
                "date_max": stat.get("date_max"),
            })

    files = get_file_stats(base_dir=base_dir)
    return tables, files


def write_markdown(
    tables: list[dict],
    files: list[dict],
    output_path: str,
) -> None:
    """Write a human-readable data inventory markdown file."""
    from datetime import date as _date
    lines = [
        "# MHDE Data Inventory",
        "",
        f"_Generated: {_date.today().isoformat()}_",
        "",
        "---",
        "",
        "## Database Tables",
        "",
        f"Database: `data/mhde.duckdb`  |  Total tables: {len(tables)}",
        "",
    ]

    for t in sorted(tables, key=lambda x: x["table"]):
        row_label = f"{t['row_count']:,}" if t["row_count"] is not None else "N/A"
        date_range = ""
        if t.get("date_min") and t.get("date_max"):
            date_range = f"{t['date_min']} → {t['date_max']}"
        elif t.get("date_min"):
            date_range = f"from {t['date_min']}"

        lines += [
            f"### `{t['table']}`",
            "",
            t["description"],
            "",
            f"- **Primary key:** `{t['pk']}`",
            f"- **Row count:** {row_label}",
        ]
        if date_range:
            lines.append(f"- **Date coverage:** {date_range}")
        lines += [
            f"- **Source:** {t['source']}",
            f"- **Freshness:** {t['freshness']}",
        ]
        if t["important_cols"]:
            cols_str = ", ".join(f"`{c}`" for c in t["important_cols"])
            lines.append(f"- **Key columns:** {cols_str}")
        if t["consumers"]:
            cons_str = ", ".join(f"`{c}`" for c in t["consumers"])
            lines.append(f"- **Consumers:** {cons_str}")
        lines.append("")

    lines += [
        "---",
        "",
        "## Flat Files",
        "",
        f"Base directory: `data/processed/`  |  Files tracked: {len(files)}",
        "",
    ]

    for f in files:
        size_kb = f["size_bytes"] / 1024
        row_label = f"{f['row_count']:,}" if f["row_count"] else "N/A"
        lines += [
            f"### `{f['rel_path']}`",
            "",
            f["description"] or "(no catalog entry)",
            "",
            f"- **Format:** {f['format']}",
            f"- **Size:** {size_kb:.1f} KB",
            f"- **Rows:** {row_label}",
        ]
        if f["consumers"]:
            cons_str = ", ".join(f"`{c}`" for c in f["consumers"])
            lines.append(f"- **Consumers:** {cons_str}")
        lines.append("")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text("\n".join(lines))


def write_csv(
    tables: list[dict],
    files: list[dict],
    output_path: str,
) -> None:
    """Write a summary CSV with one row per asset (table or file)."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "asset_type", "name", "description", "source_or_format",
        "row_count", "date_min", "date_max", "freshness", "consumers",
    ]

    rows = []
    for t in tables:
        rows.append({
            "asset_type": "db_table",
            "name": t["table"],
            "description": t["description"],
            "source_or_format": t["source"],
            "row_count": t["row_count"] if t["row_count"] is not None else "",
            "date_min": t.get("date_min") or "",
            "date_max": t.get("date_max") or "",
            "freshness": t["freshness"],
            "consumers": "; ".join(t["consumers"]),
        })

    for f in files:
        rows.append({
            "asset_type": "flat_file",
            "name": f["rel_path"],
            "description": f["description"],
            "source_or_format": f["format"],
            "row_count": f["row_count"] if f["row_count"] else "",
            "date_min": "",
            "date_max": "",
            "freshness": "generated on demand",
            "consumers": "; ".join(f["consumers"]),
        })

    with open(output_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
```

- [ ] **Step 2: Run the failing tests to confirm they now pass**

```bash
venv/bin/python -m pytest tests/test_inventory.py -v 2>&1 | tail -30
```

Expected: all tests pass.

- [ ] **Step 3: Commit the module**

```bash
git add storage/inventory.py
git commit -m "feat: add storage/inventory.py data catalog and stats module"
```

---

## Task 3: Add `data inventory` CLI command to `main.py`

**Files:**
- Modify: `main.py` (add after the `learn` group, before the `review` group)

- [ ] **Step 1: Add the `data` group and `inventory` subcommand**

Open `main.py`. After the `learn` group definition (around line 310, after `learn_summarize`), insert the following block:

```python
@cli.group()
def data():
    """Data inventory and inspection commands."""


@data.command("inventory")
@click.option("--docs-out", default="docs/data_inventory.md", show_default=True,
              help="Path for the markdown inventory output.")
@click.option("--csv-out", default="data/processed/data_inventory_summary.csv",
              show_default=True, help="Path for the CSV summary output.")
@click.option("--base-dir", default="data/processed", show_default=True,
              help="Base directory to scan for flat files.")
def data_inventory(docs_out, csv_out, base_dir):
    """Generate a complete data inventory: DB tables + flat files.

    Writes docs/data_inventory.md and data/processed/data_inventory_summary.csv.
    """
    from storage.inventory import build_inventory, write_markdown, write_csv

    cfg, conn = _engine_setup()
    try:
        click.echo("Building inventory...")
        tables, files = build_inventory(conn, base_dir=base_dir)
        click.echo(f"  DB tables : {len(tables)}")
        click.echo(f"  Flat files: {len(files)}")

        write_markdown(tables, files, docs_out)
        click.echo(f"  Markdown  : {docs_out}")

        write_csv(tables, files, csv_out)
        click.echo(f"  CSV       : {csv_out}")

        total_rows = sum(t["row_count"] or 0 for t in tables)
        click.echo(f"\nTotal DB rows across all tables: {total_rows:,}")
    finally:
        conn.close()
```

The insertion point is between the `learn_summarize` command (around line ~320) and the `review` group definition (around line ~325).

- [ ] **Step 2: Verify the CLI command is wired correctly**

```bash
venv/bin/python main.py data --help 2>&1
```

Expected output includes:
```
Usage: main.py data [OPTIONS] COMMAND [ARGS]...

  Data inventory and inspection commands.

Commands:
  inventory  Generate a complete data inventory: DB tables + flat files.
```

```bash
venv/bin/python main.py data inventory --help 2>&1
```

Expected output includes `--docs-out`, `--csv-out`, `--base-dir` options.

- [ ] **Step 3: Run the command end-to-end**

```bash
venv/bin/python main.py data inventory 2>&1
```

Expected output:
```
Building inventory...
  DB tables : 29
  Flat files: <N>
  Markdown  : docs/data_inventory.md
  CSV       : data/processed/data_inventory_summary.csv

Total DB rows across all tables: <N>
```

- [ ] **Step 4: Verify output files exist and look correct**

```bash
head -30 docs/data_inventory.md 2>&1
```

Expected: starts with `# MHDE Data Inventory` and has today's date.

```bash
head -3 data/processed/data_inventory_summary.csv 2>&1
```

Expected: CSV header row `asset_type,name,description,...` followed by data rows.

- [ ] **Step 5: Commit**

```bash
git add main.py docs/data_inventory.md data/processed/data_inventory_summary.csv
git commit -m "feat: add 'main.py data inventory' CLI command and generate initial inventory"
```

---

## Task 4: Add CLI integration test and run full test suite

**Files:**
- Modify: `tests/test_inventory.py` (append integration test)

- [ ] **Step 1: Add the CLI integration test**

Append to `tests/test_inventory.py`:

```python
# ── CLI integration test ───────────────────────────────────────────────────────

from click.testing import CliRunner
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
```

- [ ] **Step 2: Run the full test suite**

```bash
venv/bin/python -m pytest tests/test_inventory.py -v 2>&1 | tail -30
```

Expected: all tests pass.

- [ ] **Step 3: Run the broader test suite to check for regressions**

```bash
venv/bin/python -m pytest tests/ -x -q 2>&1 | tail -20
```

Expected: no regressions; all previously passing tests still pass.

- [ ] **Step 4: Final commit**

```bash
git add tests/test_inventory.py
git commit -m "test: add CLI integration test for data inventory command"
```

---

## Self-Review

**Spec coverage check:**

| Requirement | Task |
|-------------|------|
| Inspect database schema and data/processed artifacts | Task 2 `get_db_table_stats` + `get_file_stats` |
| List every table for each data category (universe, prices, …) | Task 2 `TABLE_CATALOG` covers all 29 tables |
| Document path/table, PKs, important cols, row counts, date coverage, source, freshness, consumers | Task 2 `TABLE_CATALOG` + `build_inventory()` |
| Produce `docs/data_inventory.md` | Task 2 `write_markdown`, Task 3 |
| Produce `data/processed/data_inventory_summary.csv` | Task 2 `write_csv`, Task 3 |
| Add `main.py data inventory` CLI command | Task 3 |
| Do not change scoring logic | ✅ No scoring files touched |
| Run tests | Task 4 |

**Placeholder scan:** No TBDs, TODOs, or vague instructions found. All code blocks contain complete implementations.

**Type consistency:**
- `get_db_table_stats(conn)` → `list[dict]` ✅
- `get_file_stats(base_dir)` → `list[dict]` ✅
- `build_inventory(conn, base_dir)` → `tuple[list[dict], list[dict]]` ✅
- `write_markdown(tables, files, output_path)` → `None` ✅
- `write_csv(tables, files, output_path)` → `None` ✅
- All test assertions match the actual function signatures ✅
