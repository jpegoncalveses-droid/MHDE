from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_CONFIG_DIR = Path(__file__).parent.parent / "config"
_DEFAULT_DB = "data/mhde.duckdb"

# MHDE host secrets (Telegram creds etc.) live in a gitignored file outside the
# repo so they survive the working tree being moved/removed. Overridable via
# MHDE_ENV_FILE (used by tests and alternate deployments).
_MHDE_ENV_FILE_DEFAULT = Path.home() / ".config" / "mhde" / "telegram.env"


def mhde_env_path() -> Path:
    override = os.environ.get("MHDE_ENV_FILE")
    return Path(override) if override else _MHDE_ENV_FILE_DEFAULT


def load_env_file(path: str | Path | None = None) -> None:
    """Load KEY=VALUE pairs from the MHDE host env file into os.environ.

    Uses os.environ.setdefault, so values already present in the environment
    (e.g. injected by a systemd EnvironmentFile) always win. A missing file is a
    no-op. Blank lines, `#` comments, and surrounding quotes are tolerated.
    """
    p = Path(path) if path else mhde_env_path()
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def load_engine_config(config_dir: str | Path | None = None) -> dict[str, Any]:
    d = Path(config_dir) if config_dir else _CONFIG_DIR

    # Pull host secrets (Telegram creds etc.) into the environment first so the
    # overlay below picks them up without depending on any other module's import.
    load_env_file()

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
        ("twelvedata_api_key", "TWELVEDATA_API_KEY"),
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
