"""Per-candidate data quality guard hits and catalyst evidence — Phase 4 TDD suite."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta

import pytest

from storage.db import get_connection, init_schema
from review.packet_builder import build_packet, write_packet


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _company(conn, ticker, name="Test Corp"):
    conn.execute(
        "INSERT OR IGNORE INTO companies (ticker, company_name, is_active) VALUES (?, ?, true)",
        [ticker, name],
    )


def _score(conn, ticker, run_id, tier="C", total=50.0, cheap=50.0, quality=50.0,
           catalyst=50.0, momentum=50.0, sentiment=50.0, risk=25.0):
    conn.execute(
        """INSERT INTO scores
           (id, run_id, ticker, as_of_date, cheap_score, quality_score,
            catalyst_score, momentum_score, sentiment_score, risk_penalty,
            total_score, tier, confidence, why_ranked, missing_data_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [uuid.uuid4().hex[:16], run_id, ticker, date.today(),
         cheap, quality, catalyst, momentum, sentiment, risk, total,
         tier, "low", "test", "[]", datetime.utcnow()],
    )


def _feature(conn, ticker, run_id, group="quality", name="net_margin",
             score=55.0, confidence="low", metadata=None):
    meta_json = json.dumps(metadata) if metadata else None
    conn.execute(
        """INSERT INTO features
           (id, run_id, ticker, as_of_date, feature_group, feature_name,
            feature_value, feature_score, source, confidence, metadata_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [uuid.uuid4().hex[:16], run_id, ticker, date.today(),
         group, name, 10.0, score, "sec_edgar", confidence, meta_json, datetime.utcnow()],
    )


def _filing(conn, ticker, form_type="8-K", filing_date=None, description="form8-k.htm"):
    conn.execute(
        """INSERT INTO filings
           (id, ticker, form_type, filing_date, description, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [uuid.uuid4().hex[:16], ticker, form_type,
         (filing_date or date.today() - timedelta(days=5)).isoformat(),
         description, datetime.utcnow()],
    )


def _event(conn, ticker, event_type="earnings", event_date=None, title=None, is_upcoming=True):
    conn.execute(
        """INSERT INTO events
           (id, ticker, event_type, event_date, title, source, is_upcoming, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [uuid.uuid4().hex[:16], ticker, event_type,
         (event_date or date.today() + timedelta(days=7)).isoformat(),
         title or f"Earnings: {ticker}", "nasdaq_earnings", is_upcoming, datetime.utcnow()],
    )


def _run(conn, ticker="AAPL", run_id=None, tier="C", total=50.0):
    run_id = run_id or uuid.uuid4().hex[:16]
    _company(conn, ticker)
    _score(conn, ticker, run_id, tier=tier, total=total)
    return run_id


# ── guard_hits field existence ────────────────────────────────────────────────

def test_candidate_has_guard_hits_field(conn):
    """Every candidate in every section has a 'guard_hits' key."""
    run_id = _run(conn, "AAPL")

    packet = build_packet(conn, run_id)

    for section_name, candidates in packet.sections.items():
        for c in candidates:
            assert "guard_hits" in c, (
                f"Candidate {c['ticker']} in section '{section_name}' missing 'guard_hits'"
            )


def test_clean_candidate_has_empty_guard_hits(conn):
    """Candidate with no low-confidence or missing_reason features → guard_hits=[]."""
    run_id = _run(conn, "MSFT")
    # Seed a clean high-confidence feature (no missing_reason, no stale metadata)
    _feature(conn, "MSFT", run_id, confidence="high", metadata=None)

    packet = build_packet(conn, run_id)
    candidates = packet.sections.get("c_tier", [])
    msft = next((c for c in candidates if c["ticker"] == "MSFT"), None)
    assert msft is not None
    assert msft["guard_hits"] == [], f"Clean candidate should have empty guard_hits, got {msft['guard_hits']}"


# ── guard_hits content ────────────────────────────────────────────────────────

def test_stale_fundamentals_shows_in_guard_hits(conn):
    """Feature with stale_fundamentals_days in metadata → appears in guard_hits."""
    run_id = _run(conn, "STALE")
    _feature(conn, "STALE", run_id, name="net_income_positive",
             confidence="low", metadata={"stale_fundamentals_days": 200})

    packet = build_packet(conn, run_id)
    candidates = packet.sections.get("c_tier", [])
    c = next((x for x in candidates if x["ticker"] == "STALE"), None)
    assert c is not None
    assert any("stale_fundamentals_days" in h for h in c["guard_hits"]), (
        f"stale_fundamentals_days not found in guard_hits: {c['guard_hits']}"
    )


def test_missing_reason_shows_in_guard_hits(conn):
    """Feature with missing_reason in metadata → appears in guard_hits."""
    run_id = _run(conn, "MR")
    _feature(conn, "MR", run_id, name="ps_proxy", score=None,
             confidence="low", metadata={"missing_reason": "foreign_currency_not_normalized"})

    packet = build_packet(conn, run_id)
    candidates = packet.sections.get("c_tier", [])
    c = next((x for x in candidates if x["ticker"] == "MR"), None)
    assert c is not None
    assert any("foreign_currency_not_normalized" in h for h in c["guard_hits"]), (
        f"missing_reason not found in guard_hits: {c['guard_hits']}"
    )


# ── catalyst_evidence field existence ────────────────────────────────────────

def test_candidate_has_catalyst_evidence_field(conn):
    """Every candidate in every section has a 'catalyst_evidence' key."""
    run_id = _run(conn, "NVDA")

    packet = build_packet(conn, run_id)

    for section_name, candidates in packet.sections.items():
        for c in candidates:
            assert "catalyst_evidence" in c, (
                f"Candidate {c['ticker']} in section '{section_name}' missing 'catalyst_evidence'"
            )


def test_catalyst_evidence_includes_recent_filings(conn):
    """Recent filing within 60 days appears in catalyst_evidence."""
    run_id = _run(conn, "CFIL")
    _filing(conn, "CFIL", form_type="8-K",
            filing_date=date.today() - timedelta(days=10),
            description="8k_earnings_beat.htm")

    packet = build_packet(conn, run_id)
    candidates = packet.sections.get("c_tier", [])
    c = next((x for x in candidates if x["ticker"] == "CFIL"), None)
    assert c is not None
    evids = c.get("catalyst_evidence", [])
    filings = [e for e in evids if e.get("type") == "filing"]
    assert len(filings) >= 1, f"Expected recent filing in catalyst_evidence, got {evids}"


def test_catalyst_evidence_includes_upcoming_events(conn):
    """Upcoming earnings event within 14 days appears in catalyst_evidence."""
    run_id = _run(conn, "CEVT")
    _event(conn, "CEVT", event_type="earnings",
           event_date=date.today() + timedelta(days=7),
           title="Earnings: CEVT", is_upcoming=True)

    packet = build_packet(conn, run_id)
    candidates = packet.sections.get("c_tier", [])
    c = next((x for x in candidates if x["ticker"] == "CEVT"), None)
    assert c is not None
    evids = c.get("catalyst_evidence", [])
    events = [e for e in evids if e.get("type") == "event"]
    assert len(events) >= 1, f"Expected upcoming event in catalyst_evidence, got {evids}"


# ── Markdown rendering ────────────────────────────────────────────────────────

def test_markdown_renders_guard_hits_section(tmp_path, conn):
    """Candidate with guard hits → markdown contains 'Data quality guards triggered'."""
    run_id = _run(conn, "GUARD")
    _feature(conn, "GUARD", run_id, name="net_income_positive",
             confidence="low", metadata={"stale_fundamentals_days": 220})

    packet = build_packet(conn, run_id)
    md_path, _ = write_packet(packet, output_dir=str(tmp_path))
    md = md_path.read_text()

    assert "Data quality guards triggered" in md, (
        "Markdown should contain 'Data quality guards triggered' for candidate with guard hits"
    )


def test_markdown_omits_guard_section_for_clean_candidate(tmp_path, conn):
    """Candidate with no guard hits → markdown does NOT contain 'Data quality guards'."""
    run_id = _run(conn, "CLEAN")
    _feature(conn, "CLEAN", run_id, confidence="high", metadata=None)

    packet = build_packet(conn, run_id)
    md_path, _ = write_packet(packet, output_dir=str(tmp_path))
    md = md_path.read_text()

    assert "Data quality guards triggered" not in md, (
        "Markdown should NOT contain 'Data quality guards' for clean candidate"
    )


def test_markdown_renders_catalyst_evidence(tmp_path, conn):
    """Candidate with recent filing → markdown contains 'Catalyst evidence'."""
    run_id = _run(conn, "CATEVT")
    _filing(conn, "CATEVT", form_type="8-K",
            filing_date=date.today() - timedelta(days=5),
            description="8k_merger.htm")

    packet = build_packet(conn, run_id)
    md_path, _ = write_packet(packet, output_dir=str(tmp_path))
    md = md_path.read_text()

    assert "Catalyst evidence" in md, (
        "Markdown should contain 'Catalyst evidence' for candidate with recent filing"
    )
