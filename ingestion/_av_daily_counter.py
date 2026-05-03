"""Alpha Vantage daily call counter -- enforces 25-call free-tier cap."""
from __future__ import annotations
import datetime
import json
import logging
import os

logger = logging.getLogger(__name__)

_COUNTER_PATH = "data/processed/alpha_vantage_daily_usage.json"
AV_DAILY_CAP = 25


def _load() -> dict:
    try:
        with open(_COUNTER_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(_COUNTER_PATH)), exist_ok=True)
    with open(_COUNTER_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)


def get_remaining_calls(cap: int = AV_DAILY_CAP) -> int:
    """Return how many AV calls remain for today."""
    data = _load()
    today = str(datetime.date.today())
    if data.get("date") != today:
        return cap
    return max(0, cap - data.get("calls", 0))


def record_call(n: int = 1) -> int:
    """Increment today's call counter by n. Returns new total."""
    data = _load()
    today = str(datetime.date.today())
    if data.get("date") != today:
        data = {"date": today, "calls": 0}
    data["calls"] = data.get("calls", 0) + n
    _save(data)
    logger.debug("av_counter: %d/%d calls used today", data["calls"], AV_DAILY_CAP)
    return data["calls"]


def is_cap_reached(cap: int = AV_DAILY_CAP) -> bool:
    return get_remaining_calls(cap) == 0
