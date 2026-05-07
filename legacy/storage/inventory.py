"""storage/inventory.py — MHDE data catalog and stats module.

Provides TABLE_CATALOG, FILE_CATALOG, and functions to query live stats from
the DuckDB database and flat files in data/processed/.
"""
from __future__ import annotations

import csv
import fnmatch
from pathlib import Path
from typing import Optional

import duckdb

# ---------------------------------------------------------------------------
# TABLE_CATALOG — one entry per DB table
# ---------------------------------------------------------------------------

TABLE_CATALOG: dict[str, dict] = {
    "schema_version": {
        "description": "Tracks applied DB schema migrations by version number.",
        "pk": "version",
        "important_cols": ["version", "applied_at", "description"],
        "source": "storage/migrations.py",
        "freshness": "on schema migration",
        "consumers": ["storage/migrations.py", "storage/db.py"],
    },
    "companies": {
        "description": "Master universe of equities with metadata, sector, market cap, and SEC CIK.",
        "pk": "ticker",
        "important_cols": ["ticker", "cik", "company_name", "exchange", "sector", "market_cap", "universe_tier", "is_active"],
        "source": "ingestion/ingest_prices.py, ingestion/ingest_sec.py",
        "freshness": "daily",
        "consumers": [
            "features/feature_builder.py",
            "features/momentum.py",
            "features/quality.py",
            "features/valuation.py",
            "features/catalyst.py",
            "scoring/scorecard.py",
            "health/operational.py",
            "pipelines/daily_radar.py",
            "missed/catalyst_queue.py",
        ],
    },
    "source_runs": {
        "description": "Audit log of each data ingestion run: source, status, record counts, errors.",
        "pk": "id",
        "important_cols": ["id", "run_id", "source_name", "status", "started_at", "records_inserted", "records_failed"],
        "source": "ingestion/orchestrator.py",
        "freshness": "each pipeline run",
        "consumers": ["health/operational.py", "ingestion/orchestrator.py"],
    },
    "filings": {
        "description": "SEC EDGAR filing index (10-K, 10-Q, 8-K, etc.) per ticker.",
        "pk": "id",
        "important_cols": ["id", "ticker", "cik", "form_type", "filing_date", "accession_number", "doc_url"],
        "source": "SEC EDGAR XBRL API (ingestion/ingest_sec.py)",
        "freshness": "daily",
        "consumers": [
            "features/catalyst.py",
            "features/risk.py",
            "features/filer_utils.py",
            "missed/catalyst_sampler.py",
            "missed/detector.py",
            "missed/investigator.py",
            "review/packet_builder.py",
            "ingestion/ingest_sec.py",
        ],
    },
    "fundamentals_raw": {
        "description": "Raw XBRL concept values from SEC filings (revenue, net income, shares, etc.).",
        "pk": "id",
        "important_cols": ["id", "ticker", "cik", "concept", "value", "as_of_date", "form"],
        "source": "SEC EDGAR XBRL API (ingestion/ingest_sec.py)",
        "freshness": "daily",
        "consumers": ["features/quality.py", "features/valuation.py", "health/operational.py"],
    },
    "fundamentals_features": {
        "description": "Processed fundamental features per ticker per date (revenue growth, net margin, dilution, etc.).",
        "pk": "id",
        "important_cols": ["id", "ticker", "as_of_date", "revenue", "net_income", "revenue_growth_yoy", "net_margin", "pe_proxy"],
        "source": "features/quality.py, features/valuation.py",
        "freshness": "daily",
        "consumers": ["scoring/scorecard.py", "health/operational.py", "features/feature_builder.py"],
    },
    "prices_daily": {
        "description": "Daily OHLCV price data per ticker from Stooq/Polygon.",
        "pk": "id",
        "important_cols": ["id", "ticker", "trade_date", "open", "high", "low", "close", "volume", "adjusted_close"],
        "source": "ingestion/ingest_prices.py (Stooq/Polygon)",
        "freshness": "daily",
        "consumers": [
            "features/momentum.py",
            "features/valuation.py",
            "features/risk.py",
            "features/feature_builder.py",
            "pipelines/daily_radar.py",
            "backtest/historical_replay.py",
            "backtest/labels.py",
            "missed/detector.py",
            "missed/investigator.py",
            "review/packet_builder.py",
        ],
    },
    "macro_series": {
        "description": "Macroeconomic time series (FRED: rates, spreads, VIX, etc.).",
        "pk": "id",
        "important_cols": ["id", "series_id", "series_name", "value", "as_of_date", "frequency", "source"],
        "source": "FRED API (ingestion/ingest_prices.py)",
        "freshness": "daily",
        "consumers": ["features/momentum.py", "health/operational.py"],
    },
    "short_interest": {
        "description": "FINRA short interest data per ticker per settlement date.",
        "pk": "id",
        "important_cols": ["id", "ticker", "settlement_date", "short_interest", "days_to_cover"],
        "source": "FINRA API (ingestion/ingest_finra.py)",
        "freshness": "bi-monthly (FINRA schedule)",
        "consumers": ["features/momentum.py", "health/operational.py"],
    },
    "events": {
        "description": "Catalytic events per ticker: earnings, FDA, conferences, insider buys, etc.",
        "pk": "id",
        "important_cols": ["id", "ticker", "event_type", "event_date", "title", "source", "is_upcoming"],
        "source": "ingestion/ingest_events.py (multiple APIs)",
        "freshness": "daily",
        "consumers": ["features/catalyst.py", "health/operational.py"],
    },
    "features": {
        "description": "Computed feature scores per ticker per run (grouped by feature_group).",
        "pk": "id",
        "important_cols": ["id", "run_id", "ticker", "as_of_date", "feature_group", "feature_name", "feature_value", "feature_score"],
        "source": "features/feature_builder.py",
        "freshness": "each pipeline run",
        "consumers": ["scoring/scorecard.py", "health/operational.py"],
    },
    "scores": {
        "description": "Composite scores per ticker per run (cheap, quality, catalyst, momentum, total) with tier assignment.",
        "pk": "id",
        "important_cols": ["id", "run_id", "ticker", "as_of_date", "total_score", "tier", "cheap_score", "quality_score", "catalyst_score", "momentum_score"],
        "source": "scoring/scorecard.py",
        "freshness": "each pipeline run",
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
            "pipelines/daily_radar.py",
            "learning/calibration.py",
            "main.py (shadow command)",
        ],
    },
    "hypotheses": {
        "description": "Investment theses for top-ranked candidates with structured evidence and status tracking.",
        "pk": "hypothesis_id",
        "important_cols": ["hypothesis_id", "run_id", "ticker", "tier", "total_score", "thesis", "why_now", "status", "review_status"],
        "source": "pipelines/daily_radar.py",
        "freshness": "each pipeline run",
        "consumers": ["review/packet_builder.py", "health/operational.py", "learning/insights.py"],
    },
    "rejections": {
        "description": "Tickers rejected from scoring with reasons and risk flags.",
        "pk": "id",
        "important_cols": ["id", "run_id", "ticker", "reason", "risk_flags_json"],
        "source": "scoring/tiers.py",
        "freshness": "each pipeline run",
        "consumers": ["health/operational.py", "pipelines/daily_radar.py"],
    },
    "candidate_outcomes": {
        "description": "Forward return tracking for scored candidates (1d, 5d, 20d, 60d, 120d returns and drawdowns).",
        "pk": "candidate_id",
        "important_cols": ["candidate_id", "ticker", "as_of_date", "tier", "total_score", "forward_return_20d", "hit_10pct_before_down_10pct", "review_status"],
        "source": "learning/insights.py",
        "freshness": "daily (lookback fill)",
        "consumers": ["learning/insights.py", "review/packet_builder.py", "health/operational.py"],
    },
    "backtest_runs": {
        "description": "Backtest summary results: hit rate, avg return, metrics per run.",
        "pk": "backtest_run_id",
        "important_cols": ["backtest_run_id", "run_id", "as_of_date", "tickers_tested", "hit_rate", "avg_return"],
        "source": "learning/insights.py",
        "freshness": "on demand / pipeline run",
        "consumers": ["learning/insights.py", "health/operational.py"],
    },
    "model_runs": {
        "description": "ML model training run metadata (XGBoost, features, metrics).",
        "pk": "model_run_id",
        "important_cols": ["model_run_id", "run_id", "model_type", "target", "train_start_date", "train_end_date", "metrics_json"],
        "source": "learning/insights.py",
        "freshness": "on demand",
        "consumers": ["learning/insights.py", "health/operational.py"],
    },
    "llm_runs": {
        "description": "LLM inference audit log: provider, model, prompt version, tokens, cost, input/output hashes.",
        "pk": "llm_run_id",
        "important_cols": ["llm_run_id", "run_id", "ticker", "job_type", "provider", "model", "status", "estimated_cost", "created_at"],
        "source": "features/catalyst.py, pipelines/daily_radar.py",
        "freshness": "each pipeline run",
        "consumers": ["health/operational.py", "pipelines/daily_radar.py"],
    },
    "alerts": {
        "description": "Outbound alerts sent per ticker/channel with deduplication keys.",
        "pk": "alert_id",
        "important_cols": ["alert_id", "run_id", "ticker", "channel", "alert_type", "status", "sent_at"],
        "source": "pipelines/daily_radar.py",
        "freshness": "each pipeline run",
        "consumers": ["health/operational.py", "pipelines/daily_radar.py"],
    },
    "pipeline_runs": {
        "description": "High-level pipeline execution log: universe size, sources, candidates scored, tier counts, status.",
        "pk": "pipeline_run_id",
        "important_cols": ["pipeline_run_id", "run_id", "run_date", "pipeline_type", "universe_size", "candidates_scored", "status"],
        "source": "pipelines/daily_radar.py",
        "freshness": "each pipeline run",
        "consumers": ["health/operational.py", "health/data_quality.py", "main.py"],
    },
    "review_notes": {
        "description": "Human analyst notes attached to tickers or hypotheses.",
        "pk": "note_id",
        "important_cols": ["note_id", "ticker", "run_id", "hypothesis_id", "note_type", "body", "author"],
        "source": "review/packet_builder.py (user input)",
        "freshness": "on analyst review",
        "consumers": ["review/packet_builder.py", "health/operational.py"],
    },
    "dashboard_actions": {
        "description": "Audit log of actions taken via the review dashboard (status changes, annotations).",
        "pk": "action_id",
        "important_cols": ["action_id", "action_type", "target_table", "target_id", "payload_json", "performed_by"],
        "source": "review/packet_builder.py (dashboard API)",
        "freshness": "on user action",
        "consumers": ["review/packet_builder.py", "health/operational.py"],
    },
    "candidate_reviews": {
        "description": "Structured analyst reviews of scored candidates: usefulness, thesis quality, evidence quality, false positive reasons.",
        "pk": "review_id",
        "important_cols": ["review_id", "ticker", "run_id", "review_status", "usefulness_score", "thesis_quality_score", "false_positive_reason"],
        "source": "review/packet_builder.py (user input)",
        "freshness": "on analyst review",
        "consumers": ["review/packet_builder.py", "learning/insights.py", "health/operational.py"],
    },
    "scorecard_experiments": {
        "description": "Proposed and tested scoring changes: hypothesis, expected effect, backtest results, approval status.",
        "pk": "experiment_id",
        "important_cols": ["experiment_id", "hypothesis", "proposed_change_json", "status", "backtest_result_json", "applied_at"],
        "source": "learning/insights.py, scoring/scorecard.py",
        "freshness": "on experiment proposal/review",
        "consumers": ["learning/insights.py", "health/operational.py"],
    },
    "missed_opportunity_events": {
        "description": "Detected missed opportunity events: large moves in tickers we did or didn't catch.",
        "pk": "event_id",
        "important_cols": ["event_id", "ticker", "event_date", "event_type", "return_value", "was_scored", "tier_before_event", "investigation_status"],
        "source": "missed/catalyst_queue.py",
        "freshness": "daily",
        "consumers": ["missed/catalyst_queue.py", "health/operational.py"],
    },
    "missed_opportunity_investigations": {
        "description": "Root cause investigations for missed opportunity events, with LLM enrichment status.",
        "pk": "investigation_id",
        "important_cols": ["investigation_id", "event_id", "ticker", "primary_root_cause", "summary", "experiment_proposed"],
        "source": "missed/catalyst_queue.py",
        "freshness": "on investigation",
        "consumers": ["missed/catalyst_queue.py", "health/operational.py"],
    },
    "missed_opportunity_root_causes": {
        "description": "Individual root cause records per missed opportunity investigation.",
        "pk": "rc_id",
        "important_cols": ["rc_id", "investigation_id", "ticker", "event_date", "root_cause", "confidence", "evidence"],
        "source": "missed/catalyst_queue.py",
        "freshness": "on investigation",
        "consumers": ["missed/catalyst_queue.py", "health/operational.py"],
    },
    "promotion_gate_results": {
        "description": "Go/no-go gate results for promoting model or scoring experiments to production.",
        "pk": "gate_result_id",
        "important_cols": ["gate_result_id", "experiment_id", "model_run_id", "gate_name", "status", "metric_value", "threshold", "passed"],
        "source": "learning/insights.py",
        "freshness": "on promotion evaluation",
        "consumers": ["learning/insights.py", "health/operational.py"],
    },
    "health_checks": {
        "description": "Operational health check results: check name, status, severity, message.",
        "pk": "id",
        "important_cols": ["id", "run_id", "check_name", "status", "severity", "message", "created_at"],
        "source": "health/data_quality.py, health/operational.py",
        "freshness": "each pipeline run",
        "consumers": ["main.py", "health/operational.py", "health/data_quality.py"],
    },
}

# ---------------------------------------------------------------------------
# FILE_CATALOG — flat-file patterns in data/processed/
# ---------------------------------------------------------------------------

FILE_CATALOG: list[dict] = [
    {
        "pattern": "daily_catalyst_queue_enriched.jsonl",
        "format": "JSONL",
        "description": "Current daily catalyst queue with LLM-enriched summaries per candidate.",
        "consumers": ["missed/catalyst_queue.py", "pipelines/daily_radar.py", ".claude/local_scripts/regen_daily_queue.py"],
    },
    {
        "pattern": "daily_catalyst_queue.csv",
        "format": "CSV",
        "description": "Current daily catalyst queue as a flat CSV for spreadsheet review.",
        "consumers": ["missed/catalyst_queue.py", "pipelines/daily_radar.py"],
    },
    {
        "pattern": "daily_catalyst_queue.md",
        "format": "MD",
        "description": "Current daily catalyst queue rendered as Markdown for human review.",
        "consumers": ["missed/catalyst_queue.py", "pipelines/daily_radar.py"],
    },
    {
        "pattern": "daily_catalyst_queue.html",
        "format": "HTML",
        "description": "Current daily catalyst queue rendered as HTML for browser review.",
        "consumers": ["missed/catalyst_queue.py", "pipelines/daily_radar.py"],
    },
    {
        "pattern": "daily_catalyst_queue_cache.jsonl",
        "format": "JSONL",
        "description": "LLM response cache for the daily catalyst queue to avoid redundant API calls.",
        "consumers": ["missed/catalyst_queue.py", ".claude/local_scripts/regen_daily_queue.py"],
    },
    {
        "pattern": "catalyst_queue_history/*/daily_catalyst_queue_enriched.jsonl",
        "format": "JSONL",
        "description": "Historical daily catalyst queue enriched files archived by date.",
        "consumers": ["missed/catalyst_queue.py"],
    },
    {
        "pattern": "catalyst_queue_history/*/daily_catalyst_queue.csv",
        "format": "CSV",
        "description": "Historical daily catalyst queue CSV files archived by date.",
        "consumers": ["missed/catalyst_queue.py"],
    },
    {
        "pattern": "catalyst_queue_history/*/manual_review.csv",
        "format": "CSV",
        "description": "Historical manual review CSV files archived by date.",
        "consumers": ["missed/catalyst_queue.py"],
    },
    {
        "pattern": "catalyst_queue_history/*/run_metadata.json",
        "format": "JSON",
        "description": "Run metadata JSON for historical catalyst queue runs.",
        "consumers": ["missed/catalyst_queue.py"],
    },
    {
        "pattern": "catalyst_llm_pilot_enriched.jsonl",
        "format": "JSONL",
        "description": "LLM pilot study enriched candidates for catalyst scoring evaluation.",
        "consumers": ["features/catalyst.py", ".claude/local_scripts/regen_daily_queue.py"],
    },
    {
        "pattern": "catalyst_llm_pilot_sample.jsonl",
        "format": "JSONL",
        "description": "Sample of candidates used in the catalyst LLM pilot study.",
        "consumers": ["features/catalyst.py"],
    },
    {
        "pattern": "catalyst_llm_pilot_review.csv",
        "format": "CSV",
        "description": "Human review results for the catalyst LLM pilot study.",
        "consumers": ["features/catalyst.py", "learning/insights.py"],
    },
    {
        "pattern": "catalyst_near_threshold_enriched.jsonl",
        "format": "JSONL",
        "description": "Near-threshold candidates enriched by LLM for calibration analysis.",
        "consumers": ["features/catalyst.py", "scoring/scorecard.py"],
    },
    {
        "pattern": "catalyst_shadow_score_rows.csv",
        "format": "CSV",
        "description": "Shadow scoring rows for catalyst model evaluation.",
        "consumers": ["scoring/scorecard.py", "features/catalyst.py"],
    },
    {
        "pattern": "catalyst_shadow_score_report.md",
        "format": "MD",
        "description": "Summary report of catalyst shadow scoring results.",
        "consumers": ["scoring/scorecard.py"],
    },
]

# ---------------------------------------------------------------------------
# _DATE_COL — table → date column for MIN/MAX queries
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# get_db_table_stats
# ---------------------------------------------------------------------------

def get_db_table_stats(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Return row counts and date ranges for all DB tables."""
    try:
        rows = conn.execute("SHOW TABLES").fetchall()
        table_names = [r[0] for r in rows]
    except Exception:
        return []

    result = []
    for tbl in table_names:
        row_count = None
        date_min = None
        date_max = None

        try:
            rc = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()
            row_count = rc[0] if rc else 0
        except Exception:
            row_count = 0

        if tbl in _DATE_COL:
            col = _DATE_COL[tbl]
            try:
                dr = conn.execute(f"SELECT MIN({col}), MAX({col}) FROM {tbl}").fetchone()
                if dr:
                    date_min = str(dr[0]) if dr[0] is not None else None
                    date_max = str(dr[1]) if dr[1] is not None else None
            except Exception:
                pass

        result.append({
            "table": tbl,
            "row_count": row_count,
            "date_min": date_min,
            "date_max": date_max,
        })

    return result


# ---------------------------------------------------------------------------
# _count_lines
# ---------------------------------------------------------------------------

def _count_lines(path: str, has_header: bool = False) -> int:
    """Count non-empty lines in a text file, optionally subtracting the header."""
    count = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    count += 1
    except Exception:
        return 0
    if has_header and count > 0:
        count -= 1
    return count


# ---------------------------------------------------------------------------
# get_file_stats
# ---------------------------------------------------------------------------

def get_file_stats(base_dir: str = "data/processed") -> list[dict]:
    """Walk base_dir and return stats for catalogued flat files."""
    base = Path(base_dir)
    if not base.exists():
        return []

    valid_extensions = {".jsonl", ".csv", ".md", ".json", ".html"}

    # Build a lookup: pattern → catalog entry
    catalog_by_pattern = {entry["pattern"]: entry for entry in FILE_CATALOG}

    result = []
    for file_path in base.rglob("*"):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in valid_extensions:
            continue

        rel = str(file_path.relative_to(base))
        name = file_path.name
        ext = file_path.suffix.lower()

        # Match against FILE_CATALOG patterns
        matched_entry = None
        for pat, entry in catalog_by_pattern.items():
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
                matched_entry = entry
                break

        if matched_entry is None:
            continue

        # Determine format
        fmt_map = {
            ".jsonl": "JSONL",
            ".csv": "CSV",
            ".md": "MD",
            ".json": "JSON",
            ".html": "HTML",
        }
        fmt = fmt_map.get(ext, ext.lstrip(".").upper())

        # Count rows
        if ext == ".csv":
            row_count = _count_lines(str(file_path), has_header=True)
        elif ext in {".jsonl", ".md", ".json", ".html"}:
            row_count = _count_lines(str(file_path), has_header=False)
        else:
            row_count = 0

        try:
            size_bytes = file_path.stat().st_size
        except Exception:
            size_bytes = 0

        result.append({
            "path": str(file_path),
            "rel_path": rel,
            "format": fmt,
            "size_bytes": size_bytes,
            "row_count": row_count,
            "description": matched_entry["description"],
            "consumers": matched_entry["consumers"],
        })

    return result


# ---------------------------------------------------------------------------
# build_inventory
# ---------------------------------------------------------------------------

def build_inventory(
    conn: duckdb.DuckDBPyConnection,
    base_dir: str = "data/processed",
) -> tuple[list[dict], list[dict]]:
    """Build combined inventory from DB stats and file stats.

    Returns (tables_list, files_list).
    """
    db_stats_list = get_db_table_stats(conn)
    db_stats = {row["table"]: row for row in db_stats_list}

    tables = []
    # Merge TABLE_CATALOG with live stats
    for tbl, meta in TABLE_CATALOG.items():
        stat = db_stats.get(tbl, {})
        entry = {
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
        }
        tables.append(entry)

    # Add any DB tables not in TABLE_CATALOG
    for tbl, stat in db_stats.items():
        if tbl not in TABLE_CATALOG:
            tables.append({
                "table": tbl,
                "description": "(not in catalog)",
                "pk": "",
                "important_cols": [],
                "source": "",
                "freshness": "",
                "consumers": [],
                "row_count": stat.get("row_count", 0),
                "date_min": stat.get("date_min"),
                "date_max": stat.get("date_max"),
            })

    files = get_file_stats(base_dir=base_dir)

    return (tables, files)


# ---------------------------------------------------------------------------
# write_markdown
# ---------------------------------------------------------------------------

def write_markdown(tables: list[dict], files: list[dict], output_path: str) -> None:
    """Write inventory as Markdown."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    lines = ["# MHDE Data Inventory", ""]
    lines.append("## Database Tables")
    lines.append("")
    for t in tables:
        lines.append(f"### {t['table']}")
        lines.append(f"- **Description**: {t['description']}")
        lines.append(f"- **Primary Key**: `{t['pk']}`")
        lines.append(f"- **Source**: {t['source']}")
        lines.append(f"- **Freshness**: {t['freshness']}")
        lines.append(f"- **Row Count**: {t['row_count']}")
        if t.get("date_min") or t.get("date_max"):
            lines.append(f"- **Date Range**: {t['date_min']} → {t['date_max']}")
        if t.get("consumers"):
            consumers_str = ", ".join(t["consumers"])
            lines.append(f"- **Consumers**: {consumers_str}")
        lines.append("")

    lines.append("## Flat Files")
    lines.append("")
    for f in files:
        lines.append(f"### {f['rel_path']}")
        lines.append(f"- **Format**: {f['format']}")
        lines.append(f"- **Description**: {f['description']}")
        lines.append(f"- **Size**: {f['size_bytes']} bytes")
        lines.append(f"- **Row Count**: {f['row_count']}")
        if f.get("consumers"):
            consumers_str = ", ".join(f["consumers"])
            lines.append(f"- **Consumers**: {consumers_str}")
        lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# write_csv
# ---------------------------------------------------------------------------

def write_csv(tables: list[dict], files: list[dict], output_path: str) -> None:
    """Write inventory as CSV."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "asset_type",
        "name",
        "description",
        "source_or_format",
        "row_count",
        "date_min",
        "date_max",
        "freshness",
        "consumers",
    ]

    rows = []

    for t in tables:
        rows.append({
            "asset_type": "db_table",
            "name": t["table"],
            "description": t["description"],
            "source_or_format": t["source"],
            "row_count": t["row_count"],
            "date_min": t.get("date_min") or "",
            "date_max": t.get("date_max") or "",
            "freshness": t["freshness"],
            "consumers": ";".join(t["consumers"]) if t["consumers"] else "",
        })

    for f in files:
        rows.append({
            "asset_type": "flat_file",
            "name": f["rel_path"],
            "description": f["description"],
            "source_or_format": f["format"],
            "row_count": f["row_count"],
            "date_min": "",
            "date_max": "",
            "freshness": "",
            "consumers": ";".join(f["consumers"]) if f["consumers"] else "",
        })

    with open(str(out), "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
