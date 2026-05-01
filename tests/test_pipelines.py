from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from storage.db import get_connection, init_schema


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


@pytest.fixture
def minimal_cfg():
    return {
        "universe": {"max_symbols": 10, "fallback_tickers": []},
        "llm": {"enabled": True, "default_provider": "mock", "max_candidates": 3},
        "notifications": {"telegram": {"enabled": False}, "email": {"enabled": False}},
        "scoring": {"weights": {}},
    }


def test_daily_radar_runs_without_crash(conn, minimal_cfg, tmp_path):
    with patch("universe.universe_builder.build_universe") as mock_build, \
         patch("ingestion.orchestrator.run_all"), \
         patch("features.feature_builder.build_features"), \
         patch("scoring.scorecard.compute_scores"), \
         patch("scoring.ranker.rank_tickers", return_value=[]), \
         patch("hypotheses.generator.generate_hypotheses", return_value=[]), \
         patch("hypotheses.rejection_logger.log_rejections"), \
         patch("reports.markdown_report.write_daily_report", return_value="outputs/test.md"), \
         patch("reports.json_report.write_json_report"), \
         patch("health.checks.run_all_checks", return_value=[]):
        mock_build.return_value = None
        conn.execute("INSERT INTO companies (ticker, company_name) VALUES ('AAPL', 'Apple')")

        from pipelines.daily_radar import run
        summary = run(minimal_cfg, conn)
        assert summary.run_id is not None
        assert summary.run_date is not None


def test_daily_radar_summary_has_all_fields(conn, minimal_cfg):
    with patch("universe.universe_builder.build_universe"), \
         patch("ingestion.orchestrator.run_all"), \
         patch("features.feature_builder.build_features"), \
         patch("scoring.scorecard.compute_scores"), \
         patch("scoring.ranker.rank_tickers", return_value=[]), \
         patch("hypotheses.generator.generate_hypotheses", return_value=[]), \
         patch("hypotheses.rejection_logger.log_rejections"), \
         patch("reports.markdown_report.write_daily_report", return_value="test.md"), \
         patch("reports.json_report.write_json_report"), \
         patch("health.checks.run_all_checks", return_value=[]):
        from pipelines.daily_radar import run, RunSummary
        summary = run(minimal_cfg, conn)
        assert isinstance(summary, RunSummary)
        assert isinstance(summary.warnings, list)
