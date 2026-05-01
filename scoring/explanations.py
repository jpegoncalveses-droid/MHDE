from __future__ import annotations


def generate_why_ranked(scores: dict) -> str:
    parts = []
    cheap = scores.get("cheap_score")
    quality = scores.get("quality_score")
    catalyst = scores.get("catalyst_score")
    momentum = scores.get("momentum_score")
    if cheap and cheap > 60:
        parts.append(f"Cheap: score={cheap:.0f}")
    if quality and quality > 60:
        parts.append(f"Quality: score={quality:.0f}")
    if catalyst and catalyst > 40:
        parts.append(f"Catalyst: score={catalyst:.0f}")
    if momentum and momentum > 60:
        parts.append(f"Momentum: score={momentum:.0f}")
    return ". ".join(parts) if parts else "Scores above threshold."


def generate_why_rejected(scores: dict, missing: list[str]) -> str:
    parts = []
    total = scores.get("total_score", 0)
    risk = scores.get("risk_penalty", 0)
    catalyst = scores.get("catalyst_score", 0)
    if total < 45:
        parts.append(f"Total score too low ({total:.0f} < 45)")
    if risk and risk > 75:
        parts.append(f"Risk penalty too high ({risk:.0f})")
    if catalyst is not None and catalyst < 20:
        parts.append(f"No catalyst signals")
    if missing:
        parts.append(f"Missing data: {', '.join(missing[:3])}")
    return ". ".join(parts) if parts else "Below threshold."
