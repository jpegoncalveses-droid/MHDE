from __future__ import annotations


def generate_why_ranked(scores: dict) -> str:
    parts = []
    cheap = scores.get("cheap_score")
    quality = scores.get("quality_score")
    catalyst = scores.get("catalyst_score")
    momentum = scores.get("momentum_score")
    if cheap is not None and cheap > 60:
        parts.append(f"Cheap: score={cheap:.0f}")
    if quality is not None and quality > 60:
        parts.append(f"Quality: score={quality:.0f}")
    if catalyst is not None and catalyst > 40:
        parts.append(f"Catalyst: score={catalyst:.0f}")
    if momentum is not None and momentum > 60:
        parts.append(f"Momentum: score={momentum:.0f}")
    return ". ".join(parts) if parts else "Scores above threshold."


def generate_why_rejected(scores: dict, missing: list[str], tier: str = "Reject") -> str:
    parts = []
    total = scores.get("total_score", 0)
    risk = scores.get("risk_penalty")
    catalyst = scores.get("catalyst_score")
    coverage = scores.get("coverage", 1.0)

    if tier == "Incomplete":
        parts.append(f"Insufficient data coverage ({coverage:.0%} of component weight observed)")
        if missing:
            parts.append(f"Missing: {', '.join(missing[:4])}")
        return ". ".join(parts)

    if total < 45:
        parts.append(f"Total score too low ({total:.0f} < 45)")
    if risk is not None and risk > 75:
        parts.append(f"Risk penalty too high ({risk:.0f})")
    if catalyst is not None and catalyst < 20:
        parts.append("No catalyst signals")
    if missing:
        parts.append(f"Missing data: {', '.join(missing[:3])}")
    return ". ".join(parts) if parts else "Below threshold."
