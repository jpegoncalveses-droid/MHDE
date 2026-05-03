"""Move episode lifecycle: create, update, and resolve episodes.

An episode tracks a price-move sequence for a ticker from initial spike
through continuation, fading, and resolution. Episodes link to the
parent catalyst event when known.
"""
from __future__ import annotations

import datetime
import uuid
from enum import Enum
from typing import Optional


class EpisodeStatus(Enum):
    ACTIVE = "active"
    ACCELERATING = "accelerating"
    FADING = "fading"
    RESOLVED = "resolved"


def _today_str() -> str:
    return datetime.date.today().isoformat()


def create_or_update_episode(
    conn,
    ticker: str,
    date: str,
    return_1d: float,
    catalyst_id: Optional[str],
    inactivity_days: int = 30,
) -> str:
    """Create a new episode or update an existing open one. Returns episode_id.

    If an open episode exists for ticker and the gap since latest_date is within
    inactivity_days, the existing episode is updated. If the gap exceeds
    inactivity_days, the episode is resolved and a new one is created.
    """
    existing = conn.execute(
        """
        SELECT episode_id, latest_date, cumulative_return, max_1d_return
        FROM move_episodes
        WHERE ticker = ? AND status != 'resolved'
        ORDER BY latest_date DESC
        LIMIT 1
        """,
        [ticker],
    ).fetchone()

    if existing:
        ep_id, latest, cum_ret, old_max_1d = existing
        latest_date = datetime.date.fromisoformat(str(latest))
        event_date = datetime.date.fromisoformat(date)
        days_gap = (event_date - latest_date).days

        if days_gap > inactivity_days:
            conn.execute(
                "UPDATE move_episodes SET status = 'resolved' WHERE episode_id = ?",
                [ep_id],
            )
            return _new_episode(conn, ticker, date, return_1d, catalyst_id)

        new_cum = (cum_ret or 0.0) + return_1d
        new_max_1d = max(old_max_1d or 0.0, return_1d)
        conn.execute(
            """
            UPDATE move_episodes
            SET latest_date = ?, cumulative_return = ?, max_1d_return = ?
            WHERE episode_id = ?
            """,
            [date, new_cum, new_max_1d, ep_id],
        )
        return ep_id

    return _new_episode(conn, ticker, date, return_1d, catalyst_id)


def _new_episode(
    conn,
    ticker: str,
    date: str,
    return_1d: float,
    catalyst_id: Optional[str],
) -> str:
    ep_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO move_episodes
            (episode_id, ticker, start_date, latest_date,
             cumulative_return, max_1d_return, status, parent_catalyst_event_id)
        VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        [ep_id, ticker, date, date, return_1d, return_1d, catalyst_id],
    )
    return ep_id


def get_episode_status(
    conn,
    ticker: str,
    as_of_date: str,
    inactivity_days: int = 30,
) -> EpisodeStatus:
    """Return the current status of the most recent open episode for ticker.

    Returns EpisodeStatus.RESOLVED if no open episode exists or if the episode
    has been inactive for more than inactivity_days.
    """
    row = conn.execute(
        """
        SELECT latest_date, status
        FROM move_episodes
        WHERE ticker = ? AND status != 'resolved'
        ORDER BY latest_date DESC
        LIMIT 1
        """,
        [ticker],
    ).fetchone()

    if not row:
        return EpisodeStatus.RESOLVED

    latest, status_str = row
    latest_date = datetime.date.fromisoformat(str(latest))
    as_of = datetime.date.fromisoformat(as_of_date)
    days_gap = (as_of - latest_date).days

    if days_gap > inactivity_days:
        return EpisodeStatus.RESOLVED

    try:
        return EpisodeStatus(status_str)
    except ValueError:
        return EpisodeStatus.ACTIVE
