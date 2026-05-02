"""Deterministic sampler: selects representative text-evidence events for LLM pilot.

Priority order:
  1. Near-threshold scored candidates (score 35–48, just below C-tier boundary)
  2. All other text-evidence events
Within each priority band, ordering is stable via SHA-256(event_id + salt).

By default (include_non_text_forms=False) the LATERAL join only selects from
text-resolvable form types (8-K, 6-K, 10-K, 10-Q, 20-F, 40-F, etc.).
This avoids the common case where the most-recent filing is a Form 4 or 144,
which carries no prose text for LLM analysis. Events where no text filing
exists before the event date still appear in the sample but with
filing_form_type=None, making them clearly unresolvable by the source resolver.
"""
from __future__ import annotations

import hashlib
import logging
import re

import duckdb

logger = logging.getLogger("mhde.missed.catalyst_sampler")

PILOT_SIZE = 100
_SEED_SALT = "catalyst_pilot_v1"
_NEAR_THRESHOLD_MIN = 35.0
_NEAR_THRESHOLD_MAX = 48.0

# Targeted near-threshold mode: Reject tickers within one catalyst bump of C-tier
_NT_SCORE_MIN = 40.0
_NT_SCORE_MAX = 44.9
_NT_PILOT_SIZE = 50

# ── Event priority scoring constants ─────────────────────────────────────────

# Forms that typically contain material catalyst disclosures.
_HIGH_SIGNAL_FORMS = frozenset({"8-K", "8-K/A", "6-K", "6-K/A", "S-4", "S-4/A"})
# Proxy/merger forms get extra weight too.
_MERGER_FORMS = frozenset({"S-4", "S-4/A", "DEFM14A", "PREM14A"})
# Routine forms — deprioritize unless description has high-signal terms.
_ROUTINE_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A", "ARS"})
# High-return event types get a small bonus.
_HIGH_RETURN_EVENT_TYPES = frozenset({"gain_20d_20pct", "gain_60d_30pct", "52wk_high_breakout"})

_HIGH_SIGNAL_TERMS_RE = re.compile(
    r'\b(merger|acquisition|acqui|definitive agreement|agreement to acquire'
    r'|settlement|settl\w+'
    r'|guidance|outlook|results|earnings|revenue|EPS'
    r'|clinical trial|clinical results|approval|approved|fda|ema'
    r'|contract|partnership|collaboration|license agreement'
    r'|dividend|buyback|repurchase|tender offer'
    r'|strategic review|restructur|spin.?off|divestiture|divest)\b',
    re.IGNORECASE,
)
_GOVERNANCE_TERMS_RE = re.compile(
    r'\b(director|board|proxy|annual meeting|compensation|governance'
    r'|amendment|by.?laws|charter|insider|form 4|reporting person'
    r'|investor presentation|current report|corporate update)\b',
    re.IGNORECASE,
)
_BOILERPLATE_FILING_RE = re.compile(
    r'^(form\s*)?(8-k|10-k|10-q|6-k|20-f|40-f|ars)\.htm$',
    re.IGNORECASE,
)
_SOURCE_AVAILABLE_THRESHOLD = 200  # chars; matches source_resolver / queue logic


def compute_event_priority(event: dict) -> tuple[int, list[str]]:
    """Score an event dict for per-ticker selection quality.

    Returns (priority_score, reasons) where higher score = preferred event.
    Priority is a signed integer; reasons are short human-readable strings.
    """
    score = 0
    reasons: list[str] = []

    form = (event.get("filing_form_type") or "").strip()
    desc = (event.get("filing_description") or "").strip()
    event_type = event.get("event_type") or ""
    source_chars = int(event.get("source_text_char_count") or 0)

    # ── Form type quality ─────────────────────────────────────────────────────
    if not form:
        score -= 1
        reasons.append("no-filing: -1")
    elif form in _HIGH_SIGNAL_FORMS:
        score += 2
        reasons.append(f"form:{form}: +2")
    elif form in _MERGER_FORMS:
        score += 2
        reasons.append(f"form:{form}(merger/proxy): +2")
    elif form in _ROUTINE_FORMS:
        score -= 1
        reasons.append(f"form:{form}(routine): -1")

    # ── Description signal quality ────────────────────────────────────────────
    if desc:
        if _BOILERPLATE_FILING_RE.match(desc):
            # Generic filename wrapper — no extra bonus or penalty
            reasons.append("desc:boilerplate-filename")
        elif _HIGH_SIGNAL_TERMS_RE.search(desc):
            signal_count = len(_HIGH_SIGNAL_TERMS_RE.findall(desc))
            bonus = min(signal_count, 3)
            score += bonus
            reasons.append(f"desc:high-signal-terms({signal_count}): +{bonus}")
        elif _GOVERNANCE_TERMS_RE.search(desc):
            score -= 1
            reasons.append("desc:governance/admin: -1")

    # ── Source text availability ──────────────────────────────────────────────
    if source_chars >= _SOURCE_AVAILABLE_THRESHOLD:
        score += 1
        reasons.append(f"source-resolvable({source_chars}chars): +1")

    # ── Event type (return magnitude signal) ──────────────────────────────────
    if event_type in _HIGH_RETURN_EVENT_TYPES:
        score += 1
        reasons.append(f"event-type:{event_type}: +1")

    return score, reasons


# Form types that contain useful prose text for catalyst identification.
_TEXT_FORM_TYPES = (
    "'8-K'", "'8-K/A'",
    "'6-K'", "'6-K/A'",
    "'10-K'", "'10-K/A'",
    "'10-Q'", "'10-Q/A'",
    "'20-F'", "'20-F/A'",
    "'40-F'", "'40-F/A'",
    "'S-1'", "'S-1/A'",
    "'S-4'", "'S-4/A'",
    "'ARS'",
)
_TEXT_FORM_IN_CLAUSE = ", ".join(_TEXT_FORM_TYPES)


def sample_pilot_events(
    conn: duckdb.DuckDBPyConnection,
    n: int = PILOT_SIZE,
    include_non_text_forms: bool = False,
) -> list[dict]:
    """Return up to n text-evidence events, deterministically ordered.

    By default, the filing context for each event is taken from the most recent
    TEXT filing before the event date. Form 4, 144, SC 13G, and similar
    non-prose filings are excluded from the filing context unless
    include_non_text_forms=True.

    Selects from investigations where text_enrichment_needed=True OR
    primary_root_cause is one of the precise text causes.
    """
    if include_non_text_forms:
        filing_where = ""
    else:
        filing_where = f"AND form_type IN ({_TEXT_FORM_IN_CLAUSE})"

    query = f"""
        SELECT
            i.investigation_id,
            i.event_id,
            i.ticker,
            i.event_date,
            i.primary_root_cause,
            i.root_causes_json,
            e.event_type,
            e.return_value,
            e.was_scored,
            e.score_before_event,
            f.form_type       AS filing_form_type,
            f.filing_date,
            f.description     AS filing_description,
            f.accession_number,
            f.cik
        FROM missed_opportunity_investigations i
        JOIN missed_opportunity_events e ON i.event_id = e.event_id
        LEFT JOIN LATERAL (
            SELECT form_type, filing_date, description, accession_number, cik
            FROM filings
            WHERE ticker = i.ticker
              AND filing_date < i.event_date
              {filing_where}
            ORDER BY filing_date DESC
            LIMIT 1
        ) f ON true
        WHERE i.text_enrichment_needed = true
           OR i.primary_root_cause IN (
               'text_evidence_available_not_classified',
               'needs_llm_text_enrichment'
           )
    """

    rows = conn.execute(query).fetchall()

    if not rows:
        return []

    cols = (
        "investigation_id", "event_id", "ticker", "event_date",
        "primary_root_cause", "root_causes_json", "event_type",
        "return_value", "was_scored", "score_before_event",
        "filing_form_type", "filing_date", "filing_description",
        "accession_number", "cik",
    )
    events = [dict(zip(cols, r)) for r in rows]

    def _sort_key(e: dict) -> tuple:
        score = e.get("score_before_event")
        is_near = (
            score is not None
            and _NEAR_THRESHOLD_MIN <= float(score) <= _NEAR_THRESHOLD_MAX
        )
        tier = 0 if is_near else 1  # lower = higher priority
        h = hashlib.sha256(f"{e['event_id']}{_SEED_SALT}".encode()).hexdigest()
        return (tier, h)

    events.sort(key=_sort_key)
    sample = events[:n]
    logger.info("Catalyst pilot: sampled %d / %d text-evidence events", len(sample), len(events))
    return sample


def sample_near_threshold_events(
    conn: duckdb.DuckDBPyConnection,
    n: int = _NT_PILOT_SIZE,
    include_non_text_forms: bool = False,
    score_min: float = _NT_SCORE_MIN,
    score_max: float = _NT_SCORE_MAX,
    max_events_per_ticker: int = 9999,
) -> list[dict]:
    """Return up to n events for near-threshold Reject tickers.

    Joins against the scores table (latest run per ticker) to filter candidates
    with total_score in [score_min, score_max] and tier='Reject'.  These are
    tickers one good catalyst away from C-tier.

    max_events_per_ticker: keep at most this many events per ticker, selecting
    the most recent event_date (with filing-present events preferred over
    filing-absent events within the same ticker).  Default 9999 = no deduplication.
    The daily-catalyst-queue command uses max_events_per_ticker=1 to guarantee
    one unique ticker per queue slot.

    Each returned event dict includes extra fields from the latest score row:
        current_score, current_catalyst_score, current_risk_penalty,
        current_tier, current_run_id

    Ordered by current_score DESC, event_date DESC (highest-score near-miss first).
    Non-text form types are excluded from filing context unless include_non_text_forms=True.
    """
    if include_non_text_forms:
        filing_where = ""
    else:
        filing_where = f"AND form_type IN ({_TEXT_FORM_IN_CLAUSE})"

    # No SQL LIMIT here — Python applies n after per-ticker deduplication.
    query = f"""
        WITH latest_scores AS (
            SELECT ticker, total_score, catalyst_score, risk_penalty, tier, run_id,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY run_id DESC) AS rn
            FROM scores
        )
        SELECT
            i.investigation_id,
            i.event_id,
            i.ticker,
            i.event_date,
            i.primary_root_cause,
            i.root_causes_json,
            e.event_type,
            e.return_value,
            e.was_scored,
            e.score_before_event,
            f.form_type       AS filing_form_type,
            f.filing_date,
            f.description     AS filing_description,
            f.accession_number,
            f.cik,
            ls.total_score    AS current_score,
            ls.catalyst_score AS current_catalyst_score,
            ls.risk_penalty   AS current_risk_penalty,
            ls.tier           AS current_tier,
            ls.run_id         AS current_run_id
        FROM missed_opportunity_investigations i
        JOIN missed_opportunity_events e ON i.event_id = e.event_id
        JOIN latest_scores ls ON ls.ticker = i.ticker AND ls.rn = 1
        LEFT JOIN LATERAL (
            SELECT form_type, filing_date, description, accession_number, cik
            FROM filings
            WHERE ticker = i.ticker
              AND filing_date < i.event_date
              {filing_where}
            ORDER BY filing_date DESC
            LIMIT 1
        ) f ON true
        WHERE ls.total_score >= {score_min}
          AND ls.total_score <= {score_max}
          AND ls.tier = 'Reject'
          AND (
              i.text_enrichment_needed = true
              OR i.primary_root_cause IN (
                  'text_evidence_available_not_classified',
                  'needs_llm_text_enrichment'
              )
          )
        ORDER BY ls.total_score DESC, i.event_date DESC
    """

    rows = conn.execute(query).fetchall()

    if not rows:
        return []

    cols = (
        "investigation_id", "event_id", "ticker", "event_date",
        "primary_root_cause", "root_causes_json", "event_type",
        "return_value", "was_scored", "score_before_event",
        "filing_form_type", "filing_date", "filing_description",
        "accession_number", "cik",
        "current_score", "current_catalyst_score", "current_risk_penalty",
        "current_tier", "current_run_id",
    )
    events = [dict(zip(cols, r)) for r in rows]

    # Annotate every event with priority score + reasons for debugging.
    for e in events:
        pri, reasons = compute_event_priority(e)
        e["event_priority"] = pri
        e["event_priority_reasons"] = reasons

    # Per-ticker deduplication: keep max_events_per_ticker per ticker.
    # Sort order: priority DESC, then event_date DESC (tie-break).
    by_ticker: dict[str, list] = {}
    for e in events:
        t = e["ticker"]
        if t not in by_ticker:
            by_ticker[t] = []
        by_ticker[t].append(e)

    selected: list[dict] = []
    for ticker_events in by_ticker.values():
        ticker_events.sort(key=lambda e: str(e.get("event_date", "")), reverse=True)
        ticker_events.sort(key=lambda e: -e.get("event_priority", 0))
        selected.extend(ticker_events[:max_events_per_ticker])

    # Re-sort globally: current_score DESC then event_date DESC (two stable passes).
    selected.sort(key=lambda e: str(e.get("event_date", "")), reverse=True)
    selected.sort(key=lambda e: -float(e.get("current_score") or 0))

    sample = selected[:n]
    logger.info(
        "Near-threshold pilot: sampled %d events (score %.1f–%.1f, Reject)",
        len(sample), score_min, score_max,
    )
    return sample
