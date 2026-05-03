"""Flask read-only review server for the daily catalyst queue."""
from __future__ import annotations

import csv
import hmac
import json
import logging
import os
import struct
import zlib
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, Response, current_app, redirect, render_template_string, request, url_for
from markupsafe import Markup

logger = logging.getLogger("mhde.review.server")

# ── Auth helpers ──────────────────────────────────────────────────────────────

def _username() -> str:
    return os.environ.get("REVIEW_UI_USERNAME", "")


def _password() -> str:
    return os.environ.get("REVIEW_UI_PASSWORD", "")


def _require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_app.config.get("UNSAFE_NO_AUTH"):
            return f(*args, **kwargs)
        auth = request.authorization
        ok = (
            auth is not None
            and hmac.compare_digest(auth.username, _username())
            and hmac.compare_digest(auth.password, _password())
            and _username()
            and _password()
        )
        if not ok:
            return Response(
                "Unauthorized",
                401,
                {"WWW-Authenticate": 'Basic realm="MHDE Catalyst Review"'},
            )
        return f(*args, **kwargs)
    return decorated


# ── History helpers ───────────────────────────────────────────────────────────

def _dated_dirs(history_root: str) -> list[str]:
    """Return sorted list of YYYY-MM-DD directory names (descending)."""
    if not os.path.isdir(history_root):
        return []
    dirs = []
    for name in os.listdir(history_root):
        if len(name) == 10 and name[4] == "-" and name[7] == "-":
            path = os.path.join(history_root, name)
            if os.path.isdir(path):
                dirs.append(name)
    return sorted(dirs, reverse=True)


def _read_metadata(history_root: str, date_str: str) -> dict:
    path = os.path.join(history_root, date_str, "run_metadata.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def _read_csv_entries(history_root: str, date_str: str) -> list[dict]:
    path = os.path.join(history_root, date_str, "daily_catalyst_queue.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _read_reviews(history_root: str, date_str: str) -> dict[str, dict]:
    """Return {ticker: review_row} from manual_review.csv for a given run date."""
    path = os.path.join(history_root, date_str, "manual_review.csv")
    if not os.path.exists(path):
        return {}
    with open(path, newline="") as f:
        return {r["ticker"]: r for r in csv.DictReader(f)}


def _write_review(
    history_root: str,
    date_str: str,
    ticker: str,
    decision: str,
    notes: str,
) -> None:
    """Upsert a review row to manual_review.csv (creates file if missing)."""
    from missed.catalyst_history import MANUAL_REVIEW_COLS
    path = os.path.join(history_root, date_str, "manual_review.csv")
    existing = _read_reviews(history_root, date_str)
    existing[ticker] = {
        "ticker": ticker,
        "run_date": date_str,
        "analyst_decision": decision,
        "analyst_notes": notes,
        "reviewed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MANUAL_REVIEW_COLS)
        writer.writeheader()
        writer.writerows(existing.values())


def _is_promoted(row: dict) -> bool:
    v = row.get("final_should_affect_score", "")
    return str(v).lower() in ("true", "1", "yes")


def _is_crossing(row: dict) -> bool:
    return _is_promoted(row) and "→C" in str(row.get("tier_move", ""))


def _is_weak(row: dict) -> bool:
    return row.get("validation_status", "") in (
        "weak_evidence", "invalid_quote", "neutral_sentiment"
    )


# ── PWA assets ────────────────────────────────────────────────────────────────

def _make_minimal_png(width: int, height: int, rgb: tuple) -> bytes:
    """Generate a minimal solid-color PNG without external dependencies."""
    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    r, g, b = rgb
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    row = b"\x00" + bytes([r, g, b]) * width
    idat = _chunk(b"IDAT", zlib.compress(row * height))
    iend = _chunk(b"IEND", b"")
    return b"\x89PNG\r\n\x1a\n" + ihdr + idat + iend


_ICON_COLOR = (17, 24, 39)   # #111827 — dark navy
_ICON_192 = _make_minimal_png(192, 192, _ICON_COLOR)
_ICON_512 = _make_minimal_png(512, 512, _ICON_COLOR)

_MANIFEST_JSON = json.dumps({
    "name": "MHDE Catalyst Queue",
    "short_name": "MHDE",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#111827",
    "theme_color": "#111827",
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}, indent=2)

_SW_JS = """\
const CACHE = 'mhde-shell-v1';
const SHELL = [
  '/manifest.webmanifest',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(ks =>
      Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // Shell assets only — never cache authenticated queue data
  if (SHELL.includes(url.pathname)) {
    e.respondWith(
      caches.match(e.request).then(cached => cached || fetch(e.request))
    );
    return;
  }
  // Network-first for all routes (/ /runs /runs/* etc.)
  e.respondWith(fetch(e.request));
});
"""

_SW_REGISTER = """\
<script>
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/service-worker.js', {scope: '/'})
    .catch(function(e) { console.warn('SW registration failed', e); });
}
</script>"""

# ── HTML templates ────────────────────────────────────────────────────────────

_VALID_DECISIONS = {"accept", "reject", "watch", "unknown"}

_ARTIFACT_FILES = {
    "html": "daily_catalyst_queue.html",
    "md":   "daily_catalyst_queue.md",
    "csv":  "daily_catalyst_queue.csv",
    "jsonl": "daily_catalyst_queue.jsonl",
}
_ARTIFACT_MIME = {
    "html": "text/html; charset=utf-8",
    "md":   "text/plain; charset=utf-8",
    "csv":  "text/csv; charset=utf-8",
    "jsonl": "text/plain; charset=utf-8",
}

_LEARNING_ARTIFACT_FILES = {
    "report_md":     "prediction_vs_actual_report.md",
    "rows_csv":      "prediction_vs_actual_rows.csv",
    "enriched_csv":  "prediction_vs_actual_enriched_rows.csv",
    "root_cause_md": "root_cause_enrichment_report.md",
}
_LEARNING_ARTIFACT_MIME = {
    "report_md":     "text/plain; charset=utf-8",
    "rows_csv":      "text/csv; charset=utf-8",
    "enriched_csv":  "text/csv; charset=utf-8",
    "root_cause_md": "text/plain; charset=utf-8",
}


def _is_valid_date(s: str) -> bool:
    """Return True iff s is exactly YYYY-MM-DD with digit/dash only."""
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        return False
    return all(c.isdigit() for c in (s[:4] + s[5:7] + s[8:]))

_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  max-width:720px;margin:0 auto;padding:16px;color:#222;}
h1{font-size:1.3rem;margin-bottom:4px;}
h2{font-size:1rem;border-bottom:1px solid #ddd;padding-bottom:4px;margin-top:20px;}
.banner{background:#fff3e0;border-left:4px solid #ff9800;padding:8px 12px;
  margin:12px 0;font-size:.9rem;}
.banner.shadow{border-color:#1565c0;background:#e3f2fd;}
table{border-collapse:collapse;width:100%;font-size:.85rem;margin:8px 0;}
th,td{padding:5px 8px;border-bottom:1px solid #eee;text-align:left;}
th{background:#f5f5f5;}
tr.cross{background:#e8f5e9;}
tr.bear{background:#fff3e0;}
a{color:#1565c0;}
.muted{color:#888;font-size:.8rem;}
details summary{cursor:pointer;color:#555;}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.8rem;font-weight:600;}
.badge-accept{background:#c8e6c9;color:#1b5e20;}
.badge-watch{background:#fff9c4;color:#f57f17;}
.badge-reject{background:#ffcdd2;color:#b71c1c;}
.badge-unknown{background:#e0e0e0;color:#444;}
.review-form{margin-top:6px;font-size:.85rem;}
.review-form select,.review-form input[type=text]{padding:3px 6px;border:1px solid #ccc;border-radius:3px;}
.review-form button{padding:3px 10px;background:#1565c0;color:#fff;border:none;border-radius:3px;cursor:pointer;}
.stat-grid{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}
.stat-card{background:#f8f9fa;border:1px solid #e0e0e0;border-radius:8px;padding:12px 18px;min-width:140px}
.stat-label{display:block;font-size:0.75rem;color:#666;margin-bottom:4px}
.stat-val{font-size:1.4rem;font-weight:700;color:#1565c0}
.warn-box{background:#fff3e0;border-left:4px solid #ff9800;padding:10px 14px;margin:10px 0}
"""

_BASE_TMPL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#111827">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="MHDE">
<title>{{ title }}</title>
<style>{{ css }}</style>
</head>
<body>
<h1>MHDE Catalyst Review</h1>
<div class="banner shadow">&#128274; Shadow-only — production scores are <strong>not</strong> changed by this tool.</div>
{{ body }}
<hr>
<p class="muted">MHDE &mdash; read-only review server</p>
{{ sw_register }}
</body>
</html>"""


def _render(title: str, body: str) -> str:
    # Mark body, css, and sw_register as safe — they are server-generated HTML;
    # user-provided data is already escaped by _esc() before insertion.
    return render_template_string(
        _BASE_TMPL,
        title=title,
        css=Markup(_CSS),
        body=Markup(body),
        sw_register=Markup(_SW_REGISTER),
    )


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v or 0)
    except (ValueError, TypeError):
        return default


# ── Route handlers ────────────────────────────────────────────────────────────

def _homepage(history_root: str, output_dir: str) -> str:
    dates = _dated_dirs(history_root)
    if not dates:
        body = """
<p>No catalyst queue runs archived yet.</p>
<p class="muted">Run: <code>python main.py missed daily-catalyst-queue --history-root data/processed/catalyst_queue_history</code></p>
"""
        return _render("MHDE Catalyst Review", body)

    latest = dates[0]
    meta = _read_metadata(history_root, latest)
    entries = _read_csv_entries(history_root, latest)
    crossings = sum(1 for e in entries if _is_crossing(e))
    promoted = sum(1 for e in entries if _is_promoted(e))

    artifact_links = (
        '<p class="muted">Download latest: '
        '<a href="/artifacts/latest/html">HTML</a> &bull; '
        '<a href="/artifacts/latest/md">Markdown</a> &bull; '
        '<a href="/artifacts/latest/csv">CSV</a> &bull; '
        '<a href="/artifacts/latest/jsonl">JSONL</a>'
        '</p>'
    )

    reviews = _read_reviews(history_root, latest)
    by_decision: dict[str, int] = {}
    for r in reviews.values():
        d = r.get("analyst_decision", "unknown")
        by_decision[d] = by_decision.get(d, 0) + 1

    review_summary = ""
    if by_decision:
        parts = ", ".join(
            f'<span class="badge badge-{_esc(d)}">{_esc(d)}: {c}</span>'
            for d, c in sorted(by_decision.items())
        )
        review_summary = f"<p>Reviews: {parts}</p>"

    rows = "".join(
        f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>"
        for k, v in [
            ("Run date", latest),
            ("Sampled", meta.get("sampled", "—")),
            ("Source available", meta.get("source_available", "—")),
            ("Valid actionable", meta.get("valid_actionable", promoted)),
            ("Reject→C crossings", crossings),
            ("Provider", meta.get("provider", "—")),
        ]
    )

    body = f"""
<h2>Latest Run — {_esc(latest)}</h2>
<table><tr><th>Field</th><th>Value</th></tr>{rows}</table>
{review_summary}
{artifact_links}
<p>
  <a href="/runs">All historical runs</a>
  &nbsp;|&nbsp;
  <a href="/runs/{_esc(latest)}">View this run</a>
</p>
"""
    return _render("MHDE Catalyst Review", body)


def _runs_list(history_root: str) -> str:
    dates = _dated_dirs(history_root)
    if not dates:
        body = "<p>No runs found.</p>"
        return _render("All Runs — MHDE", body)

    rows = []
    for d in dates:
        meta = _read_metadata(history_root, d)
        entries = _read_csv_entries(history_root, d)
        crossings = sum(1 for e in entries if _is_crossing(e))
        promoted = sum(1 for e in entries if _is_promoted(e))
        rows.append(
            f'<tr><td><a href="/runs/{_esc(d)}">{_esc(d)}</a></td>'
            f"<td>{meta.get('sampled', '—')}</td>"
            f"<td>{promoted}</td>"
            f"<td>{crossings}</td></tr>"
        )

    body = f"""
<h2>Historical Runs</h2>
<table>
<tr><th>Date</th><th>Sampled</th><th>Promoted</th><th>Reject→C</th></tr>
{''.join(rows)}
</table>
<p><a href="/">← Home</a></p>
"""
    return _render("All Runs — MHDE", body)


def _run_detail(history_root: str, date_str: str) -> tuple[str, int]:
    meta = _read_metadata(history_root, date_str)
    if not meta and not os.path.isdir(os.path.join(history_root, date_str)):
        body = f"<p>Run <strong>{_esc(date_str)}</strong> not found.</p><p><a href='/runs'>← All runs</a></p>"
        return _render(f"{date_str} — MHDE", body), 404

    entries = _read_csv_entries(history_root, date_str)
    reviews = _read_reviews(history_root, date_str)
    crossings = [e for e in entries if _is_crossing(e)]
    valid_no_cross = [e for e in entries if _is_promoted(e) and not _is_crossing(e)]
    bear = [e for e in entries if not _is_promoted(e)
            and e.get("sentiment") == "bearish"
            and float(e.get("llm_adjustment", 0) or 0) < 0]
    weak = [e for e in entries if _is_weak(e)]

    def _score_cell(e: dict) -> str:
        orig = float(e.get("original_score", 0) or 0)
        shad = float(e.get("shadow_score", 0) or 0)
        adj = float(e.get("llm_adjustment", 0) or 0)
        scaled_raw = e.get("scaled_adjustment")
        scaled = float(scaled_raw) if scaled_raw not in (None, "") else None
        scaled_shad_raw = e.get("scaled_shadow_score")
        scaled_shad = float(scaled_shad_raw) if scaled_shad_raw not in (None, "") else None
        base = f"{orig:.1f} → <strong>{shad:.1f}</strong> (llm:{adj:+.1f})"
        if scaled is not None:
            base += f'<br><small style="color:#388e3c">scaled:{scaled:+.2f} → {(scaled_shad or orig):.1f}</small>'
        return base

    def _review_cell(ticker: str) -> str:
        rev = reviews.get(ticker)
        d = rev.get("analyst_decision", "unknown") if rev else ""
        notes = _esc(rev.get("analyst_notes", "")) if rev else ""
        badge = ""
        if d and d in _VALID_DECISIONS:
            badge = f'<span class="badge badge-{_esc(d)}">{_esc(d)}</span> '
            if notes:
                badge += f'<span class="muted">{notes}</span><br>'
        options = "".join(
            f'<option value="{v}"{" selected" if d == v else ""}>{v}</option>'
            for v in ("accept", "watch", "reject", "unknown")
        )
        form = (
            f'<form class="review-form" method="post" action="/runs/{_esc(date_str)}/review">'
            f'<input type="hidden" name="ticker" value="{_esc(ticker)}">'
            f'<select name="analyst_decision">{options}</select> '
            f'<input type="text" name="analyst_notes" placeholder="notes" value="{notes}" maxlength="200"> '
            f'<button type="submit">Save</button>'
            f'</form>'
        )
        return badge + form

    def _cross_rows() -> str:
        if not crossings:
            return '<tr><td colspan="6"><em>None</em></td></tr>'
        out = []
        for e in crossings:
            url = e.get("constructed_url") or ""
            sec = f' <a href="{_esc(url)}">[SEC]</a>' if url else ""
            quote = _esc(str(e.get("evidence_quote", ""))[:200])
            ticker = e["ticker"]
            out.append(
                f'<tr class="cross">'
                f'<td><strong>{_esc(ticker)}</strong><br>{_review_cell(ticker)}</td>'
                f'<td>{_score_cell(e)}</td>'
                f'<td>{_esc(e.get("catalyst_type",""))}</td>'
                f'<td>{float(e.get("confidence",0) or 0):.2f}</td>'
                f'<td>{quote}{sec}</td>'
                f'</tr>'
            )
        return "\n".join(out)

    def _simple_rows(lst: list[dict]) -> str:
        if not lst:
            return '<tr><td colspan="4"><em>None</em></td></tr>'
        out = []
        for e in lst:
            out.append(
                f"<tr><td>{_esc(e['ticker'])}</td>"
                f"<td>{_score_cell(e)}</td>"
                f"<td>{_esc(e.get('catalyst_type',''))}</td>"
                f"<td>{_esc(str(e.get('evidence_quote',''))[:80])}</td></tr>"
            )
        return "\n".join(out)

    weak_summary = ""
    if weak:
        from collections import Counter
        by_status: Counter = Counter(e.get("validation_status", "unknown") for e in weak)
        weak_summary = "".join(
            f"<tr><td>{_esc(s)}</td><td>{c}</td></tr>"
            for s, c in by_status.most_common()
        )

    meta_rows = "".join(
        f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>"
        for k, v in [
            ("Sampled", meta.get("sampled", "—")),
            ("Source available", meta.get("source_available", "—")),
            ("Valid actionable", meta.get("valid_actionable", "—")),
            ("Reject→C", len(crossings)),
            ("Bearish", len(bear)),
            ("Weak/rejected", len(weak)),
            ("Provider", meta.get("provider", "—")),
        ]
    )

    body = f"""
<h2>Run: {_esc(date_str)}</h2>
<table>{meta_rows}</table>

<h2>Reject→C Crossings</h2>
<table>
<tr><th>Ticker</th><th>Score</th><th>Catalyst</th><th>Conf</th><th>Evidence</th></tr>
{_cross_rows()}
</table>

<h2>Valid — No Tier Change</h2>
<table>
<tr><th>Ticker</th><th>Score</th><th>Catalyst</th><th>Evidence</th></tr>
{_simple_rows(valid_no_cross)}
</table>

<h2>Bearish Downgrades</h2>
<table>
<tr><th>Ticker</th><th>Score</th><th>Catalyst</th><th>Evidence</th></tr>
{_simple_rows(bear)}
</table>

<h2>Weak / Rejected Evidence</h2>
<details open>
<summary>{len(weak)} entries</summary>
<table>
<tr><th>Status</th><th>Count</th></tr>
{weak_summary if weak_summary else '<tr><td colspan="2"><em>None</em></td></tr>'}
</table>
</details>

<p><a href="/runs">← All runs</a> | <a href="/">Home</a></p>
"""
    return _render(f"{date_str} — MHDE Catalyst Review", body), 200


def _learning_page(output_dir: str) -> str:
    import csv as _csv
    from collections import Counter as _Counter
    from pathlib import Path

    rows_path = Path(output_dir) / "prediction_vs_actual_rows.csv"
    enriched_path = Path(output_dir) / "prediction_vs_actual_enriched_rows.csv"

    if not rows_path.exists():
        body = (
            '<h2>Prediction vs Actual — Learning Summary</h2>'
            '<p class="muted">No prediction report found. '
            'Run <code>python main.py missed prediction-vs-actual</code> to generate.</p>'
        )
        return _render("Learning — MHDE", body)

    with open(rows_path, newline="") as f:
        rows = list(_csv.DictReader(f))
    total = len(rows)
    clf = _Counter(r.get("classification", "") for r in rows)
    report_date = rows[0].get("event_date", "") if rows else ""

    clf_rows = "".join(
        f"<tr><td>{_esc(k)}</td><td>{v}</td></tr>"
        for k, v in sorted(clf.items(), key=lambda x: -x[1])
    )

    rc_rows_html = ""
    if enriched_path.exists():
        with open(enriched_path, newline="") as f:
            enriched = list(_csv.DictReader(f))
        rc = _Counter(r.get("root_cause_group", "unknown") for r in enriched)
        rc_rows_html = "".join(
            f"<tr><td>{_esc(k)}</td><td>{v}</td></tr>"
            for k, v in sorted(rc.items(), key=lambda x: -x[1])
        )

    artifact_links = " &bull; ".join(
        f'<a href="/learning/{_esc(atype)}">{_esc(label)}</a>'
        for atype, label in [
            ("report_md", "Prediction Report"),
            ("rows_csv", "Rows CSV"),
            ("enriched_csv", "Enriched CSV"),
            ("root_cause_md", "Root Cause Report"),
        ]
    )

    body = f"""
<h2>Prediction vs Actual — Learning Summary</h2>
<p class="muted">Report date: {_esc(report_date)} &mdash; Total events: {total}</p>
<div class="banner shadow">&#128274; Shadow-only — production scores unchanged.</div>

<h2>Classification Breakdown</h2>
<table>
<tr><th>Classification</th><th>Count</th></tr>
{clf_rows}
</table>

<h2>Root Cause Groups</h2>
<table>
<tr><th>Group</th><th>Count</th></tr>
{rc_rows_html if rc_rows_html else '<tr><td colspan="2"><em>Run enrich-root-causes to populate</em></td></tr>'}
</table>

<h2>Artifacts</h2>
<p class="muted">{artifact_links}</p>
<p><a href="/">&#8592; Home</a></p>
"""
    return _render("Learning — MHDE", body)


def _today_page(history_root: str, output_dir: str) -> str:
    import csv as _csv
    from pathlib import Path

    dates = _dated_dirs(history_root)
    nav = (
        '<p><a href="/candidates">Candidates</a> &nbsp;|&nbsp; '
        '<a href="/learning">Learning</a> &nbsp;|&nbsp; '
        '<a href="/moves">Moves</a> &nbsp;|&nbsp; '
        '<a href="/ops">Ops</a> &nbsp;|&nbsp; '
        '<a href="/">Home</a></p>'
    )

    if not dates:
        body = (
            '<h2>Today — No Runs Found</h2>'
            '<p class="muted">No catalyst queue runs archived yet.</p>'
            + nav
        )
        return _render("Today — MHDE", body)

    latest = dates[0]
    entries = _read_csv_entries(history_root, latest)
    meta = _read_metadata(history_root, latest)

    crossings = sum(1 for r in entries if _is_crossing(r))
    reviews = _read_reviews(history_root, latest)
    promoted_entries = [r for r in entries if _is_promoted(r)]
    needs_review = [
        r for r in promoted_entries
        if reviews.get(r.get("ticker"), {}).get("analyst_decision", "unknown") in ("", "unknown")
    ]
    watch_count = sum(1 for v in reviews.values() if v.get("analyst_decision") == "watch")

    pva_path = Path(output_dir) / "prediction_vs_actual_rows.csv"
    pva_counts: dict[str, int] = {}
    pva_warn = ""
    if pva_path.exists():
        with open(pva_path, newline="") as f:
            for row in _csv.DictReader(f):
                clf = row.get("classification", "")
                pva_counts[clf] = pva_counts.get(clf, 0) + 1
    else:
        pva_warn = (
            '<div class="warn-box">prediction_vs_actual_rows.csv not found. '
            'Run <code>python main.py missed prediction-vs-actual</code>.</div>'
        )

    def _stat(label: str, val) -> str:
        return (
            f'<div class="stat-card">'
            f'<span class="stat-label">{_esc(label)}</span>'
            f'<span class="stat-val">{_esc(str(val))}</span>'
            f'</div>'
        )

    stats = (
        _stat("Latest Run", latest)
        + _stat("Entries", len(entries))
        + _stat("Reject→C", crossings)
        + _stat("Needs Review", len(needs_review))
        + _stat("Watch", watch_count)
        + _stat("True Miss", pva_counts.get("true_miss", "—"))
        + _stat("Near Threshold", pva_counts.get("near_threshold", "—"))
        + _stat("Scored Missed", pva_counts.get("scored_missed", "—"))
    )

    top_rows = needs_review[:10]

    review_table = ""
    if top_rows:
        tr_html = "".join(
            f"<tr>"
            f"<td>{_esc(r.get('ticker', ''))}</td>"
            f"<td>{_esc(r.get('shadow_tier', ''))}</td>"
            f"<td>{_esc(r.get('scaled_shadow_score', r.get('shadow_score', '')))}</td>"
            f"</tr>"
            for r in top_rows
        )
        review_table = (
            '<h2>Needs Review (top 10)</h2>'
            '<table>'
            '<tr><th>Ticker</th><th>Shadow Tier</th><th>Scaled Shadow Score</th></tr>'
            + tr_html
            + '</table>'
        )

    body = (
        f'<h2>Today — {_esc(latest)}</h2>'
        f'<div class="stat-grid">{stats}</div>'
        + pva_warn
        + review_table
        + nav
    )
    return _render("Today — MHDE", body)


def _candidates_page(history_root: str) -> tuple[str, int]:
    dates = _dated_dirs(history_root)
    if not dates:
        body = (
            '<h2>Candidates — No Runs Found</h2>'
            '<p class="muted">No catalyst queue runs archived yet.</p>'
            '<p><a href="/">Home</a></p>'
        )
        return _render("Candidates — MHDE", body), 200

    latest = dates[0]
    html, code = _run_detail(history_root, latest)
    # Replace page title to say "Candidates — {date}"
    html = html.replace(
        f"<title>{_esc(latest)} — MHDE Catalyst Review</title>",
        f"<title>Candidates — {_esc(latest)}</title>",
    )
    return html, code


def _moves_page(output_dir: str) -> str:
    import csv as _csv
    from pathlib import Path

    nav = (
        '<p><a href="/today">Today</a> &nbsp;|&nbsp; '
        '<a href="/candidates">Candidates</a> &nbsp;|&nbsp; '
        '<a href="/learning">Learning</a> &nbsp;|&nbsp; '
        '<a href="/ops">Ops</a> &nbsp;|&nbsp; '
        '<a href="/">Home</a></p>'
    )

    pva_path = Path(output_dir) / "prediction_vs_actual_rows.csv"
    if not pva_path.exists():
        body = (
            '<h2>Moves</h2>'
            '<p class="muted">prediction_vs_actual_rows.csv not found.</p>'
            '<p class="muted">Run: <code>python main.py missed prediction-vs-actual</code></p>'
            + nav
        )
        return _render("Moves — MHDE", body)

    with open(pva_path, newline="") as f:
        rows = list(_csv.DictReader(f))

    by_window: dict[str, list[dict]] = {}
    for r in rows:
        w = r.get("window_days", "unknown")
        by_window.setdefault(w, []).append(r)

    sections = []
    for window in sorted(by_window.keys(), key=lambda x: (int(x) if x.isdigit() else 9999)):
        group = sorted(
            by_window[window],
            key=lambda r: _safe_float(r.get("return_value")),
            reverse=True,
        )[:20]
        tr_html = "".join(
            f"<tr>"
            f"<td>{_esc(r.get('ticker', ''))}</td>"
            f"<td>{_esc(r.get('event_date', ''))}</td>"
            f"<td>{_esc(r.get('return_value', ''))}</td>"
            f"<td>{_esc(r.get('classification', ''))}</td>"
            f"</tr>"
            for r in group
        )
        sections.append(
            f'<h2>Window: {_esc(window)} days</h2>'
            f'<table>'
            f'<tr><th>Ticker</th><th>Date</th><th>Return</th><th>Classification</th></tr>'
            f'{tr_html}'
            f'</table>'
        )

    body = '<h2>Moves — Prediction vs Actual</h2>' + "".join(sections) + nav
    return _render("Moves — MHDE", body)


def _ops_page(history_root: str, output_dir: str) -> str:
    import subprocess
    from pathlib import Path

    nav = (
        '<p><a href="/today">Today</a> &nbsp;|&nbsp; '
        '<a href="/candidates">Candidates</a> &nbsp;|&nbsp; '
        '<a href="/learning">Learning</a> &nbsp;|&nbsp; '
        '<a href="/moves">Moves</a> &nbsp;|&nbsp; '
        '<a href="/">Home</a></p>'
    )

    # Artifact checks
    artifact_names = [
        "prediction_vs_actual_rows.csv",
        "prediction_vs_actual_enriched_rows.csv",
        "root_cause_enrichment_report.md",
        "daily_catalyst_queue.csv",
    ]
    from datetime import datetime as _dt
    artifact_rows = []
    for fname in artifact_names:
        fpath = Path(output_dir) / fname
        if fpath.exists():
            stat = fpath.stat()
            mtime = _dt.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            size_kb = f"{stat.st_size / 1024:.1f} KB"
            status = "&#9989;"
        else:
            mtime = "—"
            size_kb = "—"
            status = "&#10060; missing"
        artifact_rows.append(
            f"<tr><td>{_esc(fname)}</td><td>{status}</td><td>{mtime}</td><td>{size_kb}</td></tr>"
        )

    # Latest run date
    dates = _dated_dirs(history_root)
    latest_run = dates[0] if dates else "—"

    # Systemd timers
    timer_lines = []
    try:
        result = subprocess.run(
            ["systemctl", "list-timers", "--no-legend", "--no-pager"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if "mhde" in line.lower():
                timer_lines.append(line)
    except Exception:
        pass

    if timer_lines:
        timer_html = "<pre>" + _esc("\n".join(timer_lines)) + "</pre>"
    else:
        timer_html = '<p class="muted">No mhde timers active.</p>'

    # Env var presence checks (NEVER show values or full key names)
    env_keys = ["POLYGON_API_KEY", "ALPHA_VANTAGE_API_KEY", "OPENAI_API_KEY"]
    env_labels = {
        "POLYGON_API_KEY": "Polygon",
        "ALPHA_VANTAGE_API_KEY": "Alpha Vantage",
        "OPENAI_API_KEY": "OpenAI",
    }
    missing_keys = [k for k in env_keys if not bool(os.environ.get(k))]
    env_warn = ""
    if missing_keys:
        env_warn = (
            '<div class="warn-box">Missing credentials: '
            + ", ".join(_esc(env_labels.get(k, k)) for k in missing_keys)
            + ". Set the required environment variables before running the pipeline.</div>"
        )

    body = (
        '<h2>Ops</h2>'
        f'<p><strong>Latest run:</strong> {_esc(latest_run)}</p>'
        '<h2>Artifacts</h2>'
        '<table>'
        '<tr><th>File</th><th>Status</th><th>Modified</th><th>Size</th></tr>'
        + "".join(artifact_rows)
        + '</table>'
        + env_warn
        + '<h2>Systemd Timers</h2>'
        + timer_html
        + nav
    )
    return _render("Ops — MHDE", body)


# ── App factory ───────────────────────────────────────────────────────────────

def create_app(
    history_root: str,
    output_dir: str,
    unsafe_no_auth: bool = False,
    unsafe_public_bind: bool = False,
) -> Flask:
    app = Flask(__name__)
    app.config["UNSAFE_NO_AUTH"] = unsafe_no_auth
    app.config["UNSAFE_PUBLIC_BIND"] = unsafe_public_bind
    app.config["HISTORY_ROOT"] = history_root
    app.config["OUTPUT_DIR"] = output_dir
    app.config["BIND_HOST"] = "127.0.0.1"

    # ── PWA public assets (no auth required) ──────────────────────────────────

    @app.route("/manifest.webmanifest")
    def manifest():
        return Response(_MANIFEST_JSON, mimetype="application/manifest+json")

    @app.route("/service-worker.js")
    def service_worker():
        resp = Response(_SW_JS, mimetype="application/javascript")
        resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    @app.route("/static/icons/icon-192.png")
    def icon_192():
        return Response(_ICON_192, mimetype="image/png")

    @app.route("/static/icons/icon-512.png")
    def icon_512():
        return Response(_ICON_512, mimetype="image/png")

    # ── Authenticated routes ───────────────────────────────────────────────────

    @app.route("/")
    @_require_auth
    def homepage():
        return _homepage(history_root, output_dir)

    @app.route("/runs")
    @_require_auth
    def runs_list():
        return _runs_list(history_root)

    @app.route("/runs/<date_str>")
    @_require_auth
    def run_detail(date_str: str):
        html, status = _run_detail(history_root, date_str)
        return html, status

    @app.route("/artifacts/latest/<atype>")
    @_require_auth
    def artifact_latest(atype: str):
        if atype not in _ARTIFACT_FILES:
            return Response("Unknown artifact type", 404)
        dates = _dated_dirs(history_root)
        if not dates:
            return Response("No runs found", 404)
        fpath = os.path.join(history_root, dates[0], _ARTIFACT_FILES[atype])
        if not os.path.exists(fpath):
            return Response("Artifact not found", 404)
        with open(fpath, "rb") as f:
            data = f.read()
        return Response(data, mimetype=_ARTIFACT_MIME[atype])

    @app.route("/artifacts/<date_str>/<atype>")
    @_require_auth
    def artifact_dated(date_str: str, atype: str):
        if not _is_valid_date(date_str):
            return Response("Invalid date format", 400)
        if atype not in _ARTIFACT_FILES:
            return Response("Unknown artifact type", 404)
        fpath = os.path.join(history_root, date_str, _ARTIFACT_FILES[atype])
        fpath = os.path.realpath(fpath)
        base = os.path.realpath(history_root)
        if not fpath.startswith(base + os.sep):
            return Response("Forbidden", 403)
        if not os.path.exists(fpath):
            return Response("Artifact not found", 404)
        with open(fpath, "rb") as f:
            data = f.read()
        return Response(data, mimetype=_ARTIFACT_MIME[atype])

    @app.route("/runs/<date_str>/review", methods=["POST"])
    @_require_auth
    def post_review(date_str: str):
        if not os.path.isdir(os.path.join(history_root, date_str)):
            return Response("Run not found", 404)
        ticker = (request.form.get("ticker") or "").strip().upper()
        decision = (request.form.get("analyst_decision") or "").strip().lower()
        notes = (request.form.get("analyst_notes") or "").strip()[:200]

        entries = _read_csv_entries(history_root, date_str)
        valid_tickers = {e["ticker"] for e in entries if _is_promoted(e)}
        if ticker not in valid_tickers:
            return Response(f"Unknown ticker: {_esc(ticker)}", 400)
        if decision not in _VALID_DECISIONS:
            return Response(f"Invalid decision: {_esc(decision)}", 400)

        _write_review(history_root, date_str, ticker, decision, notes)
        return redirect(url_for("run_detail", date_str=date_str))

    @app.route("/learning")
    @_require_auth
    def learning_page():
        return _learning_page(output_dir)

    @app.route("/learning/<atype>")
    @_require_auth
    def learning_artifact(atype: str):
        if atype not in _LEARNING_ARTIFACT_FILES:
            return Response("Unknown artifact type", 404)
        fpath = os.path.join(output_dir, _LEARNING_ARTIFACT_FILES[atype])
        if not os.path.exists(fpath):
            return Response("Artifact not found", 404)
        with open(fpath, "rb") as f:
            data = f.read()
        return Response(data, mimetype=_LEARNING_ARTIFACT_MIME[atype])

    @app.route("/today")
    @_require_auth
    def today():
        return _today_page(history_root, output_dir)

    @app.route("/candidates")
    @_require_auth
    def candidates():
        html, code = _candidates_page(history_root)
        return html, code

    @app.route("/moves")
    @_require_auth
    def moves():
        return _moves_page(output_dir)

    @app.route("/ops")
    @_require_auth
    def ops():
        return _ops_page(history_root, output_dir)

    return app


def run_server(
    host: str,
    port: int,
    history_root: str,
    output_dir: str,
    unsafe_no_auth: bool = False,
    unsafe_public_bind: bool = False,
    _dry_run: bool = False,
) -> None:
    """Start the Flask review server with pre-flight safety checks."""
    if not unsafe_no_auth:
        if not os.environ.get("REVIEW_UI_USERNAME") or not os.environ.get("REVIEW_UI_PASSWORD"):
            raise SystemExit(
                "Error: REVIEW_UI_USERNAME and REVIEW_UI_PASSWORD must be set.\n"
                "Use --unsafe-no-auth for local-only testing (loopback only).\n"
                "Never log or print the password value."
            )

    if host == "0.0.0.0" and not unsafe_public_bind:
        raise SystemExit(
            "Error: Binding to 0.0.0.0 is rejected by default.\n"
            "Use --unsafe-public-bind only when protected by a reverse proxy "
            "with TLS (e.g. Caddy). Default: 127.0.0.1."
        )

    logger.info("Starting review server on %s:%s", host, port)
    if _dry_run:
        return

    app = create_app(history_root, output_dir, unsafe_no_auth, unsafe_public_bind)
    app.config["BIND_HOST"] = host
    app.run(host=host, port=port, debug=False)
