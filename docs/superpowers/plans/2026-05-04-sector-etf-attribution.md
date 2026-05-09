# Sector ETF Attribution — Upgrade to ETF-Confirmed Subcauses

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade sector_cluster_move attribution to use actual ETF return data, producing six deterministic subcauses instead of the current peer-only classification.

**Architecture:** `health/sector_diagnostics.py` gains ETF return lookup, richer classification logic, and a new `SUBCAUSE_FIXES` dict. `missed/root_cause_enrichment.py` pre-fetches ETF returns and peer counts and passes them through `_assign_root_cause()`. CLI and `/learning` page surface the new columns (ETF return, ticker return, relative return, peer count, suggested fix). All changes are shadow/diagnostic only — no production scores touched.

**Tech Stack:** Python 3.11, DuckDB, Flask, standard library only. No new dependencies.

**Stop conditions:** test failure that cannot be fixed safely | scoring logic modified | feature flag introduced | LLM called | secret exposed

---

## Context

ETF prices now exist in `prices_daily` for sector ETF tickers (XLK, XLF, XLE, XLV, XLI, XLP, XLU, XLB, XLRE, XLC, XLY). The `close` column for these rows stores the intraday return `(close - open) / open`. All 15 `sector_cluster_move` enriched rows currently show `peer_cluster_only_no_etf_data` even though ETF data exists — because `classify_sector_cluster_row()` doesn't yet look up the ETF return for the specific event date.

**Thresholds:**
- `ETF_MATERIAL_THRESHOLD = 0.005` — a 0.5% or greater ETF intraday return is a material sector move
- `TICKER_OUTPERFORM_RATIO = 3.0` — ticker return ≥ 3× ETF return → idiosyncratic outperformance

**Subcause decision tree (applied in order):**
1. No sector or sector not in SECTOR_TO_ETF → `missing_sector_mapping`
2. `etf_coverage[etf] == 0` → `missing_sector_etf_prices`
3. No ETF row for event_date, or ETF return ≤ 0 → `peer_cluster_only_no_etf_data`
4. `0 < etf_return < ETF_MATERIAL_THRESHOLD` → `sector_signal_underweighted`
5. `etf_return >= ETF_MATERIAL_THRESHOLD` AND `ticker_return / etf_return >= TICKER_OUTPERFORM_RATIO` AND `peer_count >= 2` → `ticker_outperformed_sector`
6. `etf_return >= ETF_MATERIAL_THRESHOLD` → `sector_etf_confirmed`

---

## Codebase Orientation

| Area | Path |
|------|------|
| Diagnostics engine | `health/sector_diagnostics.py` — major update |
| Diagnostics tests | `tests/test_sector_diagnostics.py` — update + new tests |
| Root-cause enrichment | `missed/root_cause_enrichment.py` — add ETF returns, peer counts |
| Enrichment subcause tests | `tests/test_enrichment_sector_subcause.py` — update |
| CLI command | `main.py` — `data sector-diagnostics` — add ETF/relative/peer columns |
| Learning page | `review/server.py` — `_learning_page()` sector diag table, add columns |
| ETF return storage | `prices_daily.close` for ETF tickers = intraday return `(c-o)/o` |
| `_coerce_date()` | `missed/root_cause_enrichment.py` — already exists, use for date handling |
| `sector_map` | Pre-fetched in `enrich_rows()` as `dict[str, str]` |
| `_INCOMPLETE_DIAG_EMPTY` | Already has `sector_cluster_subcause` field — no schema changes needed |
| `enrich_rows()` signature | `enrich_rows(rows, conn)` — no signature change |
| `_assign_root_cause()` | Currently takes `sector_map`, `etf_coverage` — will add `etf_returns`, `peer_counts` |

---

## Task 1: Update `health/sector_diagnostics.py`

**Files:**
- Modify: `health/sector_diagnostics.py` — full rewrite with new logic
- Modify: `tests/test_sector_diagnostics.py` — add 6 new tests

- [ ] **Step 1: Write the new tests first (append to existing file)**

The existing 14 tests must continue to pass. The calls to `classify_sector_cluster_row` without `event_date`/`etf_returns` must still return `peer_cluster_only_no_etf_data` (the default when no ETF return data is available).

Add to `tests/test_sector_diagnostics.py`:

```python
# ── ETF-based classification tests ────────────────────────────────────────

def test_classify_sector_etf_confirmed():
    from health.sector_diagnostics import classify_sector_cluster_row
    etf_returns = {("XLK", "2026-05-01"): 0.008}  # 0.8% > threshold
    result = classify_sector_cluster_row(
        "AAPL", "Information Technology", {"XLK": 5},
        event_date="2026-05-01", etf_returns=etf_returns, ticker_return=0.03, peer_count=2,
    )
    assert result == "sector_etf_confirmed"


def test_classify_ticker_outperformed_sector():
    from health.sector_diagnostics import classify_sector_cluster_row
    etf_returns = {("XLK", "2026-05-01"): 0.008}  # 0.8%
    result = classify_sector_cluster_row(
        "AAPL", "Information Technology", {"XLK": 5},
        event_date="2026-05-01", etf_returns=etf_returns,
        ticker_return=0.10,   # 10% / 0.8% = 12.5x >= 3x
        peer_count=3,
    )
    assert result == "ticker_outperformed_sector"


def test_classify_sector_signal_underweighted():
    from health.sector_diagnostics import classify_sector_cluster_row
    etf_returns = {("XLK", "2026-05-01"): 0.003}  # 0.3% < 0.5% threshold
    result = classify_sector_cluster_row(
        "AAPL", "Information Technology", {"XLK": 5},
        event_date="2026-05-01", etf_returns=etf_returns, ticker_return=0.05,
    )
    assert result == "sector_signal_underweighted"


def test_classify_peer_cluster_only_when_etf_return_missing_for_date():
    from health.sector_diagnostics import classify_sector_cluster_row
    # ETF exists in DB but no row for this specific date
    etf_returns = {("XLK", "2026-04-28"): 0.01}  # different date
    result = classify_sector_cluster_row(
        "AAPL", "Information Technology", {"XLK": 1},
        event_date="2026-05-01", etf_returns=etf_returns,
    )
    assert result == "peer_cluster_only_no_etf_data"


def test_classify_peer_cluster_only_when_etf_return_negative():
    from health.sector_diagnostics import classify_sector_cluster_row
    etf_returns = {("XLK", "2026-05-01"): -0.005}  # negative
    result = classify_sector_cluster_row(
        "AAPL", "Information Technology", {"XLK": 1},
        event_date="2026-05-01", etf_returns=etf_returns,
    )
    assert result == "peer_cluster_only_no_etf_data"


def test_ticker_not_outperformed_when_peer_count_below_2():
    from health.sector_diagnostics import classify_sector_cluster_row
    etf_returns = {("XLK", "2026-05-01"): 0.008}
    # 10/0.008 = 12.5x >= 3x BUT peer_count < 2
    result = classify_sector_cluster_row(
        "AAPL", "Information Technology", {"XLK": 5},
        event_date="2026-05-01", etf_returns=etf_returns,
        ticker_return=0.10, peer_count=1,
    )
    assert result == "sector_etf_confirmed"


def test_get_etf_returns_returns_intraday_return():
    import uuid
    from health.sector_diagnostics import get_etf_returns
    conn = _make_conn([("XLK", "2026-05-01")])
    conn.execute(
        "UPDATE prices_daily SET close = 0.012 WHERE ticker = 'XLK'"
    )
    returns = get_etf_returns(conn)
    assert ("XLK", "2026-05-01") in returns
    assert abs(returns[("XLK", "2026-05-01")] - 0.012) < 1e-9


def test_get_etf_returns_empty_when_no_etf_rows():
    from health.sector_diagnostics import get_etf_returns
    conn = _make_conn()
    assert get_etf_returns(conn) == {}


def test_generate_sector_diagnostics_produces_etf_confirmed():
    import uuid
    from health.sector_diagnostics import generate_sector_diagnostics
    conn = _make_conn([("XLK", "2026-05-01")])
    # Set ETF return to 1% (material)
    conn.execute("UPDATE prices_daily SET close = 0.010 WHERE ticker = 'XLK'")
    conn.execute("INSERT INTO companies VALUES ('AAPL', 'Information Technology', true)")
    rows = [{"ticker": "AAPL", "event_date": "2026-05-01",
             "enriched_root_cause": "sector_cluster_move", "return_value": "0.03"}]
    diags = generate_sector_diagnostics(conn, rows)
    assert len(diags) == 1
    d = diags[0]
    assert d.etf_return is not None
    assert abs(d.etf_return - 0.010) < 1e-9
    assert d.subcause == "sector_etf_confirmed"
    assert d.suggested_fix != ""


def test_generate_sector_diagnostics_includes_relative_return():
    import uuid
    from health.sector_diagnostics import generate_sector_diagnostics
    conn = _make_conn([("XLK", "2026-05-01")])
    conn.execute("UPDATE prices_daily SET close = 0.008 WHERE ticker = 'XLK'")
    conn.execute("INSERT INTO companies VALUES ('AAPL', 'Information Technology', true)")
    rows = [{"ticker": "AAPL", "event_date": "2026-05-01",
             "enriched_root_cause": "sector_cluster_move", "return_value": "0.05"}]
    diags = generate_sector_diagnostics(conn, rows)
    d = diags[0]
    assert d.ticker_return is not None
    assert abs(d.ticker_return - 0.05) < 1e-9
    assert d.relative_return is not None
    assert abs(d.relative_return - (0.05 - 0.008)) < 1e-6
```

- [ ] **Step 2: Run to confirm new tests fail**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_sector_diagnostics.py -v 2>&1 | tail -15
```
Expected: existing 14 pass, new tests fail with AttributeError (get_etf_returns not found) or similar.

- [ ] **Step 3: Rewrite `health/sector_diagnostics.py`**

Write the complete new file:

```python
"""Sector cluster diagnostics — classify why a sector_cluster_move row was missed."""
from __future__ import annotations

import logging
from dataclasses import dataclass
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

# Classification thresholds
ETF_MATERIAL_THRESHOLD = 0.005   # 0.5% — sector had a meaningful positive move
TICKER_OUTPERFORM_RATIO = 3.0    # ticker return ≥ 3× ETF return → idiosyncratic

SUBCAUSE_FIXES: dict[str, str] = {
    "missing_sector_mapping":
        "Add sector to ticker via: python main.py data enrich-ticker-details",
    "missing_sector_etf_prices":
        "Run: python main.py data ingest-sector-etfs --date <event_date> --delay 13",
    "peer_cluster_only_no_etf_data":
        "No ETF row for event date (or ETF return was non-positive) — ingest-sector-etfs for this date",
    "sector_etf_confirmed":
        "Wire sector ETF return into momentum scoring — ingestion/ingest_sector_etfs.py is ready",
    "ticker_outperformed_sector":
        "Add idiosyncratic momentum signal — ticker materially led sector peers on this move",
    "sector_signal_underweighted":
        "Sector signal existed pre-move but scored below threshold; consider sector momentum weight",
}


@dataclass
class SectorClusterDiag:
    ticker: str
    event_date: str
    sector: Optional[str]
    etf_ticker: Optional[str]
    etf_price_count: int
    etf_return: Optional[float]
    ticker_return: Optional[float]
    relative_return: Optional[float]
    peer_count: int
    subcause: str
    suggested_fix: str


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


def get_etf_returns(conn) -> dict[tuple[str, str], float]:
    """Return {(etf_ticker, trade_date_str): intraday_return} for all sector ETF rows.

    The 'close' column for sector ETF rows stores (close - open) / open, the intraday return.
    """
    etf_set = set(SECTOR_TO_ETF.values())
    try:
        rows = conn.execute(
            "SELECT ticker, CAST(trade_date AS VARCHAR), close FROM prices_daily"
        ).fetchall()
        return {
            (ticker, str(trade_date)): close
            for ticker, trade_date, close in rows
            if ticker in etf_set and close is not None
        }
    except Exception as exc:
        logger.warning("sector_diagnostics.get_etf_returns: %s", exc)
        return {}


def classify_sector_cluster_row(
    ticker: str,
    sector: Optional[str],
    etf_coverage: dict[str, int],
    event_date: Optional[str] = None,
    etf_returns: Optional[dict[tuple[str, str], float]] = None,
    ticker_return: Optional[float] = None,
    peer_count: int = 0,
) -> str:
    """Return the most specific subcause for a sector_cluster_move row.

    Decision tree (most to least specific):
      missing_sector_mapping      — no sector or sector not in SECTOR_TO_ETF
      missing_sector_etf_prices   — ETF mapped but 0 rows in prices_daily
      peer_cluster_only_no_etf_data — ETF exists but no/non-positive return for event_date
      sector_signal_underweighted — weak positive ETF signal (< 0.5%)
      ticker_outperformed_sector  — ETF material AND ticker ≥ 3× ETF AND peer_count ≥ 2
      sector_etf_confirmed        — ETF had material (≥ 0.5%) positive move
    """
    if not sector:
        return "missing_sector_mapping"
    etf = SECTOR_TO_ETF.get(sector)
    if etf is None:
        return "missing_sector_mapping"
    if etf_coverage.get(etf, 0) == 0:
        return "missing_sector_etf_prices"

    etf_return: Optional[float] = None
    if etf_returns and event_date:
        etf_return = etf_returns.get((etf, event_date))

    if etf_return is None or etf_return <= 0:
        return "peer_cluster_only_no_etf_data"

    if etf_return < ETF_MATERIAL_THRESHOLD:
        return "sector_signal_underweighted"

    # Material positive sector move
    if (ticker_return is not None
            and etf_return > 0
            and ticker_return / etf_return >= TICKER_OUTPERFORM_RATIO
            and peer_count >= 2):
        return "ticker_outperformed_sector"

    return "sector_etf_confirmed"


def _peer_counts_from_rows(cluster_rows: list[dict]) -> dict[tuple[str, str], int]:
    """Return {(ticker, event_date): peer_count} counting same-date cluster rows."""
    counts: dict[tuple[str, str], int] = {}
    for row in cluster_rows:
        ticker = str(row.get("ticker", ""))
        event_date = str(row.get("event_date", ""))
        peers = sum(
            1 for other in cluster_rows
            if str(other.get("ticker", "")) != ticker
            and str(other.get("event_date", "")) == event_date
        )
        counts[(ticker, event_date)] = peers
    return counts


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
    etf_returns = get_etf_returns(conn)

    sector_map: dict[str, str] = {}
    try:
        rows = conn.execute(
            "SELECT ticker, sector FROM companies WHERE is_active = true AND sector IS NOT NULL"
        ).fetchall()
        sector_map = {ticker: sector for ticker, sector in rows}
    except Exception as exc:
        logger.warning("sector_diagnostics.generate: could not load sector_map: %s", exc)

    peer_counts = _peer_counts_from_rows(cluster_rows)

    result: list[SectorClusterDiag] = []
    for row in cluster_rows:
        ticker = str(row.get("ticker", ""))
        event_date = str(row.get("event_date", ""))
        sector = sector_map.get(ticker)
        etf = SECTOR_TO_ETF.get(sector or "")
        etf_count = etf_coverage.get(etf or "", 0) if etf else 0
        etf_ret = etf_returns.get((etf, event_date)) if etf and event_date else None

        ticker_ret: Optional[float] = None
        raw = row.get("return_value")
        if raw is not None:
            try:
                ticker_ret = float(raw)
            except (ValueError, TypeError):
                pass

        relative_ret: Optional[float] = (
            ticker_ret - etf_ret
            if ticker_ret is not None and etf_ret is not None
            else None
        )

        peer_count = peer_counts.get((ticker, event_date), 0)

        subcause = classify_sector_cluster_row(
            ticker, sector, etf_coverage,
            event_date=event_date,
            etf_returns=etf_returns,
            ticker_return=ticker_ret,
            peer_count=peer_count,
        )

        result.append(SectorClusterDiag(
            ticker=ticker,
            event_date=event_date,
            sector=sector,
            etf_ticker=etf,
            etf_price_count=etf_count,
            etf_return=etf_ret,
            ticker_return=ticker_ret,
            relative_return=relative_ret,
            peer_count=peer_count,
            subcause=subcause,
            suggested_fix=SUBCAUSE_FIXES.get(subcause, ""),
        ))
    return result
```

- [ ] **Step 4: Run all sector diagnostics tests**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_sector_diagnostics.py -v 2>&1 | tail -30
```
Expected: all tests pass (14 existing + 11 new = 25 total).

If any of the existing tests fail, the backward-compatible defaults are not working. Check:
- `classify_sector_cluster_row("AAPL", None, {})` → `missing_sector_mapping` ✓
- `classify_sector_cluster_row("AAPL", "Information Technology", {})` → `missing_sector_etf_prices` ✓
- `classify_sector_cluster_row("AAPL", "Information Technology", {"XLK": 1})` → `peer_cluster_only_no_etf_data` (no event_date provided, so etf_return=None) ✓

- [ ] **Step 5: Commit**

```bash
git add health/sector_diagnostics.py tests/test_sector_diagnostics.py
git commit -m "feat(health): add ETF-confirmed sector attribution with 6 subcauses"
```

---

## Task 2: Update root-cause enrichment to pass ETF returns and peer counts

**Files:**
- Modify: `missed/root_cause_enrichment.py`
- Modify: `tests/test_enrichment_sector_subcause.py` — add ETF-confirmed test

The key change: in `enrich_rows()`, pre-fetch ETF returns and compute peer counts, then pass them to `_assign_root_cause()`. In `_assign_root_cause()`, pass `event_date`, `etf_returns`, `ticker_return`, and `peer_count` to `classify_sector_cluster_row()`.

- [ ] **Step 1: Add failing test to `tests/test_enrichment_sector_subcause.py`**

Append:

```python
def test_sector_cluster_subcause_etf_confirmed_when_etf_return_material(conn):
    """sector_cluster_subcause = sector_etf_confirmed when ETF return >= 0.5%."""
    import uuid
    from missed.root_cause_enrichment import enrich_rows
    for ticker in ("TST", "P1", "P2"):
        _insert_company(conn, ticker, "Information Technology")
    # Insert XLK with a material return (1.2%) for the cluster event date
    event_date = date.today() - timedelta(days=10)
    conn.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES (?, ?, ?, ?)",
        [uuid.uuid4().hex[:16], "XLK", str(event_date), 0.012],
    )
    rows = _make_cluster_rows(event_date=event_date)
    enriched = enrich_rows(rows, conn)
    tst = next(r for r in enriched if r["ticker"] == "TST")
    assert tst["enriched_root_cause"] == "sector_cluster_move"
    assert tst["sector_cluster_subcause"] == "sector_etf_confirmed"
```

Run to confirm failure:
```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_enrichment_sector_subcause.py::test_sector_cluster_subcause_etf_confirmed_when_etf_return_material -v 2>&1 | tail -10
```
Expected: `AssertionError` — subcause is still `peer_cluster_only_no_etf_data`.

- [ ] **Step 2: Add `_build_peer_counts()` to `missed/root_cause_enrichment.py`**

After the `_detect_sector_clusters()` function (line ~412), add:

```python
def _build_peer_counts(
    rows: list[dict],
    sector_map: dict[str, str],
    sector_clusters: set[tuple[str, str, Any]],
) -> dict[tuple[str, str, Any], int]:
    """Return {(ticker, event_date_str, window): peer_count} for cluster rows."""
    from collections import defaultdict
    sector_date_window: dict[tuple, list[str]] = defaultdict(list)
    for row in rows:
        ticker = str(row["ticker"])
        event_date = _coerce_date(row["event_date"])
        if event_date is None:
            continue
        sector = sector_map.get(ticker)
        if not sector:
            continue
        window = row.get("window_days")
        sector_date_window[(sector, str(event_date), window)].append(ticker)

    counts: dict[tuple[str, str, Any], int] = {}
    for (ticker, date_str, window) in sector_clusters:
        sector = sector_map.get(ticker)
        if not sector:
            counts[(ticker, date_str, window)] = 0
            continue
        group = sector_date_window.get((sector, date_str, window), [])
        counts[(ticker, date_str, window)] = len([t for t in group if t != ticker])
    return counts
```

- [ ] **Step 3: Update `enrich_rows()` to pre-fetch ETF returns and peer counts**

Find the pre-fetch block in `enrich_rows()`:

Old:
```python
    from health.sector_diagnostics import get_etf_coverage
    score_components = _fetch_score_components(conn)
    earnings_dates   = _fetch_earnings_dates(conn)
    sector_map       = _fetch_sector_map(conn)
    companies_data   = _fetch_companies_data(conn)
    sector_clusters  = _detect_sector_clusters(rows, sector_map)
    etf_coverage     = get_etf_coverage(conn)
    today            = date.today()
```

New:
```python
    from health.sector_diagnostics import get_etf_coverage, get_etf_returns
    score_components = _fetch_score_components(conn)
    earnings_dates   = _fetch_earnings_dates(conn)
    sector_map       = _fetch_sector_map(conn)
    companies_data   = _fetch_companies_data(conn)
    sector_clusters  = _detect_sector_clusters(rows, sector_map)
    etf_coverage     = get_etf_coverage(conn)
    etf_returns      = get_etf_returns(conn)
    peer_counts      = _build_peer_counts(rows, sector_map, sector_clusters)
    today            = date.today()
```

- [ ] **Step 4: Add `etf_returns` and `peer_counts` to `_assign_root_cause()` signature**

Find the current signature (the two new optional params after `etf_coverage`):

Old:
```python
    sector_map: dict[str, str] | None = None,
    etf_coverage: dict[str, int] | None = None,
    today: date,
```

New:
```python
    sector_map: dict[str, str] | None = None,
    etf_coverage: dict[str, int] | None = None,
    etf_returns: dict[tuple[str, str], float] | None = None,
    peer_counts: dict[tuple, int] | None = None,
    today: date,
```

- [ ] **Step 5: Update the sector cluster branch in `_assign_root_cause()`**

Find:
```python
    # Priority 5 — sector cluster
    if event_date is not None and (ticker, str(event_date), window) in sector_clusters:
        diag = dict(_INCOMPLETE_DIAG_EMPTY)
        if sector_map is not None and etf_coverage is not None:
            from health.sector_diagnostics import classify_sector_cluster_row
            diag["sector_cluster_subcause"] = classify_sector_cluster_row(
                ticker, sector_map.get(ticker), etf_coverage
            )
        return "sector_cluster_move", diag
```

Replace with:
```python
    # Priority 5 — sector cluster
    if event_date is not None and (ticker, str(event_date), window) in sector_clusters:
        diag = dict(_INCOMPLETE_DIAG_EMPTY)
        if sector_map is not None and etf_coverage is not None:
            from health.sector_diagnostics import classify_sector_cluster_row
            _ticker_ret: float | None = None
            _raw = row.get("return_value")
            if _raw is not None:
                try:
                    _ticker_ret = float(_raw)
                except (ValueError, TypeError):
                    pass
            _peer_count = (
                peer_counts.get((ticker, str(event_date), window), 0)
                if peer_counts is not None else 0
            )
            diag["sector_cluster_subcause"] = classify_sector_cluster_row(
                ticker, sector_map.get(ticker), etf_coverage,
                event_date=str(event_date),
                etf_returns=etf_returns,
                ticker_return=_ticker_ret,
                peer_count=_peer_count,
            )
        return "sector_cluster_move", diag
```

- [ ] **Step 6: Pass new params in `enrich_rows()` call to `_assign_root_cause()`**

Find the call:
```python
        label, diag = _assign_root_cause(
            row,
            score_components=score_components,
            earnings_dates=earnings_dates,
            sector_clusters=sector_clusters,
            companies_data=companies_data,
            sector_map=sector_map,
            etf_coverage=etf_coverage,
            today=today,
        )
```

Replace with:
```python
        label, diag = _assign_root_cause(
            row,
            score_components=score_components,
            earnings_dates=earnings_dates,
            sector_clusters=sector_clusters,
            companies_data=companies_data,
            sector_map=sector_map,
            etf_coverage=etf_coverage,
            etf_returns=etf_returns,
            peer_counts=peer_counts,
            today=today,
        )
```

- [ ] **Step 7: Run enrichment tests**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_enrichment_sector_subcause.py tests/test_root_cause_enrichment.py -v 2>&1 | tail -25
```
Expected: all pass including the new `test_sector_cluster_subcause_etf_confirmed_when_etf_return_material`.

- [ ] **Step 8: Commit**

```bash
git add missed/root_cause_enrichment.py tests/test_enrichment_sector_subcause.py
git commit -m "feat(enrichment): pass ETF returns and peer counts into sector cluster subcause classification"
```

---

## Task 3: Update `data sector-diagnostics` CLI command

**Files:**
- Modify: `main.py` — update the `data_sector_diagnostics_cmd` function to show new columns

- [ ] **Step 1: Find and update the CLI command in `main.py`**

Find the current command body (look for `data_sector_diagnostics_cmd`). Replace the output section to show ETF return, relative return, peer count, and suggested fix.

Old output section:
```python
    click.echo(f"Sector Cluster Diagnostics — {len(diags)} rows")
    click.echo(f"{'Ticker':<8} {'Event Date':<12} {'Sector':<26} {'ETF':<6} {'ETF Prices':<11} Subcause")
    click.echo("-" * 80)
    for d in diags:
        click.echo(
            f"{d.ticker:<8} {d.event_date:<12} {(d.sector or '—'):<26} "
            f"{(d.etf_ticker or '—'):<6} {d.etf_price_count:<11} {d.subcause}"
        )
```

New output section:
```python
    click.echo(f"Sector Cluster Diagnostics — {len(diags)} rows")
    click.echo(
        f"{'Ticker':<8} {'Date':<12} {'Sector':<22} {'ETF':<5} "
        f"{'ETF Ret':>8} {'Tkr Ret':>8} {'Rel Ret':>8} {'Peers':>5}  Subcause"
    )
    click.echo("-" * 95)
    for d in diags:
        etf_r = f"{d.etf_return:.3%}" if d.etf_return is not None else "—"
        tkr_r = f"{d.ticker_return:.3%}" if d.ticker_return is not None else "—"
        rel_r = f"{d.relative_return:.3%}" if d.relative_return is not None else "—"
        click.echo(
            f"{d.ticker:<8} {d.event_date:<12} {(d.sector or '—'):<22} "
            f"{(d.etf_ticker or '—'):<5} {etf_r:>8} {tkr_r:>8} {rel_r:>8} {d.peer_count:>5}  {d.subcause}"
        )
    # Group by subcause
    from collections import Counter
    sc_counts = Counter(d.subcause for d in diags)
    click.echo("\nSubcause summary:")
    for sc, cnt in sorted(sc_counts.items(), key=lambda x: -x[1]):
        fix = next((d.suggested_fix for d in diags if d.subcause == sc), "")
        click.echo(f"  {cnt:>3}x {sc}")
        if fix:
            click.echo(f"       → {fix}")
```

- [ ] **Step 2: Smoke-test CLI**

```bash
cd /home/jpcg/MHDE && venv/bin/python main.py data sector-diagnostics 2>&1 | head -25
```
Expected: table with ETF Ret, Tkr Ret, Rel Ret, Peers columns, then subcause summary.

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat(cli): add ETF return, relative return, peer count columns to sector-diagnostics"
```

---

## Task 4: Update `/learning` Sector Cluster Diagnostics section

**Files:**
- Modify: `review/server.py` — update `_learning_page()` sector_diag_html block

- [ ] **Step 1: Find the sector_diag_html block in `_learning_page()`**

Locate the block that builds `_diag_rows_html` in `_learning_page()`. Update the table to show 8 columns: Ticker, Date, Sector, ETF, ETF Ret, Ticker Ret, Rel Ret, Subcause, Suggested Fix.

Find the block:
```python
                if _diags:
                    _diag_rows_html = "".join(
                        f"<tr><td>{_esc(d.ticker)}</td><td>{_esc(d.event_date)}</td>"
                        f"<td>{_esc(d.sector or '—')}</td><td>{_esc(d.etf_ticker or '—')}</td>"
                        f"<td>{d.etf_price_count}</td><td><code>{_esc(d.subcause)}</code></td></tr>"
                        for d in _diags
                    )
                    sector_diag_html = (
                        "<h2>Sector Cluster Diagnostics</h2>"
                        '<p class="muted">Why each sector_cluster_move missed: '
                        "ETF coverage and wiring status.</p>"
                        "<table><tr><th>Ticker</th><th>Event Date</th><th>Sector</th>"
                        "<th>ETF</th><th>ETF Prices</th><th>Subcause</th></tr>"
                        + _diag_rows_html
                        + "</table>"
                    )
```

Replace with:
```python
                if _diags:
                    def _pct(v):
                        return f"{v:.2%}" if v is not None else "—"

                    _diag_rows_html = "".join(
                        f"<tr>"
                        f"<td>{_esc(d.ticker)}</td>"
                        f"<td>{_esc(d.event_date)}</td>"
                        f"<td>{_esc(d.sector or '—')}</td>"
                        f"<td>{_esc(d.etf_ticker or '—')}</td>"
                        f"<td class='num'>{_pct(d.etf_return)}</td>"
                        f"<td class='num'>{_pct(d.ticker_return)}</td>"
                        f"<td class='num'>{_pct(d.relative_return)}</td>"
                        f"<td>{d.peer_count}</td>"
                        f"<td><code>{_esc(d.subcause)}</code></td>"
                        f"<td class='muted' style='font-size:0.75rem'>{_esc(d.suggested_fix)}</td>"
                        f"</tr>"
                        for d in _diags
                    )
                    sector_diag_html = (
                        "<h2>Sector Cluster Diagnostics</h2>"
                        '<p class="muted">Why each sector_cluster_move missed — '
                        "ETF return, ticker return, relative outperformance, peer cluster size.</p>"
                        "<table><tr>"
                        "<th>Ticker</th><th>Date</th><th>Sector</th><th>ETF</th>"
                        "<th>ETF Ret</th><th>Tkr Ret</th><th>Rel Ret</th>"
                        "<th>Peers</th><th>Subcause</th><th>Suggested Fix</th>"
                        "</tr>"
                        + _diag_rows_html
                        + "</table>"
                    )
```

- [ ] **Step 2: Run review server tests**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_review_server.py -v 2>&1 | tail -20
```
Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add review/server.py
git commit -m "feat(dashboard): expand sector cluster diagnostics table with ETF/ticker/relative returns"
```

---

## Task 5: Full verification

- [ ] **Step 1: Run full test suite**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -10
```
Expected: ≥1265 passing (prior baseline), 0 failures.

- [ ] **Step 2: Run refresh-learning to regenerate enriched CSV with new subcauses**

```bash
cd /home/jpcg/MHDE && venv/bin/python main.py missed refresh-learning 2>&1 | tail -5
```

- [ ] **Step 3: Verify live CLI output shows ETF-confirmed subcauses**

```bash
cd /home/jpcg/MHDE && venv/bin/python main.py data sector-diagnostics 2>&1
```
Expected: rows show ETF return values, at least some rows should show `sector_etf_confirmed` (for ETF tickers that had positive ≥ 0.5% returns on 2026-05-01).

- [ ] **Step 4: Verify /learning page**

Restart the Flask review server and check:
```bash
curl -si http://127.0.0.1:8765/learning 2>/dev/null | grep -i "etf ret\|sector_etf\|ticker_outper\|sector_signal" | head -5
```

- [ ] **Step 5: Verify no scoring changes**

```bash
grep -n "score\|tier\|weight" /home/jpcg/MHDE/health/sector_diagnostics.py | grep -v "#\|str\|doc\|logger\|logging\|suggested"
```
Expected: no matches.

---

## Verification Summary

| Task | Test command | Expected |
|------|-------------|----------|
| 1: sector_diagnostics.py | `pytest tests/test_sector_diagnostics.py` | 25 PASS |
| 2: enrichment ETF pass-through | `pytest tests/test_enrichment_sector_subcause.py tests/test_root_cause_enrichment.py` | all PASS |
| 3: CLI | smoke-test shows ETF/relative return columns | no crash |
| 4: /learning | HTML contains ETF Ret / Subcause columns | 200 OK |
| 5: full suite | `pytest tests/ -q` | ≥1265, 0 failures |
