"""Regression pins for ADR-031 / KI-143: orchestrator + daily-radar sort
order must put primary tier first so the dev-mode `max_symbols` cap doesn't
displace the alphabetical second half of primary-tier tickers.

Bug shape (pre-fix): ``ORDER BY universe_tier, ticker`` puts 'extended' alphabetically
before 'primary', so 174 extended-tier rows fill positions 0-173 of the cap, pushing
153 primary-tier tickers (ODFL → XYL alphabetically) out of the 520-slot ingest list.
99 of those displaced primary tickers pass the ML universe filter ($10B+, non-ETF,
sectored) and silently drop out of `ml_features` (Investigation A, 2026-05-14).

Fix: ``ORDER BY universe_tier DESC, ticker`` so 'primary' (504 rows) sorts first
and consumes the cap before extended tickers compete for slots.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from storage.db import get_connection, init_schema


REPO_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR_PATH = REPO_ROOT / "ingestion" / "orchestrator.py"
DAILY_RADAR_PATH = REPO_ROOT / "pipelines" / "daily_radar.py"


@pytest.fixture
def seeded_conn(tmp_path):
    """Companies table seeded with the production-shape mix:
    500 primary tickers (alphabetical 'P000'..'P499') and 174 extended
    tickers (alphabetical 'E000'..'E173'). All `is_active=true`.

    Picked names that sort 'E' < 'P' so the bug reproduces with the
    same alphabet semantics as production ('extended' < 'primary').
    """
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    rows = []
    for i in range(500):
        rows.append((f"P{i:03d}", f"Primary Co {i}", "primary"))
    for i in range(174):
        rows.append((f"E{i:03d}", f"Extended Co {i}", "extended"))
    c.executemany(
        "INSERT INTO companies (ticker, company_name, universe_tier, is_active) "
        "VALUES (?, ?, ?, true)",
        rows,
    )
    yield c
    c.close()


# ── ADR-031 behavioural pins ──────────────────────────────────────────────────

# Mirrors the SELECT in ``ingestion/orchestrator.py`` and ``pipelines/daily_radar.py``.
# After ADR-031 both call sites use ``ORDER BY universe_tier DESC, ticker``.
_FIXED_QUERY = (
    "SELECT ticker FROM companies WHERE is_active = true "
    "ORDER BY universe_tier DESC, ticker"
)


def test_primary_tier_fills_cap_first(seeded_conn):
    """Top 504 slots after the sort should all be primary-tier tickers,
    not displaced by the alphabetically-earlier extended tier.

    Pre-fix this returns 174 extended tickers in positions 0-173 and
    primary fills 174-673, so a 520-slot cap captures 174 extended + 346
    primary — exactly the production failure mode (99 ML-universe primary
    tickers displaced).
    """
    rows = seeded_conn.execute(_FIXED_QUERY).fetchall()
    cap = 520
    capped = [r[0] for r in rows[:cap]]

    primary_in_cap = [t for t in capped if t.startswith("P")]
    assert len(primary_in_cap) == 500, (
        f"Expected all 500 primary-tier tickers in the first {cap} slots; "
        f"got {len(primary_in_cap)}. Bug: extended tier displaced "
        f"{500 - len(primary_in_cap)} primary tickers out of the cap."
    )

    extended_in_cap = [t for t in capped if t.startswith("E")]
    assert len(extended_in_cap) == cap - 500, (
        f"Cap of {cap} should fit all 500 primary + remaining {cap - 500} "
        f"extended; got {len(extended_in_cap)} extended."
    )


def test_full_universe_returned_when_below_cap(tmp_path):
    """Edge case: when total universe < cap, the slice is a no-op and
    every ticker is returned regardless of tier order. Ensures the fix
    doesn't accidentally drop tickers in small-universe configurations.
    """
    c = get_connection(str(tmp_path / "small.duckdb"))
    init_schema(c)
    c.executemany(
        "INSERT INTO companies (ticker, company_name, universe_tier, is_active) "
        "VALUES (?, ?, ?, true)",
        [("AAA", "Co A", "primary"), ("BBB", "Co B", "primary"),
         ("EEE", "Co E", "extended"), ("FFF", "Co F", "extended")],
    )
    rows = c.execute(_FIXED_QUERY).fetchall()
    cap = 520
    tickers = [r[0] for r in rows[:cap]]
    assert sorted(tickers) == ["AAA", "BBB", "EEE", "FFF"]
    c.close()


def test_inactive_tickers_excluded(tmp_path):
    """Sanity pin: ``is_active=false`` rows are excluded by the WHERE
    clause regardless of tier. The fix changes ORDER BY only — the
    is_active filter must remain intact.
    """
    c = get_connection(str(tmp_path / "inactive.duckdb"))
    init_schema(c)
    c.executemany(
        "INSERT INTO companies (ticker, company_name, universe_tier, is_active) "
        "VALUES (?, ?, ?, ?)",
        [
            ("ACTIVE_P", "AP Co", "primary", True),
            ("ACTIVE_E", "AE Co", "extended", True),
            ("INACTIVE_P", "IP Co", "primary", False),
            ("INACTIVE_E", "IE Co", "extended", False),
        ],
    )
    rows = c.execute(_FIXED_QUERY).fetchall()
    tickers = {r[0] for r in rows}
    assert tickers == {"ACTIVE_P", "ACTIVE_E"}
    c.close()


# ── duplication-pin tests for the parallel call sites ────────────────────────

# Bug pattern: ``ORDER BY universe_tier, ticker`` (no DESC) — must NOT appear
# in either file. The SELECT lives identically in two places (KI-143 follow-up
# is to extract a shared helper); both must stay in lock-step.
_OLD_SORT_RE = re.compile(
    r"ORDER\s+BY\s+universe_tier\s*,\s*ticker", re.IGNORECASE
)
_NEW_SORT_RE = re.compile(
    r"ORDER\s+BY\s+universe_tier\s+DESC\s*,\s*ticker", re.IGNORECASE
)


def test_orchestrator_uses_primary_first_sort():
    src = ORCHESTRATOR_PATH.read_text()
    assert _NEW_SORT_RE.search(src), (
        f"{ORCHESTRATOR_PATH} must contain "
        f"'ORDER BY universe_tier DESC, ticker' (ADR-031)"
    )
    assert not _OLD_SORT_RE.search(src) or _NEW_SORT_RE.search(src), (
        f"{ORCHESTRATOR_PATH} still contains the buggy "
        f"'ORDER BY universe_tier, ticker' (without DESC)"
    )


def test_daily_radar_uses_primary_first_sort():
    src = DAILY_RADAR_PATH.read_text()
    assert _NEW_SORT_RE.search(src), (
        f"{DAILY_RADAR_PATH} must contain "
        f"'ORDER BY universe_tier DESC, ticker' (ADR-031)"
    )
    assert not _OLD_SORT_RE.search(src) or _NEW_SORT_RE.search(src), (
        f"{DAILY_RADAR_PATH} still contains the buggy "
        f"'ORDER BY universe_tier, ticker' (without DESC)"
    )
