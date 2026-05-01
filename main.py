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
def daily_radar():
    """Run the full daily opportunity discovery pipeline."""
    from pipelines.daily_radar import run as pipeline_run
    cfg, conn = _engine_setup()
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
    from health.checks import run_all_checks

    cfg, conn = _engine_setup()
    run_id = uuid.uuid4().hex[:16]
    try:
        results = run_all_checks(conn, run_id, cfg)
        click.echo(f"\n{'Check':<35} {'Status':<8} {'Severity':<10} Message")
        click.echo("-" * 80)
        for r in results:
            click.echo(
                f"{r['check_name']:<35} {r['status']:<8} {r.get('severity', ''):<10} {r.get('message', '')}"
            )
        failed = [r for r in results if r["status"] == "fail"]
        warned = [r for r in results if r["status"] == "warn"]
        click.echo(f"\n{len(results)} checks: {len(failed)} failed, {len(warned)} warnings.")
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
