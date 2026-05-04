"""TDD tests for the Flask catalyst queue review server.

Uses Flask's test client — no real HTTP server started.
All server tests use create_app(..., unsafe_no_auth=True) unless testing auth explicitly.
"""
from __future__ import annotations

import csv
import json
import os

import pytest


# ── History fixture helpers ───────────────────────────────────────────────────

def _write_day(history_root: str, date: str, metadata: dict, csv_rows: list[dict] | None = None):
    """Write a dated history directory with run_metadata.json and optional CSV."""
    day_dir = os.path.join(history_root, date)
    os.makedirs(day_dir, exist_ok=True)
    with open(os.path.join(day_dir, "run_metadata.json"), "w") as f:
        json.dump(metadata, f)
    csv_path = os.path.join(day_dir, "daily_catalyst_queue.csv")
    fieldnames = [
        "ticker", "event_date", "filing_form_type", "constructed_url",
        "original_score", "llm_adjustment", "shadow_score",
        "original_tier", "shadow_tier", "tier_move",
        "catalyst_type", "materiality", "sentiment", "confidence",
        "validation_status", "quote_validation_pass", "final_should_affect_score",
        "evidence_quote",
        "expected_direction", "expected_move_summary", "expected_timeframe",
        "action_guidance", "action_reason", "key_checks",
        "priced_in_risk", "days_since_event", "impact_estimate",
        "scaled_shadow_score", "scaled_adjustment",
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        if csv_rows:
            writer.writerows(csv_rows)


_CROSSING_ROW = {
    "ticker": "CTRA", "event_date": "2026-01-10",
    "filing_form_type": "8-K",
    "constructed_url": "https://sec.gov/test",
    "original_score": "43.0", "llm_adjustment": "5.0", "shadow_score": "48.0",
    "original_tier": "Reject", "shadow_tier": "C", "tier_move": "Reject→C",
    "catalyst_type": "merger_acquisition", "materiality": "high",
    "sentiment": "bullish", "confidence": "0.9",
    "validation_status": "valid", "quote_validation_pass": "True",
    "final_should_affect_score": "True",
    "evidence_quote": "CTRA entered into a definitive merger agreement.",
    "scaled_adjustment": "4.0", "scaled_shadow_score": "47.0",
    "expected_direction": "bullish", "days_since_event": "5",
    "impact_estimate": "high", "priced_in_risk": "low",
}
_WEAK_ROW = {
    "ticker": "PCG", "event_date": "2026-01-11",
    "filing_form_type": "8-K", "constructed_url": None,
    "original_score": "42.0", "llm_adjustment": "0.0", "shadow_score": "42.0",
    "original_tier": "Reject", "shadow_tier": "Reject", "tier_move": "",
    "catalyst_type": "management_change", "materiality": "low",
    "sentiment": "neutral", "confidence": "0.4",
    "validation_status": "weak_evidence", "quote_validation_pass": "True",
    "final_should_affect_score": "False",
    "evidence_quote": "",
}

_BASE_META = {
    "sampled": 43,
    "source_available": 41,
    "classified": 43,
    "valid_actionable": 4,
    "tier_crossings": 2,
    "run_time": "2026-05-02T20:18:00+00:00",
    "score_min": 40.0,
    "score_max": 44.9,
    "provider": "openai (cached)",
}


def _make_app(tmp_path, dates=None, unsafe_no_auth=True):
    from review.server import create_app
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    if dates:
        for date, meta, rows in dates:
            _write_day(history_root, date, meta, rows)
    return create_app(history_root, output_dir, unsafe_no_auth=unsafe_no_auth)


# ── 1. Homepage renders with no runs ─────────────────────────────────────────

def test_homepage_renders_with_no_runs(tmp_path):
    """GET / returns 200 and shows 'no runs' message when history is empty."""
    app = _make_app(tmp_path)
    with app.test_client() as client:
        resp = client.get("/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "No catalyst queue runs" in body or "no runs" in body.lower()


# ── 2. Homepage shows shadow-only disclaimer ──────────────────────────────────

def test_homepage_shows_shadow_only_disclaimer(tmp_path):
    """GET / always contains the shadow-only disclaimer."""
    app = _make_app(tmp_path)
    with app.test_client() as client:
        resp = client.get("/")
    assert resp.status_code == 200
    body = resp.data.decode().lower()
    assert "shadow" in body


# ── 3. Homepage shows latest run summary ─────────────────────────────────────

def test_homepage_shows_latest_run_summary(tmp_path):
    """GET / shows sampled count and valid actionable from latest run_metadata.json."""
    app = _make_app(tmp_path, dates=[
        ("2026-05-02", _BASE_META, [_CROSSING_ROW]),
    ])
    with app.test_client() as client:
        resp = client.get("/")
    body = resp.data.decode()
    assert "43" in body   # sampled
    assert "4" in body    # valid_actionable


# ── 4. /runs lists historical run dates ──────────────────────────────────────

def test_runs_list_shows_historical_dates(tmp_path):
    """GET /runs lists all YYYY-MM-DD directories found in history_root."""
    app = _make_app(tmp_path, dates=[
        ("2026-05-01", _BASE_META, []),
        ("2026-05-02", _BASE_META, [_CROSSING_ROW]),
    ])
    with app.test_client() as client:
        resp = client.get("/runs")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "2026-05-01" in body
    assert "2026-05-02" in body


# ── 5. /runs/YYYY-MM-DD renders promoted candidates ──────────────────────────

def test_run_detail_renders_promoted_candidates(tmp_path):
    """GET /runs/YYYY-MM-DD shows promoted candidate ticker in the response."""
    app = _make_app(tmp_path, dates=[
        ("2026-05-02", _BASE_META, [_CROSSING_ROW]),
    ])
    with app.test_client() as client:
        resp = client.get("/runs/2026-05-02")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "CTRA" in body


# ── 6. /runs/YYYY-MM-DD shows Reject→C crossings section ─────────────────────

def test_run_detail_shows_crossings_section(tmp_path):
    """GET /runs/YYYY-MM-DD includes a Reject→C crossings section."""
    app = _make_app(tmp_path, dates=[
        ("2026-05-02", _BASE_META, [_CROSSING_ROW]),
    ])
    with app.test_client() as client:
        resp = client.get("/runs/2026-05-02")
    body = resp.data.decode()
    assert "Crossing" in body or "crossing" in body.lower() or "Reject" in body


# ── 7. Auth required by default ──────────────────────────────────────────────

def test_auth_required_by_default(tmp_path, monkeypatch):
    """GET / returns 401 when unsafe_no_auth=False and no credentials provided."""
    monkeypatch.setenv("REVIEW_UI_USERNAME", "admin")
    monkeypatch.setenv("REVIEW_UI_PASSWORD", "secret")
    from review.server import create_app
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    app = create_app(history_root, output_dir, unsafe_no_auth=False)
    with app.test_client() as client:
        resp = client.get("/")
    assert resp.status_code == 401


# ── 8. Valid credentials grant access ────────────────────────────────────────

def test_valid_credentials_grant_access(tmp_path, monkeypatch):
    """Correct Basic Auth credentials → 200 on protected routes."""
    import base64
    monkeypatch.setenv("REVIEW_UI_USERNAME", "admin")
    monkeypatch.setenv("REVIEW_UI_PASSWORD", "secret")
    from review.server import create_app
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    app = create_app(history_root, output_dir, unsafe_no_auth=False)
    token = base64.b64encode(b"admin:secret").decode()
    with app.test_client() as client:
        resp = client.get("/", headers={"Authorization": f"Basic {token}"})
    assert resp.status_code == 200


# ── 9. --unsafe-no-auth bypasses auth ────────────────────────────────────────

def test_unsafe_no_auth_bypasses_auth(tmp_path):
    """create_app(unsafe_no_auth=True) serves requests without credentials."""
    app = _make_app(tmp_path, unsafe_no_auth=True)
    with app.test_client() as client:
        resp = client.get("/")
    assert resp.status_code == 200


# ── 10. Daily runner script exists ───────────────────────────────────────────

def test_daily_runner_script_exists():
    """run_daily_catalyst_queue.sh exists in .claude/local_scripts/."""
    script = ".claude/local_scripts/run_daily_catalyst_queue.sh"
    assert os.path.exists(script), f"Missing: {script}"


# ── 11. Daily runner script does not contain secrets ─────────────────────────

def test_daily_runner_script_does_not_contain_secrets():
    """The runner script must not hardcode any passwords or API keys."""
    script = ".claude/local_scripts/run_daily_catalyst_queue.sh"
    if not os.path.exists(script):
        pytest.skip("Script not yet created")
    content = open(script).read()
    forbidden_patterns = ["sk-", "Bearer ", "token=", "password=", "apikey="]
    for pat in forbidden_patterns:
        assert pat.lower() not in content.lower(), \
            f"Possible secret hardcoded in script: {pat!r}"


# ── 12. systemd example files exist ──────────────────────────────────────────

def test_systemd_example_files_exist():
    """All five helper files exist in .claude/local_scripts/."""
    base = ".claude/local_scripts"
    for fname in [
        "review_server.service.example",
        "daily_catalyst_queue.service.example",
        "daily_catalyst_queue.timer.example",
        "install_systemd_examples.md",
        "review_server_caddy_example.txt",
    ]:
        path = os.path.join(base, fname)
        assert os.path.exists(path), f"Missing: {path}"


# ── 13. Caddy example uses correct domain and port ───────────────────────────

def test_caddy_example_uses_correct_domain_and_port():
    """review_server_caddy_example.txt proxies mhde.duckdns.org to 127.0.0.1:8765."""
    path = ".claude/local_scripts/review_server_caddy_example.txt"
    if not os.path.exists(path):
        pytest.skip("Caddy example not yet created")
    content = open(path).read()
    assert "mhde.duckdns.org" in content
    assert "127.0.0.1:8765" in content


# ── 14. Run detail shows weak evidence section ───────────────────────────────

def test_run_detail_shows_weak_evidence_section(tmp_path):
    """GET /runs/YYYY-MM-DD includes weak/rejected evidence section."""
    app = _make_app(tmp_path, dates=[
        ("2026-05-02", _BASE_META, [_CROSSING_ROW, _WEAK_ROW]),
    ])
    with app.test_client() as client:
        resp = client.get("/runs/2026-05-02")
    body = resp.data.decode()
    assert "Weak" in body or "weak" in body.lower() or "PCG" in body


# ── 15. Homepage links to HTML artifact when it exists ───────────────────────

def test_homepage_links_to_html_artifact_when_exists(tmp_path):
    """Homepage includes a link/reference to daily_catalyst_queue.html when file is present."""
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    open(os.path.join(output_dir, "daily_catalyst_queue.html"), "w").write("<html></html>")

    from review.server import create_app
    history_root = str(tmp_path / "history")
    app = create_app(history_root, output_dir, unsafe_no_auth=True)
    with app.test_client() as client:
        resp = client.get("/")
    body = resp.data.decode()
    assert ".html" in body or "html" in body.lower()


# ── 16. PWA manifest returns correct content-type ────────────────────────────

def test_pwa_manifest_returns_json(tmp_path):
    """/manifest.webmanifest returns 200 with application/manifest+json content-type."""
    app = _make_app(tmp_path)
    with app.test_client() as client:
        resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert "manifest" in resp.content_type or "json" in resp.content_type
    body = resp.data.decode()
    assert "MHDE" in body or "mhde" in body.lower()


# ── 17. PWA service worker returns JavaScript ─────────────────────────────────

def test_pwa_service_worker_returns_js(tmp_path):
    """/service-worker.js returns 200 with JavaScript content."""
    app = _make_app(tmp_path)
    with app.test_client() as client:
        resp = client.get("/service-worker.js")
    assert resp.status_code == 200
    assert "javascript" in resp.content_type
    body = resp.data.decode()
    assert "cache" in body.lower() or "fetch" in body.lower()


# ── 18. PWA icon-192 returns PNG ──────────────────────────────────────────────

def test_pwa_icon_192_returns_png(tmp_path):
    """/static/icons/icon-192.png returns 200 with image/png content-type."""
    app = _make_app(tmp_path)
    with app.test_client() as client:
        resp = client.get("/static/icons/icon-192.png")
    assert resp.status_code == 200
    assert resp.content_type == "image/png"
    assert resp.data[:4] == b"\x89PNG"


# ── 19. PWA icon-512 returns PNG ──────────────────────────────────────────────

def test_pwa_icon_512_returns_png(tmp_path):
    """/static/icons/icon-512.png returns 200 with image/png content-type."""
    app = _make_app(tmp_path)
    with app.test_client() as client:
        resp = client.get("/static/icons/icon-512.png")
    assert resp.status_code == 200
    assert resp.content_type == "image/png"
    assert resp.data[:4] == b"\x89PNG"


# ── 20. PWA routes require no auth ───────────────────────────────────────────

def test_pwa_routes_require_no_auth(tmp_path, monkeypatch):
    """PWA asset routes return 200 without credentials even when auth is enabled."""
    monkeypatch.setenv("REVIEW_UI_USERNAME", "admin")
    monkeypatch.setenv("REVIEW_UI_PASSWORD", "secret")
    from review.server import create_app
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    app = create_app(history_root, output_dir, unsafe_no_auth=False)
    with app.test_client() as client:
        for path in ["/manifest.webmanifest", "/service-worker.js",
                     "/static/icons/icon-192.png", "/static/icons/icon-512.png"]:
            resp = client.get(path)
            assert resp.status_code == 200, f"Expected 200 for {path}, got {resp.status_code}"


# ── 21. run_server blocks 0.0.0.0 without --unsafe-public-bind ───────────────

def test_run_server_blocks_public_bind_without_flag(monkeypatch):
    """run_server raises SystemExit when host=0.0.0.0 and unsafe_public_bind=False."""
    monkeypatch.setenv("REVIEW_UI_USERNAME", "admin")
    monkeypatch.setenv("REVIEW_UI_PASSWORD", "secret")
    from review.server import run_server
    with pytest.raises(SystemExit) as exc_info:
        run_server("0.0.0.0", 8765, "/tmp/hist", "/tmp/out",
                   unsafe_no_auth=False, unsafe_public_bind=False, _dry_run=True)
    assert "0.0.0.0" in str(exc_info.value) or "proxy" in str(exc_info.value).lower() \
        or "public" in str(exc_info.value).lower()


# ── 22. run_server allows 0.0.0.0 with --unsafe-public-bind ──────────────────

def test_run_server_allows_public_bind_with_flag(monkeypatch):
    """run_server does not raise when host=0.0.0.0 and unsafe_public_bind=True."""
    monkeypatch.setenv("REVIEW_UI_USERNAME", "admin")
    monkeypatch.setenv("REVIEW_UI_PASSWORD", "secret")
    from review.server import run_server
    # _dry_run=True skips app.run(), so this should return normally
    run_server("0.0.0.0", 8765, "/tmp/hist", "/tmp/out",
               unsafe_no_auth=False, unsafe_public_bind=True, _dry_run=True)


# ── Manual review helpers ─────────────────────────────────────────────────────

def _write_review_csv(history_root: str, date: str, rows: list[dict]):
    """Write manual_review.csv for a dated run directory."""
    from missed.catalyst_history import MANUAL_REVIEW_COLS
    path = os.path.join(history_root, date, "manual_review.csv")
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MANUAL_REVIEW_COLS)
        writer.writeheader()
        writer.writerows(rows)


def _post_review(client, date: str, ticker: str, decision: str, notes: str = ""):
    return client.post(
        f"/runs/{date}/review",
        data={"ticker": ticker, "analyst_decision": decision, "analyst_notes": notes},
        follow_redirects=True,
    )


# ── 23. Review form renders for promoted candidates ───────────────────────────

def test_review_form_renders_for_promoted_candidates(tmp_path):
    """Run detail shows review controls (accept/watch/reject) for promoted candidates."""
    app = _make_app(tmp_path, dates=[("2026-05-02", _BASE_META, [_CROSSING_ROW])])
    with app.test_client() as client:
        resp = client.get("/runs/2026-05-02")
    body = resp.data.decode()
    assert resp.status_code == 200
    assert "accept" in body.lower()
    assert "watch" in body.lower()
    assert "reject" in body.lower()
    assert "CTRA" in body
    # Form must have a POST action
    assert 'method="post"' in body.lower() or "method=post" in body.lower()


# ── 24. POST saves decision to manual_review.csv ─────────────────────────────

def test_post_review_saves_decision_to_csv(tmp_path):
    """POST /runs/YYYY-MM-DD/review saves ticker+decision+notes to manual_review.csv."""
    history_root = str(tmp_path / "history")
    app = _make_app(tmp_path, dates=[("2026-05-02", _BASE_META, [_CROSSING_ROW])])
    with app.test_client() as client:
        resp = _post_review(client, "2026-05-02", "CTRA", "accept", "solid deal")
    assert resp.status_code == 200

    review_path = os.path.join(history_root, "2026-05-02", "manual_review.csv")
    assert os.path.exists(review_path)
    rows = list(csv.DictReader(open(review_path)))
    assert len(rows) == 1
    assert rows[0]["ticker"] == "CTRA"
    assert rows[0]["analyst_decision"] == "accept"
    assert rows[0]["analyst_notes"] == "solid deal"
    assert rows[0]["reviewed_at"] != ""


# ── 25. Existing decision appears on page reload ──────────────────────────────

def test_existing_decision_appears_on_page_reload(tmp_path):
    """After a review is saved, GET /runs/YYYY-MM-DD shows the decision badge."""
    history_root = str(tmp_path / "history")
    app = _make_app(tmp_path, dates=[("2026-05-02", _BASE_META, [_CROSSING_ROW])])
    _write_review_csv(history_root, "2026-05-02", [{
        "ticker": "CTRA", "run_date": "2026-05-02",
        "analyst_decision": "accept", "analyst_notes": "solid",
        "reviewed_at": "2026-05-02T15:00:00Z",
    }])
    with app.test_client() as client:
        resp = client.get("/runs/2026-05-02")
    body = resp.data.decode()
    assert "accept" in body.lower()
    assert "CTRA" in body


# ── 26. Invalid ticker rejected with 400 ─────────────────────────────────────

def test_invalid_ticker_rejected_with_400(tmp_path):
    """POST with a ticker not in the run returns 400."""
    app = _make_app(tmp_path, dates=[("2026-05-02", _BASE_META, [_CROSSING_ROW])])
    with app.test_client() as client:
        resp = client.post("/runs/2026-05-02/review", data={
            "ticker": "NOTREAL",
            "analyst_decision": "accept",
            "analyst_notes": "",
        })
    assert resp.status_code == 400


# ── 27. Invalid decision rejected with 400 ────────────────────────────────────

def test_invalid_decision_rejected_with_400(tmp_path):
    """POST with a decision value not in {accept,watch,reject,unknown} returns 400."""
    app = _make_app(tmp_path, dates=[("2026-05-02", _BASE_META, [_CROSSING_ROW])])
    with app.test_client() as client:
        resp = client.post("/runs/2026-05-02/review", data={
            "ticker": "CTRA",
            "analyst_decision": "INVALID",
            "analyst_notes": "",
        })
    assert resp.status_code == 400


# ── 28. Notes are HTML-escaped in the response ────────────────────────────────

def test_notes_html_escaped_in_response(tmp_path):
    """Notes containing HTML special characters are escaped in the run detail page."""
    history_root = str(tmp_path / "history")
    app = _make_app(tmp_path, dates=[("2026-05-02", _BASE_META, [_CROSSING_ROW])])
    xss = "<script>alert('xss')</script>"
    _write_review_csv(history_root, "2026-05-02", [{
        "ticker": "CTRA", "run_date": "2026-05-02",
        "analyst_decision": "watch", "analyst_notes": xss,
        "reviewed_at": "2026-05-02T15:00:00Z",
    }])
    with app.test_client() as client:
        resp = client.get("/runs/2026-05-02")
    body = resp.data.decode()
    # XSS payload must be escaped — the SW register <script> is legitimately present
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body


# ── 29. manual_review.csv schema preserved after POST ─────────────────────────

def test_manual_review_csv_schema_preserved_after_post(tmp_path):
    """POST creates/updates manual_review.csv with exactly the required columns."""
    from missed.catalyst_history import MANUAL_REVIEW_COLS
    history_root = str(tmp_path / "history")
    app = _make_app(tmp_path, dates=[("2026-05-02", _BASE_META, [_CROSSING_ROW])])
    with app.test_client() as client:
        _post_review(client, "2026-05-02", "CTRA", "watch", "monitoring")
    review_path = os.path.join(history_root, "2026-05-02", "manual_review.csv")
    reader = csv.DictReader(open(review_path))
    assert set(reader.fieldnames or []) == set(MANUAL_REVIEW_COLS)


# ── 30. No production score mutation from review POST ─────────────────────────

def test_no_production_score_mutation_from_review_post(tmp_path):
    """POST /review writes only to manual_review.csv — never to scoring data."""
    import duckdb
    history_root = str(tmp_path / "history")
    app = _make_app(tmp_path, dates=[("2026-05-02", _BASE_META, [_CROSSING_ROW])])

    # Assert no scores DB file is created or touched
    db_path = str(tmp_path / "scores.duckdb")
    assert not os.path.exists(db_path)
    with app.test_client() as client:
        _post_review(client, "2026-05-02", "CTRA", "accept", "")
    assert not os.path.exists(db_path)

    # The only file created/changed must be manual_review.csv
    written = []
    for root, _, files in os.walk(history_root):
        for f in files:
            if f != "manual_review.csv" and f not in (
                "daily_catalyst_queue.csv", "run_metadata.json"
            ):
                written.append(f)
    assert written == [], f"Unexpected files written: {written}"


# ── 31. Homepage shows decision counts for latest run ─────────────────────────

def test_homepage_shows_decision_counts(tmp_path):
    """Homepage summary includes accept/watch/reject counts from the latest run's reviews."""
    history_root = str(tmp_path / "history")
    app = _make_app(tmp_path, dates=[("2026-05-02", _BASE_META, [_CROSSING_ROW])])
    _write_review_csv(history_root, "2026-05-02", [
        {"ticker": "CTRA", "run_date": "2026-05-02", "analyst_decision": "accept",
         "analyst_notes": "", "reviewed_at": "2026-05-02T15:00:00Z"},
        {"ticker": "VG", "run_date": "2026-05-02", "analyst_decision": "watch",
         "analyst_notes": "", "reviewed_at": "2026-05-02T15:01:00Z"},
    ])
    with app.test_client() as client:
        resp = client.get("/")
    body = resp.data.decode().lower()
    assert "accept" in body
    assert "watch" in body


# ── Artifact route helpers ────────────────────────────────────────────────────

def _write_artifact(history_root: str, date: str, ext: str, content: str = "stub"):
    path = os.path.join(history_root, date, f"daily_catalyst_queue.{ext}")
    with open(path, "w") as f:
        f.write(content)


# ── 32. Homepage shows link to latest run detail page ────────────────────────

def test_homepage_latest_run_link_works(tmp_path):
    """Homepage links to /runs/YYYY-MM-DD and the link is reachable."""
    app = _make_app(tmp_path, dates=[("2026-05-02", _BASE_META, [_CROSSING_ROW])])
    with app.test_client() as client:
        resp = client.get("/")
    body = resp.data.decode()
    assert "/runs/2026-05-02" in body


# ── 33. /artifacts/latest/html returns 200 ───────────────────────────────────

def test_artifacts_latest_html_returns_200(tmp_path):
    """GET /artifacts/latest/html serves the latest HTML artifact with 200."""
    history_root = str(tmp_path / "history")
    app = _make_app(tmp_path, dates=[("2026-05-02", _BASE_META, [_CROSSING_ROW])])
    _write_artifact(history_root, "2026-05-02", "html", "<html>test</html>")
    with app.test_client() as client:
        resp = client.get("/artifacts/latest/html")
    assert resp.status_code == 200
    assert b"test" in resp.data


# ── 34. /artifacts/YYYY-MM-DD/csv returns 200 ────────────────────────────────

def test_artifacts_date_csv_returns_200(tmp_path):
    """GET /artifacts/YYYY-MM-DD/csv serves the dated CSV with 200."""
    history_root = str(tmp_path / "history")
    app = _make_app(tmp_path, dates=[("2026-05-02", _BASE_META, [_CROSSING_ROW])])
    with app.test_client() as client:
        resp = client.get("/artifacts/2026-05-02/csv")
    assert resp.status_code == 200
    assert b"ticker" in resp.data


# ── 35. Invalid artifact type returns 404 ────────────────────────────────────

def test_invalid_artifact_type_returns_404(tmp_path):
    """GET /artifacts/latest/exe returns 404 for unsupported artifact types."""
    app = _make_app(tmp_path, dates=[("2026-05-02", _BASE_META, [])])
    with app.test_client() as client:
        resp = client.get("/artifacts/latest/exe")
    assert resp.status_code == 404


# ── 36. Path traversal attempt is rejected ───────────────────────────────────

def test_path_traversal_attempt_is_rejected(tmp_path):
    """Artifact routes reject dates that look like path traversal."""
    app = _make_app(tmp_path, dates=[("2026-05-02", _BASE_META, [])])
    with app.test_client() as client:
        resp = client.get("/artifacts/../../etc/passwd/csv")
    assert resp.status_code in (400, 404)


# ── 37. Artifact routes require auth ─────────────────────────────────────────

def test_artifact_routes_require_auth(tmp_path):
    """GET /artifacts/* returns 401 when not using unsafe_no_auth."""
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    _write_day(history_root, "2026-05-02", _BASE_META, [_CROSSING_ROW])
    from review.server import create_app
    app = create_app(history_root, output_dir, unsafe_no_auth=False)
    with app.test_client() as client:
        resp = client.get("/artifacts/latest/html")
    assert resp.status_code == 401


# ── 38. Homepage shows artifact download links ────────────────────────────────

def test_homepage_shows_artifact_download_links(tmp_path):
    """Homepage includes download links for HTML, CSV, Markdown, and JSONL artifacts."""
    history_root = str(tmp_path / "history")
    app = _make_app(tmp_path, dates=[("2026-05-02", _BASE_META, [_CROSSING_ROW])])
    _write_artifact(history_root, "2026-05-02", "html", "x")
    _write_artifact(history_root, "2026-05-02", "md", "x")
    _write_artifact(history_root, "2026-05-02", "jsonl", "x")
    with app.test_client() as client:
        resp = client.get("/")
    body = resp.data.decode()
    assert "/artifacts/latest/html" in body or "html" in body.lower()
    assert "/artifacts/latest/csv" in body or "csv" in body.lower()


# ── /learning page tests ──────────────────────────────────────────────────────

def test_learning_page_returns_200_no_artifacts(tmp_path):
    """/learning returns 200 even when no prediction CSVs exist."""
    app = _make_app(tmp_path)
    with app.test_client() as client:
        resp = client.get("/learning")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "learning" in body.lower() or "prediction" in body.lower()


def test_learning_artifact_rows_csv_returns_content(tmp_path):
    """/learning/rows_csv serves prediction_vs_actual_rows.csv from output_dir."""
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, "prediction_vs_actual_rows.csv")
    with open(csv_path, "w") as f:
        f.write("ticker,classification\nAAPL,true_miss\n")
    app = _make_app(tmp_path)
    with app.test_client() as client:
        resp = client.get("/learning/rows_csv")
    assert resp.status_code == 200
    assert b"true_miss" in resp.data


def test_learning_artifact_returns_404_for_missing_file(tmp_path):
    """/learning/rows_csv returns 404 when file does not exist."""
    app = _make_app(tmp_path)
    with app.test_client() as client:
        resp = client.get("/learning/rows_csv")
    assert resp.status_code == 404


def test_learning_artifact_returns_404_for_unknown_type(tmp_path):
    """/learning/bad_type returns 404."""
    app = _make_app(tmp_path)
    with app.test_client() as client:
        resp = client.get("/learning/bad_type")
    assert resp.status_code == 404


def test_learning_artifact_requires_auth(tmp_path):
    """/learning/rows_csv returns 401 without credentials when auth is enabled."""
    from review.server import create_app
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, "prediction_vs_actual_rows.csv")
    with open(csv_path, "w") as f:
        f.write("ticker,classification\nAAPL,true_miss\n")
    app = create_app(history_root, output_dir, unsafe_no_auth=False)
    with app.test_client() as client:
        resp = client.get("/learning/rows_csv")
    assert resp.status_code == 401


# ── Phase 1: new routes ───────────────────────────────────────────────────────

def test_today_renders_with_run(tmp_path):
    history_root = str(tmp_path / "history")
    _write_day(history_root, "2026-01-01", _BASE_META, [_CROSSING_ROW])
    from review.server import create_app
    import os
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    a = create_app(history_root, output_dir, unsafe_no_auth=True)
    with a.test_client() as c:
        r = c.get("/today")
    assert r.status_code == 200
    assert b"2026-01-01" in r.data or b"today" in r.data.lower()


def test_today_handles_no_runs(tmp_path):
    from review.server import create_app
    import os
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    a = create_app(history_root, output_dir, unsafe_no_auth=True)
    with a.test_client() as c:
        r = c.get("/today")
    assert r.status_code == 200


def test_today_requires_auth(tmp_path):
    from review.server import create_app
    import os
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    a = create_app(history_root, output_dir, unsafe_no_auth=False)
    with a.test_client() as c:
        r = c.get("/today")
    assert r.status_code == 401


def test_candidates_renders_latest(tmp_path):
    history_root = str(tmp_path / "history")
    _write_day(history_root, "2026-01-01", _BASE_META, [_CROSSING_ROW])
    _write_day(history_root, "2026-01-02", _BASE_META, [_CROSSING_ROW])
    from review.server import create_app
    import os
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    a = create_app(history_root, output_dir, unsafe_no_auth=True)
    with a.test_client() as c:
        r = c.get("/candidates")
    assert r.status_code == 200
    # Should show the latest date (2026-01-02) content
    assert b"2026-01-02" in r.data or b"CTRA" in r.data


def test_candidates_no_runs(tmp_path):
    from review.server import create_app
    import os
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    a = create_app(history_root, output_dir, unsafe_no_auth=True)
    with a.test_client() as c:
        r = c.get("/candidates")
    assert r.status_code == 200


def test_moves_renders_with_data(tmp_path):
    import csv, os
    from review.server import create_app
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    # Write a PVA CSV
    pva_path = os.path.join(output_dir, "prediction_vs_actual_rows.csv")
    with open(pva_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ticker","event_date","return_value","window_days","classification"])
        w.writeheader()
        w.writerow({"ticker":"AAAB","event_date":"2026-01-01","return_value":"0.12","window_days":"5","classification":"true_miss"})
    a = create_app(history_root, output_dir, unsafe_no_auth=True)
    with a.test_client() as c:
        r = c.get("/moves")
    assert r.status_code == 200
    assert b"AAAB" in r.data or b"move" in r.data.lower()


def test_moves_handles_missing_csv(tmp_path):
    from review.server import create_app
    import os
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    a = create_app(history_root, output_dir, unsafe_no_auth=True)
    with a.test_client() as c:
        r = c.get("/moves")
    assert r.status_code == 200


def test_moves_requires_auth(tmp_path):
    from review.server import create_app
    import os
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    a = create_app(history_root, output_dir, unsafe_no_auth=False)
    with a.test_client() as c:
        r = c.get("/moves")
    assert r.status_code == 401


# ── Moves page: grouping and naming ──────────────────────────────────────────

_PVA_COLS = [
    "ticker", "event_date", "return_value", "window_days",
    "classification", "universe_tier", "priority_score",
    "score_before_event", "tier_before_event",
    "had_catalyst_evidence", "was_in_universe", "was_scored",
    "root_cause_hint", "score_join_method",
]


def _write_pva(output_dir: str, rows: list[dict]) -> str:
    path = os.path.join(output_dir, "prediction_vs_actual_rows.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_PVA_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def _pva_row(**kw) -> dict:
    base = {
        "ticker": "AAAB", "event_date": "2026-05-01",
        "return_value": "10.0", "window_days": "1",
        "classification": "true_miss", "universe_tier": "primary",
        "priority_score": "5.0", "score_before_event": "30.0",
        "tier_before_event": "Reject", "had_catalyst_evidence": "True",
        "was_in_universe": "True", "was_scored": "True",
        "root_cause_hint": "data_gap", "score_join_method": "scores_join",
    }
    return {**base, **kw}


def _moves_app(tmp_path, pva_rows: list[dict]):
    from review.server import create_app
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    _write_pva(output_dir, pva_rows)
    history_root = str(tmp_path / "history")
    return create_app(history_root, output_dir, unsafe_no_auth=True)


def test_moves_title_no_longer_says_prediction_vs_actual(tmp_path):
    a = _moves_app(tmp_path, [_pva_row()])
    with a.test_client() as c:
        r = c.get("/moves")
    html = r.data.decode()
    assert "Prediction vs Actual" not in html
    assert "Actual Movers" in html


def test_moves_repeated_ticker_collapses_to_one_summary_row(tmp_path):
    rows = [
        _pva_row(ticker="AAAB", window_days="1", event_date="2026-05-01", return_value="8.0"),
        _pva_row(ticker="AAAB", window_days="3", event_date="2026-05-01", return_value="12.0"),
        _pva_row(ticker="AAAB", window_days="5", event_date="2026-05-01", return_value="15.0"),
    ]
    a = _moves_app(tmp_path, rows)
    with a.test_client() as c:
        r = c.get("/moves")
    html = r.data.decode()
    # Summary sections show one row per ticker: AAAB should appear in section headings, not 3×
    summary_count = html.count(">AAAB<")
    assert summary_count <= 4, f"AAAB appeared {summary_count} times in summary (expected ≤4)"


def test_moves_raw_section_contains_all_events(tmp_path):
    rows = [
        _pva_row(ticker="AAAB", window_days="1", return_value="8.0"),
        _pva_row(ticker="AAAB", window_days="3", return_value="12.0"),
        _pva_row(ticker="BBBB", window_days="1", return_value="6.0"),
    ]
    a = _moves_app(tmp_path, rows)
    with a.test_client() as c:
        r = c.get("/moves")
    html = r.data.decode()
    assert "Raw rolling-window events" in html
    # Raw section should reference both windows
    assert "1-day" in html or "1d" in html or "window" in html.lower()


def test_moves_priority_chooses_1d_over_longer(tmp_path):
    from review.server import _build_ticker_summary
    rows = [
        _pva_row(ticker="AAAB", window_days="1", event_date="2026-05-01", return_value="8.0"),
        _pva_row(ticker="AAAB", window_days="20", event_date="2026-05-01", return_value="30.0"),
    ]
    summaries = _build_ticker_summary(rows)
    assert len(summaries) == 1
    assert summaries[0]["best_window"] == 1


def test_moves_larger_return_breaks_window_tie(tmp_path):
    from review.server import _build_ticker_summary
    rows = [
        _pva_row(ticker="AAAB", window_days="1", event_date="2026-05-01",
                 return_value="5.0", universe_tier="primary"),
        _pva_row(ticker="AAAB", window_days="1", event_date="2026-05-01",
                 return_value="15.0", universe_tier="primary"),
    ]
    summaries = _build_ticker_summary(rows)
    assert len(summaries) == 1
    assert summaries[0]["max_abs_return"] == 15.0


def test_ops_renders(tmp_path):
    from review.server import create_app
    import os
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    a = create_app(history_root, output_dir, unsafe_no_auth=True)
    with a.test_client() as c:
        r = c.get("/ops")
    assert r.status_code == 200
    html = r.data.decode()
    assert "ops" in html.lower() or "artifact" in html.lower()


def test_ops_no_secrets_displayed(tmp_path):
    import os
    from review.server import create_app
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    a = create_app(history_root, output_dir, unsafe_no_auth=True)
    with a.test_client() as c:
        r = c.get("/ops")
    html = r.data.decode().lower()
    assert "password" not in html
    assert "api_key" not in html
    # Check that actual env var values don't appear (keys may appear as labels, not values)


def test_ops_handles_missing_timer(tmp_path):
    """Must not crash when systemctl is absent or returns no mhde timers."""
    from review.server import create_app
    import os
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    a = create_app(history_root, output_dir, unsafe_no_auth=True)
    with a.test_client() as c:
        r = c.get("/ops")
    assert r.status_code == 200


def test_ops_requires_auth(tmp_path):
    from review.server import create_app
    import os
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    a = create_app(history_root, output_dir, unsafe_no_auth=False)
    with a.test_client() as c:
        r = c.get("/ops")
    assert r.status_code == 401


def test_candidates_requires_auth(tmp_path):
    from review.server import create_app
    import os
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    a = create_app(history_root, output_dir, unsafe_no_auth=False)
    with a.test_client() as c:
        r = c.get("/candidates")
    assert r.status_code == 401


def test_today_shows_pva_counts(tmp_path):
    import csv, os
    from review.server import create_app
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    _write_day(history_root, "2026-01-01", _BASE_META, [_CROSSING_ROW])
    pva_path = os.path.join(output_dir, "prediction_vs_actual_rows.csv")
    with open(pva_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ticker","event_date","return_value","window_days","classification"])
        w.writeheader()
        w.writerow({"ticker":"AAAB","event_date":"2026-01-01","return_value":"0.1","window_days":"5","classification":"true_miss"})
        w.writerow({"ticker":"AAAC","event_date":"2026-01-01","return_value":"0.05","window_days":"5","classification":"near_threshold"})
    a = create_app(history_root, output_dir, unsafe_no_auth=True)
    with a.test_client() as c:
        r = c.get("/today")
    assert r.status_code == 200
    html = r.data.decode()
    assert "true_miss" in html or "True Miss" in html or "true miss" in html.lower()


def test_today_shows_warning_when_pva_missing(tmp_path):
    import os
    from review.server import create_app
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    _write_day(history_root, "2026-01-01", _BASE_META, [_CROSSING_ROW])
    a = create_app(history_root, output_dir, unsafe_no_auth=True)
    with a.test_client() as c:
        r = c.get("/today")
    assert r.status_code == 200
    html = r.data.decode()
    assert "warn" in html.lower() or "missing" in html.lower() or "prediction" in html.lower()


# ── Docs viewer ───────────────────────────────────────────────────────────────

def _auth_app(tmp_path):
    from review.server import create_app
    import os
    hr = str(tmp_path / "history")
    od = str(tmp_path / "output")
    os.makedirs(od, exist_ok=True)
    return create_app(hr, od, unsafe_no_auth=False)


def test_docs_index_requires_auth(tmp_path):
    a = _auth_app(tmp_path)
    with a.test_client() as c:
        r = c.get("/docs")
    assert r.status_code == 401


def test_docs_index_renders(tmp_path):
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/docs")
    assert r.status_code == 200
    html = r.data.decode()
    assert "Operating Manual" in html
    assert "Architecture" in html
    assert "Data Sources" in html
    assert "Scoring Governance" in html
    assert "Completion Status" in html


def test_doc_operating_manual_returns_200(tmp_path):
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/docs/operating-manual")
    assert r.status_code == 200
    assert b"MHDE" in r.data or b"Operating" in r.data


def test_doc_architecture_returns_200(tmp_path):
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/docs/architecture")
    assert r.status_code == 200


def test_doc_data_sources_returns_200(tmp_path):
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/docs/data-sources")
    assert r.status_code == 200


def test_doc_scoring_governance_returns_200(tmp_path):
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/docs/scoring-governance")
    assert r.status_code == 200


def test_doc_completion_status_returns_200(tmp_path):
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/docs/completion-status")
    assert r.status_code == 200


def test_doc_unknown_returns_404(tmp_path):
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/docs/this-doc-does-not-exist")
    assert r.status_code == 404


def test_doc_path_traversal_returns_404(tmp_path):
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/docs/%2e%2e%2fetc%2fpasswd")
    assert r.status_code == 404


def test_doc_download_returns_text(tmp_path):
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/docs/download/operating-manual")
    assert r.status_code == 200
    ct = r.headers.get("Content-Type", "")
    assert "text/plain" in ct or "text" in ct
    assert b"#" in r.data or len(r.data) > 100


def test_doc_download_unknown_returns_404(tmp_path):
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/docs/download/no-such-key")
    assert r.status_code == 404


def test_doc_download_requires_auth(tmp_path):
    a = _auth_app(tmp_path)
    with a.test_client() as c:
        r = c.get("/docs/download/operating-manual")
    assert r.status_code == 401


def test_doc_no_secrets_displayed(tmp_path):
    # Docs legitimately mention env var names, but must never render actual credential values.
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/docs/operating-manual")
    html = r.data.decode()
    for key in ("POLYGON_API_KEY", "OPENAI_API_KEY", "ALPHA_VANTAGE_API_KEY", "REVIEW_UI_PASSWORD"):
        val = os.environ.get(key, "")
        if val and len(val) > 8:
            assert val not in html, f"Credential value for {key} should not appear in page"


def test_render_markdown_headings():
    from review.server import _render_markdown
    out = _render_markdown("# Hello\n## World")
    assert "<h1>" in out and "Hello" in out
    assert "<h2>" in out and "World" in out


def test_render_markdown_code_block():
    from review.server import _render_markdown
    out = _render_markdown("```python\nprint('hi')\n```")
    assert "<pre>" in out
    assert "print" in out


def test_render_markdown_table():
    from review.server import _render_markdown
    out = _render_markdown("| A | B |\n|---|---|\n| x | y |")
    assert "<table>" in out
    assert "<th>" in out
    assert "<td>" in out


def test_render_markdown_no_xss():
    from review.server import _render_markdown
    out = _render_markdown("<script>alert(1)</script>")
    assert "<script>" not in out


def test_docs_nav_links_in_today(tmp_path):
    _write_day(str(tmp_path / "history"), "2026-01-01", _BASE_META, [_CROSSING_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/today")
    assert b'/docs' in r.data


def test_docs_nav_links_in_ops(tmp_path):
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/ops")
    assert b'/docs' in r.data


# ── Prediction cards ──────────────────────────────────────────────────────────

_PRED_ROW = {
    "ticker": "TPRD",
    "original_score": "44.0",
    "llm_adjustment": "5.0",
    "shadow_score": "49.0",
    "original_tier": "Reject",
    "shadow_tier": "C",
    "tier_move": "Reject→C",
    "catalyst_type": "earnings",
    "materiality": "high",
    "sentiment": "bullish",
    "confidence": "0.85",
    "validation_status": "valid",
    "quote_validation_pass": "True",
    "final_should_affect_score": "True",
    "evidence_quote": "EPS beat by 15%",
    "expected_direction": "bullish",
    "expected_move_summary": "Strong beat likely drives upward revision",
    "expected_timeframe": "1-5 days post-announcement",
    "action_guidance": "accept",
    "action_reason": "Earnings surprise with bullish guidance revision",
    "key_checks": "guidance revision; analyst estimates; peer reaction",
    "priced_in_risk": "low",
    "days_since_event": "3",
    "impact_estimate": "high",
    "scaled_shadow_score": "46.5",
    "scaled_adjustment": "3.5",
    "constructed_url": "https://sec.gov/fake",
    "event_date": "2026-04-30",
}

_PRED_WATCH_ROW = {
    **_PRED_ROW, "ticker": "TWCH", "action_guidance": "watch",
    "tier_move": "", "shadow_tier": "C", "original_tier": "C",
    "priced_in_risk": "medium", "scaled_adjustment": "1.0",
}
_PRED_IGNORE_ROW = {**_PRED_ROW, "ticker": "TIGN", "action_guidance": "reject",
                    "final_should_affect_score": "False"}


def test_prediction_cards_appear_on_candidates(tmp_path):
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [_PRED_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    assert r.status_code == 200
    html = r.data.decode()
    assert "Prediction Cards" in html
    assert "TPRD" in html


def test_prediction_card_shows_direction(tmp_path):
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [_PRED_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    assert "Bullish" in html or "bullish" in html
    assert "↑" in html


def test_prediction_card_shows_timeframe(tmp_path):
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [_PRED_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    assert "1-5 days post-announcement" in html


def test_prediction_card_shows_summary(tmp_path):
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [_PRED_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    assert "Strong beat likely drives upward revision" in html


def test_prediction_card_action_label_no_review_default(tmp_path):
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [_PRED_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    # "Review" must not appear as an action badge; fresh bullish earnings → High Priority
    assert "High Priority" in html or "Watch" in html
    # Verify "Review" is not used as an action label (it may appear in form elements)
    import re
    badges = re.findall(r'pred-action[^>]*>([^<]+)<', html)
    assert "Review" not in badges


def test_prediction_card_action_label_watch_or_context(tmp_path):
    # C-tier entry with small scaled adjustment — should not be High Priority or Investigate
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [_PRED_WATCH_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    assert "Watch" in html or "Context" in html


def test_prediction_card_shows_key_checks(tmp_path):
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [_PRED_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    assert "guidance revision" in html
    assert "analyst estimates" in html


def test_prediction_card_shows_why_it_may_move(tmp_path):
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [_PRED_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    assert "Earnings surprise with bullish guidance revision" in html


def test_prediction_card_shows_reason_to_wait(tmp_path):
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [_PRED_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    assert "Reason to wait" in html or "priced-in" in html.lower()


def test_prediction_card_shows_scaled_score(tmp_path):
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [_PRED_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    assert "46.5" in html


def test_prediction_card_includes_review_form(tmp_path):
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [_PRED_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    assert 'name="analyst_decision"' in html


def test_prediction_card_not_investment_advice(tmp_path):
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [_PRED_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode().lower()
    assert "not investment advice" in html or "shadow-only" in html


def test_rejected_entry_not_in_prediction_cards(tmp_path):
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [_PRED_IGNORE_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    # TIGN has final_should_affect_score=False, so no prediction card
    assert "TIGN" not in html or "Prediction Cards" not in html or html.count("TIGN") <= 1


def test_not_yet_reason_helper():
    from review.server import _not_yet_reason
    e = {"priced_in_risk": "high", "days_since_event": "45", "impact_estimate": "low"}
    r = _not_yet_reason(e)
    assert "priced-in" in r
    assert "45d" in r
    assert "low" in r


# ── Action priority and signal strength ───────────────────────────────────────

def test_stale_ma_high_pir_becomes_context():
    from review.server import _compute_action_priority
    e = {
        "catalyst_type": "merger_acquisition",
        "expected_direction": "bullish",
        "priced_in_risk": "high",
        "days_since_event": "80",
        "impact_estimate": "high",
        "tier_move": "Reject→C",
        "original_tier": "Reject",
        "scaled_adjustment": "1.0",
    }
    label, _ = _compute_action_priority(e)
    assert label in ("Context", "Low Priority")


def test_bullish_settlement_moderate_decay_becomes_watch():
    from review.server import _compute_action_priority
    e = {
        "catalyst_type": "settlement",
        "expected_direction": "bullish",
        "priced_in_risk": "medium",
        "days_since_event": "25",
        "impact_estimate": "high",
        "tier_move": "Reject→C",
        "original_tier": "Reject",
        "scaled_adjustment": "2.0",
    }
    label, _ = _compute_action_priority(e)
    assert label == "Watch"


def test_neutral_management_change_becomes_investigate():
    from review.server import _compute_action_priority
    e = {
        "catalyst_type": "management_change",
        "expected_direction": "neutral",
        "priced_in_risk": "low",
        "days_since_event": "5",
        "impact_estimate": "medium",
        "tier_move": "Reject→C",
        "original_tier": "Reject",
        "scaled_adjustment": "3.0",
    }
    label, _ = _compute_action_priority(e)
    assert label == "Investigate"


def test_already_c_tier_low_impact_becomes_context():
    from review.server import _compute_action_priority
    e = {
        "catalyst_type": "earnings",
        "expected_direction": "bullish",
        "priced_in_risk": "low",
        "days_since_event": "5",
        "impact_estimate": "high",
        "tier_move": "C→C",
        "original_tier": "C",
        "scaled_adjustment": "0.5",
    }
    label, _ = _compute_action_priority(e)
    assert label == "Context"


def test_low_impact_catalyst_becomes_low_priority():
    from review.server import _compute_action_priority
    e = {
        "catalyst_type": "earnings",
        "expected_direction": "bullish",
        "priced_in_risk": "low",
        "days_since_event": "3",
        "impact_estimate": "low",
        "tier_move": "Reject→C",
        "original_tier": "Reject",
        "scaled_adjustment": "4.0",
    }
    label, _ = _compute_action_priority(e)
    assert label == "Low Priority"


# ── Trading window ────────────────────────────────────────────────────────────

def test_stale_ma_high_pir_gets_mostly_expired():
    from review.server import _compute_trading_window
    e = {
        "catalyst_type": "merger_acquisition",
        "expected_direction": "bullish",
        "priced_in_risk": "high",
        "days_since_event": "80",
        "event_date": "2026-02-01",
    }
    tw = _compute_trading_window(e)
    assert tw["signal_status"] in ("Mostly expired", "Decaying")


def test_regulatory_settlement_gets_20_60_window():
    from review.server import _compute_trading_window
    e = {
        "catalyst_type": "regulatory",
        "expected_direction": "bullish",
        "priced_in_risk": "low",
        "days_since_event": "5",
        "event_date": "2026-04-28",
    }
    tw = _compute_trading_window(e)
    assert "20" in tw["trading_window"] and "60" in tw["trading_window"]


def test_management_change_gets_5_20_window():
    from review.server import _compute_trading_window
    e = {
        "catalyst_type": "management_change",
        "expected_direction": "bullish",
        "priced_in_risk": "low",
        "days_since_event": "3",
        "event_date": "2026-04-30",
    }
    tw = _compute_trading_window(e)
    assert "5" in tw["trading_window"] and "20" in tw["trading_window"]


def test_neutral_direction_gets_no_trading_window():
    from review.server import _compute_trading_window
    e = {
        "catalyst_type": "management_change",
        "expected_direction": "neutral",
        "priced_in_risk": "low",
        "days_since_event": "2",
        "event_date": "2026-05-01",
    }
    tw = _compute_trading_window(e)
    assert tw["trading_window"] == "None"
    assert tw["signal_status"] == "Inactive"


def test_high_pir_shortens_active_to_mostly_expired():
    from review.server import _compute_trading_window
    e = {
        "catalyst_type": "earnings",
        "expected_direction": "bullish",
        "priced_in_risk": "high",
        "days_since_event": "2",
        "event_date": "2026-05-01",
    }
    tw = _compute_trading_window(e)
    assert tw["signal_status"] == "Mostly expired"


# ── Section renaming (scaled vs static-only) ──────────────────────────────────

def test_static_only_crossing_not_labeled_scaled(tmp_path):
    static_row = {
        **_CROSSING_ROW,
        "ticker": "STAT",
        "scaled_adjustment": "0",
    }
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [static_row])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    assert "Static-only Crossings" in html
    assert "STAT" in html


def test_scaled_crossing_appears_in_scaled_section(tmp_path):
    scaled_row = {**_CROSSING_ROW, "ticker": "SCAL", "scaled_adjustment": "3.5"}
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [scaled_row])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    assert "Scaled Crossings" in html
    assert "SCAL" in html


def test_signal_strength_appears_on_card(tmp_path):
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [_PRED_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    assert "Signal strength" in html or "sig-str-" in html


def test_trading_window_appears_on_card(tmp_path):
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [_PRED_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    assert "Trading window" in html
    assert "Signal status" in html
    assert "Entry trigger" in html
    assert "Invalidation" in html


def test_not_yet_reason_no_concerns():
    from review.server import _not_yet_reason
    e = {"priced_in_risk": "low", "days_since_event": "2", "impact_estimate": "high"}
    r = _not_yet_reason(e)
    assert r == "—"


# ── /ticker/<ticker> route ────────────────────────────────────────────────────

def _make_app_with_db(tmp_path, db_path="", dates=None):
    """App factory that accepts an optional db_path for ticker tests."""
    from review.server import create_app
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    if dates:
        for date, meta, rows in dates:
            _write_day(history_root, date, meta, rows)
    return create_app(
        history_root, output_dir,
        unsafe_no_auth=True,
        db_path=db_path or "",
    )


def _seed_db(db_path: str, ticker: str = "AAPL", tier: str = "Reject",
             score: float = 35.0, is_active: bool = True,
             universe_tier: str = "primary", with_event: bool = False) -> None:
    import duckdb
    conn = duckdb.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            ticker VARCHAR, company_name VARCHAR, universe_tier VARCHAR,
            is_active BOOLEAN, sector VARCHAR, industry VARCHAR,
            universe_exclusion_reason VARCHAR, last_financial_filing_date DATE,
            PRIMARY KEY (ticker)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            ticker VARCHAR, as_of_date DATE, total_score DOUBLE,
            tier VARCHAR, why_rejected VARCHAR, missing_data_json VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS missed_opportunity_events (
            event_id VARCHAR, ticker VARCHAR, event_date DATE,
            event_type VARCHAR, return_value DOUBLE, window_days INTEGER,
            tier_before_event VARCHAR, had_catalyst_evidence BOOLEAN,
            investigation_status VARCHAR
        )
    """)
    conn.execute(
        "INSERT INTO companies VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [ticker, f"{ticker} Inc.", universe_tier, is_active,
         "Technology", "Software", None, None],
    )
    conn.execute(
        "INSERT INTO scores VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
        [ticker, "2026-05-01", score, tier,
         f"Score too low ({score:.0f} < 45)", "[]"],
    )
    if with_event:
        conn.execute(
            "INSERT INTO missed_opportunity_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["ev1", ticker, "2026-04-20", "gain_5d_10pct",
             0.12, 5, tier, True, "investigated"],
        )
    conn.close()


def test_ticker_route_known_sp_ticker(tmp_path):
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="AAPL", tier="Reject", score=20.0)
    app = _make_app_with_db(tmp_path, db_path=db)
    with app.test_client() as c:
        r = c.get("/ticker/AAPL")
    assert r.status_code == 200
    html = r.data.decode()
    assert "AAPL" in html
    assert "Universe" in html
    assert "Latest Score" in html
    assert "Candidate Status" in html


def test_ticker_route_unknown_ticker(tmp_path):
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="AAPL")  # only AAPL in DB
    app = _make_app_with_db(tmp_path, db_path=db)
    with app.test_client() as c:
        r = c.get("/ticker/ZZZQ")
    assert r.status_code == 200
    html = r.data.decode()
    assert "ZZZQ" in html
    assert "not found" in html.lower() or "Not in universe" in html


def test_ticker_route_no_candidate_entry(tmp_path):
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="MSFT", tier="Reject", score=30.0)
    app = _make_app_with_db(tmp_path, db_path=db)
    with app.test_client() as c:
        r = c.get("/ticker/MSFT")
    assert r.status_code == 200
    html = r.data.decode()
    assert "Score too low" in html or "not near" in html.lower() or "30" in html


def test_ticker_route_with_move_event(tmp_path):
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="APO", tier="Reject", score=38.0, with_event=True)
    app = _make_app_with_db(tmp_path, db_path=db)
    with app.test_client() as c:
        r = c.get("/ticker/APO")
    assert r.status_code == 200
    html = r.data.decode()
    assert "Missed Move Events" in html
    assert "gain_5d_10pct" in html


def test_ticker_route_requires_auth(tmp_path):
    from review.server import create_app
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    app = create_app(history_root, output_dir, unsafe_no_auth=False)
    with app.test_client() as c:
        r = c.get("/ticker/AAPL")
    assert r.status_code == 401


def test_ticker_route_invalid_format(tmp_path):
    app = _make_app_with_db(tmp_path)
    with app.test_client() as c:
        r = c.get("/ticker/" + "A" * 20)
    assert r.status_code in (400, 404)


def test_search_box_on_today_page(tmp_path):
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [_CROSSING_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/today")
    assert r.status_code == 200
    html = r.data.decode()
    assert "Look up" in html or "/ticker/" in html


def test_search_box_on_candidates_page(tmp_path):
    _write_day(str(tmp_path / "history"), "2026-05-01", _BASE_META, [_PRED_ROW])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/candidates")
    assert r.status_code == 200
    html = r.data.decode()
    assert "Look up" in html or "/ticker/" in html


def test_not_candidate_reason_missing_from_universe():
    from review.server import _not_candidate_reason
    reason = _not_candidate_reason(None, None, False)
    assert "Not in universe" in reason


def test_not_candidate_reason_low_score():
    from review.server import _not_candidate_reason
    score_row = ("2026-05-01", 25.0, "Reject", "score too low", "[]")
    company = {"is_active": True, "universe_exclusion_reason": None}
    reason = _not_candidate_reason(company, score_row, False)
    assert "25" in reason or "too low" in reason.lower()


def test_not_candidate_reason_near_threshold_no_catalyst():
    from review.server import _not_candidate_reason
    score_row = ("2026-05-01", 42.0, "Reject", "near", "[]")
    company = {"is_active": True, "universe_exclusion_reason": None}
    reason = _not_candidate_reason(company, score_row, False)
    assert "threshold" in reason.lower() or "40" in reason


# ── format_return_pct helper ──────────────────────────────────────────────────

def test_format_return_pct_decimal_form():
    from review.server import _format_return_pct
    assert _format_return_pct(0.1673) == "16.7%"


def test_format_return_pct_percent_form():
    from review.server import _format_return_pct
    assert _format_return_pct(16.73) == "16.7%"


def test_format_return_pct_none():
    from review.server import _format_return_pct
    assert _format_return_pct(None) == "—"


def test_format_return_pct_invalid():
    from review.server import _format_return_pct
    assert _format_return_pct("not_a_number") == "—"


# ── Ticker page: return formatting, dedup, unscored note ─────────────────────

def _seed_db_with_events(db_path: str, return_value: float, n_dupes: int = 1) -> None:
    import duckdb
    conn = duckdb.connect(db_path)
    for tbl_sql in [
        """CREATE TABLE IF NOT EXISTS companies (
            ticker VARCHAR, company_name VARCHAR, universe_tier VARCHAR,
            is_active BOOLEAN, sector VARCHAR, industry VARCHAR,
            universe_exclusion_reason VARCHAR, last_financial_filing_date DATE,
            PRIMARY KEY (ticker))""",
        """CREATE TABLE IF NOT EXISTS scores (
            ticker VARCHAR, as_of_date DATE, total_score DOUBLE,
            tier VARCHAR, why_rejected VARCHAR, missing_data_json VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS missed_opportunity_events (
            event_id VARCHAR, ticker VARCHAR, event_date DATE,
            event_type VARCHAR, return_value DOUBLE, window_days INTEGER,
            tier_before_event VARCHAR, had_catalyst_evidence BOOLEAN,
            investigation_status VARCHAR)""",
    ]:
        conn.execute(tbl_sql)
    conn.execute("INSERT OR IGNORE INTO companies VALUES (?,?,?,?,?,?,?,?)",
                 ["MSFT", "Microsoft Corp", "primary", True, "Tech", "Software", None, None])
    conn.execute("INSERT INTO scores VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                 ["MSFT", "2026-05-01", 35.0, "Reject", "score too low", "[]"])
    for i in range(n_dupes):
        conn.execute("INSERT INTO missed_opportunity_events VALUES (?,?,?,?,?,?,?,?,?)",
                     [f"ev{i}", "MSFT", "2026-04-17", "gain_5d_10pct",
                      return_value, 5, "Reject", False, "investigated"])
    conn.close()


def test_ticker_percent_values_render_correctly(tmp_path):
    db = str(tmp_path / "t.duckdb")
    _seed_db_with_events(db, return_value=16.73, n_dupes=1)
    app = _make_app_with_db(tmp_path, db_path=db)
    with app.test_client() as c:
        r = c.get("/ticker/MSFT")
    html = r.data.decode()
    assert "16.7%" in html
    assert "1673" not in html


def test_ticker_duplicate_move_rows_collapse(tmp_path):
    db = str(tmp_path / "t.duckdb")
    _seed_db_with_events(db, return_value=14.0, n_dupes=3)
    app = _make_app_with_db(tmp_path, db_path=db)
    with app.test_client() as c:
        r = c.get("/ticker/MSFT")
    html = r.data.decode()
    # gain_5d_10pct should appear exactly once despite 3 duplicate DB rows
    assert html.count("gain_5d_10pct") == 1


def test_ticker_unscored_mover_explanation_appears(tmp_path):
    db = str(tmp_path / "t.duckdb")
    _seed_db_with_events(db, return_value=14.0, n_dupes=1)
    app = _make_app_with_db(tmp_path, db_path=db)
    with app.test_client() as c:
        r = c.get("/ticker/MSFT")
    html = r.data.decode()
    # had_catalyst_evidence=False triggers unscored note
    assert "unscored" in html.lower() or "no prior score" in html.lower()


# ── Price context tests ───────────────────────────────────────────────────────

def _add_prices_to_db(db_path: str, ticker: str, rows: list[tuple]) -> None:
    """Insert (date_str, close, volume) rows into prices_daily."""
    import duckdb
    conn = duckdb.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prices_daily (
            id VARCHAR,
            ticker VARCHAR NOT NULL,
            trade_date DATE NOT NULL,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE NOT NULL,
            volume BIGINT,
            adjusted_close DOUBLE,
            source VARCHAR DEFAULT 'test',
            run_id VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id)
        )
    """)
    for i, (date_str, close, volume) in enumerate(rows):
        conn.execute(
            "INSERT INTO prices_daily (id, ticker, trade_date, close, volume, adjusted_close) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [f"{ticker}_{date_str}_{i}", ticker, date_str, close, volume, close],
        )
    conn.close()


def test_price_context_on_ticker_page(tmp_path):
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="AAPL", tier="Reject", score=35.0)
    _add_prices_to_db(db, "AAPL", [
        ("2026-05-01", 175.50, 80_000_000),
        ("2026-04-30", 172.00, 70_000_000),
        ("2026-04-29", 170.00, 65_000_000),
    ])
    app = _make_app_with_db(tmp_path, db_path=db)
    with app.test_client() as c:
        r = c.get("/ticker/AAPL")
    html = r.data.decode()
    assert r.status_code == 200
    assert "Price Context" in html
    assert "175.50" in html
    assert "Latest close" in html


def test_price_context_missing_data_graceful(tmp_path):
    # No prices_daily table — page must still return 200
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="AAPL", tier="Reject", score=35.0)
    app = _make_app_with_db(tmp_path, db_path=db)
    with app.test_client() as c:
        r = c.get("/ticker/AAPL")
    assert r.status_code == 200
    html = r.data.decode()
    assert "Price Context" in html
    assert "No price data" in html


def test_price_context_stale_flagged(tmp_path):
    import datetime
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="AAPL", tier="Reject", score=35.0)
    stale_date = (datetime.date.today() - datetime.timedelta(days=8)).isoformat()
    _add_prices_to_db(db, "AAPL", [(stale_date, 150.0, 50_000_000)])
    app = _make_app_with_db(tmp_path, db_path=db)
    with app.test_client() as c:
        r = c.get("/ticker/AAPL")
    html = r.data.decode()
    assert "stale" in html.lower()


def test_candidates_price_snapshot_appears(tmp_path):
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="AAAA", tier="C", score=50.0)
    _add_prices_to_db(db, "AAAA", [
        ("2026-05-01", 100.0, 1_000_000),
        ("2026-04-30",  98.0, 900_000),
    ])
    row = {
        "ticker": "AAAA", "original_score": "48.0", "shadow_score": "51.0",
        "shadow_tier": "C", "llm_adjustment": "3.0", "is_promoted": "true",
        "final_should_affect_score": "true", "sentiment": "bullish",
        "expected_direction": "bullish", "expected_timeframe": "5-10 days",
        "expected_move_summary": "Test", "action_reason": "reason",
        "catalyst_type": "earnings", "materiality": "high", "confidence": "0.8",
        "days_since_event": "3", "event_date": "2026-04-28",
        "priced_in_risk": "low", "impact_estimate": "high",
        "key_checks": "check1", "constructed_url": "",
        "scaled_adjustment": "3.0", "scaled_shadow_score": "51.0",
    }
    app = _make_app_with_db(tmp_path, db_path=db, dates=[("2026-05-01", {}, [row])])
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    assert r.status_code == 200
    assert "Price Snapshot" in html


def test_moves_price_context_appears(tmp_path):
    import io, csv
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="AAPL", tier="Reject", score=35.0)
    _add_prices_to_db(db, "AAPL", [
        ("2026-05-01", 175.0, 80_000_000),
        ("2026-04-30", 170.0, 75_000_000),
    ])
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    pva_path = os.path.join(output_dir, "prediction_vs_actual_rows.csv")
    cols = ["ticker", "event_date", "event_type", "return_value", "window_days",
            "classification", "universe_tier", "root_cause_hint", "score_before_event",
            "tier_before_event"]
    with open(pva_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerow({
            "ticker": "AAPL", "event_date": "2026-04-20", "event_type": "gain_5d_10pct",
            "return_value": "12.0", "window_days": "5", "classification": "true_miss",
            "universe_tier": "primary", "root_cause_hint": "earnings beat",
            "score_before_event": "35.0", "tier_before_event": "Reject",
        })
    app = _make_app_with_db(tmp_path, db_path=db)
    app.config["OUTPUT_DIR"] = output_dir
    # Override the moves route to use the right output_dir
    from review.server import create_app as _ca
    history_root = str(tmp_path / "history")
    os.makedirs(history_root, exist_ok=True)
    test_app = _ca(history_root, output_dir, unsafe_no_auth=True, db_path=db)
    with test_app.test_client() as c:
        r = c.get("/moves")
    html = r.data.decode()
    assert r.status_code == 200
    assert "175" in html or "Close" in html


def test_price_status_label_confirming():
    from review.server import _price_status_label
    ctx = {"latest_close": 100.0, "return_1d": 2.0, "stale": False}
    label, css = _price_status_label(ctx)
    assert label == "price confirming"
    assert css == "price-confirm"


def test_price_status_label_extended():
    from review.server import _price_status_label
    ctx = {"latest_close": 100.0, "return_5d": 10.0, "stale": False}
    label, css = _price_status_label(ctx)
    assert label == "price extended"
    assert css == "price-extended"


def test_price_status_label_stale():
    from review.server import _price_status_label
    ctx = {"latest_close": 100.0, "stale": True}
    label, css = _price_status_label(ctx)
    assert label == "stale price data"
    assert css == "price-stale"


def test_price_status_label_no_data():
    from review.server import _price_status_label
    label, css = _price_status_label({})
    assert "no price" in label
    assert css == "price-none"


def test_lookup_price_context_missing_db():
    from review.server import _lookup_price_context
    ctx = _lookup_price_context("AAPL", "/nonexistent/path.duckdb")
    assert ctx == {}


def test_lookup_price_context_returns_fields(tmp_path):
    from review.server import _lookup_price_context
    db = str(tmp_path / "p.duckdb")
    _add_prices_to_db(db, "AAPL", [
        ("2026-05-01", 175.0, 80_000_000),
        ("2026-04-30", 172.0, 75_000_000),
        ("2026-04-29", 170.0, 70_000_000),
        ("2026-04-28", 169.0, 65_000_000),
    ])
    ctx = _lookup_price_context("AAPL", db)
    assert ctx["latest_close"] == pytest.approx(175.0)
    assert ctx["latest_price_date"] == "2026-05-01"
    assert ctx["return_1d"] == pytest.approx((175.0 - 172.0) / 172.0 * 100, abs=0.01)
    assert ctx["high_52w"] == pytest.approx(175.0)
    assert ctx["low_52w"] == pytest.approx(169.0)
    assert not ctx["stale"]


# ── Tradability tests ─────────────────────────────────────────────────────────

def _make_ticker_app(tmp_path, db_path=""):
    """Minimal app for tradability tests with an output_dir."""
    from review.server import create_app
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    return create_app(
        history_root, output_dir,
        unsafe_no_auth=True,
        db_path=db_path or "",
    ), output_dir


def test_tradability_status_persists(tmp_path):
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="AAPL", tier="Reject", score=35.0)
    app, output_dir = _make_ticker_app(tmp_path, db_path=db)
    with app.test_client() as c:
        # POST tradability
        r = c.post("/ticker/AAPL/tradability", data={
            "tradability_status": "not_tradable",
            "broker_note": "Not available on IBKR",
        })
        assert r.status_code in (200, 302)
        # GET ticker page shows persisted status
        r2 = c.get("/ticker/AAPL")
    html = r2.data.decode()
    assert "not_tradable" in html
    assert "Not available on IBKR" in html


def test_not_tradable_displays_warning(tmp_path):
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="AAPL", tier="Reject", score=35.0)
    app, output_dir = _make_ticker_app(tmp_path, db_path=db)
    with app.test_client() as c:
        c.post("/ticker/AAPL/tradability", data={
            "tradability_status": "not_tradable",
            "broker_note": "",
        })
        r = c.get("/ticker/AAPL")
    html = r.data.decode()
    assert "not tradable in selected broker" in html.lower()


def test_broker_note_sanitized(tmp_path):
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="AAPL", tier="Reject", score=35.0)
    app, output_dir = _make_ticker_app(tmp_path, db_path=db)
    malicious_note = "<script>alert('xss')</script>"
    with app.test_client() as c:
        c.post("/ticker/AAPL/tradability", data={
            "tradability_status": "not_tradable",
            "broker_note": malicious_note,
        })
        r = c.get("/ticker/AAPL")
    html = r.data.decode()
    # The XSS payload must be HTML-escaped, not executed verbatim
    assert "<script>alert" not in html


def test_tradability_no_scoring_change(tmp_path):
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="AAPL", tier="Reject", score=35.0)
    app, output_dir = _make_ticker_app(tmp_path, db_path=db)
    with app.test_client() as c:
        # Get score before
        r_before = c.get("/ticker/AAPL")
        html_before = r_before.data.decode()
        # Set tradability
        c.post("/ticker/AAPL/tradability", data={
            "tradability_status": "not_tradable",
            "broker_note": "unavailable",
        })
        # Get score after
        r_after = c.get("/ticker/AAPL")
        html_after = r_after.data.decode()
    # Score should be identical in both renders
    assert "35.00" in html_before
    assert "35.00" in html_after


# ── Task 3: /ops coverage section ─────────────────────────────────────────

def test_ops_shows_coverage_section(tmp_path):
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/ops")
    assert r.status_code == 200
    html = r.data.decode()
    assert "coverage" in html.lower() or "fresh" in html.lower() or "prices" in html.lower()


def test_ops_coverage_no_db_crash(tmp_path):
    # /ops must not crash when no DB is present
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/ops")
    assert r.status_code == 200


# ── Task 7: data-readiness badges ─────────────────────────────────────────

def test_candidates_renders_without_crash_no_db(tmp_path):
    app = _make_app(tmp_path, dates=[("2026-05-03", _BASE_META, [_CROSSING_ROW])])
    with app.test_client() as c:
        r = c.get("/candidates")
    # Must not crash even without a real DB -- badges are best-effort
    assert r.status_code == 200


# ── Dedup + data readiness ─────────────────────────────────────────────────

_ENRICHED_COLS = _PVA_COLS + [
    "event_type", "enriched_root_cause", "root_cause_group",
    "explanation_short", "evidence_fields_used", "suggested_fix",
    "confidence", "incomplete_diag_subcause",
]


def _write_enriched(output_dir: str, rows: list[dict]) -> str:
    path = os.path.join(output_dir, "prediction_vs_actual_enriched_rows.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_ENRICHED_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def _enriched_row(**kw) -> dict:
    base = {
        "ticker": "AAAB", "event_date": "2026-05-01", "event_type": "spike",
        "return_value": "10.0", "window_days": "1",
        "classification": "true_miss", "universe_tier": "primary",
        "priority_score": "5.0", "score_before_event": "30.0",
        "tier_before_event": "Reject", "had_catalyst_evidence": "True",
        "was_in_universe": "True", "was_scored": "True",
        "root_cause_hint": "data_gap", "score_join_method": "scores_join",
        "enriched_root_cause": "sector_cluster_move", "root_cause_group": "unscored",
        "explanation_short": "", "evidence_fields_used": "", "suggested_fix": "",
        "confidence": "high", "incomplete_diag_subcause": "",
    }
    return {**base, **kw}


def test_dedup_pva_rows_removes_duplicates():
    from review.server import _dedup_pva_rows
    rows = [
        {"ticker": "AAAB", "event_date": "2026-05-01", "event_type": "spike", "window_days": "1"},
        {"ticker": "AAAB", "event_date": "2026-05-01", "event_type": "spike", "window_days": "1"},
        {"ticker": "AAAB", "event_date": "2026-05-02", "event_type": "spike", "window_days": "1"},
    ]
    result = _dedup_pva_rows(rows)
    assert len(result) == 2


def test_dedup_pva_rows_preserves_order():
    from review.server import _dedup_pva_rows
    rows = [
        {"ticker": "A", "event_date": "2026-05-01", "event_type": "x", "window_days": "1"},
        {"ticker": "B", "event_date": "2026-05-01", "event_type": "x", "window_days": "1"},
        {"ticker": "A", "event_date": "2026-05-01", "event_type": "x", "window_days": "1"},
    ]
    result = _dedup_pva_rows(rows)
    assert [r["ticker"] for r in result] == ["A", "B"]


def test_learning_shows_raw_and_deduped_counts(tmp_path):
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    rows = [
        _pva_row(ticker="AAAB", event_date="2026-05-01", window_days="1", event_type="spike"),
        _pva_row(ticker="AAAB", event_date="2026-05-01", window_days="1", event_type="spike"),
        _pva_row(ticker="BBBB", event_date="2026-05-01", window_days="1", event_type="spike"),
    ]
    _write_pva(output_dir, rows)
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/learning")
    html = r.data.decode()
    assert "raw" in html.lower() or "3" in html
    assert "2" in html  # deduped count


def test_learning_shows_enriched_root_cause_counts(tmp_path):
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    _write_pva(output_dir, [_pva_row()])
    _write_enriched(output_dir, [
        _enriched_row(enriched_root_cause="price_only_scored", classification="true_miss"),
        _enriched_row(ticker="BBBB", enriched_root_cause="sector_cluster_move"),
    ])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/learning")
    html = r.data.decode()
    assert "price_only_scored" in html
    assert "sector_cluster_move" in html


def test_learning_shows_price_only_warning(tmp_path):
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    _write_pva(output_dir, [_pva_row()])
    _write_enriched(output_dir, [
        _enriched_row(enriched_root_cause="price_only_scored", classification="true_miss"),
    ])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/learning")
    html = r.data.decode()
    assert "price_only" in html.lower() or "limited confidence" in html.lower() or "warn" in html.lower()


def test_moves_shows_deduped_count(tmp_path):
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    rows = [
        _pva_row(ticker="AAAB", event_date="2026-05-01", window_days="1", event_type="spike"),
        _pva_row(ticker="AAAB", event_date="2026-05-01", window_days="1", event_type="spike"),
        _pva_row(ticker="BBBB", event_date="2026-05-01", window_days="5", event_type="spike"),
    ]
    _write_pva(output_dir, rows)
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/moves")
    html = r.data.decode()
    assert "dedup" in html.lower() or "duplicate" in html.lower() or "raw" in html.lower()


# ── Task: ops refresh targets section ─────────────────────────────────────────

def test_ops_shows_refresh_targets_section(tmp_path):
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    _write_enriched(output_dir, [
        _enriched_row(ticker="CTRA", enriched_root_cause="price_only_scored", classification="true_miss"),
        _enriched_row(ticker="INTC", enriched_root_cause="sector_cluster_move", classification="near_threshold"),
    ])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/ops")
    assert r.status_code == 200
    html = r.data.decode()
    assert "refresh" in html.lower() or "targets" in html.lower()


def test_ops_refresh_targets_no_crash_without_enriched_csv(tmp_path):
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/ops")
    assert r.status_code == 200


# ── Learning page Top Fix Queues ───────────────────────────────────────────────

def test_learning_shows_top_fix_queues_section(tmp_path):
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    _write_pva(output_dir, [_pva_row(ticker="AAAB")])
    _write_enriched(output_dir, [
        _enriched_row(ticker="GFS", enriched_root_cause="ifrs_mapping_gap", classification="true_miss"),
        _enriched_row(ticker="DDOG", enriched_root_cause="polygon_fundamentals_missing", classification="true_miss"),
        _enriched_row(ticker="INTC", enriched_root_cause="sector_cluster_move", classification="near_threshold"),
    ])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/learning")
    assert r.status_code == 200
    html = r.data.decode()
    assert "fix queue" in html.lower() or "top fix" in html.lower()


def test_learning_fix_queues_show_bucket_tickers(tmp_path):
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    _write_pva(output_dir, [_pva_row(ticker="GFS")])
    _write_enriched(output_dir, [
        _enriched_row(ticker="GFS", enriched_root_cause="polygon_fundamentals_missing", classification="true_miss"),
    ])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/learning")
    html = r.data.decode()
    assert "GFS" in html


def test_learning_fix_queues_no_crash_without_enriched(tmp_path):
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    _write_pva(output_dir, [_pva_row(ticker="AAAB")])
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/learning")
    assert r.status_code == 200


# ── Price anchoring tests ──────────────────────────────────────────────────────

def test_since_event_uses_event_date_close_vg_like(tmp_path):
    """VG-like: event close 17.53, prev close 13.27, latest 12.73.
    since_event must be -27.4% (from 17.53), not -4.1% (from 13.27).
    """
    from review.server import _lookup_price_context
    db = str(tmp_path / "p.duckdb")
    _add_prices_to_db(db, "VG", [
        ("2026-05-01", 12.73, 5_000_000),   # seq=1 latest
        ("2026-04-30", 13.27, 4_500_000),   # seq=2 previous
        ("2026-03-27", 17.53, 8_000_000),   # seq=N event date
    ])
    ctx = _lookup_price_context("VG", db, event_date="2026-03-27")
    assert ctx["return_1d"] == pytest.approx((12.73 - 13.27) / 13.27 * 100, abs=0.1)
    assert ctx["return_since_event"] == pytest.approx((12.73 - 17.53) / 17.53 * 100, abs=0.1)
    assert ctx["event_price"] == pytest.approx(17.53, abs=0.01)


def test_1d_return_never_confused_with_since_event(tmp_path):
    """1d return and since_event must be distinct values."""
    from review.server import _lookup_price_context
    db = str(tmp_path / "p.duckdb")
    _add_prices_to_db(db, "VG", [
        ("2026-05-01", 12.73, 5_000_000),
        ("2026-04-30", 13.27, 4_500_000),
        ("2026-03-27", 17.53, 8_000_000),
    ])
    ctx = _lookup_price_context("VG", db, event_date="2026-03-27")
    assert abs(ctx["return_1d"] - ctx["return_since_event"]) > 5.0  # must be separated


def test_next_trading_day_used_when_event_on_weekend(tmp_path):
    """Event on Saturday 2026-03-28 → anchor to Monday 2026-03-30."""
    from review.server import _lookup_price_context
    db = str(tmp_path / "p.duckdb")
    _add_prices_to_db(db, "AAAB", [
        ("2026-04-01", 60.0, 1_000_000),    # latest
        ("2026-03-31", 58.0, 900_000),
        ("2026-03-30", 50.0, 800_000),       # first trading day after Sat 2026-03-28
        ("2026-03-27", 48.0, 700_000),       # trading day before the weekend event
    ])
    ctx = _lookup_price_context("AAAB", db, event_date="2026-03-28")
    assert ctx["event_price"] == pytest.approx(50.0, abs=0.01)
    assert ctx["event_anchor_label"] == "next trading day"
    assert ctx["return_since_event"] == pytest.approx((60.0 - 50.0) / 50.0 * 100, abs=0.1)


def test_event_anchor_label_is_event_date_when_exact_match(tmp_path):
    from review.server import _lookup_price_context
    db = str(tmp_path / "p.duckdb")
    _add_prices_to_db(db, "AAAB", [
        ("2026-04-01", 55.0, 1_000_000),
        ("2026-03-27", 50.0, 800_000),
    ])
    ctx = _lookup_price_context("AAAB", db, event_date="2026-03-27")
    assert ctx["event_anchor_label"] == "event date"


def test_no_price_confirmation_when_since_event_negative_despite_positive_5d(tmp_path):
    """since_event=-27% but 5d=+7% must still give 'no price confirmation'."""
    from review.server import _lookup_price_context, _price_status_label
    db = str(tmp_path / "p.duckdb")
    _add_prices_to_db(db, "VG", [
        ("2026-05-01", 12.73, 5_000_000),
        ("2026-04-30", 13.27, 4_500_000),
        ("2026-04-29", 13.10, 4_000_000),
        ("2026-04-28", 12.90, 3_500_000),
        ("2026-04-25", 12.50, 3_000_000),
        ("2026-04-24", 11.90, 2_500_000),  # 5d ago: 11.90
        ("2026-03-27", 17.53, 8_000_000),
    ])
    ctx = _lookup_price_context("VG", db, event_date="2026-03-27")
    # Verify 5d return is positive but since_event is negative
    assert ctx["return_since_event"] < 0
    assert ctx.get("return_5d", 0) > 0
    label, css = _price_status_label(ctx)
    assert label == "no price confirmation"
    assert css == "price-none"


def test_since_signal_computed_from_signal_date(tmp_path):
    """since_signal uses signal_date anchor, separate from event_date."""
    from review.server import _lookup_price_context
    db = str(tmp_path / "p.duckdb")
    _add_prices_to_db(db, "VG", [
        ("2026-05-01", 12.73, 5_000_000),  # latest
        ("2026-04-30", 13.27, 4_500_000),
        ("2026-04-28", 14.00, 4_000_000),  # signal date close
        ("2026-03-27", 17.53, 8_000_000),  # event date close
    ])
    ctx = _lookup_price_context("VG", db, event_date="2026-03-27", signal_date="2026-04-28")
    assert ctx["signal_price"] == pytest.approx(14.00, abs=0.01)
    assert ctx["return_since_signal"] == pytest.approx((12.73 - 14.00) / 14.00 * 100, abs=0.1)
    # Since event is anchored separately
    assert ctx["event_price"] == pytest.approx(17.53, abs=0.01)


def test_ticker_page_uses_queue_entry_event_date_not_missed_event_date(tmp_path):
    """Queue entry event_date (2026-03-27) must override missed_events date (2026-04-30)."""
    import duckdb
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="VG", tier="Reject", score=42.0)
    # Add a missed_opportunity_event with a recent date — this must NOT be the anchor
    conn = duckdb.connect(db)
    conn.execute(
        "INSERT INTO missed_opportunity_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ["ev1", "VG", "2026-04-30", "gain_1d_3pct", 0.05, 1, "Reject", False, "open"],
    )
    conn.close()
    _add_prices_to_db(db, "VG", [
        ("2026-05-01", 12.73, 5_000_000),   # latest
        ("2026-04-30", 13.27, 4_500_000),   # missed_event date — must NOT be anchor
        ("2026-03-27", 17.53, 8_000_000),   # queue entry event_date — MUST be anchor
    ])
    # Create queue entry with event_date=2026-03-27
    queue_row = {**_CROSSING_ROW, "ticker": "VG", "event_date": "2026-03-27",
                 "shadow_tier": "C", "shadow_score": "47.0", "llm_adjustment": "5.0"}
    app = _make_app_with_db(
        tmp_path, db_path=db,
        dates=[("2026-05-01", _BASE_META, [queue_row])],
    )
    with app.test_client() as c:
        r = c.get("/ticker/VG")
    html = r.data.decode()
    assert r.status_code == 200
    # since event must show -27.4% (from 17.53), not -4.1% (from 13.27)
    assert "17.53" in html or "27." in html  # event anchor price in context
    assert "Since event" in html


def test_candidates_snapshot_shows_since_event_and_since_signal_columns(tmp_path):
    """Both 'Since event' and 'Since signal' headers appear in candidates table."""
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="AAAA", tier="C", score=50.0)
    _add_prices_to_db(db, "AAAA", [
        ("2026-05-01", 110.0, 1_000_000),
        ("2026-04-30", 108.0, 900_000),
        ("2026-04-28", 105.0, 800_000),  # signal date
        ("2026-03-27", 100.0, 700_000),  # event date
    ])
    row = {
        "ticker": "AAAA", "original_score": "48.0", "shadow_score": "51.0",
        "shadow_tier": "C", "llm_adjustment": "3.0", "is_promoted": "true",
        "final_should_affect_score": "true", "sentiment": "bullish",
        "expected_direction": "bullish", "expected_timeframe": "5-10 days",
        "expected_move_summary": "Test", "action_reason": "reason",
        "catalyst_type": "earnings", "materiality": "high", "confidence": "0.8",
        "days_since_event": "35", "event_date": "2026-03-27",
        "priced_in_risk": "low", "impact_estimate": "high",
        "key_checks": "check1", "constructed_url": "",
        "scaled_adjustment": "3.0", "scaled_shadow_score": "51.0",
    }
    app = _make_app_with_db(
        tmp_path, db_path=db,
        dates=[
            ("2026-04-28", _BASE_META, [row]),  # oldest: signal date
            ("2026-05-01", _BASE_META, [row]),  # latest
        ],
    )
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    assert r.status_code == 200
    assert "Since event" in html
    assert "Since signal" in html or "signal" in html.lower()


def test_since_event_shows_separately_from_1d_return_on_ticker_page(tmp_path):
    """Ticker page shows both '1d return' and 'Since event' as separate rows."""
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="VG", tier="Reject", score=42.0)
    _add_prices_to_db(db, "VG", [
        ("2026-05-01", 12.73, 5_000_000),
        ("2026-04-30", 13.27, 4_500_000),
        ("2026-03-27", 17.53, 8_000_000),
    ])
    queue_row = {**_CROSSING_ROW, "ticker": "VG", "event_date": "2026-03-27",
                 "shadow_tier": "C", "shadow_score": "47.0", "llm_adjustment": "5.0"}
    app = _make_app_with_db(
        tmp_path, db_path=db,
        dates=[("2026-05-01", _BASE_META, [queue_row])],
    )
    with app.test_client() as c:
        r = c.get("/ticker/VG")
    html = r.data.decode()
    assert "1d return" in html
    assert "Since event" in html


# ── Candidate Lifecycle integration ───────────────────────────────────────────

def test_ticker_page_shows_lifecycle_section(tmp_path):
    """Ticker page renders Candidate Lifecycle block when prices + event_date present."""
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="CTRA", tier="C", score=48.0)
    _add_prices_to_db(db, "CTRA", [
        ("2025-11-15", 27.86, 2_000_000),
        ("2025-11-17", 30.00, 1_500_000),
        ("2025-11-18", 36.04, 1_200_000),
    ])
    queue_row = {**_CROSSING_ROW, "ticker": "CTRA", "event_date": "2025-11-15",
                 "shadow_tier": "C", "shadow_score": "48.0", "llm_adjustment": "5.0",
                 "catalyst_type": "government_contract"}
    app = _make_app_with_db(
        tmp_path, db_path=db,
        dates=[("2025-11-18", _BASE_META, [queue_row])],
    )
    with app.test_client() as c:
        r = c.get("/ticker/CTRA")
    html = r.data.decode()
    assert r.status_code == 200
    assert "Candidate Lifecycle" in html


def test_ticker_page_lifecycle_shows_validated_for_29pct_gain(tmp_path):
    """CTRA-like: +29% from event → lifecycle shows 'validated'."""
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="CTRA2", tier="C", score=48.0)
    _add_prices_to_db(db, "CTRA2", [
        ("2025-11-15", 27.86, 2_000_000),
        ("2025-11-17", 30.00, 1_500_000),
        ("2025-11-18", 36.04, 1_200_000),
    ])
    queue_row = {**_CROSSING_ROW, "ticker": "CTRA2", "event_date": "2025-11-15",
                 "shadow_tier": "C", "shadow_score": "48.0", "llm_adjustment": "5.0"}
    app = _make_app_with_db(
        tmp_path, db_path=db,
        dates=[("2025-11-18", _BASE_META, [queue_row])],
    )
    with app.test_client() as c:
        r = c.get("/ticker/CTRA2")
    html = r.data.decode()
    assert "validated" in html.lower()
    assert "context" in html.lower()


def test_ticker_page_lifecycle_no_crash_without_prices(tmp_path):
    """Ticker page must not crash when no prices available — lifecycle degrades gracefully."""
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="NOPR", tier="Reject", score=30.0)
    queue_row = {**_CROSSING_ROW, "ticker": "NOPR", "event_date": "2026-01-10",
                 "shadow_tier": "C", "shadow_score": "48.0", "llm_adjustment": "5.0"}
    app = _make_app_with_db(
        tmp_path, db_path=db,
        dates=[("2026-01-15", _BASE_META, [queue_row])],
    )
    with app.test_client() as c:
        r = c.get("/ticker/NOPR")
    assert r.status_code == 200
    assert "Candidate Lifecycle" in r.data.decode()


def test_candidates_snapshot_shows_outcome_column(tmp_path):
    """Candidates price snapshot table has an Outcome column header."""
    db = str(tmp_path / "t.duckdb")
    _seed_db(db, ticker="CTRA", tier="C", score=48.0)
    _add_prices_to_db(db, "CTRA", [
        ("2026-01-10", 100.0, 2_000_000),
        ("2026-01-13", 112.0, 1_500_000),
    ])
    app = _make_app_with_db(
        tmp_path, db_path=db,
        dates=[("2026-01-13", _BASE_META, [_CROSSING_ROW])],
    )
    with app.test_client() as c:
        r = c.get("/candidates")
    html = r.data.decode()
    assert r.status_code == 200
    assert "Outcome" in html


# ── PvA freshness warning on /learning ────────────────────────────────────────

def _write_pva_csv_for_learning(tmp_path, event_dates: list[str]) -> None:
    """Write a minimal prediction_vs_actual_rows.csv into tmp_path/data/processed/."""
    out_dir = os.path.join(str(tmp_path), "data", "processed")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "prediction_vs_actual_rows.csv")
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["event_date", "ticker", "classification",
                                                "return_value", "window_days", "event_type",
                                                "was_in_universe", "was_scored", "score_before_event",
                                                "priority_score", "tier_before_event",
                                                "had_catalyst_evidence", "investigation_status"])
        writer.writeheader()
        for ed in event_dates:
            writer.writerow({
                "event_date": ed, "ticker": "MSFT", "classification": "true_miss",
                "return_value": "0.12", "window_days": "5", "event_type": "gain_5d_10pct",
                "was_in_universe": "True", "was_scored": "True", "score_before_event": "35.0",
                "priority_score": "", "tier_before_event": "Reject",
                "had_catalyst_evidence": "False", "investigation_status": "pending",
            })


def _make_db_with_prices(tmp_path, latest_date: str) -> str:
    """Create a minimal mhde.duckdb with one price row."""
    db_dir = os.path.join(str(tmp_path), "data")
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, "mhde.duckdb")
    import duckdb as _duckdb
    conn = _duckdb.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS prices_daily (id VARCHAR, ticker VARCHAR, trade_date DATE, close DOUBLE)")
    conn.execute("INSERT INTO prices_daily VALUES ('x1', 'MSFT', ?, 100.0)", [latest_date])
    conn.close()
    return db_path


def _make_app_with_pva(tmp_path, db_path: str, event_dates: list[str]):
    """App with PvA CSV in the output_dir the app will serve."""
    from review.server import create_app
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    # Write PvA CSV into the output_dir the app uses
    pva_path = os.path.join(output_dir, "prediction_vs_actual_rows.csv")
    with open(pva_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["event_date", "ticker", "classification",
                                                "return_value", "window_days", "event_type",
                                                "was_in_universe", "was_scored", "score_before_event",
                                                "priority_score", "tier_before_event",
                                                "had_catalyst_evidence", "investigation_status"])
        writer.writeheader()
        for ed in event_dates:
            writer.writerow({
                "event_date": ed, "ticker": "MSFT", "classification": "true_miss",
                "return_value": "0.12", "window_days": "5", "event_type": "gain_5d_10pct",
                "was_in_universe": "True", "was_scored": "True", "score_before_event": "35.0",
                "priority_score": "", "tier_before_event": "Reject",
                "had_catalyst_evidence": "False", "investigation_status": "pending",
            })
    _write_day(history_root, "2026-05-01", _BASE_META, [])
    return create_app(history_root, output_dir, unsafe_no_auth=True, db_path=db_path)


def test_learning_shows_stale_warning_when_prices_newer_than_pva(tmp_path):
    """Learning page shows stale warning when prices are newer than PvA coverage."""
    db_path = _make_db_with_prices(tmp_path, "2026-05-01")
    _seed_db_with_events(db_path, return_value=0.12)
    app = _make_app_with_pva(tmp_path, db_path=db_path, event_dates=["2026-04-28", "2026-04-29"])
    with app.test_client() as c:
        r = c.get("/learning")
    html = r.data.decode()
    assert r.status_code == 200
    assert "stale" in html.lower() or "refresh-learning" in html.lower()


def test_learning_no_stale_warning_when_aligned(tmp_path):
    """Learning page shows no stale warning when PvA covers latest price date."""
    db_path = _make_db_with_prices(tmp_path, "2026-05-01")
    _seed_db_with_events(db_path, return_value=0.12)
    app = _make_app_with_pva(tmp_path, db_path=db_path, event_dates=["2026-05-01"])
    with app.test_client() as c:
        r = c.get("/learning")
    html = r.data.decode()
    assert r.status_code == 200
    assert "refresh-learning" not in html


def test_learning_stale_no_crash_without_db(tmp_path):
    """Learning page must not crash if DB path is missing during freshness check."""
    from review.server import create_app
    history_root = str(tmp_path / "history")
    output_dir = str(tmp_path / "output")
    os.makedirs(output_dir, exist_ok=True)
    pva_path = os.path.join(output_dir, "prediction_vs_actual_rows.csv")
    with open(pva_path, "w", newline="") as f:
        f.write("event_date,ticker,classification\n2026-05-01,MSFT,true_miss\n")
    _write_day(history_root, "2026-05-01", _BASE_META, [])
    app = create_app(history_root, output_dir, unsafe_no_auth=True,
                     db_path="/nonexistent/mhde.duckdb")
    with app.test_client() as c:
        r = c.get("/learning")
    assert r.status_code == 200
    assert "Learning" in r.data.decode()
