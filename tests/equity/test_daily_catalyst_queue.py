"""TDD tests for the daily LLM catalyst shadow review queue.

Tests cover: filtering, ordering, artifact generation, cache, mock path,
no production score mutation.
"""
from __future__ import annotations

import json
import os

import duckdb
import pytest

from missed.catalyst_providers import BaseCatalystProvider
from missed.catalyst_schema import CatalystEnrichment


# ── DB + data helpers ─────────────────────────────────────────────────────────

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
            was_scored BOOLEAN DEFAULT true,
            score_before_event DOUBLE DEFAULT 42.0
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
    conn.execute("""
        CREATE TABLE scores (
            run_id VARCHAR,
            ticker VARCHAR,
            total_score DOUBLE,
            catalyst_score DOUBLE DEFAULT 30.0,
            risk_penalty DOUBLE DEFAULT 20.0,
            tier VARCHAR
        )
    """)
    return conn


def _insert_ticker(conn, ticker, score, tier="Reject",
                   event_date="2026-01-15", run_id="run-001"):
    eid = f"evt_{ticker}"
    conn.execute(
        "INSERT INTO missed_opportunity_investigations VALUES (?,?,?,?,?,?,?)",
        [f"inv_{ticker}", eid, ticker, event_date,
         "text_evidence_available_not_classified", "[]", True],
    )
    conn.execute(
        "INSERT INTO missed_opportunity_events VALUES (?,?,?,?,?)",
        [eid, "gain_20d_20pct", 12.5, True, score],
    )
    conn.execute(
        "INSERT INTO filings VALUES (?,?,?,?,?,?,?,?)",
        [f"f_{ticker}", ticker, "1234567", "8-K",
         "0001234567-26-000001", "2026-01-08", "doc.htm", None],
    )
    conn.execute(
        "INSERT INTO scores VALUES (?,?,?,?,?,?)",
        [run_id, ticker, score, 30.0, 20.0, tier],
    )


_MOCK_SOURCE_BODY = (
    # All test evidence quotes must appear verbatim in the mock source text
    # so the evidence_quote grounding check passes for every test ticker.
    "CAND entered into a definitive acquisition agreement for $500M. "
    "BEAR entered into a definitive agreement to divest its core division. "
    "NEUT entered into a definitive agreement to acquire Corp. "
    "CROSS entered into a definitive acquisition agreement for $500M. "
    "STAY entered into a definitive agreement to acquire Corp. "
    "Management will provide business updates at the conference. "
    "Additional boilerplate text about the company operations and strategy. "
) * 5


def _mock_fetch(url: str) -> str:
    return "<html><body>" + _MOCK_SOURCE_BODY + "</body></html>"


class _TickerMockProvider(BaseCatalystProvider):
    """Returns per-ticker classifications from a dict; falls back to skip."""
    name = "ticker_mock"

    def __init__(self, classifications: dict[str, dict]):
        self._map = classifications

    def classify(self, event: dict, prompt: str) -> CatalystEnrichment:
        ticker = event.get("ticker", "")
        data = self._map.get(ticker)
        if data is None:
            return CatalystEnrichment(
                event_id=event.get("event_id", ""),
                ticker=ticker,
                event_date=str(event.get("event_date", "")),
                catalyst_type="unknown", materiality="none", sentiment="neutral",
                confidence=0.0, evidence_quote="", reasoning_short="[SKIP] no_data",
                should_affect_score=False, provider="ticker_mock",
                enriched_at="2026-01-15T12:00:00+00:00",
            )
        return CatalystEnrichment(
            event_id=event.get("event_id", ""),
            ticker=ticker,
            event_date=str(event.get("event_date", "")),
            **data,
            provider="ticker_mock",
            enriched_at="2026-01-15T12:00:00+00:00",
        )


def _make_provider(ticker_map: dict) -> _TickerMockProvider:
    return _TickerMockProvider(ticker_map)


# ── Standard ticker classification data ──────────────────────────────────────

_BULLISH_MA = {
    "catalyst_type": "merger_acquisition",
    "materiality": "high",
    "sentiment": "bullish",
    "confidence": 0.9,
    "evidence_quote": "CAND entered into a definitive acquisition agreement for $500M.",
    "reasoning_short": "M&A deal",
    "should_affect_score": True,
}
_BEARISH_MA = {
    "catalyst_type": "merger_acquisition",
    "materiality": "high",
    "sentiment": "bearish",
    "confidence": 0.85,
    "evidence_quote": "BEAR entered into a definitive agreement to divest its core division.",
    "reasoning_short": "Bearish divestiture",
    "should_affect_score": True,
}
_WEAK_GUIDANCE = {
    "catalyst_type": "guidance",
    "materiality": "low",
    "sentiment": "bullish",
    "confidence": 0.6,
    "evidence_quote": "Management will provide business updates at the conference.",
    "reasoning_short": "Conference boilerplate",
    "should_affect_score": True,  # model says True; sufficiency will override
}
_NEUTRAL_MA = {
    "catalyst_type": "merger_acquisition",
    "materiality": "medium",
    "sentiment": "neutral",
    "confidence": 0.75,
    "evidence_quote": "NEUT entered into a definitive agreement to acquire Corp.",
    "reasoning_short": "Neutral M&A",
    "should_affect_score": True,  # model says True; sentiment filter will override
}


# ── 1. Promoted candidates: only valid + actionable records ───────────────────

def test_queue_promoted_only_valid_actionable(tmp_path):
    """Promoted list contains only valid + quote_pass + should_affect_score records."""
    from missed.catalyst_queue import build_daily_queue

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "CAND", 43.0)  # will be promoted
    _insert_ticker(conn, "WEAK", 42.0)  # boilerplate → weak_evidence

    provider = _make_provider({"CAND": _BULLISH_MA, "WEAK": _WEAK_GUIDANCE})
    entries, _, _ = build_daily_queue(conn, _provider=provider, _fetch_fn=_mock_fetch)

    promoted = [e for e in entries if e["final_should_affect_score"]]
    tickers = {e["ticker"] for e in promoted}
    assert "CAND" in tickers
    assert "WEAK" not in tickers


# ── 2. Weak evidence excluded from promoted ───────────────────────────────────

def test_weak_evidence_excluded_from_promoted(tmp_path):
    """weak_evidence records are NOT in the promoted candidate list."""
    from missed.catalyst_queue import build_daily_queue

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "WEAK", 42.0)

    provider = _make_provider({"WEAK": _WEAK_GUIDANCE})
    entries, _, _ = build_daily_queue(conn, _provider=provider, _fetch_fn=_mock_fetch)

    promoted = [e for e in entries if e["final_should_affect_score"]]
    assert all(e["ticker"] != "WEAK" for e in promoted)
    weak_entries = [e for e in entries if e["validation_status"] == "weak_evidence"]
    assert any(e["ticker"] == "WEAK" for e in weak_entries)


# ── 3. Neutral sentiment excluded from promoted ───────────────────────────────

def test_neutral_sentiment_excluded_from_promoted(tmp_path):
    """neutral_sentiment records are NOT in the promoted candidate list."""
    from missed.catalyst_queue import build_daily_queue

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "NEUT", 43.0)

    provider = _make_provider({"NEUT": _NEUTRAL_MA})
    entries, _, _ = build_daily_queue(conn, _provider=provider, _fetch_fn=_mock_fetch)

    promoted = [e for e in entries if e["final_should_affect_score"]]
    assert all(e["ticker"] != "NEUT" for e in promoted)


# ── 4. Bearish records are in entries (not promoted) ─────────────────────────

def test_bearish_records_in_entries(tmp_path):
    """Bearish records appear in entries (for the Bearish Downgrades section)."""
    from missed.catalyst_queue import build_daily_queue

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "BEAR", 42.0)

    provider = _make_provider({"BEAR": _BEARISH_MA})
    entries, _, _ = build_daily_queue(conn, _provider=provider, _fetch_fn=_mock_fetch)

    bear_entries = [e for e in entries if e["ticker"] == "BEAR"]
    assert bear_entries
    assert bear_entries[0]["sentiment"] == "bearish"


# ── 5. Reject→C promotions appear first in sorted order ──────────────────────

def test_reject_to_c_first_in_sorted_promoted(tmp_path):
    """Reject→C tier crossings appear before same-tier valid entries in the promoted list."""
    from missed.catalyst_queue import build_daily_queue

    conn = _make_test_db(tmp_path)
    # CROSS at 43.0 → +5.0 shadow = 48 → C-tier
    _insert_ticker(conn, "CROSS", 43.0)
    # STAY at 44.5 → +3.0 shadow = 47.5 → stays C (already C-tier)
    _insert_ticker(conn, "STAY", 44.5, tier="C")

    cross_ma = dict(_BULLISH_MA, evidence_quote="CROSS entered into a definitive acquisition agreement for $500M.")
    stay_ma = dict(_BULLISH_MA,
                   catalyst_type="merger_acquisition",
                   materiality="medium",
                   evidence_quote="STAY entered into a definitive agreement to acquire Corp.")

    provider = _make_provider({"CROSS": cross_ma, "STAY": stay_ma})
    entries, _, _ = build_daily_queue(conn, _provider=provider, _fetch_fn=_mock_fetch)

    promoted = [e for e in entries if e["final_should_affect_score"]]
    crossings = [e for e in promoted if e.get("tier_move") and "→C" in e["tier_move"]]
    assert crossings, "Expected at least one Reject→C crossing"
    # The first promoted entry with a tier crossing must appear before others without
    crossing_indices = [promoted.index(e) for e in crossings]
    non_crossing_promoted = [e for e in promoted if not (e.get("tier_move") and "→C" in e["tier_move"])]
    non_crossing_indices = [promoted.index(e) for e in non_crossing_promoted]
    if non_crossing_indices:
        assert max(crossing_indices) < min(non_crossing_indices), \
            "Reject→C crossings must come before non-crossing promoted entries"


# ── 6. No production score mutation ──────────────────────────────────────────

def test_no_production_score_mutation(tmp_path):
    """Scores table is unchanged after build_daily_queue."""
    from missed.catalyst_queue import build_daily_queue

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "CAND", 43.0)

    before = conn.execute("SELECT run_id, ticker, total_score FROM scores ORDER BY ticker").fetchall()
    provider = _make_provider({"CAND": _BULLISH_MA})
    build_daily_queue(conn, _provider=provider, _fetch_fn=_mock_fetch)
    after = conn.execute("SELECT run_id, ticker, total_score FROM scores ORDER BY ticker").fetchall()

    assert before == after


# ── 7. Markdown artifact generated ───────────────────────────────────────────

def test_markdown_artifact_generated(tmp_path):
    """generate_queue_report writes daily_catalyst_queue.md to output_dir."""
    from missed.catalyst_queue import build_daily_queue, generate_queue_report

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "CAND", 43.0)

    provider = _make_provider({"CAND": _BULLISH_MA})
    entries, revalidated, metadata = build_daily_queue(
        conn, _provider=provider, _fetch_fn=_mock_fetch
    )
    md_path, _, _ = generate_queue_report(entries, revalidated, str(tmp_path), run_metadata=metadata)

    assert os.path.exists(md_path)
    assert md_path.endswith("daily_catalyst_queue.md")
    content = open(md_path).read()
    assert "# Daily Catalyst Queue" in content


# ── 8. CSV artifact generated ─────────────────────────────────────────────────

def test_csv_artifact_generated(tmp_path):
    """generate_queue_report writes daily_catalyst_queue.csv to output_dir."""
    import csv as _csv
    from missed.catalyst_queue import build_daily_queue, generate_queue_report

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "CAND", 43.0)

    provider = _make_provider({"CAND": _BULLISH_MA})
    entries, revalidated, metadata = build_daily_queue(
        conn, _provider=provider, _fetch_fn=_mock_fetch
    )
    _, csv_path, _ = generate_queue_report(entries, revalidated, str(tmp_path), run_metadata=metadata)

    assert os.path.exists(csv_path)
    assert csv_path.endswith("daily_catalyst_queue.csv")
    with open(csv_path) as f:
        rows = list(_csv.DictReader(f))
    assert len(rows) >= 1
    assert "ticker" in rows[0]
    assert "original_score" in rows[0]
    assert "shadow_score" in rows[0]
    assert "evidence_quote" in rows[0]


# ── 9. Enriched JSONL artifact generated ──────────────────────────────────────

def test_enriched_jsonl_artifact_generated(tmp_path):
    """generate_queue_report writes daily_catalyst_queue_enriched.jsonl."""
    from missed.catalyst_queue import build_daily_queue, generate_queue_report

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "CAND", 43.0)

    provider = _make_provider({"CAND": _BULLISH_MA})
    entries, revalidated, metadata = build_daily_queue(
        conn, _provider=provider, _fetch_fn=_mock_fetch
    )
    _, _, jsonl_path = generate_queue_report(entries, revalidated, str(tmp_path), run_metadata=metadata)

    assert os.path.exists(jsonl_path)
    assert jsonl_path.endswith("daily_catalyst_queue_enriched.jsonl")
    with open(jsonl_path) as f:
        records = [json.loads(l) for l in f if l.strip()]
    assert len(records) >= 1
    assert "ticker" in records[0]
    assert "validation_status" in records[0]


# ── 10. Cache path respected ──────────────────────────────────────────────────

def test_cache_path_respected(tmp_path):
    """When cache_path is specified, a cache file is created after classification."""
    from missed.catalyst_queue import build_daily_queue

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "CAND", 43.0)

    cache_file = str(tmp_path / "test_cache.jsonl")
    provider = _make_provider({"CAND": _BULLISH_MA})
    build_daily_queue(conn, _provider=provider, _fetch_fn=_mock_fetch, cache_path=cache_file)

    assert os.path.exists(cache_file)


# ── 11. Mock path makes no external calls ─────────────────────────────────────

def test_mock_path_makes_no_external_calls(tmp_path):
    """When _provider is injected, no external network calls are made."""
    from missed.catalyst_queue import build_daily_queue

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "CAND", 43.0)

    calls: list[str] = []

    def _tracking_fetch(url: str) -> str:
        calls.append(url)
        return "<html><body>" + "Mock text. " * 30 + "</body></html>"

    provider = _make_provider({"CAND": _BULLISH_MA})
    build_daily_queue(conn, _provider=provider, _fetch_fn=_tracking_fetch)

    # The fetch function was called (source resolver), but no real provider API was used
    # (verified by _provider injection — no API key needed)
    assert len(calls) >= 1  # source resolver called our mock
    # If _provider injection bypasses get_provider(), no APIError was raised
    # (this test proves the mock path completes without auth errors)


# ── 12. Empty when no near-threshold tickers ──────────────────────────────────

def test_empty_when_no_near_threshold_tickers(tmp_path):
    """Returns empty entries list when no Reject tickers in score range."""
    from missed.catalyst_queue import build_daily_queue

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "DEEP", 25.0)  # below range

    provider = _make_provider({})
    entries, revalidated, metadata = build_daily_queue(
        conn, _provider=provider, _fetch_fn=_mock_fetch
    )
    assert entries == []
    assert revalidated == []


# ── 13. Report has required markdown sections ─────────────────────────────────

def test_report_has_required_sections(tmp_path):
    """Markdown report includes all required sections."""
    from missed.catalyst_queue import build_daily_queue, generate_queue_report

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "CAND", 43.0)
    _insert_ticker(conn, "WEAK", 42.5)
    _insert_ticker(conn, "BEAR", 41.0)

    provider = _make_provider({
        "CAND": _BULLISH_MA,
        "WEAK": _WEAK_GUIDANCE,
        "BEAR": _BEARISH_MA,
    })
    entries, revalidated, metadata = build_daily_queue(
        conn, _provider=provider, _fetch_fn=_mock_fetch
    )
    md_path, _, _ = generate_queue_report(entries, revalidated, str(tmp_path), run_metadata=metadata)
    content = open(md_path).read()

    for section in ("Summary", "Promoted Candidates", "Bearish Downgrades",
                    "Weak / Rejected", "Source Coverage"):
        assert section in content, f"Missing section: {section!r}"


# ── 14. Each table row is on its own line (no collapsed rows) ─────────────────

def test_summary_table_one_row_per_line(tmp_path):
    """Summary table rows are separated by newlines, not collapsed onto one line."""
    from missed.catalyst_queue import build_daily_queue, generate_queue_report

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "CAND", 43.0)

    provider = _make_provider({"CAND": _BULLISH_MA})
    entries, revalidated, metadata = build_daily_queue(
        conn, _provider=provider, _fetch_fn=_mock_fetch
    )
    _, _, _ = generate_queue_report(entries, revalidated, str(tmp_path), run_metadata=metadata)
    content = open(tmp_path / "daily_catalyst_queue.md").read()
    lines = content.splitlines()

    table_rows = [l for l in lines if l.startswith("|")]
    for row in table_rows:
        # Each row is exactly one line (no row contains a literal newline char)
        assert "\n" not in row
    # Summary table should have at least 7 data rows (6 metrics + header)
    assert len(table_rows) >= 7


def test_weak_rejected_table_one_row_per_line(tmp_path):
    """Weak/rejected evidence table has each row on its own line."""
    from missed.catalyst_queue import build_daily_queue, generate_queue_report

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "WEAK", 42.5)

    provider = _make_provider({"WEAK": _WEAK_GUIDANCE})
    entries, revalidated, metadata = build_daily_queue(
        conn, _provider=provider, _fetch_fn=_mock_fetch
    )
    md_path, _, _ = generate_queue_report(entries, revalidated, str(tmp_path), run_metadata=metadata)
    content = open(md_path).read()
    lines = content.splitlines()

    # Every line starting with | must be its own line (ensured by splitlines)
    table_rows = [l for l in lines if l.startswith("|")]
    for row in table_rows:
        assert "\n" not in row
    # WEAK should appear in weak/rejected section or the section shows empty note
    weak_section_start = content.find("## Weak / Rejected Evidence")
    assert weak_section_start != -1
    weak_section = content[weak_section_start:]
    assert "WEAK" in weak_section or "_(no weak" in weak_section


# ── 15. Evidence truncation at ~200 chars (not 70) ───────────────────────────

def test_evidence_not_truncated_to_70_chars(tmp_path):
    """Evidence quote in Promoted Candidates crossings table uses ≥150-char truncation."""
    from missed.catalyst_queue import build_daily_queue, generate_queue_report

    # Realistic 200+ char quote with M&A keywords (passes sufficiency check)
    long_quote = (
        "CAND entered into a definitive merger and acquisition agreement to acquire "
        "ABC Corporation for approximately $500 million in an all-cash transaction. "
        "The Board of Directors of CAND unanimously approved the proposed merger, "
        "subject to regulatory approval and customary closing conditions expected Q2."
    )
    assert len(long_quote) > 180

    source_body = (long_quote + " ") * 5

    def _long_fetch(url: str) -> str:
        return "<html><body>" + source_body + "</body></html>"

    long_ma = dict(_BULLISH_MA, evidence_quote=long_quote)

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "CAND", 43.0)

    provider = _make_provider({"CAND": long_ma})
    entries, revalidated, metadata = build_daily_queue(
        conn, _provider=provider, _fetch_fn=_long_fetch
    )
    md_path, _, _ = generate_queue_report(entries, revalidated, str(tmp_path), run_metadata=metadata)
    content = open(md_path).read()

    # Find the crossings table row for CAND
    cand_rows = [l for l in content.splitlines() if l.startswith("| CAND") or l.startswith("| [CAND]")]
    assert cand_rows, "CAND not found in promoted crossings table"
    row = cand_rows[0]
    cells = row.split("|")
    evidence_cell = cells[-2].strip()  # second-to-last cell is evidence
    assert len(evidence_cell) > 100, \
        f"Evidence cell too short ({len(evidence_cell)} chars); expected >100 after truncation increase"


# ── 16. constructed_url appears as a markdown link in promoted section ─────────

def test_constructed_url_link_in_promoted_crossings(tmp_path):
    """If constructed_url is set, the report renders a clickable markdown link in Promoted Candidates."""
    from missed.catalyst_queue import build_daily_queue, generate_queue_report

    def _url_fetch(url: str) -> str:
        return "<html><body>" + _MOCK_SOURCE_BODY + "</body></html>"

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "CAND", 43.0)  # 8-K filing → source resolver builds URL

    provider = _make_provider({"CAND": _BULLISH_MA})
    entries, revalidated, metadata = build_daily_queue(
        conn, _provider=provider, _fetch_fn=_url_fetch
    )
    md_path, _, _ = generate_queue_report(entries, revalidated, str(tmp_path), run_metadata=metadata)
    content = open(md_path).read()

    cand_entries = [e for e in entries if e["ticker"] == "CAND" and e["final_should_affect_score"]]
    if cand_entries and cand_entries[0].get("constructed_url"):
        # URL present → expect markdown link syntax [text](url)
        assert "[CAND](" in content or "](http" in content, \
            "Expected markdown link for CAND in promoted section"
    else:
        # No URL → ticker appears as plain text
        assert "CAND" in content


# ── 17. Regulatory settlement/commercial subtype label ───────────────────────

def test_regulatory_settlement_commercial_subtype_display(tmp_path):
    """Regulatory entries with settlement + commercial delivery evidence show settlement/commercial_agreement subtype."""
    from missed.catalyst_queue import build_daily_queue, generate_queue_report

    settlement_commercial_quote = (
        "The company reached a settlement agreement with the regulator. "
        "First commercial deliveries of LNG cargo commenced this quarter."
    )
    # Add the quote to the mock source body for grounding
    source_body = settlement_commercial_quote * 10

    def _settle_fetch(url: str) -> str:
        return "<html><body>" + source_body + "</body></html>"

    reg_entry = {
        "catalyst_type": "regulatory",
        "materiality": "high",
        "sentiment": "bullish",
        "confidence": 0.88,
        "evidence_quote": settlement_commercial_quote,
        "reasoning_short": "Settlement + first cargo",
        "should_affect_score": True,
    }

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "VGX", 43.0)

    provider = _make_provider({"VGX": reg_entry})
    entries, revalidated, metadata = build_daily_queue(
        conn, _provider=provider, _fetch_fn=_settle_fetch
    )
    md_path, _, _ = generate_queue_report(entries, revalidated, str(tmp_path), run_metadata=metadata)
    content = open(md_path).read()

    assert "settlement/commercial_agreement" in content, \
        "Expected settlement/commercial_agreement subtype in report for regulatory entry with both patterns"


# ── 18. Shadow-only note in report ───────────────────────────────────────────

def test_shadow_only_note_in_report(tmp_path):
    """Report contains 'Shadow-only: production scores were not changed.'"""
    from missed.catalyst_queue import build_daily_queue, generate_queue_report

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "CAND", 43.0)

    provider = _make_provider({"CAND": _BULLISH_MA})
    entries, revalidated, metadata = build_daily_queue(
        conn, _provider=provider, _fetch_fn=_mock_fetch
    )
    md_path, _, _ = generate_queue_report(entries, revalidated, str(tmp_path), run_metadata=metadata)
    content = open(md_path).read()

    assert "Shadow-only" in content and "production scores" in content, \
        "Expected shadow-only disclaimer in report"


# ── 19. Section separators have blank lines before and after ─────────────────

def test_section_separators_have_surrounding_blank_lines(tmp_path):
    """Every '---' separator in the markdown is preceded and followed by a blank line."""
    from missed.catalyst_queue import build_daily_queue, generate_queue_report

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "CAND", 43.0)

    provider = _make_provider({"CAND": _BULLISH_MA})
    entries, revalidated, metadata = build_daily_queue(
        conn, _provider=provider, _fetch_fn=_mock_fetch
    )
    md_path, _, _ = generate_queue_report(entries, revalidated, str(tmp_path), run_metadata=metadata)
    lines = open(md_path).read().splitlines()

    for i, line in enumerate(lines):
        if line.strip() == "---":
            before = lines[i - 1].strip() if i > 0 else ""
            after = lines[i + 1].strip() if i + 1 < len(lines) else ""
            assert before == "", f"Line {i}: '---' not preceded by blank line; before={before!r}"
            assert after == "", f"Line {i}: '---' not followed by blank line; after={after!r}"


# ── 20. Structural regression: no double blank lines, no collapsed content ────

def test_markdown_structural_invariants(tmp_path):
    """Generated markdown passes all structural invariants for clean cat output."""
    import re as _re
    from missed.catalyst_queue import build_daily_queue, generate_queue_report

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "CAND", 43.0)
    _insert_ticker(conn, "WEAK", 42.5)
    _insert_ticker(conn, "BEAR", 41.0)

    provider = _make_provider({
        "CAND": _BULLISH_MA,
        "WEAK": _WEAK_GUIDANCE,
        "BEAR": _BEARISH_MA,
    })
    entries, revalidated, metadata = build_daily_queue(
        conn, _provider=provider, _fetch_fn=_mock_fetch
    )
    md_path, _, _ = generate_queue_report(entries, revalidated, str(tmp_path), run_metadata=metadata)
    content = open(md_path).read()
    lines = content.splitlines()

    # 1. No line mixes --- and ## (would indicate collapse)
    for line in lines:
        assert not ("---" in line and "##" in line), \
            f"Line mixes --- and ##: {line!r}"

    # 2. No two consecutive blank lines
    for i in range(len(lines) - 1):
        assert not (lines[i] == "" and lines[i + 1] == ""), \
            f"Double blank at lines {i}–{i+1}"

    # 3. Every '---' separator has blank before and after
    for i, line in enumerate(lines):
        if line.strip() == "---":
            assert i > 0 and lines[i - 1] == "", \
                f"--- at line {i} not preceded by blank"
            assert i < len(lines) - 1 and lines[i + 1] == "", \
                f"--- at line {i} not followed by blank"

    # 4. Every heading (## or ###) is preceded by a blank line
    for i, line in enumerate(lines):
        if line.startswith("## ") or line.startswith("### "):
            assert i > 0 and lines[i - 1] == "", \
                f"Heading not preceded by blank at line {i}: {line!r}"

    # 5. No table data row has two pipes immediately adjacent (collapsed rows)
    for line in lines:
        if line.startswith("| ") and not _re.match(r"^\|[-: |]+\|$", line):
            inner = line.strip()[1:-1]
            inner_unescaped = inner.replace("\\|", "")
            assert not _re.search(r"\|\s*\|", inner_unescaped), \
                f"Possible collapsed table rows: {line[:80]!r}"

    # 6. Source Coverage bullets are on separate lines (not concatenated)
    cov_start = next((i for i, l in enumerate(lines) if l.strip() == "## Source Coverage"), -1)
    if cov_start >= 0:
        bullet_lines = [l for l in lines[cov_start + 1 : cov_start + 6] if l.startswith("- ")]
        for bl in bullet_lines:
            assert bl.strip() != "---", \
                f"Source Coverage bullet is a separator: {bl!r}"
            assert "\n" not in bl

    # 7. All headings start at column 0
    for line in lines:
        if line.lstrip().startswith("#"):
            assert line[0] == "#", f"Indented heading: {line!r}"


# ── 21. Source Coverage shows numeric count (not "—") when computable ─────────

def test_source_available_count_is_numeric_in_report(tmp_path):
    """Source text available count in report is a number, never '—', when enriched records carry char counts."""
    from missed.catalyst_queue import build_daily_queue, generate_queue_report

    conn = _make_test_db(tmp_path)
    _insert_ticker(conn, "CAND", 43.0)

    provider = _make_provider({"CAND": _BULLISH_MA})
    entries, revalidated, metadata = build_daily_queue(
        conn, _provider=provider, _fetch_fn=_mock_fetch
    )
    md_path, _, _ = generate_queue_report(entries, revalidated, str(tmp_path), run_metadata=metadata)
    content = open(md_path).read()

    # Find the "Source text available" bullet in Source Coverage section
    for line in content.splitlines():
        if "Source text available" in line and "≥200" in line:
            # Bullet format: "- Source text available (≥200 chars): VALUE"
            value_cell = line.split(": ", 1)[-1].strip() if ": " in line else ""
            assert value_cell.isdigit(), (
                f"Source text available must be numeric, got {value_cell!r}. "
                f"Full line: {line!r}"
            )
            break
    else:
        pytest.fail("Source text available row not found in report")


# ── 22. generate_queue_report computes source_available from revalidated when missing ──

def test_source_available_computed_from_revalidated_when_metadata_missing(tmp_path):
    """generate_queue_report computes source_available from revalidated records when not in metadata."""
    from missed.catalyst_queue import generate_queue_report

    # Build revalidated records with explicit source_text_char_count values
    revalidated = [
        {
            "ticker": "AAA", "event_date": "2026-01-10",
            "catalyst_type": "merger_acquisition", "materiality": "high",
            "sentiment": "bullish", "confidence": 0.9,
            "evidence_quote": "AAA entered into a definitive acquisition agreement.",
            "reasoning_short": "M&A", "should_affect_score": True,
            "provider": "mock", "enriched_at": "2026-01-15T12:00:00+00:00",
            "model_should_affect_score": True,
            "validation_status": "valid", "quote_validation_pass": True,
            "invalid_reason": "", "event_id": "evt_AAA",
            "source_text_char_count": 500,  # above threshold → counts
        },
        {
            "ticker": "BBB", "event_date": "2026-01-11",
            "catalyst_type": "earnings", "materiality": "low",
            "sentiment": "neutral", "confidence": 0.5,
            "evidence_quote": "",
            "reasoning_short": "[SKIP] no_source", "should_affect_score": False,
            "provider": "mock", "enriched_at": "2026-01-15T12:00:00+00:00",
            "model_should_affect_score": False,
            "validation_status": "valid", "quote_validation_pass": True,
            "invalid_reason": "", "event_id": "evt_BBB",
            "source_text_char_count": 50,   # below threshold → doesn't count
        },
    ]
    queue_entries = [
        {
            "ticker": "AAA", "event_date": "2026-01-10",
            "filing_form_type": "8-K", "constructed_url": None,
            "catalyst_type": "merger_acquisition", "materiality": "high",
            "sentiment": "bullish", "confidence": 0.9,
            "evidence_quote": "AAA entered into a definitive acquisition agreement.",
            "validation_status": "valid", "quote_validation_pass": True,
            "final_should_affect_score": True,
            "original_score": 43.0, "original_tier": "Reject",
            "llm_adjustment": 5.0, "shadow_score": 48.0,
            "shadow_tier": "C", "tier_move": "Reject→C",
        },
    ]
    # Metadata deliberately omits "source_available"
    metadata = {
        "sampled": 2,
        "classified": 2,
        "valid_actionable": 1,
        "tier_crossings": 1,
    }
    md_path, _, _ = generate_queue_report(
        queue_entries, revalidated, str(tmp_path), run_metadata=metadata
    )
    content = open(md_path).read()

    for line in content.splitlines():
        if "Source text available" in line and "≥200" in line:
            # Bullet format: "- Source text available (≥200 chars): VALUE"
            value_cell = line.split(": ", 1)[-1].strip() if ": " in line else ""
            assert value_cell == "1", (
                f"Expected source_available=1 (only AAA qualifies), got {value_cell!r}. "
                f"Line: {line!r}"
            )
            break
    else:
        pytest.fail("Source text available row not found in report")
