"""Tests for move episode lifecycle tracking."""
import datetime

import duckdb
import pytest

from missed.episode_tracker import (
    EpisodeStatus,
    create_or_update_episode,
    get_episode_status,
)


@pytest.fixture
def mem_db():
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE move_episodes (
            episode_id VARCHAR PRIMARY KEY,
            ticker VARCHAR NOT NULL,
            start_date DATE NOT NULL,
            latest_date DATE NOT NULL,
            cumulative_return DOUBLE DEFAULT 0,
            max_1d_return DOUBLE,
            max_3d_return DOUBLE,
            max_5d_return DOUBLE,
            status VARCHAR DEFAULT 'active',
            parent_catalyst_event_id VARCHAR,
            attribution_type VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    return conn


def test_create_new_episode(mem_db):
    ep_id = create_or_update_episode(mem_db, ticker="AAAB", date="2026-01-01",
                                     return_1d=0.08, catalyst_id=None)
    assert ep_id is not None
    row = mem_db.execute("SELECT * FROM move_episodes WHERE ticker='AAAB'").fetchone()
    assert row is not None


def test_episode_status_active_after_creation(mem_db):
    create_or_update_episode(mem_db, ticker="AAAB", date="2026-01-01",
                             return_1d=0.08, catalyst_id=None)
    status = get_episode_status(mem_db, ticker="AAAB", as_of_date="2026-01-02")
    assert status == EpisodeStatus.ACTIVE


def test_episode_updated_not_duplicated(mem_db):
    ep_id1 = create_or_update_episode(mem_db, ticker="AAAB", date="2026-01-01",
                                      return_1d=0.08, catalyst_id=None)
    ep_id2 = create_or_update_episode(mem_db, ticker="AAAB", date="2026-01-03",
                                      return_1d=0.03, catalyst_id=None)
    assert ep_id1 == ep_id2
    count = mem_db.execute("SELECT COUNT(*) FROM move_episodes").fetchone()[0]
    assert count == 1


def test_cumulative_return_accumulates(mem_db):
    create_or_update_episode(mem_db, ticker="AAAB", date="2026-01-01",
                             return_1d=0.08, catalyst_id=None)
    create_or_update_episode(mem_db, ticker="AAAB", date="2026-01-02",
                             return_1d=0.05, catalyst_id=None)
    row = mem_db.execute("SELECT cumulative_return FROM move_episodes").fetchone()
    assert abs(row[0] - 0.13) < 1e-9


def test_max_1d_return_tracked(mem_db):
    create_or_update_episode(mem_db, ticker="AAAB", date="2026-01-01",
                             return_1d=0.08, catalyst_id=None)
    create_or_update_episode(mem_db, ticker="AAAB", date="2026-01-02",
                             return_1d=0.15, catalyst_id=None)
    create_or_update_episode(mem_db, ticker="AAAB", date="2026-01-03",
                             return_1d=0.03, catalyst_id=None)
    row = mem_db.execute("SELECT max_1d_return FROM move_episodes").fetchone()
    assert abs(row[0] - 0.15) < 1e-9


def test_episode_resolved_after_inactivity(mem_db):
    create_or_update_episode(mem_db, ticker="AAAB", date="2026-01-01",
                             return_1d=0.08, catalyst_id=None)
    # 40 days later — exceeds default 30-day inactivity threshold
    status = get_episode_status(mem_db, ticker="AAAB",
                                as_of_date="2026-02-10", inactivity_days=30)
    assert status == EpisodeStatus.RESOLVED


def test_new_episode_created_after_inactivity(mem_db):
    ep_id1 = create_or_update_episode(mem_db, ticker="AAAB", date="2026-01-01",
                                      return_1d=0.08, catalyst_id=None)
    # 40 days later — old episode resolved, new one created
    ep_id2 = create_or_update_episode(mem_db, ticker="AAAB", date="2026-02-10",
                                      return_1d=0.06, catalyst_id=None, inactivity_days=30)
    assert ep_id1 != ep_id2
    count = mem_db.execute("SELECT COUNT(*) FROM move_episodes").fetchone()[0]
    assert count == 2


def test_resolved_episode_status(mem_db):
    # No open episode → RESOLVED
    status = get_episode_status(mem_db, ticker="AAAB", as_of_date="2026-01-01")
    assert status == EpisodeStatus.RESOLVED


def test_episode_status_enum_values():
    assert EpisodeStatus.ACTIVE.value == "active"
    assert EpisodeStatus.ACCELERATING.value == "accelerating"
    assert EpisodeStatus.FADING.value == "fading"
    assert EpisodeStatus.RESOLVED.value == "resolved"


def test_catalyst_id_stored(mem_db):
    ep_id = create_or_update_episode(mem_db, ticker="AAAB", date="2026-01-01",
                                     return_1d=0.08, catalyst_id="cat-123")
    row = mem_db.execute(
        "SELECT parent_catalyst_event_id FROM move_episodes WHERE episode_id = ?",
        [ep_id],
    ).fetchone()
    assert row[0] == "cat-123"
