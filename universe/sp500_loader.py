from __future__ import annotations

from pathlib import Path

import yaml


def load_sp500_yaml(yaml_path: str | Path) -> list[dict]:
    """Load S&P 500 tickers from YAML file. Returns empty list if file is absent or malformed."""
    path = Path(yaml_path)
    if not path.exists():
        return []
    with path.open() as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        return []
    return data.get("tickers", [])
