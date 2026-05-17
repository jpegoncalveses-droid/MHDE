"""Tests for crypto/ingestion/fear_greed_client.py.

Pattern matches MHDE conventions: pure-function tests on parser, HTTP
integration done at the backfill driver level (mock-injected) rather
than mocking requests directly.
"""
from datetime import date

import pytest

from crypto.ingestion.fear_greed_client import (
    ALTERNATIVE_ME_FNG_URL,
    FearGreedClient,
    parse_fng_row,
)


def test_url_constant_points_at_alternative_me():
    assert ALTERNATIVE_ME_FNG_URL == "https://api.alternative.me/fng/"


def test_parse_fng_row_extracts_value_and_date():
    raw = {
        "value": "53",
        "value_classification": "Neutral",
        "timestamp": "1735689600",  # 2025-01-01 00:00 UTC
        "time_until_update": "12345",
    }
    parsed = parse_fng_row(raw)
    assert parsed["date"] == date(2025, 1, 1)
    assert parsed["value"] == 53
    assert parsed["value_classification"] == "Neutral"


def test_parse_fng_row_coerces_value_to_int():
    """API returns value as string; we store as INTEGER."""
    parsed = parse_fng_row({
        "value": "0",
        "value_classification": "Extreme Fear",
        "timestamp": "1700000000",
    })
    assert isinstance(parsed["value"], int)
    assert parsed["value"] == 0


def test_parse_fng_row_handles_missing_classification():
    """Some early historical rows have no classification — accept None."""
    parsed = parse_fng_row({
        "value": "75",
        "timestamp": "1700000000",
    })
    assert parsed["value_classification"] is None


def test_fetch_history_params_uses_limit_zero_for_full_history():
    """alternative.me: limit=0 returns full available history."""
    client = FearGreedClient()
    params = client._params(limit=0)
    assert params == {"limit": "0", "format": "json"}


def test_fetch_history_params_with_specific_limit():
    client = FearGreedClient()
    params = client._params(limit=30)
    assert params == {"limit": "30", "format": "json"}


def test_client_parses_response_payload(monkeypatch):
    """End-to-end: monkeypatched _get returns a fake payload, fetch returns
    parsed rows ready for DB insert."""
    fake_payload = {
        "name": "Fear and Greed Index",
        "data": [
            {"value": "55", "value_classification": "Neutral", "timestamp": "1735689600"},
            {"value": "30", "value_classification": "Fear", "timestamp": "1735603200"},
        ],
        "metadata": {"error": None},
    }
    client = FearGreedClient()
    monkeypatch.setattr(client, "_get", lambda url, params=None: fake_payload)
    rows = client.fetch_history(limit=2)
    assert len(rows) == 2
    assert rows[0]["date"] == date(2025, 1, 1)
    assert rows[0]["value"] == 55
    assert rows[1]["date"] == date(2024, 12, 31)


def test_client_raises_on_api_error(monkeypatch):
    """alternative.me returns metadata.error if rate-limited / failed."""
    fake_payload = {"data": [], "metadata": {"error": "rate limit exceeded"}}
    client = FearGreedClient()
    monkeypatch.setattr(client, "_get", lambda url, params=None: fake_payload)
    with pytest.raises(RuntimeError, match="rate limit"):
        client.fetch_history(limit=10)
