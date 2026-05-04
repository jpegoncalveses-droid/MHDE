"""Root-cause enrichment for prediction-vs-actual missed-spike rows.

Assigns deterministic (no LLM) root-cause labels to each row returned by
missed.prediction_report.build_rows().  Shadow/diagnostic only — no
production scores are written.
"""
from __future__ import annotations

import csv
import logging
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

logger = logging.getLogger("mhde.missed.root_cause_enrichment")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ROOT_CAUSE_GROUPS: dict[str, str] = {
    "universe_not_seeded":        "universe_gap",
    "pre_score_history":          "data_gap",
    "incomplete_fundamentals":    "data_gap",
    # incomplete_fundamentals subcauses
    "missing_cik":                "data_gap",
    "missing_sec_companyfacts":   "data_gap",
    "foreign_filer_or_adr":       "data_gap",
    "stale_fundamentals":         "data_gap",
    "recent_ipo_or_short_history": "data_gap",
    "sector_specific_model_gap":  "scoring_gap",
    "polygon_fundamentals_missing": "data_gap",
    "ifrs_mapping_gap":           "data_gap",
    "price_only_scored":          "data_gap",
    # end subcauses
    "no_evidence_no_filing":      "data_gap",
    "missing_earnings_context":   "feature_gap",
    "sector_cluster_move":        "feature_gap",
    "low_catalyst_score":         "scoring_gap",
    "low_quality_score":          "scoring_gap",
    "near_threshold_no_catalyst": "near_miss",
    "near_threshold_scored":      "near_miss",
    "unknown":                    "unknown",
}

_EXPLANATIONS: dict[str, str] = {
    "universe_not_seeded":
        "Ticker not in the MHDE universe YAML — never scored.",
    "pre_score_history":
        "Event predates score history; no prior score to join.",
    "incomplete_fundamentals":
        "Tier=Incomplete: fewer than 2 fundamental components available (legacy label).",
    "missing_cik":
        "Tier=Incomplete: no CIK mapped — cannot link to SEC EDGAR company facts.",
    "missing_sec_companyfacts":
        "Tier=Incomplete: CIK present but active_sec_reporter=False — SEC data not downloaded.",
    "foreign_filer_or_adr":
        "Tier=Incomplete: ADR or foreign registrant — SEC filings use non-US GAAP forms.",
    "stale_fundamentals":
        "Tier=Incomplete: last SEC filing > 180 days old — fundamental data is stale.",
    "recent_ipo_or_short_history":
        "Tier=Incomplete: no filing date on record — likely recent IPO or new universe addition.",
    "sector_specific_model_gap":
        "Tier=Incomplete: Financials/Real Estate/Utilities sector — standard ratios don't apply.",
    "polygon_fundamentals_missing":
        "Tier=Incomplete: market_cap not populated — Polygon enrichment hasn't run for this ticker.",
    "ifrs_mapping_gap":
        "Tier=Incomplete: active domestic reporter with filing history — likely IFRS or non-standard reporting format.",
    "price_only_scored":
        "Tier=Incomplete: has market_cap (SEC EDGAR) but last_financial_filing_date not yet ingested — fundamental ratios unavailable.",
    "no_evidence_no_filing":
        "No catalyst text found; had_catalyst_evidence=False.",
    "missing_earnings_context":
        "Event within 7 days of an earnings event; no earnings-surprise signal.",
    "sector_cluster_move":
        "3+ sector peers moved the same window in the same 3-day window.",
    "low_catalyst_score":
        "Catalyst component score below 30; weak or absent catalyst signal.",
    "low_quality_score":
        "Quality component score below 40; business quality signal too weak.",
    "near_threshold_no_catalyst":
        "Score near C-tier threshold but catalyst score < 30.",
    "near_threshold_scored":
        "Score near C-tier threshold; a stronger catalyst signal might tip it.",
    "unknown":
        "Root cause could not be determined from available data.",
}

_SUGGESTED_FIXES: dict[str, str] = {
    "universe_not_seeded":
        "Verify YAML covers all current S&P 500 members; add missing tickers.",
    "pre_score_history":
        "Accumulate score history — no code change needed.",
    "incomplete_fundamentals":
        "Add fundamentals data source (Alpha Vantage or Polygon) for this ticker.",
    "missing_cik":
        "Run CIK validator: python main.py data validate-ciks. Map missing CIKs in universe YAML.",
    "missing_sec_companyfacts":
        "Run SEC company facts ingest for this ticker: python main.py data ingest-sec-facts --ticker <TICKER>.",
    "foreign_filer_or_adr":
        "Mark is_adr=true in universe YAML; use Polygon fundamentals instead of SEC for this ticker.",
    "stale_fundamentals":
        "Re-run SEC company facts ingest; check if company is still actively reporting to SEC.",
    "recent_ipo_or_short_history":
        "No action needed — scoring history will accumulate. Verify ticker is in universe YAML.",
    "sector_specific_model_gap":
        "Implement sector-adjusted scoring model for Financials/RE/Utilities (book value, FFO, rate sensitivity).",
    "polygon_fundamentals_missing":
        "Run Polygon ticker details enrichment: python main.py data enrich-ticker-details.",
    "ifrs_mapping_gap":
        "Add IFRS-to-US-GAAP field mapping in features/valuation.py for non-standard metric names.",
    "price_only_scored":
        "last_financial_filing_date is now present for all affected tickers. Real gap: fundamental ratio features (P/E, revenue growth, margins) are not being computed from fundamentals_raw by the features pipeline. Fix: wire ratio computation in features/valuation.py and re-run scoring.",
    "no_evidence_no_filing":
        "Add EFTS fallback or press-release scraper to increase filing coverage.",
    "missing_earnings_context":
        "Add EPS estimates adapter; wire earnings-proximity feature to scoring.",
    "sector_cluster_move":
        "Sector move detected via peer clustering only — no ETF data used. ingest_sector_etfs.py exists but is not wired into the orchestrator and requires a Polygon API key. Sector-relative features are not computed at scoring time. Fix: wire sector ETF ingestor and add sector-momentum feature to scoring.",
    "low_catalyst_score":
        "Investigate catalyst source coverage for this ticker and date.",
    "low_quality_score":
        "Review quality fundamentals for this ticker; check data freshness.",
    "near_threshold_no_catalyst":
        "Improve catalyst coverage; this ticker may tip to C-tier with better signals.",
    "near_threshold_scored":
        "Calibrate threshold — consider 43.0 as a watch-list boundary.",
    "unknown":
        "Manual investigation required.",
}

_CONFIDENCE: dict[str, str] = {
    "universe_not_seeded":          "high",
    "pre_score_history":            "high",
    "incomplete_fundamentals":      "high",
    "missing_cik":                  "high",
    "missing_sec_companyfacts":     "high",
    "foreign_filer_or_adr":         "high",
    "stale_fundamentals":           "high",
    "recent_ipo_or_short_history":  "medium",
    "sector_specific_model_gap":    "medium",
    "polygon_fundamentals_missing": "medium",
    "ifrs_mapping_gap":             "medium",
    "price_only_scored":            "low",
    "no_evidence_no_filing":        "medium",
    "missing_earnings_context":     "medium",
    "sector_cluster_move":          "medium",
    "low_catalyst_score":           "medium",
    "low_quality_score":            "low",
    "near_threshold_no_catalyst":   "medium",
    "near_threshold_scored":        "medium",
    "unknown":                      "low",
}

_EVIDENCE_FIELDS: dict[str, str] = {
    "universe_not_seeded":          "was_in_universe,classification",
    "pre_score_history":            "classification,score_join_method",
    "incomplete_fundamentals":      "tier_before_event",
    "missing_cik":                  "tier_before_event,companies.cik",
    "missing_sec_companyfacts":     "tier_before_event,companies.cik,companies.active_sec_reporter",
    "foreign_filer_or_adr":         "tier_before_event,companies.is_adr",
    "stale_fundamentals":           "tier_before_event,companies.last_financial_filing_date",
    "recent_ipo_or_short_history":  "tier_before_event,companies.last_financial_filing_date",
    "sector_specific_model_gap":    "tier_before_event,companies.sector",
    "polygon_fundamentals_missing": "tier_before_event,companies.market_cap",
    "ifrs_mapping_gap":             "tier_before_event,companies.cik,companies.active_sec_reporter",
    "price_only_scored":            "tier_before_event",
    "no_evidence_no_filing":        "had_catalyst_evidence",
    "missing_earnings_context":     "event_date,events.event_date,events.event_type",
    "sector_cluster_move":          "companies.sector,window_days,event_date",
    "low_catalyst_score":           "scores.catalyst_score",
    "low_quality_score":            "scores.quality_score",
    "near_threshold_no_catalyst":   "score_before_event,scores.catalyst_score",
    "near_threshold_scored":        "score_before_event,scores.catalyst_score",
    "unknown":                      "",
}

_REPORT_ENRICHED_CSV = "prediction_vs_actual_enriched_rows.csv"
_REPORT_ENRICHED_MD  = "root_cause_enrichment_report.md"

_ENRICHMENT_EXTRA_COLS = [
    "enriched_root_cause",
    "root_cause_group",
    "explanation_short",
    "evidence_fields_used",
    "suggested_fix",
    "confidence",
    # incomplete_fundamentals diagnostic fields (empty string for non-Incomplete rows)
    "incomplete_diag_ticker_in_companies",
    "incomplete_diag_cik",
    "incomplete_diag_active_sec_reporter",
    "incomplete_diag_is_adr",
    "incomplete_diag_last_filing_date",
    "incomplete_diag_filing_age_days",
    "incomplete_diag_sector",
    "incomplete_diag_market_cap_known",
    "incomplete_diag_subcause",
]

_INCOMPLETE_DIAG_EMPTY: dict[str, str] = {
    "incomplete_diag_ticker_in_companies": "",
    "incomplete_diag_cik": "",
    "incomplete_diag_active_sec_reporter": "",
    "incomplete_diag_is_adr": "",
    "incomplete_diag_last_filing_date": "",
    "incomplete_diag_filing_age_days": "",
    "incomplete_diag_sector": "",
    "incomplete_diag_market_cap_known": "",
    "incomplete_diag_subcause": "",
}

# Threshold boundaries (must match prediction_report.py)
_NEAR_THRESHOLD_MIN = 40.0
_NEAR_THRESHOLD_MAX = 45.0

_SECTOR_MODEL_GAPS: frozenset[str] = frozenset({"Financials", "Real Estate", "Utilities"})

# ---------------------------------------------------------------------------
# DB lookup helpers
# ---------------------------------------------------------------------------


def _fetch_score_components(conn: duckdb.DuckDBPyConnection) -> dict[tuple[str, str], dict[str, Any]]:
    """Return {(ticker, as_of_date_str): {catalyst_score, quality_score, ...}}."""
    rows = conn.execute("""
        SELECT ticker, as_of_date, catalyst_score, quality_score, momentum_score, cheap_score
        FROM scores
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY ticker, as_of_date ORDER BY created_at DESC
        ) = 1
    """).fetchall()
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for ticker, as_of_date, cat, qual, mom, cheap in rows:
        key = (str(ticker), str(as_of_date))
        result[key] = {
            "catalyst_score": cat,
            "quality_score":  qual,
            "momentum_score": mom,
            "cheap_score":    cheap,
        }
    return result


def _fetch_earnings_dates(conn: duckdb.DuckDBPyConnection) -> dict[str, list[date]]:
    """Return {ticker: [earnings_date, ...]} from the events table."""
    rows = conn.execute(
        "SELECT ticker, event_date FROM events "
        "WHERE event_type = 'earnings' AND ticker IS NOT NULL"
    ).fetchall()
    result: dict[str, list[date]] = defaultdict(list)
    for ticker, event_date in rows:
        if event_date is not None:
            d = _coerce_date(event_date)
            if d is not None:
                result[str(ticker)].append(d)
    return dict(result)


def _fetch_sector_map(conn: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """Return {ticker: sector} for companies with non-NULL sector."""
    rows = conn.execute(
        "SELECT ticker, sector FROM companies WHERE sector IS NOT NULL"
    ).fetchall()
    return {str(ticker): str(sector) for ticker, sector in rows}


def _fetch_companies_data(conn: duckdb.DuckDBPyConnection) -> dict[str, dict[str, Any]]:
    """Return {ticker: {cik, is_adr, active_sec_reporter, last_financial_filing_date, sector, market_cap}}."""
    rows = conn.execute("""
        SELECT ticker, cik, is_adr, active_sec_reporter,
               last_financial_filing_date, sector, market_cap
        FROM companies
    """).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for ticker, cik, is_adr, active_sec, last_filing, sector, market_cap in rows:
        result[str(ticker)] = {
            "cik": cik,
            "is_adr": is_adr,
            "active_sec_reporter": active_sec,
            "last_financial_filing_date": last_filing,
            "sector": sector,
            "market_cap": market_cap,
        }
    return result


def _classify_incomplete_subcause(
    ticker: str,
    co: dict[str, Any] | None,
    today: date,
) -> tuple[str, dict[str, str]]:
    """Return (subcause_label, diag_fields) for an Incomplete-tier row.

    Evaluates companies table fields in priority order; first match wins.
    """
    diag: dict[str, str] = {
        "incomplete_diag_ticker_in_companies": "no" if co is None else "yes",
        "incomplete_diag_cik": "",
        "incomplete_diag_active_sec_reporter": "",
        "incomplete_diag_is_adr": "",
        "incomplete_diag_last_filing_date": "",
        "incomplete_diag_filing_age_days": "",
        "incomplete_diag_sector": "",
        "incomplete_diag_market_cap_known": "",
        "incomplete_diag_subcause": "",
    }

    if co is None:
        diag["incomplete_diag_subcause"] = "missing_cik"
        return "missing_cik", diag

    cik = co.get("cik")
    is_adr = bool(co.get("is_adr")) if co.get("is_adr") is not None else False
    active_sec = co.get("active_sec_reporter")
    last_filing = _coerce_date(co.get("last_financial_filing_date"))
    sector = co.get("sector") or ""
    market_cap = co.get("market_cap")

    diag["incomplete_diag_cik"] = str(cik) if cik else "null"
    diag["incomplete_diag_active_sec_reporter"] = (
        str(active_sec).lower() if active_sec is not None else "null"
    )
    diag["incomplete_diag_is_adr"] = "true" if is_adr else "false"
    diag["incomplete_diag_sector"] = sector
    diag["incomplete_diag_market_cap_known"] = "yes" if market_cap is not None else "no"

    if last_filing is not None:
        diag["incomplete_diag_last_filing_date"] = str(last_filing)
        diag["incomplete_diag_filing_age_days"] = str((today - last_filing).days)

    # Rule 1: No CIK — cannot link to SEC at all
    if not cik:
        subcause = "missing_cik"

    # Rule 2: Has CIK but SEC reporting explicitly disabled
    elif active_sec is False:
        subcause = "missing_sec_companyfacts"

    # Rule 3: ADR or foreign registrant
    elif is_adr:
        subcause = "foreign_filer_or_adr"

    # Rule 4: Last filing is stale (> 180 days)
    elif last_filing is not None and (today - last_filing).days > 180:
        subcause = "stale_fundamentals"

    # Rule 5: No data at all — no filing date AND no market_cap (truly unknown)
    elif last_filing is None and market_cap is None:
        subcause = "recent_ipo_or_short_history"

    # Rule 6: Sector with non-standard ratios (Financials, Real Estate, Utilities)
    elif sector in _SECTOR_MODEL_GAPS:
        subcause = "sector_specific_model_gap"

    # Rule 7: Has filing date but no market_cap (Polygon enrichment missing)
    elif last_filing is not None and market_cap is None:
        subcause = "polygon_fundamentals_missing"

    # Rule 8: Has CIK, active reporter, domestic, recent filing — likely IFRS
    elif active_sec is True and not is_adr and last_filing is not None:
        subcause = "ifrs_mapping_gap"

    # Rule 9: Has some data (market_cap via SEC) but no filing date — data gap
    else:
        subcause = "price_only_scored"

    diag["incomplete_diag_subcause"] = subcause
    return subcause, diag


def _detect_sector_clusters(
    rows: list[dict],
    sector_map: dict[str, str],
) -> set[tuple[str, str, Any]]:
    """Return set of (ticker, event_date_str, window_days) that are in a 3+ cluster.

    A cluster: for a given row, 2+ OTHER rows share the same sector and window_days,
    and have event_date within ±3 days.
    """
    clusters: set[tuple[str, str, Any]] = set()

    for i, row in enumerate(rows):
        ticker = row["ticker"]
        sector = sector_map.get(ticker)
        if not sector:
            continue
        event_date = _coerce_date(row["event_date"])
        if event_date is None:
            continue
        window = row.get("window_days")
        peers = []
        for j, other in enumerate(rows):
            if i == j:
                continue
            if other["ticker"] == ticker:
                continue
            if other.get("window_days") != window:
                continue
            other_sector = sector_map.get(other["ticker"])
            if other_sector != sector:
                continue
            other_date = _coerce_date(other["event_date"])
            if other_date is None:
                continue
            if abs((other_date - event_date).days) <= 3:
                peers.append(other)
        if len(peers) >= 2:
            clusters.add((ticker, str(event_date), window))
    return clusters


def _coerce_date(value: Any) -> date | None:
    """Coerce value to a date object, or None on failure."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _best_score_key(
    ticker: str,
    event_date_raw: Any,
    score_components: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    """Return component scores for the latest score on or before event_date."""
    event_date = _coerce_date(event_date_raw)
    if event_date is None:
        return None
    candidates = [
        (date.fromisoformat(d_str), components)
        for (t, d_str), components in score_components.items()
        if t == ticker
        and _coerce_date(d_str) is not None
        and date.fromisoformat(d_str) <= event_date
    ]
    if not candidates:
        return None
    _, best_components = max(candidates, key=lambda x: x[0])
    return best_components


# ---------------------------------------------------------------------------
# Classification logic
# ---------------------------------------------------------------------------


def _assign_root_cause(
    row: dict,
    *,
    score_components: dict[tuple[str, str], dict[str, Any]],
    earnings_dates: dict[str, list[date]],
    sector_clusters: set[tuple[str, str, Any]],
    companies_data: dict[str, dict[str, Any]],
    today: date,
) -> tuple[str, dict[str, str]]:
    """First-match root-cause assignment; returns (label, diag_fields)."""
    classification = row.get("classification", "")
    ticker = str(row["ticker"])
    event_date_raw = row["event_date"]
    event_date = _coerce_date(event_date_raw)
    window = row.get("window_days")
    tier = row.get("tier_before_event") or ""

    # Priority 1
    if classification == "universe_miss":
        return "universe_not_seeded", dict(_INCOMPLETE_DIAG_EMPTY)

    # Priority 2
    if classification == "unscored_mover":
        return "pre_score_history", dict(_INCOMPLETE_DIAG_EMPTY)

    # Priority 3 — Incomplete tier: classify into specific subcause
    if tier == "Incomplete":
        co = companies_data.get(ticker)
        subcause, diag = _classify_incomplete_subcause(ticker, co, today)
        return subcause, diag

    # Priority 4 — earnings within ±7 days
    if event_date is not None:
        for e_date in earnings_dates.get(ticker, []):
            if abs((e_date - event_date).days) <= 7:
                return "missing_earnings_context", dict(_INCOMPLETE_DIAG_EMPTY)

    # Priority 5 — sector cluster
    if event_date is not None and (ticker, str(event_date), window) in sector_clusters:
        return "sector_cluster_move", dict(_INCOMPLETE_DIAG_EMPTY)

    # Priority 6
    if not row.get("had_catalyst_evidence"):
        return "no_evidence_no_filing", dict(_INCOMPLETE_DIAG_EMPTY)

    # Look up score components for priorities 7–10
    comps = _best_score_key(ticker, event_date_raw, score_components)
    catalyst_score = comps.get("catalyst_score") if comps else None
    quality_score  = comps.get("quality_score")  if comps else None

    # Determine whether this is a near-threshold event
    score = row.get("score_before_event")
    is_near_threshold = (
        score is not None
        and _NEAR_THRESHOLD_MIN <= float(score) < _NEAR_THRESHOLD_MAX
    ) or classification == "near_threshold"

    if is_near_threshold:
        # Priority 9 / 10
        if catalyst_score is not None and catalyst_score < 30:
            return "near_threshold_no_catalyst", dict(_INCOMPLETE_DIAG_EMPTY)
        return "near_threshold_scored", dict(_INCOMPLETE_DIAG_EMPTY)

    # Priority 7
    if catalyst_score is not None and catalyst_score < 30:
        return "low_catalyst_score", dict(_INCOMPLETE_DIAG_EMPTY)

    # Priority 8
    if quality_score is not None and quality_score < 40:
        return "low_quality_score", dict(_INCOMPLETE_DIAG_EMPTY)

    return "unknown", dict(_INCOMPLETE_DIAG_EMPTY)


def _build_enrichment(label: str, diag_fields: dict[str, str]) -> dict[str, str]:
    """Build enrichment fields from a root-cause label and diagnostic dict."""
    return {
        "enriched_root_cause":  label,
        "root_cause_group":     _ROOT_CAUSE_GROUPS.get(label, "unknown"),
        "explanation_short":    _EXPLANATIONS.get(label, ""),
        "evidence_fields_used": _EVIDENCE_FIELDS.get(label, ""),
        "suggested_fix":        _SUGGESTED_FIXES.get(label, ""),
        "confidence":           _CONFIDENCE.get(label, "low"),
        **diag_fields,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enrich_rows(rows: list[dict], conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Add enrichment fields (6 base + 9 diagnostic) to every row.

    Returns a new list; input rows are NOT mutated.
    Shadow-only — no scores are written to the database.
    """
    score_components = _fetch_score_components(conn)
    earnings_dates   = _fetch_earnings_dates(conn)
    sector_map       = _fetch_sector_map(conn)
    companies_data   = _fetch_companies_data(conn)
    sector_clusters  = _detect_sector_clusters(rows, sector_map)
    today            = date.today()

    result: list[dict] = []
    for row in rows:
        label, diag = _assign_root_cause(
            row,
            score_components=score_components,
            earnings_dates=earnings_dates,
            sector_clusters=sector_clusters,
            companies_data=companies_data,
            today=today,
        )
        enrichment = _build_enrichment(label, diag)
        result.append({**row, **enrichment})
    return result


def generate_enrichment_report(
    enriched_rows: list[dict],
    output_dir: str = "data/processed",
) -> tuple[Path, Path]:
    """Write enriched CSV and root-cause markdown report.

    Returns (csv_path, md_path). Shadow-only — no scores written.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # --- CSV ---
    csv_path = out / _REPORT_ENRICHED_CSV
    base_cols = [
        "ticker", "event_date", "event_type", "return_value", "window_days",
        "classification", "priority_score", "universe_tier",
        "score_before_event", "tier_before_event",
        "had_catalyst_evidence", "was_in_universe", "was_scored",
        "root_cause_hint", "score_join_method",
    ]
    all_cols = base_cols + _ENRICHMENT_EXTRA_COLS

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(enriched_rows)

    # --- Markdown ---
    md_path = out / _REPORT_ENRICHED_MD
    lines: list[str] = _build_md(enriched_rows)
    md_path.write_text("\n".join(lines) + "\n")

    logger.info("Root-cause enrichment report: %s (%d rows)", md_path, len(enriched_rows))
    return csv_path, md_path


def _build_md(enriched_rows: list[dict]) -> list[str]:
    """Render the markdown report as a list of lines."""
    today = date.today().isoformat()

    group_counts: Counter[str] = Counter(
        r.get("root_cause_group", "unknown") for r in enriched_rows
    )
    label_counts: Counter[str] = Counter(
        r.get("enriched_root_cause", "unknown") for r in enriched_rows
    )

    lines: list[str] = [
        "# Root Cause Enrichment Report",
        "",
        f"Generated: {today} | Total rows: {len(enriched_rows)}",
        "",
        "> **Shadow-only: no production scores were changed.**",
        "",
    ]

    # --- Group Summary ---
    lines += [
        "## Root Cause Group Summary",
        "",
        "| Group | Count |",
        "|-------|-------|",
    ]
    for group in ("data_gap", "scoring_gap", "feature_gap", "near_miss", "universe_gap", "unknown"):
        if group in group_counts:
            lines.append(f"| `{group}` | {group_counts[group]} |")
    lines.append("")

    # --- Detailed Breakdown ---
    lines += [
        "## Detailed Root Cause Breakdown",
        "",
        "| Root Cause | Group | Count | Confidence |",
        "|------------|-------|-------|------------|",
    ]
    for label in sorted(label_counts, key=lambda l: label_counts[l], reverse=True):
        group      = _ROOT_CAUSE_GROUPS.get(label, "unknown")
        confidence = _CONFIDENCE.get(label, "low")
        lines.append(f"| `{label}` | {group} | {label_counts[label]} | {confidence} |")
    lines.append("")

    # --- Incomplete Fundamentals Subcause Breakdown ---
    incomplete_subcause_labels = {
        "missing_cik", "missing_sec_companyfacts", "foreign_filer_or_adr",
        "stale_fundamentals", "recent_ipo_or_short_history", "sector_specific_model_gap",
        "polygon_fundamentals_missing", "ifrs_mapping_gap", "price_only_scored",
    }
    incomplete_rows = [
        r for r in enriched_rows
        if r.get("enriched_root_cause") in incomplete_subcause_labels
    ]
    if incomplete_rows:
        subcause_counts: Counter[str] = Counter(
            r.get("enriched_root_cause", "unknown") for r in incomplete_rows
        )
        lines += [
            "## Incomplete Fundamentals — Subcause Breakdown",
            "",
            f"Total Incomplete-tier rows: {len(incomplete_rows)}",
            "",
            "| Subcause | Count | Suggested Fix |",
            "|----------|-------|---------------|",
        ]
        for subcause in sorted(subcause_counts, key=lambda l: subcause_counts[l], reverse=True):
            fix = _SUGGESTED_FIXES.get(subcause, "—")
            lines.append(f"| `{subcause}` | {subcause_counts[subcause]} | {fix} |")
        lines.append("")

        # Top tickers per subcause
        lines += ["### Top Tickers by Subcause", ""]
        by_subcause: dict[str, list[str]] = {}
        for r in incomplete_rows:
            sc = r.get("enriched_root_cause", "unknown")
            by_subcause.setdefault(sc, []).append(r.get("ticker", ""))
        for subcause in sorted(subcause_counts, key=lambda l: subcause_counts[l], reverse=True):
            tickers_for_sc = list(dict.fromkeys(by_subcause.get(subcause, [])))[:10]
            lines.append(f"**{subcause}**: {', '.join(tickers_for_sc)}")
        lines.append("")

    # --- Top Enriched Rows ---
    lines += [
        "## Top Enriched Rows (true_miss / scored_missed / near_threshold)",
        "",
    ]
    focus_rows = [
        r for r in enriched_rows
        if r.get("classification") in ("true_miss", "scored_missed", "near_threshold")
    ]
    if focus_rows:
        lines += [
            "| Ticker | Date | Window | Score | Root Cause | Group | Fix |",
            "|--------|------|--------|-------|------------|-------|-----|",
        ]
        for r in focus_rows[:50]:
            score = (
                f"{r['score_before_event']:.1f}"
                if r.get("score_before_event") is not None
                else "—"
            )
            lines.append(
                f"| {r['ticker']}"
                f" | {r.get('event_date', '—')}"
                f" | {r.get('window_days', '—')}d"
                f" | {score}"
                f" | `{r.get('enriched_root_cause', '—')}`"
                f" | {r.get('root_cause_group', '—')}"
                f" | {r.get('suggested_fix', '—')} |"
            )
    else:
        lines.append("_(no true_miss / scored_missed / near_threshold rows)_")
    lines.append("")

    return lines
