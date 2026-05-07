"""Missed-opportunity CLI commands — TDD suite."""
from __future__ import annotations

from click.testing import CliRunner

from main import cli


def test_missed_detect_cli_exists():
    """python main.py missed detect runs without import errors."""
    runner = CliRunner()
    result = runner.invoke(cli, ["missed", "detect", "--help"])
    assert result.exit_code == 0, f"missed detect --help failed:\n{result.output}"


def test_missed_investigate_cli_exists():
    """python main.py missed investigate --help exits cleanly."""
    runner = CliRunner()
    result = runner.invoke(cli, ["missed", "investigate", "--help"])
    assert result.exit_code == 0, f"missed investigate --help failed:\n{result.output}"


def test_missed_report_cli_exists():
    """python main.py missed report --help exits cleanly."""
    runner = CliRunner()
    result = runner.invoke(cli, ["missed", "report", "--help"])
    assert result.exit_code == 0, f"missed report --help failed:\n{result.output}"


def test_missed_run_cli_exists():
    """python main.py missed run --help exits cleanly."""
    runner = CliRunner()
    result = runner.invoke(cli, ["missed", "run", "--help"])
    assert result.exit_code == 0, f"missed run --help failed:\n{result.output}"


def test_missed_group_exists():
    """python main.py missed --help lists subcommands."""
    runner = CliRunner()
    result = runner.invoke(cli, ["missed", "--help"])
    assert result.exit_code == 0
    assert "detect" in result.output or "run" in result.output, (
        f"missed group should list subcommands:\n{result.output}"
    )
