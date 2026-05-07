"""TDD tests for the near-threshold targeted sampler.

Covers: Reject 40.0–44.9 filtering, non-text form exclusion, deterministic ordering,
n limit, current_score/tier fields in output, no production score mutation.
"""
from __future__ import annotations

import duckdb
import pytest

from missed.catalyst_sampler import compute_event_priority, sample_near_threshold_events


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


def _insert_investigation(conn, ticker, event_id=None, event_date="2026-01-15",
                           root_cause="text_evidence_available_not_classified"):
    inv_id = f"inv_{event_id or ticker}"
    eid = event_id or f"evt_{ticker}"
    conn.execute(
        "INSERT INTO missed_opportunity_investigations VALUES (?, ?, ?, ?, ?, ?, ?)",
        [inv_id, eid, ticker, event_date, root_cause, "[]", True],
    )
    conn.execute(
        "INSERT INTO missed_opportunity_events VALUES (?, ?, ?, ?, ?)",
        [eid, "gain_20d_20pct", 12.5, True, 41.0],
    )
    return eid


def _insert_filing(conn, ticker, form_type, filing_date,
                   cik="1234567", accession="0001234567-26-000001", description="doc.htm"):
    fid = f"f_{ticker}_{form_type}_{filing_date}"
    conn.execute(
        "INSERT INTO filings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [fid, ticker, cik, form_type, accession, filing_date, description, None],
    )


def _insert_score(conn, ticker, total_score, tier="Reject", run_id="run-001",
                  catalyst_score=30.0, risk_penalty=20.0):
    conn.execute(
        "INSERT INTO scores VALUES (?, ?, ?, ?, ?, ?)",
        [run_id, ticker, total_score, catalyst_score, risk_penalty, tier],
    )


# ── 1. Basic near-threshold selection ────────────────────────────────────────

def test_selects_only_reject_40_to_44_9(tmp_path):
    """Only Reject tickers with score in [40.0, 44.9] are returned."""
    conn = _make_test_db(tmp_path)
    for ticker, score, tier in [
        ("DEEP",  30.0, "Reject"),   # too low
        ("NEAR",  42.0, "Reject"),   # near-threshold ← included
        ("CTIER", 50.0, "C"),        # already C-tier
    ]:
        _insert_investigation(conn, ticker)
        _insert_filing(conn, ticker, "8-K", "2026-01-08")
        _insert_score(conn, ticker, score, tier)

    events = sample_near_threshold_events(conn)
    tickers = {e["ticker"] for e in events}
    assert "NEAR" in tickers
    assert "DEEP" not in tickers
    assert "CTIER" not in tickers


# ── 2. Non-Reject tier excluded even when score in range ─────────────────────

def test_excludes_non_reject_tier_in_score_range(tmp_path):
    conn = _make_test_db(tmp_path)
    _insert_investigation(conn, "AAPL")
    _insert_filing(conn, "AAPL", "8-K", "2026-01-08")
    _insert_score(conn, "AAPL", 42.0, tier="C")

    events = sample_near_threshold_events(conn)
    assert events == []


# ── 3. Lower boundary: 40.0 included ─────────────────────────────────────────

def test_score_40_0_is_included(tmp_path):
    conn = _make_test_db(tmp_path)
    _insert_investigation(conn, "AAPL")
    _insert_filing(conn, "AAPL", "8-K", "2026-01-08")
    _insert_score(conn, "AAPL", 40.0, tier="Reject")

    events = sample_near_threshold_events(conn)
    assert len(events) == 1


# ── 4. Upper boundary: 44.9 included ─────────────────────────────────────────

def test_score_44_9_is_included(tmp_path):
    conn = _make_test_db(tmp_path)
    _insert_investigation(conn, "AAPL")
    _insert_filing(conn, "AAPL", "8-K", "2026-01-08")
    _insert_score(conn, "AAPL", 44.9, tier="Reject")

    events = sample_near_threshold_events(conn)
    assert len(events) == 1


# ── 5. Score just above range excluded (45.0) ────────────────────────────────

def test_score_45_0_is_excluded(tmp_path):
    conn = _make_test_db(tmp_path)
    _insert_investigation(conn, "AAPL")
    _insert_filing(conn, "AAPL", "8-K", "2026-01-08")
    _insert_score(conn, "AAPL", 45.0, tier="C")

    events = sample_near_threshold_events(conn)
    assert events == []


# ── 6. Score just below range excluded (39.9) ────────────────────────────────

def test_score_39_9_is_excluded(tmp_path):
    conn = _make_test_db(tmp_path)
    _insert_investigation(conn, "AAPL")
    _insert_filing(conn, "AAPL", "8-K", "2026-01-08")
    _insert_score(conn, "AAPL", 39.9, tier="Reject")

    events = sample_near_threshold_events(conn)
    assert events == []


# ── 7. Deterministic ordering: score DESC, then event_date DESC ───────────────

def test_ordering_score_desc_then_event_date_desc(tmp_path):
    conn = _make_test_db(tmp_path)
    for ticker, score, event_date in [
        ("LOW",  41.0, "2026-01-10"),
        ("MID",  43.0, "2026-01-12"),
        ("HIGH", 44.0, "2026-01-08"),
    ]:
        _insert_investigation(conn, ticker, event_date=event_date)
        _insert_filing(conn, ticker, "8-K", "2026-01-05")
        _insert_score(conn, ticker, score, tier="Reject")

    events = sample_near_threshold_events(conn)
    assert len(events) == 3
    assert [e["current_score"] for e in events] == [44.0, 43.0, 41.0]


# ── 8. Deterministic: event_date DESC tiebreaker ─────────────────────────────

def test_ordering_event_date_tiebreaker(tmp_path):
    conn = _make_test_db(tmp_path)
    # Same score, different event dates
    for ticker, event_date in [("OLD", "2026-01-05"), ("NEW", "2026-01-20")]:
        _insert_investigation(conn, ticker, event_date=event_date)
        _insert_filing(conn, ticker, "8-K", "2026-01-03")
        _insert_score(conn, ticker, 42.0, tier="Reject")

    events = sample_near_threshold_events(conn)
    assert len(events) == 2
    # Most recent event first
    assert events[0]["ticker"] == "NEW"
    assert events[1]["ticker"] == "OLD"


# ── 9. n limit respected ─────────────────────────────────────────────────────

def test_respects_n_limit(tmp_path):
    conn = _make_test_db(tmp_path)
    for ticker, score in [("AAPL", 44.0), ("NVDA", 43.0), ("MSFT", 42.0)]:
        _insert_investigation(conn, ticker)
        _insert_filing(conn, ticker, "8-K", "2026-01-08")
        _insert_score(conn, ticker, score, tier="Reject")

    events = sample_near_threshold_events(conn, n=2)
    assert len(events) == 2
    # Top 2 by score
    assert {e["ticker"] for e in events} == {"AAPL", "NVDA"}


# ── 10. current_score and current_tier fields present in output ───────────────

def test_returns_current_score_and_tier_fields(tmp_path):
    conn = _make_test_db(tmp_path)
    _insert_investigation(conn, "AAPL")
    _insert_filing(conn, "AAPL", "8-K", "2026-01-08")
    _insert_score(conn, "AAPL", 42.5, tier="Reject")

    events = sample_near_threshold_events(conn)
    assert len(events) == 1
    assert events[0]["current_score"] == 42.5
    assert events[0]["current_tier"] == "Reject"


# ── 11. No production score mutation ─────────────────────────────────────────

def test_no_production_score_mutation(tmp_path):
    conn = _make_test_db(tmp_path)
    _insert_investigation(conn, "AAPL")
    _insert_filing(conn, "AAPL", "8-K", "2026-01-08")
    _insert_score(conn, "AAPL", 42.0, tier="Reject")

    before = conn.execute("SELECT total_score FROM scores").fetchall()
    sample_near_threshold_events(conn)
    after = conn.execute("SELECT total_score FROM scores").fetchall()
    assert before == after


# ── 12. Non-text forms excluded by default ───────────────────────────────────

def test_non_text_forms_excluded_by_default(tmp_path):
    """With only a Form 4 filing, filing context is None (source resolver will skip)."""
    conn = _make_test_db(tmp_path)
    _insert_investigation(conn, "AAPL")
    _insert_filing(conn, "AAPL", "4", "2026-01-12")  # non-text form
    _insert_score(conn, "AAPL", 42.0, tier="Reject")

    events = sample_near_threshold_events(conn)
    assert len(events) == 1
    assert events[0]["filing_form_type"] is None  # text form filter found nothing


# ── 13. Include non-text forms flag exposes Form 4 context ───────────────────

def test_include_non_text_forms_shows_form4(tmp_path):
    conn = _make_test_db(tmp_path)
    _insert_investigation(conn, "AAPL")
    _insert_filing(conn, "AAPL", "4", "2026-01-12")
    _insert_score(conn, "AAPL", 42.0, tier="Reject")

    events = sample_near_threshold_events(conn, include_non_text_forms=True)
    assert len(events) == 1
    assert events[0]["filing_form_type"] == "4"


# ── 14. Empty when no near-threshold tickers ─────────────────────────────────

def test_empty_when_no_near_threshold_tickers(tmp_path):
    conn = _make_test_db(tmp_path)
    _insert_investigation(conn, "AAPL")
    _insert_filing(conn, "AAPL", "8-K", "2026-01-08")
    _insert_score(conn, "AAPL", 28.0, tier="Reject")  # below range

    events = sample_near_threshold_events(conn)
    assert events == []


# ── 15. Latest run score used when ticker has multiple runs ───────────────────

def test_latest_run_score_used_for_filtering(tmp_path):
    """When multiple runs exist, the latest run_id's score drives filtering."""
    conn = _make_test_db(tmp_path)
    _insert_investigation(conn, "AAPL")
    _insert_filing(conn, "AAPL", "8-K", "2026-01-08")
    # Old run: in near-threshold range
    _insert_score(conn, "AAPL", 42.0, tier="Reject", run_id="run-001-old")
    # Latest run: score improved, no longer near-threshold
    _insert_score(conn, "AAPL", 55.0, tier="C", run_id="run-999-new")

    events = sample_near_threshold_events(conn)
    # Latest run says C-tier/55 → should be excluded
    assert events == []


# ── 16. Deduplication: max_events_per_ticker=1 returns one per ticker ─────────

def test_repeated_ticker_deduplicated_to_one(tmp_path):
    """With max_events_per_ticker=1, a ticker with 3 events returns only 1."""
    conn = _make_test_db(tmp_path)
    # Three events for the same ticker
    _insert_investigation(conn, "AAPL", event_id="evt_a1", event_date="2026-01-10")
    _insert_investigation(conn, "AAPL", event_id="evt_a2", event_date="2026-01-15")
    _insert_investigation(conn, "AAPL", event_id="evt_a3", event_date="2026-01-20")
    _insert_filing(conn, "AAPL", "8-K", "2026-01-05")
    _insert_score(conn, "AAPL", 42.0, tier="Reject")

    events = sample_near_threshold_events(conn, max_events_per_ticker=1)
    aapl_events = [e for e in events if e["ticker"] == "AAPL"]
    assert len(aapl_events) == 1


# ── 17. max_events_per_ticker=2 allows exactly two per ticker ─────────────────

def test_max_events_per_ticker_2_allows_two(tmp_path):
    """With max_events_per_ticker=2, a ticker with 3 events returns exactly 2."""
    conn = _make_test_db(tmp_path)
    _insert_investigation(conn, "AAPL", event_id="evt_b1", event_date="2026-01-10")
    _insert_investigation(conn, "AAPL", event_id="evt_b2", event_date="2026-01-15")
    _insert_investigation(conn, "AAPL", event_id="evt_b3", event_date="2026-01-20")
    _insert_filing(conn, "AAPL", "8-K", "2026-01-05")
    _insert_score(conn, "AAPL", 42.0, tier="Reject")

    events = sample_near_threshold_events(conn, max_events_per_ticker=2)
    aapl_events = [e for e in events if e["ticker"] == "AAPL"]
    assert len(aapl_events) == 2


# ── 18. Best event kept is the most recent (deterministic ordering) ────────────

def test_best_event_kept_is_most_recent(tmp_path):
    """With max_events_per_ticker=1, the event with the most recent event_date is kept."""
    conn = _make_test_db(tmp_path)
    _insert_investigation(conn, "AAPL", event_id="evt_old", event_date="2026-01-05")
    _insert_investigation(conn, "AAPL", event_id="evt_new", event_date="2026-01-20")
    _insert_filing(conn, "AAPL", "8-K", "2026-01-03")
    _insert_score(conn, "AAPL", 42.0, tier="Reject")

    events = sample_near_threshold_events(conn, max_events_per_ticker=1)
    assert len(events) == 1
    assert str(events[0]["event_date"]) == "2026-01-20"


# ── 19. No score mutation with max_events_per_ticker ─────────────────────────

def test_no_score_mutation_with_max_events_per_ticker(tmp_path):
    """Scores table is unchanged after sampling with max_events_per_ticker=1."""
    conn = _make_test_db(tmp_path)
    _insert_investigation(conn, "AAPL", event_id="evt_c1", event_date="2026-01-10")
    _insert_investigation(conn, "AAPL", event_id="evt_c2", event_date="2026-01-15")
    _insert_filing(conn, "AAPL", "8-K", "2026-01-05")
    _insert_score(conn, "AAPL", 42.0, tier="Reject")

    before = conn.execute("SELECT total_score FROM scores").fetchall()
    sample_near_threshold_events(conn, max_events_per_ticker=1)
    after = conn.execute("SELECT total_score FROM scores").fetchall()
    assert before == after


# ── 20. compute_event_priority: merger 8-K beats generic later event ──────────

def test_priority_merger_8k_beats_generic_later_event():
    """An 8-K with 'acquisition agreement' outranks a later routine 10-Q."""
    merger_event = {
        "event_id": "evt_merger",
        "event_date": "2026-01-10",
        "filing_form_type": "8-K",
        "filing_description": "Form 8-K: definitive acquisition agreement",
        "event_type": "gain_20d_20pct",
        "source_text_char_count": 800,
    }
    routine_event = {
        "event_id": "evt_routine",
        "event_date": "2026-01-20",  # later date
        "filing_form_type": "10-Q",
        "filing_description": "form10-q.htm",
        "event_type": "gain_20d_20pct",
        "source_text_char_count": 0,
    }
    merger_priority, merger_reasons = compute_event_priority(merger_event)
    routine_priority, routine_reasons = compute_event_priority(routine_event)
    assert merger_priority > routine_priority
    assert any("8-K" in r or "form" in r.lower() for r in merger_reasons)


# ── 21. compute_event_priority: source-resolvable beats no-source event ───────

def test_priority_source_resolvable_beats_no_source():
    """An event with source text (≥200 chars) scores higher than one without."""
    with_source = {
        "event_id": "evt_src",
        "event_date": "2026-01-15",
        "filing_form_type": "8-K",
        "filing_description": "form8-k.htm",
        "event_type": "gain_20d_20pct",
        "source_text_char_count": 500,
    }
    no_source = {
        "event_id": "evt_nosrc",
        "event_date": "2026-01-15",
        "filing_form_type": "8-K",
        "filing_description": "form8-k.htm",
        "event_type": "gain_20d_20pct",
        "source_text_char_count": 0,
    }
    p_src, reasons_src = compute_event_priority(with_source)
    p_nosrc, _ = compute_event_priority(no_source)
    assert p_src > p_nosrc
    assert any("source" in r.lower() for r in reasons_src)


# ── 22. compute_event_priority: high-signal description beats generic 10-K ────

def test_priority_high_signal_description_beats_generic_10k():
    """An 8-K description with 'settlement' keyword ranks higher than a bare 10-K."""
    settlement_event = {
        "event_id": "evt_settle",
        "event_date": "2026-01-15",
        "filing_form_type": "8-K",
        "filing_description": "8-K: settlement agreement with regulatory authority",
        "event_type": "gain_20d_20pct",
        "source_text_char_count": 0,
    }
    generic_10k = {
        "event_id": "evt_10k",
        "event_date": "2026-01-16",  # later date
        "filing_form_type": "10-K",
        "filing_description": "annual report form10-k.htm",
        "event_type": "gain_20d_20pct",
        "source_text_char_count": 0,
    }
    p_settle, settle_reasons = compute_event_priority(settlement_event)
    p_10k, _ = compute_event_priority(generic_10k)
    assert p_settle > p_10k
    assert any("signal" in r.lower() or "term" in r.lower() for r in settle_reasons)


# ── 23. compute_event_priority: deterministic tie-break by event_id ───────────

def test_priority_deterministic_on_identical_events():
    """Two structurally identical events produce the same priority score."""
    event_a = {
        "event_id": "evt_aaa",
        "event_date": "2026-01-15",
        "filing_form_type": "8-K",
        "filing_description": "form8-k.htm",
        "event_type": "gain_20d_20pct",
        "source_text_char_count": 0,
    }
    event_b = dict(event_a, event_id="evt_bbb")
    p_a, _ = compute_event_priority(event_a)
    p_b, _ = compute_event_priority(event_b)
    assert p_a == p_b  # same structure → same priority score


# ── 24. per-ticker dedup: priority beats recency when filing is higher quality ─

def test_sample_selects_higher_priority_event_over_later_event(tmp_path):
    """With max_events_per_ticker=1, the 8-K merger event wins over a later 10-Q."""
    conn = _make_test_db(tmp_path)
    # Two events for AAPL: merger 8-K (earlier) vs routine 10-Q (later)
    _insert_investigation(conn, "AAPL", event_id="evt_merger_8k", event_date="2026-01-10")
    _insert_investigation(conn, "AAPL", event_id="evt_routine_10q", event_date="2026-01-20")
    # 8-K filing for first event (earlier), 10-Q filing for second (later)
    conn.execute(
        "INSERT INTO filings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ["f_merger", "AAPL", "1234", "8-K", "0001-26-001", "2026-01-08",
         "8-K: definitive acquisition agreement for $500M", None],
    )
    conn.execute(
        "INSERT INTO filings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ["f_10q", "AAPL", "1234", "10-Q", "0001-26-002", "2026-01-18",
         "form10-q.htm", None],
    )
    _insert_score(conn, "AAPL", 42.0, tier="Reject")

    events = sample_near_threshold_events(conn, max_events_per_ticker=1)
    assert len(events) == 1
    assert events[0]["event_id"] == "evt_merger_8k"
    assert events[0].get("event_priority") is not None
    assert isinstance(events[0].get("event_priority_reasons"), list)


# ── 25. event debug fields present in sampled events ─────────────────────────

def test_sampled_events_have_priority_debug_fields(tmp_path):
    """Every sampled event dict includes event_priority (int) and event_priority_reasons (list)."""
    conn = _make_test_db(tmp_path)
    _insert_investigation(conn, "AAPL")
    _insert_filing(conn, "AAPL", "8-K", "2026-01-08")
    _insert_score(conn, "AAPL", 42.0, tier="Reject")

    events = sample_near_threshold_events(conn)
    assert len(events) == 1
    assert isinstance(events[0]["event_priority"], int)
    assert isinstance(events[0]["event_priority_reasons"], list)
