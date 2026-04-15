import os
import pytest
from runner.config_loader import load_settings, load_tickers


def test_load_settings_returns_dict(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text("http:\n  timeout: 15\n")
    result = load_settings(str(cfg))
    assert result["http"]["timeout"] == 15


def test_load_settings_injects_env_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test_key_123")
    cfg = tmp_path / "settings.yaml"
    cfg.write_text("polygon:\n  base_url: https://api.polygon.io\n")
    result = load_settings(str(cfg))
    assert result["polygon"]["api_key"] == "test_key_123"


def test_load_tickers_returns_list(tmp_path):
    cfg = tmp_path / "tickers.yaml"
    cfg.write_text("basket:\n  - ticker: AAPL\n    name: Apple\n    cik: '0000320193'\n    type: stock\n")
    result = load_tickers(str(cfg))
    assert len(result) == 1
    assert result[0]["ticker"] == "AAPL"


def test_load_tickers_default_path():
    # Uses actual config/tickers.yaml
    result = load_tickers()
    tickers = [t["ticker"] for t in result]
    assert "AAPL" in tickers
    assert "IWM" in tickers
    assert len(result) == 8
