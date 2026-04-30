from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

load_dotenv()

_DEFAULT_SETTINGS = Path(__file__).parent.parent / "config" / "settings.yaml"
_DEFAULT_TICKERS = Path(__file__).parent.parent / "config" / "tickers.yaml"

_ENV_KEY_MAP = {
    "POLYGON_API_KEY": ("polygon", "api_key"),
    "ALPHA_VANTAGE_API_KEY": ("alpha_vantage", "api_key"),
    "FRED_API_KEY": ("fred", "api_key"),
}


def load_settings(path: Optional[str] = None) -> dict:
    cfg_path = Path(path) if path else _DEFAULT_SETTINGS
    with open(cfg_path) as fh:
        settings = yaml.safe_load(fh) or {}
    for env_var, (section, key) in _ENV_KEY_MAP.items():
        value = os.getenv(env_var)
        if value:
            settings.setdefault(section, {})[key] = value
    return settings


def load_tickers(path: Optional[str] = None) -> list[dict]:
    cfg_path = Path(path) if path else _DEFAULT_TICKERS
    with open(cfg_path) as fh:
        data = yaml.safe_load(fh)
    return data.get("basket", [])
