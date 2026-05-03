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


def _is_scaled_crossing(row: dict) -> bool:
    """Scaled crossing: scaled_adjustment also contributed (not just raw LLM)."""
    if not _is_crossing(row):
        return False
    try:
        return float(row.get("scaled_adjustment") or 0) > 0
    except (ValueError, TypeError):
        return False


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
.review-form{margin-top:6px;font-size:.85rem;display:flex;flex-wrap:wrap;gap:4px;align-items:center;}
.review-form select{padding:4px 6px;border:1px solid #ccc;border-radius:3px;min-width:90px;}
.review-form input[type=text]{padding:4px 6px;border:1px solid #ccc;border-radius:3px;flex:1;min-width:100px;}
.review-form button{padding:4px 10px;background:#1565c0;color:#fff;border:none;border-radius:3px;cursor:pointer;white-space:nowrap;}
.stat-grid{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}
.stat-card{background:#f8f9fa;border:1px solid #e0e0e0;border-radius:8px;padding:12px 18px;min-width:140px}
.stat-label{display:block;font-size:0.75rem;color:#666;margin-bottom:4px}
.stat-val{font-size:1.4rem;font-weight:700;color:#1565c0}
.warn-box{background:#fff3e0;border-left:4px solid #ff9800;padding:10px 14px;margin:10px 0}
.doc-content{line-height:1.75;font-size:.95rem;}
.doc-content h1,.doc-content h2,.doc-content h3,.doc-content h4{margin-top:1.4em;}
.doc-content pre{background:#f5f5f5;border-radius:4px;padding:10px 14px;overflow-x:auto;font-size:.78rem;white-space:pre-wrap;word-break:break-word;}
.doc-content code{background:#f0f0f0;padding:1px 4px;border-radius:3px;font-size:.85em;}
.doc-content pre code{background:none;padding:0;font-size:inherit;}
.doc-content .tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:8px 0;}
.doc-content ul,.doc-content ol{padding-left:1.4em;}
.pred-card{border-left:4px solid #bbb;background:#fff;border-radius:4px;padding:12px 14px;margin:10px 0;box-shadow:0 1px 3px rgba(0,0,0,.07);}
.pred-card-high{border-color:#1b5e20;}.pred-card-watch{border-color:#fb8c00;}
.pred-card-investigate{border-color:#1565c0;}.pred-card-low{border-color:#ef9a9a;}
.pred-card-context{border-color:#b0bec5;}.pred-card-ignore{border-color:#9e9e9e;}
.pred-header{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px;margin-bottom:6px;}
.pred-ticker{font-size:1.15rem;font-weight:700;}
.pred-score{font-weight:600;color:#1565c0;}
.pred-action{padding:2px 8px;border-radius:3px;font-size:.73rem;font-weight:700;}
.pred-action-high{background:#e8f5e9;color:#1b5e20;}.pred-action-watch{background:#fff3e0;color:#e65100;}
.pred-action-investigate{background:#e3f2fd;color:#0d47a1;}.pred-action-ignore{background:#f5f5f5;color:#757575;}
.pred-action-low{background:#ffebee;color:#b71c1c;}.pred-action-context{background:#eceff1;color:#546e7a;}
.pred-dir-bullish{color:#2e7d32;font-weight:600;}.pred-dir-bearish{color:#c62828;font-weight:600;}.pred-dir-neutral{color:#757575;}
.pred-timeframe{font-size:.78rem;color:#888;}
.pred-summary{font-size:.9rem;font-weight:500;margin:4px 0 8px;}
.pred-meta{display:flex;flex-wrap:wrap;gap:14px;margin:6px 0;}
.pred-col .pred-label{display:block;font-size:.68rem;color:#999;text-transform:uppercase;letter-spacing:.04em;}
.pred-col .pred-val{font-size:.83rem;font-weight:500;}
.pred-reason{font-size:.83rem;padding:5px 8px;background:#f9f9f9;border-radius:3px;margin:6px 0;}
.pred-checks{margin:4px 0 4px 16px;font-size:.82rem;padding:0;}
.pred-risk{font-size:.8rem;color:#666;margin-top:4px;}
.sig-str-high{color:#1b5e20;font-weight:700;}.sig-str-medium{color:#e65100;}.sig-str-low{color:#9e9e9e;}
.trade-win{margin-top:10px;display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;padding:8px 10px;background:#f8f9fa;border-radius:4px;}
.tw-label{display:block;font-size:.68rem;color:#999;text-transform:uppercase;letter-spacing:.04em;}
.tw-val{font-size:.82rem;font-weight:500;}
.ss-active{color:#1b5e20;font-weight:700;}.ss-decaying{color:#c62828;font-weight:600;}.ss-monitoring{color:#e65100;}
.ss-mostly-expired{color:#9e9e9e;}.ss-inactive{color:#b0bec5;}
.detail-table{width:100%;border-collapse:collapse;margin:6px 0 14px;}
.detail-table td{padding:5px 8px;border-bottom:1px solid #e5e7eb;font-size:.88rem;vertical-align:top;}
.detail-table td.tlabel{color:#6b7280;font-size:.78rem;white-space:nowrap;width:160px;}
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


def _format_return_pct(value) -> str:
    """Format a return value as a percentage string.

    Stored values may be decimal (0.1673) or already-percent (16.73).
    Heuristic: if |value| < 2.0 treat as decimal fraction → multiply by 100.
    """
    if value is None:
        return "—"
    try:
        v = float(value)
    except (ValueError, TypeError):
        return "—"
    if abs(v) < 2.0:
        v = v * 100
    return f"{v:.1f}%"


# ── Docs viewer ───────────────────────────────────────────────────────────────

_DOCS_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")
)

_DOCS_REGISTRY: dict[str, tuple[str, str]] = {
    "operating-manual": ("Operating Manual", "mhde_operating_manual.md"),
    "architecture": ("Architecture", "mhde_architecture.md"),
    "data-sources": ("Data Sources", "mhde_data_sources.md"),
    "scoring-governance": ("Scoring Governance", "mhde_scoring_governance.md"),
    "completion-status": ("Completion Status", "mhde_full_completion_status.md"),
}


def _doc_path(key: str) -> "str | None":
    entry = _DOCS_REGISTRY.get(key)
    if not entry:
        return None
    _, filename = entry
    path = os.path.normpath(os.path.join(_DOCS_ROOT, filename))
    if not path.startswith(_DOCS_ROOT + os.sep):
        return None
    return path


def _inline_md(text: str) -> str:
    import re as _re
    text = _esc(text)
    text = _re.sub(r'\*\*\*(.*?)\*\*\*', r'<strong><em>\1</em></strong>', text)
    text = _re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', text)
    text = _re.sub(r'(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)', r'<em>\1</em>', text)
    text = _re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    text = _re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
    return text


def _render_markdown(text: str) -> str:
    import re as _re
    lines = text.split("\n")
    out: list[str] = []
    in_code = False
    in_table = False
    in_ul = False
    in_ol = False

    def flush_list() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    def flush_table() -> None:
        nonlocal in_table
        if in_table:
            out.append("</tbody></table></div>")
            in_table = False

    for line in lines:
        if line.strip().startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                flush_list()
                flush_table()
                lang = _esc(line.strip()[3:].strip())
                cls = f' class="language-{lang}"' if lang else ""
                out.append(f"<pre><code{cls}>")
                in_code = True
            continue

        if in_code:
            out.append(_esc(line))
            continue

        if line.strip().startswith("|") and line.strip().endswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(_re.match(r"^:?-+:?$", c) for c in cells if c.strip()):
                continue
            if not in_table:
                flush_list()
                out.append('<div class="tbl-wrap"><table>')
                out.append(
                    "<thead><tr>"
                    + "".join(f"<th>{_inline_md(c)}</th>" for c in cells)
                    + "</tr></thead><tbody>"
                )
                in_table = True
            else:
                out.append(
                    "<tr>"
                    + "".join(f"<td>{_inline_md(c)}</td>" for c in cells)
                    + "</tr>"
                )
            continue
        flush_table()

        m = _re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush_list()
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline_md(m.group(2))}</h{lvl}>")
            continue

        m = _re.match(r"^[-*]\s+(.*)", line)
        if m:
            if not in_ul:
                flush_list()
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline_md(m.group(1))}</li>")
            continue

        m = _re.match(r"^\d+\.\s+(.*)", line)
        if m:
            if not in_ol:
                flush_list()
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{_inline_md(m.group(1))}</li>")
            continue

        if not line.strip():
            flush_list()
            out.append("")
            continue

        if _re.match(r"^---+\s*$", line) or _re.match(r"^===+\s*$", line):
            flush_list()
            out.append("<hr>")
            continue

        flush_list()
        out.append(f"<p>{_inline_md(line)}</p>")

    if in_code:
        out.append("</code></pre>")
    flush_list()
    flush_table()
    return "\n".join(out)


def _docs_index_page() -> str:
    items: list[str] = []
    for key, (title, _) in _DOCS_REGISTRY.items():
        path = _doc_path(key)
        if path and os.path.exists(path):
            items.append(
                f'<li><a href="/docs/{_esc(key)}">{_esc(title)}</a>'
                f' <a class="muted" href="/docs/download/{_esc(key)}">[raw]</a></li>'
            )
        else:
            items.append(f'<li><span class="muted">{_esc(title)} — not found</span></li>')
    nav = '<p><a href="/">&#8592; Home</a></p>'
    body = "<h2>Documentation</h2><ul>" + "".join(items) + "</ul>" + nav
    return _render("Docs — MHDE", body)


def _doc_page(key: str) -> "tuple[str, int]":
    path = _doc_path(key)
    if not path:
        return _render("Not Found — MHDE", "<p>Unknown document.</p>"), 404
    if not os.path.exists(path):
        return _render("Not Found — MHDE", "<p>Document file not found on disk.</p>"), 404
    title, _ = _DOCS_REGISTRY[key]
    try:
        with open(path, encoding="utf-8") as fh:
            md_text = fh.read()
    except Exception as exc:
        return _render("Error — MHDE", f"<p>Could not read file: {_esc(str(exc))}</p>"), 500
    nav = (
        f'<p><a href="/docs">&#8592; Docs</a>'
        f' &nbsp;|&nbsp; <a href="/docs/download/{_esc(key)}">Download raw</a></p>'
    )
    body = f'<h2>{_esc(title)}</h2>{nav}<div class="doc-content">{_render_markdown(md_text)}</div>'
    return _render(f"{_esc(title)} — MHDE", body), 200


def _doc_download(key: str) -> "Response | tuple[str, int]":
    path = _doc_path(key)
    if not path or not os.path.exists(path):
        return "Not found", 404
    _, filename = _DOCS_REGISTRY[key]
    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
    except Exception:
        return "Error reading file", 500
    return Response(
        content,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Prediction cards ──────────────────────────────────────────────────────────

_DIR_ARROW = {"bullish": "↑", "bearish": "↓", "neutral": "→", "mixed": "↔"}

_TRADING_WINDOWS: dict[str, tuple[int, int]] = {
    "merger_acquisition": (5, 60),
    "earnings": (1, 20),
    "guidance": (1, 60),
    "regulatory": (20, 60),
    "settlement": (20, 60),
    "management_change": (5, 20),
    "product_launch": (5, 30),
    "restructuring": (5, 20),
    "government_contract": (5, 30),
    "contract_expansion": (5, 30),
    "subscriber_metric": (5, 20),
    "insider_buying": (5, 15),
}


def _compute_signal_strength(e: dict) -> str:
    try:
        conf = float(e.get("confidence") or 0)
    except (ValueError, TypeError):
        conf = 0.0
    try:
        days = int(float(e.get("days_since_event") or 0))
    except (ValueError, TypeError):
        days = 0
    impact = (e.get("impact_estimate") or "").lower()
    if conf >= 0.8 and impact == "high" and days <= 10:
        return "High"
    if conf < 0.5 or impact == "low" or days > 45:
        return "Low"
    return "Medium"


def _compute_action_priority(e: dict) -> tuple[str, str]:
    """Returns (label, css_key). Uses signal characteristics, not action_guidance."""
    direction = (e.get("expected_direction") or "").lower()
    pir = (e.get("priced_in_risk") or "").lower()
    impact = (e.get("impact_estimate") or "").lower()
    tier_move = str(e.get("tier_move") or "")
    try:
        days = int(float(e.get("days_since_event") or 0))
    except (ValueError, TypeError):
        days = 0
    try:
        scaled_adj = float(e.get("scaled_adjustment") or 0)
    except (ValueError, TypeError):
        scaled_adj = 0.0

    if direction == "neutral":
        return ("Investigate", "investigate")

    already_c = tier_move.startswith("C→") or (
        (e.get("original_tier") or "").upper() == "C" and not tier_move
    )
    if already_c and abs(scaled_adj) < 2.0:
        return ("Context", "context")

    if pir == "high" and days > 30:
        return ("Context", "context")

    if pir == "high":
        return ("Watch", "watch")

    if impact == "low":
        return ("Low Priority", "low")

    if pir == "medium" and days > 20:
        return ("Watch", "watch")

    if days > 45:
        return ("Watch", "watch")

    if scaled_adj >= 3.0 and days <= 14 and pir not in ("high", "medium"):
        return ("High Priority", "high")

    if pir == "medium":
        return ("Watch", "watch")

    return ("Watch", "watch")


def _compute_trading_window(e: dict) -> dict:
    import datetime as _dt
    catalyst_type = (e.get("catalyst_type") or "").lower().replace(" ", "_")
    try:
        days_since = int(float(e.get("days_since_event") or 0))
    except (ValueError, TypeError):
        days_since = 0
    pir = (e.get("priced_in_risk") or "").lower()
    direction = (e.get("expected_direction") or "").lower()

    _NONE = {
        "trading_window": "None",
        "signal_status": "Inactive",
        "signal_status_css": "ss-inactive",
        "signal_expiry_date": "",
        "entry_trigger": "—",
        "invalidation_condition": "—",
        "review_cadence": "—",
    }

    window_range = _TRADING_WINDOWS.get(catalyst_type)
    if not window_range or direction == "neutral":
        return _NONE

    lo, hi = window_range

    if catalyst_type == "merger_acquisition":
        if pir == "high" and days_since > 75:
            status, css = "Mostly expired", "ss-mostly-expired"
        elif days_since > 45:
            status, css = "Decaying", "ss-decaying"
        elif days_since > lo:
            status, css = "Monitoring", "ss-monitoring"
        else:
            status, css = "Active", "ss-active"
    elif catalyst_type in ("regulatory", "settlement"):
        if days_since > 45:
            status, css = "Decaying", "ss-decaying"
        elif days_since > lo:
            status, css = "Monitoring", "ss-monitoring"
        else:
            status, css = "Active", "ss-active"
    elif catalyst_type == "management_change":
        if days_since > hi:
            status, css = "Mostly expired", "ss-mostly-expired"
        elif days_since > lo:
            status, css = "Monitoring", "ss-monitoring"
        else:
            status, css = "Active", "ss-active"
    elif catalyst_type in ("earnings", "guidance"):
        if days_since > hi:
            status, css = "Mostly expired", "ss-mostly-expired"
        elif days_since > lo:
            status, css = "Monitoring", "ss-monitoring"
        else:
            status, css = "Active", "ss-active"
    else:
        if days_since > hi:
            status, css = "Mostly expired", "ss-mostly-expired"
        elif days_since > lo:
            status, css = "Monitoring", "ss-monitoring"
        else:
            status, css = "Active", "ss-active"

    if pir == "high" and status in ("Active", "Monitoring"):
        status, css = "Mostly expired", "ss-mostly-expired"

    trading_window = f"{lo}–{hi} trading days"

    expiry_str = ""
    try:
        event_date_raw = e.get("event_date") or ""
        if event_date_raw:
            base = _dt.date.fromisoformat(str(event_date_raw)[:10])
            expiry = base + _dt.timedelta(days=int(hi * 1.4))
            expiry_str = expiry.isoformat()
    except Exception:
        pass

    if catalyst_type == "merger_acquisition":
        entry_trigger = "Deal confirmed, no competing bid, spread > 2%"
        invalidation = "Deal terminated or regulatory block announced"
    elif catalyst_type in ("earnings", "guidance"):
        entry_trigger = "Beat confirmed, guidance raised, no prior run-up"
        invalidation = "Guide-down in follow-up commentary or sector rotation"
    elif catalyst_type in ("regulatory", "settlement"):
        entry_trigger = "FDA/regulatory decision or settlement terms confirmed"
        invalidation = "Rejection, CRL, or appeal; settlement voided"
    elif catalyst_type == "management_change":
        entry_trigger = "Strategy announcement or restructuring plan within 30d"
        invalidation = "No strategy update within window; departure reversed"
    elif catalyst_type in ("government_contract", "contract_expansion"):
        entry_trigger = "Contract value and start date officially announced"
        invalidation = "Contract cancelled, delayed, or competitor win announced"
    else:
        entry_trigger = "Confirmation via follow-on news or volume spike"
        invalidation = "Catalyst reversal or negative follow-on announcement"

    if status == "Active":
        cadence = "Weekly"
    elif status == "Monitoring":
        cadence = "Bi-weekly"
    else:
        cadence = "Monthly or archive"

    return {
        "trading_window": trading_window,
        "signal_status": status,
        "signal_status_css": css,
        "signal_expiry_date": expiry_str,
        "entry_trigger": entry_trigger,
        "invalidation_condition": invalidation,
        "review_cadence": cadence,
    }


def _not_yet_reason(e: dict) -> str:
    parts: list[str] = []
    pir = (e.get("priced_in_risk") or "").lower()
    if pir in ("high", "medium"):
        parts.append(f"priced-in risk is {pir}")
    try:
        d = int(float(e.get("days_since_event") or 0))
        if d > 14:
            parts.append(f"event is {d}d old")
    except (ValueError, TypeError):
        pass
    impact = (e.get("impact_estimate") or "").lower()
    if impact == "low":
        parts.append("low estimated impact")
    return "; ".join(parts) if parts else "—"


def _prediction_card(e: dict, review_form_html: str) -> str:
    ticker = _esc(e.get("ticker", ""))

    try:
        orig_score = float(e.get("original_score") or 0)
        orig_fmt = f"{orig_score:.1f}"
    except (ValueError, TypeError):
        orig_fmt = "—"

    scaled = e.get("scaled_shadow_score") or e.get("shadow_score") or ""
    try:
        scaled_fmt = f"{float(scaled):.1f}"
    except (ValueError, TypeError):
        scaled_fmt = _esc(str(scaled))

    label, css_key = _compute_action_priority(e)
    strength = _compute_signal_strength(e)
    tw = _compute_trading_window(e)

    direction = (e.get("expected_direction") or "").lower()
    arrow = _DIR_ARROW.get(direction, "")
    dir_class = f"pred-dir-{direction}" if direction in _DIR_ARROW else "pred-dir-neutral"
    dir_html = f'<span class="{dir_class}">{arrow} {_esc(direction.capitalize())}</span>' if direction else ""

    timeframe = _esc(e.get("expected_timeframe") or "")
    summary = _esc(e.get("expected_move_summary") or "")
    why_move = _esc(e.get("action_reason") or "")
    not_yet = _esc(_not_yet_reason(e))
    catalyst = _esc(e.get("catalyst_type") or "")
    materiality = _esc(e.get("materiality") or "")
    conf = e.get("confidence") or ""
    try:
        conf_fmt = f"{float(conf):.0%}"
    except (ValueError, TypeError):
        conf_fmt = _esc(str(conf))
    days = _esc(str(e.get("days_since_event") or ""))

    raw_checks = e.get("key_checks") or ""
    check_items = [c.strip() for c in raw_checks.split(";") if c.strip()]
    checks_html = "".join(f"<li>{_esc(c)}</li>" for c in check_items)

    url = e.get("constructed_url") or ""
    sec_link = f'<a href="{_esc(url)}" target="_blank">[SEC filing]</a>' if url else ""

    str_css = f"sig-str-{strength.lower()}"
    ss_css = tw["signal_status_css"]

    tw_html = (
        f'<div class="trade-win">'
        f'<div><span class="tw-label">Trading window</span><span class="tw-val">{_esc(tw["trading_window"])}</span></div>'
        f'<div><span class="tw-label">Signal status</span><span class="tw-val {ss_css}">{_esc(tw["signal_status"])}</span></div>'
        f'<div><span class="tw-label">Review cadence</span><span class="tw-val">{_esc(tw["review_cadence"])}</span></div>'
        + (f'<div><span class="tw-label">Est. expiry</span><span class="tw-val">{_esc(tw["signal_expiry_date"])}</span></div>' if tw["signal_expiry_date"] else "")
        + f'<div><span class="tw-label">Entry trigger</span><span class="tw-val">{_esc(tw["entry_trigger"])}</span></div>'
        f'<div><span class="tw-label">Invalidation</span><span class="tw-val">{_esc(tw["invalidation_condition"])}</span></div>'
        f'</div>'
    )

    return (
        f'<div class="pred-card pred-card-{css_key}">'
        f'<div class="pred-header">'
        f'<span class="pred-ticker">{ticker}</span>'
        f'<span class="pred-score">Prod: {orig_fmt} &rarr; Scaled: {scaled_fmt}</span>'
        f'<span class="pred-action pred-action-{css_key}">{_esc(label)}</span>'
        f'{dir_html}'
        f'<span class="pred-timeframe">{timeframe}</span>'
        f'</div>'
        f'<div class="pred-summary">{summary}</div>'
        f'<div class="pred-meta">'
        f'<div class="pred-col"><span class="pred-label">Catalyst</span><span class="pred-val">{catalyst}</span></div>'
        f'<div class="pred-col"><span class="pred-label">Signal strength</span><span class="pred-val {str_css}">{_esc(strength)}</span></div>'
        f'<div class="pred-col"><span class="pred-label">Materiality</span><span class="pred-val">{materiality}</span></div>'
        f'<div class="pred-col"><span class="pred-label">Confidence</span><span class="pred-val">{conf_fmt}</span></div>'
        f'<div class="pred-col"><span class="pred-label">Days since event</span><span class="pred-val">{days}</span></div>'
        f'</div>'
        f'<div class="pred-reason"><strong>Why it may move:</strong> {why_move}</div>'
        f'<div class="pred-reason"><strong>Reason to wait:</strong> {not_yet}</div>'
        f'{tw_html}'
        f'<details><summary style="cursor:pointer;font-size:.82rem;color:#555">Key checks &amp; review form</summary>'
        f'<ul class="pred-checks">{checks_html}</ul>'
        f'<div class="pred-risk">Priced-in risk: <strong>{_esc(e.get("priced_in_risk") or "—")}</strong>'
        f' &nbsp;·&nbsp; Impact: <strong>{_esc(e.get("impact_estimate") or "—")}</strong></div>'
        f'{sec_link}'
        f'<div style="margin-top:8px">{review_form_html}</div>'
        f'</details>'
        f'</div>'
    )


def _prediction_cards_section(
    entries: list[dict],
    review_cell_fn: "callable",
) -> str:
    promoted = [e for e in entries if _is_promoted(e)]
    if not promoted:
        return ""
    cards = "".join(
        _prediction_card(e, review_cell_fn(e["ticker"]))
        for e in promoted
    )
    return (
        '<h2>Prediction Cards</h2>'
        '<p class="muted">Shadow-only — forward-looking signals from the catalyst queue. '
        'Not investment advice. Production scoring is unchanged.</p>'
        + cards
    )


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
            ("Crossings (scaled+static)", crossings),
            ("Provider", meta.get("provider", "—")),
        ]
    )

    nav = (
        '<nav style="margin-bottom:18px">'
        '<a href="/today"><strong>Today</strong></a> &nbsp;|&nbsp; '
        '<a href="/candidates">Candidates</a> &nbsp;|&nbsp; '
        '<a href="/moves">Moves</a> &nbsp;|&nbsp; '
        '<a href="/learning">Learning</a> &nbsp;|&nbsp; '
        '<a href="/ops">Ops</a> &nbsp;|&nbsp; '
        '<a href="/docs">Docs</a> &nbsp;|&nbsp; '
        '<a href="/runs">All runs</a>'
        '</nav>'
    )

    body = f"""
{nav}
<h2>Latest Run — {_esc(latest)}</h2>
<table><tr><th>Field</th><th>Value</th></tr>{rows}</table>
{review_summary}
{artifact_links}
<p>
  <a href="/runs">All historical runs</a>
  &nbsp;|&nbsp;
  <a href="/runs/{_esc(latest)}">View this run</a>
  &nbsp;|&nbsp;
  <a href="/today">Dashboard</a>
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
<tr><th>Date</th><th>Sampled</th><th>Promoted</th><th>Crossings</th></tr>
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
    scaled_crossings = [e for e in crossings if _is_scaled_crossing(e)]
    static_crossings = [e for e in crossings if not _is_scaled_crossing(e)]
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

    def _cross_rows(lst: list[dict]) -> str:
        if not lst:
            return '<tr><td colspan="6"><em>None</em></td></tr>'
        out = []
        for e in lst:
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

    def _reviewable_rows(lst: list[dict]) -> str:
        """Like _simple_rows but includes the analyst review form."""
        if not lst:
            return '<tr><td colspan="5"><em>None</em></td></tr>'
        out = []
        for e in lst:
            ticker = e["ticker"]
            url = e.get("constructed_url") or ""
            sec = f' <a href="{_esc(url)}">[SEC]</a>' if url else ""
            quote = _esc(str(e.get("evidence_quote", ""))[:200])
            out.append(
                f"<tr>"
                f"<td><strong>{_esc(ticker)}</strong><br>{_review_cell(ticker)}</td>"
                f"<td>{_score_cell(e)}</td>"
                f"<td>{_esc(e.get('catalyst_type', ''))}</td>"
                f"<td>{float(e.get('confidence', 0) or 0):.2f}</td>"
                f"<td>{quote}{sec}</td>"
                f"</tr>"
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
            ("Scaled crossings", len(scaled_crossings)),
            ("Static-only crossings", len(static_crossings)),
            ("Bearish", len(bear)),
            ("Weak/rejected", len(weak)),
            ("Provider", meta.get("provider", "—")),
        ]
    )

    cards_html = _prediction_cards_section(entries, _review_cell)

    body = f"""
<h2>Run: {_esc(date_str)}</h2>
<table>{meta_rows}</table>

{cards_html}

<h2>Scaled Crossings</h2>
<p class="muted">Scaled adjustment also crossed tier threshold.</p>
<div style="overflow-x:auto"><table>
<tr><th>Ticker</th><th>Score</th><th>Catalyst</th><th>Conf</th><th>Evidence</th></tr>
{_cross_rows(scaled_crossings)}
</table></div>

<h2>Static-only Crossings</h2>
<p class="muted">LLM shadow score crosses but scaled adjustment does not.</p>
<div style="overflow-x:auto"><table>
<tr><th>Ticker</th><th>Score</th><th>Catalyst</th><th>Conf</th><th>Evidence</th></tr>
{_cross_rows(static_crossings)}
</table></div>

<h2>Valid — No Tier Change</h2>
<div style="overflow-x:auto"><table>
<tr><th>Ticker</th><th>Score</th><th>Catalyst</th><th>Conf</th><th>Evidence</th></tr>
{_reviewable_rows(valid_no_cross)}
</table></div>

<h2>Bearish Downgrades</h2>
<div style="overflow-x:auto"><table>
<tr><th>Ticker</th><th>Score</th><th>Catalyst</th><th>Evidence</th></tr>
{_simple_rows(bear)}
</table></div>

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
        '<a href="/docs">Docs</a> &nbsp;|&nbsp; '
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
        + _stat("Crossings", crossings)
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
            f"<td><a href='/candidates'>{_esc(r.get('ticker', ''))}</a></td>"
            f"<td>{_esc(r.get('shadow_tier', ''))}</td>"
            f"<td>{_esc(r.get('scaled_shadow_score', r.get('shadow_score', '')))}</td>"
            f"</tr>"
            for r in top_rows
        )
        review_table = (
            '<h2>Needs Review (top 10)</h2>'
            '<p class="muted">Review forms are on the <a href="/candidates">Candidates</a> page — scroll to "Valid — No Tier Change".</p>'
            '<table>'
            '<tr><th>Ticker</th><th>Shadow Tier</th><th>Scaled Shadow Score</th></tr>'
            + tr_html
            + '</table>'
        )

    body = (
        f'<h2>Today — {_esc(latest)}</h2>'
        + _SEARCH_BOX
        + f'<div class="stat-grid">{stats}</div>'
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
            + _SEARCH_BOX
            + '<p class="muted">No catalyst queue runs archived yet.</p>'
            '<p><a href="/">Home</a></p>'
        )
        return _render("Candidates — MHDE", body), 200

    latest = dates[0]
    html, code = _run_detail(history_root, latest)
    html = html.replace(
        f"<title>{_esc(latest)} — MHDE Catalyst Review</title>",
        f"<title>Candidates — {_esc(latest)}</title>",
    )
    # Inject search box right after the opening h2
    html = html.replace(
        f"<h2>Run: {_esc(latest)}</h2>",
        f"<h2>Candidates — {_esc(latest)}</h2>" + _SEARCH_BOX,
        1,
    )
    return html, code


_WINDOW_PRIORITY = {1: 0, 3: 1, 5: 2, 10: 3, 20: 4, 60: 5, 252: 6}

_CLF_PRIORITY = {
    "true_miss": 0, "scored_missed": 1, "near_threshold": 2, "unscored_mover": 3,
}

_CLF_BADGE = {
    "true_miss": "badge-reject",
    "scored_missed": "badge-watch",
    "near_threshold": "badge-accept",
    "unscored_mover": "",
}


def _best_event_key(r: dict) -> tuple:
    tier_pri = 0 if r.get("universe_tier") == "primary" else 1
    try:
        date_neg = -int((r.get("event_date") or "").replace("-", "") or 0)
    except ValueError:
        date_neg = 0
    try:
        w = int(r.get("window_days") or 999)
    except (ValueError, TypeError):
        w = 999
    window_pri = _WINDOW_PRIORITY.get(w, 99)
    neg_ret = -abs(_safe_float(r.get("return_value")))
    return (tier_pri, date_neg, window_pri, neg_ret)


def _build_ticker_summary(rows: list[dict]) -> list[dict]:
    """Collapse all events to one row per ticker using priority rules."""
    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        t = r.get("ticker", "")
        by_ticker.setdefault(t, []).append(r)

    summaries = []
    for ticker, events in by_ticker.items():
        best = min(events, key=_best_event_key)
        try:
            best_w = int(best.get("window_days") or 0)
        except (ValueError, TypeError):
            best_w = 0
        max_ret = max(abs(_safe_float(e.get("return_value"))) for e in events)
        latest_date = max(e.get("event_date", "") for e in events)
        clfs = [e.get("classification", "") for e in events]
        best_clf = min(clfs, key=lambda c: _CLF_PRIORITY.get(c, 99))
        hints = [e.get("root_cause_hint", "") for e in events if e.get("root_cause_hint")]
        hint = hints[0] if hints else ""
        summaries.append({
            "ticker": ticker,
            "latest_date": latest_date,
            "best_window": best_w,
            "best_return": _safe_float(best.get("return_value")),
            "max_abs_return": max_ret,
            "classification": best_clf,
            "root_cause_hint": hint,
            "n_events": len(events),
            "universe_tier": best.get("universe_tier", ""),
        })

    summaries.sort(key=lambda s: (
        _CLF_PRIORITY.get(s["classification"], 99),
        -s["max_abs_return"],
    ))
    return summaries


def _summary_table(rows: list[dict], max_rows: int = 50) -> str:
    if not rows:
        return "<p class='muted'>None.</p>"
    th = (
        "<tr><th>Ticker</th><th>Latest date</th><th>Best window</th>"
        "<th>Max return</th><th>Classification</th>"
        "<th>Root cause</th><th>Events</th></tr>"
    )
    trs = "".join(
        f'<tr>'
        f'<td><a href="/ticker/{_esc(s["ticker"])}">{_esc(s["ticker"])}</a></td>'
        f'<td>{_esc(s["latest_date"])}</td>'
        f'<td>{s["best_window"]}d</td>'
        f'<td><strong>{s["max_abs_return"]:.1f}%</strong></td>'
        f'<td><span class="badge {_esc(_CLF_BADGE.get(s["classification"], ""))}">'
        f'{_esc(s["classification"])}</span></td>'
        f'<td class="muted">{_esc(s["root_cause_hint"])}</td>'
        f'<td class="muted">{s["n_events"]}</td>'
        f'</tr>'
        for s in rows[:max_rows]
    )
    return f'<div style="overflow-x:auto"><table>{th}{trs}</table></div>'


def _moves_page(output_dir: str) -> str:
    import csv as _csv
    from pathlib import Path

    nav = (
        '<p><a href="/today">Today</a> &nbsp;|&nbsp; '
        '<a href="/candidates">Candidates</a> &nbsp;|&nbsp; '
        '<a href="/learning">Learning</a> &nbsp;|&nbsp; '
        '<a href="/ops">Ops</a> &nbsp;|&nbsp; '
        '<a href="/docs">Docs</a> &nbsp;|&nbsp; '
        '<a href="/">Home</a></p>'
    )

    pva_path = Path(output_dir) / "prediction_vs_actual_rows.csv"
    if not pva_path.exists():
        body = (
            '<h2>Moves — Actual Movers</h2>'
            '<p class="muted">prediction_vs_actual_rows.csv not found.</p>'
            '<p class="muted">Run: <code>python main.py missed prediction-vs-actual</code></p>'
            + nav
        )
        return _render("Moves — MHDE", body)

    with open(pva_path, newline="") as f:
        all_rows = list(_csv.DictReader(f))

    summaries = _build_ticker_summary(all_rows)
    n_tickers = len(summaries)
    n_events = len(all_rows)

    # Named sections
    spikes_1d = [s for s in summaries if s["best_window"] == 1]
    accumulations = [s for s in summaries if s["best_window"] in (3, 5)]
    trends = [s for s in summaries if s["best_window"] in (10, 20, 60, 252)]
    repeated = [s for s in summaries if s["n_events"] >= 4]

    def _section(title: str, rows: list[dict], desc: str = "") -> str:
        if not rows:
            return (
                f'<h3>{title}</h3>'
                f'<p class="muted">None in this period.</p>'
            )
        return (
            f'<h3>{title} <span class="muted">({len(rows)})</span></h3>'
            + (f'<p class="muted">{desc}</p>' if desc else "")
            + _summary_table(rows, max_rows=25)
        )

    # Raw events grouped by window (collapsible)
    by_window: dict[str, list[dict]] = {}
    for r in all_rows:
        w = r.get("window_days", "?")
        by_window.setdefault(w, []).append(r)

    raw_sections = []
    for window in sorted(by_window.keys(), key=lambda x: (int(x) if str(x).isdigit() else 9999)):
        group = sorted(by_window[window], key=lambda r: -_safe_float(r.get("return_value")))[:20]
        trs = "".join(
            f'<tr>'
            f'<td><a href="/ticker/{_esc(r.get("ticker",""))}">{_esc(r.get("ticker",""))}</a></td>'
            f'<td>{_esc(r.get("event_date",""))}</td>'
            f'<td>{_safe_float(r.get("return_value")):.1f}%</td>'
            f'<td>{_esc(r.get("classification",""))}</td>'
            f'<td class="muted">{_esc(r.get("universe_tier",""))}</td>'
            f'</tr>'
            for r in group
        )
        raw_sections.append(
            f'<h4>{window}-day window (top 20 of {len(by_window[window])})</h4>'
            f'<div style="overflow-x:auto"><table>'
            f'<tr><th>Ticker</th><th>Date</th><th>Return</th><th>Class.</th><th>Tier</th></tr>'
            f'{trs}</table></div>'
        )

    raw_block = (
        '<details style="margin-top:20px">'
        f'<summary style="cursor:pointer;font-size:.9rem;color:#555">'
        f'Raw rolling-window events ({n_events} rows across {len(by_window)} windows)</summary>'
        + "".join(raw_sections)
        + "</details>"
    )

    body = (
        '<h2>Moves — Actual Movers</h2>'
        '<p class="muted">Detected price moves with prediction status. '
        f'{n_tickers} unique tickers, {n_events} raw events.</p>'
        + _section("New 1d Spikes", spikes_1d, "Single-day moves ≥ threshold.")
        + _section("Active Accumulations", accumulations, "3d or 5d moves — short-term build-up.")
        + _section("Longer Trend Moves", trends, "10d/20d/60d moves — sustained trend or drift.")
        + _section("Repeated Movers", repeated,
                   "Tickers with 4+ events across multiple windows — persistent signal.")
        + raw_block
        + nav
    )
    return _render("Moves — Actual Movers", body)


def _ops_page(history_root: str, output_dir: str) -> str:
    import subprocess
    from pathlib import Path

    nav = (
        '<p><a href="/today">Today</a> &nbsp;|&nbsp; '
        '<a href="/candidates">Candidates</a> &nbsp;|&nbsp; '
        '<a href="/learning">Learning</a> &nbsp;|&nbsp; '
        '<a href="/moves">Moves</a> &nbsp;|&nbsp; '
        '<a href="/docs">Docs</a> &nbsp;|&nbsp; '
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

    # Systemd timers — check user-level units (all mhde services run as --user)
    timer_lines = []
    try:
        result = subprocess.run(
            ["systemctl", "--user", "list-timers", "--no-legend", "--no-pager"],
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


# ── Ticker search ─────────────────────────────────────────────────────────────

_TICKER_RE = __import__("re").compile(r"^[A-Z0-9.\-]{1,10}$")

_SEARCH_BOX = (
    '<form style="margin:8px 0 14px" '
    'onsubmit="var t=this.q.value.trim().toUpperCase();'
    "if(t){window.location='/ticker/'+encodeURIComponent(t);}return false;\">"
    '<input name="q" placeholder="Ticker (e.g. AAPL)" maxlength="10" '
    'style="padding:5px 8px;border:1px solid #ccc;border-radius:3px;font-size:.9rem;width:140px">'
    ' <button type="submit" style="padding:5px 10px;background:#1565c0;color:#fff;'
    'border:none;border-radius:3px;cursor:pointer">Look up</button>'
    '</form>'
)


def _not_candidate_reason(
    company: dict | None,
    score_row: tuple | None,
    in_queue: bool,
) -> str:
    if company is None:
        return "Not in universe — ticker not found in companies table"
    if not company.get("is_active"):
        excl = company.get("universe_exclusion_reason") or ""
        return f"Not active in universe{(': ' + excl) if excl else ''}"
    if score_row is None:
        return "No score found — not yet scored by the pipeline"
    _, total_score, tier, why_rejected, missing_json = score_row
    if tier == "Incomplete":
        try:
            import json as _json
            missing = _json.loads(missing_json or "[]")
            m = ", ".join(missing[:4]) if missing else "unknown"
        except Exception:
            m = str(missing_json or "")[:60]
        return f"Incomplete score — missing data: {m}"
    if total_score is not None and total_score >= 45:
        return "Already C-tier or above — score meets threshold"
    if total_score is not None and total_score >= 40:
        if in_queue:
            return "Near threshold (40–45) — present in catalyst queue"
        return "Near threshold (40–45) — no qualifying catalyst in recent queue run"
    if total_score is not None and total_score < 40:
        return f"Score too low ({total_score:.1f} < 40) — not near candidate threshold"
    return "Unknown — check pipeline logs"


def _ticker_page(
    ticker: str,
    db_path: str,
    history_root: str,
    output_dir: str,
) -> tuple[str, int]:
    import csv as _csv

    ticker = ticker.upper().strip()
    if not _TICKER_RE.match(ticker):
        return _render("Ticker — MHDE", "<p>Invalid ticker format.</p>"), 400

    nav = (
        '<p><a href="/today">Today</a> &nbsp;|&nbsp; '
        '<a href="/candidates">Candidates</a> &nbsp;|&nbsp; '
        '<a href="/moves">Moves</a> &nbsp;|&nbsp; '
        '<a href="/learning">Learning</a> &nbsp;|&nbsp; '
        '<a href="/docs">Docs</a></p>'
        + _SEARCH_BOX
    )

    # ── DuckDB lookups ─────────────────────────────────────────────────────────
    company: dict | None = None
    score_row: tuple | None = None    # (as_of_date, total_score, tier, why_rejected, missing_json)
    missed_events: list[dict] = []
    db_error = ""

    if os.path.exists(db_path):
        try:
            import duckdb as _duckdb
            conn = _duckdb.connect(db_path, read_only=True)

            row = conn.execute(
                "SELECT ticker, company_name, universe_tier, is_active, sector, industry, "
                "universe_exclusion_reason, last_financial_filing_date "
                "FROM companies WHERE ticker = ?",
                [ticker],
            ).fetchone()
            if row:
                company = {
                    "ticker": row[0], "company_name": row[1], "universe_tier": row[2],
                    "is_active": row[3], "sector": row[4], "industry": row[5],
                    "universe_exclusion_reason": row[6], "last_financial_filing_date": row[7],
                }

            srow = conn.execute(
                "SELECT as_of_date, total_score, tier, why_rejected, missing_data_json "
                "FROM scores WHERE ticker = ? ORDER BY as_of_date DESC LIMIT 1",
                [ticker],
            ).fetchone()
            if srow:
                score_row = srow

            mrows = conn.execute(
                "SELECT event_date, event_type, return_value, window_days, "
                "tier_before_event, had_catalyst_evidence, investigation_status "
                "FROM missed_opportunity_events WHERE ticker = ? ORDER BY event_date DESC LIMIT 10",
                [ticker],
            ).fetchall()
            seen_keys: set[tuple] = set()
            for r in mrows:
                key = (str(r[0]), r[1], r[3])  # event_date, event_type, window_days
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                missed_events.append({
                    "event_date": str(r[0]), "event_type": r[1], "return_value": r[2],
                    "window_days": r[3], "tier_before_event": r[4],
                    "had_catalyst_evidence": r[5], "investigation_status": r[6],
                })
            conn.close()
        except Exception as exc:
            db_error = f"DB error: {_esc(str(exc)[:120])}"
    else:
        db_error = f"Database not found at {_esc(db_path)}"

    # ── Prediction-vs-actual CSV ────────────────────────────────────────────────
    pva_rows: list[dict] = []
    pva_path = os.path.join(output_dir, "prediction_vs_actual_rows.csv")
    if os.path.exists(pva_path):
        try:
            with open(pva_path, newline="") as f:
                pva_rows = [r for r in _csv.DictReader(f) if r.get("ticker") == ticker]
        except Exception:
            pass

    # ── Catalyst queue history scan ────────────────────────────────────────────
    queue_entries: list[dict] = []
    dates = _dated_dirs(history_root)
    for d in dates[:10]:
        csv_path = os.path.join(history_root, d, "daily_catalyst_queue.csv")
        if not os.path.exists(csv_path):
            continue
        try:
            with open(csv_path, newline="") as f:
                for row in _csv.DictReader(f):
                    if row.get("ticker") == ticker:
                        row["_run_date"] = d
                        queue_entries.append(row)
        except Exception:
            pass

    in_queue = bool(queue_entries)
    reason = _not_candidate_reason(company, score_row, in_queue)

    # ── Build HTML ─────────────────────────────────────────────────────────────
    def _row(label: str, val: str) -> str:
        return f'<tr><td class="tlabel">{_esc(label)}</td><td>{val}</td></tr>'

    # Universe block
    if company:
        tier_badge = _esc(company.get("universe_tier") or "—")
        active_val = "Yes" if company.get("is_active") else '<span style="color:#c62828">No</span>'
        uni_rows = (
            _row("Company", _esc(company.get("company_name") or "—"))
            + _row("Universe tier", tier_badge)
            + _row("Active", active_val)
            + _row("Sector", _esc(company.get("sector") or "—"))
            + _row("Industry", _esc(company.get("industry") or "—"))
            + _row("Exclusion reason", _esc(company.get("universe_exclusion_reason") or "—"))
            + _row("Last filing", _esc(str(company.get("last_financial_filing_date") or "—")))
        )
        uni_block = f'<h3>Universe</h3><table class="detail-table">{uni_rows}</table>'
    else:
        uni_block = (
            '<h3>Universe</h3>'
            f'<p class="warn-box">Ticker <strong>{_esc(ticker)}</strong> not found in the companies table.</p>'
        )

    # Score block
    if score_row:
        as_of, total, tier, why_rej, _ = score_row
        tier_style = "color:#1b5e20;font-weight:700" if tier == "C" else (
            "color:#e65100" if tier == "Reject" else "color:#9e9e9e"
        )
        score_rows_html = (
            _row("Score date", _esc(str(as_of)))
            + _row("Total score", f'<strong>{total:.2f}</strong>' if total else "—")
            + _row("Tier", f'<span style="{tier_style}">{_esc(tier)}</span>')
            + _row("Why rejected", _esc(str(why_rej or "—")[:200]))
        )
        score_block = f'<h3>Latest Score</h3><table class="detail-table">{score_rows_html}</table>'
    else:
        score_block = '<h3>Latest Score</h3><p class="muted">No score found.</p>'

    # Candidate status block
    reason_style = "color:#1b5e20" if "meets threshold" in reason or "present in" in reason else "color:#c62828"
    not_cand_label = "Not a candidate because: " if "meets threshold" not in reason else "Status: "
    cand_block = (
        f'<h3>Candidate Status</h3>'
        f'<p><strong>In recent queue:</strong> {"Yes" if in_queue else "No"}</p>'
        f'<p><strong>{_esc(not_cand_label)}</strong>'
        f'<span style="{reason_style}">{_esc(reason)}</span></p>'
    )

    # Missed events block
    has_unscored = any(not e.get("had_catalyst_evidence") for e in missed_events)
    unscored_note = (
        '<p class="muted" style="font-size:.82rem">'
        'Rows without catalyst evidence are unscored movers — no prior score existed '
        'before those dates or the event was outside the scored universe at the time.</p>'
        if has_unscored else ""
    )
    if missed_events:
        me_rows = "".join(
            f'<tr>'
            f'<td>{_esc(e["event_date"])}</td>'
            f'<td>{_esc(e["event_type"])}</td>'
            f'<td>{_format_return_pct(e["return_value"])}</td>'
            f'<td>{e["window_days"]}d</td>'
            f'<td>{_esc(e["tier_before_event"] or "—")}</td>'
            f'<td>{"✓" if e["had_catalyst_evidence"] else "✗"}</td>'
            f'<td>{_esc(e["investigation_status"] or "—")}</td>'
            f'</tr>'
            for e in missed_events
        )
        miss_block = (
            '<h3>Missed Move Events</h3>'
            + unscored_note
            + '<div style="overflow-x:auto"><table>'
            '<tr><th>Date</th><th>Type</th><th>Return</th><th>Window</th>'
            '<th>Tier before</th><th>Catalyst?</th><th>Status</th></tr>'
            + me_rows + '</table></div>'
        )
    else:
        miss_block = '<h3>Missed Move Events</h3><p class="muted">None found.</p>'

    # Prediction-vs-actual block
    if pva_rows:
        pva_html = "".join(
            f'<tr>'
            f'<td>{_esc(r.get("event_date",""))}</td>'
            f'<td>{_esc(r.get("event_type",""))}</td>'
            f'<td>{_esc(r.get("classification",""))}</td>'
            f'<td>{_format_return_pct(r.get("return_value"))}</td>'
            f'<td>{_esc(r.get("score_before_event",""))[:6]}</td>'
            f'<td>{_esc(r.get("tier_before_event",""))}</td>'
            f'</tr>'
            for r in pva_rows[:10]
        )
        pva_block = (
            '<h3>Prediction-vs-Actual</h3>'
            '<div style="overflow-x:auto"><table>'
            '<tr><th>Date</th><th>Event</th><th>Classification</th>'
            '<th>Return</th><th>Score before</th><th>Tier before</th></tr>'
            + pva_html + '</table></div>'
        )
    else:
        pva_block = '<h3>Prediction-vs-Actual</h3><p class="muted">No entries found.</p>'

    # Queue entries block
    if queue_entries:
        q_html = "".join(
            f'<tr>'
            f'<td>{_esc(e.get("_run_date",""))}</td>'
            f'<td>{_esc(e.get("catalyst_type",""))}</td>'
            f'<td>{_esc(e.get("shadow_tier",""))}</td>'
            f'<td>{_esc(e.get("scaled_shadow_score",""))}</td>'
            f'<td>{_esc(e.get("final_should_affect_score",""))}</td>'
            f'<td>{_esc(str(e.get("expected_move_summary",""))[:60])}</td>'
            f'</tr>'
            for e in queue_entries[:10]
        )
        queue_block = (
            '<h3>Catalyst Queue History</h3>'
            '<div style="overflow-x:auto"><table>'
            '<tr><th>Run</th><th>Catalyst</th><th>Shadow tier</th>'
            '<th>Scaled score</th><th>Promoted</th><th>Summary</th></tr>'
            + q_html + '</table></div>'
        )
    else:
        queue_block = '<h3>Catalyst Queue History</h3><p class="muted">No entries found.</p>'

    db_warn = f'<div class="warn-box">{db_error}</div>' if db_error else ""

    body = (
        f'<h2>Ticker: {_esc(ticker)}</h2>'
        + db_warn
        + uni_block
        + score_block
        + cand_block
        + queue_block
        + pva_block
        + miss_block
        + nav
    )
    return _render(f"{ticker} — MHDE", body), 200


# ── App factory ───────────────────────────────────────────────────────────────

def create_app(
    history_root: str,
    output_dir: str,
    unsafe_no_auth: bool = False,
    unsafe_public_bind: bool = False,
    db_path: str = "",
) -> Flask:
    _db_path = db_path or os.path.join(os.getcwd(), "data", "mhde.duckdb")

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

    @app.route("/docs")
    @_require_auth
    def docs_index():
        return _docs_index_page()

    @app.route("/docs/<doc_key>")
    @_require_auth
    def doc_view(doc_key: str):
        html, code = _doc_page(doc_key)
        return html, code

    @app.route("/docs/download/<doc_key>")
    @_require_auth
    def doc_download(doc_key: str):
        return _doc_download(doc_key)

    @app.route("/ticker/<ticker_sym>")
    @_require_auth
    def ticker_lookup(ticker_sym: str):
        html, code = _ticker_page(ticker_sym, _db_path, history_root, output_dir)
        return html, code

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
