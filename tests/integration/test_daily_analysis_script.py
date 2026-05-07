"""Tests for run_mhde_daily_analysis.sh — syntax, executability, and env handling."""
import os
import subprocess

SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "..", ".claude", "local_scripts", "run_mhde_daily_analysis.sh"
)


def test_script_exists():
    assert os.path.exists(SCRIPT), f"Script not found: {SCRIPT}"


def test_script_is_executable():
    assert os.access(SCRIPT, os.X_OK), "Script is not executable"


def test_script_bash_syntax():
    result = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
    assert result.returncode == 0, f"Syntax error: {result.stderr}"


def test_no_secrets_hardcoded_in_script():
    """Script must not contain any hardcoded secrets."""
    with open(SCRIPT) as fh:
        content = fh.read()
    for pattern in ["sk-", "=sk-", "password=", "api_key="]:
        assert pattern not in content.lower(), f"Possible hardcoded secret: {pattern}"


def test_email_guard_present():
    """Email step must be conditional on MHDE_SEND_EMAIL and SMTP_HOST."""
    with open(SCRIPT) as fh:
        content = fh.read()
    assert "MHDE_SEND_EMAIL" in content
    assert "SMTP_HOST" in content


def test_skip_ingestion_guard_present():
    """Script must support MHDE_DAILY_SKIP_INGESTION=true to skip data fetching."""
    with open(SCRIPT) as fh:
        content = fh.read()
    assert "MHDE_DAILY_SKIP_INGESTION" in content
    assert "skip-ingestion" in content


def test_log_dir_creation_present():
    """Script must create data/logs/ before writing log file."""
    with open(SCRIPT) as fh:
        content = fh.read()
    assert "mkdir -p" in content
    assert "data/logs" in content
