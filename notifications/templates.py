from __future__ import annotations


def format_telegram_alert(hypothesis: dict) -> str:
    tier = hypothesis.get("tier", "?")
    ticker = hypothesis.get("ticker", "?")
    company = hypothesis.get("company_name", ticker)
    score = hypothesis.get("total_score", 0)
    why = hypothesis.get("why_ranked") or hypothesis.get("thesis") or "No analysis available."

    lines = [
        f"*MHDE Candidate — {tier}-Tier*",
        f"*{ticker}* — {company}",
        f"Score: `{score:.0f}/100`",
        "",
        why[:400],
        "",
        "_This is a research candidate, not a buy/sell recommendation._",
    ]
    return "\n".join(lines)


def format_email_digest(run_summary: dict) -> tuple[str, str]:
    run_id = run_summary.get("run_id", "unknown")
    candidates = run_summary.get("candidates", [])
    sent = run_summary.get("sent", 0)

    subject = f"MHDE Daily Digest — {len(candidates)} candidates, {sent} alerts"

    rows = []
    for c in candidates[:20]:
        rows.append(
            f"  [{c.get('tier','?'):>6}] {c.get('ticker','?'):<8} "
            f"score={c.get('total_score',0):.0f}"
        )

    body = "\n".join([
        f"MHDE Daily Digest",
        f"Run ID: {run_id}",
        f"Candidates: {len(candidates)}",
        f"Alerts sent: {sent}",
        "",
        "Top candidates:",
        *rows,
        "",
        "This is a research summary, not a buy/sell recommendation.",
    ])
    return subject, body
