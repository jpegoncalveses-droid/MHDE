from __future__ import annotations

import csv
import pytest


def test_yaml_cik_matches_sec():
    from universe.cik_validator import validate_cik_vs_sec
    yaml_entries = [{"ticker": "AAPL", "company_name": "Apple Inc", "cik": "0000320193"}]
    sec_map = {"AAPL": "0000320193"}
    corrected, report = validate_cik_vs_sec(yaml_entries, sec_map)
    assert corrected[0]["cik"] == "0000320193"
    assert report[0]["status"] == "matched"
    assert report[0]["chosen_cik"] == "0000320193"


def test_yaml_cik_corrected_from_sec():
    from universe.cik_validator import validate_cik_vs_sec
    yaml_entries = [{"ticker": "AAPL", "company_name": "Apple Inc", "cik": "9999999999"}]
    sec_map = {"AAPL": "0000320193"}
    corrected, report = validate_cik_vs_sec(yaml_entries, sec_map)
    assert corrected[0]["cik"] == "0000320193"
    assert report[0]["status"] == "corrected"
    assert report[0]["yaml_cik"] == "9999999999"
    assert report[0]["sec_cik"] == "0000320193"


def test_missing_in_sec_keeps_yaml_cik():
    from universe.cik_validator import validate_cik_vs_sec
    yaml_entries = [{"ticker": "BRK.B", "company_name": "Berkshire Hathaway", "cik": "0001067983"}]
    sec_map = {}
    corrected, report = validate_cik_vs_sec(yaml_entries, sec_map)
    assert corrected[0]["cik"] == "0001067983"
    assert report[0]["status"] == "missing_in_sec"
    assert report[0]["chosen_cik"] == "0001067983"


def test_yaml_no_cik_gets_sec_cik():
    from universe.cik_validator import validate_cik_vs_sec
    yaml_entries = [{"ticker": "MSFT", "company_name": "Microsoft Corp"}]
    sec_map = {"MSFT": "0000789019"}
    corrected, report = validate_cik_vs_sec(yaml_entries, sec_map)
    assert corrected[0]["cik"] == "0000789019"
    assert report[0]["status"] == "matched"
    assert report[0]["yaml_cik"] == ""


def test_write_validation_report(tmp_path):
    from universe.cik_validator import validate_cik_vs_sec, write_validation_report
    yaml_entries = [
        {"ticker": "AAPL", "company_name": "Apple Inc", "cik": "0000320193"},
        {"ticker": "GOOG", "company_name": "Alphabet", "cik": "WRONG"},
    ]
    sec_map = {"AAPL": "0000320193", "GOOG": "0001652044"}
    _, report = validate_cik_vs_sec(yaml_entries, sec_map)
    out = tmp_path / "report.csv"
    write_validation_report(report, out)
    with out.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["status"] == "matched"
    assert rows[1]["status"] == "corrected"
    assert set(rows[0].keys()) == {"ticker", "yaml_cik", "sec_cik", "chosen_cik", "status", "company_name"}
