# Universe S&P 500 YAML Seed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the arbitrary SEC CIK-ordered ~500-ticker universe with an explicit, reproducible S&P 500 seed that marks all index members as `universe_tier="primary"`, populates `companies.sector` and `companies.industry` from YAML data, and deactivates removed primary tickers on each rebuild.

**Architecture:** A generator script fetches the live S&P 500 list from Wikipedia and writes `universe/sp500_tickers.yaml`. A new `universe/sp500_loader.py` module reads that YAML with zero side effects. `universe/universe_builder.py` merges YAML tickers with the existing `config/universe.yaml` fallback list into one combined primary set, writes `sector`/`industry` on upsert (COALESCE so existing values are never overwritten with NULL), then runs a reconciliation query that deactivates `universe_tier='primary'` tickers no longer present in either list. `max_symbols` caps only the extended tier; primary tickers are never truncated.

**Tech Stack:** Python 3.11, DuckDB, PyYAML (already in requirements), requests, pytest, click

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `.claude/local_scripts/generate_sp500_yaml.py` | Create | One-shot script: fetches Wikipedia table, writes `universe/sp500_tickers.yaml` |
| `universe/sp500_tickers.yaml` | Create (generated) | Committed data file: ~503 S&P 500 tickers with sector/industry/cik |
| `universe/sp500_loader.py` | Create | `load_sp500_yaml(path) → list[dict]` — pure loader, no side effects |
| `universe/universe_builder.py` | Modify | Import loader; merge YAML + config fallback into primary set; add sector/industry to upsert; run reconciliation after upserts |
| `config/universe.yaml` | Modify | `max_symbols: 500 → 520` (leaves room for extended after ~503 primaries) |
| `tests/test_universe_builder.py` | Create | 8 unit tests for new builder behaviours |
| `main.py` | Modify | Add `data universe-stats` subcommand |

---

### Task 1: Generator script and committed YAML

**Files:**
- Create: `.claude/local_scripts/generate_sp500_yaml.py`
- Create: `universe/sp500_tickers.yaml` (run the script, commit the output)

- [ ] **Step 1: Create the generator script**

Create `.claude/local_scripts/generate_sp500_yaml.py` with this exact content:

```python
#!/usr/bin/env python3
"""Fetch the S&P 500 constituent table from Wikipedia and write universe/sp500_tickers.yaml.

Run from the MHDE project root:
    venv/bin/python .claude/local_scripts/generate_sp500_yaml.py

Wikipedia table columns (0-indexed within <td> cells):
    0  Symbol          (ticker)
    1  Security        (company name)
    2  GICS Sector
    3  GICS Sub-Industry
    4  Headquarters Location
    5  Date First Added
    6  CIK
    7  Founded
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

import requests
import yaml

_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_USER_AGENT = "MHDE-Engine contact@example.com"


def _clean(html: str) -> str:
    text = re.sub(r"<[^>]+>", "", html)
    return text.replace("\n", " ").replace("\xa0", " ").strip()


def fetch_sp500() -> list[dict]:
    resp = requests.get(_URL, headers={"User-Agent": _USER_AGENT}, timeout=30)
    resp.raise_for_status()
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", resp.text, re.DOTALL)
    companies: list[dict] = []
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        if len(cells) < 4:
            continue
        ticker = _clean(cells[0])
        name = _clean(cells[1])
        if not ticker or not name:
            continue
        entry: dict = {"ticker": ticker, "company_name": name}
        sector = _clean(cells[2]) if len(cells) > 2 else ""
        industry = _clean(cells[3]) if len(cells) > 3 else ""
        cik_raw = _clean(cells[6]) if len(cells) > 6 else ""
        if sector:
            entry["sector"] = sector
        if industry:
            entry["industry"] = industry
        if cik_raw and re.fullmatch(r"\d+", cik_raw):
            entry["cik"] = cik_raw.zfill(10)
        companies.append(entry)
    return companies


def main() -> None:
    print(f"Fetching S&P 500 list from {_URL} ...", file=sys.stderr)
    companies = fetch_sp500()
    if len(companies) < 400:
        print(
            f"ERROR: only {len(companies)} companies found — "
            "Wikipedia table format may have changed",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"Found {len(companies)} companies", file=sys.stderr)
    data = {
        "last_updated": str(date.today()),
        "source": _URL,
        "tickers": companies,
    }
    out = Path("universe/sp500_tickers.yaml")
    with out.open("w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    print(f"Written {len(companies)} entries to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the generator**

```bash
venv/bin/python .claude/local_scripts/generate_sp500_yaml.py
```

Expected stderr output:
```
Fetching S&P 500 list from https://en.wikipedia.org/wiki/List_of_S%26P_500_companies ...
Found 503 companies
Written 503 entries to universe/sp500_tickers.yaml
```

(Exact count varies by index rebalance; any value between 490 and 510 is acceptable.)

- [ ] **Step 3: Verify the YAML**

```bash
head -8 universe/sp500_tickers.yaml
grep -c "  ticker:" universe/sp500_tickers.yaml
grep "BRK.B\|BF.B" universe/sp500_tickers.yaml
grep "  cik:" universe/sp500_tickers.yaml | head -5
```

Expected: header shows `last_updated`, `source`, `tickers:`; count is 490–510; BRK.B and BF.B have entries with `cik:` field; at least one cik line appears.

- [ ] **Step 4: Commit**

```bash
git add .claude/local_scripts/generate_sp500_yaml.py universe/sp500_tickers.yaml
git commit -m "data: add S&P 500 YAML seed and generator script"
```

---

### Task 2: `universe/sp500_loader.py`

**Files:**
- Create: `universe/sp500_loader.py`
- Test: `tests/test_universe_builder.py` (written in Task 5, but referenced here for context)

- [ ] **Step 1: Write the failing test (loader)**

Create `tests/test_universe_builder.py` with only the loader test for now:

```python
from __future__ import annotations

import yaml
import pytest
import duckdb

from storage.db import init_schema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_yaml(path, tickers):
    """Write a minimal sp500_tickers.yaml to path."""
    data = {
        "last_updated": "2026-05-03",
        "source": "test",
        "tickers": tickers,
    }
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False)


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    init_schema(c)
    yield c
    c.close()


def _cfg(max_symbols=20, fallback_tickers=None):
    return {
        "universe": {
            "max_symbols": max_symbols,
            "fallback_tickers": fallback_tickers or [],
            "exclude_etfs": True,
            "exclude_funds": True,
            "exclude_adrs": False,
        }
    }


def _sec_co(ticker, name="Corp"):
    return {"ticker": ticker, "cik": "0001234567", "company_name": f"{ticker} {name}"}


# ---------------------------------------------------------------------------
# Task 2 loader test
# ---------------------------------------------------------------------------

def test_load_sp500_yaml_returns_list(tmp_path):
    from universe.sp500_loader import load_sp500_yaml
    f = tmp_path / "sp500.yaml"
    _write_yaml(f, [{"ticker": "AAPL", "company_name": "Apple Inc", "sector": "IT"}])
    result = load_sp500_yaml(f)
    assert result == [{"ticker": "AAPL", "company_name": "Apple Inc", "sector": "IT"}]


def test_load_sp500_yaml_missing_file_returns_empty(tmp_path):
    from universe.sp500_loader import load_sp500_yaml
    result = load_sp500_yaml(tmp_path / "nonexistent.yaml")
    assert result == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
venv/bin/python -m pytest tests/test_universe_builder.py::test_load_sp500_yaml_returns_list -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'universe.sp500_loader'`

- [ ] **Step 3: Create `universe/sp500_loader.py`**

```python
from __future__ import annotations

from pathlib import Path

import yaml


def load_sp500_yaml(yaml_path: str | Path) -> list[dict]:
    """Load S&P 500 tickers from YAML file. Returns empty list if file is absent or malformed."""
    path = Path(yaml_path)
    if not path.exists():
        return []
    with path.open() as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        return []
    return data.get("tickers", [])
```

- [ ] **Step 4: Run loader tests to verify they pass**

```bash
venv/bin/python -m pytest tests/test_universe_builder.py::test_load_sp500_yaml_returns_list tests/test_universe_builder.py::test_load_sp500_yaml_missing_file_returns_empty -v
```

Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add universe/sp500_loader.py tests/test_universe_builder.py
git commit -m "feat: add universe/sp500_loader.py with load_sp500_yaml"
```

---

### Task 3: Add remaining builder tests

**Files:**
- Modify: `tests/test_universe_builder.py` (add 8 builder behaviour tests)

All 8 tests mock both `fetch_sec_company_tickers` and `load_sp500_yaml` via `monkeypatch`, so no network calls are made. The `conn` fixture uses an in-memory DuckDB with the full schema.

- [ ] **Step 1: Add all 8 builder behaviour tests to `tests/test_universe_builder.py`**

Append the following below the two loader tests already in the file:

```python
# ---------------------------------------------------------------------------
# Builder behaviour tests (Tasks 3 and 4 validate these)
# ---------------------------------------------------------------------------

def test_yaml_tickers_load_as_primary(conn, monkeypatch):
    yaml_entries = [{"ticker": "FOO", "company_name": "Foo Inc", "sector": "Financials"}]
    sec = [_sec_co("BAR"), _sec_co("BAZ")]
    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: yaml_entries)
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: sec)
    from universe.universe_builder import build_universe
    build_universe(conn, _cfg())
    row = conn.execute(
        "SELECT universe_tier FROM companies WHERE ticker = 'FOO'"
    ).fetchone()
    assert row is not None and row[0] == "primary"


def test_config_fallback_tickers_preserved(conn, monkeypatch):
    yaml_entries = [{"ticker": "FOO", "company_name": "Foo Inc"}]
    sec = [_sec_co("BAR")]
    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: yaml_entries)
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: sec)
    from universe.universe_builder import build_universe
    build_universe(conn, _cfg(fallback_tickers=["BZZZ"]))
    row = conn.execute(
        "SELECT universe_tier FROM companies WHERE ticker = 'BZZZ'"
    ).fetchone()
    assert row is not None and row[0] == "primary"


def test_sec_fillers_load_as_extended(conn, monkeypatch):
    yaml_entries = [{"ticker": "FOO", "company_name": "Foo Inc"}]
    sec = [_sec_co("BAR"), _sec_co("BAZ")]
    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: yaml_entries)
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: sec)
    from universe.universe_builder import build_universe
    build_universe(conn, _cfg())
    row = conn.execute(
        "SELECT universe_tier FROM companies WHERE ticker = 'BAR'"
    ).fetchone()
    assert row is not None and row[0] == "extended"


def test_primary_tickers_not_truncated_by_max_symbols(conn, monkeypatch):
    """10 YAML primaries with max_symbols=5 — all 10 must survive."""
    yaml_entries = [
        {"ticker": f"Y{i:03d}", "company_name": f"Y{i} Inc"} for i in range(10)
    ]
    sec = [_sec_co(f"S{i:03d}") for i in range(5)]
    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: yaml_entries)
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: sec)
    from universe.universe_builder import build_universe
    build_universe(conn, _cfg(max_symbols=5))
    count = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE universe_tier = 'primary' AND is_active = true"
    ).fetchone()[0]
    assert count == 10


def test_sector_industry_populate_from_yaml(conn, monkeypatch):
    yaml_entries = [{
        "ticker": "FOO",
        "company_name": "Foo Inc",
        "sector": "Energy",
        "industry": "Oil & Gas Exploration & Production",
    }]
    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: yaml_entries)
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: [])
    from universe.universe_builder import build_universe
    build_universe(conn, _cfg())
    row = conn.execute(
        "SELECT sector, industry FROM companies WHERE ticker = 'FOO'"
    ).fetchone()
    assert row is not None
    assert row[0] == "Energy"
    assert row[1] == "Oil & Gas Exploration & Production"


def test_duplicate_tickers_deduped(conn, monkeypatch):
    yaml_entries = [
        {"ticker": "FOO", "company_name": "Foo Inc"},
        {"ticker": "FOO", "company_name": "Foo Duplicate"},
    ]
    sec = [_sec_co("FOO")]
    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: yaml_entries)
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: sec)
    from universe.universe_builder import build_universe
    build_universe(conn, _cfg())
    count = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE ticker = 'FOO'"
    ).fetchone()[0]
    assert count == 1


def test_removed_primary_ticker_deactivated(conn, monkeypatch):
    """FOO is primary on run 1. YAML is emptied on run 2. FOO must become inactive."""
    from universe.universe_builder import build_universe

    # Run 1: FOO in YAML
    monkeypatch.setattr(
        "universe.universe_builder.load_sp500_yaml",
        lambda p: [{"ticker": "FOO", "company_name": "Foo Inc"}],
    )
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: [])
    build_universe(conn, _cfg())
    assert conn.execute(
        "SELECT is_active FROM companies WHERE ticker = 'FOO'"
    ).fetchone()[0] is True

    # Run 2: YAML empty, config fallback empty
    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: [])
    build_universe(conn, _cfg())
    assert conn.execute(
        "SELECT is_active FROM companies WHERE ticker = 'FOO'"
    ).fetchone()[0] is False


def test_dot_ticker_bypasses_filter(conn, monkeypatch):
    """BRK.B is dropped by filter_non_equities (dot in ticker) but must be inserted
    when it comes from the YAML primary list, preserving its CIK."""
    yaml_entries = [{
        "ticker": "BRK.B",
        "company_name": "Berkshire Hathaway Inc",
        "sector": "Financials",
        "industry": "Multi-Sector Holdings",
        "cik": "0001067983",
    }]
    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: yaml_entries)
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: [])
    from universe.universe_builder import build_universe
    build_universe(conn, _cfg())
    row = conn.execute(
        "SELECT ticker, universe_tier, cik FROM companies WHERE ticker = 'BRK.B'"
    ).fetchone()
    assert row is not None, "BRK.B must be in companies table"
    assert row[1] == "primary"
    assert row[2] == "0001067983"
```

- [ ] **Step 2: Run all builder behaviour tests to verify they fail**

```bash
venv/bin/python -m pytest tests/test_universe_builder.py -k "not load_sp500" -v 2>&1 | tail -20
```

Expected: 8 tests fail (build_universe doesn't call load_sp500_yaml yet; primary tickers still come only from config).

- [ ] **Step 3: Commit test file with all tests (red)**

```bash
git add tests/test_universe_builder.py
git commit -m "test: add 8 failing universe builder behaviour tests"
```

---

### Task 4: Modify `universe/universe_builder.py`

**Files:**
- Modify: `universe/universe_builder.py`

Current file is 117 lines at `universe/universe_builder.py`. Replace its entire contents with the following (all changes are visible — no hidden differences):

- [ ] **Step 1: Overwrite `universe/universe_builder.py`**

```python
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path

import duckdb

from universe.sec_company_tickers import fetch_sec_company_tickers
from universe.filters import filter_non_equities, classify_company
from universe.sp500_loader import load_sp500_yaml

logger = logging.getLogger("mhde.universe")

_SP500_YAML = Path(__file__).parent / "sp500_tickers.yaml"

_WARNING = (
    "Universe primary tier sourced from universe/sp500_tickers.yaml + "
    "config fallback_tickers. Extended tier filled from SEC list filtered "
    "by name heuristics. No market-cap or liquidity filter applied."
)


def build_universe(conn: duckdb.DuckDBPyConnection, cfg: dict) -> int:
    """Build the universe: primary tier from S&P 500 YAML + config fallback,
    extended tier from filtered SEC list. Returns active company count.
    """
    universe_cfg = cfg.get("universe", {})
    max_symbols = universe_cfg.get("max_symbols", 500)
    config_fallback = [t.upper() for t in universe_cfg.get("fallback_tickers", [])]

    # --- Merge YAML + config into one ordered primary dict (YAML first) ---
    sp500_entries = load_sp500_yaml(_SP500_YAML)
    primary_meta: dict[str, dict] = {}
    for e in sp500_entries:
        t = e.get("ticker", "").upper()
        if t:
            primary_meta[t] = e
    for t in config_fallback:
        if t not in primary_meta:
            primary_meta[t] = {"ticker": t}

    logger.warning(_WARNING)

    # --- Fetch SEC list (used only to look up cik/name for primary tickers
    #     that exist in SEC's registry, and to fill extended slots) ---
    raw = fetch_sec_company_tickers()
    if not raw:
        logger.warning("SEC fetch failed — using primary list only")
        raw = []

    filtered = filter_non_equities(raw, universe_cfg)
    filtered_lookup: dict[str, dict] = {co["ticker"]: co for co in filtered}

    seen: set[str] = set()
    ordered: list[dict] = []

    # Primary tier: never capped by max_symbols
    for ticker, meta in primary_meta.items():
        if ticker in seen:
            continue
        if ticker in filtered_lookup:
            co = filtered_lookup[ticker].copy()
        else:
            co = {
                "ticker": ticker,
                "cik": meta.get("cik"),
                "company_name": meta.get("company_name", ticker),
                "is_etf": False,
                "is_fund": False,
                "is_adr": False,
                "is_active": True,
            }
            co = classify_company(co)
        co["universe_tier"] = "primary"
        # YAML sector/industry override SEC (which has none anyway)
        co["sector"] = meta.get("sector")
        co["industry"] = meta.get("industry")
        # YAML CIK takes precedence (needed for dot-class tickers)
        if meta.get("cik"):
            co["cik"] = meta["cik"]
        ordered.append(co)
        seen.add(ticker)

    # Extended tier: fills up to max_symbols
    for co in filtered:
        if len(ordered) >= max_symbols:
            break
        if co["ticker"] not in seen:
            co["universe_tier"] = "extended"
            co["sector"] = None
            co["industry"] = None
            ordered.append(co)
            seen.add(co["ticker"])

    now = datetime.utcnow()
    for co in ordered:
        try:
            conn.execute(
                """
                INSERT INTO companies
                    (ticker, cik, company_name, sector, industry,
                     is_etf, is_fund, is_adr, is_active, universe_tier,
                     last_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (ticker) DO UPDATE SET
                    cik            = COALESCE(excluded.cik, companies.cik),
                    company_name   = excluded.company_name,
                    sector         = COALESCE(excluded.sector, companies.sector),
                    industry       = COALESCE(excluded.industry, companies.industry),
                    is_etf         = excluded.is_etf,
                    is_fund        = excluded.is_fund,
                    is_adr         = excluded.is_adr,
                    is_active      = excluded.is_active,
                    universe_tier  = excluded.universe_tier,
                    last_seen_at   = excluded.last_seen_at,
                    updated_at     = excluded.updated_at
                """,
                [
                    co["ticker"],
                    co.get("cik"),
                    co.get("company_name", co["ticker"]),
                    co.get("sector"),
                    co.get("industry"),
                    co.get("is_etf", False),
                    co.get("is_fund", False),
                    co.get("is_adr", False),
                    co.get("is_active", True),
                    co.get("universe_tier", "extended"),
                    now,
                    now,
                ],
            )
        except Exception as exc:
            logger.warning("Failed to upsert %s: %s", co.get("ticker"), exc)

    # Reconcile: deactivate primary-tier rows no longer in the primary set
    current_primary = list(primary_meta.keys())
    if current_primary:
        placeholders = ", ".join("?" * len(current_primary))
        conn.execute(
            f"""
            UPDATE companies
            SET is_active = false
            WHERE universe_tier = 'primary'
              AND ticker NOT IN ({placeholders})
            """,
            current_primary,
        )

    count = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE is_active = true"
    ).fetchone()[0]
    logger.info(
        "Universe built: %d active (%d primary, %d extended slots used)",
        count,
        len(current_primary),
        max(0, count - len(current_primary)),
    )
    return count
```

- [ ] **Step 2: Run all builder tests to verify they pass**

```bash
venv/bin/python -m pytest tests/test_universe_builder.py -v
```

Expected: 10 passed (2 loader tests + 8 behaviour tests)

- [ ] **Step 3: Run full test suite to verify no regressions**

```bash
venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -10
```

Expected: all previously-passing tests still pass (834+)

- [ ] **Step 4: Commit**

```bash
git add universe/universe_builder.py
git commit -m "feat: load S&P 500 YAML into primary tier, populate sector/industry, reconcile deactivations"
```

---

### Task 5: Update `config/universe.yaml`

**Files:**
- Modify: `config/universe.yaml`

- [ ] **Step 1: Bump max_symbols**

Replace the entire file with:

```yaml
# Primary tier: all tickers in universe/sp500_tickers.yaml + fallback_tickers below.
# Primary tickers are never capped by max_symbols.
#
# max_symbols caps only the extended tier (SEC-filtered fillers).
# Set high enough to leave a few extended slots after ~503 S&P primaries.
max_symbols: 520
min_price: 2.0
exclude_etfs: true
exclude_funds: true
exclude_adrs: false
exchanges:
  - NYSE
  - NASDAQ
  - AMEX

# Additional primary tickers beyond the S&P 500 YAML.
# These receive universe_tier="primary" and bypass the max_symbols cap.
fallback_tickers:
  - AAPL
  - NVDA
  - TSLA
  - JPM
  - UBER
  - RKLB
```

- [ ] **Step 2: Run full test suite**

```bash
venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: all tests pass

- [ ] **Step 3: Commit**

```bash
git add config/universe.yaml
git commit -m "config: raise max_symbols to 520, document primary vs extended cap behaviour"
```

---

### Task 6: Add `data universe-stats` CLI command

**Files:**
- Modify: `main.py`

The existing `data` group is defined around line 570 in `main.py`. Find the `data` group and the `data_inventory` command. Add the new command immediately after `data_inventory`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_universe_builder.py`:

```python
def test_universe_stats_cli(conn, monkeypatch, tmp_path):
    """Smoke-test the universe-stats CLI: outputs four labelled counts."""
    import storage.config as _cfg_mod
    from click.testing import CliRunner
    from main import cli

    # Seed one primary company with sector, one without
    conn.execute(
        "INSERT INTO companies (ticker, company_name, universe_tier, sector, is_active) "
        "VALUES ('AAPL', 'Apple Inc', 'primary', 'Information Technology', true)"
    )
    conn.execute(
        "INSERT INTO companies (ticker, company_name, universe_tier, sector, is_active) "
        "VALUES ('MSFT', 'Microsoft Corp', 'primary', NULL, true)"
    )
    conn.close()

    db_path = str(tmp_path / "test.duckdb")
    import duckdb as _ddb
    tmp_conn = _ddb.connect(db_path)
    from storage.db import init_schema
    init_schema(tmp_conn)
    tmp_conn.execute(
        "INSERT INTO companies (ticker, company_name, universe_tier, sector, is_active) "
        "VALUES ('AAPL', 'Apple Inc', 'primary', 'Information Technology', true)"
    )
    tmp_conn.execute(
        "INSERT INTO companies (ticker, company_name, universe_tier, sector, is_active) "
        "VALUES ('MSFT', 'Microsoft Corp', 'primary', NULL, true)"
    )
    tmp_conn.close()

    monkeypatch.setattr(_cfg_mod, "load_engine_config", lambda: {"db_path": db_path})
    runner = CliRunner()
    result = runner.invoke(cli, ["data", "universe-stats"])
    assert result.exit_code == 0, result.output
    assert "Active companies" in result.output
    assert "Primary tier" in result.output
    assert "Distinct sectors" in result.output
    assert "Null sector" in result.output
```

- [ ] **Step 2: Run the new test to verify it fails**

```bash
venv/bin/python -m pytest tests/test_universe_builder.py::test_universe_stats_cli -v
```

Expected: FAIL with `Error: No such command 'universe-stats'`

- [ ] **Step 3: Add the command to `main.py`**

Find the `data` group in `main.py`. The `data_inventory` command ends with `conn.close()`. Immediately after that function, add:

```python
@data.command("universe-stats")
def data_universe_stats():
    """Show universe composition: active count, primary count, sector coverage."""
    cfg, conn = _engine_setup()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE is_active = true"
        ).fetchone()[0]
        primary = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE is_active = true AND universe_tier = 'primary'"
        ).fetchone()[0]
        sectors = conn.execute(
            "SELECT COUNT(DISTINCT sector) FROM companies "
            "WHERE is_active = true AND sector IS NOT NULL"
        ).fetchone()[0]
        null_sector = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE is_active = true AND sector IS NULL"
        ).fetchone()[0]
        click.echo(f"Active companies : {total}")
        click.echo(f"Primary tier     : {primary}")
        click.echo(f"Distinct sectors : {sectors}")
        click.echo(f"Null sector      : {null_sector}")
    finally:
        conn.close()
```

- [ ] **Step 4: Run the CLI test**

```bash
venv/bin/python -m pytest tests/test_universe_builder.py::test_universe_stats_cli -v
```

Expected: PASS

- [ ] **Step 5: Run full test suite**

```bash
venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_universe_builder.py
git commit -m "feat: add 'data universe-stats' CLI command"
```

---

### Task 7: Final verification

- [ ] **Step 1: Run full test suite**

```bash
venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -10
```

Expected: all tests pass (previously 834; now 834 + 11 new)

- [ ] **Step 2: Spot-check the live universe-stats command**

```bash
venv/bin/python main.py data universe-stats
```

Expected output (approximate — exact numbers depend on real data):
```
Active companies : 520
Primary tier     : 503
Distinct sectors : 11
Null sector      : 17
```

Primary tier ≥ 503, Distinct sectors ≥ 10 (GICS has 11), Null sector < 25.

- [ ] **Step 3: Commit final state**

All changes should already be committed. Run:

```bash
git log --oneline -7
```

Expected: last 5 commits cover YAML data, loader, builder changes, config, CLI command.

---

## Self-Review

### 1. Spec coverage

| Requirement | Task |
|---|---|
| `universe/sp500_tickers.yaml` with last_updated, source, tickers (ticker, company_name, sector, industry, cik) | Task 1 |
| Load YAML if present, merge with config fallback, dedupe, mark primary | Task 4 |
| Primary tickers never dropped by max_symbols | Task 4 (primary loop has no cap; max_symbols only in extended loop) |
| Reconciliation: removed primary tickers deactivated | Task 4 (reconciliation block after upserts) |
| Populate sector/industry from YAML | Task 4 (COALESCE upsert) |
| config/universe.yaml max_symbols bump | Task 5 |
| Tests: YAML primary, config fallback, SEC extended, max_symbols guard, sector/industry, dedup, removed=deactivated, dot tickers | Tasks 3 + 6 |
| CLI health output (active, primary, sector, null sector) | Task 6 |
| Full tests pass | Task 7 |
| No scoring changes | ✅ (no files in scoring/ or features/ touched) |
| No OpenAI calls | ✅ |
| No paid dependencies | ✅ (requests + PyYAML already in requirements) |
| Deterministic and reproducible | ✅ (YAML committed to repo) |

### 2. Placeholder scan

None found.

### 3. Type consistency

- `load_sp500_yaml(path: str | Path) -> list[dict]` — used in `universe_builder.py` as `load_sp500_yaml(_SP500_YAML)` ✅
- `primary_meta: dict[str, dict]` — keys are uppercase tickers, values are entry dicts from YAML ✅
- `current_primary: list[str]` — passed as positional args to DuckDB execute ✅
- `_cfg()` fixture returns `{"universe": {...}}` matching what `build_universe` expects ✅
