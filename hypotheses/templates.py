from __future__ import annotations


def build_thesis_text(scores: dict, company_name: str) -> str:
    cheap = scores.get("cheap_score", 0) or 0
    quality = scores.get("quality_score", 0) or 0
    catalyst = scores.get("catalyst_score", 0) or 0
    risk = scores.get("risk_penalty", 0) or 0
    total = scores.get("total_score", 0) or 0

    parts = [f"{company_name} ({scores.get('ticker', '?')}) — Score: {total:.0f}"]

    if cheap > 60:
        parts.append(f"Valuation: Cheapness score {cheap:.0f}/100 suggests potential undervaluation.")
    if quality > 60:
        parts.append(f"Quality: Quality score {quality:.0f}/100 indicates solid business fundamentals.")
    if catalyst > 30:
        parts.append(f"Catalyst: Score {catalyst:.0f}/100 suggests potential near-term catalysts.")
    if risk > 50:
        parts.append(f"Risk: Elevated risk penalty ({risk:.0f}/100) — review before acting.")

    parts.append(
        "Note: This is a research candidate, not a buy/sell recommendation. "
        "Experimental. Not validated for decision use."
    )
    return " ".join(parts)


def build_why_now(scores: dict) -> str:
    catalyst = scores.get("catalyst_score", 0) or 0
    momentum = scores.get("momentum_score", 0) or 0

    signals = []
    if catalyst > 50:
        signals.append("catalyst signals present")
    if momentum > 60:
        signals.append("positive price momentum")
    if not signals:
        signals.append("no immediate catalyst — long-term thesis")

    return "Why now: " + ", ".join(signals) + "."
