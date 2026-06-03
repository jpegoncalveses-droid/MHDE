"""Tests for capture-core load-test sizing math."""
from __future__ import annotations

import pytest

from crypto.research.capture_core import loadtest


def test_summarize_computes_rates_and_daily_projection():
    s = loadtest.summarize(messages=1000, bytes_in=100_000, duration_s=10.0,
                           n_symbols=529)
    assert s["msgs_per_s"] == pytest.approx(100.0)
    assert s["raw_bytes_per_s"] == pytest.approx(10_000.0)
    # 10 KB/s * 86400 s = 864 MB/day = 0.864 GB/day
    assert s["raw_gb_per_day"] == pytest.approx(0.864)
    assert s["n_symbols"] == 529


def test_summarize_includes_parquet_compression_when_provided():
    s = loadtest.summarize(messages=1000, bytes_in=100_000, duration_s=10.0,
                           n_symbols=529, parquet_bytes=25_000)
    assert s["compression_ratio"] == pytest.approx(4.0)
    assert s["parquet_gb_per_day"] == pytest.approx(0.864 / 4.0)


def test_summarize_handles_zero_duration_safely():
    s = loadtest.summarize(messages=0, bytes_in=0, duration_s=0.0, n_symbols=0)
    assert s["msgs_per_s"] == 0.0
    assert s["raw_gb_per_day"] == 0.0
