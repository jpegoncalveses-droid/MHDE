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


@cli.group()
def ml():
    """ML prediction engine commands."""


@ml.command("predict")
@click.option("--date", default=None, help="Prediction date (YYYY-MM-DD). Default: latest.")
@click.option("--skip-outcomes", is_flag=True, help="Skip filling historical outcomes.")
@click.option("--allow-stale-features", is_flag=True,
              help="KI-149: downgrade ml_features-behind-prices_daily from raise to WARNING.")
def ml_predict(date, skip_outcomes, allow_stale_features):
    """Run ML prediction pipeline: score universe, fill outcomes, print results."""
    from datetime import date as date_cls
    from pipelines.ml_prediction_pipeline import run_prediction_pipeline

    cfg, conn = _engine_setup()
    try:
        pred_date = date_cls.fromisoformat(date) if date else None
        run_prediction_pipeline(conn, prediction_date=pred_date,
                                skip_features=True, skip_outcomes=skip_outcomes,
                                allow_stale_features=allow_stale_features)
    finally:
        conn.close()


@ml.command("backfill-labels")
def ml_backfill_labels():
    """Compute ML labels for all historical ticker-dates."""
    import logging
    from ml.labels import compute_labels

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg, conn = _engine_setup()
    try:
        total = compute_labels(conn)
        click.echo(f"Labels computed: {total:,} rows")
    finally:
        conn.close()


@ml.command("backfill-features")
def ml_backfill_features():
    """Compute ML features for all historical ticker-dates."""
    import logging
    from ml.features import compute_features

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg, conn = _engine_setup()
    try:
        total = compute_features(conn)
        click.echo(f"Features computed: {total:,} rows")
    finally:
        conn.close()


@ml.command("train")
@click.option("--label", default="label_20d_5pct", help="Label column to train on.")
@click.option("--horizon", default="20d", help="Prediction horizon.")
@click.option("--threshold", default=0.05, type=float, help="Target threshold.")
def ml_train_cmd(label, horizon, threshold):
    """Train ML model with walk-forward CV."""
    import logging
    from ml.train import train_walk_forward
    from ml.evaluate import print_walk_forward_results

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg, conn = _engine_setup()
    try:
        results = train_walk_forward(conn, label_col=label, horizon=horizon, threshold=threshold)
        print_walk_forward_results(results, label, horizon)
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
    click.echo(f"{'Ticker':<8} {'Date':<12} {'Sector':<22} {'ETF':<6} {'TkrRet':>8} {'ETFRet':>8} {'Rel':>8} {'Peers':>5} Subcause")
    click.echo("-" * 105)
    for d in diags:
        def _pct(v):
            return f"{v*100:+.2f}%" if v is not None else "—"
        click.echo(
            f"{d.ticker:<8} {d.event_date:<12} {(d.sector or '—'):<22} "
            f"{(d.etf_ticker or '—'):<6} {_pct(d.ticker_return):>8} {_pct(d.etf_return):>8} "
            f"{_pct(d.relative_return):>8} {str(d.peer_cluster_count or '—'):>5} {d.subcause}"
        )


@data.command("peer-cluster-diagnostics")
@click.option("--db-path", default="data/mhde.duckdb", show_default=True)
@click.option(
    "--enriched-csv",
    default="data/processed/prediction_vs_actual_enriched_rows.csv",
    show_default=True,
)
def data_peer_cluster_diagnostics_cmd(db_path, enriched_csv):
    """Show peer/theme cluster attribution for sector_cluster_move events."""
    import csv as _csv
    import os as _os
    import duckdb as _duckdb
    from health.peer_cluster_attribution import generate_peer_cluster_diagnostics

    if not _os.path.exists(enriched_csv):
        click.echo(f"No enriched CSV: {enriched_csv}")
        click.echo("Run: python main.py missed refresh-learning")
        return
    with open(enriched_csv, newline="") as f:
        enriched_rows = list(_csv.DictReader(f))
    conn = _duckdb.connect(db_path, read_only=True)
    diags = generate_peer_cluster_diagnostics(conn, enriched_rows)
    conn.close()
    if not diags:
        click.echo("No sector_cluster_move rows found in enriched CSV.")
        return

    def _pct(v):
        return f"{v*100:+.2f}%" if v is not None else "—"

    click.echo(f"Peer Cluster Diagnostics — {len(diags)} rows\n")
    click.echo(
        f"{'Ticker':<8} {'Date':<12} {'Win':>3} "
        f"{'TkrRet':>8} {'ETFRet':>8} {'vsETF':>8} "
        f"{'Cluster':<22} {'ClstRet':>8} {'vsClst':>8} "
        f"{'Peers':>5} Attribution"
    )
    click.echo("-" * 120)
    for d in diags:
        cluster_name = d.best_cluster.cluster_label[:20] if d.best_cluster else "—"
        cluster_ret = _pct(d.best_cluster.cluster_median_return) if d.best_cluster else "—"
        vs_cluster = _pct(d.best_cluster.ticker_vs_cluster) if d.best_cluster else "—"
        peers = str(d.best_cluster.peers_with_prices) if d.best_cluster else "—"
        click.echo(
            f"{d.ticker:<8} {d.event_date:<12} {str(d.window_days or ''):>3} "
            f"{_pct(d.ticker_return):>8} {_pct(d.etf_return):>8} {_pct(d.ticker_vs_etf):>8} "
            f"{cluster_name:<22} {cluster_ret:>8} {vs_cluster:>8} "
            f"{peers:>5} {d.attribution}"
        )

    from collections import Counter
    attr_counts = Counter(d.attribution for d in diags)
    click.echo(f"\nAttribution summary:")
    for attr, cnt in attr_counts.most_common():
        click.echo(f"  {attr}: {cnt}")


@data.command("ingest-sector-etfs")
@click.option("--db-path", default="data/mhde.duckdb", show_default=True)
@click.option("--date", "trade_date", default=None, help="Trade date YYYY-MM-DD (default: today).")
@click.option("--lookback-days", default=1, type=int, show_default=True,
              help="Number of recent trading days to fetch (1 = today only).")
@click.option("--delay", default=0.5, type=float, show_default=True,
              help="Seconds between Polygon API calls per ETF (increase for free-tier rate limits).")
def data_ingest_sector_etfs_cmd(db_path, trade_date, lookback_days, delay):
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
        n = ingest_sector_etfs_to_db(db_path, dt, api_key, delay=delay)
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


# ── Crypto prediction engine ──────────────────────────────────────────────────

@cli.group()
def crypto():
    """Crypto prediction engine commands."""


@crypto.command("build-universe")
@click.option("--dry-run", is_flag=True, default=False,
              help="Print ADD/REMOVE/PENDING decisions without modifying the DB.")
def crypto_build_universe(dry_run):
    """Apply daily hysteresis-based rebuild of crypto_universe.

    Reads the last 7 days from crypto_universe_ranking_buffer. ADDs require
    7 consecutive in_top_50=TRUE days plus >=60d listed on Binance perp;
    REMOVEs require 7 consecutive in_top_50=FALSE days. Anything else is
    a no-op (transitions are smoothed).
    """
    from crypto.ingestion.universe_builder import build_universe

    cfg, conn = _engine_setup()
    try:
        result = build_universe(conn, dry_run=dry_run)
        prefix = "[DRY-RUN] " if dry_run else ""
        click.echo(
            f"{prefix}build-universe: "
            f"{len(result['adds'])} adds, "
            f"{len(result['removes'])} removes, "
            f"{len(result['pendings'])} pendings, "
            f"{len(result['no_ops'])} no-ops "
            f"(latest buffer date {result['latest_buffer_date']})"
        )
        if result["adds"]:
            click.echo("\nADDs:")
            for a in result["adds"]:
                click.echo(f"  + {a['symbol']:<14} (listed {a['days_listed']}d)")
        if result["removes"]:
            click.echo("\nREMOVEs:")
            for r in result["removes"]:
                click.echo(f"  - {r['symbol']}")
        if result["pendings"]:
            click.echo("\nPENDINGs:")
            for p in result["pendings"]:
                click.echo(
                    f"  ? {p['symbol']:<14} "
                    f"({p['days_listed']}d listed, eligible after "
                    f"{p['eligible_after_date']}, "
                    f"{p['consecutive_top_50']}d consecutive top-50)"
                )
    finally:
        conn.close()


@crypto.command("backfill-universe-rankings")
@click.option("--start-date", "start_str", required=True,
              help="First ranking_date YYYY-MM-DD (inclusive).")
@click.option("--end-date", "end_str", default=None,
              help="Last ranking_date YYYY-MM-DD (default: today UTC).")
@click.option("--top-n", default=100, show_default=True, type=int,
              help="Top-N symbols persisted per date.")
def crypto_backfill_universe_rankings(start_str, end_str, top_n):
    """Backfill crypto_universe_ranking_buffer for every date in the range.

    Each date uses a point-in-time 30-day window — i.e., the ranking for
    date D is computed from the 30-day window ending on D, not 'today'. This
    seeds hysteresis for the daily build_universe rebuild.

    Per-date atomic replace; per-date Binance failures are logged and skipped.
    """
    from datetime import date as _date_cls
    from crypto.ingestion.rank_universe import backfill_universe_rankings

    start_date = _date_cls.fromisoformat(start_str)
    end_date = _date_cls.fromisoformat(end_str) if end_str else None

    cfg, conn = _engine_setup()
    try:
        results = backfill_universe_rankings(
            conn, start_date=start_date, end_date=end_date, top_n=top_n,
        )
        ok = sum(1 for n in results.values() if n >= 0)
        fail = sum(1 for n in results.values() if n < 0)
        total_rows = sum(n for n in results.values() if n >= 0)
        click.echo(
            f"backfill-universe-rankings: {ok} dates ok, {fail} failed, "
            f"{total_rows} total rows written"
        )
    finally:
        conn.close()


@crypto.command("rank-universe-daily")
@click.option("--date", "date_str", default=None,
              help="Ranking date YYYY-MM-DD (default: today UTC).")
@click.option("--top-n", default=100, show_default=True, type=int,
              help="Number of top-ranked symbols to persist.")
def crypto_rank_universe_daily(date_str, top_n):
    """Compute 30-day avg quote volume for all eligible perps; persist the
    top-N into crypto_universe_ranking_buffer. Idempotent on
    (symbol, ranking_date). Does NOT modify crypto_universe.

    Feeds the daily build_universe rebuild (hysteresis ADD/REMOVE rules
    consume 7 consecutive days of this buffer).
    """
    from datetime import date as _date_cls
    from crypto.ingestion.rank_universe import rank_universe_daily

    ranking_date = None
    if date_str:
        ranking_date = _date_cls.fromisoformat(date_str)

    cfg, conn = _engine_setup()
    try:
        n = rank_universe_daily(conn, ranking_date=ranking_date, top_n=top_n)
        click.echo(f"rank-universe-daily: wrote {n} rows for "
                   f"{ranking_date or 'today'}")
    finally:
        conn.close()


@crypto.command("backfill-prices")
def crypto_backfill_prices():
    """Backfill 2+ years of daily OHLCV from Binance futures."""
    from crypto.ingestion.backfill_ohlcv import backfill_ohlcv

    cfg, conn = _engine_setup()
    try:
        total = backfill_ohlcv(conn)
        click.echo(f"OHLCV backfill complete: {total:,} rows")
    finally:
        conn.close()


@crypto.command("backfill-intraday")
@click.option("--interval", default="1m", show_default=True,
              help="Kline interval (1m for the faithful replay).")
@click.option("--start", "start_str", default=None,
              help="First prediction_date YYYY-MM-DD (inclusive). "
                   "Default: min walkfold-10d prediction_date.")
@click.option("--end", "end_str", default=None,
              help="Last prediction_date YYYY-MM-DD (inclusive). "
                   "Default: max walkfold-10d prediction_date.")
@click.option("--symbols", default=None,
              help="Comma-separated symbol override. Default: DISTINCT walkfold-10d "
                   "prediction symbols in the window.")
@click.option("--research-db", default=None,
              help="Research DB path. Default: crypto.execution.backtest."
                   "intraday_klines.RESEARCH_DB_PATH.")
@click.option("--force", is_flag=True, default=False,
              help="Bypass the live-window guard (22:00-23:30 / 00:25-00:50 UTC).")
def crypto_backfill_intraday(interval, start_str, end_str, symbols, research_db, force):
    """Backfill 1-minute klines into the SEPARATE research DB for the intraday
    faithful replay. Never writes mhde.duckdb. Paginated + idempotent; run
    paced and OUTSIDE the live predict/export windows.
    """
    from datetime import (
        date as _date_cls, datetime as _dt, timedelta, timezone as _tz,
    )

    import duckdb

    from storage.config import load_engine_config
    from crypto.ingestion.binance_client import BinanceClient
    from crypto.execution.backtest.intraday_klines import (
        RESEARCH_DB_PATH, backfill_intraday, connect_research_db,
    )

    # Live-window guard — avoid contending with the 22:00-23:30 predict path
    # and the 00:25-00:50 export/entry path.
    now = _dt.now(tz=_tz.utc).time()
    in_live = ((now.hour == 22) or (now.hour == 23 and now.minute <= 30)
               or (now.hour == 0 and 25 <= now.minute <= 50))
    if in_live and not force:
        raise click.ClickException(
            f"Refusing to run during a live window (now {now.strftime('%H:%M')} "
            "UTC). Re-run outside 22:00-23:30 / 00:25-00:50 UTC, or pass --force."
        )

    db_path = load_engine_config()["db_path"]
    mhde = duckdb.connect(db_path, read_only=True)
    try:
        if start_str:
            start_date = _date_cls.fromisoformat(start_str)
        else:
            start_date = mhde.execute(
                "SELECT MIN(prediction_date) FROM crypto_ml_predictions "
                "WHERE model_id LIKE '%walkfold%' AND model_id LIKE '%10d%'"
            ).fetchone()[0]
        if end_str:
            end_date = _date_cls.fromisoformat(end_str)
        else:
            end_date = mhde.execute(
                "SELECT MAX(prediction_date) FROM crypto_ml_predictions "
                "WHERE model_id LIKE '%walkfold%' AND model_id LIKE '%10d%'"
            ).fetchone()[0]

        if symbols:
            sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
        else:
            rows = mhde.execute(
                "SELECT DISTINCT symbol FROM crypto_ml_predictions "
                "WHERE model_id LIKE '%walkfold%' AND model_id LIKE '%10d%' "
                "AND prediction_date BETWEEN ? AND ? ORDER BY symbol",
                [start_date, end_date],
            ).fetchall()
            sym_list = [r[0] for r in rows]
    finally:
        mhde.close()

    # Klines window: entries run prediction_date+1, horizon +10d → pad to +13d.
    fetch_start = _dt.combine(start_date, _dt.min.time(), tzinfo=_tz.utc)
    fetch_end = _dt.combine(end_date, _dt.min.time(), tzinfo=_tz.utc) + timedelta(days=13)

    click.echo(
        f"backfill-intraday: {len(sym_list)} symbols, interval={interval}, "
        f"klines [{fetch_start.date()} → {fetch_end.date()}] "
        f"(pred window {start_date} → {end_date})"
    )

    research = connect_research_db(research_db or RESEARCH_DB_PATH)
    try:
        client = BinanceClient()
        summary = backfill_intraday(
            client, research, symbols=sym_list, interval=interval,
            start=fetch_start, end=fetch_end,
        )
    finally:
        research.close()

    click.echo(
        f"backfill-intraday done: {summary['rows_written']:,} rows, "
        f"{summary['symbols_ok']} ok, {len(summary['symbols_skipped'])} skipped, "
        f"{summary['gaps']} gaps"
    )
    if summary["symbols_skipped"]:
        click.echo(f"  skipped: {', '.join(summary['symbols_skipped'])}")


@crypto.command("signal-probe-collect")
@click.option("--research-db", default=None,
              help="Probe research DB path. Default: "
                   "crypto.research.signal_probe.config.RESEARCH_DB_PATH.")
@click.option("--no-depth", is_flag=True, default=False,
              help="Skip the per-symbol order-book depth call this cycle.")
@click.option("--symbols", default=None,
              help="Comma-separated symbol override. Default: the probe "
                   "UNIVERSE snapshot in config.py.")
def crypto_signal_probe_collect(research_db, no_depth, symbols):
    """Run ONE signal-probe collection cycle (research-only).

    Pulls raw multi-window features per symbol from Binance USDT-M PUBLIC
    endpoints and UPSERTs one row per symbol for the current closed minute
    into the SEPARATE research DB (never mhde.duckdb). Read-only against the
    engine; no spec/interface change. Driven every 60s by the
    mhde-signal-probe-collector.timer; safe to run manually too.
    """
    from crypto.research.signal_probe import config as probe_cfg
    from crypto.research.signal_probe.client import ProbeBinanceClient
    from crypto.research.signal_probe.collector import run_cycle
    from crypto.research.signal_probe.store import connect_probe_db

    sym_list = (
        [s.strip() for s in symbols.split(",") if s.strip()]
        if symbols else list(probe_cfg.UNIVERSE)
    )
    conn = connect_probe_db(research_db or probe_cfg.RESEARCH_DB_PATH)
    try:
        client = ProbeBinanceClient()
        summary = run_cycle(
            client, conn, symbols=sym_list,
            btc_symbol=probe_cfg.BTC_SYMBOL, include_depth=not no_depth,
        )
    finally:
        conn.close()

    click.echo(
        f"signal-probe-collect: ts={summary['ts']} "
        f"{summary['rows_written']} rows, {summary['symbols_ok']} ok, "
        f"{len(summary['symbols_skipped'])} skipped"
    )
    if summary["symbols_skipped"]:
        click.echo(f"  skipped: {', '.join(summary['symbols_skipped'])}")


@crypto.command("capture-core-run")
@click.option("--root", default=None,
              help="Raw capture dir. Default: capture_core.config.RAW_DIR.")
def crypto_capture_core_run(root):
    """Run the capture-core raw market-data capture service (BLOCKS).

    Resolves the full TRADING USDT-M perp universe live (re-resolved on a
    cadence), captures every ``@aggTrade`` event across it, and writes raw
    zstd parquet under the capture dir. Read-only against Binance USDT-M PUBLIC
    WS/REST endpoints; NEVER opens mhde.duckdb or the engine DB. Long-running
    (Type=simple service); stops cleanly on SIGTERM/SIGINT, flushing buffers.
    """
    import asyncio
    import logging

    from crypto.research.capture_core import config as cc_cfg
    from crypto.research.capture_core.client import CaptureRestClient
    from crypto.research.capture_core.service import CaptureService

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    svc = CaptureService(root=root or cc_cfg.RAW_DIR, client=CaptureRestClient())
    asyncio.run(svc.run())


@crypto.command("capture-core-loadtest")
@click.option("--seconds", default=60.0, type=float,
              help="Measurement window length in seconds.")
@click.option("--write-root", default=None,
              help="If set, also write a parquet sample here for a real "
                   "compression ratio in the report.")
@click.option("--streams", "stream_set",
              type=click.Choice(["aggtrade", "depth-bookticker", "all"]),
              default="aggtrade",
              help="Which stream set to size. 'depth-bookticker' = the PARTIAL "
                   "sizing of the streams that deliver from this host.")
def crypto_capture_core_loadtest(seconds, write_root, stream_set):
    """Size a stream-set firehose over a bounded window.

    Drives the real connection manager against the chosen stream set across
    every TRADING USDT-M perp, then prints messages/sec, raw bytes/sec, MiB/min,
    and projected daily volume (raw, plus parquet-compressed when --write-root
    is given). 'depth-bookticker' is the PARTIAL sizing (aggTrade/markPrice/
    forceOrder are unmeasured here and ADD to the total).
    """
    import asyncio
    import json
    import logging

    from crypto.research.capture_core import service as cc_svc
    from crypto.research.capture_core.loadtest import run_loadtest

    factory = {
        "aggtrade": cc_svc.aggtrade_streams,
        "depth-bookticker": cc_svc.depth_bookticker_streams,
        "all": cc_svc.capture_streams,
    }[stream_set]

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    result = asyncio.run(run_loadtest(duration_s=seconds, write_root=write_root,
                                      stream_factory=factory))
    result["stream_set"] = stream_set
    click.echo(json.dumps(result, indent=2, default=str))


@crypto.command("capture-rest-run")
@click.option("--root", default=None,
              help="Raw capture dir. Default: capture_core.config.RAW_DIR.")
def crypto_capture_rest_run(root):
    """Run the capture-core REST present-state collector (BLOCKS).

    Polls public USDT-M REST present-state series (open interest, premium index /
    funding, long-short ratios, taker ratio, basis) on a budget-aware self-pacing
    schedule and writes raw zstd parquet per series under the capture dir.
    Read-only against Binance PUBLIC REST; NEVER opens mhde.duckdb or the engine
    DB. Coexists with the depth SnapshotScheduler on the shared IP weight.
    Long-running (Type=simple service); stops cleanly on SIGTERM/SIGINT.
    """
    import asyncio
    import logging

    from crypto.research.capture_core import config as cc_cfg
    from crypto.research.capture_core.client import CaptureRestClient
    from crypto.research.capture_core.rest_collector import RestPresentStateCollector

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    client = CaptureRestClient()
    collector = RestPresentStateCollector(
        root=root or cc_cfg.RAW_DIR, client=client,
        universe_fn=client.fetch_usdtm_perp_universe,
    )
    asyncio.run(collector.run())


@crypto.command("capture-klines-run")
@click.option("--root", default=None,
              help="Raw capture dir. Default: capture_core.config.RAW_DIR.")
def crypto_capture_klines_run(root):
    """Run the long-horizon 1h klines forward maintenance (BLOCKS).

    Hourly, fetches the latest few CLOSED 1h bars per USDT-M perp and dedup-appends
    them (the in-progress bar is never persisted). This is the ADR-035 long-context
    reference frame — NOT a backtest. Reuses the present-state collector wholesale
    (shared /fapi weight pacer + in-memory openTime dedup cursor + universe). Run
    `capture-klines-seed` once first for the ~90d backfill. Read-only public REST;
    NEVER opens mhde.duckdb. Long-running (Type=simple); clean stop on SIGTERM.
    """
    import asyncio
    import logging

    from crypto.research.capture_core import config as cc_cfg
    from crypto.research.capture_core import klines_store as ks

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    collector = ks.build_maintenance_collector(root or cc_cfg.RAW_DIR)
    asyncio.run(collector.run())


@crypto.command("capture-klines-seed")
@click.option("--root", default=None,
              help="Raw capture dir. Default: capture_core.config.RAW_DIR.")
@click.option("--days", default=None, type=int,
              help="Backfill horizon in days. Default: config.KLINES_SEED_DAYS (90).")
def crypto_capture_klines_seed(root, days):
    """One-time paginated ~90d backfill of closed 1h bars (run once at deploy).

    Paces under the shared /fapi weight budget (~2 calls/symbol at 90d). Idempotent
    enough to re-run (read-side dedup on (symbol, openTime) absorbs overlap).
    """
    import logging

    from crypto.research.capture_core import config as cc_cfg
    from crypto.research.capture_core import klines_store as ks

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    written = ks.seed(root or cc_cfg.RAW_DIR,
                      days=days or cc_cfg.KLINES_SEED_DAYS)
    click.echo(f"klines seed: {written} closed bars written")


@crypto.command("capture-klines-expire")
@click.option("--root", default=None,
              help="Raw capture dir. Default: capture_core.config.RAW_DIR.")
@click.option("--days", default=None, type=int,
              help="Retention window in days. Default: config.KLINES_RETENTION_DAYS (90).")
def crypto_capture_klines_expire(root, days):
    """Expire klines_1h date partitions older than the retention window (rolling).

    Filesystem-only under the capture store; intended for a daily timer.
    """
    import logging

    from crypto.research.capture_core import config as cc_cfg
    from crypto.research.capture_core import klines_store as ks

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    removed = ks.expire_klines_partitions(root or cc_cfg.RAW_DIR,
                                          days=days or cc_cfg.KLINES_RETENTION_DAYS)
    click.echo(f"klines retention: {len(removed)} partitions expired")


@crypto.command("capture-firehose-expire")
@click.option("--root", default=None,
              help="Raw capture dir. Default: capture_core.config.RAW_DIR.")
@click.option("--days", default=None, type=int,
              help="Retention window in days. Default: config.CAPTURE_RAW_RETENTION_DAYS (14).")
def crypto_capture_firehose_expire(root, days):
    """Expire raw FIREHOSE date partitions older than the rolling window.

    Whole date= partitions, oldest-first, never today's, firehose datasets only
    (klines_1h / REST series / _gaps untouched). Filesystem-only under the capture
    store; never opens the production DB. Intended for a daily timer; complements
    PR-3's in-loop free-space byte guard (this is the TIME bound).
    """
    import logging

    from crypto.research.capture_core import config as cc_cfg
    from crypto.research.capture_core import maintenance as mt

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    removed = mt.expire_firehose_partitions(
        root or cc_cfg.RAW_DIR, days=days or cc_cfg.CAPTURE_RAW_RETENTION_DAYS)
    click.echo(f"firehose retention: {len(removed)} partitions expired")


@crypto.command("capture-firehose-compact")
@click.option("--root", default=None,
              help="Raw capture dir. Default: capture_core.config.RAW_DIR.")
@click.option("--dates", default=None,
              help="Comma-separated YYYY-MM-DD to restrict the sweep (e.g. surviving days).")
@click.option("--dry-run", is_flag=True, default=False,
              help="Measure only — report files/rows without writing or deleting.")
def crypto_capture_firehose_compact(root, dates, dry_run):
    """One-shot compaction of raw FIREHOSE partitions into the bounded-file layout.

    Merges the many small part-*.parquet of each symbol=/date= partition into one
    verified file (rows kept in recv_ts_ns order; originals removed only after a
    pre/post row-count parity check). Use after deploying the compact-on-write fix
    to fold the surviving tiny-file days into the new layout. Filesystem-only; never
    opens the production DB.
    """
    import logging

    from crypto.research.capture_core import config as cc_cfg
    from crypto.research.capture_core import maintenance as mt

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    date_list = [d.strip() for d in dates.split(",")] if dates else None
    report = mt.migrate_compact(root or cc_cfg.RAW_DIR, dates=date_list,
                                dry_run=dry_run)
    click.echo(
        f"firehose compaction{' (DRY RUN)' if dry_run else ''}: "
        f"scanned {report.partitions_scanned}, compacted {report.partitions_compacted}, "
        f"files {report.files_before}->{report.files_after}, "
        f"rows {report.rows_before}->{report.rows_after}, "
        f"mismatches {len(report.mismatches)}")
    if report.mismatches:
        for m in report.mismatches:
            click.echo(f"  MISMATCH: {m}")


@crypto.command("intraday-replay")
@click.option("--start", "start_str", required=True,
              help="First prediction_date YYYY-MM-DD (inclusive).")
@click.option("--end", "end_str", required=True,
              help="Last prediction_date YYYY-MM-DD (inclusive).")
@click.option("--research-db", default=None, help="Research klines DB path.")
@click.option("--top-n", default=6, show_default=True, type=int,
              help="Daily top-N post-parabolic-filter traded subset size.")
@click.option("--day-offset", default=1, show_default=True, type=int,
              help="Entry day offset from prediction_date (KI-141: live is +1).")
@click.option("--entry-offset-hours", default=None, type=int,
              help="If set, use FixedOffsetEntry(hours) instead of DeployedEntry "
                   "(00:45). Interface demo only — not swept here.")
@click.option("--out-dir", default="data/reports", show_default=True,
              help="Directory for the generated markdown report (gitignored).")
def crypto_intraday_replay(start_str, end_str, research_db, top_n, day_offset,
                           entry_offset_hours, out_dir):
    """Replay walk-forward predictions against 1-minute klines under the
    deployed trail + arm-aware hard floor. Reads mhde.duckdb and the research
    DB strictly read-only; writes only the gitignored markdown report.
    """
    import os
    from datetime import date as _date_cls, datetime as _dt, timezone as _tz

    import duckdb

    from storage.config import load_engine_config
    from crypto.execution.backtest.intraday_klines import (
        RESEARCH_DB_PATH, connect_research_db,
    )
    from crypto.execution.backtest.intraday_replay import (
        DeployedEntry, FixedOffsetEntry, render_report, run_intraday_replay,
    )

    start_date = _date_cls.fromisoformat(start_str)
    end_date = _date_cls.fromisoformat(end_str)

    if entry_offset_hours is not None:
        entry_rule = FixedOffsetEntry(entry_offset_hours, day_offset=day_offset)
    else:
        entry_rule = DeployedEntry(day_offset=day_offset)

    db_path = load_engine_config()["db_path"]
    mhde = duckdb.connect(db_path, read_only=True)
    research = connect_research_db(research_db or RESEARCH_DB_PATH, read_only=True)
    try:
        report = run_intraday_replay(
            mhde, research, start_date=start_date, end_date=end_date,
            entry_rule=entry_rule, top_n=top_n,
        )
    finally:
        mhde.close()
        research.close()

    as_of = _dt.now(tz=_tz.utc).date()
    text = render_report(report, as_of=as_of)
    click.echo(text)

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"intraday_replay_{as_of.isoformat()}.md")
    with open(out_path, "w") as fh:
        fh.write(text)
    click.echo(f"\n[report written to {out_path}]")


@crypto.command("check-data-quality")
@click.option("--date", "date_str", default=None,
              help="Date to check (YYYY-MM-DD). Default: latest in crypto_prices_daily.")
def crypto_check_data_quality(date_str):
    """OHLCV plausibility / volume-cliff guard.

    Runs in the daily pipeline between backfill-prices and the downstream
    stages. On a *systemic* anomaly (more than SYSTEMIC_FLAG_RATIO of the
    evaluable universe shows a volume/range/trade cliff vs its 20-day
    median — the 2026-05-07 partial-candle shape) it fires a CRITICAL
    Telegram alert and exits non-zero, which blocks the rest of the
    systemd ExecStart chain (backfill-funding/oi/labels/features/predict).
    Per-symbol-only anomalies fire a WARN alert and do NOT block. Escape
    hatch: set MHDE_DATA_QUALITY_GUARD_OVERRIDE=1 to never block.
    """
    import os
    import sys as _sys
    from datetime import date as _date
    from crypto.config import SYSTEMIC_FLAG_RATIO
    from crypto.schema import create_all_tables
    from pipelines.data_quality_guard import check_ohlcv_plausibility, persist_report
    from monitoring.alert import MonitorResult, send_alert

    cfg, conn = _engine_setup()
    try:
        create_all_tables(conn)
        if date_str:
            target = _date.fromisoformat(date_str)
        else:
            row = conn.execute("SELECT MAX(trade_date) FROM crypto_prices_daily").fetchone()
            target = row[0] if row and row[0] else _date.today()

        report = check_ohlcv_plausibility(conn, target)
        n_written = persist_report(conn, report)
        click.echo(
            f"data-quality {target}: on_date={report.n_symbols_on_date} "
            f"evaluated={report.n_evaluated} flagged={report.n_flagged} "
            f"systemic_ratio={report.systemic_ratio:.2f} severity={report.severity} "
            f"rows_written={n_written}"
        )

        if report.severity != "ok":
            flagged_syms = sorted({f.symbol for f in report.per_symbol_flags})
            body = [
                f"date={target}  evaluated={report.n_evaluated}  flagged={report.n_flagged}  "
                f"systemic_ratio={report.systemic_ratio:.0%} (threshold {SYSTEMIC_FLAG_RATIO:.0%})",
            ]
            if flagged_syms:
                body.append("flagged: " + ", ".join(flagged_syms[:15])
                            + (f" … (+{len(flagged_syms) - 15} more)" if len(flagged_syms) > 15 else ""))
            if report.is_systemic:
                body.append("→ BLOCKING downstream stages (backfill-funding/oi/labels/features/predict). "
                            "Likely an ingestion/data-source issue; the next backfill-prices run self-heals "
                            "via the re-fetch window. To override: MHDE_DATA_QUALITY_GUARD_OVERRIDE=1.")
            send_alert(MonitorResult(
                monitor="crypto_data_quality_guard",
                status="fail" if report.is_systemic else "warn",
                severity="critical" if report.is_systemic else "warn",
                title=("Systemic OHLCV corruption — daily pipeline blocked"
                       if report.is_systemic
                       else f"OHLCV plausibility: {report.n_flagged} symbol(s) flagged"),
                body="\n".join(body),
                metrics={"date": str(target), "n_evaluated": report.n_evaluated,
                         "n_flagged": report.n_flagged,
                         "systemic_ratio": round(report.systemic_ratio, 3)},
            ))

        override = os.environ.get("MHDE_DATA_QUALITY_GUARD_OVERRIDE", "").lower() in {"1", "true", "yes"}
        if report.is_systemic and not override:
            click.echo("SYSTEMIC OHLCV anomaly — exiting non-zero to block downstream pipeline "
                       "stages (set MHDE_DATA_QUALITY_GUARD_OVERRIDE=1 to bypass).", err=True)
            _sys.exit(2)
        if report.is_systemic and override:
            click.echo("SYSTEMIC OHLCV anomaly — MHDE_DATA_QUALITY_GUARD_OVERRIDE set; NOT blocking.", err=True)
    finally:
        conn.close()


@crypto.command("backfill-funding")
def crypto_backfill_funding():
    """Backfill funding rate history from Binance futures."""
    from crypto.ingestion.backfill_funding import backfill_funding

    cfg, conn = _engine_setup()
    try:
        total = backfill_funding(conn)
        click.echo(f"Funding rate backfill complete: {total:,} rows")
    finally:
        conn.close()


@crypto.command("backfill-oi")
def crypto_backfill_oi():
    """Backfill open interest history from Binance futures."""
    from crypto.ingestion.backfill_oi import backfill_open_interest

    cfg, conn = _engine_setup()
    try:
        total = backfill_open_interest(conn)
        click.echo(f"OI backfill complete: {total:,} rows")
    finally:
        conn.close()


@crypto.command("backfill-labels")
def crypto_backfill_labels():
    """Compute crypto ML labels for all historical symbol-dates."""
    from crypto.ml.labels import compute_labels

    cfg, conn = _engine_setup()
    try:
        total = compute_labels(conn)
        click.echo(f"Labels computed: {total:,} rows")
    finally:
        conn.close()


@crypto.command("backfill-features")
def crypto_backfill_features():
    """Compute crypto ML features for all historical symbol-dates."""
    from crypto.ml.features import compute_features

    cfg, conn = _engine_setup()
    try:
        total = compute_features(conn)
        click.echo(f"Features computed: {total:,} rows")
    finally:
        conn.close()


@crypto.command("hypothesis-tests")
def crypto_hypothesis_tests():
    """Run signal validation hypothesis tests (Step 0.5 checkpoint)."""
    from crypto.ml.hypothesis_tests import run_all_tests, print_test_results

    cfg, conn = _engine_setup()
    try:
        summary = run_all_tests(conn)
        print_test_results(summary)
    finally:
        conn.close()


@crypto.command("train")
@click.option("--label", default="label_10d_10pct", help="Label column (legacy kind only).")
@click.option("--horizon", default="10d", help="Prediction horizon (legacy kind only).")
@click.option("--threshold", default=0.10, type=float, help="Target threshold (legacy kind only).")
@click.option("--label-kind", "label_kind", type=click.Choice(["legacy", "knockout"]), default="legacy",
              help="'knockout' trains 5d AND 10d models on label_Nd_knockout; the new rows are "
                   "is_active=false and are NOT auto-promoted (operator decides).")
def crypto_train_cmd(label, horizon, threshold, label_kind):
    """Train crypto ML model(s) with walk-forward CV."""
    from crypto.ml.train import train_walk_forward
    from crypto.ml.evaluate import print_walk_forward_results

    cfg, conn = _engine_setup()
    try:
        if label_kind == "knockout":
            from crypto.config import KNOCKOUT_TP
            for hz in ("5d", "10d"):
                lc = f"label_{hz}_knockout"
                click.echo(f"=== knockout training: horizon={hz}, label_col={lc} (is_active stays false, no auto-promote) ===")
                results = train_walk_forward(conn, label_col=lc, horizon=hz, threshold=KNOCKOUT_TP,
                                             label_kind="knockout", auto_promote=False)
                print_walk_forward_results(results, lc, hz)
        else:
            results = train_walk_forward(conn, label_col=label, horizon=horizon, threshold=threshold)
            print_walk_forward_results(results, label, horizon)
    finally:
        conn.close()


@crypto.command("predict")
@click.option("--date", default=None, help="Prediction date (YYYY-MM-DD). Default: latest.")
@click.option("--skip-outcomes", is_flag=True, help="Skip filling historical outcomes.")
def crypto_predict(date, skip_outcomes):
    """Run crypto prediction pipeline: score universe, fill outcomes, print results."""
    from datetime import date as date_cls
    from pipelines.crypto_prediction_pipeline import run_crypto_prediction_pipeline

    cfg, conn = _engine_setup()
    try:
        pred_date = date_cls.fromisoformat(date) if date else None
        run_crypto_prediction_pipeline(conn, prediction_date=pred_date,
                                       skip_features=True, skip_outcomes=skip_outcomes)
    finally:
        conn.close()


@crypto.command("export-spec")
@click.option("--dry-run", is_flag=True, help="Print the spec JSON without writing.")
def crypto_export_spec(dry_run):
    """Build active_spec.json from current Phase 1B winner row.

    Reads crypto_backtest_summary / crypto_backtest_runs for run_id
    pinned in crypto/exports/spec_config.py:PHASE1B_WINNER_RUN_ID.
    Computes portfolio metrics via simulate_portfolio. Writes to
    data/exports/active_spec.json (atomic).
    """
    from crypto.exports import write_active_spec

    cfg, conn = _engine_setup()
    try:
        spec = write_active_spec.write(conn, dry_run=dry_run)
        if not dry_run:
            click.echo(
                f"wrote {write_active_spec.ACTIVE_SPEC_PATH} "
                f"(spec_hash={spec['spec_hash']})"
            )
    except write_active_spec.ExportSpecError as e:
        raise click.ClickException(str(e))
    finally:
        conn.close()


@crypto.command("export-predictions")
@click.option("--date", "date_str", default=None,
              help="Prediction date YYYY-MM-DD. Default: today UTC.")
@click.option("--dry-run", is_flag=True,
              help="Print the predictions JSON without writing.")
def crypto_export_predictions(date_str, dry_run):
    """Build predictions_YYYY-MM-DD.json (full active universe ranked)
    and update predictions_latest.json symlink.

    Strict preflight: features for prediction_date must exist for
    every active universe symbol. Failure exits non-zero without
    touching output files; engine handles stale symlink per
    INTERFACE.md §5.3.
    """
    from datetime import date as date_cls
    from crypto.exports import write_daily_predictions

    pred_date = date_cls.fromisoformat(date_str) if date_str else None
    cfg, conn = _engine_setup()
    try:
        payload = write_daily_predictions.write(
            conn, prediction_date=pred_date, dry_run=dry_run,
        )
        if not dry_run:
            click.echo(
                f"wrote predictions_{payload['export_date']}.json "
                f"(n={payload['n_predictions']}, model={payload['model_id']})"
            )
    except write_daily_predictions.ExportPreflightError as e:
        raise click.ClickException(f"preflight failed: {e}")
    finally:
        conn.close()


@crypto.command("retrain")
def crypto_retrain():
    """Weekly retrain: recompute labels/features and retrain all horizons."""
    from crypto.ml.retrain import retrain_all

    cfg, conn = _engine_setup()
    try:
        retrain_all(conn)
    finally:
        conn.close()


@crypto.command("backtest")
@click.option("--horizon", type=click.Choice(["5d", "10d"]), required=True,
              help="Prediction horizon. Must match Phase 1A backfill.")
@click.option("--policy", type=click.Choice(["A", "B", "C", "D", "E"]),
              required=True, help="Exit policy id (see SPEC.md).")
@click.option("--selection", type=click.Choice(["top_n", "threshold"]),
              required=True, help="Selection rule applied per day.")
@click.option("--params", default=None,
              help='JSON dict of selection + policy params, e.g. \'{"n": 6}\' '
                   'or \'{"tp_pct": 0.05, "sl_pct": 0.03}\'.')
@click.option("--force", is_flag=True,
              help="Overwrite an existing run with the same run_id.")
@click.option("--dry-run", is_flag=True,
              help="Run the lifecycle but skip DB persistence.")
def crypto_backtest(horizon, policy, selection, params, force, dry_run):
    """Phase 1B execution backtest (single configuration).

    See crypto/execution/backtest/SPEC.md.
    """
    import json as _json
    from crypto.execution.backtest.harness import run_backtest
    from crypto.execution.backtest.metrics import compute_and_persist_summary

    if params:
        try:
            params_dict = _json.loads(params)
        except _json.JSONDecodeError as exc:
            raise click.BadParameter(
                f"--params must be valid JSON: {exc}", param_hint="--params"
            )
        if not isinstance(params_dict, dict):
            raise click.BadParameter(
                "--params must decode to a JSON object",
                param_hint="--params",
            )
    else:
        params_dict = {}

    # Selection vs policy params share the user-facing JSON dict but the
    # underlying modules want disjoint kwargs.
    SELECTION_KEYS = {"n", "threshold"}
    selection_params = {k: v for k, v in params_dict.items()
                        if k in SELECTION_KEYS}
    policy_params = {k: v for k, v in params_dict.items()
                     if k not in SELECTION_KEYS}

    cfg, conn = _engine_setup()
    try:
        try:
            state = run_backtest(
                conn,
                horizon=horizon, exit_policy_id=policy,
                selection_rule=selection,
                selection_params=selection_params,
                policy_params=policy_params,
                dry_run=dry_run, force=force,
            )
        except RuntimeError as exc:
            click.echo(f"\n{exc}", err=True)
            raise click.Abort()
        if not dry_run:
            compute_and_persist_summary(conn, state.run_id)
    finally:
        conn.close()

    # Summary
    click.echo()
    click.echo(f"  run_id                       : {state.run_id}")
    click.echo(f"  predictions seen             : {state.n_predictions_seen:,}")
    click.echo(f"  trades                       : {len(state.closed_trades):,}")
    click.echo(f"  skipped (duplicate)          : {state.n_skipped_duplicates:,}")
    n_atr = sum(1 for s in state.skipped if s.reason == "missing_atr")
    click.echo(f"  skipped (missing ATR)        : {n_atr}")
    click.echo(f"  data-gap exits               : {state.n_data_gap_exits}")
    click.echo(f"  forward-fills                : {state.n_forward_fills}")
    click.echo(f"  excluded by funding floor    : {state.n_excluded_by_funding_floor}")
    click.echo(f"  missing-funding warnings     : {state.n_missing_funding_warnings}")
    if dry_run:
        click.echo("\n  DRY RUN — no rows persisted.")
    else:
        click.echo("\n  Persisted to crypto_backtest_runs / crypto_backtest_trades.")


@crypto.command("backtest-grid")
@click.option("--grid", type=click.Choice(["base", "sensitivity"]),
              default="base", show_default=True,
              help="Which grid to run.")
@click.option("--top-run-ids", default=None,
              help="(sensitivity only) Comma-separated list of base run_ids "
                   "to sweep around. Default: read top-3 from "
                   "crypto_backtest_summary ORDER BY sharpe_ratio DESC.")
@click.option("--allow-iterated", is_flag=True,
              help="(sensitivity only) Bypass the iterated-sweep guard. "
                   "Required when any base run_id is itself a sensitivity-"
                   "shape config (i.e. not in the canonical base grid). "
                   "See KNOWN_ISSUES.md KI-125. With this flag the CLI "
                   "still warns loudly and logs the iterated bases.")
@click.option("--force", is_flag=True,
              help="Overwrite existing rows for any colliding run_id.")
@click.option("--skip-existing/--no-skip-existing", default=True,
              show_default=True,
              help="If a run_id already exists: skip silently (default) "
                   "or mark it as a per-config failure with --no-skip-existing.")
@click.option("--dry-run", is_flag=True,
              help="Run lifecycles but persist nothing.")
def crypto_backtest_grid(grid, top_run_ids, allow_iterated, force,
                         skip_existing, dry_run):
    """Phase 1B grid runner — base or sensitivity configuration matrix.

    See crypto/execution/backtest/SPEC.md and docs/PATH_TO_LIVE_PLAN.md.

    The sensitivity grid sweeps ONE axis at a time per base run. Running
    this command more than once against an evolving DB produces multi-
    axis configs through greedy axis-by-axis hill climbing — the second
    invocation re-ranks against the first invocation's outputs and
    starts sweeping around them. To prevent this silent drift, the CLI
    refuses to proceed when any selected base is not in the canonical
    base grid. Pass `--allow-iterated` to override deliberately. See
    KI-125 in KNOWN_ISSUES.md.
    """
    from crypto.execution.backtest.runner import (
        base_grid_configs, run_grid, sensitivity_grid_configs,
        summarize_grid_result,
    )

    cfg, conn = _engine_setup()
    try:
        if grid == "sensitivity":
            if top_run_ids:
                ids = [s.strip() for s in top_run_ids.split(",") if s.strip()]
            else:
                # Top-3 by sum-of-fractions sharpe_ratio. SPEC.md
                # ranking-preservation note: the SET of top 3 is the
                # same whether ranked by sum-of-fractions Sharpe or
                # portfolio Sharpe; only the order within differs, and
                # order within doesn't matter for sensitivity (we run
                # the same sweep on each). Portfolio metrics not
                # required for top-3 selection.
                rows = conn.execute(
                    "SELECT run_id FROM crypto_backtest_summary "
                    "ORDER BY sharpe_ratio DESC LIMIT 3"
                ).fetchall()
                ids = [r[0] for r in rows]
                if not ids:
                    raise click.ClickException(
                        "no rows in crypto_backtest_summary; run "
                        "'crypto backtest-grid --grid base' first."
                    )

            # KI-125 guard: the canonical base grid emits a fixed set of
            # 20 run_ids. Anything else is a sensitivity-shape config —
            # sweeping around it produces multi-axis stacks via greedy
            # hill climbing. Refuse by default; `--allow-iterated`
            # overrides with a loud warning.
            base_grid_ids = {c.run_id for c in base_grid_configs()}
            iterated = [rid for rid in ids if rid not in base_grid_ids]
            if iterated:
                if not allow_iterated:
                    raise click.ClickException(
                        "sensitivity grid would sweep around "
                        f"{len(iterated)} non-base-grid run_id(s):\n"
                        + "\n".join(f"  - {rid}" for rid in iterated)
                        + "\n\nThese are sensitivity-shape configs, not "
                        "canonical base-grid runs. Sweeping around them "
                        "produces multi-axis configs through greedy axis-"
                        "by-axis hill climbing (KI-125). To proceed "
                        "deliberately, pass --allow-iterated. To run a "
                        "clean single-axis sensitivity grid, pass "
                        "--top-run-ids with run_ids drawn from the base "
                        "grid (or first re-run the base grid)."
                    )
                click.echo(
                    "⚠  --allow-iterated: sweeping around "
                    f"{len(iterated)} sensitivity-shape base(s):"
                )
                for rid in iterated:
                    click.echo(f"     - {rid}")
                click.echo(
                    "   This will emit multi-axis configs via greedy "
                    "hill climbing. See KI-125."
                )
                click.echo("")

            click.echo(f"Sensitivity grid: sweeping around {len(ids)} base "
                       f"run_id(s):")
            for i in ids:
                click.echo(f"  {i}")
            click.echo("")
            configs = sensitivity_grid_configs(conn, ids)
        else:
            configs = base_grid_configs()

        result = run_grid(
            conn, configs,
            force=force, skip_existing=skip_existing, dry_run=dry_run,
        )
    finally:
        conn.close()

    click.echo(summarize_grid_result(result))
    if dry_run:
        click.echo("\n  DRY RUN — no rows persisted.")


@crypto.command("backtest-report")
@click.option("--top-n", default=3, type=int, show_default=True,
              help="Number of top runs to detail (sorted by --sort-by).")
@click.option("--run-id", default=None,
              help="Optional single run_id; emits ONLY that run's detail "
                   "(no leaderboard).")
@click.option("--sort-by", default="sharpe_ratio", show_default=True,
              type=click.Choice(sorted([
                  "sharpe_ratio", "net_pnl_total_pct",
                  "net_pnl_annualized_pct", "max_drawdown_pct",
                  "hit_rate", "profit_factor",
              ])))
def crypto_backtest_report(top_n, run_id, sort_by):
    """Phase 1B reports — leaderboard + top-N detail with simulated portfolio.

    See crypto/execution/backtest/SPEC.md.
    """
    from crypto.execution.backtest.report import (
        format_portfolio_result,
        generate_ranking_table,
        generate_run_detail,
        generate_top_n_detail,
        simulate_portfolio,
    )

    cfg, conn = _engine_setup()
    try:
        if run_id:
            click.echo(generate_run_detail(conn, run_id))
            click.echo("")
            portfolio = simulate_portfolio(conn, run_id)
            click.echo(format_portfolio_result(portfolio))
        else:
            click.echo(generate_ranking_table(conn, sort_by=sort_by))
            click.echo("\n\n---\n")
            click.echo(generate_top_n_detail(conn, n=top_n, sort_by=sort_by))
    finally:
        conn.close()


@crypto.command("phase0-report")
@click.option("--model-id", default=None,
              help="Evaluate a single model_id. Default: every "
                   "is_active=true crypto model.")
@click.option("--out", default=None,
              help="Output path for the markdown report. Default: "
                   "data/reports/phase0_report_YYYY-MM-DD.md. Pass `-` "
                   "to write to stdout only (no file saved).")
def crypto_phase0_report(model_id, out):
    """Phase 0 calibration report — go/no-go markdown for the four
    Phase 0 criteria across active crypto models.

    See docs/PATH_TO_LIVE_PLAN.md § "Phase 0: Live Calibration
    Validation" for the criteria. The report is INTERIM until the
    200-sample gate is met; all four metrics are still computed and
    shown so the operator sees the trajectory week over week.
    """
    from pathlib import Path
    from crypto.ml.phase0_report import build_report, save_report

    cfg, conn = _engine_setup()
    try:
        text = build_report(conn)
    finally:
        conn.close()

    click.echo(text)

    if out == "-":
        return  # stdout only, no save
    target = Path(out) if out else None
    saved = save_report(text, path=target)
    click.echo(f"\n_(report saved to {saved})_", err=True)


@crypto.command("backfill-walkforward-predictions")
@click.option(
    "--horizons", default="5d,10d", show_default=True,
    help="Comma-separated horizons to backfill.",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Run training and outcome computation but write nothing to the DB.",
)
@click.option(
    "--force", is_flag=True,
    help="Overwrite any existing backfill rows for the planned model_ids.",
)
def crypto_backfill_walkforward(horizons, dry_run, force):
    """Phase 1A — persist walk-forward OOS predictions for Phase 1B.

    For each horizon, runs the existing walk-forward CV (no retraining cost
    beyond the existing crypto retrain) and persists each fold's OOS
    probabilities to crypto_ml_predictions, tagged with a fold-specific
    model_id (`crypto_{horizon}_walkfold_{YYYY_MM}`). New model_runs rows
    are inserted with is_active=false; the live daily predict pipeline is
    unaffected. See crypto/ml/PHASE1A_SPEC.md.
    """
    from crypto.ml.backfill_walkforward import (
        HORIZON_CONFIGS,
        backfill_horizon,
        format_backfill_summary,
        format_validation_report,
        validate_backfill,
    )

    horizon_list = [h.strip() for h in horizons.split(",") if h.strip()]
    invalid = [h for h in horizon_list if h not in HORIZON_CONFIGS]
    if invalid:
        raise click.BadParameter(
            f"unknown horizons {invalid!r}; valid: {sorted(HORIZON_CONFIGS)}"
        )

    cfg, conn = _engine_setup()
    try:
        for hz in horizon_list:
            try:
                result = backfill_horizon(conn, hz, dry_run=dry_run, force=force)
            except RuntimeError as exc:
                # Idempotency collision message — surface cleanly, do not
                # traceback through a healthy guard.
                click.echo(f"\n[{hz}] {exc}", err=True)
                raise click.Abort()
            click.echo(format_backfill_summary(result))

        if dry_run:
            click.echo("\nDRY RUN — no rows written. Re-run without --dry-run to commit.")
        else:
            checks = validate_backfill(conn)
            click.echo(format_validation_report(checks))
            if not all(c.passed for c in checks):
                click.echo("\nVALIDATION FAILED — see above.", err=True)
                raise click.Abort()
    finally:
        conn.close()


@cli.group()
def fx():
    """GBP/EUR FX prediction engine commands."""


@fx.command("import-data")
def fx_import_data():
    """Import GBP/EUR hourly bars from CSV into DuckDB."""
    from fx.data.import_csv import import_hourly_csv

    cfg, conn = _engine_setup()
    try:
        total = import_hourly_csv(conn)
        click.echo(f"Import complete: {total:,} hourly bars")
    finally:
        conn.close()


@fx.command("refresh-prices")
def fx_refresh_prices():
    """Fetch latest hourly bar from Dukascopy and upsert into DuckDB."""
    from fx.data.refresh import refresh_prices

    cfg, conn = _engine_setup()
    try:
        result = refresh_prices(conn)
        click.echo(
            f"Refresh: fetch={result['fetch_status']} "
            f"bar={result['fetched_hour']} inserted={result['rows_inserted']}"
        )
        if result["fetch_error"]:
            click.echo(f"  note: {result['fetch_error']}")
    finally:
        conn.close()


@fx.command("refresh-prices-twelvedata")
def fx_refresh_prices_twelvedata():
    """Fetch latest hourly bar from TwelveData (parallel migration fetcher).

    Writes to fx_prices_hourly_twelvedata, NOT to fx_prices_hourly.
    Production predict / features / labels keep reading the Dukascopy
    table. See DECISIONS.md ADR-013.
    """
    from fx.data.refresh_twelvedata import refresh_prices

    cfg, conn = _engine_setup()
    try:
        result = refresh_prices(conn)
        click.echo(
            f"Refresh (TwelveData): fetch={result['fetch_status']} "
            f"bar={result['fetched_hour']} inserted={result['rows_inserted']}"
        )
        if result["fetch_error"]:
            click.echo(f"  note: {result['fetch_error']}")
        # Exit 1 only on hard errors so systemd unit doesn't fail on the
        # expected NO_DATA / CLOSED cases.
        if result["fetch_status"] == "ERROR":
            raise SystemExit(1)
    finally:
        conn.close()


@fx.command("compare-sources")
@click.option("--hours", default=24, show_default=True,
              help="Comparison window in hours (counted back from now).")
@click.option("--threshold-pips", default=5.0, show_default=True, type=float,
              help="Maximum acceptable pip diff on close before flagging.")
def fx_compare_sources(hours, threshold_pips):
    """Diff Dukascopy ↔ TwelveData FX bars over the recent window.

    Exit 0 if every matched bar is within `--threshold-pips`. Exit 1
    otherwise. Used as the gate for the Session 2 cutover.
    """
    from fx.data.compare_sources import compare_recent, format_report

    cfg, conn = _engine_setup()
    try:
        result = compare_recent(conn, hours=hours, threshold_pips=threshold_pips)
        click.echo(format_report(result))
        if not result["all_within_threshold"]:
            raise SystemExit(1)
    finally:
        conn.close()


@fx.command("hypothesis-tests")
def fx_hypothesis_tests():
    """Run signal validation hypothesis tests (checkpoint gate)."""
    from fx.ml.hypothesis_tests import run_all_tests, print_test_results

    cfg, conn = _engine_setup()
    try:
        summary = run_all_tests(conn)
        print_test_results(summary)
    finally:
        conn.close()


@fx.command("retrain")
def fx_retrain():
    """Weekly retrain: recompute labels/features and retrain all models."""
    from fx.ml.retrain import retrain_all

    cfg, conn = _engine_setup()
    try:
        retrain_all(conn)
    finally:
        conn.close()


@fx.command("predict")
@click.option("--datetime", "dt", default=None, help="Bar datetime (YYYY-MM-DD HH:MM). Default: latest.")
@click.option("--no-alert", is_flag=True, help="Skip Telegram alert.")
def fx_predict(dt, no_alert):
    """Run FX prediction: score bar, generate signal, optionally alert."""
    from datetime import datetime as dt_cls
    from fx.ml.predict import score_bar, fill_outcomes
    from fx.ml.signals import generate_signal, send_telegram_alert
    from pipelines.freshness import check_fx_freshness

    cfg, conn = _engine_setup()
    try:
        freshness = check_fx_freshness(conn)
        if not freshness.is_fresh:
            click.echo(f"WARNING: FX data is stale ({freshness.message})")
            import logging as _logging
            _logging.getLogger("mhde.fx.predict").warning(
                "DATA STALE: %s", freshness.message
            )

        bar_dt = dt_cls.fromisoformat(dt) if dt else None
        result = score_bar(conn, bar_dt)

        if not result["predictions"]:
            click.echo("No predictions generated.")
            return

        click.echo(f"\nFX Predictions -- {result['datetime']}")
        click.echo(f"GBP/EUR: {result['price']:.5f}")
        click.echo(f"{'Direction':<12} {'Horizon':<8} {'Probability':<12}")
        click.echo("-" * 35)
        for key, pred in sorted(result["predictions"].items()):
            click.echo(f"{pred['direction']:<12} {pred['horizon']:<8} {pred['probability']:.1%}")

        signal = generate_signal(result["predictions"], result["datetime"], result["price"], conn)
        if signal:
            click.echo(f"\nSIGNAL: {signal['type']}")
            if not no_alert:
                send_telegram_alert(signal, conn)
        else:
            click.echo("\nSignal: WAIT (no action)")

        fill_outcomes(conn)
    finally:
        conn.close()


@fx.command("train")
@click.option("--direction", default=None, help="Train single direction: up or down")
@click.option("--horizon", default=None, help="Train single horizon: 24h or 48h")
def fx_train(direction, horizon):
    """Train FX directional models with walk-forward CV."""
    from fx.ml.train import train_all_models, train_model, MODEL_CONFIGS
    from fx.ml.evaluate import print_training_results

    cfg, conn = _engine_setup()
    try:
        if direction and horizon:
            matching = [c for c in MODEL_CONFIGS if c["direction"] == direction and c["horizon"] == horizon]
            if matching:
                results = {f"{direction}_{horizon}": train_model(conn, **matching[0])}
            else:
                click.echo(f"Unknown config: {direction}_{horizon}")
                return
        else:
            results = train_all_models(conn)
        print_training_results(results)
    finally:
        conn.close()


@fx.command("backfill-features")
def fx_backfill_features():
    """Compute FX ML features for all hourly bars."""
    from fx.ml.features import compute_features

    cfg, conn = _engine_setup()
    try:
        total = compute_features(conn)
        click.echo(f"Features computed: {total:,} rows")
    finally:
        conn.close()


@fx.command("backfill-labels")
def fx_backfill_labels():
    """Compute FX ML labels for all hourly bars."""
    from fx.ml.labels import compute_labels

    cfg, conn = _engine_setup()
    try:
        total = compute_labels(conn)
        click.echo(f"Labels computed: {total:,} rows")
    finally:
        conn.close()


@fx.command("bot")
def fx_bot():
    """Run the FX Telegram bot (long-polling, blocks forever)."""
    import logging as _logging
    from fx.bot.telegram_bot import run_bot

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(name)-30s %(levelname)-8s %(message)s",
    )
    run_bot()


@fx.command("set-position")
@click.option("--holding", required=True, type=click.Choice(["GBP", "EUR"]),
              help="Currency you currently hold.")
@click.option("--rate", required=True, type=float, help="GBP/EUR rate at conversion.")
@click.option("--date", "entry_date", required=True,
              help="Entry date (YYYY-MM-DD or ISO timestamp).")
def fx_set_position(holding, rate, entry_date):
    """Record current FX position (HOLDING_GBP or HOLDING_EUR)."""
    from datetime import datetime as dt_cls
    from fx.schema import create_all_tables

    cfg, conn = _engine_setup()
    try:
        create_all_tables(conn)
        try:
            dt_obj = dt_cls.fromisoformat(entry_date)
        except ValueError:
            dt_obj = dt_cls.strptime(entry_date, "%Y-%m-%d")
        position = f"HOLDING_{holding}"
        conn.execute("DELETE FROM fx_position")
        conn.execute(
            "INSERT INTO fx_position (position, entry_rate, entry_date, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            [position, rate, dt_obj],
        )
        click.echo(f"Position set: {position} @ {rate} on {dt_obj.date()}")
    finally:
        conn.close()


@fx.command("backfill-macro")
def fx_backfill_macro():
    """Fetch macro indicators from FRED API."""
    from fx.data.macro import backfill_macro

    cfg, conn = _engine_setup()
    try:
        total = backfill_macro(conn)
        click.echo(f"Macro backfill complete: {total:,} observations")
    finally:
        conn.close()


@cli.group()
def system():
    """System-level commands (health checks, diagnostics)."""


@system.command("health-check")
def system_health_check():
    """Run morning health check across all 3 engines and post Telegram summary."""
    from pipelines.health_check import run_health_check

    ok = run_health_check()
    raise SystemExit(0 if ok else 1)


@cli.group()
def monitor():
    """Production monitors that fire Telegram alerts on detected anomalies.

    See HARDENING_PLAN.md Session 6 and OPERATIONS.md "Monitors" for
    schedules and intent. MONITORING_DRY_RUN=true suppresses real
    Telegram sends.
    """


@monitor.command("dashboard-consistency")
def monitor_dashboard_consistency():
    from monitoring import dashboard_consistency
    raise SystemExit(dashboard_consistency.main())


@monitor.command("pipeline-execution")
def monitor_pipeline_execution():
    from monitoring import pipeline_execution
    raise SystemExit(pipeline_execution.main())


@monitor.command("config-drift")
def monitor_config_drift():
    from monitoring import config_drift
    raise SystemExit(config_drift.main())


@monitor.command("model-performance")
def monitor_model_performance():
    from monitoring import model_performance
    raise SystemExit(model_performance.main())


@monitor.command("data-quality")
def monitor_data_quality():
    from monitoring import data_quality
    raise SystemExit(data_quality.main())


@monitor.command("smoke")
def monitor_smoke():
    from monitoring import smoke_test
    raise SystemExit(smoke_test.main())


@monitor.command("streamlit-freshness")
def monitor_streamlit_freshness():
    from monitoring import streamlit_freshness
    raise SystemExit(streamlit_freshness.main())


@monitor.command("dashboard-synthetic")
def monitor_dashboard_synthetic():
    from monitoring import dashboard_synthetic
    raise SystemExit(dashboard_synthetic.main())


@monitor.command("cross-artifact")
def monitor_cross_artifact():
    from monitoring import cross_artifact
    raise SystemExit(cross_artifact.main())


@monitor.command("phase0-calibration")
def monitor_phase0_calibration():
    from monitoring import phase0_calibration
    raise SystemExit(phase0_calibration.main())


@monitor.command("paper-trading-drift")
def monitor_paper_trading_drift():
    from monitoring import paper_trading_drift
    raise SystemExit(paper_trading_drift.main())


@monitor.command("crypto-pipeline")
def monitor_crypto_pipeline():
    """Daily crypto-pipeline monitor — one Telegram message, every step green/red/skipped."""
    from monitoring.pipeline_monitor import daily_runner
    raise SystemExit(daily_runner.main("crypto"))


@monitor.command("equity-pipeline")
def monitor_equity_pipeline():
    """Daily equity-pipeline monitor — one Telegram message, every step green/red/skipped."""
    from monitoring.pipeline_monitor import daily_runner
    raise SystemExit(daily_runner.main("equity"))


@monitor.command("fx-pipeline")
def monitor_fx_pipeline():
    """Daily FX-pipeline monitor — one Telegram message, every step green/red/skipped."""
    from monitoring.pipeline_monitor import daily_runner
    raise SystemExit(daily_runner.main("fx"))


@monitor.command("continuous")
def monitor_continuous():
    """Continuous monitor (FX bar freshness + crypto engine timers); alerts on red only."""
    from monitoring.pipeline_monitor import continuous_runner
    raise SystemExit(continuous_runner.main())


if __name__ == "__main__":
    cli()
