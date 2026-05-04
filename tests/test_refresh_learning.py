"""Tests for missed refresh-learning CLI command."""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

from click.testing import CliRunner

from main import missed


def test_refresh_learning_runs_pva_then_enrich_in_order():
    """refresh-learning must run prediction-vs-actual before enrich-root-causes."""
    call_order = []

    def fake_pva(conn, output_dir, lookback_days):
        call_order.append("pva")
        return ("/tmp/r.md", "/tmp/r.csv", "/tmp/r.jsonl")

    def fake_enrich(rows, conn):
        call_order.append("enrich")
        return rows

    def fake_report(enriched, output_dir):
        return "/tmp/e.csv", "/tmp/e.md"

    runner = CliRunner()
    with runner.isolated_filesystem():
        import os, duckdb
        os.makedirs("data/processed", exist_ok=True)
        # Minimal DB
        conn = duckdb.connect("data/mhde.duckdb")
        conn.execute("CREATE TABLE prices_daily (id VARCHAR, ticker VARCHAR, trade_date DATE, close DOUBLE)")
        conn.execute("INSERT INTO prices_daily VALUES ('x','AAPL','2026-05-01',100.0)")
        conn.execute("CREATE TABLE companies (ticker VARCHAR PRIMARY KEY, company_name VARCHAR, universe_tier VARCHAR, is_active BOOLEAN, sector VARCHAR, industry VARCHAR, universe_exclusion_reason VARCHAR, last_financial_filing_date DATE)")
        conn.execute("CREATE TABLE scores (ticker VARCHAR, as_of_date DATE, total_score DOUBLE, tier VARCHAR, why_rejected VARCHAR, missing_data_json VARCHAR, created_at TIMESTAMP)")
        conn.close()

        # Write a minimal PvA CSV so enrich step can proceed
        with open("data/processed/prediction_vs_actual_rows.csv", "w") as f:
            f.write("event_date,ticker,classification,return_value,window_days,event_type,"
                    "was_in_universe,was_scored,score_before_event,priority_score,"
                    "tier_before_event,had_catalyst_evidence,investigation_status\n")
            f.write("2026-05-01,AAPL,true_miss,0.12,5,gain_5d_10pct,"
                    "True,True,35.0,,Reject,False,pending\n")

        with (
            patch("missed.prediction_report.generate_prediction_report", side_effect=fake_pva),
            patch("missed.root_cause_enrichment.enrich_rows", side_effect=fake_enrich),
            patch("missed.root_cause_enrichment.generate_enrichment_report", side_effect=fake_report),
        ):
            result = runner.invoke(missed, ["refresh-learning"])

    assert result.exit_code == 0, result.output
    assert call_order.index("pva") < call_order.index("enrich"), \
        "prediction-vs-actual must run before enrich-root-causes"


def test_refresh_learning_shows_stale_warning_when_stale():
    """refresh-learning warns when PvA is stale before running."""
    runner = CliRunner()

    def fake_pva(conn, output_dir, lookback_days):
        return ("/tmp/r.md", "/tmp/r.csv", "/tmp/r.jsonl")

    def fake_enrich(rows, conn):
        return rows

    def fake_report(enriched, output_dir):
        return "/tmp/e.csv", "/tmp/e.md"

    with runner.isolated_filesystem():
        import os, duckdb
        os.makedirs("data/processed", exist_ok=True)
        conn = duckdb.connect("data/mhde.duckdb")
        conn.execute("CREATE TABLE prices_daily (id VARCHAR, ticker VARCHAR, trade_date DATE, close DOUBLE)")
        conn.execute("INSERT INTO prices_daily VALUES ('x','AAPL','2026-05-01',100.0)")
        conn.execute("CREATE TABLE companies (ticker VARCHAR PRIMARY KEY, company_name VARCHAR, universe_tier VARCHAR, is_active BOOLEAN, sector VARCHAR, industry VARCHAR, universe_exclusion_reason VARCHAR, last_financial_filing_date DATE)")
        conn.execute("CREATE TABLE scores (ticker VARCHAR, as_of_date DATE, total_score DOUBLE, tier VARCHAR, why_rejected VARCHAR, missing_data_json VARCHAR, created_at TIMESTAMP)")
        conn.close()

        # PvA only covers through 2026-04-30 — stale relative to 2026-05-01 prices
        with open("data/processed/prediction_vs_actual_rows.csv", "w") as f:
            f.write("event_date,ticker,classification,return_value,window_days,event_type,"
                    "was_in_universe,was_scored,score_before_event,priority_score,"
                    "tier_before_event,had_catalyst_evidence,investigation_status\n")
            f.write("2026-04-30,AAPL,true_miss,0.12,5,gain_5d_10pct,"
                    "True,True,35.0,,Reject,False,pending\n")

        with (
            patch("missed.prediction_report.generate_prediction_report", side_effect=fake_pva),
            patch("missed.root_cause_enrichment.enrich_rows", side_effect=fake_enrich),
            patch("missed.root_cause_enrichment.generate_enrichment_report", side_effect=fake_report),
        ):
            result = runner.invoke(missed, ["refresh-learning"])

    assert result.exit_code == 0, result.output
    assert "stale" in result.output.lower()


def test_refresh_learning_no_scoring_changes():
    """refresh-learning command must not touch scoring logic."""
    import inspect
    import main as _main
    # Click wraps the function; get the callback for source inspection
    fn = _main.missed_refresh_learning.callback
    src = inspect.getsource(fn)
    for bad in ("feature_flag", "FeatureFlag", "openai", "anthropic"):
        assert bad.lower() not in src.lower(), f"Prohibited term '{bad}' in refresh-learning"
