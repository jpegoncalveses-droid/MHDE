"""Sector cluster diagnostics — classify why a sector_cluster_move row was missed."""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

SECTOR_TO_ETF: dict[str, str] = {
    "Information Technology": "XLK",
    "Financials": "XLF",
    "Energy": "XLE",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Consumer Staples": "XLP",
    "Utilities": "XLU",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
}

_ETF_CONFIRMED_THRESHOLD = 0.01
_OUTPERFORMANCE_THRESHOLD = 0.03


@dataclass
class SectorClusterDiag:
    ticker: str
    event_date: str
    sector: Optional[str]
    etf_ticker: Optional[str]
    etf_price_count: int
    subcause: str
    window_days: Optional[int] = None
    ticker_return: Optional[float] = None
    etf_return: Optional[float] = None
    relative_return: Optional[float] = None
    peer_cluster_count: Optional[int] = None
    suggested_fix: str = ""


def get_etf_coverage(conn) -> dict[str, int]:
    """Return {etf_ticker: row_count} for all sector ETF tickers in prices_daily."""
    etf_set = set(SECTOR_TO_ETF.values())
    try:
        rows = conn.execute(
            "SELECT ticker, COUNT(*) FROM prices_daily GROUP BY ticker"
        ).fetchall()
        return {ticker: count for ticker, count in rows if ticker in etf_set}
    except Exception as exc:
        logger.warning("sector_diagnostics.get_etf_coverage: %s", exc)
        return {}


def compute_etf_window_return(
    conn,
    etf_ticker: str,
    event_date: str,
    window_days: int,
) -> Optional[float]:
    """Compute ETF return over the same window as the ticker event.

    Sector ETF rows (source='polygon_sector_etf') store daily returns in the
    close column, not price levels.  For those rows we compound the daily
    returns that fall within the window.  For normal price rows we compute
    (end_price / start_price) - 1.
    """
    try:
        ed = datetime.date.fromisoformat(event_date)
    except (ValueError, TypeError):
        return None

    start_date = ed - datetime.timedelta(days=max(window_days * 2, 7))

    try:
        rows = conn.execute(
            """
            SELECT trade_date, close, source FROM prices_daily
            WHERE ticker = ? AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [etf_ticker, str(start_date), event_date],
        ).fetchall()
    except Exception:
        return None

    if not rows:
        return None

    is_return_data = any(
        r[2] == "polygon_sector_etf" for r in rows
    )

    if is_return_data:
        return _compound_daily_returns(rows, ed, window_days)

    return _price_based_return(rows, event_date, ed, window_days)


def _compound_daily_returns(
    rows: list,
    event_date: datetime.date,
    window_days: int,
) -> Optional[float]:
    """Compound daily return values within the event window."""
    if window_days <= 1:
        for d, close, _src in reversed(rows):
            if isinstance(d, datetime.date) and d <= event_date:
                return round(float(close), 6)
        return None

    window_start = event_date - datetime.timedelta(days=window_days)
    product = 1.0
    count = 0
    for d, close, _src in rows:
        if isinstance(d, datetime.date) and window_start < d <= event_date:
            product *= (1.0 + float(close))
            count += 1
    if count == 0:
        return None
    return round(product - 1.0, 6)


def _price_based_return(
    rows: list,
    event_date_str: str,
    event_date: datetime.date,
    window_days: int,
) -> Optional[float]:
    """Compute return from price levels."""
    if len(rows) < 2:
        return None

    end_price = None
    for d, close, _src in reversed(rows):
        if str(d) <= event_date_str:
            end_price = close
            break

    if window_days <= 1:
        start_price = None
        for d, close, _src in rows:
            if str(d) < event_date_str:
                start_price = close
        if start_price is None or end_price is None:
            return None
    else:
        target_start = event_date - datetime.timedelta(days=window_days)
        start_price = None
        for d, close, _src in rows:
            if str(d) <= str(target_start):
                start_price = close
        if start_price is None:
            start_price = rows[0][1]

    if start_price is None or end_price is None or start_price == 0:
        return None

    return round((end_price - start_price) / start_price, 6)


_SUGGESTED_FIXES: dict[str, str] = {
    "missing_sector_mapping":
        "Add sector to companies table for this ticker.",
    "missing_sector_etf_prices":
        "Run: python main.py data ingest-sector-etfs --lookback-days 30",
    "peer_cluster_only_no_etf_data":
        "ETF prices exist but no data for this event window. Run: python main.py data ingest-sector-etfs --lookback-days 30",
    "sector_etf_confirmed":
        "Sector move confirmed by ETF. Add sector-momentum feature to scoring model.",
    "ticker_outperformed_sector":
        "Ticker outperformed sector ETF — likely company-specific catalyst on top of sector move.",
    "sector_signal_underweighted":
        "Sector/ETF signal existed but candidate was not surfaced. Increase sector feature weight or add sector-momentum gate.",
}


def classify_sector_cluster_row(
    ticker: str,
    sector: Optional[str],
    etf_coverage: dict[str, int],
    etf_return: Optional[float] = None,
    ticker_return: Optional[float] = None,
) -> str:
    """Return the most specific subcause for a sector_cluster_move row."""
    if not sector:
        return "missing_sector_mapping"
    etf = SECTOR_TO_ETF.get(sector)
    if etf is None:
        return "missing_sector_mapping"
    if etf_coverage.get(etf, 0) == 0:
        return "missing_sector_etf_prices"

    if etf_return is None:
        return "peer_cluster_only_no_etf_data"

    if ticker_return is None:
        return "peer_cluster_only_no_etf_data"

    relative = ticker_return - etf_return

    same_direction = (ticker_return > 0 and etf_return > 0) or (ticker_return < 0 and etf_return < 0)
    etf_material = abs(etf_return) >= _ETF_CONFIRMED_THRESHOLD

    if same_direction and etf_material and abs(relative) < _OUTPERFORMANCE_THRESHOLD:
        return "sector_etf_confirmed"

    if abs(relative) >= _OUTPERFORMANCE_THRESHOLD:
        return "ticker_outperformed_sector"

    if etf_material and same_direction:
        return "sector_signal_underweighted"

    return "peer_cluster_only_no_etf_data"


def _count_peer_clusters(
    enriched_rows: list[dict],
    sector_map: dict[str, str],
) -> dict[tuple[str, str], int]:
    """Return {(sector, event_date): count_of_tickers} for sector_cluster_move rows."""
    from collections import Counter
    keys: list[tuple[str, str]] = []
    for row in enriched_rows:
        if row.get("enriched_root_cause") != "sector_cluster_move":
            continue
        ticker = str(row.get("ticker", ""))
        sector = sector_map.get(ticker)
        if sector:
            keys.append((sector, str(row.get("event_date", ""))))
    return dict(Counter(keys))


def generate_sector_diagnostics(
    conn,
    enriched_rows: list[dict],
) -> list[SectorClusterDiag]:
    """Return SectorClusterDiag for every sector_cluster_move row in enriched_rows."""
    cluster_rows = [
        r for r in enriched_rows
        if r.get("enriched_root_cause") == "sector_cluster_move"
    ]
    if not cluster_rows:
        return []

    etf_coverage = get_etf_coverage(conn)

    sector_map: dict[str, str] = {}
    try:
        rows = conn.execute(
            "SELECT ticker, sector FROM companies WHERE is_active = true AND sector IS NOT NULL"
        ).fetchall()
        sector_map = {ticker: sector for ticker, sector in rows}
    except Exception as exc:
        logger.warning("sector_diagnostics.generate: could not load sector_map: %s", exc)

    peer_counts = _count_peer_clusters(enriched_rows, sector_map)

    result: list[SectorClusterDiag] = []
    for row in cluster_rows:
        ticker = str(row.get("ticker", ""))
        event_date = str(row.get("event_date", ""))
        sector = sector_map.get(ticker)
        etf = SECTOR_TO_ETF.get(sector or "")
        count = etf_coverage.get(etf or "", 0) if etf else 0

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

        etf_return = None
        if etf and count > 0 and window_days is not None:
            etf_return = compute_etf_window_return(conn, etf, event_date, window_days)

        relative_return = None
        if ticker_return is not None and etf_return is not None:
            relative_return = round(ticker_return - etf_return, 6)

        subcause = classify_sector_cluster_row(
            ticker, sector, etf_coverage,
            etf_return=etf_return,
            ticker_return=ticker_return,
        )

        peer_count = peer_counts.get((sector or "", event_date), 0)

        result.append(SectorClusterDiag(
            ticker=ticker,
            event_date=event_date,
            sector=sector,
            etf_ticker=etf,
            etf_price_count=count,
            subcause=subcause,
            window_days=window_days,
            ticker_return=ticker_return,
            etf_return=etf_return,
            relative_return=relative_return,
            peer_cluster_count=peer_count if peer_count > 0 else None,
            suggested_fix=_SUGGESTED_FIXES.get(subcause, ""),
        ))
    return result
