from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_CONFIG_DIR = Path(__file__).parent.parent / "config"
_DEFAULT_DB = "data/mhde.duckdb"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def load_engine_config(config_dir: str | Path | None = None) -> dict[str, Any]:
    d = Path(config_dir) if config_dir else _CONFIG_DIR

    cfg: dict[str, Any] = {
        "universe": _load_yaml(d / "universe.yaml"),
        "sources": _load_yaml(d / "sources.yaml"),
        "scoring": _load_yaml(d / "scoring.yaml"),
        "llm": _load_yaml(d / "llm.yaml"),
        "notifications": _load_yaml(d / "notifications.yaml"),
        "settings": _load_yaml(d / "settings.yaml"),
        "db_path": os.environ.get("MHDE_DB_PATH", _DEFAULT_DB),
    }

    # Overlay env vars for API keys
    for key, env in [
        ("polygon_api_key", "POLYGON_API_KEY"),
        ("fred_api_key", "FRED_API_KEY"),
        ("openai_api_key", "OPENAI_API_KEY"),
        ("nvidia_api_key", "NVIDIA_API_KEY"),
        ("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
        ("telegram_chat_id", "TELEGRAM_CHAT_ID"),
        ("smtp_host", "SMTP_HOST"),
        ("smtp_port", "SMTP_PORT"),
        ("smtp_username", "SMTP_USERNAME"),
        ("smtp_password", "SMTP_PASSWORD"),
        ("email_from", "EMAIL_FROM"),
        ("email_to", "EMAIL_TO"),
    ]:
        val = os.environ.get(env)
        if val:
            cfg[key] = val

    return cfg
