from __future__ import annotations


def build_thesis_text(scores: dict, company_name: str) -> str:
    cheap = scores.get("cheap_score")
    quality = scores.get("quality_score")
    catalyst = scores.get("catalyst_score")
    risk = scores.get("risk_penalty") or 0
    total = scores.get("total_score") or 0
    coverage = scores.get("coverage", 1.0)

    parts = [f"{company_name} ({scores.get('ticker', '?')}) — Score: {total:.0f}"]

    if quality is not None:
        if quality > 75:
            parts.append(f"Quality: score {quality:.0f}/100 — solid business fundamentals.")
        elif quality > 50:
            parts.append(f"Quality: score {quality:.0f}/100 — moderate fundamentals.")
        else:
            parts.append(f"Quality: score {quality:.0f}/100 — weak or mixed fundamentals.")
    else:
        parts.append("Quality: no fundamental data available.")

    if cheap is not None:
        if cheap > 70:
            parts.append(f"Valuation: score {cheap:.0f}/100 — appears potentially undervalued.")
        elif cheap > 40:
            parts.append(f"Valuation: score {cheap:.0f}/100 — moderate valuation signal.")
        else:
            parts.append(f"Valuation: score {cheap:.0f}/100 — no clear cheapness signal.")
    else:
        parts.append("Valuation: no price or fundamental data to assess.")

    if catalyst is not None:
        if catalyst > 50:
            parts.append(f"Catalyst: score {catalyst:.0f}/100 — active catalyst signals detected.")
        elif catalyst > 20:
            parts.append(f"Catalyst: score {catalyst:.0f}/100 — limited catalyst activity (recent filing).")
        else:
            parts.append("Catalyst: no catalyst evidence found.")
    else:
        parts.append("Catalyst: catalyst data unavailable.")

    if risk > 50:
        parts.append(f"Risk: elevated risk penalty ({risk:.0f}/100) — review carefully before acting.")

    if coverage < 0.60:
        parts.append(
            f"Note: data coverage is {coverage:.0%} — key components are missing. "
            "Verify independently before any action."
        )

    parts.append(
        "This is a research candidate, not a buy/sell recommendation. "
        "Not validated for decision use."
    )
    return " ".join(parts)


def build_why_now(scores: dict) -> str:
    catalyst = scores.get("catalyst_score")
    momentum = scores.get("momentum_score")

    signals = []
    if catalyst is not None and catalyst > 50:
        signals.append("catalyst signals present")
    elif catalyst is not None and catalyst > 20:
        signals.append("recent filing activity only")
    elif catalyst is not None:
        signals.append("no catalyst signals found")
    else:
        signals.append("catalyst data unavailable")

    if momentum is not None and momentum > 60:
        signals.append("positive price momentum")
    elif momentum is None:
        signals.append("price history unavailable")

    return "Why now: " + "; ".join(signals) + "."
