"""Source text resolver for LLM catalyst pilot.

Resolves actual SEC filing text for each pilot event so the LLM is grounded
in real source material rather than hallucinating from metadata alone.

Resolution priority:
  1. SEC EDGAR URL constructed from accession_number + cik + description
  2. Unavailable — if form type is non-prose (Form 4, 13G, etc.) or no accession

Classifier uses source_text_char_count < MIN_SOURCE_TEXT_CHARS to skip the LLM.
validate_evidence_quote() enforces that evidence_quote appears verbatim in source_text.
compute_source_coverage() tallies resolution outcomes for the preflight report.
"""
from __future__ import annotations

import html as _html
import logging
import re
import unicodedata

logger = logging.getLogger("mhde.missed.catalyst_source_resolver")

MIN_SOURCE_TEXT_CHARS = 200
_MAX_SOURCE_TEXT_CHARS = 8_000
_FETCH_TIMEOUT = 10

# Form types that contain no prose text useful for catalyst identification.
_NON_TEXT_FORM_TYPES: frozenset[str] = frozenset([
    "4", "4/A",
    "3", "3/A", "5", "5/A",
    "144",
    "SC 13G", "SC 13G/A",
    "SC 13D", "SC 13D/A",
    "SCHEDULE 13G", "SCHEDULE 13G/A",
    "SCHEDULE 13D", "SCHEDULE 13D/A",
    "DEF 14A", "DEFA14A", "PRE 14A",
    "SD",
    "FWP",
    "425", "424B2", "424B3", "424B5",
    "CORRESP",
    "UPLOAD",
    "13F-NT", "13F-HR",
])

# Form types that contain useful prose text for catalyst identification.
_TEXT_FORM_TYPES: frozenset[str] = frozenset([
    "8-K", "8-K/A",
    "6-K", "6-K/A",
    "10-K", "10-K/A",
    "10-Q", "10-Q/A",
    "20-F", "20-F/A",
    "40-F", "40-F/A",
    "S-1", "S-1/A",
    "S-4", "S-4/A",
    "ARS",
])


def _is_non_text_form(form_type: str) -> bool:
    clean = form_type.strip().upper()
    return clean in {f.upper() for f in _NON_TEXT_FORM_TYPES}


def _build_sec_url(cik: str, accession_number: str, description: str) -> str | None:
    """Construct SEC EDGAR filing URL from accession number, CIK, and doc filename.

    accession: "0001193125-26-011731"  →  nodash: "000119312526011731" (18 chars)
    cik:       "1193125"
    url:       https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/{doc}
    """
    if not cik or not accession_number or not description:
        return None
    accession_nodash = accession_number.replace("-", "")
    if len(accession_nodash) != 18:
        return None
    # Strip XSLT subpath prefix (e.g. "xslF345X06/wk-form4.xml" → "wk-form4.xml")
    doc_filename = description.split("/")[-1] if "/" in description else description
    if not doc_filename:
        return None
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{doc_filename}"
    )


def _strip_html(html: str) -> str:
    """Strip HTML tags, decode all HTML entities, collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = _html.unescape(text)  # decodes &#8217; &#160; &amp; &lt; &gt; etc.
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_text(text: str) -> str:
    """Normalize for quote comparison: NFKD, curly quotes → straight, whitespace collapse."""
    text = unicodedata.normalize("NFKD", text)
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = re.sub(r"\s+", " ", text).strip()
    return text.lower()


def _fetch_and_clean(url: str, *, _fetch_fn=None) -> str:
    """Fetch filing at url; return plain text stripped of HTML tags.

    _fetch_fn: optional callable(url) -> str for testing (bypasses requests).
    """
    if _fetch_fn is not None:
        raw = _fetch_fn(url)
    else:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "mhde-pilot research@example.com"})
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

    return _strip_html(raw)[:_MAX_SOURCE_TEXT_CHARS]


def resolve_source_text(event: dict, *, _fetch_fn=None) -> dict:
    """Resolve source filing text for one event.

    Returns a dict with keys:
        source_text              str
        source_text_char_count   int
        source_text_origin       "sec_url" | "unavailable"
        source_text_error        str | None
        has_cik                  bool
        has_accession_number     bool
        has_primary_doc          bool
        constructed_url          str | None
    """
    form_type = (event.get("filing_form_type") or "").split("/")[0].strip()

    # Non-prose filings carry no useful text for catalyst identification.
    if form_type and _is_non_text_form(form_type):
        return {
            "source_text": "",
            "source_text_char_count": 0,
            "source_text_origin": "unavailable",
            "source_text_error": f"non_text_filing:{form_type}",
            "has_cik": bool(str(event.get("cik") or "").strip()),
            "has_accession_number": bool(str(event.get("accession_number") or "").strip()),
            "has_primary_doc": bool(str(event.get("filing_description") or "").strip()),
            "constructed_url": None,
        }

    description = str(event.get("filing_description") or "").strip()

    # PDF files are not supported without a dedicated parser.
    if description.lower().endswith(".pdf"):
        return {
            "source_text": "",
            "source_text_char_count": 0,
            "source_text_origin": "unavailable",
            "source_text_error": "pdf_not_supported",
            "has_cik": bool(str(event.get("cik") or "").strip()),
            "has_accession_number": bool(str(event.get("accession_number") or "").strip()),
            "has_primary_doc": True,  # the doc exists, just unsupported format
            "constructed_url": None,
        }

    cik = str(event.get("cik") or "").strip()
    accession = str(event.get("accession_number") or "").strip()
    has_cik = bool(cik)
    has_accession = bool(accession)
    has_doc = bool(description)
    url = _build_sec_url(cik, accession, description)

    if not url:
        return {
            "source_text": "",
            "source_text_char_count": 0,
            "source_text_origin": "unavailable",
            "source_text_error": "no_doc_url",
            "has_cik": has_cik,
            "has_accession_number": has_accession,
            "has_primary_doc": has_doc,
            "constructed_url": None,
        }

    try:
        text = _fetch_and_clean(url, _fetch_fn=_fetch_fn)
        return {
            "source_text": text,
            "source_text_char_count": len(text),
            "source_text_origin": "sec_url",
            "source_text_error": None,
            "has_cik": has_cik,
            "has_accession_number": has_accession,
            "has_primary_doc": has_doc,
            "constructed_url": url,
        }
    except Exception as exc:
        logger.debug("Failed to fetch %s: %s", url, exc)
        return {
            "source_text": "",
            "source_text_char_count": 0,
            "source_text_origin": "unavailable",
            "source_text_error": str(exc)[:200],
            "has_cik": has_cik,
            "has_accession_number": has_accession,
            "has_primary_doc": has_doc,
            "constructed_url": url,
        }


def enrich_events_with_source(events: list[dict], *, _fetch_fn=None) -> list[dict]:
    """Return new event dicts enriched with source_text_* and diagnostic fields."""
    enriched: list[dict] = []
    for i, event in enumerate(events):
        resolved = resolve_source_text(event, _fetch_fn=_fetch_fn)
        enriched.append(dict(event, **resolved))
        if resolved["source_text_char_count"] >= MIN_SOURCE_TEXT_CHARS:
            logger.debug(
                "[%d/%d] %s — source resolved: %d chars via %s",
                i + 1, len(events), event.get("ticker"),
                resolved["source_text_char_count"], resolved["source_text_origin"],
            )
        else:
            logger.debug(
                "[%d/%d] %s — source unavailable: %s",
                i + 1, len(events), event.get("ticker"), resolved["source_text_error"],
            )
    return enriched


def compute_source_coverage(sample: list[dict]) -> dict:
    """Tally source-text resolution outcomes for the preflight report.

    Input: list of event dicts that have already been enriched by
    enrich_events_with_source() (or constructed with the same keys).

    Returns:
        sampled_count            int
        resolvable_source_count  int  (char_count >= MIN_SOURCE_TEXT_CHARS)
        skipped_non_text_form_count int
        missing_cik_count        int  (no_doc_url + has_cik=False)
        missing_accession_count  int  (no_doc_url + has_accession_number=False)
        missing_primary_doc_count int (no_doc_url + has_primary_doc=False)
        pdf_not_supported_count  int
        fetch_error_count        int  (URL constructed but fetch failed)
    """
    resolvable = 0
    non_text = 0
    missing_cik = 0
    missing_accession = 0
    missing_doc = 0
    pdf_unsupported = 0
    fetch_errors = 0

    for e in sample:
        char_count = e.get("source_text_char_count") or 0
        err = e.get("source_text_error") or ""
        if char_count >= MIN_SOURCE_TEXT_CHARS:
            resolvable += 1
        elif err.startswith("non_text_filing:"):
            non_text += 1
        elif err == "no_doc_url":
            if not e.get("has_cik", True):
                missing_cik += 1
            if not e.get("has_accession_number", True):
                missing_accession += 1
            if not e.get("has_primary_doc", True):
                missing_doc += 1
        elif err == "pdf_not_supported":
            pdf_unsupported += 1
        elif err:
            fetch_errors += 1

    return {
        "sampled_count": len(sample),
        "resolvable_source_count": resolvable,
        "skipped_non_text_form_count": non_text,
        "missing_cik_count": missing_cik,
        "missing_accession_count": missing_accession,
        "missing_primary_doc_count": missing_doc,
        "pdf_not_supported_count": pdf_unsupported,
        "fetch_error_count": fetch_errors,
    }


def validate_evidence_quote(quote: str, source_text: str, *, min_len: int = 10) -> bool:
    """Return True if quote appears (after normalization) in source_text.

    Normalization handles: HTML entities (via upstream _strip_html), curly quotes,
    extra whitespace. Empty or very short quotes are trivially valid.
    """
    if not quote or len(quote) < min_len:
        return True
    if not source_text:
        return False
    return _normalize_text(quote) in _normalize_text(source_text)


# ── Catalyst sufficiency checks ───────────────────────────────────────────────

_MA_KEYWORDS = ("acqui", "merger", "definitive agreement", "completion",
                "purchase of", "sale of", "tender offer")

_EARNINGS_FINANCIAL_RE = re.compile(
    r'\$[\d,\.]+|\d+\s*%|revenue|earnings per|eps|net income|net loss'
    r'|operating income|gross profit|beat|bookings|arr|margin|cash flow',
    re.IGNORECASE,
)

_GUIDANCE_DETAIL_RE = re.compile(
    r'guidance|forecast|outlook|raised|lowered|revised'
    r'|expect\w*\s+\w*\s*(revenue|earnings|profit|sales)'
    r'|revenue|earnings|\$[\d,\.]+|\d+\s*%',
    re.IGNORECASE,
)
_GUIDANCE_BOILERPLATE_PHRASES = (
    "business update",
    "investor presentation",
    "investor conference",
    "investor day",
    "will use the information",
    "will be providing",
    "conference call",
    "earnings call",
    "presentation at",
)

# management_change: C-suite titles (whole-word matched; "vice president" excluded)
# "executive chairman" and "chairman of the board" are NOT here — they go through
# the chairman-specific check below, which requires turnaround context.
_MGMT_C_SUITE_RE = re.compile(
    r'\b(chief executive officer|ceo|chief financial officer|cfo'
    r'|chief operating officer|coo'
    r'|founder)\b'
    r'|(?<!vice )(?<!senior vice )(?<!executive vice )\bpresident\b',
    re.IGNORECASE,
)
# management_change: chairman patterns — only actionable with turnaround context
_MGMT_CHAIRMAN_RE = re.compile(
    r'\b(executive chairman|chairman of the board)\b'
    r"|company.{0,6}s\s+chairman"
    r"|\bserve as\s+\S*\s*chairman\b"
    r"|\bappointed\s+\S*\s*chairman\b"
    r"|\bchairman\b",
    re.IGNORECASE,
)
# management_change: context that makes a chairman appointment actionable
_MGMT_TURNAROUND_RE = re.compile(
    r'\b(founder|activist|turnaround|restructur|strategic review'
    r'|succession|transition|incoming\s+(ceo|cfo|coo)'
    r'|new\s+(ceo|cfo|coo)|chief executive|chief financial|chief operating)\b',
    re.IGNORECASE,
)
# management_change: debt issuance keywords — often mislabeled by LLMs
_MGMT_DEBT_KEYWORDS = (
    "aggregate principal amount",
    "senior secured notes",
    "subordinated notes",
    "notes due",
    "issued $",
    "principal amount of",
    "aggregate principal",
)
# management_change: compensation-only actions, not strategic
_MGMT_COMPENSATION_KEYWORDS = (
    "annual base salary",
    "annual salary",
    "base salary",
    "salary increase",
    "compensation increase",
    "increase of $",
)
# management_change: routine board governance
_MGMT_ROUTINE_BOARD_KEYWORDS = (
    "nominees for director",
    "elected to the company's board",
    "elected to the board of directors",
    "board of directors of the company",
    "appointed to the board of directors",
    "fiscal council",
    "audit committee",
    "has been reviewed and approved",
    "eligibility requirements",
)

# product_launch: real launch / clinical / approval signals
_PRODUCT_ACTIONABLE_RE = re.compile(
    r'phase\s*[123]|topline results?|top.line results?|primary analysis'
    r'|positive results?|fda approval|ema approval|received approval'
    r'|launched|commercially available|approval of|approved for'
    r'|major customer|customer win|selected by|agreement to deploy'
    r'|clinical trial|trial results?|efficacy|pivotal study',
    re.IGNORECASE,
)
# product_launch: strategy/roadmap language without launch signals
_PRODUCT_STRATEGY_RE = re.compile(
    r'long.term (strategy|growth)|pillar in|is expected to generate'
    r'|will generate|future growth|strategic (priority|asset|importance)'
    r'|long.life|low cost expandable',
    re.IGNORECASE,
)

# regulatory: material legal/regulatory events (settlements, approvals, enforcement)
_REGULATORY_MATERIAL_RE = re.compile(
    r'settlement agreement|consent decree|asset freeze'
    r'|fda approval|ema approval|received approval|regulatory approval'
    r'|commercial deliveries|first\s+(lng\s+)?cargo|first deliveries'
    r'|fines of|\$[\d,\.]+\s*(million|billion)?\s*settl|settl\w*\s+\$[\d,\.]+'
    r'|enforcement action|criminal charge|indictment|material fine',
    re.IGNORECASE,
)
# regulatory: routine/generic disclosures that carry no actionable content
_REGULATORY_GENERIC_RE = re.compile(
    r'routine compliance|disclosure requirement|compliance update'
    r'|periodic report|regulatory filing',
    re.IGNORECASE,
)


def check_catalyst_sufficiency(
    catalyst_type: str,
    evidence_quote: str,
) -> tuple[bool, str]:
    """Return (sufficient, reason). Empty reason means sufficient.

    Checks that the evidence_quote actually contains meaningful evidence for
    the claimed catalyst_type. Rejects boilerplate wrappers and vague phrases
    that contain no actionable financial information.
    """
    q = (evidence_quote or "").lower()

    if catalyst_type == "merger_acquisition":
        if any(kw in q for kw in _MA_KEYWORDS):
            return True, ""
        return False, "no_ma_keyword"

    if catalyst_type == "earnings":
        is_pr_wrapper = "press release" in q
        has_financial_detail = bool(_EARNINGS_FINANCIAL_RE.search(q))
        if is_pr_wrapper and not has_financial_detail:
            return False, "pr_wrapper_no_financials"
        if has_financial_detail:
            return True, ""
        return False, "no_financial_metrics"

    if catalyst_type == "guidance":
        has_guidance_detail = bool(_GUIDANCE_DETAIL_RE.search(q))
        if has_guidance_detail:
            return True, ""
        for phrase in _GUIDANCE_BOILERPLATE_PHRASES:
            if phrase in q:
                return False, "conference_or_update_boilerplate"
        return False, "no_guidance_language"

    if catalyst_type == "management_change":
        # 1. Debt issuance often mislabeled as management_change
        if any(kw in q for kw in _MGMT_DEBT_KEYWORDS):
            return False, "debt_issuance_misclassified"
        # 2. Compensation/salary actions are not strategic leadership changes
        if any(kw in q for kw in _MGMT_COMPENSATION_KEYWORDS):
            return False, "compensation_not_catalyst"
        # 3. Chairman appointment — only actionable with turnaround/transition context
        if bool(_MGMT_CHAIRMAN_RE.search(q)):
            if bool(_MGMT_TURNAROUND_RE.search(q)):
                return True, ""
            return False, "routine_chair_appointment"
        # 4. C-suite strategic change — actionable (whole-word, excludes "vice president")
        if bool(_MGMT_C_SUITE_RE.search(q)):
            return True, ""
        # 5. Routine board governance
        if any(kw in q for kw in _MGMT_ROUTINE_BOARD_KEYWORDS):
            return False, "routine_board_governance"
        # 6. No recognizable strategic signal
        return False, "weak_management_change"

    if catalyst_type == "product_launch":
        # Explicit launch / clinical / approval language is actionable
        if bool(_PRODUCT_ACTIONABLE_RE.search(q)):
            return True, ""
        # Strategy / roadmap without a real launch event is weak
        return False, "weak_product_or_project_update"

    if catalyst_type == "regulatory":
        if bool(_REGULATORY_MATERIAL_RE.search(q)):
            return True, ""
        return False, "routine_regulatory_disclosure"

    # All other catalyst types: any non-empty quote is sufficient
    return True, ""


def check_sentiment_actionable(sentiment: str) -> tuple[bool, str]:
    """Return (actionable, reason). Bullish and bearish are actionable; neutral/mixed are not."""
    if sentiment in ("bullish", "bearish"):
        return True, ""
    return False, "neutral_or_mixed_sentiment"
