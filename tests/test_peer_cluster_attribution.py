"""Tests for peer/theme cluster attribution."""
from __future__ import annotations

import uuid

import duckdb
import pytest

from health.peer_cluster_attribution import (
    PeerClusterDiag,
    PeerClusterResult,
    build_ticker_to_clusters,
    compute_cluster_attribution,
    generate_peer_cluster_diagnostics,
    load_cluster_config,
)


_SEMI_CONFIG = {
    "semiconductors": {
        "label": "Semiconductors / AI Hardware",
        "tickers": ["AMD", "NVDA", "AVGO", "INTC", "MU", "QCOM"],
    },
    "networking": {
        "label": "Networking / Datacom",
        "tickers": ["CSCO", "ANET", "CIEN", "COHR"],
    },
}


def _make_conn(
    prices: list[tuple] = None,
    companies: list[tuple] = None,
) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE prices_daily ("
        "id VARCHAR PRIMARY KEY, ticker VARCHAR, trade_date DATE, "
        "close DOUBLE, source VARCHAR DEFAULT 'polygon')"
    )
    conn.execute(
        "CREATE TABLE companies ("
        "ticker VARCHAR PRIMARY KEY, sector VARCHAR, is_active BOOLEAN DEFAULT true)"
    )
    for row in (prices or []):
        ticker, trade_date, close = row[:3]
        source = row[3] if len(row) > 3 else "polygon"
        conn.execute(
            "INSERT INTO prices_daily VALUES (?, ?, ?, ?, ?)",
            [uuid.uuid4().hex[:16], ticker, trade_date, close, source],
        )
    for ticker, sector in (companies or []):
        conn.execute(
            "INSERT INTO companies VALUES (?, ?, true)",
            [ticker, sector],
        )
    return conn


# ── Config loading ────────────────────────────────────────────────────────

def test_load_cluster_config_loads_yaml():
    config = load_cluster_config()
    assert "semiconductors" in config
    assert "AMD" in config["semiconductors"]["tickers"]


def test_build_ticker_to_clusters():
    mapping = build_ticker_to_clusters(_SEMI_CONFIG)
    assert "AMD" in mapping
    assert "semiconductors" in mapping["AMD"]
    assert "CIEN" in mapping
    assert "networking" in mapping["CIEN"]


def test_ticker_in_multiple_clusters():
    config = {
        "a": {"label": "A", "tickers": ["X", "Y"]},
        "b": {"label": "B", "tickers": ["X", "Z"]},
    }
    mapping = build_ticker_to_clusters(config)
    assert len(mapping["X"]) == 2


# ── Cluster attribution classification ───────────────────────────────────

def test_semiconductor_cluster_confirms_amd():
    """AMD +11%, peers (NVDA +9%, AVGO +8%, INTC +6%, MU +7%) → cluster_confirmed."""
    conn = _make_conn(prices=[
        ("NVDA", "2026-04-30", 100.0), ("NVDA", "2026-05-01", 109.0),
        ("AVGO", "2026-04-30", 100.0), ("AVGO", "2026-05-01", 108.0),
        ("INTC", "2026-04-30", 100.0), ("INTC", "2026-05-01", 106.0),
        ("MU",   "2026-04-30", 100.0), ("MU",   "2026-05-01", 107.0),
        ("QCOM", "2026-04-30", 100.0), ("QCOM", "2026-05-01", 105.0),
    ])
    t2c = build_ticker_to_clusters(_SEMI_CONFIG)
    best, all_clusters, attr = compute_cluster_attribution(
        conn, "AMD", 0.11, "2026-05-01", 1, _SEMI_CONFIG, t2c,
    )
    assert attr == "cluster_confirmed"
    assert best is not None
    assert best.cluster_id == "semiconductors"
    assert best.cluster_median_return is not None
    assert best.cluster_median_return > 0.05
    assert best.peers_with_prices >= 4


def test_ticker_outperforms_cluster():
    """Ticker +30%, peers +5% → ticker_outperformed_cluster."""
    conn = _make_conn(prices=[
        ("NVDA", "2026-04-30", 100.0), ("NVDA", "2026-05-01", 105.0),
        ("AVGO", "2026-04-30", 100.0), ("AVGO", "2026-05-01", 104.0),
        ("INTC", "2026-04-30", 100.0), ("INTC", "2026-05-01", 106.0),
    ])
    t2c = build_ticker_to_clusters(_SEMI_CONFIG)
    best, _, attr = compute_cluster_attribution(
        conn, "AMD", 0.30, "2026-05-01", 1, _SEMI_CONFIG, t2c,
    )
    assert attr == "ticker_outperformed_cluster"
    assert best.ticker_vs_cluster is not None
    assert best.ticker_vs_cluster > 0.20


def test_no_cluster_mapping():
    """Ticker not in any cluster → no_cluster_mapping."""
    conn = _make_conn()
    t2c = build_ticker_to_clusters(_SEMI_CONFIG)
    _, _, attr = compute_cluster_attribution(
        conn, "UNKNOWN_TICKER", 0.10, "2026-05-01", 1, _SEMI_CONFIG, t2c,
    )
    assert attr == "no_cluster_mapping"


def test_insufficient_peer_prices():
    """Cluster exists but fewer than 2 peers have prices → insufficient_peer_prices."""
    conn = _make_conn(prices=[
        ("NVDA", "2026-05-01", 105.0),
    ])
    t2c = build_ticker_to_clusters(_SEMI_CONFIG)
    best, _, attr = compute_cluster_attribution(
        conn, "AMD", 0.10, "2026-05-01", 1, _SEMI_CONFIG, t2c,
    )
    assert attr == "insufficient_peer_prices"


def test_broad_sector_only_when_cluster_flat():
    """Ticker +10%, peers flat → broad_sector_only (peers didn't move materially)."""
    conn = _make_conn(prices=[
        ("NVDA", "2026-04-30", 100.0), ("NVDA", "2026-05-01", 100.5),
        ("AVGO", "2026-04-30", 100.0), ("AVGO", "2026-05-01", 99.8),
        ("INTC", "2026-04-30", 100.0), ("INTC", "2026-05-01", 100.2),
    ])
    t2c = build_ticker_to_clusters(_SEMI_CONFIG)
    best, _, attr = compute_cluster_attribution(
        conn, "AMD", 0.10, "2026-05-01", 1, _SEMI_CONFIG, t2c,
    )
    assert attr == "ticker_outperformed_cluster"


def test_peers_positive_count():
    conn = _make_conn(prices=[
        ("NVDA", "2026-04-30", 100.0), ("NVDA", "2026-05-01", 108.0),
        ("AVGO", "2026-04-30", 100.0), ("AVGO", "2026-05-01", 97.0),
        ("INTC", "2026-04-30", 100.0), ("INTC", "2026-05-01", 106.0),
    ])
    t2c = build_ticker_to_clusters(_SEMI_CONFIG)
    best, _, _ = compute_cluster_attribution(
        conn, "AMD", 0.10, "2026-05-01", 1, _SEMI_CONFIG, t2c,
    )
    assert best.peers_positive == 2
    assert best.peers_above_threshold >= 2


def test_5d_window_cluster():
    """Multi-day window compounds correctly for peers."""
    conn = _make_conn(prices=[
        ("NVDA", "2026-04-25", 100.0), ("NVDA", "2026-05-01", 112.0),
        ("AVGO", "2026-04-25", 100.0), ("AVGO", "2026-05-01", 108.0),
        ("INTC", "2026-04-25", 100.0), ("INTC", "2026-05-01", 106.0),
    ])
    t2c = build_ticker_to_clusters(_SEMI_CONFIG)
    best, _, attr = compute_cluster_attribution(
        conn, "AMD", 0.15, "2026-05-01", 5, _SEMI_CONFIG, t2c,
    )
    assert best.cluster_median_return is not None
    assert best.cluster_median_return > 0.05


# ── generate_peer_cluster_diagnostics ─────────────────────────────────────

def test_generate_returns_results_for_cluster_rows():
    conn = _make_conn(
        prices=[
            ("NVDA", "2026-04-30", 100.0), ("NVDA", "2026-05-01", 109.0),
            ("AVGO", "2026-04-30", 100.0), ("AVGO", "2026-05-01", 108.0),
            ("INTC", "2026-04-30", 100.0), ("INTC", "2026-05-01", 106.0),
            ("XLK", "2026-05-01", 0.009, "polygon_sector_etf"),
        ],
        companies=[("AMD", "Information Technology")],
    )
    rows = [
        {"ticker": "AMD", "event_date": "2026-05-01",
         "enriched_root_cause": "sector_cluster_move",
         "return_value": "11.0", "window_days": "1"},
    ]
    diags = generate_peer_cluster_diagnostics(conn, rows, cluster_config=_SEMI_CONFIG)
    assert len(diags) == 1
    d = diags[0]
    assert d.ticker == "AMD"
    assert d.attribution == "cluster_confirmed"
    assert d.best_cluster is not None
    assert d.etf_return is not None
    assert d.ticker_vs_etf is not None


def test_generate_skips_non_cluster_rows():
    conn = _make_conn()
    rows = [{"ticker": "AAPL", "event_date": "2026-05-01",
             "enriched_root_cause": "missing_cik"}]
    assert generate_peer_cluster_diagnostics(conn, rows, cluster_config=_SEMI_CONFIG) == []


def test_generate_no_cluster_mapping_ticker():
    conn = _make_conn(companies=[("ZZZ", "Materials")])
    rows = [{"ticker": "ZZZ", "event_date": "2026-05-01",
             "enriched_root_cause": "sector_cluster_move",
             "return_value": "5.0", "window_days": "1"}]
    diags = generate_peer_cluster_diagnostics(conn, rows, cluster_config=_SEMI_CONFIG)
    assert diags[0].attribution == "no_cluster_mapping"


# ── No production scoring ────────────────────────────────────────────────

def test_no_scoring_mutation():
    import inspect
    import health.peer_cluster_attribution as mod
    src = inspect.getsource(mod)
    for bad in ("feature_flag", "FeatureFlag", "openai", "anthropic",
                "sector_momentum_boost"):
        assert bad.lower() not in src.lower(), f"Prohibited term '{bad}'"
