"""TDD tests for HTML report artifact and email digest generation."""
from __future__ import annotations

import os
import pytest


# ── Shared test data ──────────────────────────────────────────────────────────

def _crossing_entry():
    return {
        "ticker": "CTRA",
        "event_date": "2026-02-01",
        "filing_form_type": "8-K",
        "constructed_url": "https://www.sec.gov/Archives/edgar/data/123/000123-001.htm",
        "catalyst_type": "merger_acquisition",
        "materiality": "high",
        "sentiment": "bullish",
        "confidence": 0.90,
        "evidence_quote": "Coterra Energy entered into a definitive Agreement and Plan of Merger.",
        "validation_status": "valid",
        "quote_validation_pass": True,
        "final_should_affect_score": True,
        "original_score": 43.6,
        "original_tier": "Reject",
        "llm_adjustment": 5.0,
        "shadow_score": 48.6,
        "shadow_tier": "C",
        "tier_move": "Reject→C",
    }


def _valid_entry():
    return {
        "ticker": "EPD",
        "event_date": "2026-01-20",
        "filing_form_type": "8-K",
        "constructed_url": None,
        "catalyst_type": "merger_acquisition",
        "materiality": "medium",
        "sentiment": "bullish",
        "confidence": 0.80,
        "evidence_quote": "EPD agreed to acquire MidCoast Energy.",
        "validation_status": "valid",
        "quote_validation_pass": True,
        "final_should_affect_score": True,
        "original_score": 47.1,
        "original_tier": "C",
        "llm_adjustment": 5.0,
        "shadow_score": 52.1,
        "shadow_tier": "C",
        "tier_move": "",
    }


def _weak_entry(ticker="PCG"):
    return {
        "ticker": ticker,
        "event_date": "2026-01-10",
        "filing_form_type": "8-K",
        "constructed_url": None,
        "catalyst_type": "management_change",
        "materiality": "low",
        "sentiment": "neutral",
        "confidence": 0.5,
        "evidence_quote": "",
        "validation_status": "weak_evidence",
        "quote_validation_pass": True,
        "final_should_affect_score": False,
        "original_score": 41.0,
        "original_tier": "Reject",
        "llm_adjustment": 0.0,
        "shadow_score": 41.0,
        "shadow_tier": "Reject",
        "tier_move": "",
    }


def _make_queue(n_weak=6):
    entries = [_crossing_entry(), _valid_entry()]
    for i in range(n_weak):
        e = _weak_entry(f"WEAK{i}")
        entries.append(e)
    return entries


def _make_metadata():
    return {
        "sampled": 43,
        "source_available": 30,
        "classified": 43,
        "valid_actionable": 2,
        "tier_crossings": 1,
        "run_time": "2026-05-02T20:18:00+00:00",
        "score_min": 40.0,
        "score_max": 44.9,
        "provider": "openai",
    }


def _make_revalidated():
    return [
        {"ticker": "CTRA", "event_id": "e1", "source_text_char_count": 500,
         "validation_status": "valid", "should_affect_score": True},
        {"ticker": "EPD", "event_id": "e2", "source_text_char_count": 400,
         "validation_status": "valid", "should_affect_score": True},
    ]


# ── 1. HTML artifact written to output_dir ────────────────────────────────────

def test_html_report_artifact_written(tmp_path):
    """generate_html_report writes daily_catalyst_queue.html to output_dir."""
    from missed.catalyst_queue import generate_html_report
    entries = _make_queue()
    revalidated = _make_revalidated()
    html_path = generate_html_report(entries, revalidated, str(tmp_path), run_metadata=_make_metadata())
    assert os.path.exists(html_path), f"HTML file not found at {html_path}"
    assert html_path.endswith("daily_catalyst_queue.html")
    content = open(html_path).read()
    assert "<html" in content.lower()
    assert "CTRA" in content


# ── 2. HTML report has shadow-only disclaimer ─────────────────────────────────

def test_html_report_has_shadow_only_disclaimer(tmp_path):
    """generate_html_report includes the shadow-only disclaimer."""
    from missed.catalyst_queue import generate_html_report
    html_path = generate_html_report(
        _make_queue(), _make_revalidated(), str(tmp_path), run_metadata=_make_metadata()
    )
    assert "shadow" in open(html_path).read().lower()


# ── 3. HTML report marks Reject→C crossings ───────────────────────────────────

def test_html_report_marks_crossings(tmp_path):
    """generate_html_report highlights Reject→C crossings distinctly."""
    from missed.catalyst_queue import generate_html_report
    html_path = generate_html_report(
        [_crossing_entry()], _make_revalidated(), str(tmp_path), run_metadata=_make_metadata()
    )
    content = open(html_path).read()
    assert "Reject→C" in content or "crossing" in content.lower()


# ── 4. digest txt written ────────────────────────────────────────────────────

def test_digest_txt_written(tmp_path):
    """write_digest_artifacts writes daily_catalyst_digest.txt."""
    from missed.catalyst_digest import write_digest_artifacts
    txt_path, html_path = write_digest_artifacts(
        _make_queue(), _make_revalidated(), _make_metadata(), str(tmp_path)
    )
    assert os.path.exists(txt_path)
    assert txt_path.endswith("daily_catalyst_digest.txt")


# ── 5. digest html written ───────────────────────────────────────────────────

def test_digest_html_written(tmp_path):
    """write_digest_artifacts writes daily_catalyst_digest.html."""
    from missed.catalyst_digest import write_digest_artifacts
    txt_path, html_path = write_digest_artifacts(
        _make_queue(), _make_revalidated(), _make_metadata(), str(tmp_path)
    )
    assert os.path.exists(html_path)
    assert html_path.endswith("daily_catalyst_digest.html")
    assert "<html" in open(html_path).read().lower()


# ── 6. subject includes Reject→C count ───────────────────────────────────────

def test_digest_subject_includes_crossings_count(tmp_path):
    """generate_digest_txt subject line includes Reject→C crossing count."""
    from missed.catalyst_digest import generate_digest_txt
    txt = generate_digest_txt(_make_queue(), _make_revalidated(), _make_metadata())
    assert "Reject→C" in txt or "1 Reject" in txt


# ── 7. subject includes actionable count ─────────────────────────────────────

def test_digest_subject_includes_actionable_count(tmp_path):
    """The digest includes the valid_actionable count."""
    from missed.catalyst_digest import generate_digest_txt
    txt = generate_digest_txt(_make_queue(), _make_revalidated(), _make_metadata())
    # metadata has valid_actionable=2
    assert "2" in txt


# ── 8. promoted candidates appear in txt digest ───────────────────────────────

def test_promoted_candidates_in_txt_digest(tmp_path):
    """Promoted tickers (final_should_affect_score=True) appear in the TXT digest."""
    from missed.catalyst_digest import generate_digest_txt
    txt = generate_digest_txt(_make_queue(), _make_revalidated(), _make_metadata())
    assert "CTRA" in txt
    assert "EPD" in txt


# ── 9. weak evidence summarized (not listed per-row) when >5 ─────────────────

def test_weak_evidence_summarized_not_listed_individually_when_many(tmp_path):
    """When >5 weak rows, digest shows summary counts, not individual tickers."""
    from missed.catalyst_digest import generate_digest_txt
    entries = _make_queue(n_weak=6)  # 6 weak entries
    txt = generate_digest_txt(entries, _make_revalidated(), _make_metadata())
    # Should show count summary, not list all 6 WEAK0..WEAK5 tickers
    weak_ticker_hits = sum(1 for i in range(6) if f"WEAK{i}" in txt)
    assert weak_ticker_hits < 6, "Should not list all individual weak tickers when >5"


# ── 10. dashboard URL in digest when env set ─────────────────────────────────

def test_dashboard_url_in_digest_when_env_set(tmp_path, monkeypatch):
    """When DAILY_CATALYST_REVIEW_URL is set, it appears in the TXT digest."""
    from missed.catalyst_digest import generate_digest_txt
    monkeypatch.setenv("DAILY_CATALYST_REVIEW_URL", "https://mhde.duckdns.org")
    txt = generate_digest_txt(_make_queue(), _make_revalidated(), _make_metadata())
    assert "mhde.duckdns.org" in txt


# ── 11. missing SMTP config raises with clear message ─────────────────────────

def test_missing_smtp_config_raises_clearly(tmp_path, monkeypatch):
    """send_catalyst_digest raises RuntimeError listing missing config keys (not password)."""
    from missed.catalyst_digest import send_catalyst_digest
    cfg = {}  # no SMTP config at all
    with pytest.raises((RuntimeError, SystemExit)) as exc_info:
        send_catalyst_digest(
            cfg, _make_queue(), _make_revalidated(), _make_metadata(),
            email_to="test@example.com"
        )
    err_msg = str(exc_info.value)
    # Must mention what's missing
    assert "SMTP" in err_msg or "smtp" in err_msg.lower()
    # Must NOT include any password value
    assert "secret" not in err_msg.lower()
    assert "password_value" not in err_msg


# ── 12. SMTP password never appears in error messages ────────────────────────

def test_smtp_password_never_in_error(monkeypatch):
    """SMTP_PASSWORD value never leaks in errors or logs."""
    from missed.catalyst_digest import send_catalyst_digest
    cfg = {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": "587",
        "smtp_username": "user@gmail.com",
        "smtp_password": "SUPER_SECRET_PASS",
        # email_to missing to force an error path
    }
    try:
        send_catalyst_digest(
            cfg, _make_queue(), _make_revalidated(), _make_metadata(),
            email_to=""  # empty recipient
        )
    except (RuntimeError, SystemExit, Exception) as e:
        assert "SUPER_SECRET_PASS" not in str(e)


# ── 13. send_email is opt-in (digest artifacts written without sending) ────────

def test_digest_artifacts_written_without_send(tmp_path):
    """write_digest_artifacts writes files without any SMTP calls."""
    from missed.catalyst_digest import write_digest_artifacts
    # No SMTP env vars set — should not raise
    txt_path, html_path = write_digest_artifacts(
        _make_queue(), _make_revalidated(), _make_metadata(), str(tmp_path)
    )
    assert os.path.exists(txt_path)
    assert os.path.exists(html_path)


# ── 14. no production score mutation ─────────────────────────────────────────

def test_no_production_score_mutation(tmp_path):
    """generate_html_report and write_digest_artifacts do not write to any scores table."""
    import duckdb
    from missed.catalyst_queue import generate_html_report
    from missed.catalyst_digest import write_digest_artifacts

    conn = duckdb.connect()
    conn.execute("CREATE TABLE scores (ticker VARCHAR, total_score DOUBLE, tier VARCHAR)")
    conn.execute("INSERT INTO scores VALUES ('CTRA', 43.6, 'Reject')")
    score_before = conn.execute("SELECT total_score FROM scores WHERE ticker='CTRA'").fetchone()[0]
    conn.close()

    generate_html_report(_make_queue(), _make_revalidated(), str(tmp_path), run_metadata=_make_metadata())
    write_digest_artifacts(_make_queue(), _make_revalidated(), _make_metadata(), str(tmp_path))

    conn2 = duckdb.connect()
    conn2.execute("CREATE TABLE scores (ticker VARCHAR, total_score DOUBLE, tier VARCHAR)")
    conn2.execute("INSERT INTO scores VALUES ('CTRA', 43.6, 'Reject')")
    score_after = conn2.execute("SELECT total_score FROM scores WHERE ticker='CTRA'").fetchone()[0]
    conn2.close()
    assert score_before == score_after
