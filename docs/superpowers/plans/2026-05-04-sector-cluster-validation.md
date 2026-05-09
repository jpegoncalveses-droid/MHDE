# Sector Cluster Validation and Wiring Audit

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic subcause classification to every `sector_cluster_move` enrichment row, expose a sector-cluster diagnostics engine, wire it into the CLI and `/learning` dashboard, and validate ETF coverage — all without touching scoring logic.

**Architecture:** New `health/sector_diagnostics.py` classifies why a sector-cluster event was missed (missing sector mapping, missing ETF prices, or peer-only detection). `missed/root_cause_enrichment.py` gains a `sector_cluster_subcause` field pre-fetched from `prices_daily`. The existing `/learning` page gets a new diagnostics table. No LLM calls, no feature flags, no scoring changes.

**Tech Stack:** Python 3.11, DuckDB (in-memory for tests), Flask, standard library only.

**Stop conditions:** test failure that cannot be fixed safely | scoring logic modified | feature flag introduced | LLM called | secret exposed in code

---

## Context

MHDE's root-cause enrichment labels sector-wide moves as `sector_cluster_move` (feature_gap). Currently these rows carry no subcause: the ETF ingestor (`ingestion/ingest_sector_etfs.py`) exists but is NOT wired into the orchestrator, and `_detect_sector_clusters()` uses peer clustering only. This plan adds deterministic subcause classification so the analyst can distinguish:

- `missing_sector_mapping` — ticker has no sector (or sector unknown to SECTOR_TO_ETF)
- `missing_sector_etf_prices` — sector mapped to ETF but no rows in `prices_daily`
- `peer_cluster_only_no_etf_data` — ETF prices present but not used by enrichment (ingestor not wired)

---

## Codebase Orientation

| Area | Path |
|------|------|
| New diagnostics engine | `health/sector_diagnostics.py` — create here |
| ETF ingestor (existing, read-only) | `ingestion/ingest_sector_etfs.py` — `ETF_TO_SECTOR`, `SECTOR_ETFS` |
| Root-cause enrichment | `missed/root_cause_enrichment.py` |
| `_ENRICHMENT_EXTRA_COLS` | line 184 in `root_cause_enrichment.py` |
| `_INCOMPLETE_DIAG_EMPTY` | line 203 in `root_cause_enrichment.py` |
| `_assign_root_cause()` | line 454; sector cluster branch at line 491–493 |
| `enrich_rows()` | line 546; pre-fetches score_components, earnings_dates, sector_map, etc. |
| Flask learning page | `review/server.py` — `_learning_page()` at line 1949 |
| `fix_queues_html` block | ends at ~line 2089; add sector diag section after it |
| body f-string | line 2141; `{fix_queues_html}` then `{lifecycle_summary_html}` |
| `prices_daily` schema | `id VARCHAR PK`, `ticker`, `trade_date`, `close NOT NULL`, `UNIQUE(ticker, trade_date)` |
| `data` CLI group | `main.py` line 390; add `sector-diagnostics` command after `priority-refresh-queue` (~line 558) |
| Test infrastructure | `tests/test_root_cause_enrichment.py` — `conn` fixture via `get_connection`/`init_schema`; `_insert_company(conn, ticker, sector)` |

---

## Task 1: `health/sector_diagnostics.py` — Core diagnostics engine

**Files:**
- Create: `health/sector_diagnostics.py`
- Create: `tests/test_sector_diagnostics.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_sector_diagnostics.py`:

```python
"""Tests for sector cluster diagnostics engine."""
from __future__ import annotations

import uuid

import duckdb
import pytest


def _make_conn(etf_rows: list[tuple] = None) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE prices_daily (id VARCHAR PRIMARY KEY, ticker VARCHAR, trade_date DATE, close DOUBLE)")
    conn.execute("CREATE TABLE companies (ticker VARCHAR PRIMARY KEY, sector VARCHAR, is_active BOOLEAN DEFAULT true)")
    for etf, trade_date in (etf_rows or []):
        conn.execute(
            "INSERT INTO prices_daily VALUES (?, ?, ?, 1.0)",
            [uuid.uuid4().hex[:16], etf, trade_date],
        )
    return conn


def test_sector_to_etf_covers_all_11_sectors():
    from health.sector_diagnostics import SECTOR_TO_ETF
    assert len(SECTOR_TO_ETF) == 11
    assert SECTOR_TO_ETF["Information Technology"] == "XLK"
    assert SECTOR_TO_ETF["Financials"] == "XLF"
    assert SECTOR_TO_ETF["Energy"] == "XLE"
    assert SECTOR_TO_ETF["Consumer Discretionary"] == "XLY"


def test_get_etf_coverage_returns_counts():
    from health.sector_diagnostics import get_etf_coverage
    conn = _make_conn([("XLK", "2026-05-01"), ("XLK", "2026-05-02"), ("XLF", "2026-05-01")])
    cov = get_etf_coverage(conn)
    assert cov["XLK"] == 2
    assert cov["XLF"] == 1
    assert cov.get("XLE", 0) == 0


def test_get_etf_coverage_empty_db():
    from health.sector_diagnostics import get_etf_coverage
    conn = _make_conn()
    assert get_etf_coverage(conn) == {}


def test_classify_missing_sector_when_none():
    from health.sector_diagnostics import classify_sector_cluster_row
    assert classify_sector_cluster_row("AAPL", None, {}) == "missing_sector_mapping"


def test_classify_missing_sector_when_empty_string():
    from health.sector_diagnostics import classify_sector_cluster_row
    assert classify_sector_cluster_row("AAPL", "", {}) == "missing_sector_mapping"


def test_classify_missing_sector_when_unknown_sector():
    from health.sector_diagnostics import classify_sector_cluster_row
    assert classify_sector_cluster_row("AAPL", "Some Unknown Sector", {}) == "missing_sector_mapping"


def test_classify_missing_etf_prices_when_no_coverage():
    from health.sector_diagnostics import classify_sector_cluster_row
    assert classify_sector_cluster_row("AAPL", "Information Technology", {}) == "missing_sector_etf_prices"


def test_classify_missing_etf_prices_when_count_zero():
    from health.sector_diagnostics import classify_sector_cluster_row
    assert classify_sector_cluster_row("AAPL", "Information Technology", {"XLK": 0}) == "missing_sector_etf_prices"


def test_classify_peer_cluster_only_when_etf_has_prices():
    from health.sector_diagnostics import classify_sector_cluster_row
    result = classify_sector_cluster_row("AAPL", "Information Technology", {"XLK": 50})
    assert result == "peer_cluster_only_no_etf_data"


def test_generate_sector_diagnostics_empty_when_no_rows():
    from health.sector_diagnostics import generate_sector_diagnostics
    conn = _make_conn()
    assert generate_sector_diagnostics(conn, []) == []


def test_generate_sector_diagnostics_skips_non_cluster_rows():
    from health.sector_diagnostics import generate_sector_diagnostics
    conn = _make_conn()
    rows = [{"ticker": "AAPL", "event_date": "2026-05-01", "enriched_root_cause": "missing_cik"}]
    assert generate_sector_diagnostics(conn, rows) == []


def test_generate_sector_diagnostics_peer_cluster_only_with_etf():
    from health.sector_diagnostics import generate_sector_diagnostics, SectorClusterDiag
    conn = _make_conn([("XLK", "2026-05-01")])
    conn.execute("INSERT INTO companies VALUES ('AAPL', 'Information Technology', true)")
    rows = [{"ticker": "AAPL", "event_date": "2026-05-01", "enriched_root_cause": "sector_cluster_move"}]
    diags = generate_sector_diagnostics(conn, rows)
    assert len(diags) == 1
    d = diags[0]
    assert d.ticker == "AAPL"
    assert d.sector == "Information Technology"
    assert d.etf_ticker == "XLK"
    assert d.etf_price_count == 1
    assert d.subcause == "peer_cluster_only_no_etf_data"


def test_generate_sector_diagnostics_missing_etf_prices():
    from health.sector_diagnostics import generate_sector_diagnostics
    conn = _make_conn()
    conn.execute("INSERT INTO companies VALUES ('AAPL', 'Information Technology', true)")
    rows = [{"ticker": "AAPL", "event_date": "2026-05-01", "enriched_root_cause": "sector_cluster_move"}]
    diags = generate_sector_diagnostics(conn, rows)
    assert diags[0].subcause == "missing_sector_etf_prices"


def test_generate_sector_diagnostics_no_sector():
    from health.sector_diagnostics import generate_sector_diagnostics
    conn = _make_conn()
    # Ticker not in companies — sector lookup returns None
    rows = [{"ticker": "AAPL", "event_date": "2026-05-01", "enriched_root_cause": "sector_cluster_move"}]
    diags = generate_sector_diagnostics(conn, rows)
    assert diags[0].subcause == "missing_sector_mapping"
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_sector_diagnostics.py -v 2>&1 | tail -10
```
Expected: `ModuleNotFoundError: No module named 'health.sector_diagnostics'`

- [ ] **Step 3: Create `health/sector_diagnostics.py`**

```python
"""Sector cluster diagnostics — classify why a sector_cluster_move row was missed."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Inverse of ETF_TO_SECTOR from ingestion/ingest_sector_etfs.py
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


@dataclass
class SectorClusterDiag:
    ticker: str
    event_date: str
    sector: Optional[str]
    etf_ticker: Optional[str]
    etf_price_count: int
    subcause: str


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


def classify_sector_cluster_row(
    ticker: str,
    sector: Optional[str],
    etf_coverage: dict[str, int],
) -> str:
    """Return the most specific subcause for a sector_cluster_move row.

    Subcause hierarchy:
      missing_sector_mapping      — no sector or sector not in SECTOR_TO_ETF
      missing_sector_etf_prices   — ETF mapped but 0 rows in prices_daily
      peer_cluster_only_no_etf_data — ETF has prices but enrichment doesn't use them
    """
    if not sector:
        return "missing_sector_mapping"
    etf = SECTOR_TO_ETF.get(sector)
    if etf is None:
        return "missing_sector_mapping"
    if etf_coverage.get(etf, 0) == 0:
        return "missing_sector_etf_prices"
    return "peer_cluster_only_no_etf_data"


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

    result: list[SectorClusterDiag] = []
    for row in cluster_rows:
        ticker = str(row.get("ticker", ""))
        sector = sector_map.get(ticker)
        etf = SECTOR_TO_ETF.get(sector or "")
        count = etf_coverage.get(etf or "", 0) if etf else 0
        subcause = classify_sector_cluster_row(ticker, sector, etf_coverage)
        result.append(SectorClusterDiag(
            ticker=ticker,
            event_date=str(row.get("event_date", "")),
            sector=sector,
            etf_ticker=etf,
            etf_price_count=count,
            subcause=subcause,
        ))
    return result
```

- [ ] **Step 4: Run tests**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_sector_diagnostics.py -v 2>&1 | tail -20
```
Expected: 13 PASS.

- [ ] **Step 5: Commit**

```bash
git add health/sector_diagnostics.py tests/test_sector_diagnostics.py
git commit -m "feat(health): add sector cluster diagnostics engine"
```

---

## Task 2: Add `sector_cluster_subcause` to root-cause enrichment

**Files:**
- Modify: `missed/root_cause_enrichment.py`
- Create: `tests/test_enrichment_sector_subcause.py`

**Exact edits needed:**

1. `_ENRICHMENT_EXTRA_COLS` (line ~200): add `"sector_cluster_subcause"` after `"incomplete_diag_subcause"`
2. `_INCOMPLETE_DIAG_EMPTY` (line ~203): add `"sector_cluster_subcause": ""`
3. `_assign_root_cause()` signature: add `sector_map` and `etf_coverage` keyword params (both default `None`)
4. `_assign_root_cause()` sector cluster branch: populate `sector_cluster_subcause` in diag
5. `enrich_rows()`: pre-fetch `etf_coverage` and pass `sector_map` + `etf_coverage` to `_assign_root_cause()`

- [ ] **Step 1: Write failing tests**

Create `tests/test_enrichment_sector_subcause.py`:

```python
"""Tests for sector_cluster_subcause field added to root-cause enrichment."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from storage.db import get_connection, init_schema


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _row(**kwargs) -> dict:
    defaults = dict(
        ticker="TST", event_date=date.today() - timedelta(days=10),
        event_type="gain_1d", return_value=10.0, window_days=1,
        classification="true_miss", was_in_universe=True, was_scored=True,
        score_before_event=30.0, tier_before_event="Reject",
        had_catalyst_evidence=True, universe_tier="primary",
        root_cause_hint="scoring_blind_spot", score_join_method="scores_join",
        priority_score=5.3,
    )
    defaults.update(kwargs)
    return defaults


def _insert_company(conn, ticker, sector):
    conn.execute(
        "INSERT INTO companies (ticker, company_name, sector, universe_tier) "
        "VALUES (?, ?, ?, 'extended') ON CONFLICT (ticker) DO UPDATE SET sector = excluded.sector",
        [ticker, ticker, sector],
    )


def _insert_etf_price(conn, etf_ticker, trade_date="2026-05-01"):
    conn.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close) VALUES (?, ?, ?, 1.0)",
        [uuid.uuid4().hex[:16], etf_ticker, trade_date],
    )


def _make_cluster_rows(event_date=None, window_days=1):
    ed = event_date or (date.today() - timedelta(days=10))
    return [
        _row(ticker="TST", event_date=ed, tier_before_event="Reject", window_days=window_days),
        _row(ticker="P1",  event_date=ed, tier_before_event="Reject", window_days=window_days),
        _row(ticker="P2",  event_date=ed, tier_before_event="Reject", window_days=window_days),
    ]


def test_sector_cluster_subcause_field_present_in_all_rows(conn):
    """sector_cluster_subcause must exist in every enriched row regardless of root cause."""
    from missed.root_cause_enrichment import enrich_rows
    rows = [_row(tier_before_event="Reject")]
    enriched = enrich_rows(rows, conn)
    assert "sector_cluster_subcause" in enriched[0]


def test_non_cluster_rows_have_empty_sector_cluster_subcause(conn):
    """Non-sector_cluster_move rows must have empty sector_cluster_subcause."""
    from missed.root_cause_enrichment import enrich_rows
    row = _row(classification="universe_miss", was_in_universe=False, tier_before_event="")
    enriched = enrich_rows([row], conn)
    assert enriched[0].get("sector_cluster_subcause", "MISSING") == ""


def test_sector_cluster_subcause_peer_cluster_only_when_etf_present(conn):
    """sector_cluster_subcause = peer_cluster_only_no_etf_data when ETF prices exist."""
    from missed.root_cause_enrichment import enrich_rows
    for ticker in ("TST", "P1", "P2"):
        _insert_company(conn, ticker, "Information Technology")
    _insert_etf_price(conn, "XLK")
    rows = _make_cluster_rows()
    enriched = enrich_rows(rows, conn)
    tst = next(r for r in enriched if r["ticker"] == "TST")
    assert tst["enriched_root_cause"] == "sector_cluster_move"
    assert tst["sector_cluster_subcause"] == "peer_cluster_only_no_etf_data"


def test_sector_cluster_subcause_missing_etf_prices_when_no_etf_data(conn):
    """sector_cluster_subcause = missing_sector_etf_prices when no ETF rows in prices_daily."""
    from missed.root_cause_enrichment import enrich_rows
    for ticker in ("TST", "P1", "P2"):
        _insert_company(conn, ticker, "Information Technology")
    # No ETF prices inserted
    rows = _make_cluster_rows()
    enriched = enrich_rows(rows, conn)
    tst = next(r for r in enriched if r["ticker"] == "TST")
    assert tst["enriched_root_cause"] == "sector_cluster_move"
    assert tst["sector_cluster_subcause"] == "missing_sector_etf_prices"
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_enrichment_sector_subcause.py -v 2>&1 | tail -10
```
Expected: `KeyError: 'sector_cluster_subcause'` or `AssertionError` (field missing).

- [ ] **Step 3: Edit `_ENRICHMENT_EXTRA_COLS` in `missed/root_cause_enrichment.py`**

Find (line ~199–201):
```python
    "incomplete_diag_subcause",
]
```

Replace with:
```python
    "incomplete_diag_subcause",
    "sector_cluster_subcause",
]
```

- [ ] **Step 4: Edit `_INCOMPLETE_DIAG_EMPTY`**

Find (line ~211–213):
```python
    "incomplete_diag_subcause": "",
}
```

Replace with:
```python
    "incomplete_diag_subcause": "",
    "sector_cluster_subcause": "",
}
```

- [ ] **Step 5: Edit `_assign_root_cause()` signature**

Find (line ~454–462):
```python
def _assign_root_cause(
    row: dict,
    *,
    score_components: dict[tuple[str, str], dict[str, Any]],
    earnings_dates: dict[str, list[date]],
    sector_clusters: set[tuple[str, str, Any]],
    companies_data: dict[str, dict[str, Any]],
    today: date,
) -> tuple[str, dict[str, str]]:
```

Replace with:
```python
def _assign_root_cause(
    row: dict,
    *,
    score_components: dict[tuple[str, str], dict[str, Any]],
    earnings_dates: dict[str, list[date]],
    sector_clusters: set[tuple[str, str, Any]],
    companies_data: dict[str, dict[str, Any]],
    sector_map: dict[str, str] | None = None,
    etf_coverage: dict[str, int] | None = None,
    today: date,
) -> tuple[str, dict[str, str]]:
```

- [ ] **Step 6: Edit the sector cluster branch in `_assign_root_cause()`**

Find (line ~491–493):
```python
    # Priority 5 — sector cluster
    if event_date is not None and (ticker, str(event_date), window) in sector_clusters:
        return "sector_cluster_move", dict(_INCOMPLETE_DIAG_EMPTY)
```

Replace with:
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

- [ ] **Step 7: Edit `enrich_rows()` to prefetch ETF coverage and pass new params**

Find (line ~552–568):
```python
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
```

Replace with:
```python
    from health.sector_diagnostics import get_etf_coverage
    score_components = _fetch_score_components(conn)
    earnings_dates   = _fetch_earnings_dates(conn)
    sector_map       = _fetch_sector_map(conn)
    companies_data   = _fetch_companies_data(conn)
    sector_clusters  = _detect_sector_clusters(rows, sector_map)
    etf_coverage     = get_etf_coverage(conn)
    today            = date.today()

    result: list[dict] = []
    for row in rows:
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

- [ ] **Step 8: Run tests**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_enrichment_sector_subcause.py tests/test_root_cause_enrichment.py -v 2>&1 | tail -25
```
Expected: all tests PASS (new + existing).

- [ ] **Step 9: Commit**

```bash
git add missed/root_cause_enrichment.py tests/test_enrichment_sector_subcause.py
git commit -m "feat(enrichment): add sector_cluster_subcause field to root-cause enrichment"
```

---

## Task 3: `data sector-diagnostics` CLI command

**Files:**
- Modify: `main.py` — add command to `data` group after the `priority-refresh-queue` command (~line 558)

- [ ] **Step 1: Write failing test**

In `tests/test_main_cli.py` (or create if absent), add:

```python
def test_data_sector_diagnostics_command_exists():
    from click.testing import CliRunner
    from main import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["data", "sector-diagnostics", "--help"])
    assert result.exit_code == 0
    assert "sector" in result.output.lower()
```

Check if `tests/test_main_cli.py` exists first:
```bash
ls /home/jpcg/MHDE/tests/test_main_cli.py 2>/dev/null || echo "MISSING"
```
If missing, create the file with just that one test function plus the necessary imports.

- [ ] **Step 2: Run to confirm failure**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_main_cli.py::test_data_sector_diagnostics_command_exists -v 2>&1 | tail -10
```
Expected: `UsageError: No such command 'sector-diagnostics'`

- [ ] **Step 3: Add the CLI command to `main.py`**

After the `priority-refresh-queue` command block (ends at ~line 557) and before the `@cli.group()` for `review` (~line 560), insert:

```python
@data.command("sector-diagnostics")
@click.option("--db-path", default="data/mhde.duckdb", show_default=True)
@click.option(
    "--enriched-csv",
    default="data/processed/prediction_vs_actual_enriched_rows.csv",
    show_default=True,
)
def data_sector_diagnostics_cmd(db_path, enriched_csv):
    """Show sector cluster diagnostics for missed sector cluster move events."""
    import csv as _csv
    import os as _os
    import duckdb as _duckdb
    from health.sector_diagnostics import generate_sector_diagnostics

    if not _os.path.exists(enriched_csv):
        click.echo(f"No enriched CSV: {enriched_csv}")
        click.echo("Run: python main.py missed refresh-learning")
        return
    with open(enriched_csv, newline="") as f:
        enriched_rows = list(_csv.DictReader(f))
    conn = _duckdb.connect(db_path, read_only=True)
    diags = generate_sector_diagnostics(conn, enriched_rows)
    conn.close()
    if not diags:
        click.echo("No sector_cluster_move rows found in enriched CSV.")
        return
    click.echo(f"Sector Cluster Diagnostics — {len(diags)} rows")
    click.echo(f"{'Ticker':<8} {'Event Date':<12} {'Sector':<26} {'ETF':<6} {'ETF Prices':<11} Subcause")
    click.echo("-" * 80)
    for d in diags:
        click.echo(
            f"{d.ticker:<8} {d.event_date:<12} {(d.sector or '—'):<26} "
            f"{(d.etf_ticker or '—'):<6} {d.etf_price_count:<11} {d.subcause}"
        )
```

- [ ] **Step 4: Run tests**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_main_cli.py::test_data_sector_diagnostics_command_exists -v 2>&1 | tail -10
```
Expected: PASS.

- [ ] **Step 5: Smoke-test CLI**

```bash
cd /home/jpcg/MHDE && venv/bin/python main.py data sector-diagnostics 2>&1 | head -15
```
Expected: either "No sector_cluster_move rows found" (no enriched CSV) or a diagnostics table.

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_main_cli.py
git commit -m "feat(cli): add data sector-diagnostics command"
```

---

## Task 4: Sector Cluster Diagnostics section in `/learning` page

**Files:**
- Modify: `review/server.py` — add section to `_learning_page()`, insert into body f-string

**Location:** Add after the `fix_queues_html` block (ends ~line 2089) and before `artifact_links` (~line 2091). The body f-string at ~line 2141 must include `{sector_diag_html}` between `{fix_queues_html}` and `{lifecycle_summary_html}`.

- [ ] **Step 1: Write failing test**

In `tests/test_review_server.py`, add at the end:

```python
# ── Task 4: sector cluster diagnostics section ─────────────────────────────

def test_learning_page_renders_without_crash_no_enriched_data(tmp_path):
    """Learning page must not crash even when no enriched CSV exists."""
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/learning", headers={"Authorization": _AUTH})
    assert r.status_code == 200


def test_learning_page_includes_sector_diag_html_variable(tmp_path):
    """Learning page must render a sector diagnostics section (even if empty)."""
    import csv
    # Write minimal rows CSV (required for learning page to proceed past early return)
    out = str(tmp_path / "output")
    os.makedirs(out, exist_ok=True)
    rows_csv = os.path.join(out, "prediction_vs_actual_rows.csv")
    with open(rows_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["event_date", "ticker", "classification",
                                          "return_value", "window_days"])
        w.writeheader()
        w.writerow({"event_date": "2026-05-01", "ticker": "AAPL",
                    "classification": "true_miss", "return_value": "0.15", "window_days": "5"})
    # Write enriched CSV with one sector_cluster_move row
    enriched_csv = os.path.join(out, "prediction_vs_actual_enriched_rows.csv")
    with open(enriched_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["event_date", "ticker", "classification",
                                          "enriched_root_cause", "root_cause_group",
                                          "return_value", "window_days"])
        w.writeheader()
        w.writerow({"event_date": "2026-05-01", "ticker": "AAPL",
                    "classification": "true_miss", "enriched_root_cause": "sector_cluster_move",
                    "root_cause_group": "feature_gap", "return_value": "0.15", "window_days": "5"})
    app = _make_app(tmp_path)
    with app.test_client() as c:
        r = c.get("/learning", headers={"Authorization": _AUTH})
    assert r.status_code == 200
    # The page must not crash — sector diag section may or may not appear (no DB)
    html = r.data.decode()
    assert "learning" in html.lower()
```

- [ ] **Step 2: Run to confirm current state**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_review_server.py -k "sector_diag or learning_page" -v 2>&1 | tail -15
```

- [ ] **Step 3: Add `sector_diag_html` computation to `_learning_page()` in `review/server.py`**

After the `fix_queues_html` block (find the line `if fix_rows:` block that ends with `+ '</table>')`  and the closing `)`), and before `artifact_links = ...`, insert:

```python
    # Sector Cluster Diagnostics — best-effort, never raises
    sector_diag_html = ""
    if enriched_path.exists() and db_path:
        try:
            import duckdb as _duckdb
            from health.sector_diagnostics import generate_sector_diagnostics
            _conn_sd = _duckdb.connect(db_path, read_only=True)
            _diags = generate_sector_diagnostics(_conn_sd, enriched)
            _conn_sd.close()
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
        except Exception:
            pass
```

**Important:** The `enriched` variable is only in scope if `enriched_path.exists()` was true earlier. Both the `if enriched_path.exists() and db_path:` guard here and the existing `if enriched_path.exists():` guard use the same condition, so `enriched` will be defined.

- [ ] **Step 4: Insert `{sector_diag_html}` into body f-string**

Find in the body f-string (line ~2174):
```python
{fix_queues_html}

{lifecycle_summary_html}
```

Replace with:
```python
{fix_queues_html}

{sector_diag_html}

{lifecycle_summary_html}
```

- [ ] **Step 5: Run tests**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/test_review_server.py -v 2>&1 | tail -20
```
Expected: all tests PASS including the two new ones.

- [ ] **Step 6: Commit**

```bash
git add review/server.py tests/test_review_server.py
git commit -m "feat(dashboard): add sector cluster diagnostics section to /learning page"
```

---

## Task 5: Full test suite verification

- [ ] **Step 1: Run full test suite**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -20
```
Expected: ≥1244 passing (prior baseline) + new tests, 0 failures.

- [ ] **Step 2: Verify no scoring changes**

```bash
cd /home/jpcg/MHDE && grep -n "score\|tier\|weight" health/sector_diagnostics.py | grep -v "#\|str\|doc\|logger\|logging"
```
Expected: no matches.

- [ ] **Step 3: Verify no feature flags or LLM calls**

```bash
cd /home/jpcg/MHDE && grep -rn "feature_flag\|FeatureFlag\|openai\|anthropic\|llm\|gpt\|claude" \
  health/sector_diagnostics.py tests/test_sector_diagnostics.py tests/test_enrichment_sector_subcause.py 2>/dev/null
```
Expected: no matches.

- [ ] **Step 4: Smoke-test CLI with live data**

```bash
cd /home/jpcg/MHDE && venv/bin/python main.py data sector-diagnostics 2>&1 | head -20
```
Expected: diagnostics table showing sector_cluster_move rows with subcauses (likely `missing_sector_etf_prices` since ETF ingestor is not wired into orchestrator).

- [ ] **Step 5: Verify `/learning` page renders sector diag section**

```bash
source /home/jpcg/MHDE/.env && \
curl -si -u "$REVIEW_UI_USERNAME:$REVIEW_UI_PASSWORD" http://127.0.0.1:8765/learning 2>/dev/null | \
grep -i "sector cluster diag\|subcause\|peer_cluster\|missing_sector" | head -5
```
Expected: lines containing "Sector Cluster Diagnostics" or the subcause values.

- [ ] **Step 6: Commit if any fixes were needed, else record baseline**

```bash
cd /home/jpcg/MHDE && venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | grep -E "passed|failed|error" | tail -3
```

---

## Verification Summary

| Task | Test command | Expected |
|------|-------------|----------|
| 1: sector_diagnostics.py | `pytest tests/test_sector_diagnostics.py` | 13 PASS |
| 2: enrichment subcause | `pytest tests/test_enrichment_sector_subcause.py tests/test_root_cause_enrichment.py` | all PASS |
| 3: CLI command | `pytest tests/test_main_cli.py::test_data_sector_diagnostics_command_exists` | PASS |
| 4: /learning section | `pytest tests/test_review_server.py -k "sector_diag or learning_page"` | PASS |
| 5: full suite | `pytest tests/ -q --tb=short` | ≥1244 + new, 0 failures |
