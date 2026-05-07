from __future__ import annotations

import pytest
from datetime import date

from storage.db import get_connection, init_schema
from backtest.smoke_test import run_smoke
from backtest.metrics import compute_metrics
from backtest.labels import compute_labels


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def test_smoke_runs_without_data(conn):
    result = run_smoke(conn, {})
    assert "warning" in result
    assert "Experimental" in result["warning"] or "WARNING" in result["warning"]


def test_smoke_persists_backtest_run(conn):
    run_smoke(conn, {})
    count = conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0]
    assert count == 1


def test_compute_metrics_empty():
    metrics = compute_metrics([])
    assert metrics["tickers"] == 0
    assert metrics["hit_rate"] is None


def test_compute_metrics_with_data():
    labels = [
        {"ticker": "A", "forward_return": 0.10},
        {"ticker": "B", "forward_return": -0.05},
        {"ticker": "C", "forward_return": 0.20},
    ]
    metrics = compute_metrics(labels)
    assert metrics["tickers"] == 3
    assert metrics["hit_rate"] == pytest.approx(2 / 3)
    assert metrics["avg_return"] == pytest.approx(0.25 / 3)


def test_compute_labels_insufficient_data(conn):
    labels = compute_labels(conn, ["AAPL", "NVDA"], date.today(), forward_days=20)
    assert labels == []
