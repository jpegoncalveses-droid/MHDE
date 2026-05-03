"""Deterministic first-pass catalyst classification rules.

These rules run before the LLM to classify obvious catalysts cheaply and reliably.
Returns None when no rule matches — caller falls through to LLM for ambiguous cases.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DeterministicResult:
    catalyst_type: str
    confidence: float
    sentiment: str   # bullish | bearish | neutral | mixed
    matched_rule: str


# Each entry: (compiled_pattern, catalyst_type, sentiment, rule_name)
_RULES: list[tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"definitive\s+(merger|acquisition)\s+agreement", re.I),
     "merger_acquisition", "bullish", "merger_agreement"),
    (re.compile(r"(acquired|acquisition\s+of|to\s+be\s+acquired\s+by)", re.I),
     "merger_acquisition", "bullish", "acquisition"),
    (re.compile(r"(quarterly|annual|fourth.quarter|third.quarter|second.quarter|first.quarter)\s+(earnings|results).{0,100}(per\s+share|EPS|revenue)", re.I),
     "earnings", "neutral", "earnings_release"),
    (re.compile(r"(eps|earnings\s+per\s+share).{0,80}(exceeded|beat|surpassed|missed|below\s+estimates)", re.I),
     "earnings", "mixed", "earnings_surprise"),
    (re.compile(r"(raise[sd]?|increase[sd]?|upward).{0,50}(full.year\s+)?(revenue\s+)?(guidance|outlook|forecast)", re.I),
     "guidance", "bullish", "guidance_raised"),
    (re.compile(r"(lower[sd]?|reduce[sd]?|cut[sd]?|narrow[sd]?).{0,50}(guidance|outlook|forecast)", re.I),
     "guidance", "bearish", "guidance_lowered"),
    (re.compile(r"(appointed|named|elected|promoted).{0,60}(chief\s+executive|chief\s+financial|president\s+and\s+ceo|ceo|cfo)\b", re.I),
     "management_change", "neutral", "exec_appointment"),
    (re.compile(r"(resign[sed]*|step[ped]*\s+down|depart[sed]*|leaving).{0,60}(chief\s+executive|ceo|chief\s+financial|cfo)", re.I),
     "management_change", "bearish", "exec_departure"),
    (re.compile(r"(settlement\s+agreement|agreed\s+to\s+pay|agreed\s+to\s+settle).{0,100}(million|billion|lawsuit|litigation|class\s+action)", re.I),
     "regulatory", "bearish", "legal_settlement"),
    (re.compile(r"phase\s+[123].{0,100}(primary\s+endpoint|met.{0,20}endpoint|statistically\s+significant)", re.I),
     "regulatory", "bullish", "clinical_trial_positive"),
    (re.compile(r"(fda\s+(approved|granted\s+approval|cleared)|510.?k\s+clearance)", re.I),
     "regulatory", "bullish", "fda_approval"),
    (re.compile(r"(fda\s+(rejected|refused|issued\s+a\s+complete\s+response|crl)|complete\s+response\s+letter)", re.I),
     "regulatory", "bearish", "fda_rejection"),
    (re.compile(r"(general\s+availability|ga\s+release|commercially\s+available|product\s+launch\b)", re.I),
     "product_launch", "bullish", "product_launch"),
    (re.compile(r"(share\s+repurchase\s+program|buyback\s+program|repurchase\s+up\s+to)", re.I),
     "product_launch", "bullish", "buyback_program"),
]


def classify_deterministic(text: str) -> Optional[DeterministicResult]:
    """Return a DeterministicResult if any rule matches, else None."""
    if not text:
        return None
    for pattern, catalyst_type, sentiment, rule_name in _RULES:
        if pattern.search(text):
            return DeterministicResult(
                catalyst_type=catalyst_type,
                confidence=0.85,
                sentiment=sentiment,
                matched_rule=rule_name,
            )
    return None
