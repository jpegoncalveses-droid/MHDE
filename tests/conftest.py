"""Project-wide pytest fixtures.

Layered conftest:
  - The fixtures here are available to every test under `tests/`.
  - Per-engine subdirs (tests/equity/, tests/crypto/, tests/fx/, ...) may
    add their own conftest.py for engine-specific fixtures, but most
    cross-cutting plumbing lives here.

Extension policy: keep fixtures small and orthogonal. A test should be
able to opt into one of {temp_db, synthetic_prices_*, synthetic_filings,
synthetic_fundamentals, mock_telegram} without paying for the others.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from typing import Any, Callable

import pytest


# ──────────────────────────────────────────────────────────────────────
# Existing fixtures — preserve as-is
# ──────────────────────────────────────────────────────────────────────

from adapters.base import Scores, ValidationResult


@pytest.fixture
def minimal_settings(tmp_path):
    return {
        "outputs": {"samples_dir": str(tmp_path / "samples"), "dir": str(tmp_path / "outputs")},
        "http": {"timeout": 10, "retries": 1, "retry_delay": 0},
        "sec_edgar": {"base_url": "https://data.sec.gov", "user_agent": "test", "rate_limit_delay": 0},
        "polygon": {"base_url": "https://api.polygon.io", "api_key": "fake_key", "rate_limit_delay": 0},
        "alpha_vantage": {"base_url": "https://www.alphavantage.co", "api_key": "fake_key", "rate_limit_delay": 0},
        "company_ir": {"request_delay": 0, "request_timeout": 5},
        "nasdaq_earnings": {"base_url": "https://api.nasdaq.com", "rate_limit_delay": 0},
        "fred": {"base_url": "https://api.stlouisfed.org/fred", "api_key": "fake_key", "rate_limit_delay": 0},
        "finra": {"base_url": "https://cdn.finra.org/equity/otcmarket/biweekly", "rate_limit_delay": 0},
        "cftc": {
            "tff_url": "https://publicreporting.cftc.gov/resource/gpe5-46if.json",
            "disag_url": "https://publicreporting.cftc.gov/resource/kh3c-gbw2.json",
            "rate_limit_delay": 0,
            "history_weeks": 4,
        },
    }


@pytest.fixture
def sample_tickers():
    return [
        {"ticker": "AAPL", "name": "Apple Inc", "cik": "0000320193",
         "ir_press_url": "https://investor.apple.com/press-releases/default.aspx",
         "ir_events_url": "https://investor.apple.com/events/default.aspx", "type": "stock"},
        {"ticker": "NVDA", "name": "NVIDIA Corporation", "cik": "0001045810",
         "ir_press_url": "https://nvidianews.nvidia.com/releases",
         "ir_events_url": "https://investor.nvidia.com/events-presentations/events/default.aspx", "type": "stock"},
        {"ticker": "IWM", "name": "iShares Russell 2000 ETF", "cik": None,
         "ir_press_url": None, "ir_events_url": None, "type": "etf"},
    ]


def make_validation_result(source="test", use_case="uc", final_status="Core", **kwargs):
    defaults = dict(
        source=source, use_case=use_case, tickers_tested=["AAPL"],
        access_result="ok", access_error=None, required_fields_present=True,
        missing_fields=[], historical_depth="5y", freshness="1d",
        parsing_difficulty="easy", rate_limit_notes="none",
        fallback_suggestion="none", final_status=final_status,
        notes="", scores=Scores(5, 5, 5, 5, 5, 5, 5), raw_sample_path=None,
    )
    defaults.update(kwargs)
    return ValidationResult(**defaults)


# ──────────────────────────────────────────────────────────────────────
# Session 2 additions: in-memory DuckDB + synthetic data + mocks
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db():
    """In-memory DuckDB pre-loaded with every active schema.

    Loads in order:
      1. storage/schema.sql + storage/migrations.py  (equity / shared)
      2. ml/schema.py        (equity ML)
      3. crypto/schema.py    (crypto ML)
      4. fx/schema.py        (FX ML)

    All ~52 production tables exist after this fixture yields.
    """
    import duckdb

    conn = duckdb.connect(":memory:")
    from storage.migrations import run_migrations
    from ml.schema import create_all_tables as _create_ml
    from crypto.schema import create_all_tables as _create_crypto
    from fx.schema import create_all_tables as _create_fx

    run_migrations(conn)
    _create_ml(conn)
    _create_crypto(conn)
    _create_fx(conn)

    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def synthetic_prices_equity() -> Callable[..., list[dict]]:
    """Factory for prices_daily rows.

    Generates a deterministic random walk with weekend-skipping. Output
    shape matches `storage/schema.sql:prices_daily`. Caller is responsible
    for the INSERT.

    Defaults: 60 trading days ending today, $100 start, 2% daily vol,
    seed=42 (deterministic).
    """
    def _make(
        ticker: str,
        num_days: int = 60,
        start_price: float = 100.0,
        start_date: date | None = None,
        volatility: float = 0.02,
        seed: int = 42,
    ) -> list[dict]:
        rng = random.Random(seed)
        start_date = start_date or (date.today() - timedelta(days=num_days * 2))
        rows: list[dict] = []
        price = start_price
        cur = start_date
        produced = 0
        while produced < num_days:
            if cur.weekday() < 5:  # Mon-Fri
                ret = rng.gauss(0, volatility)
                o = price
                c = price * (1 + ret)
                h = max(o, c) * (1 + abs(rng.gauss(0, volatility / 2)))
                lo = min(o, c) * (1 - abs(rng.gauss(0, volatility / 2)))
                v = int(rng.uniform(1e6, 1e8))
                rows.append({
                    "id": f"{ticker}-{cur}",
                    "ticker": ticker, "trade_date": cur,
                    "open": o, "high": h, "low": lo, "close": c,
                    "volume": v, "adjusted_close": c,
                    "source": "synth", "run_id": "test-run",
                })
                price = c
                produced += 1
            cur += timedelta(days=1)
        return rows

    return _make


@pytest.fixture
def synthetic_prices_crypto() -> Callable[..., list[dict]]:
    """Factory for crypto_prices_daily rows.

    Crypto runs 7 days/week. Default 60 calendar days, $50k start
    (BTC-ish), 4% daily vol — wider than equity to reflect crypto
    realized vol.
    """
    def _make(
        symbol: str = "BTCUSDT",
        num_days: int = 60,
        start_price: float = 50_000.0,
        start_date: date | None = None,
        volatility: float = 0.04,
        seed: int = 42,
    ) -> list[dict]:
        rng = random.Random(seed)
        start_date = start_date or (date.today() - timedelta(days=num_days))
        rows: list[dict] = []
        price = start_price
        for i in range(num_days):
            d = start_date + timedelta(days=i)
            ret = rng.gauss(0, volatility)
            o = price
            c = price * (1 + ret)
            h = max(o, c) * (1 + abs(rng.gauss(0, volatility / 2)))
            lo = min(o, c) * (1 - abs(rng.gauss(0, volatility / 2)))
            v = rng.uniform(1e6, 1e9)
            rows.append({
                "symbol": symbol, "trade_date": d,
                "open": o, "high": h, "low": lo, "close": c,
                "volume": v, "trades": int(v / 100),
                "taker_buy_volume": v * rng.uniform(0.4, 0.6),
                "source": "synth",
            })
            price = c
        return rows

    return _make


@pytest.fixture
def synthetic_prices_fx() -> Callable[..., list[dict]]:
    """Factory for fx_prices_hourly rows (GBP/EUR shape).

    Skips the FX weekend window (Sat 21:00 UTC → Sun 21:00 UTC). Default
    168 hours (7 days), GBP/EUR ≈ 1.18 start, 8 bps hourly vol.
    """
    def _make(
        num_hours: int = 168,
        start_price: float = 1.18,
        start_datetime: datetime | None = None,
        volatility: float = 0.0008,
        seed: int = 42,
    ) -> list[dict]:
        rng = random.Random(seed)
        start_datetime = (start_datetime or
                          datetime.utcnow().replace(minute=0, second=0, microsecond=0)
                          - timedelta(hours=num_hours))
        rows: list[dict] = []
        price = start_price
        produced = 0
        cur = start_datetime
        while produced < num_hours:
            wd, hr = cur.weekday(), cur.hour
            in_weekend = (wd == 5 and hr >= 21) or (wd == 6 and hr < 21)
            if not in_weekend:
                ret = rng.gauss(0, volatility)
                o = price
                c = price * (1 + ret)
                h = max(o, c) * (1 + abs(rng.gauss(0, volatility / 2)))
                lo = min(o, c) * (1 - abs(rng.gauss(0, volatility / 2)))
                rows.append({
                    "datetime_utc": cur,
                    "date": cur.date(),
                    "weekday": cur.strftime("%A"),
                    "hour_utc": cur.hour,
                    "gbpeur_open": o, "gbpeur_high": h,
                    "gbpeur_low": lo, "gbpeur_close": c,
                    "tick_count": rng.randint(50, 500),
                    "data_quality": "good",
                })
                price = c
                produced += 1
            cur += timedelta(hours=1)
        return rows

    return _make


@pytest.fixture
def synthetic_filings() -> Callable[..., list[dict]]:
    """Factory for filings rows. Defaults: 5 filings cycling 8-K/10-Q/10-K
    over the last 90 days, weekly cadence."""
    def _make(
        ticker: str,
        count: int = 5,
        form_types: list[str] | None = None,
        start_date: date | None = None,
    ) -> list[dict]:
        form_types = form_types or ["8-K", "10-Q", "10-K"]
        start_date = start_date or (date.today() - timedelta(days=90))
        rows: list[dict] = []
        for i in range(count):
            ft = form_types[i % len(form_types)]
            d = start_date + timedelta(days=i * 7)
            rows.append({
                "id": f"filing-{ticker}-{i}",
                "ticker": ticker,
                "cik": "0000000000",
                "form_type": ft,
                "accession_number": f"0000000000-00-{i:06d}",
                "filing_date": d,
                "report_date": d - timedelta(days=2),
                "description": f"Synthetic {ft} for {ticker}",
                "doc_url": f"https://example.com/{ticker}/{i}",
                "run_id": "test-run",
            })
        return rows

    return _make


@pytest.fixture
def synthetic_fundamentals() -> Callable[..., list[dict]]:
    """Factory for fundamentals_features rows. Defaults: 4 quarterly
    points with 5% QoQ revenue growth, 15% net margin."""
    def _make(
        ticker: str,
        count: int = 4,
        start_date: date | None = None,
        start_revenue: float = 1e9,
    ) -> list[dict]:
        start_date = start_date or (date.today() - timedelta(days=120))
        rows: list[dict] = []
        rev = start_revenue
        for i in range(count):
            d = start_date + timedelta(days=i * 90)
            rev *= 1.05
            ni = rev * 0.15
            rows.append({
                "id": f"fund-{ticker}-{i}",
                "ticker": ticker,
                "as_of_date": d,
                "revenue": rev,
                "net_income": ni,
                "shares_outstanding": 1e9,
                "revenue_growth_yoy": 0.20,
                "net_margin": 0.15,
                "dilution_rate": 0.01,
                "pe_proxy": 20.0,
                "ps_proxy": 5.0,
                "data_freshness_days": 30,
                "run_id": "test-run",
            })
        return rows

    return _make


@pytest.fixture
def mock_telegram(monkeypatch):
    """Block real Telegram sends; capture intended messages.

    Returns a list of `{"url", "json", "kwargs"}` dicts that any code
    using `requests.post` to https://api.telegram.org/* would have
    sent. Production callers (notifications/telegram.py and
    fx/bot/telegram_bot.py) all bottom out in `requests.post` so this
    one shim covers both.

    Tests assert against the captured list:
        assert len(mock_telegram) == 1
        assert "BUY_GBP" in mock_telegram[0]["json"]["text"]
    """
    captured: list[dict[str, Any]] = []

    class _FakeResponse:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {"ok": True, "result": {}}

    def _fake_post(url, **kwargs):
        captured.append({
            "url": url,
            "json": kwargs.get("json"),
            "data": kwargs.get("data"),
            "kwargs": {k: v for k, v in kwargs.items() if k not in {"json", "data"}},
        })
        return _FakeResponse()

    import requests
    monkeypatch.setattr(requests, "post", _fake_post)

    # If the codebase exposes a higher-level helper, patch that too so
    # tests don't have to know about the bottom-of-the-stack requests
    # call. notifications.telegram is the conventional surface.
    try:
        import notifications.telegram as _ntel
        for fname in ("send_message", "send_alert", "notify"):
            if hasattr(_ntel, fname):
                monkeypatch.setattr(_ntel, fname,
                                    lambda *a, **kw: captured.append({"helper": fname, "args": a, "kwargs": kw}) or True)
    except ImportError:
        pass

    return captured
