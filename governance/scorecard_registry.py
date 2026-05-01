from __future__ import annotations

from pathlib import Path

import yaml


def get_scorecard_config() -> dict:
    path = Path(__file__).parent.parent / "config" / "scoring.yaml"
    if not path.exists():
        return {}
    with open(path) as fh:
        return yaml.safe_load(fh) or {}
