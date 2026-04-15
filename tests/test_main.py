from click.testing import CliRunner
from main import cli


def test_cli_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "validate" in result.output or "Usage" in result.output


def test_validate_command_exists():
    runner = CliRunner()
    result = runner.invoke(cli, ["validate", "--help"])
    assert result.exit_code == 0


def test_validate_accepts_source_filter():
    runner = CliRunner()
    result = runner.invoke(cli, ["validate", "--help"])
    assert "--source" in result.output


def test_validate_accepts_tickers_flag():
    runner = CliRunner()
    result = runner.invoke(cli, ["validate", "--help"])
    assert "--ticker" in result.output
