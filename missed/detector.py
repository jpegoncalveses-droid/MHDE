"""Missed-opportunity detector.

Scans prices_daily for significant price moves and annotates each detected event
with universe/scoring context that existed BEFORE the move.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta

import duckdb

from missed.labels import (
    GAIN_5D_THRESHOLD,
    GAIN_20D_THRESHOLD,
    GAIN_60D_THRESHOLD,
)

logger = logging.getLogger("mhde.missed.detector")


def detect_missed_opportunities(
    conn: duckdb.DuckDBPyConnection,
    lookback_days: int = 90,
) -> list[dict]:
    """
    Scan prices_daily for significant moves in the past lookback_days.
    Returns a list of event dicts — does NOT write to DB (caller decides to persist).
    """
    cutoff = date.today() - timedelta(days=lookback_days)
    events: list[dict] = []

    _detect_gains(conn, cutoff, 5, GAIN_5D_THRESHOLD, "gain_5d_10pct", events)
    _detect_gains(conn, cutoff, 20, GAIN_20D_THRESHOLD, "gain_20d_20pct", events)
    _detect_gains(conn, cutoff, 60, GAIN_60D_THRESHOLD, "gain_60d_30pct", events)
    _detect_52wk_breakouts(conn, cutoff, events)

    return events


def _detect_gains(
    conn: duckdb.DuckDBPyConnection,
    cutoff: date,
    window: int,
    threshold: float,
    event_type: str,
    events: list[dict],
) -> None:
    rows = conn.execute(
        """
        SELECT p_now.ticker,
               p_now.trade_date AS event_date,
               p_before.close   AS ref_price,
               p_now.close      AS peak_price
        FROM prices_daily p_now
        JOIN prices_daily p_before
          ON p_now.ticker = p_before.ticker
         AND p_before.trade_date = (
             SELECT MAX(trade_date) FROM prices_daily
              WHERE ticker = p_now.ticker
                AND trade_date <= p_now.trade_date - INTERVAL (? || ' days')
         )
        WHERE p_now.trade_date >= ?
          AND p_before.close > 0
          AND (p_now.close - p_before.close) / p_before.close >= ?
        ORDER BY p_now.ticker, p_now.trade_date
        """,
        [window, cutoff.isoformat(), threshold],
    ).fetchall()

    for ticker, event_date, ref_price, peak_price in rows:
        if isinstance(event_date, str):
            event_date = date.fromisoformat(event_date)
        return_value = (peak_price - ref_price) / ref_price * 100
        events.append(_build_event(conn, ticker, event_date, event_type,
                                   return_value, window, ref_price, peak_price))


def _detect_52wk_breakouts(
    conn: duckdb.DuckDBPyConnection,
    cutoff: date,
    events: list[dict],
) -> None:
    rows = conn.execute(
        """
        SELECT p.ticker, p.trade_date, p.close,
               (SELECT MAX(ph.close) FROM prices_daily ph
                 WHERE ph.ticker = p.ticker
                   AND ph.trade_date < p.trade_date
                   AND ph.trade_date >= p.trade_date - INTERVAL '252 days') AS prior_high
        FROM prices_daily p
        WHERE p.trade_date >= ?
        ORDER BY p.ticker, p.trade_date
        """,
        [cutoff.isoformat()],
    ).fetchall()

    seen: set[str] = set()
    for ticker, event_date, close, prior_high in rows:
        if prior_high is None or close is None:
            continue
        if isinstance(event_date, str):
            event_date = date.fromisoformat(event_date)
        if close > prior_high and ticker not in seen:
            seen.add(ticker)
            return_val = (close - prior_high) / prior_high * 100
            events.append(_build_event(conn, ticker, event_date, "52wk_high_breakout",
                                       return_val, 252, prior_high, close))


def _build_event(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    event_date: date,
    event_type: str,
    return_value: float,
    window_days: int,
    ref_price: float,
    peak_price: float,
) -> dict:
    was_in_universe = _was_in_universe(conn, ticker)
    was_scored, score_before, tier_before = _score_before_event(conn, ticker, event_date)
    had_catalyst = _had_catalyst_evidence(conn, ticker, event_date)

    return {
        "event_id": uuid.uuid4().hex[:16],
        "ticker": ticker,
        "event_date": event_date,
        "event_type": event_type,
        "return_value": round(return_value, 2),
        "window_days": window_days,
        "reference_price": ref_price,
        "peak_price": peak_price,
        "was_in_universe": was_in_universe,
        "was_scored": was_scored,
        "score_before_event": score_before,
        "tier_before_event": tier_before,
        "was_rejected": tier_before == "Reject" if tier_before else False,
        "was_incomplete": tier_before == "Incomplete" if tier_before else False,
        "had_catalyst_evidence": had_catalyst,
    }


def _was_in_universe(conn: duckdb.DuckDBPyConnection, ticker: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE ticker = ?", [ticker]
    ).fetchone()
    return bool(row and row[0] > 0)


def _score_before_event(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    event_date: date,
) -> tuple[bool, float | None, str | None]:
    row = conn.execute(
        """SELECT total_score, tier FROM scores
           WHERE ticker = ? AND as_of_date < ?
           ORDER BY as_of_date DESC LIMIT 1""",
        [ticker, event_date.isoformat()],
    ).fetchone()
    if row:
        return True, row[0], row[1]
    return False, None, None


def _had_catalyst_evidence(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    event_date: date,
) -> bool:
    row = conn.execute(
        """SELECT COUNT(*) FROM filings
           WHERE ticker = ? AND filing_date < ?
             AND filing_date >= ?""",
        [ticker, event_date.isoformat(),
         (event_date - timedelta(days=60)).isoformat()],
    ).fetchone()
    return bool(row and row[0] > 0)


def cluster_events(events: list[dict]) -> list[dict]:
    """
    Deduplicate overlapping detections of the same price move.

    Groups events by (ticker, event_type) and merges any whose event_dates
    are within window_days of each other (they represent overlapping detection
    windows on the same underlying move).

    Returns one event per cluster — the one with the highest return_value —
    enriched with cluster metadata: cluster_id, raw_detection_count,
    event_window_start, event_window_end.
    """
    if not events:
        return []

    from itertools import groupby

    key_fn = lambda e: (e["ticker"], e["event_type"])
    sorted_events = sorted(events, key=lambda e: (e["ticker"], e["event_type"], e["event_date"]))

    result: list[dict] = []
    for (ticker, event_type), group_iter in groupby(sorted_events, key=key_fn):
        group = list(group_iter)
        clusters: list[list[dict]] = []
        current_cluster: list[dict] = [group[0]]

        for evt in group[1:]:
            prev = current_cluster[-1]
            window = evt.get("window_days", 20)
            delta = (evt["event_date"] - prev["event_date"]).days
            if delta <= window:
                current_cluster.append(evt)
            else:
                clusters.append(current_cluster)
                current_cluster = [evt]
        clusters.append(current_cluster)

        for cluster in clusters:
            primary = max(cluster, key=lambda e: e["return_value"])
            enriched = dict(primary)
            enriched["cluster_id"] = uuid.uuid4().hex[:16]
            enriched["raw_detection_count"] = len(cluster)
            enriched["event_window_start"] = min(e["event_date"] for e in cluster)
            enriched["event_window_end"] = max(e["event_date"] for e in cluster)
            result.append(enriched)

    return result


def persist_events(conn: duckdb.DuckDBPyConnection, events: list[dict]) -> int:
    """Write detected events to missed_opportunity_events table. Returns insert count."""
    inserted = 0
    for e in events:
        try:
            conn.execute(
                """INSERT OR IGNORE INTO missed_opportunity_events
                   (event_id, ticker, event_date, event_type, return_value, window_days,
                    reference_price, peak_price, was_in_universe, was_scored,
                    score_before_event, tier_before_event, was_rejected, was_incomplete,
                    had_catalyst_evidence, investigation_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                [e["event_id"], e["ticker"], e["event_date"].isoformat(),
                 e["event_type"], e["return_value"], e["window_days"],
                 e.get("reference_price"), e.get("peak_price"),
                 e["was_in_universe"], e["was_scored"],
                 e.get("score_before_event"), e.get("tier_before_event"),
                 e.get("was_rejected", False), e.get("was_incomplete", False),
                 e["had_catalyst_evidence"]],
            )
            inserted += 1
        except Exception as exc:
            logger.warning("Failed to persist event %s: %s", e.get("event_id"), exc)
    return inserted
