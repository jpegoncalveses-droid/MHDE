"""Daily catalyst queue email digest — txt + html generation and SMTP sending."""
from __future__ import annotations

import logging
import os
import smtplib
from collections import Counter
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from missed.catalyst_queue import _enrich_with_interpretation  # re-exported for convenience
from missed.catalyst_shadow_scorer import _safe_cell

logger = logging.getLogger("mhde.missed.catalyst_digest")

_DIGEST_TXT = "daily_catalyst_digest.txt"
_DIGEST_HTML = "daily_catalyst_digest.html"
_SOURCE_THRESHOLD = 200


def _promoted(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e.get("final_should_affect_score")]


def _crossings(entries: list[dict]) -> list[dict]:
    return [e for e in _promoted(entries) if e.get("tier_move") and "→C" in str(e["tier_move"])]


def _valid_no_cross(entries: list[dict]) -> list[dict]:
    return [e for e in _promoted(entries) if not e.get("tier_move")]


def _bearish(entries: list[dict]) -> list[dict]:
    return [e for e in entries
            if not e.get("final_should_affect_score")
            and e.get("sentiment") == "bearish"
            and (e.get("llm_adjustment") or 0) < 0]


def _weak(entries: list[dict]) -> list[dict]:
    return [e for e in entries
            if e.get("validation_status") in ("weak_evidence", "invalid_quote", "neutral_sentiment")]


def _run_date(metadata: dict) -> str:
    rt = metadata.get("run_time", "")
    if isinstance(rt, str) and len(rt) >= 10:
        return rt[:10]
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _subject(entries: list[dict], metadata: dict) -> str:
    n_cross = len(_crossings(entries))
    n_action = metadata.get("valid_actionable", len(_promoted(entries)))
    date = _run_date(metadata)
    return f"MHDE Catalyst Queue — {n_cross} Reject→C, {n_action} actionable [{date}]"


def generate_digest_txt(
    queue_entries: list[dict],
    revalidated: list[dict],
    metadata: dict,
) -> str:
    """Return the full text/plain digest body including the subject as the first line."""
    subj = _subject(queue_entries, metadata)
    cross = _crossings(queue_entries)
    valid = _valid_no_cross(queue_entries)
    bear = _bearish(queue_entries)
    wk = _weak(queue_entries)
    date = _run_date(metadata)
    review_url = os.environ.get("DAILY_CATALYST_REVIEW_URL", "")

    lines: list[str] = [
        f"Subject: {subj}",
        "=" * 70,
        "",
        "MHDE CATALYST QUEUE — SHADOW-ONLY ANALYSIS",
        "Production scores were NOT changed.",
        "",
        f"Run date:         {date}",
        f"Sampled:          {metadata.get('sampled', '—')}",
        f"Source available: {metadata.get('source_available', '—')}",
        f"Valid actionable: {metadata.get('valid_actionable', len(_promoted(queue_entries)))}",
        f"Reject→C:         {len(cross)}",
        f"Bearish:          {len(bear)}",
        f"Weak/rejected:    {len(wk)}",
        "",
        "=" * 70,
        "REJECT→C TIER CROSSINGS",
        "=" * 70,
    ]

    if cross:
        for e in cross:
            url = e.get("constructed_url") or ""
            quote = _safe_cell(e.get("evidence_quote", ""), max_len=250)
            lines += [
                "",
                f"  {e['ticker']}  {e.get('original_score', 0):.1f} → {e.get('shadow_score', 0):.1f}"
                f"  (llm:{e.get('llm_adjustment', 0):+.1f} / scaled:{e.get('scaled_adjustment', 0) or 0:+.2f})",
                f"  Catalyst: {e.get('catalyst_type', '')}  |  Confidence: {e.get('confidence', 0):.2f}",
                f"  Evidence: {quote}",
            ]
            direction = e.get("expected_direction", "")
            guidance = e.get("action_guidance", "")
            timeframe = e.get("expected_timeframe", "")
            key_checks = e.get("key_checks", "")
            if direction or guidance:
                lines.append(
                    f"  Action guidance: {guidance}  |  Direction: {direction}  |  Timeframe: {timeframe}"
                )
            if key_checks:
                lines.append(f"  Key checks: {key_checks}")
            if url:
                lines.append(f"  SEC:      {url}")
            if review_url:
                lines.append(f"  Review:   {review_url}/runs/{_run_date(metadata)}")
    else:
        lines.append("  (none)")

    lines += [
        "",
        "=" * 70,
        "VALID — NO TIER CHANGE",
        "=" * 70,
    ]
    if valid:
        for e in valid:
            lines.append(
                f"  {e['ticker']:8}  {e.get('original_score', 0):.1f} → {e.get('shadow_score', 0):.1f}"
                f"  {e.get('catalyst_type', '')}  |  {_safe_cell(e.get('evidence_quote',''), max_len=80)}"
            )
    else:
        lines.append("  (none)")

    lines += [
        "",
        "=" * 70,
        "BEARISH DOWNGRADES",
        "=" * 70,
    ]
    if bear:
        for e in bear:
            lines.append(
                f"  {e['ticker']:8}  {e.get('original_score', 0):.1f} → {e.get('shadow_score', 0):.1f}"
                f"  {e.get('catalyst_type', '')}  |  {_safe_cell(e.get('evidence_quote',''), max_len=80)}"
            )
    else:
        lines.append("  (none)")

    lines += [
        "",
        "=" * 70,
        "WEAK / REJECTED EVIDENCE SUMMARY",
        "=" * 70,
    ]
    if wk:
        by_status: Counter = Counter(e.get("validation_status", "unknown") for e in wk)
        by_type: Counter = Counter(e.get("catalyst_type", "unknown") for e in wk)
        lines.append(f"  Total: {len(wk)}")
        lines.append("  By status:")
        for status, cnt in by_status.most_common():
            lines.append(f"    {status}: {cnt}")
        lines.append("  By catalyst type:")
        for ctype, cnt in by_type.most_common():
            lines.append(f"    {ctype}: {cnt}")
        # Only list individual entries when ≤5
        if len(wk) <= 5:
            lines.append("  Entries:")
            for e in wk:
                lines.append(f"    {e['ticker']} — {e.get('validation_status','')} ({e.get('catalyst_type','')})")
    else:
        lines.append("  (none)")

    lines += ["", "=" * 70]
    if review_url:
        lines.append(f"Dashboard:  {review_url}")
    lines += [
        "Artifacts:  daily_catalyst_queue.md / .csv / .jsonl / .html",
        "",
    ]
    return "\n".join(lines)


def generate_digest_html(
    queue_entries: list[dict],
    revalidated: list[dict],
    metadata: dict,
) -> str:
    """Return the full text/html digest body."""
    subj = _subject(queue_entries, metadata)
    cross = _crossings(queue_entries)
    valid = _valid_no_cross(queue_entries)
    bear = _bearish(queue_entries)
    wk = _weak(queue_entries)
    date = _run_date(metadata)
    review_url = os.environ.get("DAILY_CATALYST_REVIEW_URL", "")

    def _esc(s: str) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _cross_rows() -> str:
        rows = []
        for e in cross:
            url = e.get("constructed_url") or ""
            quote = _esc(_safe_cell(e.get("evidence_quote", ""), max_len=250))
            sec = f' <a href="{_esc(url)}">[SEC]</a>' if url else ""
            review_link = (
                f' <a href="{_esc(review_url)}/runs/{date}">[Review]</a>' if review_url else ""
            )
            rows.append(
                f'<tr style="background:#e8f5e9">'
                f'<td><strong>{_esc(e["ticker"])}</strong></td>'
                f'<td>{e.get("original_score",0):.1f} → <strong>{e.get("shadow_score",0):.1f}</strong>'
                f'<br><small style="color:#388e3c">scaled:{e.get("scaled_shadow_score") or e.get("original_score",0):.1f}</small></td>'
                f'<td>{e.get("llm_adjustment",0):+.1f}<br><small>{e.get("scaled_adjustment") or 0:+.2f}s</small></td>'
                f'<td>{_esc(e.get("catalyst_type",""))}</td>'
                f'<td>{e.get("confidence",0):.2f}</td>'
                f'<td>{quote}{sec}{review_link}</td>'
                f'</tr>'
            )
        return "\n".join(rows) if rows else '<tr><td colspan="6"><em>None</em></td></tr>'

    by_status: Counter = Counter(e.get("validation_status", "unknown") for e in wk)
    weak_summary_rows = "\n".join(
        f'<tr><td>{_esc(s)}</td><td>{c}</td></tr>'
        for s, c in by_status.most_common()
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(subj)}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:680px;margin:0 auto;padding:16px;color:#222;}}
h2{{font-size:1rem;border-bottom:1px solid #ddd;padding-bottom:4px;margin-top:20px;}}
.disclaimer{{background:#fff3e0;border-left:4px solid #ff9800;padding:8px 12px;margin:12px 0;font-size:.9rem;}}
table{{border-collapse:collapse;width:100%;font-size:.85rem;margin:8px 0;}}
th,td{{padding:5px 8px;border-bottom:1px solid #eee;text-align:left;}}
th{{background:#f5f5f5;}}
a{{color:#1565c0;}}
</style>
</head>
<body>
<h1 style="font-size:1.2rem">{_esc(subj)}</h1>
<div class="disclaimer">&#9888; <strong>Shadow-only</strong> — production scores unchanged.</div>
<table>
<tr><td>Run date</td><td>{_esc(date)}</td></tr>
<tr><td>Sampled</td><td>{metadata.get('sampled','—')}</td></tr>
<tr><td>Source available</td><td>{metadata.get('source_available','—')}</td></tr>
<tr><td>Valid actionable</td><td>{metadata.get('valid_actionable',len(_promoted(queue_entries)))}</td></tr>
<tr><td>Reject→C</td><td>{len(cross)}</td></tr>
<tr><td>Bearish</td><td>{len(bear)}</td></tr>
<tr><td>Weak/rejected</td><td>{len(wk)}</td></tr>
</table>

<h2>Reject→C Tier Crossings</h2>
<table>
<tr><th>Ticker</th><th>Score</th><th>Adj</th><th>Catalyst</th><th>Conf</th><th>Evidence</th></tr>
{_cross_rows()}
</table>

<h2>Weak / Rejected Summary</h2>
<table>
<tr><th>Status</th><th>Count</th></tr>
{weak_summary_rows if weak_summary_rows else '<tr><td colspan="2"><em>None</em></td></tr>'}
</table>

{"<p><a href=" + chr(34) + _esc(review_url) + chr(34) + ">Open Dashboard</a></p>" if review_url else ""}
<p style="font-size:.8rem;color:#888;">Artifacts: daily_catalyst_queue.md / .csv / .jsonl / .html</p>
</body>
</html>"""


def write_digest_artifacts(
    queue_entries: list[dict],
    revalidated: list[dict],
    metadata: dict,
    output_dir: str,
) -> tuple[str, str]:
    """Write daily_catalyst_digest.txt and .html. Returns (txt_path, html_path)."""
    os.makedirs(output_dir, exist_ok=True)
    txt = generate_digest_txt(queue_entries, revalidated, metadata)
    html = generate_digest_html(queue_entries, revalidated, metadata)
    txt_path = os.path.join(output_dir, _DIGEST_TXT)
    html_path = os.path.join(output_dir, _DIGEST_HTML)
    with open(txt_path, "w") as f:
        f.write(txt)
    with open(html_path, "w") as f:
        f.write(html)
    return txt_path, html_path


def send_catalyst_digest(
    cfg: dict,
    queue_entries: list[dict],
    revalidated: list[dict],
    metadata: dict,
    *,
    email_to: str,
) -> bool:
    """Send the catalyst digest via SMTP. Raises RuntimeError if config incomplete."""
    required = {
        "SMTP_HOST": cfg.get("smtp_host"),
        "SMTP_PORT": cfg.get("smtp_port"),
        "SMTP_USERNAME": cfg.get("smtp_username"),
        "SMTP_PASSWORD": cfg.get("smtp_password"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(
            f"--send-email requires SMTP configuration. Missing: {', '.join(missing)}. "
            "Set the corresponding environment variables."
        )
    if not email_to:
        raise RuntimeError("--send-email requires a recipient (--email-to or DAILY_CATALYST_EMAIL_TO).")

    txt_body = generate_digest_txt(queue_entries, revalidated, metadata)
    html_body = generate_digest_html(queue_entries, revalidated, metadata)
    subject = _subject(queue_entries, metadata)
    from_addr = cfg.get("email_from") or cfg.get("smtp_username") or ""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = email_to
    msg.attach(MIMEText(txt_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    host = cfg["smtp_host"]
    port = int(cfg.get("smtp_port") or 587)
    username = cfg.get("smtp_username") or ""
    # Never log the password
    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            if username and cfg.get("smtp_password"):
                server.login(username, cfg["smtp_password"])
            server.sendmail(from_addr, [email_to], msg.as_string())
        logger.info("Catalyst digest sent to %s", email_to)
        return True
    except Exception as e:
        logger.error("Failed to send catalyst digest: %s", e)
        return False
