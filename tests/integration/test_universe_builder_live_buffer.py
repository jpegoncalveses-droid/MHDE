"""Host-only regression check for the DuckDB 1.5.2 DISTINCT+DESC+LIMIT collapse.

This is the ONLY test that actually reproduces the optimizer bug fixed in
``_recent_ranking_dates`` (see KNOWN_ISSUES.md — "DuckDB 1.5.2 DISTINCT + ORDER
BY DESC + LIMIT collapse"). The collapse is layout/statistics-sensitive: it only
manifests against the real, incrementally written ``data/mhde.duckdb`` —
synthetic buffers and even a CTAS copy of the live table always return the
correct rows (verified during the investigation). Therefore this test:

  * is NOT a CI gate. It skips unless the live DB is present with at least
    ``HYSTERESIS_DAYS`` distinct ranking_dates, so it no-ops in CI and on fresh
    checkouts.
  * is point-in-time. The triggering storage layout drifts as
    ``rank-universe-daily`` appends a date each day, so a future PASS does not
    prove the bug is gone — only that the buffer's current layout no longer
    happens to trigger it. The durable logic guard is the synthetic unit test
    in ``tests/crypto/test_universe_builder_recent_dates.py``.

RED  (pre-fix ``DISTINCT ... ORDER BY DESC LIMIT 7``): returns 1 date  -> fails.
GREEN (fetch-all-DESC + Python slice):                 returns 7 dates -> passes.

When running from outside the repo root (e.g. a git worktree), point it at the
live DB with ``MHDE_DB_PATH=/abs/path/to/data/mhde.duckdb``.
"""
from __future__ import annotations

import os

import duckdb
import pytest

from crypto.ingestion.universe_builder import HYSTERESIS_DAYS, _recent_ranking_dates

_LIVE_DB = os.path.abspath(os.environ.get("MHDE_DB_PATH", "data/mhde.duckdb"))


def _distinct_ranking_dates(path: str) -> int:
    """Read-only count of distinct ranking_dates; -1 if unavailable/locked."""
    try:
        con = duckdb.connect(path, read_only=True)
    except Exception:
        return -1
    try:
        return con.execute(
            "SELECT COUNT(DISTINCT ranking_date) FROM crypto_universe_ranking_buffer"
        ).fetchone()[0]
    except Exception:
        return -1
    finally:
        con.close()


_AVAILABLE = os.path.exists(_LIVE_DB) and _distinct_ranking_dates(_LIVE_DB) >= HYSTERESIS_DAYS


@pytest.mark.skipif(
    not _AVAILABLE,
    reason=(
        f"host-only: live buffer with >={HYSTERESIS_DAYS} distinct ranking_dates "
        f"not present at {_LIVE_DB}"
    ),
)
def test_recent_ranking_dates_returns_full_window_on_live_buffer():
    """On the live DB, the hysteresis window must hold HYSTERESIS_DAYS dates.

    Pre-fix this returned 1 (the DuckDB DISTINCT+DESC+LIMIT collapse), which made
    ``build_universe`` raise "has only 1 distinct dates; need 7" against a
    perfectly healthy buffer and crash-loop the daily rebuild.
    """
    con = duckdb.connect(_LIVE_DB, read_only=True)
    try:
        dates = _recent_ranking_dates(con, HYSTERESIS_DAYS)
    finally:
        con.close()

    assert len(dates) == HYSTERESIS_DAYS, (
        f"expected {HYSTERESIS_DAYS} most-recent distinct ranking_dates, got "
        f"{len(dates)}: {dates} — DuckDB DISTINCT+DESC+LIMIT collapse "
        f"(see KNOWN_ISSUES.md)"
    )
    assert dates == sorted(dates, reverse=True), "dates must be newest-first"
    assert len(set(dates)) == len(dates), "dates must be distinct"
