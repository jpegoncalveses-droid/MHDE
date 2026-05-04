"""Tests for main.py CLI commands."""
from click.testing import CliRunner
from main import cli


def test_data_sector_diagnostics_command_exists():
    runner = CliRunner()
    result = runner.invoke(cli, ["data", "sector-diagnostics", "--help"])
    assert result.exit_code == 0
    assert "sector" in result.output.lower()
