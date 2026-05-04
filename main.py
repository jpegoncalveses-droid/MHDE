#!/usr/bin/env python3
"""MHDE — Market Hypothesis Discovery Engine — CLI entry point."""

from __future__ import annotations

import sys
import click

from runner.config_loader import load_settings, load_tickers
from runner.logger import setup_logging
from runner.runner import ValidationRunner
from runner.reporter import Reporter


@click.group()
def cli():
    """MHDE — Market Hypothesis Discovery Engine."""


# ── Existing validation harness (preserved exactly) ───────────────────────────

@cli.command()
@click.option("--source", multiple=True, help="Run only these sources (repeatable).")
@click.option("--ticker", multiple=True, help="Override ticker basket (repeatable).")
@click.option("--settings", default=None, help="Path to settings.yaml.")
@click.option("--tickers-file", default=None, help="Path to tickers.yaml.")
@click.option("--output-dir", default=None, help="Override output directory.")
@click.option("--dry-run", is_flag=True, help="Load config and list adapters, don't fetch.")
def validate(source, ticker, settings, tickers_file, output_dir, dry_run):
    """Run source validation against the ticker basket."""
    cfg = load_settings(settings)
    setup_logging(cfg)

    if output_dir:
        cfg.setdefault("outputs", {})["dir"] = output_dir
        cfg["outputs"]["samples_dir"] = f"{output_dir}/samples"

    tickers = load_tickers(tickers_file)
    if ticker:
        ticker_set = {t.upper() for t in ticker}
        tickers = [t for t in tickers if t["ticker"] in ticker_set]
        if not tickers:
            click.echo(f"No tickers matched: {ticker_set}", err=True)
            sys.exit(1)

    source_filter = list(source) if source else None

    if dry_run:
        click.echo("Dry run — adapters that would run:")
        all_sources = ["sec_edgar", "polygon", "alpha_vantage", "company_ir", "nasdaq_earnings"]
        for s in (source_filter or all_sources):
            click.echo(f"  {s}")
        click.echo(f"Tickers: {[t['ticker'] for t in tickers]}")
        return

    runner = ValidationRunner(
        settings=cfg,
        tickers=tickers,
        source_filter=source_filter,
    )
    click.echo("Running validation...")
    results = runner.run()

    output_dir_path = cfg.get("outputs", {}).get("dir", "outputs")
    reporter = Reporter(output_dir=output_dir_path)
    paths = reporter.write_all(results)

    click.echo(f"\nValidation complete. {len(results)} use-case pairs tested.")
    click.echo("Output files:")
    for p in paths:
        click.echo(f"  {p}")

    click.echo("\nResults summary:")
    click.echo(f"{'Source':<20} {'Use Case':<22} {'Access':<12} {'Status'}")
    click.echo("-" * 75)
    for r in results:
        click.echo(
            f"{r.source:<20} {r.use_case:<22} {r.access_result:<12} {r.final_status}"
        )


# ── Engine helpers ─────────────────────────────────────────────────────────────

def _engine_setup():
    """Load engine config and initialize DB. Returns (cfg, conn)."""
    import logging
    from storage.config import load_engine_config
    from storage.db import get_connection
    from storage.migrations import run_migrations

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-30s %(levelname)-8s %(message)s",
    )
    cfg = load_engine_config()
    conn = get_connection(cfg["db_path"])
    run_migrations(conn)
    return cfg, conn


# ── Engine commands ────────────────────────────────────────────────────────────

@cli.group()
def run():
    """Run engine pipelines."""


@run.command("daily-radar")
@click.option("--max-symbols", default=None, type=int,
              help="Cap universe to N symbols for dev/test runs.")
@click.option("--skip-sec-fundamentals", is_flag=True,
              help="Skip XBRL fundamentals fetch (use cached data).")
@click.option("--skip-ingestion", is_flag=True,
              help="Skip all data ingestion (score from cached data). Useful for smoke tests.")
@click.option("--incremental", is_flag=True, default=True,
              help="Skip sources with fresh data (default: on).")
def daily_radar(max_symbols, skip_sec_fundamentals, skip_ingestion, incremental):
    """Run the full daily opportunity discovery pipeline."""
    from pipelines.daily_radar import run as pipeline_run
    cfg, conn = _engine_setup()
    if max_symbols is not None:
        cfg.setdefault("universe", {})["max_symbols"] = max_symbols
    if skip_sec_fundamentals:
        cfg.setdefault("ingestion", {})["skip_sec_fundamentals"] = True
    if skip_ingestion:
        cfg.setdefault("ingestion", {})["skip_all_ingestion"] = True
    cfg.setdefault("ingestion", {})["incremental"] = incremental
    try:
        pipeline_run(cfg, conn)
    finally:
        conn.close()


@run.command("weekly-review")
def weekly_review():
    """Generate weekly performance and pipeline review."""
    from pipelines.weekly_review import run as pipeline_run
    cfg, conn = _engine_setup()
    try:
        pipeline_run(cfg, conn)
    finally:
        conn.close()


@cli.command()
@click.argument("target", default="all")
@click.option("--dry-run", is_flag=True, help="Show what would be ingested without fetching.")
def ingest(target, dry_run):
    """Ingest data from all active sources (or specify a source name)."""
    from ingestion.orchestrator import run_all
    cfg, conn = _engine_setup()
    try:
        run_all(conn, cfg, target=target, dry_run=dry_run)
    finally:
        conn.close()


@cli.command()
def score():
    """Compute feature scores and rank the current universe."""
    import uuid
    from features.feature_builder import build_features
    from scoring.scorecard import compute_scores
    from scoring.ranker import rank_tickers

    cfg, conn = _engine_setup()
    run_id = uuid.uuid4().hex[:16]
    try:
        rows = conn.execute(
            "SELECT ticker FROM companies WHERE is_active = true"
        ).fetchall()
        tickers = [r[0] for r in rows]
        if not tickers:
            click.echo("No companies in universe. Run 'ingest all' first.")
            return
        click.echo(f"Scoring {len(tickers)} tickers (run_id={run_id})...")
        build_features(conn, run_id, tickers, cfg)
        compute_scores(conn, run_id, tickers, cfg)
        ranked = rank_tickers(conn, run_id)
        click.echo(f"\nTop candidates ({len(ranked)} scored):")
        for row in ranked[:10]:
            click.echo(f"  [{row['tier']:>6}] {row['ticker']:<8} score={row['total_score']:.1f}")
    finally:
        conn.close()


@cli.command()
def brief():
    """Run LLM briefs on top-ranked candidates."""
    import uuid
    from llm.runner import run_briefs

    cfg, conn = _engine_setup()
    run_id = uuid.uuid4().hex[:16]
    try:
        rows = conn.execute(
            "SELECT * FROM hypotheses WHERE status = 'new' ORDER BY total_score DESC LIMIT 10"
        ).fetchall()
        if not rows:
            click.echo("No open hypotheses. Run 'score' first.")
            return
        click.echo(f"Running LLM briefs for {len(rows)} candidates...")
        cols = [d[0] for d in conn.description]
        hypotheses = [dict(zip(cols, r)) for r in rows]
        run_briefs(conn, run_id, hypotheses, cfg)
    finally:
        conn.close()


@cli.command()
def notify():
    """Send alerts for A-tier candidates via configured channels."""
    from notifications.telegram import TelegramNotifier
    from notifications.email import EmailNotifier

    cfg, conn = _engine_setup()
    try:
        rows = conn.execute(
            "SELECT * FROM hypotheses WHERE tier = 'A' AND status = 'new' ORDER BY total_score DESC"
        ).fetchall()
        click.echo(f"Found {len(rows)} A-tier candidates.")
        cols = [d[0] for d in conn.description]
        candidates = [dict(zip(cols, r)) for r in rows]
        tg = TelegramNotifier(cfg, conn)
        em = EmailNotifier(cfg, conn)
        sent = 0
        for c in candidates:
            if tg.send_alert(c):
                sent += 1
        run_summary = {"candidates": candidates, "run_id": "manual", "sent": sent}
        em.send_digest(run_summary)
        click.echo(f"Alerts sent: {sent}")
    finally:
        conn.close()


@cli.group()
def backtest():
    """Backtesting commands."""


@backtest.command("smoke")
def backtest_smoke():
    """Run a smoke backtest on available historical data."""
    from backtest.smoke_test import run_smoke

    cfg, conn = _engine_setup()
    try:
        run_smoke(conn, cfg)
    finally:
        conn.close()


@cli.group()
def train():
    """Model training commands."""


@train.command("xgboost-smoke")
def xgboost_smoke():
    """Run experimental XGBoost smoke training."""
    from models.xgboost_ranker import train_smoke

    cfg, conn = _engine_setup()
    try:
        train_smoke(conn, cfg)
    finally:
        conn.close()


@cli.command()
def health():
    """Run health checks and show system status."""
    import uuid
    from health.checks import run_all_checks, overall_status

    cfg, conn = _engine_setup()
    run_id = uuid.uuid4().hex[:16]
    try:
        results = run_all_checks(conn, run_id, cfg)
        click.echo(f"\n{'Check':<38} {'Status':<22} {'Sev':<8} Message")
        click.echo("-" * 100)
        for r in results:
            click.echo(
                f"{r['check_name']:<38} {r['status']:<22} {r.get('severity', ''):<8} {r.get('message', '')}"
            )

        passed = [r for r in results if r["status"] == "pass"]
        warned = [r for r in results if r["status"] == "warn"]
        failed = [r for r in results if r["status"] == "fail"]
        skipped = [r for r in results if r["status"] == "skip"]
        status = overall_status(results)

        click.echo(f"\n{'='*100}")
        click.echo(
            f"Overall: {status}  "
            f"({len(passed)} pass, {len(warned)} warn, {len(failed)} fail, {len(skipped)} skip)"
        )
        if status == "FAIL":
            click.echo("ACTION REQUIRED: fix failing checks before running the pipeline.")
        elif status == "PASS_WITH_WARNINGS":
            click.echo("System is operational. Warnings indicate immature or unconfigured components.")
        else:
            click.echo("All checks passed.")
    finally:
        conn.close()


@cli.group()
def learn():
    """Learning loop commands: calibration, insights, experiments."""


@learn.command("summarize")
@click.option("--output", default="outputs", help="Output directory or file path.")
def learn_summarize(output):
    """Generate a learning calibration report from outcome and review data."""
    from learning.summarize import write_learning_report

    cfg, conn = _engine_setup()
    try:
        path = write_learning_report(conn, output)
        click.echo(f"Learning report written: {path}")
    finally:
        conn.close()


_GOVERNANCE_AUDIT_LOG = "data/processed/signal_governance_audit.jsonl"


@learn.command("propose-signal")
@click.option("--signal-name", required=True, help="Name of the signal/feature flag.")
@click.option("--evidence-period", required=True, help="Date range of evidence, e.g. '2025-01-01 to 2026-01-01'.")
@click.option("--sample-size", required=True, type=int, help="Number of events in evidence.")
@click.option("--precision", required=True, type=float, help="Precision of the signal (0–1).")
@click.option("--recall", required=True, type=float, help="Recall of the signal (0–1).")
@click.option("--avg-return", required=True, type=float, help="Average outcome return.")
@click.option("--rollback-criteria", required=True, help="Conditions under which to roll back.")
@click.option("--actor", default="cli", show_default=True, help="Who is proposing.")
@click.option("--audit-path", default=_GOVERNANCE_AUDIT_LOG, show_default=True)
def learn_propose_signal(
    signal_name, evidence_period, sample_size, precision, recall,
    avg_return, rollback_criteria, actor, audit_path,
):
    """Propose a scoring signal for governance review."""
    from governance.signal_governance import create_proposal

    pid = create_proposal(
        signal_name=signal_name,
        evidence_period=evidence_period,
        sample_size=sample_size,
        precision=precision,
        recall=recall,
        avg_return=avg_return,
        rollback_criteria=rollback_criteria,
        audit_path=audit_path,
        actor=actor,
    )
    click.echo(f"Proposal created: {pid}")
    click.echo(f"Audit log: {audit_path}")


@learn.command("approve-signal")
@click.argument("proposal_id")
@click.option("--actor", default="cli", show_default=True)
@click.option("--audit-path", default=_GOVERNANCE_AUDIT_LOG, show_default=True)
def learn_approve_signal(proposal_id, actor, audit_path):
    """Approve a signal proposal (still requires feature flag enable in config)."""
    from governance.signal_governance import approve_proposal

    approve_proposal(proposal_id, actor=actor, audit_path=audit_path)
    click.echo(f"Approved: {proposal_id}")
    click.echo("Next step: set the feature flag to true in config/settings.yaml.")


@learn.command("rollback-signal")
@click.argument("proposal_id")
@click.option("--reason", required=True, help="Why the signal is being rolled back.")
@click.option("--actor", default="cli", show_default=True)
@click.option("--audit-path", default=_GOVERNANCE_AUDIT_LOG, show_default=True)
def learn_rollback_signal(proposal_id, reason, actor, audit_path):
    """Roll back an approved signal and record the reason."""
    from governance.signal_governance import rollback_proposal

    rollback_proposal(proposal_id, reason=reason, actor=actor, audit_path=audit_path)
    click.echo(f"Rolled back: {proposal_id}")
    click.echo("Next step: set the feature flag to false in config/settings.yaml.")


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

    Output paths are controlled by --docs-out and --csv-out.
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


@data.command("universe-stats")
def data_universe_stats():
    """Show universe composition: active count, primary count, sector coverage."""
    cfg, conn = _engine_setup()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE is_active = true"
        ).fetchone()[0]
        primary = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE is_active = true AND universe_tier = 'primary'"
        ).fetchone()[0]
        sectors = conn.execute(
            "SELECT COUNT(DISTINCT sector) FROM companies "
            "WHERE is_active = true AND sector IS NOT NULL"
        ).fetchone()[0]
        null_sector = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE is_active = true AND sector IS NULL"
        ).fetchone()[0]
        click.echo(f"Active companies : {total}")
        click.echo(f"Primary tier     : {primary}")
        click.echo(f"Distinct sectors : {sectors}")
        click.echo(f"Null sector      : {null_sector}")
    finally:
        conn.close()


@data.command("enrich-ticker-details")
@click.option("--db-path", default="data/mhde.duckdb", show_default=True,
              help="DuckDB file path.")
@click.option("--delay", default=0.12, type=float, show_default=True,
              help="Seconds between SEC EDGAR API calls (default ~8 req/sec).")
def data_enrich_ticker_details(db_path, delay):
    """Enrich companies table with market_cap derived from SEC EDGAR XBRL.

    No API key required. Fetches CommonStockSharesOutstanding from SEC EDGAR
    and multiplies by latest close price from prices_daily.
    Only processes tickers with a non-null CIK and active_sec_reporter != false.
    """
    from universe.ticker_details_enricher import run_enrichment

    result = run_enrichment(db_path=db_path, delay=delay)
    click.echo(f"Ticker details enrichment: {result}")


@data.command("incomplete-diagnostics")
@click.option("--db-path", default="data/mhde.duckdb", show_default=True)
@click.option("--output", default="data/processed/incomplete_score_diagnostics.csv",
              show_default=True, help="CSV output path.")
def data_incomplete_diagnostics(db_path, output):
    """Report why tickers are scored Incomplete in the latest run."""
    import collections
    import duckdb
    from scoring.incomplete_diagnostics import diagnose_incomplete, write_diagnostics_csv

    conn = duckdb.connect(db_path, read_only=True)
    try:
        diagnostics = diagnose_incomplete(conn)
    finally:
        conn.close()

    write_diagnostics_csv(diagnostics, output)
    counts = collections.Counter(d.reason for d in diagnostics)
    click.echo(f"Incomplete tickers: {len(diagnostics)}")
    for reason, count in counts.most_common():
        click.echo(f"  {reason}: {count}")
    if diagnostics:
        click.echo(f"Written to {output}")


@data.command("coverage-report")
@click.option("--db-path", default="data/mhde.duckdb", show_default=True)
@click.option("--output-dir", default="data/processed", show_default=True)
def coverage_report_cmd(db_path, output_dir):
    """Write data coverage report (MD + CSV) for all active tickers."""
    from health.coverage_report import generate_coverage_report
    result = generate_coverage_report(db_path=db_path, output_dir=output_dir)
    s = result["summary"]
    click.echo(f"Total: {s['total']}  Fresh: {s['fresh']}  Stale: {s['stale']}  Missing: {s['missing']}")
    click.echo(f"Has fundamentals: {s['has_fundamentals']}  Has market_cap: {s['has_market_cap']}")
    click.echo(f"Written: {result['md']}  {result['csv']}")


@data.command("priority-refresh-queue")
@click.option("--db-path", default="data/mhde.duckdb", show_default=True)
@click.option("--output", default="data/processed/priority_refresh_queue.csv", show_default=True)
@click.option("--max-tickers", default=100, type=int, show_default=True)
@click.option("--enriched-csv", default="data/processed/prediction_vs_actual_enriched_rows.csv", show_default=True)
def data_priority_refresh_queue_cmd(db_path, output, max_tickers, enriched_csv):
    """Build priority refresh queue -- tickers ordered by data staleness."""
    import csv as _csv
    import os as _os
    import duckdb as _duckdb
    from ingestion.priority_refresh import build_priority_queue, save_priority_queue

    price_only_p1: set[str] = set()
    price_only_p2: set[str] = set()
    polygon_missing: set[str] = set()
    if _os.path.exists(enriched_csv):
        with open(enriched_csv, newline="") as f:
            for row in _csv.DictReader(f):
                rc = row.get("enriched_root_cause", "")
                clf = row.get("classification", "")
                if rc == "price_only_scored":
                    if clf == "true_miss":
                        price_only_p1.add(row["ticker"])
                    elif clf in ("near_threshold", "scored_missed"):
                        price_only_p2.add(row["ticker"])
                elif rc == "polygon_fundamentals_missing" and clf in ("true_miss", "near_threshold", "scored_missed"):
                    polygon_missing.add(row["ticker"])

    conn = _duckdb.connect(db_path, read_only=True)
    queue = build_priority_queue(
        conn, max_tickers=max_tickers,
        price_only_p1_tickers=price_only_p1,
        price_only_p2_tickers=price_only_p2,
        polygon_missing_tickers=polygon_missing,
    )
    conn.close()
    save_priority_queue(queue, output)
    by_priority: dict[int, int] = {}
    for item in queue:
        by_priority.setdefault(item["priority"], 0)
        by_priority[item["priority"]] += 1
    if price_only_p1 or price_only_p2:
        click.echo(f"  price_only_scored_miss P1: {len(price_only_p1)}  P2: {len(price_only_p2)}")
    if polygon_missing:
        click.echo(f"  polygon_fundamentals_missing_miss: {len(polygon_missing)}")
    click.echo(f"Priority queue: {len(queue)} tickers -> {output}")
    for p in sorted(by_priority):
        click.echo(f"  Priority {p}: {by_priority[p]} tickers")


@data.command("sector-diagnostics")
@click.option("--db-path", default="data/mhde.duckdb", show_default=True)
@click.option(
    "--enriched-csv",
    default="data/processed/prediction_vs_actual_enriched_rows.csv",
    show_default=True,
)
def data_sector_diagnostics_cmd(db_path, enriched_csv):
    """Show sector cluster diagnostics for missed sector cluster move events."""
    import csv as _csv
    import os as _os
    import duckdb as _duckdb
    from health.sector_diagnostics import generate_sector_diagnostics

    if not _os.path.exists(enriched_csv):
        click.echo(f"No enriched CSV: {enriched_csv}")
        click.echo("Run: python main.py missed refresh-learning")
        return
    with open(enriched_csv, newline="") as f:
        enriched_rows = list(_csv.DictReader(f))
    conn = _duckdb.connect(db_path, read_only=True)
    diags = generate_sector_diagnostics(conn, enriched_rows)
    conn.close()
    if not diags:
        click.echo("No sector_cluster_move rows found in enriched CSV.")
        return
    click.echo(f"Sector Cluster Diagnostics — {len(diags)} rows")
    click.echo(f"{'Ticker':<8} {'Event Date':<12} {'Sector':<26} {'ETF':<6} {'ETF Prices':<11} Subcause")
    click.echo("-" * 80)
    for d in diags:
        click.echo(
            f"{d.ticker:<8} {d.event_date:<12} {(d.sector or '—'):<26} "
            f"{(d.etf_ticker or '—'):<6} {d.etf_price_count:<11} {d.subcause}"
        )


@data.command("ingest-sector-etfs")
@click.option("--db-path", default="data/mhde.duckdb", show_default=True)
@click.option("--date", "trade_date", default=None, help="Trade date YYYY-MM-DD (default: today).")
@click.option("--lookback-days", default=1, type=int, show_default=True,
              help="Number of recent trading days to fetch (1 = today only).")
def data_ingest_sector_etfs_cmd(db_path, trade_date, lookback_days):
    """Fetch sector ETF 1-day returns from Polygon and store in prices_daily."""
    import datetime
    import os as _os
    import duckdb as _duckdb
    from ingestion.ingest_sector_etfs import ingest_sector_etfs_to_db, SECTOR_ETFS

    api_key = _os.environ.get("POLYGON_API_KEY")
    if not api_key:
        click.echo("ERROR: POLYGON_API_KEY not set in environment.", err=True)
        raise SystemExit(1)

    if trade_date:
        dates = [trade_date]
    else:
        today = datetime.date.today()
        dates = []
        d = today
        while len(dates) < lookback_days:
            if d.weekday() < 5:  # Mon–Fri
                dates.append(str(d))
            d -= datetime.timedelta(days=1)
        dates.reverse()

    click.echo(f"Fetching {len(SECTOR_ETFS)} sector ETFs for {len(dates)} date(s): {', '.join(dates)}")
    total = 0
    for dt in dates:
        n = ingest_sector_etfs_to_db(db_path, dt, api_key)
        click.echo(f"  {dt}: {n} ETF rows written")
        total += n
    click.echo(f"Done. Total rows written: {total}")


@cli.group()
def review():
    """Candidate review commands: build review packets, import completed reviews."""


@review.command("packet")
@click.option("--run-id", default=None, help="Run ID to build packet from (default: latest).")
@click.option("--output", default="outputs", help="Output directory.")
@click.option("--suffix", default="", help="Optional suffix for output filename (e.g. 'post_stooq').")
def review_packet(run_id, output, suffix):
    """Generate a structured review packet for candidate quality assessment."""
    from review.packet_builder import build_packet, write_packet

    cfg, conn = _engine_setup()
    try:
        packet = build_packet(conn, run_id=run_id)
        md_path, json_path = write_packet(packet, output_dir=output, stem_suffix=suffix)
        click.echo(f"\nReview packet generated")
        click.echo(f"  Run ID:     {packet.run_id}")
        click.echo(f"  Run date:   {packet.run_date}")
        click.echo(f"  Markdown:   {md_path}")
        click.echo(f"  JSON:       {json_path}")

        xref = packet.meta.get("cross_reference_table", [])
        high = [r for r in xref if r["review_priority"] == "high"]
        med  = [r for r in xref if r["review_priority"] == "medium"]
        c_tickers = [c["ticker"] for c in packet.sections.get("c_tier", [])]

        click.echo(f"\nSection counts:")
        for key, candidates in packet.sections.items():
            click.echo(f"  {key:<35} {len(candidates)}")
        click.echo(f"\nReview priority: high={len(high)}, medium={len(med)}, low={len(xref)-len(high)-len(med)}")
        if c_tickers:
            click.echo(f"C-tier: {', '.join(c_tickers)}")

        if packet.warnings:
            click.echo("\nWarnings:")
            for w in packet.warnings:
                click.echo(f"  - {w}")
    finally:
        conn.close()


@review.command("import")
@click.argument("packet_path")
def review_import(packet_path):
    """Import completed review fields from a review packet JSON into candidate_reviews."""
    from review.importer import import_packet

    cfg, conn = _engine_setup()
    try:
        result = import_packet(conn, packet_path)
        click.echo(f"\nReview import complete")
        click.echo(f"  Run ID:           {result['run_id']}")
        click.echo(f"  Imported:         {result['imported']}")
        click.echo(f"  Skipped (pending):{result['skipped_pending']}")
        click.echo(f"  Skipped (dup):    {result['skipped_duplicate']}")
        click.echo(f"  Failed:           {result['failed']}")
    finally:
        conn.close()


@cli.group()
def missed():
    """Missed-opportunity detection, investigation, and reporting."""


@missed.command("detect")
@click.option("--lookback-days", default=90, type=int,
              help="Days of price history to scan (default: 90).")
@click.option("--persist", is_flag=True, default=True,
              help="Write detected events to DB (default: on).")
def missed_detect(lookback_days, persist):
    """Scan prices_daily for significant price moves and detect missed opportunities."""
    from missed.detector import detect_missed_opportunities, persist_events, cluster_events

    cfg, conn = _engine_setup()
    try:
        events = detect_missed_opportunities(conn, lookback_days=lookback_days)
        clustered = cluster_events(events)
        unique_tickers = len({e["ticker"] for e in clustered})
        click.echo(
            f"Detected {len(events)} raw events → {len(clustered)} clustered "
            f"({unique_tickers} unique tickers)."
        )
        if persist and clustered:
            n = persist_events(conn, clustered)
            click.echo(f"Persisted {n} events to missed_opportunity_events.")
        for e in clustered[:10]:
            click.echo(
                f"  {e['ticker']}: {e['event_type']} +{e['return_value']:.1f}% "
                f"on {e['event_date']} (tier_before={e.get('tier_before_event') or 'N/A'})"
            )
    finally:
        conn.close()


@missed.command("investigate")
def missed_investigate():
    """Investigate all pending missed-opportunity events and assign root causes."""
    from missed.investigator import investigate_all_pending

    cfg, conn = _engine_setup()
    try:
        n = investigate_all_pending(conn)
        click.echo(f"Investigated {n} missed-opportunity events.")
    finally:
        conn.close()


@missed.command("report")
@click.option("--output", default="outputs", help="Output directory.")
def missed_report(output):
    """Generate markdown + JSON missed-opportunity report."""
    from missed.report import generate_report

    cfg, conn = _engine_setup()
    try:
        md_path, json_path = generate_report(conn, output_dir=output)
        click.echo(f"Missed opportunities report written:")
        click.echo(f"  Markdown: {md_path}")
        click.echo(f"  JSON:     {json_path}")
    finally:
        conn.close()


@missed.command("run")
@click.option("--lookback-days", default=90, type=int,
              help="Days of price history to scan.")
@click.option("--output", default="outputs", help="Output directory.")
def missed_run(lookback_days, output):
    """Run full missed-opportunity pipeline: detect → investigate → report."""
    from missed.detector import detect_missed_opportunities, persist_events
    from missed.investigator import investigate_all_pending
    from missed.attribution import propose_experiments_from_misses
    from missed.report import generate_report

    from missed.detector import cluster_events

    cfg, conn = _engine_setup()
    try:
        events = detect_missed_opportunities(conn, lookback_days=lookback_days)
        clustered = cluster_events(events)
        unique_tickers = len({e["ticker"] for e in clustered})
        click.echo(
            f"Detected {len(events)} raw events → {len(clustered)} clustered "
            f"({unique_tickers} unique tickers)."
        )
        persist_events(conn, clustered)

        n_investigated = investigate_all_pending(conn)
        click.echo(f"Investigated {n_investigated} events.")

        proposals = propose_experiments_from_misses(conn)
        if proposals:
            click.echo(f"Proposed {len(proposals)} experiments from attribution.")

        md_path, json_path = generate_report(conn, output_dir=output)
        click.echo(f"Report: {md_path}")
    finally:
        conn.close()


@missed.command("prediction-vs-actual")
@click.option("--output-dir", default="data/processed", show_default=True,
              help="Directory for output artifacts.")
@click.option("--lookback-days", default=90, type=int, show_default=True,
              help="Days of events to include.")
def missed_prediction_vs_actual(output_dir, lookback_days):
    """Daily learning report: MHDE predictions vs actual movers."""
    from missed.prediction_report import generate_prediction_report

    cfg, conn = _engine_setup()
    try:
        md_path, csv_path, jsonl_path = generate_prediction_report(
            conn, output_dir=output_dir, lookback_days=lookback_days
        )
        click.echo("Prediction-vs-actual report written:")
        click.echo(f"  Markdown: {md_path}")
        click.echo(f"  CSV:      {csv_path}")
        click.echo(f"  JSONL:    {jsonl_path}")
    finally:
        conn.close()


@missed.command("enrich-root-causes")
@click.option(
    "--input", "input_csv",
    default="data/processed/prediction_vs_actual_rows.csv",
    show_default=True,
    help="Path to prediction-vs-actual CSV (output of 'missed prediction-vs-actual').",
)
@click.option(
    "--output-dir", default="data/processed", show_default=True,
    help="Directory for enriched CSV and markdown report.",
)
def missed_enrich_root_causes(input_csv, output_dir):
    """Deterministic root-cause enrichment for prediction-vs-actual rows.

    Reads the prediction CSV, joins DB tables (scores components, fundamentals,
    events, companies), assigns 11 structured root-cause labels, and writes
    two artifacts: an enriched CSV and a markdown summary report.
    No LLM, no new data sources, no production scores changed.
    """
    import csv as csv_mod
    from pathlib import Path
    from datetime import date
    from missed.root_cause_enrichment import enrich_rows, generate_enrichment_report

    input_path = Path(input_csv)
    if not input_path.exists():
        raise click.ClickException(
            f"Input CSV not found: {input_csv}\n"
            "Run 'missed prediction-vs-actual' first to generate it."
        )

    with open(input_path, newline="") as f:
        reader = csv_mod.DictReader(f)
        raw_rows = list(reader)

    # Coerce types that were serialised as strings in the CSV
    for r in raw_rows:
        for numeric_field in ("return_value", "score_before_event", "priority_score"):
            val = r.get(numeric_field)
            if val not in (None, "", "None"):
                try:
                    r[numeric_field] = float(val)
                except ValueError:
                    r[numeric_field] = None
            else:
                r[numeric_field] = None
        for bool_field in ("was_in_universe", "was_scored", "had_catalyst_evidence"):
            r[bool_field] = r.get(bool_field, "").lower() in ("true", "1", "yes")
        event_date_str = r.get("event_date")
        if event_date_str not in (None, "", "None"):
            r["event_date"] = date.fromisoformat(event_date_str)
        win = r.get("window_days")
        if win not in (None, "", "None"):
            try:
                r["window_days"] = int(win)
            except ValueError:
                r["window_days"] = None

    cfg, conn = _engine_setup()
    try:
        enriched = enrich_rows(raw_rows, conn)
        csv_path, md_path = generate_enrichment_report(enriched, output_dir=output_dir)
        click.echo("Root-cause enrichment report written:")
        click.echo(f"  Enriched CSV: {csv_path}")
        click.echo(f"  Markdown:     {md_path}")
        click.echo(f"  Rows enriched: {len(enriched)}")
    finally:
        conn.close()


@missed.command("refresh-learning")
@click.option("--output-dir", default="data/processed", show_default=True,
              help="Directory for output artifacts.")
@click.option("--lookback-days", default=90, type=int, show_default=True,
              help="Days of events to include in prediction-vs-actual.")
@click.option("--db-path", default="data/mhde.duckdb", show_default=True,
              help="DuckDB path for freshness check.")
def missed_refresh_learning(output_dir, lookback_days, db_path):
    """Re-run prediction-vs-actual then enrich-root-causes in correct order.

    Checks PvA freshness first and warns if already up-to-date.
    Enforces pipeline order: PvA must complete before enrichment runs.
    No scoring changes.
    """
    import os
    import csv as csv_mod
    from pathlib import Path
    from datetime import date as _date
    from health.pva_freshness import check_pva_freshness
    from missed.prediction_report import generate_prediction_report
    from missed.root_cause_enrichment import enrich_rows, generate_enrichment_report

    pva_csv = os.path.join(output_dir, "prediction_vs_actual_rows.csv")
    freshness = check_pva_freshness(db_path=db_path, pva_csv_path=pva_csv)
    if not freshness.is_stale:
        click.echo(f"PvA already current: {freshness.reason}")
        click.echo("Re-running anyway to ensure consistency.")
    else:
        click.secho(f"PvA stale: {freshness.reason}", fg="yellow")

    # Step 1: prediction-vs-actual
    click.echo("\n[1/2] Running prediction-vs-actual...")
    cfg, conn = _engine_setup()
    try:
        md_path, csv_path, jsonl_path = generate_prediction_report(
            conn, output_dir=output_dir, lookback_days=lookback_days
        )
        click.echo(f"  Written: {csv_path}")
    finally:
        conn.close()

    # Step 2: enrich-root-causes (reads CSV just written)
    click.echo("\n[2/2] Running enrich-root-causes...")
    input_path = Path(pva_csv)
    if not input_path.exists():
        raise click.ClickException(f"PvA CSV missing after generation: {pva_csv}")

    with open(input_path, newline="") as f:
        raw_rows = list(csv_mod.DictReader(f))

    for r in raw_rows:
        for numeric_field in ("return_value", "score_before_event", "priority_score"):
            val = r.get(numeric_field)
            if val not in (None, "", "None"):
                try:
                    r[numeric_field] = float(val)
                except ValueError:
                    r[numeric_field] = None
            else:
                r[numeric_field] = None
        for bool_field in ("was_in_universe", "was_scored", "had_catalyst_evidence"):
            r[bool_field] = r.get(bool_field, "").lower() in ("true", "1", "yes")
        event_date_str = r.get("event_date")
        if event_date_str not in (None, "", "None"):
            r["event_date"] = _date.fromisoformat(event_date_str)
        win = r.get("window_days")
        if win not in (None, "", "None"):
            try:
                r["window_days"] = int(win)
            except ValueError:
                r["window_days"] = None

    cfg2, conn2 = _engine_setup()
    try:
        enriched = enrich_rows(raw_rows, conn2)
        csv_e, md_e = generate_enrichment_report(enriched, output_dir=output_dir)
        click.echo(f"  Written: {csv_e}")
        click.echo(f"  Rows enriched: {len(enriched)}")
    finally:
        conn2.close()

    # Final freshness check
    freshness2 = check_pva_freshness(db_path=db_path, pva_csv_path=pva_csv)
    if freshness2.is_stale:
        click.secho(f"\nWarning: PvA still stale after refresh: {freshness2.reason}", fg="yellow")
    else:
        click.secho(f"\nLearning refresh complete. PvA current through {freshness2.pva_max_event_date}.", fg="green")


@missed.command("pilot")
@click.option("--n", default=100, type=int, show_default=True,
              help="Number of events to sample.")
@click.option("--output-dir", default="data/processed", show_default=True,
              help="Directory for pilot artifacts.")
@click.option("--mock/--no-mock", default=True, show_default=True,
              help="Use mock classifier (default). --no-mock requires OPENAI_API_KEY.")
@click.option("--provider", default="openai", show_default=True,
              help="LLM provider to use when --no-mock is set.")
@click.option("--model", default="gpt-4o-mini", show_default=True,
              help="Model name for the real provider.")
@click.option("--cache-path", default="data/processed/catalyst_llm_cache.jsonl",
              show_default=True, help="Path to JSONL cache file.")
@click.option("--refresh-cache", is_flag=True, default=False,
              help="Re-classify all events, ignoring the existing cache.")
@click.option("--report", is_flag=True, default=False,
              help="Generate markdown + CSV review report after classification.")
@click.option("--rpm-limit", default=None, type=int,
              help="Max requests/minute for real provider. Default: 3 for OpenAI.")
@click.option("--include-non-text-forms", is_flag=True, default=False,
              help="Include Form 4, 144, SC 13G etc. in filing context (diagnostic mode).")
@click.option("--allow-empty-source-run", is_flag=True, default=False,
              help="Continue even if no events have resolvable source text.")
@click.option("--report-only", is_flag=True, default=False,
              help="Skip sampling/LLM; re-validate and regenerate report from existing enriched JSONL.")
@click.option("--input-enriched", default=None, type=str,
              help="Path to existing enriched JSONL (required with --report-only).")
@click.option("--input-sample", default=None, type=str,
              help="Path to existing sample JSONL (used with --report-only for event metadata).")
@click.option("--target", default="standard",
              type=click.Choice(["standard", "near-threshold"]), show_default=True,
              help="Sampling mode: 'near-threshold' selects Reject tickers with score 40–44.9.")
def missed_pilot(n, output_dir, mock, provider, model, cache_path, refresh_cache,
                 report, rpm_limit, include_non_text_forms, allow_empty_source_run,
                 report_only, input_enriched, input_sample, target):
    """LLM catalyst enrichment pilot — samples text-evidence events, classifies, writes JSONL."""
    import json
    import os
    from missed.catalyst_sampler import sample_pilot_events
    from missed.catalyst_classifier import classify_events, revalidate_enrichments
    from missed.catalyst_providers import CatalystProviderError, preflight_check
    from missed.catalyst_schema import CatalystEnrichment, validate_enrichment
    from missed.catalyst_source_resolver import (
        compute_source_coverage, enrich_events_with_source,
    )

    # ── --report-only: re-validate existing enriched JSONL, regenerate report ──
    if report_only:
        if not input_enriched:
            click.echo("Error: --report-only requires --input-enriched <path>", err=True)
            sys.exit(1)
        if not os.path.exists(input_enriched):
            click.echo(f"Error: enriched file not found: {input_enriched}", err=True)
            sys.exit(1)

        with open(input_enriched) as f:
            raw_enrichments = [json.loads(line) for line in f if line.strip()]

        revalidated = revalidate_enrichments(raw_enrichments)
        click.echo(f"Revalidated {len(revalidated)} enriched records from {input_enriched}")
        score_affecting = sum(1 for r in revalidated if r.get("should_affect_score"))
        weak = sum(1 for r in revalidated if r.get("validation_status") == "weak_evidence")
        neutral = sum(1 for r in revalidated if r.get("validation_status") == "neutral_sentiment")
        click.echo(f"  final_should_affect_score=True : {score_affecting}")
        click.echo(f"  weak_evidence (overridden)     : {weak}")
        if neutral:
            click.echo(f"  neutral_sentiment (overridden) : {neutral}")

        # Load sample for report metadata (optional)
        sample_for_report: list[dict] = []
        sample_src = input_sample or os.path.join(
            os.path.dirname(input_enriched), "catalyst_llm_pilot_sample.jsonl"
        )
        if os.path.exists(sample_src):
            with open(sample_src) as f:
                sample_for_report = [json.loads(line) for line in f if line.strip()]
        else:
            sample_for_report = [{"event_id": r["event_id"]} for r in revalidated]

        # Convert dicts → CatalystEnrichment for report
        enriched_objs = []
        for r in revalidated:
            try:
                enriched_objs.append(CatalystEnrichment(**{
                    k: v for k, v in r.items() if k != "_cache_key"
                }))
            except Exception:
                pass  # skip malformed records

        os.makedirs(output_dir, exist_ok=True)
        from missed.catalyst_report import generate_pilot_report
        inferred_mode = "near-threshold" if "near_threshold" in (input_enriched or "") else target
        md_path, csv_path = generate_pilot_report(
            sample_for_report, enriched_objs, output_dir, target_mode=inferred_mode,
        )
        click.echo(f"\nReport: {md_path}")
        click.echo(f"Review: {csv_path}")
        return

    cfg, conn = _engine_setup()
    try:
        os.makedirs(output_dir, exist_ok=True)

        # Preflight for real provider: fail before touching data or the cache.
        if not mock:
            api_key = (
                os.environ.get("OPENAI_API_KEY")
                or (cfg or {}).get("llm", {}).get("openai_api_key", "")
            )
            try:
                preflight_check(api_key)
            except CatalystProviderError as e:
                click.echo(f"Error: {e}", err=True)
                sys.exit(1)

        # Default to 3 RPM for OpenAI if caller did not specify.
        effective_rpm = rpm_limit
        if not mock and provider == "openai" and rpm_limit is None:
            effective_rpm = 3

        if target == "near-threshold":
            from missed.catalyst_sampler import sample_near_threshold_events
            click.echo(
                f"\nNear-threshold pilot mode: selecting Reject tickers with score 40.0–44.9"
            )
            sample = sample_near_threshold_events(
                conn, n=n, include_non_text_forms=include_non_text_forms
            )
            if not sample:
                click.echo(
                    "No near-threshold candidates found. "
                    "Run 'missed run' first, or check that scores exist for your universe."
                )
                return
            click.echo(
                f"Near-threshold candidates: {len(sample)} events "
                f"(score range: "
                f"{min(e['current_score'] for e in sample):.1f}–"
                f"{max(e['current_score'] for e in sample):.1f})"
            )
        else:
            sample = sample_pilot_events(conn, n=n, include_non_text_forms=include_non_text_forms)
            if not sample:
                click.echo("No text-evidence events found. Run 'missed run' first.")
                return

        # Resolve source text for each event before writing sample or calling LLM.
        sample = enrich_events_with_source(sample)

        # ── Preflight source coverage report ──────────────────────────────────
        cov = compute_source_coverage(sample)
        click.echo(f"\nSource coverage preflight ({cov['sampled_count']} events sampled):")
        click.echo(f"  Resolvable (≥{200} chars)  : {cov['resolvable_source_count']}")
        click.echo(f"  Non-text filings          : {cov['skipped_non_text_form_count']}")
        click.echo(f"  Missing CIK               : {cov['missing_cik_count']}")
        click.echo(f"  Missing accession number  : {cov['missing_accession_count']}")
        click.echo(f"  Missing primary doc       : {cov['missing_primary_doc_count']}")
        click.echo(f"  PDF (not supported)       : {cov['pdf_not_supported_count']}")
        click.echo(f"  Fetch errors              : {cov['fetch_error_count']}")

        if cov["resolvable_source_count"] == 0:
            if not allow_empty_source_run:
                click.echo(
                    "\nError: 0 events have resolvable source text. "
                    "LLM calls would produce only hallucinated evidence.\n"
                    "Check sampler diagnostics above. "
                    "Use --allow-empty-source-run to proceed anyway (diagnostic only).",
                    err=True,
                )
                sys.exit(1)
            click.echo(
                "\nWarning: 0 resolvable sources. Proceeding due to --allow-empty-source-run.",
                err=True,
            )

        # File prefix differs per target mode to avoid clobbering standard pilot artifacts
        file_prefix = "catalyst_near_threshold" if target == "near-threshold" else "catalyst_llm_pilot"

        sample_path = os.path.join(output_dir, f"{file_prefix}_sample.jsonl")
        with open(sample_path, "w") as f:
            for record in sample:
                row = {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                       for k, v in record.items()}
                f.write(json.dumps(row) + "\n")
        click.echo(f"Sample: {len(sample)} events → {sample_path}")

        try:
            enriched = classify_events(
                sample, use_mock=mock, provider_name=provider, model=model,
                cache_path=cache_path, refresh_cache=refresh_cache, cfg=cfg,
                rpm_limit=effective_rpm,
            )
        except CatalystProviderError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        invalid = 0
        errors_count = 0
        enriched_path = os.path.join(output_dir, f"{file_prefix}_enriched.jsonl")
        with open(enriched_path, "w") as f:
            for ce in enriched:
                d = ce.to_dict()
                is_valid, errs = validate_enrichment(d)
                if not is_valid:
                    invalid += 1
                if "[ERROR]" in ce.reasoning_short:
                    errors_count += 1
                f.write(ce.to_jsonl_line() + "\n")

        provider_label = "mock" if mock else f"{provider}/{model}"
        click.echo(f"Enriched: {len(enriched)} events [{provider_label}] → {enriched_path}")
        if invalid:
            click.echo(f"  Warning: {invalid} records failed schema validation")
        if errors_count:
            click.echo(f"  Classification errors: {errors_count} (see [ERROR] in reasoning_short)")

        catalyst_counts: dict[str, int] = {}
        score_affecting = 0
        for ce in enriched:
            catalyst_counts[ce.catalyst_type] = catalyst_counts.get(ce.catalyst_type, 0) + 1
            if ce.should_affect_score:
                score_affecting += 1

        click.echo(f"\nCatalyst breakdown ({provider_label}):")
        for ctype, count in sorted(catalyst_counts.items(), key=lambda x: -x[1]):
            click.echo(f"  {ctype:<25} {count}")
        click.echo(f"\nshould_affect_score=True (final actionable): {score_affecting}/{len(enriched)}")
        click.echo("\nNote: scores table is unchanged. This is a read-only pilot.")

        # ── Near-threshold shadow scoring projection ───────────────────────────
        if target == "near-threshold":
            from missed.catalyst_shadow_scorer import compute_shadow_scores
            scores_for_shadow = [
                {
                    "run_id": e.get("current_run_id", ""),
                    "ticker": e["ticker"],
                    "total_score": float(e.get("current_score") or 0.0),
                    "catalyst_score": float(e.get("current_catalyst_score") or 30.0),
                    "risk_penalty": float(e.get("current_risk_penalty") or 20.0),
                    "tier": e.get("current_tier", "Reject"),
                }
                for e in sample if e.get("current_score") is not None
            ]
            enrichment_dicts = [ce.to_dict() for ce in enriched]
            shadow_rows = compute_shadow_scores(enrichment_dicts, scores_for_shadow)
            crossings = [r for r in shadow_rows if r.get("tier_move")]
            adj_count = sum(1 for r in shadow_rows if r["llm_adjustment"] != 0.0)
            click.echo(f"\nShadow scoring projection (near-threshold):")
            click.echo(f"  Tickers with LLM adjustment   : {adj_count}")
            click.echo(f"  Potential tier crossings       : {len(crossings)}")
            for r in crossings:
                click.echo(
                    f"    {r['ticker']}: {r['tier_move']}"
                    f" (score {r['original_total']:.1f} → {r['shadow_total']:.1f})"
                )
            if not crossings:
                click.echo("    (none — no actionable catalysts crossed the C-tier threshold)")

        if report:
            from missed.catalyst_report import generate_pilot_report
            md_path, csv_path = generate_pilot_report(
                sample, enriched, output_dir,
                target_mode=target,
            )
            click.echo(f"\nReport: {md_path}")
            click.echo(f"Review: {csv_path}")
    finally:
        conn.close()


@missed.command("shadow")
@click.option("--input-enriched", default="data/processed/catalyst_llm_pilot_enriched.jsonl",
              show_default=True, help="Path to enriched JSONL.")
@click.option("--output-dir", default="data/processed", show_default=True,
              help="Directory for shadow score artifacts.")
def missed_shadow(input_enriched, output_dir):
    """Shadow scoring experiment: measure LLM catalyst impact without touching production scores."""
    import json
    import os
    from missed.catalyst_classifier import revalidate_enrichments
    from missed.catalyst_shadow_scorer import compute_shadow_scores, generate_shadow_report

    if not os.path.exists(input_enriched):
        click.echo(f"Error: enriched file not found: {input_enriched}", err=True)
        sys.exit(1)

    with open(input_enriched) as f:
        raw = [json.loads(line) for line in f if line.strip()]
    enrichments = revalidate_enrichments(raw)
    click.echo(f"Loaded {len(enrichments)} enriched records (revalidated)")

    cfg, conn = _engine_setup()
    try:
        cur = conn.execute(
            "SELECT run_id, ticker, total_score, catalyst_score, risk_penalty, tier, confidence"
            " FROM scores"
            " ORDER BY run_id DESC, total_score DESC"
        )
        cols = [d[0] for d in cur.description]
        scores = [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()

    click.echo(f"Loaded {len(scores)} score rows from DB")
    if not scores:
        click.echo("Warning: no scores in DB — run 'main.py run daily-radar' first.", err=True)

    rows = compute_shadow_scores(enrichments, scores)
    actionable = sum(1 for r in rows if r["llm_adjustment"] != 0.0)
    crossings = sum(1 for r in rows if r["shadow_tier"] != r["original_tier"])
    click.echo(f"Shadow rows: {len(rows)} | with adjustment: {actionable} | tier crossings: {crossings}")

    md_path, csv_path = generate_shadow_report(rows, output_dir)
    click.echo(f"\nReport: {md_path}")
    click.echo(f"Rows:   {csv_path}")


@missed.command("daily-catalyst-queue")
@click.option("--n", default=50, show_default=True, help="Max near-threshold events to sample.")
@click.option("--score-min", default=40.0, show_default=True, help="Lower bound of near-threshold score range.")
@click.option("--score-max", default=44.9, show_default=True, help="Upper bound of near-threshold score range.")
@click.option("--max-events-per-ticker", default=1, show_default=True,
              help="Max events to keep per ticker (1 = one unique ticker per slot).")
@click.option("--mock/--no-mock", default=True, show_default=True,
              help="Use mock provider (no API calls). Pass --no-mock to use real LLM.")
@click.option("--provider", default="nvidia", show_default=True,
              help="LLM provider (nvidia or openai). Only used with --no-mock.")
@click.option("--model", default="meta/llama-3.3-70b-instruct", show_default=True,
              help="Model name. Only used with --no-mock.")
@click.option("--output-dir", default="data/processed", show_default=True,
              help="Directory for queue artifacts.")
@click.option("--cache-path", default=None, help="Path to JSONL cache file (avoids re-classifying).")
@click.option("--refresh-cache", is_flag=True, default=False, help="Ignore existing cache entries.")
@click.option("--rpm-limit", default=None, type=int, help="Max requests/min for real provider.")
@click.option("--history-root", default=None,
              help="Archive artifacts to history_root/YYYY-MM-DD/.")
@click.option("--html", "write_html", is_flag=True, default=False,
              help="Also write HTML report artifact.")
@click.option("--send-email", is_flag=True, default=False,
              help="Send email digest (opt-in).")
@click.option("--email-to", default=None, envvar="DAILY_CATALYST_EMAIL_TO",
              help="Email recipient (overrides DAILY_CATALYST_EMAIL_TO).")
def missed_daily_catalyst_queue(n, score_min, score_max, max_events_per_ticker, mock, provider, model,
                                output_dir, cache_path, refresh_cache, rpm_limit,
                                history_root, write_html, send_email, email_to):
    """Build the daily shadow catalyst review queue for near-threshold Reject tickers.

    Samples tickers with score in [score_min, score_max] and tier=Reject, resolves
    SEC source text, classifies with LLM, revalidates sufficiency/sentiment rules,
    and projects shadow score changes.  No production scores are written.

    Artifacts written to output_dir:
      daily_catalyst_queue.md
      daily_catalyst_queue.csv
      daily_catalyst_queue_enriched.jsonl
    """
    import os
    from missed.catalyst_queue import build_daily_queue, generate_queue_report

    cfg, conn = _engine_setup()
    provider_label = "mock" if mock else provider
    try:
        click.echo(
            f"Building daily catalyst queue: n={n}, score {score_min:.1f}–{score_max:.1f}"
            f", provider={provider_label}"
        )
        entries, revalidated, metadata = build_daily_queue(
            conn,
            n=n,
            score_min=score_min,
            score_max=score_max,
            max_events_per_ticker=max_events_per_ticker,
            use_mock=mock,
            provider_name=provider,
            model=model,
            cache_path=cache_path,
            refresh_cache=refresh_cache,
            cfg=cfg,
            rpm_limit=rpm_limit,
        )
    finally:
        conn.close()

    if not entries:
        click.echo("No near-threshold tickers found — nothing to queue.")
        return

    metadata["score_min"] = score_min
    metadata["score_max"] = score_max
    metadata["provider"] = provider_label

    promoted = [e for e in entries if e["final_should_affect_score"]]
    crossings = [e for e in promoted if e.get("tier_move") and "→C" in e["tier_move"]]
    click.echo(f"Sampled: {metadata.get('sampled', 0)} events")
    click.echo(f"Source available: {metadata.get('source_available', 0)}/{metadata.get('sampled', 0)}")
    click.echo(f"Valid + actionable: {len(promoted)}")
    click.echo(f"Reject→C crossings: {len(crossings)}")

    for r in crossings:
        click.echo(
            f"  {r['ticker']:6}: {r['original_tier']}→{r['shadow_tier']}"
            f" ({r['original_score']:.1f} → {r['shadow_score']:.1f}, {r['llm_adjustment']:+.1f})"
        )
    if not crossings:
        click.echo("  (none in this run)")

    os.makedirs(output_dir, exist_ok=True)

    html_path = None
    if write_html:
        from missed.catalyst_queue import generate_html_report
        html_path = generate_html_report(entries, revalidated, output_dir, run_metadata=metadata)
        click.echo(f"HTML   : {html_path}")

    md_path, csv_path, jsonl_path = generate_queue_report(
        entries, revalidated, output_dir, run_metadata=metadata,
        history_root=history_root, html_path=html_path,
    )
    click.echo(f"\nReport : {md_path}")
    click.echo(f"CSV    : {csv_path}")
    click.echo(f"JSONL  : {jsonl_path}")

    if send_email:
        from missed.catalyst_digest import send_catalyst_digest, write_digest_artifacts
        write_digest_artifacts(entries, revalidated, metadata, output_dir)
        ok = send_catalyst_digest(cfg, entries, revalidated, metadata, email_to=email_to or "")
        if ok:
            click.echo(f"Digest sent to {email_to}")
        else:
            click.echo("Digest send failed — check logs.")

    click.echo("\nNote: scores table is unchanged. This is a shadow-only analysis.")


@missed.command("review-server")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Bind host. Use 127.0.0.1 (default) behind a reverse proxy.")
@click.option("--port", default=8765, show_default=True, help="Bind port.")
@click.option("--history-root", default="data/processed/catalyst_queue_history",
              show_default=True, help="Directory containing dated run archives.")
@click.option("--output-dir", default="data/processed", show_default=True,
              help="Directory with latest artifacts.")
@click.option("--unsafe-no-auth", is_flag=True, default=False,
              help="Disable auth. Local testing only.")
@click.option("--unsafe-public-bind", is_flag=True, default=False,
              help="Allow 0.0.0.0 bind. Requires a TLS reverse proxy in front.")
def missed_review_server(host, port, history_root, output_dir, unsafe_no_auth, unsafe_public_bind):
    """Start the read-only catalyst queue review server."""
    from review.server import run_server
    run_server(host, port, history_root, output_dir,
               unsafe_no_auth=unsafe_no_auth,
               unsafe_public_bind=unsafe_public_bind)


@cli.group(invoke_without_command=True)
@click.pass_context
def dashboard(ctx):
    """Launch the MHDE dashboard (or use subcommands)."""
    if ctx.invoked_subcommand is None:
        import subprocess
        import sys
        click.echo("Starting MHDE dashboard at http://127.0.0.1:8501 ...")
        click.echo("Press Ctrl+C to stop.")
        subprocess.run([
            sys.executable, "-m", "streamlit", "run", "dashboard/app.py",
            "--server.address=127.0.0.1",
            "--server.port=8501",
            "--server.headless=true",
        ])


@dashboard.command("deploy-info")
def dashboard_deploy_info():
    """Print VPS deployment instructions."""
    click.echo("""
Local dashboard:
  streamlit run dashboard/app.py
  — or —
  python main.py dashboard start

VPS service:
  sudo cp deploy/dashboard/mhde-dashboard.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable mhde-dashboard
  sudo systemctl start mhde-dashboard

Reverse proxy:
  Caddy:  deploy/dashboard/Caddyfile.example
  Nginx:  deploy/dashboard/nginx.example.conf

DuckDNS:
  bash deploy/dashboard/duckdns-update.sh
  (requires DUCKDNS_DOMAIN and DUCKDNS_TOKEN in deploy/dashboard/.env)

Security:
  Set MHDE_DASHBOARD_AUTH_ENABLED=true (default)
  Set MHDE_DASHBOARD_PASSWORD_HASH to sha256(your-password)
  Do not expose Streamlit without authentication and HTTPS.

Full guide: deploy/dashboard/README.md
""")


if __name__ == "__main__":
    cli()
