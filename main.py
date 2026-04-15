#!/usr/bin/env python3
"""MHDE Source Validation Harness — CLI entry point."""

from __future__ import annotations

import sys
import click

from runner.config_loader import load_settings, load_tickers
from runner.logger import setup_logging
from runner.runner import ValidationRunner
from runner.reporter import Reporter


@click.group()
def cli():
    """MHDE source validation harness."""


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


if __name__ == "__main__":
    cli()
