"""TDD tests for SEC XBRL ifrs-full namespace ingestion.

RED state: these tests fail because SECIngestor only reads us-gaap.
"""
from __future__ import annotations

import uuid

import pytest
import responses as rsps_lib

from storage.db import get_connection, init_schema

_CIK_PADDED = "0000123456"
_CIK_BARE = "123456"

COMPANYFACTS_IFRS = {
    "cik": 123456,
    "entityName": "Foreign Corp Ltd.",
    "facts": {
        "ifrs-full": {
            "Revenue": {
                "label": "Revenue",
                "units": {"USD": [
                    {"end": "2024-12-31", "val": 5_000_000_000, "form": "20-F", "filed": "2025-03-01"},
                    {"end": "2023-12-31", "val": 4_500_000_000, "form": "20-F", "filed": "2024-03-01"},
                ]},
            },
            "ProfitLoss": {
                "label": "Profit (Loss)",
                "units": {"USD": [
                    {"end": "2024-12-31", "val": 800_000_000, "form": "20-F", "filed": "2025-03-01"},
                ]},
            },
            "CurrentAssets": {
                "label": "Current Assets",
                "units": {"USD": [
                    {"end": "2024-12-31", "val": 2_000_000_000, "form": "20-F", "filed": "2025-03-01"},
                ]},
            },
            "SomethingUnmapped": {
                "label": "Irrelevant concept",
                "units": {"USD": [
                    {"end": "2024-12-31", "val": 999, "form": "20-F"},
                ]},
            },
        },
    },
}

COMPANYFACTS_BOTH = {
    "cik": 789012,
    "entityName": "Hybrid Corp",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "label": "Revenues",
                "units": {"USD": [
                    {"end": "2024-12-31", "val": 1_000_000_000, "form": "10-K", "filed": "2025-02-01"},
                ]},
            },
        },
        "ifrs-full": {
            "Revenue": {
                "label": "Revenue",
                "units": {"USD": [
                    {"end": "2024-12-31", "val": 1_000_000_000, "form": "20-F", "filed": "2025-03-01"},
                ]},
            },
        },
    },
}

SUBMISSIONS_EMPTY = {
    "cik": _CIK_BARE,
    "filings": {"recent": {"form": [], "accessionNumber": [], "filingDate": [], "primaryDocument": []}},
}


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _seed_company(conn, ticker, cik):
    conn.execute(
        "INSERT INTO companies (ticker, cik, company_name) VALUES (?, ?, ?)",
        [ticker, cik, f"Test Corp {ticker}"],
    )


# ── Action A tests ────────────────────────────────────────────────────────────

@rsps_lib.activate
def test_ifrs_full_concepts_ingested_for_foreign_filer(conn):
    """ifrs-full namespace data is stored in fundamentals_raw."""
    _seed_company(conn, "AU", _CIK_BARE)
    rsps_lib.add(rsps_lib.GET, f"https://data.sec.gov/submissions/CIK{_CIK_PADDED}.json",
                 json=SUBMISSIONS_EMPTY, status=200)
    rsps_lib.add(rsps_lib.GET, f"https://data.sec.gov/api/xbrl/companyfacts/CIK{_CIK_PADDED}.json",
                 json=COMPANYFACTS_IFRS, status=200)

    from ingestion.ingest_sec import SECIngestor
    SECIngestor({}).ingest(conn, "run1", ["AU"])

    rows = conn.execute(
        "SELECT concept FROM fundamentals_raw WHERE ticker='AU'"
    ).fetchall()
    concepts = {r[0] for r in rows}
    assert any("ifrs-full" in c for c in concepts), \
        f"No ifrs-full concepts stored. Got: {concepts}"


@rsps_lib.activate
def test_ifrs_concept_mapped_to_gaap_equivalent(conn):
    """IFRS 'Revenue' concept is stored as 'ifrs-full/Revenues' (GAAP equivalent name)."""
    _seed_company(conn, "AU", _CIK_BARE)
    rsps_lib.add(rsps_lib.GET, f"https://data.sec.gov/submissions/CIK{_CIK_PADDED}.json",
                 json=SUBMISSIONS_EMPTY, status=200)
    rsps_lib.add(rsps_lib.GET, f"https://data.sec.gov/api/xbrl/companyfacts/CIK{_CIK_PADDED}.json",
                 json=COMPANYFACTS_IFRS, status=200)

    from ingestion.ingest_sec import SECIngestor
    SECIngestor({}).ingest(conn, "run1", ["AU"])

    row = conn.execute(
        "SELECT concept FROM fundamentals_raw WHERE ticker='AU' AND concept='ifrs-full/Revenues'"
    ).fetchone()
    assert row is not None, "Revenue should be mapped to Revenues and stored as ifrs-full/Revenues"


@rsps_lib.activate
def test_ifrs_profit_loss_mapped_to_net_income(conn):
    """IFRS 'ProfitLoss' is stored as 'ifrs-full/NetIncomeLoss'."""
    _seed_company(conn, "AU", _CIK_BARE)
    rsps_lib.add(rsps_lib.GET, f"https://data.sec.gov/submissions/CIK{_CIK_PADDED}.json",
                 json=SUBMISSIONS_EMPTY, status=200)
    rsps_lib.add(rsps_lib.GET, f"https://data.sec.gov/api/xbrl/companyfacts/CIK{_CIK_PADDED}.json",
                 json=COMPANYFACTS_IFRS, status=200)

    from ingestion.ingest_sec import SECIngestor
    SECIngestor({}).ingest(conn, "run1", ["AU"])

    row = conn.execute(
        "SELECT value FROM fundamentals_raw WHERE ticker='AU' AND concept='ifrs-full/NetIncomeLoss'"
    ).fetchone()
    assert row is not None, "ProfitLoss should map to ifrs-full/NetIncomeLoss"
    assert row[0] == 800_000_000


@rsps_lib.activate
def test_unknown_ifrs_concept_skipped(conn):
    """IFRS concepts not in the mapping are not stored."""
    _seed_company(conn, "AU", _CIK_BARE)
    rsps_lib.add(rsps_lib.GET, f"https://data.sec.gov/submissions/CIK{_CIK_PADDED}.json",
                 json=SUBMISSIONS_EMPTY, status=200)
    rsps_lib.add(rsps_lib.GET, f"https://data.sec.gov/api/xbrl/companyfacts/CIK{_CIK_PADDED}.json",
                 json=COMPANYFACTS_IFRS, status=200)

    from ingestion.ingest_sec import SECIngestor
    SECIngestor({}).ingest(conn, "run1", ["AU"])

    row = conn.execute(
        "SELECT concept FROM fundamentals_raw WHERE ticker='AU' AND concept LIKE '%SomethingUnmapped%'"
    ).fetchone()
    assert row is None, "Unmapped IFRS concept should not be stored"


@rsps_lib.activate
def test_us_gaap_still_ingested_alongside_ifrs(conn):
    """When both us-gaap and ifrs-full are present, both are ingested."""
    _seed_company(conn, "HYB", "789012")
    rsps_lib.add(rsps_lib.GET, "https://data.sec.gov/submissions/CIK0000789012.json",
                 json=SUBMISSIONS_EMPTY, status=200)
    rsps_lib.add(rsps_lib.GET, "https://data.sec.gov/api/xbrl/companyfacts/CIK0000789012.json",
                 json=COMPANYFACTS_BOTH, status=200)

    from ingestion.ingest_sec import SECIngestor
    SECIngestor({}).ingest(conn, "run1", ["HYB"])

    rows = conn.execute(
        "SELECT DISTINCT concept FROM fundamentals_raw WHERE ticker='HYB'"
    ).fetchall()
    concepts = {r[0] for r in rows}
    assert "us-gaap/Revenues" in concepts
    assert "ifrs-full/Revenues" in concepts


@rsps_lib.activate
def test_ifrs_current_assets_mapped_to_assets_current(conn):
    """IFRS 'CurrentAssets' stored as 'ifrs-full/AssetsCurrent'."""
    _seed_company(conn, "AU", _CIK_BARE)
    rsps_lib.add(rsps_lib.GET, f"https://data.sec.gov/submissions/CIK{_CIK_PADDED}.json",
                 json=SUBMISSIONS_EMPTY, status=200)
    rsps_lib.add(rsps_lib.GET, f"https://data.sec.gov/api/xbrl/companyfacts/CIK{_CIK_PADDED}.json",
                 json=COMPANYFACTS_IFRS, status=200)

    from ingestion.ingest_sec import SECIngestor
    SECIngestor({}).ingest(conn, "run1", ["AU"])

    row = conn.execute(
        "SELECT value FROM fundamentals_raw WHERE ticker='AU' AND concept='ifrs-full/AssetsCurrent'"
    ).fetchone()
    assert row is not None, "CurrentAssets should map to ifrs-full/AssetsCurrent"


@rsps_lib.activate
def test_ifrs_values_stored_correctly(conn):
    """Ingested IFRS values match the source data."""
    _seed_company(conn, "AU", _CIK_BARE)
    rsps_lib.add(rsps_lib.GET, f"https://data.sec.gov/submissions/CIK{_CIK_PADDED}.json",
                 json=SUBMISSIONS_EMPTY, status=200)
    rsps_lib.add(rsps_lib.GET, f"https://data.sec.gov/api/xbrl/companyfacts/CIK{_CIK_PADDED}.json",
                 json=COMPANYFACTS_IFRS, status=200)

    from ingestion.ingest_sec import SECIngestor
    SECIngestor({}).ingest(conn, "run1", ["AU"])

    rows = conn.execute(
        "SELECT value, as_of_date FROM fundamentals_raw WHERE ticker='AU' AND concept='ifrs-full/Revenues' ORDER BY as_of_date DESC"
    ).fetchall()
    assert len(rows) >= 1
    # Most recent Revenue entry: 5B
    assert rows[0][0] == 5_000_000_000


@rsps_lib.activate
def test_no_http_call_when_fundamentals_fresh(conn):
    """Fresh fundamentals skip XBRL fetch regardless of namespace."""
    from datetime import date, datetime, timedelta
    _seed_company(conn, "AU", _CIK_BARE)
    # Seed fresh fundamentals
    conn.execute(
        "INSERT INTO fundamentals_raw (id, ticker, cik, concept, value, unit, as_of_date, form, run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [uuid.uuid4().hex[:16], "AU", _CIK_BARE, "ifrs-full/Revenues", 5e9, "USD",
         "2024-12-31", "20-F", "prev", datetime.utcnow()],
    )
    rsps_lib.add(rsps_lib.GET, f"https://data.sec.gov/submissions/CIK{_CIK_PADDED}.json",
                 json=SUBMISSIONS_EMPTY, status=200)
    # No companyfacts mock — should not be called

    from ingestion.ingest_sec import SECIngestor
    SECIngestor({}).ingest(conn, "run2", ["AU"])

    xbrl_calls = [c for c in rsps_lib.calls if "companyfacts" in c.request.url]
    assert len(xbrl_calls) == 0, "Should skip XBRL fetch when fundamentals are fresh"
