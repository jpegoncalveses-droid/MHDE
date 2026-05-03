# SEC CIK Validation and 404 Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two post-S&P-500-seed problems: (1) Wikipedia CIKs may differ from SEC's authoritative CIKs, causing 404s; (2) SEC companyfacts 404s flood the log at WARNING level and there's no concise summary. Also add a `--skip-ingestion` smoke option.

**Architecture:** A new `universe/cik_validator.py` module compares YAML CIKs against the SEC company_tickers.json map and returns corrected entries + a CSV report. `universe_builder.py` feeds the SEC raw list (already fetched) into the validator before building `primary_meta`, so corrected CIKs flow into the DB. In `ingest_sec.py`, 404 responses are demoted to DEBUG and counted; a single WARNING summary is emitted at the end of ingestion. A `--skip-ingestion` CLI flag lets the daily radar skip all data fetching, enabling fast scoring smoke tests. No scoring weights change.

**Tech Stack:** Python 3.11, DuckDB, requests, PyYAML, click, pytest

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `universe/cik_validator.py` | Create | `validate_cik_vs_sec(yaml_entries, sec_map) → (corrected, report_rows)` + `write_validation_report(rows, path)` |
| `universe/universe_builder.py` | Modify | Move SEC fetch before `primary_meta`; call validator; write CIK report |
| `ingestion/ingest_sec.py` | Modify | Demote 404 to DEBUG; add `_not_found_count`; emit single summary WARNING |
| `ingestion/orchestrator.py` | Modify | Short-circuit all ingestion when `cfg["ingestion"]["skip_all_ingestion"]` is set |
| `main.py` | Modify | Add `--skip-ingestion` flag to `run daily-radar` |
| `tests/test_cik_validator.py` | Create | 5 unit tests for validator correctness |
| `tests/test_universe_builder.py` | Modify | 2 integration tests: CIK corrected, missing-in-SEC keeps YAML CIK |
| `tests/test_ingest_sec.py` | Create | 3 tests: 404 no retry, summary warning, skip-ingestion flag |

---

### Task 1: Create `universe/cik_validator.py`

**Files:**
- Create: `universe/cik_validator.py`
- Create: `tests/test_cik_validator.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cik_validator.py`:

```python
from __future__ import annotations

import csv
import pytest


def test_yaml_cik_matches_sec():
    from universe.cik_validator import validate_cik_vs_sec
    yaml_entries = [{"ticker": "AAPL", "company_name": "Apple Inc", "cik": "0000320193"}]
    sec_map = {"AAPL": "0000320193"}
    corrected, report = validate_cik_vs_sec(yaml_entries, sec_map)
    assert corrected[0]["cik"] == "0000320193"
    assert report[0]["status"] == "matched"
    assert report[0]["chosen_cik"] == "0000320193"


def test_yaml_cik_corrected_from_sec():
    from universe.cik_validator import validate_cik_vs_sec
    yaml_entries = [{"ticker": "AAPL", "company_name": "Apple Inc", "cik": "9999999999"}]
    sec_map = {"AAPL": "0000320193"}
    corrected, report = validate_cik_vs_sec(yaml_entries, sec_map)
    assert corrected[0]["cik"] == "0000320193"
    assert report[0]["status"] == "corrected"
    assert report[0]["yaml_cik"] == "9999999999"
    assert report[0]["sec_cik"] == "0000320193"


def test_missing_in_sec_keeps_yaml_cik():
    from universe.cik_validator import validate_cik_vs_sec
    yaml_entries = [{"ticker": "BRK.B", "company_name": "Berkshire Hathaway", "cik": "0001067983"}]
    sec_map = {}
    corrected, report = validate_cik_vs_sec(yaml_entries, sec_map)
    assert corrected[0]["cik"] == "0001067983"
    assert report[0]["status"] == "missing_in_sec"
    assert report[0]["chosen_cik"] == "0001067983"


def test_yaml_no_cik_gets_sec_cik():
    from universe.cik_validator import validate_cik_vs_sec
    yaml_entries = [{"ticker": "MSFT", "company_name": "Microsoft Corp"}]
    sec_map = {"MSFT": "0000789019"}
    corrected, report = validate_cik_vs_sec(yaml_entries, sec_map)
    assert corrected[0]["cik"] == "0000789019"
    assert report[0]["status"] == "matched"
    assert report[0]["yaml_cik"] == ""


def test_write_validation_report(tmp_path):
    from universe.cik_validator import validate_cik_vs_sec, write_validation_report
    yaml_entries = [
        {"ticker": "AAPL", "company_name": "Apple Inc", "cik": "0000320193"},
        {"ticker": "GOOG", "company_name": "Alphabet", "cik": "WRONG"},
    ]
    sec_map = {"AAPL": "0000320193", "GOOG": "0001652044"}
    _, report = validate_cik_vs_sec(yaml_entries, sec_map)
    out = tmp_path / "report.csv"
    write_validation_report(report, out)
    with out.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["status"] == "matched"
    assert rows[1]["status"] == "corrected"
    assert set(rows[0].keys()) == {"ticker", "yaml_cik", "sec_cik", "chosen_cik", "status", "company_name"}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
venv/bin/python -m pytest tests/test_cik_validator.py -v
```

Expected: 5 FAILED with `ModuleNotFoundError: No module named 'universe.cik_validator'`

- [ ] **Step 3: Create `universe/cik_validator.py`**

```python
from __future__ import annotations

import csv
import logging
from pathlib import Path

logger = logging.getLogger("mhde.universe.cik_validator")


def validate_cik_vs_sec(
    yaml_entries: list[dict],
    sec_map: dict[str, str],
) -> tuple[list[dict], list[dict]]:
    """Compare YAML CIKs against SEC authoritative CIKs.

    Args:
        yaml_entries: Tickers loaded from universe/sp500_tickers.yaml.
        sec_map: {ticker: zero-padded-cik} from SEC company_tickers.json.

    Returns:
        (corrected_entries, report_rows) where corrected_entries has CIK
        replaced by the SEC value whenever the ticker is found in sec_map.

    Status values:
        matched        — ticker found in SEC, CIKs agree (or YAML had no CIK)
        corrected      — ticker found in SEC, CIK differs → SEC CIK used
        missing_in_sec — ticker not in SEC → YAML CIK preserved (may be empty)
    """
    corrected: list[dict] = []
    report: list[dict] = []

    for entry in yaml_entries:
        ticker = entry.get("ticker", "").upper()
        yaml_cik = entry.get("cik") or ""
        sec_cik = sec_map.get(ticker, "")

        if sec_cik:
            chosen_cik = sec_cik
            status = "corrected" if (yaml_cik and yaml_cik != sec_cik) else "matched"
        else:
            chosen_cik = yaml_cik
            status = "missing_in_sec"

        corrected.append({**entry, "cik": chosen_cik or None})
        report.append({
            "ticker": ticker,
            "yaml_cik": yaml_cik,
            "sec_cik": sec_cik,
            "chosen_cik": chosen_cik,
            "status": status,
            "company_name": entry.get("company_name", ""),
        })

    corrections = sum(1 for r in report if r["status"] == "corrected")
    missing = sum(1 for r in report if r["status"] == "missing_in_sec")
    if corrections:
        logger.info("CIK validation: corrected %d mismatches (YAML → SEC CIK)", corrections)
    if missing:
        logger.debug("CIK validation: %d tickers not in SEC company_tickers.json", missing)

    return corrected, report


def write_validation_report(rows: list[dict], path: str | Path) -> None:
    """Write CIK validation rows to a CSV file. Creates parent dirs as needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["ticker", "yaml_cik", "sec_cik", "chosen_cik", "status", "company_name"]
    with p.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("CIK validation report: %s (%d rows)", p, len(rows))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
venv/bin/python -m pytest tests/test_cik_validator.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add universe/cik_validator.py tests/test_cik_validator.py
git commit -m "feat: add universe/cik_validator.py with SEC CIK correction and CSV report"
```

---

### Task 2: Integrate CIK validator into `universe/universe_builder.py`

**Files:**
- Modify: `universe/universe_builder.py`
- Modify: `tests/test_universe_builder.py` (append 2 integration tests)

The current `build_universe` loads YAML entries, builds `primary_meta`, THEN fetches SEC. This task reorders so the SEC fetch happens first, enabling CIK validation before `primary_meta` is built.

- [ ] **Step 1: Write the failing integration tests**

Append to `tests/test_universe_builder.py` (after existing tests):

```python
# ---------------------------------------------------------------------------
# CIK validator integration tests (Task 2 makes these pass)
# ---------------------------------------------------------------------------

def test_builder_corrects_yaml_cik_from_sec(conn, monkeypatch):
    """Builder should store SEC CIK when YAML CIK mismatches."""
    from universe.universe_builder import build_universe

    yaml_entries = [{
        "ticker": "AAPL",
        "company_name": "Apple Inc",
        "sector": "Information Technology",
        "cik": "WRONG_CIK",
    }]
    sec_raw = [{"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc"}]

    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: yaml_entries)
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: sec_raw)
    monkeypatch.setattr("universe.universe_builder.write_validation_report", lambda rows, path: None)

    build_universe(conn, _cfg())

    row = conn.execute("SELECT cik FROM companies WHERE ticker = 'AAPL'").fetchone()
    assert row is not None
    assert row[0] == "0000320193"


def test_builder_keeps_yaml_cik_when_missing_in_sec(conn, monkeypatch):
    """Builder keeps YAML CIK when ticker not in SEC company_tickers.json."""
    from universe.universe_builder import build_universe

    yaml_entries = [{
        "ticker": "BRK.B",
        "company_name": "Berkshire Hathaway Inc",
        "sector": "Financials",
        "cik": "0001067983",
    }]
    # BRK.B not present in SEC raw list
    monkeypatch.setattr("universe.universe_builder.load_sp500_yaml", lambda p: yaml_entries)
    monkeypatch.setattr("universe.universe_builder.fetch_sec_company_tickers", lambda: [])
    monkeypatch.setattr("universe.universe_builder.write_validation_report", lambda rows, path: None)

    build_universe(conn, _cfg())

    row = conn.execute("SELECT cik FROM companies WHERE ticker = 'BRK.B'").fetchone()
    assert row is not None
    assert row[0] == "0001067983"
```

- [ ] **Step 2: Run the 2 new tests to verify they fail**

```bash
venv/bin/python -m pytest tests/test_universe_builder.py::test_builder_corrects_yaml_cik_from_sec tests/test_universe_builder.py::test_builder_keeps_yaml_cik_when_missing_in_sec -v
```

Expected: 2 FAILED — `build_universe` does not call `validate_cik_vs_sec` yet.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_universe_builder.py
git commit -m "test: add 2 failing CIK validator integration tests for universe_builder"
```

- [ ] **Step 4: Rewrite `universe/universe_builder.py`**

Replace the entire file with the following. Changes vs current version:
- Added import of `validate_cik_vs_sec` and `write_validation_report`
- Added `_VALIDATION_REPORT` path constant
- Moved `raw = fetch_sec_company_tickers()` to BEFORE `primary_meta` construction
- Added `sec_map` build + `validate_cik_vs_sec` call + report write
- Changed `primary_meta` construction to use `corrected_sp500` instead of `sp500_entries`

```python
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import duckdb

from universe.sec_company_tickers import fetch_sec_company_tickers
from universe.filters import filter_non_equities, classify_company
from universe.sp500_loader import load_sp500_yaml
from universe.cik_validator import validate_cik_vs_sec, write_validation_report

logger = logging.getLogger("mhde.universe")

_SP500_YAML = Path(__file__).parent / "sp500_tickers.yaml"
_VALIDATION_REPORT = Path("data/processed/sp500_cik_validation_report.csv")

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

    sp500_entries = load_sp500_yaml(_SP500_YAML)

    logger.warning(_WARNING)

    # Fetch SEC list first so we can validate and correct YAML CIKs
    raw = fetch_sec_company_tickers()
    if not raw:
        logger.warning("SEC fetch failed — using primary list only")
        raw = []

    # Validate YAML CIKs against SEC authoritative list
    sec_map: dict[str, str] = {co["ticker"]: co["cik"] for co in raw if co.get("cik")}
    corrected_sp500, report_rows = validate_cik_vs_sec(sp500_entries, sec_map)
    try:
        write_validation_report(report_rows, _VALIDATION_REPORT)
    except Exception as exc:
        logger.warning("Could not write CIK validation report: %s", exc)

    # Build primary_meta from CIK-corrected YAML entries
    primary_meta: dict[str, dict] = {}
    for e in corrected_sp500:
        t = e.get("ticker", "").upper()
        if t:
            primary_meta[t] = e
    for t in config_fallback:
        if t not in primary_meta:
            primary_meta[t] = {"ticker": t}

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
        co["sector"] = meta.get("sector")
        co["industry"] = meta.get("industry")
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
    else:
        conn.execute(
            "UPDATE companies SET is_active = false WHERE universe_tier = 'primary'"
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

- [ ] **Step 5: Run all universe builder tests**

```bash
venv/bin/python -m pytest tests/test_universe_builder.py -v 2>&1 | tail -20
```

Expected: **13 passed** (11 previous + 2 new CIK integration tests)

- [ ] **Step 6: Run full test suite**

```bash
venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: 847 passed (845 previous + 2 new)

- [ ] **Step 7: Commit**

```bash
git add universe/universe_builder.py tests/test_universe_builder.py
git commit -m "feat: validate and correct YAML CIKs from SEC company_tickers.json in build_universe"
```

---

### Task 3: Fix SEC 404 handling in `ingestion/ingest_sec.py`

**Files:**
- Modify: `ingestion/ingest_sec.py`
- Create: `tests/test_ingest_sec.py`

Current problem: every 404 logs `logger.warning("SEC %s -> HTTP %s", url, r.status_code)` with the full URL. With 503+ tickers, this floods the log. The fix: 404 → DEBUG, track count, single summary WARNING at end.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ingest_sec.py`:

```python
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import duckdb
import pytest

from storage.db import init_schema


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    init_schema(c)
    yield c
    c.close()


def _seed_company(conn, ticker, cik):
    conn.execute(
        "INSERT INTO companies (ticker, cik, company_name, is_active) VALUES (?, ?, ?, true)",
        [ticker, cik, f"{ticker} Corp"],
    )


def _make_404_response():
    resp = MagicMock()
    resp.status_code = 404
    return resp


def _make_200_response(data: dict):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = data
    return resp


def test_404_companyfacts_no_retry(conn, monkeypatch):
    """A 404 on companyfacts must not be retried; total request count is bounded."""
    _seed_company(conn, "AAPL", "0000320193")
    call_urls: list[str] = []

    def mock_get(url, **kwargs):
        call_urls.append(url)
        return _make_404_response()

    monkeypatch.setattr("requests.get", mock_get)
    monkeypatch.setattr("time.sleep", lambda _: None)

    from ingestion.ingest_sec import SECIngestor
    ingestor = SECIngestor(cfg={})
    ingestor.ingest(conn, "run1", ["AAPL"])

    # submissions + companyfacts = at most 2 calls for one ticker (no retry)
    assert len(call_urls) <= 2
    # Each URL is called at most once
    assert len(call_urls) == len(set(call_urls))


def test_404_not_found_count_tracked(conn, monkeypatch):
    """_not_found_count is incremented for each 404 response."""
    _seed_company(conn, "AAPL", "0000320193")
    _seed_company(conn, "MSFT", "0000789019")

    monkeypatch.setattr("requests.get", lambda url, **kw: _make_404_response())
    monkeypatch.setattr("time.sleep", lambda _: None)

    from ingestion.ingest_sec import SECIngestor
    ingestor = SECIngestor(cfg={})
    ingestor.ingest(conn, "run1", ["AAPL", "MSFT"])

    assert ingestor._not_found_count > 0


def test_404_summary_warning_emitted(conn, monkeypatch, caplog):
    """After ingestion, a single WARNING summarising total 404s must be logged,
    not one WARNING per ticker."""
    _seed_company(conn, "AAPL", "0000320193")
    _seed_company(conn, "MSFT", "0000789019")

    monkeypatch.setattr("requests.get", lambda url, **kw: _make_404_response())
    monkeypatch.setattr("time.sleep", lambda _: None)

    from ingestion.ingest_sec import SECIngestor
    ingestor = SECIngestor(cfg={})

    with caplog.at_level(logging.WARNING, logger="mhde.ingestion.sec_edgar"):
        ingestor.ingest(conn, "run1", ["AAPL", "MSFT"])

    # Count WARNING messages that mention 404
    warning_404_msgs = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "404" in str(r.message)
    ]
    # Must be exactly 1 summary, not 2+ (one per ticker)
    assert len(warning_404_msgs) == 1
    # The summary message should include a count or "404"
    assert "404" in warning_404_msgs[0].message
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
venv/bin/python -m pytest tests/test_ingest_sec.py -v
```

Expected: 3 FAILED — `SECIngestor` has no `_not_found_count` attribute and still emits per-ticker WARNINGs.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_ingest_sec.py
git commit -m "test: add 3 failing SEC 404 handling tests"
```

- [ ] **Step 4: Modify `ingestion/ingest_sec.py`**

Make two targeted changes to the existing file:

**Change 1:** Replace the `_get` method (lines 71–80 in the current file):

OLD:
```python
    def _get(self, url: str) -> dict | None:
        time.sleep(_RATE_DELAY)
        try:
            r = requests.get(url, headers=self._headers(), timeout=30)
            if r.status_code == 200:
                return r.json()
            logger.warning("SEC %s -> HTTP %s", url, r.status_code)
        except Exception as exc:
            logger.warning("SEC fetch error: %s", exc)
        return None
```

NEW:
```python
    def _get(self, url: str) -> dict | None:
        time.sleep(_RATE_DELAY)
        try:
            r = requests.get(url, headers=self._headers(), timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                self._not_found_count += 1
                logger.debug("SEC 404: %s", url)
            else:
                logger.warning("SEC %s -> HTTP %s", url, r.status_code)
        except Exception as exc:
            logger.warning("SEC fetch error: %s", exc)
        return None
```

**Change 2:** In the `ingest` method, add `self._not_found_count = 0` as the very first line of the method body (after the `def ingest(self, conn, run_id, tickers):` line), and add a summary block just before the `self.log_run(...)` call at the end:

At start of `ingest` (add after the `def ingest(...)` line):
```python
        self._not_found_count = 0
```

Just before `self.log_run(conn, run_id, "filings+fundamentals", "ok", ...`:
```python
        if self._not_found_count:
            logger.warning(
                "SEC: %d requests returned 404 (CIK not found or no EDGAR filing)",
                self._not_found_count,
            )
```

The complete modified `ingest` method signature area and beginning:

```python
    def ingest(self, conn, run_id, tickers):
        self._not_found_count = 0
        started = datetime.utcnow()
        attempted = inserted = failed = 0
        skipped_filings = skipped_fundamentals = 0
        # ... rest of method unchanged until just before self.log_run ...
        if self._not_found_count:
            logger.warning(
                "SEC: %d requests returned 404 (CIK not found or no EDGAR filing)",
                self._not_found_count,
            )
        self.log_run(conn, run_id, "filings+fundamentals", "ok",
                     attempted, inserted, failed, started_at=started)
        self.logger.info("SEC: %d inserted, %d failed (of %d)", inserted, failed, attempted)
        return {"source": self.source_name, "status": "ok",
                "records": inserted, "failed": failed}
```

- [ ] **Step 5: Run the 3 new tests**

```bash
venv/bin/python -m pytest tests/test_ingest_sec.py -v
```

Expected: 3 passed

- [ ] **Step 6: Run full test suite**

```bash
venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: 850 passed

- [ ] **Step 7: Commit**

```bash
git add ingestion/ingest_sec.py tests/test_ingest_sec.py
git commit -m "fix: demote SEC 404 logs to DEBUG, add single summary WARNING per run"
```

---

### Task 4: Add `--skip-ingestion` smoke option

**Files:**
- Modify: `ingestion/orchestrator.py` (lines 40–52 — add early return)
- Modify: `main.py` (lines 108–127 — add `--skip-ingestion` flag)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ingest_sec.py` (append after the existing 3 tests):

```python
def test_skip_ingestion_flag_skips_all_sources(conn, monkeypatch):
    """When cfg['ingestion']['skip_all_ingestion'] is True, orchestrator returns
    immediately with sources_succeeded=0 and sources_skipped=N."""
    monkeypatch.setattr(
        "universe.universe_builder.build_universe", lambda conn, cfg: 5
    )

    from ingestion.orchestrator import run_all
    cfg = {
        "sources": {"sources": {}},
        "universe": {"max_symbols": 10, "fallback_tickers": []},
        "ingestion": {"skip_all_ingestion": True},
    }
    result = run_all(conn, cfg, tickers_override=["AAPL"])
    assert result["sources_succeeded"] == 0
    assert result["sources_skipped"] > 0
    assert result.get("skipped_reason") == "skip_all_ingestion"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
venv/bin/python -m pytest tests/test_ingest_sec.py::test_skip_ingestion_flag_skips_all_sources -v
```

Expected: FAIL — orchestrator does not check `skip_all_ingestion` yet.

- [ ] **Step 3: Add the early-return block to `ingestion/orchestrator.py`**

In `orchestrator.py`, the `run_all` function currently has this structure:

```python
def run_all(conn, cfg, target="all", dry_run=False, run_id=None, tickers_override=None):
    sources_cfg = cfg.get("sources", {}).get("sources", {})
    if not run_id:
        run_id = uuid.uuid4().hex[:16]

    # Build/refresh universe first
    logger.info("Building universe (run_id=%s)...", run_id)
    universe_count = build_universe(conn, cfg)
    logger.info("Universe: %d companies", universe_count)

    if tickers_override is not None:
        ...
```

Add the skip block AFTER the universe build (universe is always built, even in skip mode):

```python
    universe_count = build_universe(conn, cfg)
    logger.info("Universe: %d companies", universe_count)

    # NEW: skip all sources when requested (for scoring smoke tests)
    if cfg.get("ingestion", {}).get("skip_all_ingestion", False):
        logger.info("Skipping all ingestion (skip_all_ingestion=True)")
        return {
            "run_id": run_id,
            "universe_size": universe_count,
            "sources_succeeded": 0,
            "sources_failed": 0,
            "sources_skipped": len(_ALL_INGESTORS),
            "skipped_reason": "skip_all_ingestion",
            "results": {},
        }
```

- [ ] **Step 4: Add `--skip-ingestion` flag to `main.py`**

In `main.py`, find the `daily_radar` command (around line 108). The current signature is:

```python
@run.command("daily-radar")
@click.option("--max-symbols", default=None, type=int,
              help="Cap universe to N symbols for dev/test runs.")
@click.option("--skip-sec-fundamentals", is_flag=True,
              help="Skip XBRL fundamentals fetch (use cached data).")
@click.option("--incremental", is_flag=True, default=True,
              help="Skip sources with fresh data (default: on).")
def daily_radar(max_symbols, skip_sec_fundamentals, incremental):
```

Replace with:

```python
@run.command("daily-radar")
@click.option("--max-symbols", default=None, type=int,
              help="Cap universe to N symbols for dev/test runs.")
@click.option("--skip-sec-fundamentals", is_flag=True,
              help="Skip XBRL fundamentals fetch (use cached data).")
@click.option("--skip-ingestion", is_flag=True,
              help="Skip all data ingestion (score from cached data). Useful for smoke tests.")
@click.option("--incremental", is_flag=True, default=True,
              help="Skip sources with fresh data (default: on).")
def daily_radar(max_symbols, skip_sec_fundamentals, skip_ingestion, incremental):
    """Run the full daily opportunity discovery pipeline."""
    from pipelines.daily_radar import run as pipeline_run
    cfg, conn = _engine_setup()
    if max_symbols is not None:
        cfg.setdefault("universe", {})["max_symbols"] = max_symbols
    if skip_sec_fundamentals:
        cfg.setdefault("ingestion", {})["skip_sec_fundamentals"] = True
    if skip_ingestion:
        cfg.setdefault("ingestion", {})["skip_all_ingestion"] = True
    cfg.setdefault("ingestion", {})["incremental"] = incremental
    try:
        pipeline_run(cfg, conn)
    finally:
        conn.close()
```

- [ ] **Step 5: Run the skip-ingestion test**

```bash
venv/bin/python -m pytest tests/test_ingest_sec.py::test_skip_ingestion_flag_skips_all_sources -v
```

Expected: PASS

- [ ] **Step 6: Run full test suite**

```bash
venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: 851 passed

- [ ] **Step 7: Commit**

```bash
git add ingestion/orchestrator.py main.py tests/test_ingest_sec.py
git commit -m "feat: add --skip-ingestion flag to daily-radar for smoke testing"
```

---

### Task 5: Final verification

- [ ] **Step 1: Run full test suite**

```bash
venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: 851 passed (845 baseline + 5 CIK validator + 2 builder integration + 4 SEC ingestor)

- [ ] **Step 2: Verify the CIK validation report is generated**

```bash
venv/bin/python main.py data universe-stats
head -5 data/processed/sp500_cik_validation_report.csv 2>/dev/null || echo "Report not yet generated (run ingest to trigger)"
grep -c "corrected" data/processed/sp500_cik_validation_report.csv 2>/dev/null || true
```

The report is generated on each `build_universe` call. If the live DB already has the correct CIKs, most rows will be "matched".

- [ ] **Step 3: Smoke-test --skip-ingestion**

```bash
venv/bin/python main.py run daily-radar --skip-ingestion --max-symbols 5 2>&1 | grep -E "Skip|Ingestion|Stage|complete"
```

Expected: output shows ingestion stage completes immediately with sources_skipped count, no API calls.

- [ ] **Step 4: Final git log**

```bash
git log --oneline -8
```

Expected: 4 new commits covering validator, builder integration, SEC 404 fix, skip-ingestion.

---

## Self-Review

### 1. Spec coverage

| Requirement | Task |
|---|---|
| CIK validation: compare YAML vs SEC, prefer SEC | Task 1 (`validate_cik_vs_sec`), Task 2 (integration) |
| Artifact: `data/processed/sp500_cik_validation_report.csv` | Task 1 (`write_validation_report`), Task 2 (called from builder) |
| 404 non-fatal, no retry | Task 3 (`_get` returns `None` on 404, no retry loop existed — confirmed) |
| Suppress repeated per-ticker 404 WARNINGs | Task 3 (404 → DEBUG) |
| Concise 404 summary count at end | Task 3 (`_not_found_count` summary) |
| Smoke option: skip SEC ingestion | Task 4 (`--skip-ingestion`) |
| Tests: YAML CIK corrected | `test_yaml_cik_corrected_from_sec`, `test_builder_corrects_yaml_cik_from_sec` |
| Tests: missing SEC ticker keeps YAML CIK | `test_missing_in_sec_keeps_yaml_cik`, `test_builder_keeps_yaml_cik_when_missing_in_sec` |
| Tests: 404 skipped without retry | `test_404_companyfacts_no_retry` |
| Tests: warning summary generated | `test_404_summary_warning_emitted` |
| No scoring changes | ✅ no files in `scoring/` or `features/` touched |
| No OpenAI calls | ✅ |
| Full tests pass | Task 5 |

### 2. Placeholder scan

None found.

### 3. Type consistency

- `validate_cik_vs_sec(yaml_entries: list[dict], sec_map: dict[str, str]) -> tuple[list[dict], list[dict]]` — used in builder as `corrected_sp500, report_rows = validate_cik_vs_sec(sp500_entries, sec_map)` ✅
- `write_validation_report(rows: list[dict], path: str | Path) -> None` — called as `write_validation_report(report_rows, _VALIDATION_REPORT)` ✅
- `sec_map` built as `{co["ticker"]: co["cik"] for co in raw if co.get("cik")}` — values are CIK strings, matching `sec_map: dict[str, str]` ✅
- `_not_found_count` is an instance attribute (int), reset to 0 at start of `ingest()` ✅
- `skipped_reason` in orchestrator return dict — matched in test assertion ✅

### 4. Note on current 404 situation

The current `_get` has no retry loop — it calls once and returns None. The "no retry" requirement is already satisfied structurally. The test `test_404_companyfacts_no_retry` verifies the call count is bounded (≤ 2 for submissions + companyfacts). The main fix is suppressing the per-ticker WARNING.
