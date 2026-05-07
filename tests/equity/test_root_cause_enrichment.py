"""Root-cause enrichment — TDD suite."""
from __future__ import annotations

import csv
import uuid
from datetime import date, timedelta

import pytest

from storage.db import get_connection, init_schema

ENRICHMENT_FIELDS = [
    "enriched_root_cause",
    "root_cause_group",
    "explanation_short",
    "evidence_fields_used",
    "suggested_fix",
    "confidence",
]


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _row(**kwargs) -> dict:
    defaults = dict(
        ticker="TST",
        event_date=date.today() - timedelta(days=3),
        event_type="gain_1d",
        return_value=10.0,
        window_days=1,
        classification="true_miss",
        was_in_universe=True,
        was_scored=True,
        score_before_event=30.0,
        tier_before_event="Reject",
        had_catalyst_evidence=True,
        universe_tier="primary",
        root_cause_hint="scoring_blind_spot",
        score_join_method="scores_join",
        priority_score=5.3,
    )
    defaults.update(kwargs)
    return defaults


def _insert_score(conn, ticker, as_of_date, *, catalyst_score=50.0, quality_score=50.0):
    conn.execute(
        """INSERT INTO scores
           (id, run_id, ticker, as_of_date, total_score, tier,
            catalyst_score, quality_score, momentum_score, cheap_score)
           VALUES (?, ?, ?, ?, 55.0, 'C', ?, ?, 40.0, 40.0)""",
        [uuid.uuid4().hex[:16], uuid.uuid4().hex[:16],
         ticker, as_of_date, catalyst_score, quality_score],
    )


def _insert_earnings_event(conn, ticker, event_date):
    conn.execute(
        """INSERT INTO events (id, ticker, event_type, event_date)
           VALUES (?, ?, 'earnings', ?)""",
        [uuid.uuid4().hex[:16], ticker, event_date],
    )


def _insert_company(conn, ticker, sector):
    conn.execute(
        """INSERT INTO companies (ticker, company_name, sector, universe_tier)
           VALUES (?, ?, ?, 'extended')
           ON CONFLICT (ticker) DO UPDATE SET sector = excluded.sector""",
        [ticker, ticker, sector],
    )


def _insert_company_full(
    conn,
    ticker,
    *,
    cik=None,
    is_adr=False,
    active_sec_reporter=True,
    last_financial_filing_date=None,
    sector=None,
    market_cap=None,
):
    conn.execute(
        """INSERT INTO companies (
               ticker, company_name, cik, is_adr, active_sec_reporter,
               last_financial_filing_date, sector, market_cap, universe_tier
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'extended')
           ON CONFLICT (ticker) DO UPDATE SET
               cik = excluded.cik,
               is_adr = excluded.is_adr,
               active_sec_reporter = excluded.active_sec_reporter,
               last_financial_filing_date = excluded.last_financial_filing_date,
               sector = excluded.sector,
               market_cap = excluded.market_cap""",
        [ticker, ticker, cik, is_adr, active_sec_reporter,
         last_financial_filing_date, sector, market_cap],
    )


# ---------------------------------------------------------------------------
# Test 1: enrich_rows attaches all six fields
# ---------------------------------------------------------------------------

def test_enrich_rows_attaches_all_six_fields(conn):
    from missed.root_cause_enrichment import enrich_rows

    rows = [_row()]
    enriched = enrich_rows(rows, conn)
    assert len(enriched) == 1
    for field in ENRICHMENT_FIELDS:
        assert field in enriched[0], f"Missing field: {field}"


# ---------------------------------------------------------------------------
# Test 2: does not mutate input rows
# ---------------------------------------------------------------------------

def test_enrich_rows_does_not_mutate_input(conn):
    from missed.root_cause_enrichment import enrich_rows

    original = _row()
    rows = [original]
    enrich_rows(rows, conn)
    for field in ENRICHMENT_FIELDS:
        assert field not in original, f"Input row was mutated — field {field!r} added"


# ---------------------------------------------------------------------------
# Test 3: universe_miss → universe_not_seeded
# ---------------------------------------------------------------------------

def test_universe_miss_yields_universe_not_seeded(conn):
    from missed.root_cause_enrichment import enrich_rows

    row = _row(classification="universe_miss", was_in_universe=False)
    enriched = enrich_rows([row], conn)[0]
    assert enriched["enriched_root_cause"] == "universe_not_seeded"
    assert enriched["root_cause_group"] == "universe_gap"
    assert enriched["confidence"] == "high"


# ---------------------------------------------------------------------------
# Test 4: unscored_mover → pre_score_history
# ---------------------------------------------------------------------------

def test_unscored_mover_yields_pre_score_history(conn):
    from missed.root_cause_enrichment import enrich_rows

    row = _row(classification="unscored_mover", was_scored=False, score_before_event=None)
    enriched = enrich_rows([row], conn)[0]
    assert enriched["enriched_root_cause"] == "pre_score_history"
    assert enriched["root_cause_group"] == "data_gap"
    assert enriched["confidence"] == "high"


# ---------------------------------------------------------------------------
# Test 5: tier_before_event=Incomplete with no companies entry → missing_cik
# ---------------------------------------------------------------------------

def test_incomplete_tier_no_companies_entry_yields_missing_cik(conn):
    from missed.root_cause_enrichment import enrich_rows

    row = _row(
        classification="true_miss",
        tier_before_event="Incomplete",
    )
    enriched = enrich_rows([row], conn)[0]
    assert enriched["enriched_root_cause"] == "missing_cik"
    assert enriched["root_cause_group"] == "data_gap"
    assert enriched["confidence"] == "high"
    assert enriched["incomplete_diag_ticker_in_companies"] == "no"


# ---------------------------------------------------------------------------
# Test 6: earnings within 7 days → missing_earnings_context
# ---------------------------------------------------------------------------

def test_earnings_within_7_days_yields_missing_earnings_context(conn):
    from missed.root_cause_enrichment import enrich_rows

    event_date = date.today() - timedelta(days=10)
    earnings_date = event_date + timedelta(days=4)
    _insert_earnings_event(conn, "TST", earnings_date.isoformat())

    row = _row(
        event_date=event_date,
        classification="true_miss",
        tier_before_event="Reject",
    )
    enriched = enrich_rows([row], conn)[0]
    assert enriched["enriched_root_cause"] == "missing_earnings_context"
    assert enriched["root_cause_group"] == "feature_gap"


# ---------------------------------------------------------------------------
# Test 7: no catalyst evidence → no_evidence_no_filing
# ---------------------------------------------------------------------------

def test_no_catalyst_evidence_yields_no_evidence_no_filing(conn):
    from missed.root_cause_enrichment import enrich_rows

    row = _row(
        classification="true_miss",
        tier_before_event="Reject",
        had_catalyst_evidence=False,
    )
    enriched = enrich_rows([row], conn)[0]
    assert enriched["enriched_root_cause"] == "no_evidence_no_filing"
    assert enriched["root_cause_group"] == "data_gap"


# ---------------------------------------------------------------------------
# Test 8: sector cluster → sector_cluster_move
# ---------------------------------------------------------------------------

def test_sector_cluster_yields_sector_cluster_move(conn):
    from missed.root_cause_enrichment import enrich_rows

    event_date = date.today() - timedelta(days=10)
    for ticker in ("TST", "P1", "P2"):
        _insert_company(conn, ticker, "Technology")

    rows = [
        _row(ticker="TST", event_date=event_date, classification="true_miss",
             tier_before_event="Reject", had_catalyst_evidence=True, window_days=1),
        _row(ticker="P1", event_date=event_date, classification="true_miss",
             tier_before_event="Reject", had_catalyst_evidence=True, window_days=1),
        _row(ticker="P2", event_date=event_date, classification="true_miss",
             tier_before_event="Reject", had_catalyst_evidence=True, window_days=1),
    ]
    enriched = enrich_rows(rows, conn)
    tst_row = next(r for r in enriched if r["ticker"] == "TST")
    assert tst_row["enriched_root_cause"] == "sector_cluster_move"
    assert tst_row["root_cause_group"] == "feature_gap"


# ---------------------------------------------------------------------------
# Test 9: low catalyst score → low_catalyst_score
# ---------------------------------------------------------------------------

def test_low_catalyst_score_yields_low_catalyst_score(conn):
    from missed.root_cause_enrichment import enrich_rows

    event_date = date.today() - timedelta(days=5)
    score_date = (event_date - timedelta(days=1)).isoformat()
    _insert_score(conn, "TST", score_date, catalyst_score=20.0, quality_score=60.0)

    row = _row(
        event_date=event_date,
        classification="true_miss",
        tier_before_event="Reject",
        score_before_event=35.0,
        had_catalyst_evidence=True,
    )
    enriched = enrich_rows([row], conn)[0]
    assert enriched["enriched_root_cause"] == "low_catalyst_score"
    assert enriched["root_cause_group"] == "scoring_gap"


# ---------------------------------------------------------------------------
# Test 10: near_threshold + low catalyst → near_threshold_no_catalyst
# ---------------------------------------------------------------------------

def test_near_threshold_low_catalyst_yields_near_threshold_no_catalyst(conn):
    from missed.root_cause_enrichment import enrich_rows

    event_date = date.today() - timedelta(days=5)
    score_date = (event_date - timedelta(days=1)).isoformat()
    _insert_score(conn, "TST", score_date, catalyst_score=20.0, quality_score=60.0)

    row = _row(
        event_date=event_date,
        classification="near_threshold",
        tier_before_event="Reject",
        score_before_event=42.0,
        had_catalyst_evidence=True,
    )
    enriched = enrich_rows([row], conn)[0]
    assert enriched["enriched_root_cause"] == "near_threshold_no_catalyst"
    assert enriched["root_cause_group"] == "near_miss"


# ---------------------------------------------------------------------------
# Test 11: near_threshold + good catalyst → near_threshold_scored
# ---------------------------------------------------------------------------

def test_near_threshold_good_catalyst_yields_near_threshold_scored(conn):
    from missed.root_cause_enrichment import enrich_rows

    event_date = date.today() - timedelta(days=5)
    score_date = (event_date - timedelta(days=1)).isoformat()
    _insert_score(conn, "TST", score_date, catalyst_score=50.0, quality_score=60.0)

    row = _row(
        event_date=event_date,
        classification="near_threshold",
        tier_before_event="Reject",
        score_before_event=42.0,
        had_catalyst_evidence=True,
    )
    enriched = enrich_rows([row], conn)[0]
    assert enriched["enriched_root_cause"] == "near_threshold_scored"
    assert enriched["root_cause_group"] == "near_miss"


# ---------------------------------------------------------------------------
# Test 12: generate_enrichment_report writes CSV and MD
# ---------------------------------------------------------------------------

def test_generate_enrichment_report_writes_csv_and_md(tmp_path, conn):
    from missed.root_cause_enrichment import _ENRICHMENT_EXTRA_COLS, enrich_rows, generate_enrichment_report

    rows = [_row(classification="universe_miss", was_in_universe=False)]
    enriched = enrich_rows(rows, conn)
    csv_path, md_path = generate_enrichment_report(enriched, output_dir=str(tmp_path))

    assert csv_path.exists(), "CSV not written"
    assert md_path.exists(), "MD not written"

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
    for col in _ENRICHMENT_EXTRA_COLS:
        assert col in header, f"Missing enrichment column: {col}"


# ---------------------------------------------------------------------------
# Test 13: markdown report contains required section headings
# ---------------------------------------------------------------------------

def test_md_report_contains_required_sections(tmp_path, conn):
    from missed.root_cause_enrichment import enrich_rows, generate_enrichment_report

    rows = [_row(classification="universe_miss", was_in_universe=False)]
    enriched = enrich_rows(rows, conn)
    _, md_path = generate_enrichment_report(enriched, output_dir=str(tmp_path))
    md = md_path.read_text()

    required = [
        "# Root Cause Enrichment Report",
        "## Root Cause Group Summary",
        "## Detailed Root Cause Breakdown",
        "## Top Enriched Rows (true_miss / scored_missed / near_threshold)",
    ]
    for heading in required:
        assert heading in md, f"Missing section heading: {heading!r}"


# ---------------------------------------------------------------------------
# Test 14: no production score mutation
# ---------------------------------------------------------------------------

def test_no_production_score_mutation(conn):
    from missed.root_cause_enrichment import enrich_rows

    score_id = uuid.uuid4().hex[:16]
    conn.execute(
        """INSERT INTO scores (id, run_id, ticker, as_of_date, total_score, tier)
           VALUES (?, ?, 'SCORE_TST', CURRENT_DATE, 55.0, 'C')""",
        [score_id, uuid.uuid4().hex[:16]],
    )
    rows = [_row(ticker="SCORE_TST")]
    enrich_rows(rows, conn)
    row = conn.execute(
        "SELECT total_score FROM scores WHERE id = ?", [score_id]
    ).fetchone()
    assert row is not None and row[0] == 55.0, (
        f"Score was mutated: expected 55.0, got {row}"
    )


# ---------------------------------------------------------------------------
# Test 15: low quality score → low_quality_score
# ---------------------------------------------------------------------------

def test_low_quality_score_yields_low_quality_score(conn):
    from missed.root_cause_enrichment import enrich_rows

    event_date = date.today() - timedelta(days=5)
    score_date = (event_date - timedelta(days=1)).isoformat()
    _insert_score(conn, "TST", score_date, catalyst_score=50.0, quality_score=30.0)

    row = _row(
        event_date=event_date,
        classification="true_miss",
        tier_before_event="Reject",
        score_before_event=35.0,
        had_catalyst_evidence=True,
    )
    enriched = enrich_rows([row], conn)[0]
    assert enriched["enriched_root_cause"] == "low_quality_score"
    assert enriched["root_cause_group"] == "scoring_gap"


# ---------------------------------------------------------------------------
# Test 16: unknown fallback when no score data in DB
# ---------------------------------------------------------------------------

def test_unknown_fallback_when_no_score_data(conn):
    from missed.root_cause_enrichment import enrich_rows

    row = _row(
        classification="true_miss",
        tier_before_event="Reject",
        had_catalyst_evidence=True,
        score_before_event=50.0,
    )
    enriched = enrich_rows([row], conn)[0]
    assert enriched["enriched_root_cause"] == "unknown"


# ---------------------------------------------------------------------------
# Tests 17–21: incomplete_fundamentals subcauses
# ---------------------------------------------------------------------------

def test_incomplete_stale_fundamentals_subcause(conn):
    from missed.root_cause_enrichment import enrich_rows

    stale_date = (date.today() - timedelta(days=200)).isoformat()
    _insert_company_full(
        conn, "TST",
        cik="0001234567",
        is_adr=False,
        active_sec_reporter=True,
        last_financial_filing_date=stale_date,
        sector="Technology",
        market_cap=50_000_000_000.0,
    )
    row = _row(classification="true_miss", tier_before_event="Incomplete")
    enriched = enrich_rows([row], conn)[0]
    assert enriched["enriched_root_cause"] == "stale_fundamentals"
    assert enriched["root_cause_group"] == "data_gap"
    assert enriched["incomplete_diag_ticker_in_companies"] == "yes"
    assert enriched["incomplete_diag_filing_age_days"] != ""


def test_incomplete_adr_subcause(conn):
    from missed.root_cause_enrichment import enrich_rows

    _insert_company_full(
        conn, "TST",
        cik="0001234567",
        is_adr=True,
        active_sec_reporter=True,
        sector="Technology",
        market_cap=100_000_000_000.0,
    )
    row = _row(classification="true_miss", tier_before_event="Incomplete")
    enriched = enrich_rows([row], conn)[0]
    assert enriched["enriched_root_cause"] == "foreign_filer_or_adr"
    assert enriched["incomplete_diag_is_adr"] == "true"


def test_incomplete_missing_sec_companyfacts_subcause(conn):
    from missed.root_cause_enrichment import enrich_rows

    _insert_company_full(
        conn, "TST",
        cik="0001234567",
        is_adr=False,
        active_sec_reporter=False,
        sector="Technology",
        market_cap=50_000_000_000.0,
    )
    row = _row(classification="true_miss", tier_before_event="Incomplete")
    enriched = enrich_rows([row], conn)[0]
    assert enriched["enriched_root_cause"] == "missing_sec_companyfacts"
    assert enriched["incomplete_diag_active_sec_reporter"] == "false"


def test_incomplete_sector_model_gap_subcause(conn):
    from missed.root_cause_enrichment import enrich_rows

    recent_date = (date.today() - timedelta(days=30)).isoformat()
    _insert_company_full(
        conn, "TST",
        cik="0001234567",
        is_adr=False,
        active_sec_reporter=True,
        last_financial_filing_date=recent_date,
        sector="Financials",
        market_cap=80_000_000_000.0,
    )
    row = _row(classification="true_miss", tier_before_event="Incomplete")
    enriched = enrich_rows([row], conn)[0]
    assert enriched["enriched_root_cause"] == "sector_specific_model_gap"
    assert enriched["incomplete_diag_sector"] == "Financials"


def test_incomplete_diag_fields_all_present(conn):
    from missed.root_cause_enrichment import enrich_rows, _ENRICHMENT_EXTRA_COLS

    row = _row(classification="true_miss", tier_before_event="Incomplete")
    enriched = enrich_rows([row], conn)[0]
    diag_cols = [c for c in _ENRICHMENT_EXTRA_COLS if c.startswith("incomplete_diag_")]
    assert len(diag_cols) == 9, f"Expected 9 diagnostic cols, got {len(diag_cols)}"
    for col in diag_cols:
        assert col in enriched, f"Diagnostic field missing from enriched row: {col}"
