from __future__ import annotations

from datetime import datetime
from pathlib import Path

_LOG_PATH = Path(__file__).parent.parent / "docs" / "decision_log.md"


def append_decision(description: str, rationale: str) -> None:
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y-%m-%d")
    entry = f"\n## {ts} — {description}\n\n{rationale}\n"
    with open(_LOG_PATH, "a") as fh:
        fh.write(entry)
