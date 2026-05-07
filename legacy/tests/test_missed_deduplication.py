"""TDD tests for missed-opportunity event deduplication / clustering.

RED state: cluster_events() does not exist yet.
"""
from __future__ import annotations

from datetime import date


def _make_event(ticker, event_date, event_type, return_value, window_days=20):
    return {
        "event_id": f"{ticker}_{event_date}_{event_type}",
        "ticker": ticker,
        "event_date": date.fromisoformat(event_date) if isinstance(event_date, str) else event_date,
        "event_type": event_type,
        "return_value": return_value,
        "window_days": window_days,
        "reference_price": 100.0,
        "peak_price": 100.0 * (1 + return_value / 100),
        "was_in_universe": True,
        "was_scored": False,
        "score_before_event": None,
        "tier_before_event": None,
        "was_rejected": False,
        "was_incomplete": False,
        "had_catalyst_evidence": False,
    }


# ── basic clustering ──────────────────────────────────────────────────────────

def test_single_event_returns_one_cluster():
    """A single event forms a cluster of size 1."""
    from missed.detector import cluster_events
    events = [_make_event("AAPL", "2026-01-20", "gain_20d_20pct", 25.0)]
    result = cluster_events(events)
    assert len(result) == 1
    assert result[0]["raw_detection_count"] == 1


def test_overlapping_events_same_ticker_same_type_merged():
    """Two overlapping 20-day windows for same ticker+type become one cluster."""
    from missed.detector import cluster_events
    events = [
        _make_event("AAPL", "2026-01-20", "gain_20d_20pct", 25.0),
        _make_event("AAPL", "2026-01-25", "gain_20d_20pct", 28.0),  # 5 days later, same move
    ]
    result = cluster_events(events)
    assert len(result) == 1, f"Expected 1 cluster, got {len(result)}"
    assert result[0]["raw_detection_count"] == 2


def test_non_overlapping_events_form_separate_clusters():
    """Events > window_days apart are separate clusters."""
    from missed.detector import cluster_events
    events = [
        _make_event("AAPL", "2026-01-01", "gain_20d_20pct", 25.0, window_days=20),
        _make_event("AAPL", "2026-03-01", "gain_20d_20pct", 22.0, window_days=20),  # 59 days later
    ]
    result = cluster_events(events)
    assert len(result) == 2, f"Events >20 days apart should be separate clusters, got {len(result)}"


def test_different_tickers_never_clustered():
    """Events for different tickers are never clustered together."""
    from missed.detector import cluster_events
    events = [
        _make_event("AAPL", "2026-01-20", "gain_20d_20pct", 25.0),
        _make_event("MSFT", "2026-01-20", "gain_20d_20pct", 24.0),
    ]
    result = cluster_events(events)
    assert len(result) == 2


def test_different_event_types_not_clustered():
    """gain_5d_10pct and gain_20d_20pct events for same ticker are separate clusters."""
    from missed.detector import cluster_events
    events = [
        _make_event("AAPL", "2026-01-20", "gain_5d_10pct", 12.0, window_days=5),
        _make_event("AAPL", "2026-01-20", "gain_20d_20pct", 25.0, window_days=20),
    ]
    result = cluster_events(events)
    assert len(result) == 2


# ── cluster selection ─────────────────────────────────────────────────────────

def test_cluster_primary_event_is_highest_return():
    """The event with the highest return_value is the primary for the cluster."""
    from missed.detector import cluster_events
    events = [
        _make_event("AAPL", "2026-01-20", "gain_20d_20pct", 22.0),
        _make_event("AAPL", "2026-01-22", "gain_20d_20pct", 28.0),  # highest
        _make_event("AAPL", "2026-01-24", "gain_20d_20pct", 25.0),
    ]
    result = cluster_events(events)
    assert len(result) == 1
    assert result[0]["return_value"] == 28.0
    assert result[0]["event_date"] == date(2026, 1, 22)


def test_cluster_raw_detection_count_reflects_all_events():
    """raw_detection_count counts all overlapping detections in the cluster."""
    from missed.detector import cluster_events
    events = [
        _make_event("SPOT", "2026-01-01", "gain_20d_20pct", 20.0),
        _make_event("SPOT", "2026-01-05", "gain_20d_20pct", 22.0),
        _make_event("SPOT", "2026-01-10", "gain_20d_20pct", 25.0),
    ]
    result = cluster_events(events)
    assert len(result) == 1
    assert result[0]["raw_detection_count"] == 3


def test_cluster_window_start_end_computed():
    """event_window_start and event_window_end cover all events in the cluster."""
    from missed.detector import cluster_events
    events = [
        _make_event("AAPL", "2026-01-01", "gain_20d_20pct", 21.0),
        _make_event("AAPL", "2026-01-10", "gain_20d_20pct", 25.0),
        _make_event("AAPL", "2026-01-18", "gain_20d_20pct", 23.0),
    ]
    result = cluster_events(events)
    assert len(result) == 1
    assert result[0]["event_window_start"] == date(2026, 1, 1)
    assert result[0]["event_window_end"] == date(2026, 1, 18)


def test_cluster_id_is_assigned():
    """Each cluster has a non-null cluster_id string."""
    from missed.detector import cluster_events
    events = [_make_event("AAPL", "2026-01-20", "gain_20d_20pct", 25.0)]
    result = cluster_events(events)
    assert result[0].get("cluster_id") is not None
    assert isinstance(result[0]["cluster_id"], str)


def test_empty_event_list_returns_empty():
    """cluster_events([]) returns []."""
    from missed.detector import cluster_events
    assert cluster_events([]) == []


def test_multiple_tickers_clustered_independently():
    """Overlapping windows for AAPL and MSFT each produce one cluster (not combined)."""
    from missed.detector import cluster_events
    events = [
        _make_event("AAPL", "2026-01-01", "gain_20d_20pct", 25.0),
        _make_event("AAPL", "2026-01-05", "gain_20d_20pct", 27.0),
        _make_event("MSFT", "2026-01-01", "gain_20d_20pct", 22.0),
        _make_event("MSFT", "2026-01-08", "gain_20d_20pct", 24.0),
    ]
    result = cluster_events(events)
    assert len(result) == 2
    tickers = {e["ticker"] for e in result}
    assert tickers == {"AAPL", "MSFT"}
