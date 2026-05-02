"""JSONL-backed cache for catalyst enrichment results.

Key: sha256(event_id + ":" + PROMPT_VERSION + ":" + provider + ":" + model)
Value: CatalystEnrichment.to_dict()

Full rewrite on every save (pilot size ≤ 100, so no append complexity needed).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os

logger = logging.getLogger("mhde.missed.catalyst_cache")

PROMPT_VERSION = "v1"


def cache_key(event_id: str, provider_name: str, model: str) -> str:
    payload = f"{event_id}:{PROMPT_VERSION}:{provider_name}:{model}"
    return hashlib.sha256(payload.encode()).hexdigest()


def load_cache(path: str) -> dict[str, dict]:
    """Load cache from JSONL file. Returns empty dict if file doesn't exist."""
    if not os.path.exists(path):
        return {}
    entries: dict[str, dict] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                key = record.get("_cache_key")
                if key:
                    entries[key] = record
    except Exception as exc:
        logger.warning("Failed to load cache from %s: %s", path, exc)
        return {}
    logger.debug("Loaded %d cache entries from %s", len(entries), path)
    return entries


def save_cache(path: str, entries: dict[str, dict]) -> None:
    """Write cache to JSONL file (full rewrite)."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            for key, record in entries.items():
                row = dict(record)
                row["_cache_key"] = key
                f.write(json.dumps(row) + "\n")
        logger.debug("Saved %d cache entries to %s", len(entries), path)
    except Exception as exc:
        logger.warning("Failed to save cache to %s: %s", path, exc)
