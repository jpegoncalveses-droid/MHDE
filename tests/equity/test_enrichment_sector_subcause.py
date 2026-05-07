"""Tests for sector_cluster_subcause field added to root-cause enrichment."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from storage.db import get_connection, init_schema


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _row(**kwargs) -> dict:
    defaults = dict(
        ticker="TST", event_date=date.today() - timedelta(days=10),
        event_type="gain_1d", return_value=10.0, window_days=1,
        classification="true_miss", was_in_universe=True, was_scored=True,
        score_before_event=30.0, tier_before_event="Reject",
        had_catalyst_evidence=True, universe_tier="primary",
        root_cause_hint="scoring_blind_spot", score_join_method="scores_join",
        priority_score=5.3,
    )
    defaults.update(kwargs)
    return defaults


def _insert_company(conn, ticker, sector):
    conn.execute(
        "INSERT INTO companies (ticker, company_name, sector, universe_tier) "
        "VALUES (?, ?, ?, 'extended') ON CONFLICT (ticker) DO UPDATE SET sector = excluded.sector",
        [ticker, ticker, sector],
    )


def _insert_etf_price(conn, etf_ticker, trade_date="2026-05-01"):
    conn.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES (?, ?, ?, 1.0)",
        [uuid.uuid4().hex[:16], etf_ticker, trade_date],
    )


def _make_cluster_rows(event_date=None, window_days=1):
    ed = event_date or (date.today() - timedelta(days=10))
    return [
        _row(ticker="TST", event_date=ed, tier_before_event="Reject", window_days=window_days),
        _row(ticker="P1",  event_date=ed, tier_before_event="Reject", window_days=window_days),
        _row(ticker="P2",  event_date=ed, tier_before_event="Reject", window_days=window_days),
    ]


def test_sector_cluster_subcause_field_present_in_all_rows(conn):
    """sector_cluster_subcause must exist in every enriched row regardless of root cause."""
    from missed.root_cause_enrichment import enrich_rows
    rows = [_row(tier_before_event="Reject")]
    enriched = enrich_rows(rows, conn)
    assert "sector_cluster_subcause" in enriched[0]


def test_non_cluster_rows_have_empty_sector_cluster_subcause(conn):
    """Non-sector_cluster_move rows must have empty sector_cluster_subcause."""
    from missed.root_cause_enrichment import enrich_rows
    row = _row(classification="universe_miss", was_in_universe=False, tier_before_event="")
    enriched = enrich_rows([row], conn)
    assert enriched[0].get("sector_cluster_subcause", "MISSING") == ""


def test_sector_cluster_subcause_peer_cluster_only_when_etf_present(conn):
    """sector_cluster_subcause = peer_cluster_only_no_etf_data when ETF prices exist."""
    from missed.root_cause_enrichment import enrich_rows
    for ticker in ("TST", "P1", "P2"):
        _insert_company(conn, ticker, "Information Technology")
    _insert_etf_price(conn, "XLK")
    rows = _make_cluster_rows()
    enriched = enrich_rows(rows, conn)
    tst = next(r for r in enriched if r["ticker"] == "TST")
    assert tst["enriched_root_cause"] == "sector_cluster_move"
    assert tst["sector_cluster_subcause"] == "peer_cluster_only_no_etf_data"


def test_sector_cluster_subcause_missing_etf_prices_when_no_etf_data(conn):
    """sector_cluster_subcause = missing_sector_etf_prices when no ETF rows in prices_daily."""
    from missed.root_cause_enrichment import enrich_rows
    for ticker in ("TST", "P1", "P2"):
        _insert_company(conn, ticker, "Information Technology")
    # No ETF prices inserted
    rows = _make_cluster_rows()
    enriched = enrich_rows(rows, conn)
    tst = next(r for r in enriched if r["ticker"] == "TST")
    assert tst["enriched_root_cause"] == "sector_cluster_move"
    assert tst["sector_cluster_subcause"] == "missing_sector_etf_prices"
