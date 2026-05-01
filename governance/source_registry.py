from __future__ import annotations

import logging
from pathlib import Path

import yaml

logger = logging.getLogger("mhde.governance.sources")

_SOURCES_CONFIG = Path(__file__).parent.parent / "config" / "sources.yaml"


def get_source_registry() -> list[dict]:
    if not _SOURCES_CONFIG.exists():
        return []
    with open(_SOURCES_CONFIG) as fh:
        data = yaml.safe_load(fh) or {}

    sources = data.get("sources", {})
    registry = []
    for name, info in sources.items():
        if isinstance(info, dict):
            registry.append({
                "name": name,
                "status": info.get("status", "unknown"),
                "description": info.get("description", ""),
                "auth_required": info.get("auth_required", False),
                "cost": info.get("cost", "free"),
            })
    return registry
