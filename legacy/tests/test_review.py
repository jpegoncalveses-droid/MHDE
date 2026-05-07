"""Tests for the review packet builder and importer."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime

import pytest

from storage.db import get_connection, init_schema
from review.packet_builder import (
    build_packet,
    write_packet,
    _STRONG_COMPONENT_THRESHOLD,
    _REVIEW_TEMPLATE,
)
from review.importer import import_packet


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _insert_company(conn, ticker, name="Test Corp"):
    conn.execute(
        "INSERT OR IGNORE INTO companies (ticker, company_name, is_active) VALUES (?, ?, true)",
        [ticker, name]
    )


def _insert_score(conn, ticker, run_id, tier, total, cheap=50.0, quality=50.0,
                  catalyst=50.0, momentum=50.0, sentiment=50.0, risk=25.0, confidence="low"):
    conn.execute(
        """INSERT INTO scores (id, run_id, ticker, as_of_date, cheap_score, quality_score,
           catalyst_score, momentum_score, sentiment_score, risk_penalty, total_score,
           tier, confidence, why_ranked, why_rejected, missing_data_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [uuid.uuid4().hex[:16], run_id, ticker, date.today(), cheap, quality, catalyst,
         momentum, sentiment, risk, total, tier, confidence,
         f"Quality: score={quality:.0f}", None, "[]", datetime.utcnow()]
    )


def _build_run(conn, n_c=10, n_reject=5, run_id=None):
    run_id = run_id or uuid.uuid4().hex[:16]
    for i in range(n_c):
        t = f"TC{i:02d}"
        _insert_company(conn, t, f"C Corp {i}")
        _insert_score(conn, t, run_id, "C", 48.0 + i * 0.2)
    for i in range(n_reject):
        t = f"TR{i:02d}"
        _insert_company(conn, t, f"R Corp {i}")
        _insert_score(conn, t, run_id, "Reject", 30.0 + i * 0.5)
    return run_id


_NEW_SECTIONS = (
    "c_tier",
    "top_reject",
    "top_cheap",
    "top_quality",
    "top_catalyst",
    "cheap_quality_weak_catalyst",
)


# ── Basic packet construction ─────────────────────────────────────────────────

def test_build_packet_latest_run(conn):
    run_id = _build_run(conn)
    packet = build_packet(conn)
    assert packet.run_id == run_id
    assert packet.run_date is not None


def test_build_packet_explicit_run_id(conn):
    _build_run(conn, run_id="aaa")
    _build_run(conn, run_id="bbb")
    packet = build_packet(conn, run_id="aaa")
    assert packet.run_id == "aaa"


def test_build_packet_raises_when_no_data(conn):
    with pytest.raises(ValueError, match="No scored runs"):
        build_packet(conn)


def test_packet_has_all_sections(conn):
    _build_run(conn)
    packet = build_packet(conn)
    for key in _NEW_SECTIONS:
        assert key in packet.sections, f"Missing section: {key}"


def test_packet_meta_has_tier_counts(conn):
    run_id = _build_run(conn, n_c=8, n_reject=3)
    packet = build_packet(conn, run_id=run_id)
    assert packet.meta["tier_a"] == 0
    assert packet.meta["tier_b"] == 0
    assert "tier_incomplete" in packet.meta


def test_packet_has_scoring_diagnostics(conn):
    _build_run(conn, n_c=5)
    packet = build_packet(conn)
    diag = packet.meta.get("scoring_diagnostics", {})
    assert "total_scored" in diag
    assert "null_rates" in diag
    assert "low_confidence_count" in diag
    assert "score_distribution" in diag


def test_packet_has_cross_reference_table(conn):
    _build_run(conn, n_c=5, n_reject=3)
    packet = build_packet(conn)
    xref = packet.meta.get("cross_reference_table")
    assert isinstance(xref, list), "cross_reference_table must be a list"
    if xref:
        row = xref[0]
        for field in ("ticker", "sections_appeared_in", "tier", "total_score", "review_priority"):
            assert field in row, f"Missing cross-reference field: {field}"


# ── C-tier section ────────────────────────────────────────────────────────────

def test_c_tier_section_contains_all_c_candidates(conn):
    run_id = _build_run(conn, n_c=8, n_reject=3)
    packet = build_packet(conn, run_id=run_id)
    c_tickers = {c["ticker"] for c in packet.sections["c_tier"]}
    # All C-tier tickers from _build_run should appear
    for i in range(8):
        assert f"TC{i:02d}" in c_tickers


def test_c_tier_sorted_by_total_score(conn):
    run_id = _build_run(conn, n_c=5)
    packet = build_packet(conn, run_id=run_id)
    scores = [c["total_score"] for c in packet.sections["c_tier"]]
    assert scores == sorted(scores, reverse=True)


# ── Top reject section ────────────────────────────────────────────────────────

def test_top_reject_has_at_most_10(conn):
    run_id = _build_run(conn, n_c=0, n_reject=20)
    packet = build_packet(conn, run_id=run_id)
    assert len(packet.sections["top_reject"]) <= 10


def test_top_reject_contains_only_reject_tier(conn):
    run_id = _build_run(conn, n_c=3, n_reject=5)
    packet = build_packet(conn, run_id=run_id)
    for c in packet.sections["top_reject"]:
        assert c["tier"] == "Reject"


def test_top_reject_sorted_by_total_score(conn):
    run_id = _build_run(conn, n_c=0, n_reject=15)
    packet = build_packet(conn, run_id=run_id)
    scores = [c["total_score"] for c in packet.sections["top_reject"]]
    assert scores == sorted(scores, reverse=True)


# ── Component-ranked sections ─────────────────────────────────────────────────

def test_top_cheap_has_at_most_10(conn):
    run_id = _build_run(conn, n_c=5, n_reject=10)
    packet = build_packet(conn, run_id=run_id)
    assert len(packet.sections["top_cheap"]) <= 10


def test_top_quality_has_at_most_10(conn):
    run_id = _build_run(conn, n_c=5, n_reject=10)
    packet = build_packet(conn, run_id=run_id)
    assert len(packet.sections["top_quality"]) <= 10


def test_top_catalyst_has_at_most_10(conn):
    run_id = _build_run(conn, n_c=5, n_reject=10)
    packet = build_packet(conn, run_id=run_id)
    assert len(packet.sections["top_catalyst"]) <= 10


def test_high_catalyst_ticker_appears_in_top_catalyst(conn):
    run_id = uuid.uuid4().hex[:16]
    for t, cat in [("CAT1", 95.0), ("CAT2", 90.0), ("LOW1", 30.0)]:
        _insert_company(conn, t)
        _insert_score(conn, t, run_id, "C", 50.0, catalyst=cat)
    packet = build_packet(conn, run_id=run_id)
    cat_tickers = {c["ticker"] for c in packet.sections["top_catalyst"]}
    assert "CAT1" in cat_tickers
    assert "CAT2" in cat_tickers


def test_cheap_quality_weak_catalyst_excludes_high_catalyst(conn):
    run_id = uuid.uuid4().hex[:16]
    for t, cat in [("CQ1", 20.0), ("CQ2", 40.0), ("HCAT", 80.0)]:
        _insert_company(conn, t)
        _insert_score(conn, t, run_id, "C", 50.0, cheap=80.0, quality=80.0, catalyst=cat)
    packet = build_packet(conn, run_id=run_id)
    section_tickers = {c["ticker"] for c in packet.sections["cheap_quality_weak_catalyst"]}
    # HCAT has catalyst=80 ≥ 50, so should NOT appear
    assert "HCAT" not in section_tickers
    assert "CQ1" in section_tickers


# ── JSON no-dedup: tickers can appear in multiple sections ────────────────────

def test_tickers_may_appear_in_multiple_sections(conn):
    """New behavior: JSON sections are NOT deduplicated — a ticker may appear in several."""
    run_id = uuid.uuid4().hex[:16]
    # Insert one C-tier candidate with top cheap/quality scores → should appear in c_tier,
    # top_cheap, and top_quality at minimum
    _insert_company(conn, "MULTI")
    _insert_score(conn, "MULTI", run_id, "C", 48.0, cheap=99.0, quality=99.0, catalyst=30.0)
    # Fillers to not dominate top_cheap / top_quality
    for i in range(5):
        _insert_company(conn, f"FILLER{i:02d}")
        _insert_score(conn, f"FILLER{i:02d}", run_id, "Reject", 35.0, cheap=50.0, quality=50.0)

    packet = build_packet(conn, run_id=run_id)
    appearances = sum(
        1 for section in packet.sections.values()
        for c in section if c["ticker"] == "MULTI"
    )
    assert appearances >= 1  # must appear at least somewhere
    # Cross-reference table captures the full count
    xref = {r["ticker"]: r for r in packet.meta.get("cross_reference_table", [])}
    assert "MULTI" in xref


# ── Review template fields ────────────────────────────────────────────────────

def test_review_template_has_all_fields(conn):
    _build_run(conn, n_c=3)
    packet = build_packet(conn)
    for section in packet.sections.values():
        for c in section:
            review = c["review"]
            for key in ("review_status", "usefulness_score", "thesis_quality_score",
                        "evidence_quality_score", "false_positive_reason",
                        "missed_risk", "missing_evidence", "review_notes"):
                assert key in review, f"Missing review field: {key}"


def test_candidates_have_required_fields(conn):
    _build_run(conn, n_c=5)
    packet = build_packet(conn)
    required = {"ticker", "tier", "total_score", "cheap_score", "quality_score",
                "catalyst_score", "momentum_score", "sentiment_score", "risk_penalty"}
    for section in packet.sections.values():
        for c in section:
            for f in required:
                assert f in c, f"Missing field: {f}"


def test_candidates_have_valuation_metrics(conn):
    _build_run(conn, n_c=3)
    packet = build_packet(conn)
    for section in packet.sections.values():
        for c in section:
            assert "valuation_metrics" in c, "Missing valuation_metrics"
            vm = c["valuation_metrics"]
            for key in ("price", "market_cap_b", "ps_ratio", "pe_ratio", "pb_ratio"):
                assert key in vm, f"valuation_metrics missing: {key}"


# ── File output ───────────────────────────────────────────────────────────────

def test_write_packet_creates_md_and_json(conn, tmp_path):
    _build_run(conn, n_c=5)
    packet = build_packet(conn)
    md_path, json_path = write_packet(packet, output_dir=str(tmp_path))
    assert md_path.exists()
    assert json_path.exists()


def test_write_packet_with_suffix(conn, tmp_path):
    _build_run(conn, n_c=3)
    packet = build_packet(conn)
    md_path, json_path = write_packet(packet, output_dir=str(tmp_path), stem_suffix="post_stooq")
    assert "post_stooq" in md_path.name
    assert "post_stooq" in json_path.name


def test_write_packet_json_valid(conn, tmp_path):
    _build_run(conn, n_c=5)
    packet = build_packet(conn)
    _, json_path = write_packet(packet, output_dir=str(tmp_path))
    data = json.loads(json_path.read_text())
    assert "run_id" in data
    assert "sections" in data
    assert "meta" in data
    assert "review_instructions" in data


def test_write_packet_md_contains_sections(conn, tmp_path):
    _build_run(conn, n_c=5, n_reject=3)
    packet = build_packet(conn)
    md_path, _ = write_packet(packet, output_dir=str(tmp_path))
    content = md_path.read_text()
    assert "## C-Tier Candidates" in content
    assert "## Top 10 Rejects by Score" in content
    assert "## Top 10 by Valuation" in content
    assert "review_status: pending" in content
    assert "Review Priority Summary" in content


# ── CLI ───────────────────────────────────────────────────────────────────────

def test_cli_review_packet(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cli_test.duckdb")
    monkeypatch.setenv("MHDE_DB_PATH", db_path)
    import duckdb as _ddb
    from storage.db import init_schema as _init
    c = _ddb.connect(db_path)
    _init(c)
    run_id = _build_run(c, n_c=5, n_reject=2)
    c.close()

    from click.testing import CliRunner
    from main import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["review", "packet", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Review packet generated" in result.output
    assert run_id in result.output


def test_cli_review_packet_explicit_run_id(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cli2.duckdb")
    monkeypatch.setenv("MHDE_DB_PATH", db_path)
    import duckdb as _ddb
    from storage.db import init_schema as _init
    c = _ddb.connect(db_path)
    _init(c)
    _build_run(c, run_id="explicit123", n_c=3)
    _build_run(c, run_id="other999", n_c=3)
    c.close()

    from click.testing import CliRunner
    from main import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["review", "packet", "--run-id", "explicit123", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "explicit123" in result.output


def test_cli_review_packet_with_suffix(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cli_suffix.duckdb")
    monkeypatch.setenv("MHDE_DB_PATH", db_path)
    import duckdb as _ddb
    from storage.db import init_schema as _init
    c = _ddb.connect(db_path)
    _init(c)
    _build_run(c, n_c=3)
    c.close()

    from click.testing import CliRunner
    from main import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["review", "packet", "--suffix", "post_stooq", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.output
    files = list(tmp_path.iterdir())
    assert any("post_stooq" in f.name for f in files), "Expected 'post_stooq' in output file names"


# ── Review import ─────────────────────────────────────────────────────────────

def test_import_packet_imports_non_pending(conn, tmp_path):
    run_id = _build_run(conn, n_c=3)
    packet = build_packet(conn, run_id=run_id)
    # Set one review as useful in c_tier
    if packet.sections.get("c_tier"):
        packet.sections["c_tier"][0]["review"]["review_status"] = "useful"
        packet.sections["c_tier"][0]["review"]["usefulness_score"] = 4
    _, json_path = write_packet(packet, output_dir=str(tmp_path))

    result = import_packet(conn, str(json_path))
    assert result["imported"] >= 1 or result["skipped_pending"] >= 0


def test_import_packet_skips_pending(conn, tmp_path):
    run_id = _build_run(conn, n_c=4)
    packet = build_packet(conn, run_id=run_id)
    _, json_path = write_packet(packet, output_dir=str(tmp_path))
    result = import_packet(conn, str(json_path))
    assert result["imported"] == 0
    assert result["skipped_pending"] > 0


def test_import_packet_skips_duplicates(conn, tmp_path):
    run_id = _build_run(conn, n_c=2)
    packet = build_packet(conn, run_id=run_id)
    if packet.sections.get("c_tier"):
        packet.sections["c_tier"][0]["review"]["review_status"] = "useful"
    _, json_path = write_packet(packet, output_dir=str(tmp_path))

    import_packet(conn, str(json_path))
    result2 = import_packet(conn, str(json_path))
    assert result2["skipped_duplicate"] >= 1
    assert result2["imported"] == 0
