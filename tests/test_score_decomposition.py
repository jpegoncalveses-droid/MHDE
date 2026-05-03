"""TDD tests for score decomposition exposure.

Tests that:
- Component scores can be exported to CSV
- Queue entries carry component breakdown fields
- Score components explain the total (within rounding tolerance)
- No production score mutation occurs
"""
from __future__ import annotations

import csv
import os

import duckdb
import pytest


# ── DB fixture ────────────────────────────────────────────────────────────────

def _make_db(tmp_path) -> duckdb.DuckDBPyConnection:
    """Create an in-memory DB with minimal scores rows for two tickers."""
    conn = duckdb.connect()
    conn.execute("""
        CREATE TABLE scores (
            id TEXT PRIMARY KEY,
            run_id TEXT,
            ticker TEXT,
            as_of_date DATE,
            cheap_score FLOAT,
            quality_score FLOAT,
            catalyst_score FLOAT,
            momentum_score FLOAT,
            sentiment_score FLOAT,
            risk_penalty FLOAT,
            total_score FLOAT,
            tier TEXT,
            confidence TEXT,
            why_ranked TEXT,
            why_rejected TEXT,
            missing_data_json TEXT,
            created_at TIMESTAMP,
            UNIQUE(run_id, ticker)
        )
    """)
    # total_score = 0.30*cheap + 0.25*quality + 0.25*catalyst + 0.10*momentum + 0.10*sentiment - 0.20*risk
    # CTRA: 0.30*70 + 0.25*65 + 0.25*80 + 0.10*55 + 0.10*60 - 0.20*15 = 65.75 → tier B
    # VG:   0.30*45 + 0.25*55 + 0.25*60 + 0.10*40 + 0.10*50 - 0.20*25 = 46.25 → tier C
    conn.execute("""
        INSERT INTO scores VALUES
            ('id1','run1','CTRA','2026-05-02',70.0,65.0,80.0,55.0,60.0,15.0,65.75,'B','high',NULL,NULL,NULL,NOW()),
            ('id2','run1','VG',  '2026-05-02',45.0,55.0,60.0,40.0,50.0,25.0,46.25,'C','medium',NULL,NULL,NULL,NOW())
    """)
    return conn


# ── 1. export_score_components writes CSV with required columns ───────────────

def test_export_score_components_writes_csv(tmp_path):
    """export_score_components() writes CSV with all component columns."""
    from scoring.scorecard import export_score_components
    conn = _make_db(tmp_path)
    path = export_score_components(conn, str(tmp_path))
    assert os.path.exists(path)
    rows = list(csv.DictReader(open(path)))
    assert len(rows) >= 1
    row = rows[0]
    for col in ("ticker", "total_score", "tier", "cheap_score", "quality_score",
                "catalyst_score", "momentum_score", "sentiment_score", "risk_penalty"):
        assert col in row, f"Missing column: {col}"


# ── 2. export_score_components includes CTRA and VG when present ──────────────

def test_export_includes_ctra_and_vg(tmp_path):
    """CTRA and VG rows appear in the component CSV when they are in the DB."""
    from scoring.scorecard import export_score_components
    conn = _make_db(tmp_path)
    path = export_score_components(conn, str(tmp_path))
    rows = list(csv.DictReader(open(path)))
    tickers = {r["ticker"] for r in rows}
    assert "CTRA" in tickers
    assert "VG" in tickers


# ── 3. Component scores explain total within rounding tolerance ───────────────

def test_component_scores_explain_total(tmp_path):
    """total_score ≈ 0.30×cheap + 0.25×quality + 0.25×catalyst + 0.10×momentum + 0.10×sentiment - 0.20×risk."""
    from scoring.scorecard import export_score_components
    conn = _make_db(tmp_path)
    path = export_score_components(conn, str(tmp_path))
    for row in csv.DictReader(open(path)):
        cheap = float(row.get("cheap_score") or 0)
        quality = float(row.get("quality_score") or 0)
        catalyst = float(row.get("catalyst_score") or 0)
        momentum = float(row.get("momentum_score") or 0)
        sentiment = float(row.get("sentiment_score") or 0)
        risk = float(row.get("risk_penalty") or 0)
        total = float(row["total_score"])
        expected = max(0.0, min(100.0,
            0.30 * cheap + 0.25 * quality + 0.25 * catalyst
            + 0.10 * momentum + 0.10 * sentiment - 0.20 * risk
        ))
        assert abs(total - expected) < 1.0, (
            f"{row['ticker']}: computed {expected:.2f} vs stored {total:.2f}"
        )


# ── 4. Queue entries include component breakdown fields ───────────────────────

def test_queue_entries_have_component_fields(tmp_path):
    """build_daily_queue() entries include cheap_score, quality_score, catalyst_score, etc."""
    from missed.catalyst_queue import _enrich_queue_with_score_components

    entries = [
        {"ticker": "CTRA", "original_score": 43.5},
        {"ticker": "VG",   "original_score": 41.2},
    ]
    conn = _make_db(tmp_path)
    _enrich_queue_with_score_components(conn, entries)

    ctra = next(e for e in entries if e["ticker"] == "CTRA")
    assert "cheap_score" in ctra
    assert "quality_score" in ctra
    assert "catalyst_score" in ctra
    assert "risk_penalty_score" in ctra


# ── 5. major_positives and major_negatives are populated ─────────────────────

def test_major_positives_and_negatives_populated(tmp_path):
    """_enrich_queue_with_score_components() adds major_positives / major_negatives."""
    from missed.catalyst_queue import _enrich_queue_with_score_components

    entries = [{"ticker": "CTRA", "original_score": 43.5}]
    conn = _make_db(tmp_path)
    _enrich_queue_with_score_components(conn, entries)

    ctra = entries[0]
    # cheap=70, quality=65, catalyst=80 → positives; risk_penalty=15 (low) → no major negative
    assert "major_positives" in ctra
    assert "major_negatives" in ctra
    assert "catalyst" in ctra["major_positives"].lower() or "cheap" in ctra["major_positives"].lower()


# ── 6. Component fields appear in CSV artifact ────────────────────────────────

def test_component_fields_in_queue_csv(tmp_path):
    """generate_queue_report() includes component score columns in the output CSV."""
    from missed.catalyst_queue import generate_queue_report, _enrich_queue_with_score_components

    entries = [
        {
            "ticker": "CTRA", "original_score": 43.5,
            "event_date": "2026-05-02", "filing_form_type": "8-K",
            "constructed_url": None, "catalyst_type": "merger_acquisition",
            "materiality": "high", "sentiment": "bullish", "confidence": 0.9,
            "evidence_quote": "Definitive merger agreement.", "validation_status": "valid",
            "quote_validation_pass": True, "final_should_affect_score": True,
            "original_tier": "Reject", "llm_adjustment": 5.0, "shadow_score": 48.5,
            "shadow_tier": "C", "tier_move": "Reject→C",
            # already enriched by _enrich_queue_with_score_components
            "cheap_score": 70.0, "quality_score": 65.0, "catalyst_score": 80.0,
            "momentum_score": 55.0, "sentiment_score": 60.0, "risk_penalty_score": 15.0,
            "major_positives": "catalyst; cheap",
            "major_negatives": "",
        }
    ]
    from missed.catalyst_queue import _enrich_with_interpretation
    _enrich_with_interpretation(entries)
    _, csv_path, _ = generate_queue_report(entries, [], str(tmp_path))
    rows = list(csv.DictReader(open(csv_path)))
    assert len(rows) == 1
    assert "major_positives" in rows[0]
    assert "cheap_score" in rows[0]


# ── 7. No production score mutation from component enrichment ─────────────────

def test_no_production_score_mutation(tmp_path):
    """_enrich_queue_with_score_components never writes to the scores table."""
    from missed.catalyst_queue import _enrich_queue_with_score_components

    conn = _make_db(tmp_path)
    before = conn.execute("SELECT total_score FROM scores WHERE ticker='CTRA'").fetchone()[0]

    entries = [{"ticker": "CTRA", "original_score": 43.5}]
    _enrich_queue_with_score_components(conn, entries)

    after = conn.execute("SELECT total_score FROM scores WHERE ticker='CTRA'").fetchone()[0]
    assert before == after, "Score was mutated by enrichment"
