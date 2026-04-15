import csv
import json
import pytest
from pathlib import Path
from adapters.base import Scores, ValidationResult
from runner.reporter import Reporter
from tests.conftest import make_validation_result


@pytest.fixture
def two_results():
    return [
        make_validation_result("sec_edgar", "filings", "Core"),
        make_validation_result("polygon", "historical_prices", "Core",
                               scores=Scores(4, 5, 4, 4, 5, 3, 5)),
        make_validation_result("alpha_vantage", "transcripts", "Useful but optional",
                               scores=Scores(3, 3, 3, 3, 4, 3, 4),
                               rate_limit_notes="25 req/day"),
    ]


def test_write_json(tmp_path, two_results):
    r = Reporter(output_dir=str(tmp_path))
    path = r.write_json(two_results)
    assert path.exists()
    data = json.loads(path.read_text())
    assert len(data) == 3
    assert data[0]["source"] == "sec_edgar"
    assert "total" in data[0]["scores"]


def test_write_csv(tmp_path, two_results):
    r = Reporter(output_dir=str(tmp_path))
    path = r.write_csv(two_results)
    assert path.exists()
    rows = list(csv.DictReader(path.open()))
    assert len(rows) == 3
    assert rows[0]["source"] == "sec_edgar"
    assert "final_status" in rows[0]


def test_write_markdown(tmp_path, two_results):
    r = Reporter(output_dir=str(tmp_path))
    path = r.write_markdown(two_results)
    assert path.exists()
    text = path.read_text()
    assert "sec_edgar" in text
    assert "Core" in text
    assert "alpha_vantage" in text
    assert "## Summary" in text or "# MHDE" in text


def test_write_all_creates_three_files(tmp_path, two_results):
    r = Reporter(output_dir=str(tmp_path))
    paths = r.write_all(two_results)
    assert len(paths) == 3
    for p in paths:
        assert p.exists()
