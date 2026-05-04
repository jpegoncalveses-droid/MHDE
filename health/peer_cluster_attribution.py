"""Peer/theme cluster attribution for sector_cluster_move rows.

Compares each ticker's move against tighter peer baskets (not just broad
sector ETFs) to distinguish cluster-wide moves from stock-specific events.
"""
from __future__ import annotations

import datetime
import logging
import os
import statistics
from dataclasses import dataclass, field
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "peer_theme_clusters.yaml",
)

_CLUSTER_CONFIRMED_THRESHOLD = 0.03
_OUTPERFORMANCE_THRESHOLD = 0.05
_MIN_PEERS_FOR_CLUSTER = 2


def load_cluster_config(path: str = _CONFIG_PATH) -> dict[str, dict]:
    """Load peer_theme_clusters.yaml. Returns {cluster_id: {label, tickers}}."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return raw.get("clusters", {})
    except Exception as exc:
        logger.warning("peer_cluster: could not load config: %s", exc)
        return {}


def build_ticker_to_clusters(config: dict[str, dict]) -> dict[str, list[str]]:
    """Return {ticker: [cluster_id, ...]} from config."""
    mapping: dict[str, list[str]] = {}
    for cluster_id, cluster in config.items():
        for ticker in cluster.get("tickers", []):
            mapping.setdefault(ticker, []).append(cluster_id)
    return mapping


def _get_peer_returns(
    conn,
    peer_tickers: list[str],
    event_date: str,
    window_days: int,
) -> dict[str, float]:
    """Return {ticker: return} for peers over the event window."""
    try:
        ed = datetime.date.fromisoformat(event_date)
    except (ValueError, TypeError):
        return {}

    lookback = ed - datetime.timedelta(days=max(window_days * 2 + 5, 10))

    try:
        placeholders = ",".join(["?"] * len(peer_tickers))
        rows = conn.execute(
            f"""
            SELECT ticker, trade_date, close FROM prices_daily
            WHERE ticker IN ({placeholders})
              AND trade_date BETWEEN ? AND ?
              AND source != 'polygon_sector_etf'
            ORDER BY ticker, trade_date
            """,
            [*peer_tickers, str(lookback), event_date],
        ).fetchall()
    except Exception:
        return {}

    by_ticker: dict[str, list[tuple]] = {}
    for ticker, trade_date, close in rows:
        by_ticker.setdefault(ticker, []).append((trade_date, close))

    returns: dict[str, float] = {}
    for ticker, prices in by_ticker.items():
        if len(prices) < 2:
            continue
        end_price = None
        for d, c in reversed(prices):
            if str(d) <= event_date:
                end_price = c
                break
        if end_price is None:
            continue

        if window_days <= 1:
            start_price = None
            for d, c in prices:
                if str(d) < event_date:
                    start_price = c
        else:
            target_start = ed - datetime.timedelta(days=window_days)
            start_price = None
            for d, c in prices:
                if str(d) <= str(target_start):
                    start_price = c
            if start_price is None:
                start_price = prices[0][1]

        if start_price and start_price != 0:
            returns[ticker] = round((end_price - start_price) / start_price, 6)

    return returns


@dataclass
class PeerClusterResult:
    cluster_id: str
    cluster_label: str
    peer_count: int
    peers_with_prices: int
    cluster_median_return: Optional[float]
    cluster_avg_return: Optional[float]
    ticker_vs_cluster: Optional[float]
    peers_positive: int
    peers_above_threshold: int


@dataclass
class PeerClusterDiag:
    ticker: str
    event_date: str
    window_days: Optional[int]
    sector: Optional[str]
    etf_return: Optional[float]
    ticker_return: Optional[float]
    ticker_vs_etf: Optional[float]
    best_cluster: Optional[PeerClusterResult]
    attribution: str
    all_clusters: list[PeerClusterResult] = field(default_factory=list)


def compute_cluster_attribution(
    conn,
    ticker: str,
    ticker_return: float,
    event_date: str,
    window_days: int,
    cluster_config: dict[str, dict],
    ticker_to_clusters: dict[str, list[str]],
) -> tuple[Optional[PeerClusterResult], list[PeerClusterResult], str]:
    """Compute peer cluster returns and classify the attribution.

    Returns (best_cluster, all_clusters, attribution_label).
    """
    cluster_ids = ticker_to_clusters.get(ticker, [])
    if not cluster_ids:
        return None, [], "no_cluster_mapping"

    all_results: list[PeerClusterResult] = []

    for cid in cluster_ids:
        cluster = cluster_config.get(cid, {})
        label = cluster.get("label", cid)
        peers = [t for t in cluster.get("tickers", []) if t != ticker]

        if not peers:
            continue

        peer_returns = _get_peer_returns(conn, peers, event_date, window_days)

        if len(peer_returns) < _MIN_PEERS_FOR_CLUSTER:
            all_results.append(PeerClusterResult(
                cluster_id=cid,
                cluster_label=label,
                peer_count=len(peers),
                peers_with_prices=len(peer_returns),
                cluster_median_return=None,
                cluster_avg_return=None,
                ticker_vs_cluster=None,
                peers_positive=0,
                peers_above_threshold=0,
            ))
            continue

        ret_values = list(peer_returns.values())
        median_ret = round(statistics.median(ret_values), 6)
        avg_ret = round(statistics.mean(ret_values), 6)
        vs_cluster = round(ticker_return - median_ret, 6)
        peers_pos = sum(1 for r in ret_values if r > 0)
        peers_above = sum(1 for r in ret_values if abs(r) >= _CLUSTER_CONFIRMED_THRESHOLD)

        all_results.append(PeerClusterResult(
            cluster_id=cid,
            cluster_label=label,
            peer_count=len(peers),
            peers_with_prices=len(peer_returns),
            cluster_median_return=median_ret,
            cluster_avg_return=avg_ret,
            ticker_vs_cluster=vs_cluster,
            peers_positive=peers_pos,
            peers_above_threshold=peers_above,
        ))

    if not all_results:
        return None, [], "no_cluster_mapping"

    clusters_with_data = [c for c in all_results if c.cluster_median_return is not None]

    if not clusters_with_data:
        return all_results[0], all_results, "insufficient_peer_prices"

    best = min(
        clusters_with_data,
        key=lambda c: abs(c.ticker_vs_cluster) if c.ticker_vs_cluster is not None else float("inf"),
    )

    same_direction = (
        ticker_return > 0
        and best.cluster_median_return is not None
        and best.cluster_median_return > 0
    ) or (
        ticker_return < 0
        and best.cluster_median_return is not None
        and best.cluster_median_return < 0
    )
    cluster_material = (
        best.cluster_median_return is not None
        and abs(best.cluster_median_return) >= _CLUSTER_CONFIRMED_THRESHOLD
    )

    if (same_direction
            and cluster_material
            and best.ticker_vs_cluster is not None
            and abs(best.ticker_vs_cluster) < _OUTPERFORMANCE_THRESHOLD):
        attribution = "cluster_confirmed"
    elif (best.ticker_vs_cluster is not None
          and abs(best.ticker_vs_cluster) >= _OUTPERFORMANCE_THRESHOLD):
        attribution = "ticker_outperformed_cluster"
    elif same_direction and cluster_material:
        attribution = "cluster_confirmed"
    else:
        attribution = "broad_sector_only"

    return best, all_results, attribution


def generate_peer_cluster_diagnostics(
    conn,
    enriched_rows: list[dict],
    cluster_config: dict[str, dict] | None = None,
) -> list[PeerClusterDiag]:
    """Generate peer cluster diagnostics for sector_cluster_move rows."""
    from health.sector_diagnostics import SECTOR_TO_ETF, compute_etf_window_return

    cluster_rows = [
        r for r in enriched_rows
        if r.get("enriched_root_cause") == "sector_cluster_move"
    ]
    if not cluster_rows:
        return []

    if cluster_config is None:
        cluster_config = load_cluster_config()
    ticker_to_clusters = build_ticker_to_clusters(cluster_config)

    sector_map: dict[str, str] = {}
    try:
        rows = conn.execute(
            "SELECT ticker, sector FROM companies WHERE is_active = true AND sector IS NOT NULL"
        ).fetchall()
        sector_map = {t: s for t, s in rows}
    except Exception:
        pass

    results: list[PeerClusterDiag] = []
    for row in cluster_rows:
        ticker = str(row.get("ticker", ""))
        event_date = str(row.get("event_date", ""))
        raw_return = row.get("return_value")
        try:
            ticker_return = float(raw_return) / 100.0 if raw_return is not None else None
        except (ValueError, TypeError):
            ticker_return = None

        raw_window = row.get("window_days")
        try:
            window_days = int(raw_window) if raw_window is not None else None
        except (ValueError, TypeError):
            window_days = None

        sector = sector_map.get(ticker)
        etf = SECTOR_TO_ETF.get(sector or "")
        etf_return = None
        if etf and window_days is not None:
            etf_return = compute_etf_window_return(conn, etf, event_date, window_days)

        ticker_vs_etf = None
        if ticker_return is not None and etf_return is not None:
            ticker_vs_etf = round(ticker_return - etf_return, 6)

        if ticker_return is not None and window_days is not None:
            best, all_clusters, attribution = compute_cluster_attribution(
                conn, ticker, ticker_return, event_date, window_days,
                cluster_config, ticker_to_clusters,
            )
        else:
            best, all_clusters, attribution = None, [], "no_cluster_mapping"

        results.append(PeerClusterDiag(
            ticker=ticker,
            event_date=event_date,
            window_days=window_days,
            sector=sector,
            etf_return=etf_return,
            ticker_return=ticker_return,
            ticker_vs_etf=ticker_vs_etf,
            best_cluster=best,
            attribution=attribution,
            all_clusters=all_clusters,
        ))

    return results
