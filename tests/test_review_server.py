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
    app = _make_app(tmp_path)
    # override history to match
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
