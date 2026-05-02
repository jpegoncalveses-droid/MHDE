"""Prompt template for LLM catalyst classification."""
from __future__ import annotations

SYSTEM_PROMPT = """You are a financial analyst classifying catalysts for missed stock price opportunities.
Given a significant price move and the actual SEC filing text, classify what drove the move.

Respond ONLY with a valid JSON object using this exact schema:
{
  "catalyst_type": "earnings|merger_acquisition|guidance|product_launch|regulatory|management_change|macro|sector_rotation|unknown",
  "materiality": "high|medium|low|none",
  "sentiment": "bullish|bearish|neutral|mixed",
  "confidence": 0.0 to 1.0,
  "evidence_quote": "direct verbatim quote copied from the filing source text, or empty string",
  "reasoning_short": "1-2 sentence explanation of what drove the move",
  "should_affect_score": true or false
}

Rules:
- should_affect_score=true only for high/medium materiality earnings, guidance, M&A, or regulatory events
- confidence reflects how certain you are given the available evidence
- evidence_quote MUST be copied verbatim from the SOURCE TEXT provided below — do NOT paraphrase
- if no direct quote in the source text supports the catalyst type, set:
    catalyst_type=unknown, materiality=none, should_affect_score=false, evidence_quote=""
- if source text is unavailable or too short, set catalyst_type=unknown"""


def build_prompt(event: dict) -> str:
    """Build the classification prompt for a single pilot event."""
    ticker = event.get("ticker", "UNKNOWN")
    event_date = event.get("event_date", "unknown")
    if hasattr(event_date, "isoformat"):
        event_date = event_date.isoformat()
    event_type = event.get("event_type", "gain_20d_20pct")
    return_value = event.get("return_value") or 0.0
    score = event.get("score_before_event")
    score_str = f"{score:.1f}" if score is not None else "N/A"
    filing_form = event.get("filing_form_type") or "none"
    filing_date = event.get("filing_date") or "none"
    if hasattr(filing_date, "isoformat"):
        filing_date = filing_date.isoformat()
    filing_desc = event.get("filing_description") or "not available"

    source_text = (event.get("source_text") or "").strip()
    source_origin = event.get("source_text_origin") or "unavailable"
    source_char_count = event.get("source_text_char_count") or 0

    if source_text and source_char_count >= 1:
        source_section = (
            f"\nFILING SOURCE TEXT ({source_origin}, {source_char_count} chars):\n"
            f"---\n{source_text[:6000]}\n---\n"
            f"\nIMPORTANT: evidence_quote must be copied verbatim from the source text above."
        )
    else:
        source_section = (
            f"\n[No filing source text available — set catalyst_type=unknown, "
            f"materiality=none, confidence<0.3]"
        )

    user_content = (
        f"Stock: {ticker}\n"
        f"Event: {event_type} — {return_value:.1f}% gain ending {event_date}\n"
        f"Score before event: {score_str} (classified Reject/Incomplete — missed by engine)\n"
        f"\n"
        f"Most recent SEC filing before event:\n"
        f"  Form type: {filing_form}\n"
        f"  Filed: {filing_date}\n"
        f"  Primary document: {filing_desc}\n"
        f"{source_section}\n"
        f"\n"
        f"Classify the catalyst that drove this price move."
    )

    return f"SYSTEM:\n{SYSTEM_PROMPT}\n\nUSER:\n{user_content}"
