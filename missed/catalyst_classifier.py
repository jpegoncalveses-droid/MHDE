"""LLM catalyst classifier — orchestrates provider, cache, prompt building, and throttling.

Source grounding: events must carry source_text_char_count >= MIN_SOURCE_TEXT_CHARS
(set by enrich_events_with_source() in the CLI before calling here).  Events below
the threshold get a skip record instead of an LLM call.  After classification,
evidence_quote is verified against source_text; failures are tagged [INVALID_QUOTE].
"""
from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timezone

from missed.catalyst_cache import cache_key, load_cache, save_cache
from missed.catalyst_prompt import build_prompt
from missed.catalyst_providers import BaseCatalystProvider, MockCatalystProvider, get_provider
from missed.catalyst_schema import CatalystEnrichment
from missed.catalyst_source_resolver import (
    MIN_SOURCE_TEXT_CHARS,
    check_catalyst_sufficiency,
    check_sentiment_actionable,
    validate_evidence_quote,
)

logger = logging.getLogger("mhde.missed.catalyst_classifier")


def _make_skip_record(event: dict, reason: str) -> CatalystEnrichment:
    event_date = event.get("event_date", "")
    if hasattr(event_date, "isoformat"):
        event_date = event_date.isoformat()
    return CatalystEnrichment(
        event_id=event.get("event_id", ""),
        ticker=event.get("ticker", ""),
        event_date=str(event_date),
        catalyst_type="unknown",
        materiality="none",
        sentiment="neutral",
        confidence=0.0,
        evidence_quote="",
        reasoning_short=f"[SKIP] {reason}",
        should_affect_score=False,
        provider="skip_no_source_text",
        enriched_at=datetime.now(tz=timezone.utc).isoformat(),
    )


def classify_events(
    events: list[dict],
    *,
    use_mock: bool = True,
    provider_name: str = "mock",
    model: str = "gpt-4o-mini",
    cache_path: str | None = None,
    refresh_cache: bool = False,
    cfg: dict | None = None,
    rpm_limit: int | None = None,
    _provider: BaseCatalystProvider | None = None,
) -> list[CatalystEnrichment]:
    """Classify a list of sampled events.

    use_mock=True (default): zero API calls, deterministic, safe for CI.
    use_mock=False + OPENAI_API_KEY set: calls real provider.
    _provider: override for testing (bypasses get_provider logic).
    cache_path: path to JSONL cache file; None disables caching.
    refresh_cache: if True, ignore existing cache entries.
    rpm_limit: max requests/minute for real provider; sleep is inserted only
               before uncached calls (cache hits are never throttled).
    """
    if _provider is not None:
        provider = _provider
    else:
        provider = get_provider(use_mock, provider_name, model, cfg)

    # 60/rpm + 1 gives a 1-second buffer; e.g. 3 RPM → 21s min spacing.
    min_spacing: float = (60.0 / rpm_limit + 1.0) if rpm_limit else 0.0
    last_api_call: float = 0.0

    cache: dict[str, dict] = {}
    if cache_path and not refresh_cache:
        cache = load_cache(cache_path)

    results: list[CatalystEnrichment] = []
    new_entries: dict[str, dict] = dict(cache)
    n = len(events)

    for i, event in enumerate(events):
        event_id = event.get("event_id") or ""
        ticker = event.get("ticker", "")
        key = cache_key(event_id, provider.name, model)

        if not refresh_cache and key in cache:
            logger.info("[%d/%d] %s — cache hit", i + 1, n, ticker)
            cached = cache[key]
            try:
                results.append(CatalystEnrichment(**{
                    k: v for k, v in cached.items() if k != "_cache_key"
                }))
                continue
            except Exception:
                pass  # malformed cache entry — fall through to re-classify

        # Source grounding check: only enforced when the event went through
        # enrich_events_with_source() — detected by presence of "source_text_origin".
        # Events that bypassed the resolver (old tests, metadata-only paths) are not blocked.
        if "source_text_origin" in event:
            source_char_count = event.get("source_text_char_count", 0) or 0
            if source_char_count < MIN_SOURCE_TEXT_CHARS:
                reason = event.get("source_text_error") or "source_text_unavailable"
                logger.info("[%d/%d] %s — no source text (%s), skipping LLM", i + 1, n, ticker, reason)
                skip = _make_skip_record(event, reason)
                results.append(skip)
                new_entries[key] = skip.to_dict()
                continue

        # Throttle: space real API calls so we respect rpm_limit.
        if min_spacing > 0:
            elapsed = time.monotonic() - last_api_call
            sleep_secs = max(0.0, min_spacing - elapsed)
            if sleep_secs > 0:
                logger.info("[%d/%d] %s — cache miss, sleeping %.1fs", i + 1, n, ticker, sleep_secs)
                time.sleep(sleep_secs)
            else:
                logger.info("[%d/%d] %s — cache miss", i + 1, n, ticker)
        else:
            logger.info("[%d/%d] %s — cache miss", i + 1, n, ticker)

        prompt = build_prompt(event)
        enrichment = provider.classify(event, prompt)
        if min_spacing > 0:
            last_api_call = time.monotonic()

        # Quote grounding: verify evidence_quote appears (normalized) in source_text.
        source_text = event.get("source_text", "") or ""
        model_should_affect = enrichment.should_affect_score
        if source_text and not validate_evidence_quote(enrichment.evidence_quote, source_text):
            logger.warning("[%d/%d] %s — evidence_quote not found in source_text", i + 1, n, ticker)
            enrichment = replace(
                enrichment,
                should_affect_score=False,
                model_should_affect_score=model_should_affect,
                validation_status="invalid_quote",
                quote_validation_pass=False,
                invalid_reason="evidence_quote_not_in_source",
                reasoning_short=f"[INVALID_QUOTE] {enrichment.reasoning_short}",
            )
        else:
            enrichment = replace(
                enrichment,
                model_should_affect_score=model_should_affect,
                quote_validation_pass=True,
            )
            # Sufficiency check: verbatim quotes can still be boilerplate.
            sufficient, suff_reason = check_catalyst_sufficiency(
                enrichment.catalyst_type, enrichment.evidence_quote
            )
            if not sufficient:
                logger.info(
                    "[%d/%d] %s — weak evidence (%s), overriding should_affect_score",
                    i + 1, n, ticker, suff_reason,
                )
                enrichment = replace(
                    enrichment,
                    should_affect_score=False,
                    validation_status="weak_evidence",
                    invalid_reason=suff_reason,
                )
            # Sentiment filter: neutral/mixed never affect score
            elif enrichment.should_affect_score:
                sent_ok, sent_reason = check_sentiment_actionable(enrichment.sentiment)
                if not sent_ok:
                    enrichment = replace(
                        enrichment,
                        should_affect_score=False,
                        validation_status="neutral_sentiment",
                        invalid_reason=sent_reason,
                    )

        source_char_count = event.get("source_text_char_count", 0) or 0
        enrichment = replace(enrichment, source_text_char_count=source_char_count)
        results.append(enrichment)
        new_entries[key] = enrichment.to_dict()

    if cache_path:
        save_cache(cache_path, new_entries)

    return results


def revalidate_enrichments(enrichments: list[dict]) -> list[dict]:
    """Re-apply sufficiency validation to existing enrichment dicts without new LLM calls.

    Reads catalyst_type + evidence_quote from each record and re-runs the full
    sufficiency pipeline. Quote validation cannot be re-run (source_text is not
    stored in enriched records), so invalid_quote status is preserved.

    Returns a list of updated dicts (originals are not mutated).
    """
    results: list[dict] = []
    for rec in enrichments:
        rec = dict(rec)
        reasoning = rec.get("reasoning_short") or ""
        # Terminal states — pass through unchanged
        if "[SKIP]" in reasoning or "[ERROR]" in reasoning:
            results.append(rec)
            continue
        # Invalid quote — quote validation cannot be re-run; mark and skip sufficiency
        if "[INVALID_QUOTE]" in reasoning:
            rec["validation_status"] = "invalid_quote"
            rec["quote_validation_pass"] = False
            rec["should_affect_score"] = False
            rec.setdefault("model_should_affect_score", True)
            results.append(rec)
            continue

        # Ensure model_should_affect_score reflects the original LLM answer
        if "model_should_affect_score" not in rec:
            rec["model_should_affect_score"] = rec.get("should_affect_score", False)

        # Reset validation state to the model's answer, then re-validate
        rec["should_affect_score"] = rec["model_should_affect_score"]
        rec["validation_status"] = "valid"
        rec["invalid_reason"] = ""
        rec["quote_validation_pass"] = True

        sufficient, reason = check_catalyst_sufficiency(
            rec.get("catalyst_type", ""),
            rec.get("evidence_quote", ""),
        )
        if not sufficient:
            rec["should_affect_score"] = False
            rec["validation_status"] = "weak_evidence"
            rec["invalid_reason"] = reason
            logger.debug(
                "%s — revalidation: weak_evidence (%s)", rec.get("ticker", "?"), reason
            )
        # Sentiment filter: neutral/mixed never affect score (only if sufficiency passed)
        elif rec["should_affect_score"]:
            sent_ok, sent_reason = check_sentiment_actionable(rec.get("sentiment", "neutral"))
            if not sent_ok:
                rec["should_affect_score"] = False
                rec["validation_status"] = "neutral_sentiment"
                rec["invalid_reason"] = sent_reason

        results.append(rec)
    return results
