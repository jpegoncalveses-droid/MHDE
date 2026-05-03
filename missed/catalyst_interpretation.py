"""Deterministic catalyst interpretation layer. No LLM calls."""
from __future__ import annotations

_ALL_STOCK_KEYWORDS = [
    "exchange ratio", "stock-for-stock", "all-stock", "shares of acquirer",
    "shares of buyer", "stock consideration",
]
_COMMERCIAL_KEYWORDS = [
    "delivery", "cargo", "shipment", "commercial", "freight",
]
_VALID_DIRECTIONS = {"bullish", "bearish", "neutral", "event_dependent"}
_VALID_GUIDANCES = {"accept", "watch", "reject", "investigate"}


def interpret_catalyst(entry: dict) -> dict:
    """Return interpretation fields for a queue entry dict. Never mutates entry.

    Returns a dict with keys:
      expected_direction, expected_move_summary, expected_timeframe,
      action_guidance, action_reason, key_checks (semicolon-joined str),
      priced_in_risk
    """
    catalyst_type = (entry.get("catalyst_type") or "").lower()
    sentiment = (entry.get("sentiment") or "").lower()
    confidence = float(entry.get("confidence") or 0)
    materiality = (entry.get("materiality") or "").lower()
    shadow_score = float(entry.get("shadow_score") or 0)
    evidence = (entry.get("evidence_quote") or "").lower()

    direction = "neutral"
    move_summary = ""
    timeframe = "near-term"
    guidance = "watch"
    reason = ""
    key_checks: list[str] = []
    priced_in_risk = "unknown"

    # ── M&A ──────────────────────────────────────────────────────────────────
    if catalyst_type in ("merger_acquisition", "acquisition", "merger"):
        if any(kw in evidence for kw in _ALL_STOCK_KEYWORDS):
            direction = "event_dependent"
            guidance = "watch"
            move_summary = "Spread trade — depends on deal close and acquirer stock"
            timeframe = "deal close window"
            reason = "All-stock exchange ratio: price tracks acquirer stock and spread compression"
            key_checks = ["exchange ratio", "acquirer stock movement", "deal spread", "close risk", "regulatory approval"]
            priced_in_risk = "medium"
        elif sentiment == "bullish":
            direction = "bullish"
            guidance = "accept" if confidence >= 0.8 else "watch"
            move_summary = "Target premium likely announced; watch for spread compression"
            timeframe = "deal close / near-term event window"
            reason = "M&A definitive agreement with bullish sentiment"
            key_checks = ["deal spread", "close probability", "regulatory timeline", "competing bids"]
            priced_in_risk = "high" if shadow_score > 46 else "medium"
        else:
            direction = "neutral"
            guidance = "investigate"
            move_summary = "M&A with non-bullish tone — review deal terms"
            timeframe = "near-term"
            reason = "M&A event with non-bullish or unclear sentiment"
            key_checks = ["deal terms", "target vs acquirer position", "market reaction"]
            priced_in_risk = "unknown"

    # ── Regulatory / litigation ───────────────────────────────────────────────
    elif catalyst_type in ("regulatory_approval", "regulatory", "litigation_resolution",
                           "litigation", "settlement"):
        is_commercial = any(kw in evidence for kw in _COMMERCIAL_KEYWORDS)
        is_settlement = "settlement" in evidence
        if is_commercial or is_settlement:
            direction = "bullish"
            guidance = "accept" if confidence >= 0.75 else "watch"
            move_summary = "Settlement removes overhang; commercial deliveries confirm recovery"
            timeframe = "1-2 quarters post-announcement"
            reason = "Regulatory settlement with commercial/delivery language"
            key_checks = ["settlement economics", "remaining disputes", "delivery confirmation", "backlog impact"]
            priced_in_risk = "low" if confidence >= 0.8 else "medium"
        else:
            direction = "bullish" if sentiment == "bullish" else "neutral"
            guidance = "watch"
            move_summary = "Regulatory outcome — verify scope and commercial impact"
            timeframe = "1-4 weeks post-announcement"
            reason = f"Regulatory event ({catalyst_type}) — scope and impact unclear"
            key_checks = ["scope of approval", "commercial implications", "remaining risks"]
            priced_in_risk = "medium"

    # ── Management change ─────────────────────────────────────────────────────
    elif catalyst_type == "management_change":
        direction = "neutral"
        guidance = "investigate"
        move_summary = "Market reaction depends on strategic context of transition"
        timeframe = "1-4 weeks (initial market reaction)"
        reason = "Management change: investigate strategic rationale before acting"
        key_checks = ["role (CEO/CFO vs other)", "strategic rationale", "transition plan", "insider signals"]
        priced_in_risk = "unknown"

    # ── Earnings / guidance ───────────────────────────────────────────────────
    elif catalyst_type in ("earnings", "earnings_release", "guidance",
                           "revenue_guidance", "earnings_guidance"):
        if sentiment == "bullish":
            direction = "bullish"
            guidance = "accept" if (confidence >= 0.8 and materiality == "high") else "watch"
            move_summary = "Beat/raise scenario; check if price already moved on whisper"
            timeframe = "1-5 days post-announcement"
            reason = "Bullish earnings/guidance with sufficient confidence"
            key_checks = ["beat vs whisper numbers", "guidance raise size", "price already moved", "sector sentiment"]
            priced_in_risk = "high" if shadow_score > 46 else "medium"
        elif sentiment == "bearish":
            direction = "bearish"
            guidance = "reject"
            move_summary = "Miss/cut scenario; downside risk"
            timeframe = "1-5 days post-announcement"
            reason = "Bearish earnings/guidance signal"
            key_checks = ["miss vs consensus", "guidance cut magnitude", "management commentary"]
            priced_in_risk = "medium"
        else:
            direction = "neutral"
            guidance = "watch"
            move_summary = "Earnings/guidance with mixed or neutral signals"
            timeframe = "1-5 days post-announcement"
            reason = "Earnings/guidance — sentiment unclear"
            key_checks = ["revenue vs consensus", "EPS vs consensus", "guidance outlook"]
            priced_in_risk = "unknown"

    # ── General fallback ─────────────────────────────────────────────────────
    else:
        if sentiment == "bullish":
            direction = "bullish"
            guidance = "accept" if confidence >= 0.85 else "watch"
            move_summary = "Bullish catalyst — verify evidence quality before acting"
            reason = f"{catalyst_type}: bullish sentiment"
            key_checks = ["evidence quality", "catalyst materiality", "price already moved"]
            priced_in_risk = "medium"
        elif sentiment == "bearish":
            direction = "bearish"
            guidance = "reject"
            move_summary = "Bearish catalyst — downside risk signal"
            reason = f"{catalyst_type}: bearish sentiment"
            key_checks = ["downside magnitude", "catalyst specificity", "sector correlation"]
            priced_in_risk = "medium"
        else:
            direction = "neutral"
            guidance = "watch"
            move_summary = "Neutral or unclear signal"
            reason = f"{catalyst_type}: neutral or unclear signal"
            key_checks = ["catalyst type", "evidence clarity"]
            priced_in_risk = "unknown"

    if not move_summary:
        move_summary = f"{direction.capitalize()} expected"

    return {
        "expected_direction": direction,
        "expected_move_summary": move_summary,
        "expected_timeframe": timeframe,
        "action_guidance": guidance,
        "action_reason": reason,
        "key_checks": "; ".join(key_checks),
        "priced_in_risk": priced_in_risk,
    }
