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
    _NEAR_B_THRESHOLD,
    _HIGH_CATALYST_THRESHOLD,
    _HIGH_CHEAP_QUALITY_THRESHOLD,
    _STRONG_COMPONENT_THRESHOLD,
    _NEAR_B_FALLBACK,
    _HIGH_CATALYST_FALLBACK,
    _REJECTED_FALLBACK,
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


# ── Basic packet construction ─────────────────────────────────────────────────

def test_build_packet_latest_run(conn):
    run_id = _build_run(conn)
    packet = build_packet(conn)
    assert packet.run_id == run_id
    assert packet.run_date is not None


def test_build_packet_explicit_run_id(conn):
    run_id1 = _build_run(conn, run_id="aaa")
    run_id2 = _build_run(conn, run_id="bbb")
    packet = build_packet(conn, run_id="aaa")
    assert packet.run_id == "aaa"


def test_build_packet_raises_when_no_data(conn):
    with pytest.raises(ValueError, match="No scored runs"):
        build_packet(conn)


def test_packet_has_all_sections(conn):
    _build_run(conn)
    packet = build_packet(conn)
    for key in ("top_10", "near_b", "high_catalyst", "cheap_quality_no_catalyst", "rejected_worth_inspecting"):
        assert key in packet.sections


def test_packet_meta_has_tier_counts(conn):
    run_id = _build_run(conn, n_c=8, n_reject=3)
    packet = build_packet(conn, run_id=run_id)
    assert packet.meta["tier_c"] == 8
    assert packet.meta["tier_reject"] == 3
    assert packet.meta["tier_a"] == 0
    assert packet.meta["tier_b"] == 0


# ── Top 10 section ────────────────────────────────────────────────────────────

def test_top10_has_at_most_10(conn):
    _build_run(conn, n_c=20, n_reject=5)
    packet = build_packet(conn)
    assert len(packet.sections["top_10"]) <= 10


def test_top10_sorted_by_total_score(conn):
    run_id = _build_run(conn, n_c=15)
    packet = build_packet(conn, run_id=run_id)
    scores = [c["total_score"] for c in packet.sections["top_10"]]
    assert scores == sorted(scores, reverse=True)


def test_top10_candidates_have_review_template(conn):
    _build_run(conn, n_c=5)
    packet = build_packet(conn)
    for c in packet.sections["top_10"]:
        assert "review" in c
        assert c["review"]["review_status"] == "pending"
        assert c["review"]["usefulness_score"] is None


def test_top10_candidates_have_required_fields(conn):
    _build_run(conn, n_c=5)
    packet = build_packet(conn)
    required = {"ticker", "tier", "total_score", "cheap_score", "quality_score",
                "catalyst_score", "momentum_score", "sentiment_score", "risk_penalty"}
    for c in packet.sections["top_10"]:
        for field in required:
            assert field in c, f"Missing field: {field}"


# ── Near-B fallback logic ─────────────────────────────────────────────────────

def test_near_b_returns_candidates_above_threshold(conn):
    run_id = uuid.uuid4().hex[:16]
    for t, score in [("HI1", 57.0), ("HI2", 58.0), ("LO1", 46.0)]:
        _insert_company(conn, t)
        _insert_score(conn, t, run_id, "C", score)
    packet = build_packet(conn, run_id=run_id)
    near_b_tickers = {c["ticker"] for c in packet.sections["near_b"]}
    assert "HI1" in near_b_tickers
    assert "HI2" in near_b_tickers


def test_near_b_fallback_when_none_above_threshold(conn):
    run_id = _build_run(conn, n_c=8, n_reject=2)  # all scores ~48-49
    packet = build_packet(conn, run_id=run_id)
    assert len(packet.sections["near_b"]) <= _NEAR_B_FALLBACK
    assert len(packet.sections["near_b"]) > 0
    assert any("near_b" in w.lower() or f"≥{_NEAR_B_THRESHOLD}" in w
               for w in packet.warnings)


# ── High catalyst fallback logic ──────────────────────────────────────────────

def test_high_catalyst_returns_above_threshold(conn):
    run_id = uuid.uuid4().hex[:16]
    for t, cat in [("CAT1", 65.0), ("CAT2", 70.0), ("LOW1", 30.0)]:
        _insert_company(conn, t)
        _insert_score(conn, t, run_id, "C", 50.0, catalyst=cat)
    packet = build_packet(conn, run_id=run_id)
    cat_tickers = {c["ticker"] for c in packet.sections["high_catalyst"]}
    assert "CAT1" in cat_tickers
    assert "CAT2" in cat_tickers


def test_high_catalyst_fallback_when_none_above_threshold(conn):
    run_id = _build_run(conn, n_c=6, n_reject=2)
    packet = build_packet(conn, run_id=run_id)
    assert len(packet.sections["high_catalyst"]) <= _HIGH_CATALYST_FALLBACK
    assert len(packet.sections["high_catalyst"]) > 0
    assert any(f"≥{_HIGH_CATALYST_THRESHOLD}" in w or "catalyst" in w.lower()
               for w in packet.warnings)


# ── Rejected candidates fallback logic ───────────────────────────────────────

def test_rejected_strong_component_included(conn):
    run_id = uuid.uuid4().hex[:16]
    _insert_company(conn, "RJ1")
    _insert_score(conn, "RJ1", run_id, "Reject", 42.0, quality=70.0)  # strong quality
    _insert_company(conn, "RJ2")
    _insert_score(conn, "RJ2", run_id, "Reject", 35.0)  # weak everywhere
    packet = build_packet(conn, run_id=run_id)
    tickers = {c["ticker"] for c in packet.sections["rejected_worth_inspecting"]}
    assert "RJ1" in tickers


def test_rejected_fallback_when_no_strong_components(conn):
    run_id = uuid.uuid4().hex[:16]
    for i in range(6):
        t = f"WK{i}"
        _insert_company(conn, t)
        _insert_score(conn, t, run_id, "Reject", 35.0, cheap=40.0, quality=40.0, catalyst=40.0)
    packet = build_packet(conn, run_id=run_id)
    assert len(packet.sections["rejected_worth_inspecting"]) <= _REJECTED_FALLBACK
    assert len(packet.sections["rejected_worth_inspecting"]) > 0


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


# ── File output ───────────────────────────────────────────────────────────────

def test_write_packet_creates_md_and_json(conn, tmp_path):
    _build_run(conn, n_c=5)
    packet = build_packet(conn)
    md_path, json_path = write_packet(packet, output_dir=str(tmp_path))
    assert md_path.exists()
    assert json_path.exists()


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
    assert "## Top 10 Ranked Candidates" in content
    assert "## Near-B Candidates" in content
    assert "## Rejected Candidates Worth Inspecting" in content
    assert "review_status: pending" in content


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
    run_id = _build_run(c, run_id="explicit123", n_c=3)
    _build_run(c, run_id="other999", n_c=3)
    c.close()

    from click.testing import CliRunner
    from main import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["review", "packet", "--run-id", "explicit123", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "explicit123" in result.output


# ── Review import ─────────────────────────────────────────────────────────────

def test_import_packet_imports_non_pending(conn, tmp_path):
    run_id = _build_run(conn, n_c=3)
    packet = build_packet(conn, run_id=run_id)
    # Set one review as useful
    packet.sections["top_10"][0]["review"]["review_status"] = "useful"
    packet.sections["top_10"][0]["review"]["usefulness_score"] = 4
    _, json_path = write_packet(packet, output_dir=str(tmp_path))

    result = import_packet(conn, str(json_path))
    assert result["imported"] == 1
    assert result["skipped_pending"] >= len(packet.sections["top_10"]) - 1


def test_import_packet_skips_pending(conn, tmp_path):
    run_id = _build_run(conn, n_c=4)
    packet = build_packet(conn, run_id=run_id)
    _, json_path = write_packet(packet, output_dir=str(tmp_path))
    # All reviews are pending
    result = import_packet(conn, str(json_path))
    assert result["imported"] == 0
    assert result["skipped_pending"] > 0


def test_import_packet_skips_duplicates(conn, tmp_path):
    run_id = _build_run(conn, n_c=2)
    packet = build_packet(conn, run_id=run_id)
    packet.sections["top_10"][0]["review"]["review_status"] = "useful"
    _, json_path = write_packet(packet, output_dir=str(tmp_path))

    import_packet(conn, str(json_path))
    result2 = import_packet(conn, str(json_path))
    assert result2["skipped_duplicate"] >= 1
    assert result2["imported"] == 0
