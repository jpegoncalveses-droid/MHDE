"""
Step 0: Data backfill for ML prediction engine.

1. Backfill SPY, 11 sector ETFs via Yahoo Finance (full 1y history)
2. Backfill ^VIX via Yahoo Finance
3. Backfill DGS2 + DGS10 full history via FRED API
4. Verify row counts
"""

import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import duckdb
import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("step0_backfill")

DB_PATH = "data/mhde.duckdb"

# --- Part 1: Yahoo Finance price backfill ---

YF_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MHDE-Engine)",
    "Accept": "application/json",
}

TICKERS_TO_BACKFILL = [
    "SPY",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU", "XLB", "XLRE", "XLC", "XLY",
    "^VIX",
]


def fetch_yahoo_1y(ticker):
    url = f"{YF_BASE}/{ticker}?range=1y&interval=1d"
    for attempt in range(3):
        r = requests.get(url, headers=YF_HEADERS, timeout=20)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            wait = 30 * (attempt + 1)
            logger.warning(f"  429 for {ticker}, waiting {wait}s...")
            time.sleep(wait)
        else:
            logger.error(f"  HTTP {r.status_code} for {ticker}")
            return None
    return None


def parse_yahoo(data, ticker, run_id, now):
    rows = []
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return rows
    r = result[0]
    timestamps = r.get("timestamp") or []
    quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    adj_close_arr = ((r.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose")
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    for j, ts in enumerate(timestamps):
        close = closes[j] if j < len(closes) else None
        if close is None:
            continue
        open_ = opens[j] if j < len(opens) else None
        high = highs[j] if j < len(highs) else None
        low = lows[j] if j < len(lows) else None
        vol = volumes[j] if j < len(volumes) else None
        adj = adj_close_arr[j] if adj_close_arr and j < len(adj_close_arr) else close
        trade_date = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()

        # Store VIX with a clean ticker name
        db_ticker = "VIX" if ticker == "^VIX" else ticker
        rows.append([
            uuid.uuid4().hex[:16], db_ticker, trade_date,
            open_, high, low, close,
            int(vol) if vol is not None else None,
            adj if adj is not None else close,
            "yahoo", run_id, now,
        ])
    return rows


def backfill_yahoo_prices(con):
    run_id = uuid.uuid4().hex[:16]
    now = datetime.utcnow()
    total = 0

    for ticker in TICKERS_TO_BACKFILL:
        db_ticker = "VIX" if ticker == "^VIX" else ticker
        existing = con.execute(
            "SELECT COUNT(*) FROM prices_daily WHERE ticker = ?", [db_ticker]
        ).fetchone()[0]

        if existing >= 240:
            logger.info(f"  {db_ticker}: already has {existing} rows, skipping")
            continue

        logger.info(f"  Fetching {ticker}...")
        data = fetch_yahoo_1y(ticker)
        if not data:
            continue

        rows = parse_yahoo(data, ticker, run_id, now)
        if rows:
            con.executemany(
                """INSERT INTO prices_daily
                    (id, ticker, trade_date, open, high, low, close,
                     volume, adjusted_close, source, run_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (ticker, trade_date) DO NOTHING""",
                rows,
            )
            total += len(rows)
            logger.info(f"  {db_ticker}: inserted {len(rows)} rows")
        else:
            logger.warning(f"  {db_ticker}: no data parsed")

        time.sleep(0.5)

    logger.info(f"Yahoo backfill complete: {total} total rows inserted")
    return total


# --- Part 2: FRED backfill (DGS2 + DGS10, full history from 2025-05-01) ---

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
FRED_SERIES = {
    "DGS2": "2-Year Treasury Yield",
    "DGS10": "10-Year Treasury Yield",
}


def backfill_fred(con):
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        # Try loading from .env file
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../.env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("FRED_API_KEY="):
                        api_key = line.strip().split("=", 1)[1]
                        break
    if not api_key:
        logger.error("FRED_API_KEY not found in environment or .env")
        return 0

    run_id = uuid.uuid4().hex[:16]
    total = 0

    for series_id, series_name in FRED_SERIES.items():
        url = (
            f"{FRED_BASE}?series_id={series_id}&api_key={api_key}&file_type=json"
            f"&observation_start=2025-05-01&sort_order=asc&limit=1000"
        )
        logger.info(f"  Fetching FRED {series_id}...")
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            logger.error(f"  FRED {series_id}: HTTP {r.status_code}")
            continue

        obs = r.json().get("observations", [])
        inserted = 0
        for ob in obs:
            val = ob.get("value")
            if val == "." or val is None:
                continue
            date_str = ob.get("date", "")
            try:
                from datetime import date as date_cls
                as_of = date_cls.fromisoformat(date_str)
                con.execute(
                    """INSERT INTO macro_series
                        (id, series_id, series_name, value, as_of_date,
                         source, run_id, created_at)
                       VALUES (?, ?, ?, ?, ?, 'fred', ?, ?)
                       ON CONFLICT (series_id, as_of_date) DO NOTHING""",
                    [
                        uuid.uuid4().hex[:16], series_id, series_name,
                        float(val), as_of, run_id, datetime.utcnow(),
                    ],
                )
                inserted += 1
            except Exception as exc:
                logger.warning(f"  FRED {series_id} row error: {exc}")

        total += inserted
        logger.info(f"  {series_id}: inserted {inserted} observations")
        time.sleep(0.3)

    logger.info(f"FRED backfill complete: {total} total observations")
    return total


# --- Part 3: Verification ---

def verify(con):
    print("\n" + "=" * 70)
    print("VERIFICATION: Price data coverage")
    print("=" * 70)

    all_tickers = ["SPY", "VIX", "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU", "XLB", "XLRE", "XLC", "XLY"]
    print(f"\n{'Ticker':<8} | {'Rows':>5} | {'Min Date':>12} | {'Max Date':>12} | {'Status'}")
    print("-" * 60)

    for ticker in all_tickers:
        row = con.execute(
            "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM prices_daily WHERE ticker = ?",
            [ticker]
        ).fetchone()
        count, min_d, max_d = row
        status = "OK" if count >= 240 else "LOW" if count > 0 else "MISSING"
        print(f"{ticker:<8} | {count:>5} | {str(min_d):>12} | {str(max_d):>12} | {status}")

    print("\n" + "=" * 70)
    print("VERIFICATION: FRED macro data")
    print("=" * 70)
    print(f"\n{'Series':<8} | {'Rows':>5} | {'Min Date':>12} | {'Max Date':>12}")
    print("-" * 50)

    for series_id in ["DGS2", "DGS10"]:
        row = con.execute(
            "SELECT COUNT(*), MIN(as_of_date), MAX(as_of_date) FROM macro_series WHERE series_id = ?",
            [series_id]
        ).fetchone()
        count, min_d, max_d = row
        print(f"{series_id:<8} | {count:>5} | {str(min_d):>12} | {str(max_d):>12}")


# --- Main ---

if __name__ == "__main__":
    con = duckdb.connect(DB_PATH)
    try:
        print("=" * 70)
        print("STEP 0: DATA BACKFILL FOR ML PREDICTION ENGINE")
        print("=" * 70)

        print("\n[1/3] Yahoo Finance: SPY + sector ETFs + VIX")
        backfill_yahoo_prices(con)

        print("\n[2/3] FRED: DGS2 + DGS10 (yield curve)")
        backfill_fred(con)

        print("\n[3/3] Verifying...")
        verify(con)

    finally:
        con.close()
