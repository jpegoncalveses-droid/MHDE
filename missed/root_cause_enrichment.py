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
        "Tier=Incomplete: fewer than 2 fundamental components available.",
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
    "no_evidence_no_filing":
        "Add EFTS fallback or press-release scraper to increase filing coverage.",
    "missing_earnings_context":
        "Add EPS estimates adapter; wire earnings-proximity feature to scoring.",
    "sector_cluster_move":
        "Seed sector ETF tickers (XLF/XLK/XLE etc.) to enable sector-momentum feature.",
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
    "universe_not_seeded":        "high",
    "pre_score_history":          "high",
    "incomplete_fundamentals":    "high",
    "no_evidence_no_filing":      "medium",
    "missing_earnings_context":   "medium",
    "sector_cluster_move":        "medium",
    "low_catalyst_score":         "medium",
    "low_quality_score":          "low",
    "near_threshold_no_catalyst": "medium",
    "near_threshold_scored":      "medium",
    "unknown":                    "low",
}

_EVIDENCE_FIELDS: dict[str, str] = {
    "universe_not_seeded":        "was_in_universe,classification",
    "pre_score_history":          "classification,score_join_method",
    "incomplete_fundamentals":    "tier_before_event",
    "no_evidence_no_filing":      "had_catalyst_evidence",
    "missing_earnings_context":   "event_date,events.event_date,events.event_type",
    "sector_cluster_move":        "companies.sector,window_days,event_date",
    "low_catalyst_score":         "scores.catalyst_score",
    "low_quality_score":          "scores.quality_score",
    "near_threshold_no_catalyst": "score_before_event,scores.catalyst_score",
    "near_threshold_scored":      "score_before_event,scores.catalyst_score",
    "unknown":                    "",
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
]

# Threshold boundaries (must match prediction_report.py)
_NEAR_THRESHOLD_MIN = 40.0
_NEAR_THRESHOLD_MAX = 45.0

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
) -> str:
    """First-match root-cause assignment; returns a label string."""
    classification = row.get("classification", "")
    ticker = str(row["ticker"])
    event_date_raw = row["event_date"]
    event_date = _coerce_date(event_date_raw)
    window = row.get("window_days")
    tier = row.get("tier_before_event") or ""

    # Priority 1
    if classification == "universe_miss":
        return "universe_not_seeded"

    # Priority 2
    if classification == "unscored_mover":
        return "pre_score_history"

    # Priority 3
    if tier == "Incomplete":
        return "incomplete_fundamentals"

    # Priority 4 — earnings within ±7 days
    if event_date is not None:
        for e_date in earnings_dates.get(ticker, []):
            if abs((e_date - event_date).days) <= 7:
                return "missing_earnings_context"

    # Priority 5 — sector cluster
    if event_date is not None and (ticker, str(event_date), window) in sector_clusters:
        return "sector_cluster_move"

    # Priority 6
    if not row.get("had_catalyst_evidence"):
        return "no_evidence_no_filing"

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
            return "near_threshold_no_catalyst"
        return "near_threshold_scored"

    # Priority 7
    if catalyst_score is not None and catalyst_score < 30:
        return "low_catalyst_score"

    # Priority 8
    if quality_score is not None and quality_score < 40:
        return "low_quality_score"

    return "unknown"


def _build_enrichment(label: str) -> dict[str, str]:
    """Build the 6 enrichment fields from a root-cause label."""
    return {
        "enriched_root_cause":  label,
        "root_cause_group":     _ROOT_CAUSE_GROUPS.get(label, "unknown"),
        "explanation_short":    _EXPLANATIONS.get(label, ""),
        "evidence_fields_used": _EVIDENCE_FIELDS.get(label, ""),
        "suggested_fix":        _SUGGESTED_FIXES.get(label, ""),
        "confidence":           _CONFIDENCE.get(label, "low"),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enrich_rows(rows: list[dict], conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Add 6 enrichment fields to every row.

    Returns a new list; input rows are NOT mutated.
    Shadow-only — no scores are written to the database.
    """
    score_components = _fetch_score_components(conn)
    earnings_dates   = _fetch_earnings_dates(conn)
    sector_map       = _fetch_sector_map(conn)
    sector_clusters  = _detect_sector_clusters(rows, sector_map)

    result: list[dict] = []
    for row in rows:
        label      = _assign_root_cause(
            row,
            score_components=score_components,
            earnings_dates=earnings_dates,
            sector_clusters=sector_clusters,
        )
        enrichment = _build_enrichment(label)
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
