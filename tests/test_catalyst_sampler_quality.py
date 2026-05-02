"""TDD tests for sampler quality and source resolver diagnostics.

RED first — these fail until the implementation is in place.
"""
from __future__ import annotations

import duckdb
import pytest

from missed.catalyst_source_resolver import (
    MIN_SOURCE_TEXT_CHARS,
    _build_sec_url,
    compute_source_coverage,
    resolve_source_text,
)


# ── DB fixture ────────────────────────────────────────────────────────────────

def _make_test_db(tmp_path):
    conn = duckdb.connect(str(tmp_path / "test.duckdb"))
    conn.execute("""
        CREATE TABLE missed_opportunity_investigations (
            investigation_id VARCHAR PRIMARY KEY,
            event_id VARCHAR,
            ticker VARCHAR,
            event_date DATE,
            primary_root_cause VARCHAR,
            root_causes_json VARCHAR,
            text_enrichment_needed BOOLEAN DEFAULT false
        )
    """)
    conn.execute("""
        CREATE TABLE missed_opportunity_events (
            event_id VARCHAR PRIMARY KEY,
            event_type VARCHAR DEFAULT 'gain_20d_20pct',
            return_value DOUBLE DEFAULT 10.0,
            was_scored BOOLEAN DEFAULT false,
            score_before_event DOUBLE DEFAULT 40.0
        )
    """)
    conn.execute("""
        CREATE TABLE filings (
            id VARCHAR PRIMARY KEY,
            ticker VARCHAR,
            cik VARCHAR,
            form_type VARCHAR,
            accession_number VARCHAR,
            filing_date DATE,
            description VARCHAR,
            doc_url VARCHAR
        )
    """)
    return conn


def _insert_event(conn, ticker: str, event_id: str = None, event_date: str = "2026-01-15"):
    inv_id = f"inv_{event_id or ticker}"
    eid = event_id or f"evt_{ticker}"
    conn.execute(
        "INSERT INTO missed_opportunity_investigations VALUES (?, ?, ?, ?, ?, ?, ?)",
        [inv_id, eid, ticker, event_date, "text_evidence_available_not_classified", "[]", True],
    )
    conn.execute(
        "INSERT INTO missed_opportunity_events VALUES (?, ?, ?, ?, ?)",
        [eid, "gain_20d_20pct", 12.5, True, 41.0],
    )
    return eid


def _insert_filing(conn, ticker: str, form_type: str, filing_date: str,
                   cik: str = "1234567", accession: str = "0001234567-26-000001",
                   description: str = "doc.htm"):
    fid = f"{ticker}_{form_type}_{filing_date}"
    conn.execute(
        "INSERT INTO filings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [fid, ticker, cik, form_type, accession, filing_date, description, None],
    )


# ── 1. URL construction ───────────────────────────────────────────────────────

def test_url_construction_correct_format():
    """_build_sec_url produces valid SEC EDGAR URL from cik + accession + filename."""
    url = _build_sec_url("1018724", "0001018724-26-000012", "amzn-20260429.htm")
    assert url == "https://www.sec.gov/Archives/edgar/data/1018724/000101872426000012/amzn-20260429.htm"


def test_url_construction_strips_xslt_subpath():
    """Filing description with XSLT subpath (e.g. xslF345X06/doc.xml) uses only filename."""
    url = _build_sec_url("1234567", "0001234567-26-000001", "xslF345X06/wk-form4_123.xml")
    assert url.endswith("/wk-form4_123.xml")
    assert "xslF345X06" not in url


def test_url_construction_returns_none_for_invalid_accession():
    """Malformed accession_number (wrong dash count) → None."""
    url = _build_sec_url("1234567", "INVALID", "doc.htm")
    assert url is None


# ── 2. Text filing with complete metadata attempts fetch ──────────────────────

def test_text_filing_complete_metadata_attempts_fetch():
    """8-K with cik + accession + filename → URL constructed, fetch attempted."""
    fetch_calls: list[str] = []

    def fake_fetch(url: str) -> str:
        fetch_calls.append(url)
        return "Earnings results Q4 2025: revenue increased by 15%." * 10

    event = {
        "filing_form_type": "8-K",
        "accession_number": "0001018724-26-000012",
        "cik": "1018724",
        "filing_description": "amzn-20260429.htm",
    }
    result = resolve_source_text(event, _fetch_fn=fake_fetch)

    assert len(fetch_calls) == 1
    assert "1018724/000101872426000012/amzn-20260429.htm" in fetch_calls[0]
    assert result["source_text_origin"] == "sec_url"
    assert result["source_text_char_count"] >= MIN_SOURCE_TEXT_CHARS


def test_6k_complete_metadata_attempts_fetch():
    """6-K (foreign issuer quarterly) is also a resolvable text form."""
    fetch_calls: list[str] = []

    def fake_fetch(url: str) -> str:
        fetch_calls.append(url)
        return "Revenue for the period: CHF 2.1 billion, up 8% year on year." * 10

    event = {
        "filing_form_type": "6-K",
        "accession_number": "0001243429-26-000032",
        "cik": "1243429",
        "filing_description": "amform6-k432026.htm",
    }
    result = resolve_source_text(event, _fetch_fn=fake_fetch)

    assert len(fetch_calls) == 1
    assert result["source_text_origin"] == "sec_url"


# ── 3. Diagnostic fields on missing metadata ──────────────────────────────────

def test_missing_cik_gives_no_doc_url_with_diagnostic():
    """Missing cik → no_doc_url + has_cik=False diagnostic."""
    event = {
        "filing_form_type": "8-K",
        "accession_number": "0001234567-26-000001",
        "cik": None,
        "filing_description": "doc.htm",
    }
    result = resolve_source_text(event)
    assert result["source_text_error"] == "no_doc_url"
    assert result["has_cik"] is False
    assert result["has_accession_number"] is True
    assert result["has_primary_doc"] is True


def test_missing_accession_gives_no_doc_url_with_diagnostic():
    """Missing accession_number → no_doc_url + has_accession_number=False."""
    event = {
        "filing_form_type": "8-K",
        "accession_number": None,
        "cik": "1234567",
        "filing_description": "doc.htm",
    }
    result = resolve_source_text(event)
    assert result["source_text_error"] == "no_doc_url"
    assert result["has_cik"] is True
    assert result["has_accession_number"] is False
    assert result["has_primary_doc"] is True


def test_missing_primary_doc_gives_no_doc_url_with_diagnostic():
    """Missing filing_description → no_doc_url + has_primary_doc=False."""
    event = {
        "filing_form_type": "8-K",
        "accession_number": "0001234567-26-000001",
        "cik": "1234567",
        "filing_description": None,
    }
    result = resolve_source_text(event)
    assert result["source_text_error"] == "no_doc_url"
    assert result["has_primary_doc"] is False


def test_constructed_url_in_diagnostic():
    """constructed_url field is populated when all metadata is present."""
    event = {
        "filing_form_type": "8-K",
        "accession_number": "0001018724-26-000012",
        "cik": "1018724",
        "filing_description": "amzn-20260429.htm",
    }

    def fake_fetch(url: str) -> str:
        return "text " * 100

    result = resolve_source_text(event, _fetch_fn=fake_fetch)
    assert result["constructed_url"] is not None
    assert "1018724" in result["constructed_url"]


# ── 4. Sampler text-filing preference ────────────────────────────────────────

def test_sampler_prefers_text_filing_over_form4(tmp_path):
    """When both 8-K and Form 4 exist, sampler picks the 8-K as the filing context."""
    from missed.catalyst_sampler import sample_pilot_events

    conn = _make_test_db(tmp_path)
    _insert_event(conn, "AAPL")
    _insert_filing(conn, "AAPL", "4", "2026-01-12")        # more recent but non-text
    _insert_filing(conn, "AAPL", "8-K", "2026-01-08",
                   cik="320193", accession="0000320193-26-000020", description="aapl-8k.htm")

    events = sample_pilot_events(conn)

    assert len(events) == 1
    assert events[0]["filing_form_type"] == "8-K"
    assert events[0]["cik"] == "320193"
    assert events[0]["accession_number"] == "0000320193-26-000020"


def test_sampler_returns_null_form_when_only_form4_exists(tmp_path):
    """When only Form 4 filings exist, sampler returns event with null filing context."""
    from missed.catalyst_sampler import sample_pilot_events

    conn = _make_test_db(tmp_path)
    _insert_event(conn, "AAPL")
    _insert_filing(conn, "AAPL", "4", "2026-01-12")
    _insert_filing(conn, "AAPL", "4", "2026-01-05")

    events = sample_pilot_events(conn)

    assert len(events) == 1
    # No text filing found → filing_form_type should be None
    assert events[0]["filing_form_type"] is None


def test_sampler_include_non_text_forms_picks_form4(tmp_path):
    """With include_non_text_forms=True, Form 4 is included as filing context."""
    from missed.catalyst_sampler import sample_pilot_events

    conn = _make_test_db(tmp_path)
    _insert_event(conn, "AAPL")
    _insert_filing(conn, "AAPL", "4", "2026-01-12")

    events = sample_pilot_events(conn, include_non_text_forms=True)

    assert len(events) == 1
    assert events[0]["filing_form_type"] == "4"


# ── 5. Source coverage preflight ──────────────────────────────────────────────

def test_compute_source_coverage_counts_correctly():
    """compute_source_coverage tallies each error category."""
    sample = [
        {"source_text_char_count": 500, "source_text_error": None},           # resolvable
        {"source_text_char_count": 0,   "source_text_error": "non_text_filing:4"},
        {"source_text_char_count": 0,   "source_text_error": "non_text_filing:144"},
        {"source_text_char_count": 0,   "source_text_error": "no_doc_url",
         "has_cik": False, "has_accession_number": True, "has_primary_doc": True},
        {"source_text_char_count": 0,   "source_text_error": "no_doc_url",
         "has_cik": True, "has_accession_number": False, "has_primary_doc": True},
        {"source_text_char_count": 0,   "source_text_error": "no_doc_url",
         "has_cik": True, "has_accession_number": True, "has_primary_doc": False},
        {"source_text_char_count": 0,   "source_text_error": "pdf_not_supported"},
        {"source_text_char_count": 50,  "source_text_error": None},           # too short
    ]

    cov = compute_source_coverage(sample)

    assert cov["sampled_count"] == 8
    assert cov["resolvable_source_count"] == 1   # only first has >= 200 chars
    assert cov["skipped_non_text_form_count"] == 2
    assert cov["missing_cik_count"] == 1
    assert cov["missing_accession_count"] == 1
    assert cov["missing_primary_doc_count"] == 1
    assert cov["pdf_not_supported_count"] == 1


def test_preflight_coverage_is_zero_when_all_non_text():
    """All non-text filings → resolvable_source_count == 0."""
    sample = [
        {"source_text_char_count": 0, "source_text_error": "non_text_filing:4"},
        {"source_text_char_count": 0, "source_text_error": "non_text_filing:144"},
        {"source_text_char_count": 0, "source_text_error": "non_text_filing:SC 13G"},
    ]
    cov = compute_source_coverage(sample)
    assert cov["resolvable_source_count"] == 0


# ── 6. No production scoring changes ─────────────────────────────────────────

def test_no_production_scoring_changes_after_sampler_fix(tmp_path):
    """sample_pilot_events never writes to scores table."""
    from missed.catalyst_sampler import sample_pilot_events

    conn = _make_test_db(tmp_path)
    conn.execute(
        "CREATE TABLE scores (run_id VARCHAR, ticker VARCHAR, total_score DOUBLE)"
    )
    conn.execute("INSERT INTO scores VALUES ('r1', 'AAPL', 85.0)")

    _insert_event(conn, "AAPL")
    _insert_filing(conn, "AAPL", "8-K", "2026-01-08", cik="320193",
                   accession="0000320193-26-000020", description="aapl-8k.htm")
    sample_pilot_events(conn)

    rows = conn.execute("SELECT total_score FROM scores").fetchall()
    assert rows == [(85.0,)]
    conn.close()
