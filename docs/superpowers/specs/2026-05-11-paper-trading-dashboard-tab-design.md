# Paper Trading dashboard tab (Gap 3) — Design

**Date:** 2026-05-11
**Branch:** `gap3-paper-trading-dashboard-tab`
**Status:** Approved by operator (2026-05-11), ready for TDD implementation.
**Related:** ADR-020 (MHDE may read the engine DuckDB read-only); Gap 2
(`monitoring/paper_trading_drift.py`); the three-gap observability plan
(`~/.claude/plans/operator-needs-three-interconnected-zazzy-brooks.md`) Gap 3.

## Why

The crypto-trading-engine runs paper trading in a separate repo; the
operator has no in-product view of open positions, recent closed positions,
or drift-monitor status — they'd have to SSH and query the engine DuckDB.
Gap 3 adds a "Paper Trading" tab to the existing Streamlit dashboard that
surfaces this read-only.

## Where it goes

- **`dashboard/app.py`** — extend `st.tabs(["Equities", "Crypto", "FX"])`
  → add `"Paper Trading"`; add a `with tab_paper:` block at the end
  mirroring the existing `tab_crypto` structure (title → status banner →
  tables). Inline, ~80–120 lines — consistent with how app.py already does
  one tab per `with` block; no refactor of app.py.
- **`dashboard/services/queries.py`** — new functions, each opening the
  **engine** DuckDB read-only:
  - `_connect_engine()` — `duckdb.connect(os.environ.get("CRYPTO_ENGINE_DB_PATH", _DEFAULT_ENGINE_DB), read_only=True)`. `_DEFAULT_ENGINE_DB = "/home/jpcg/crypto-trading-engine/data/trading_engine.duckdb"`. Same env var as Gap 2.
  - `get_paper_open_positions(engine_conn, *, trail_pct, activation_pct) -> pd.DataFrame` — `positions` whose `current_state` is a live state (`entry_pending`, `entry_filled`, `trailing_active`, `exit_pending`). Columns: `symbol, state, entry_date, entry_price, qty, peak_price, calc_stop`. `calc_stop` = `peak_price - trail_pct*(peak_price - entry_price)` **only when the trailing stop is active** (`peak_price >= entry_price*(1+activation_pct)` and both prices non-NULL); otherwise the literal string `"— (not activated)"`. NULL `entry_price`/`qty` rows (the RECONCILE-001 phantom shape) are still returned, with those cells rendered `"—"`.
  - `get_paper_closed_trades(engine_conn, *, limit=30) -> pd.DataFrame` — `positions` where `current_state='exit_filled'`, ordered by `updated_at` desc, limited. Columns: `symbol, entry_date, entry_price, qty, peak_price, closed_at` (= `updated_at`), `close_reason` (best-effort: the most recent `reconcile_action.operator_reason` or terminal `state_change` for that position from `events`, else `""`), `exit_price` / `realized_pnl` → the literal string `"uncomputable (KI-136)"` (engine doesn't record market-exit fill prices).
  - `get_paper_failed_entries(engine_conn, *, limit=20) -> pd.DataFrame` — `positions` where `current_state='failed'`, last `limit`: `symbol, entry_date, reason` (from the position's events if present, else `""`).
  - `get_paper_engine_runs_summary(engine_conn) -> dict` — `{last_monitor_at, last_entry_at, n_open, n_closed_14d}` from `engine_runs` + `positions` (a thin convenience query; the drift banner already carries most of this in its `metrics`, so the tab can use either — see below).
  All transform logic (calc-stop, state pretty-print, per-row "uncomputable"
  strings, `close_reason` extraction) lives in these pure functions so it is
  unit-tested without Streamlit.
- `trail_pct` / `activation_pct` are read once in the tab from
  `data/exports/active_spec.json` (`phase_1b_winner.trail_pct = 0.30`,
  `activation_pct = 0.01`); if the file is missing/unparseable, fall back to
  those defaults with a `st.caption` noting it.

## Tab layout (top → bottom)

1. **Drift-monitor status banner** — 🟢 (ok/info) / 🟡 (warn) / 🔴
   (fail/critical) badge + the monitor's `title`; the full per-check list
   (`MonitorResult.body`) in a collapsed `st.expander`. Source: a cached
   wrapper around `monitoring.paper_trading_drift.run()` —
   `@st.cache_data(ttl=60) def _paper_drift_status() -> dict` returning
   `{status, severity, title, body, metrics}` (plain JSON-ish types, so it
   round-trips cleanly through `st.cache_data`). `run()` is read-only and
   has no Telegram side effect (that's only in `main()`). If `run()` raises
   (engine DB unreadable), the wrapper returns `{"status": "error",
   "title": "...", "body": str(exc)}` and the banner shows "drift monitor
   unavailable".
2. **Engine summary metrics** — `st.columns`: last `monitor`-phase run age,
   last `entry`-phase run (today / yesterday / "—"), # open positions,
   # closed in last 14d. Sourced from the cached drift `metrics` +
   `get_paper_engine_runs_summary` (one extra cheap query).
3. **Open positions** — `st.dataframe(get_paper_open_positions(...))`.
   Caption: "No live mark / unrealised P&L column — the engine does not
   populate `price_snapshots` yet (PRICE-SNAPSHOTS-001)."
4. **Recent closed positions** — `st.dataframe(get_paper_closed_trades(...))`.
   Caption: "Exit price / realised P&L show 'uncomputable' — the engine does
   not record market-exit fill prices yet (KI-136)."
5. **Rejected entries** (collapsed `st.expander`) —
   `st.dataframe(get_paper_failed_entries(...))`. Low priority, cheap.

## Error / availability handling

- If `_connect_engine()` raises (file missing, lock, corrupt), the **whole
  tab** renders a single `st.warning("Paper-trading engine database not
  available at <path> — is the crypto-trading-engine deployed?")` and stops
  there. It never lets the exception escape `with tab_paper:`, so the other
  tabs are unaffected. (Like app.py's `_open_conn` failure handling, but
  scoped to the tab rather than `st.stop()`.)
- Every per-row "uncomputable" / "—" value is a plain string; transforms
  never raise on NULL columns.

## Tests (TDD where it applies)

- **`tests/dashboard/test_paper_trading_queries.py`** — build a synthetic
  engine DuckDB in `tmp_path` (reuse the table-builder pattern from
  `tests/monitoring/test_paper_trading_drift.py`); assert, against injected
  connections:
  - `get_paper_open_positions` returns only live-state rows; `calc_stop`
    correct when activated, `"— (not activated)"` when peak below the
    activation threshold, `"—"` when `entry_price` is NULL; state strings
    pretty-printed.
  - `get_paper_closed_trades` orders by `closed_at` desc + respects `limit`;
    `exit_price`/`realized_pnl` are `"uncomputable (KI-136)"`; `close_reason`
    pulled from a `reconcile_action` event when present, `""` otherwise.
  - `get_paper_failed_entries` returns only `failed` rows + limit.
  - `get_paper_engine_runs_summary` returns the four keys with sane values
    (incl. the `last_entry_at = None` / `n_closed_14d = 0` empty cases).
  Pure-function tests; no Streamlit import.
- Extend **`.claude/local_scripts/test_dashboard_queries.py`** with calls to
  the four new query functions against the real engine DB
  (`CRYPTO_ENGINE_DB_PATH`), asserting they return without raising — run via
  the project dashboard-query smoke command, not pytest.
- The `with tab_paper:` Streamlit block itself is not unit-tested
  (consistent with the rest of `app.py`); covered by the smoke script + a
  manual `streamlit run` look and an import check.

## Docs

- `OPERATIONS.md` — a line under the dashboard section: the Paper Trading
  tab, that it reads `CRYPTO_ENGINE_DB_PATH` read-only, and what
  "uncomputable" cells mean (point at KI-136 / PRICE-SNAPSHOTS-001).
- `ARCHITECTURE.md` — one line in the dashboard description noting the tab +
  the read-only engine-DB read (cross-ref ADR-020).
- `SESSION_LOG.md` — Gap 3 entry.
- No new ADR (ADR-020 already authorises this read); no new KI (the tab
  surfaces KI-136 / PRICE-SNAPSHOTS-001 in-product rather than hiding them).

## Deliverable order

spec doc (commit) → `dashboard/services/queries.py` functions + their unit
tests (TDD: tests first) → `tab_paper` block in `dashboard/app.py` →
extend the local dashboard-query smoke script → docs → verify
(`pytest tests/dashboard/test_paper_trading_queries.py`, the dashboard query
smoke command, an import of `dashboard.app`, a manual `streamlit run` look)
→ **STOP for operator review** → on approval: push
`gap3-paper-trading-dashboard-tab`, open PR via `gh`; then squash-merge the
PR + `git checkout master && git pull && git log -3` here, and stop for the
operator's streamlit restart.

## Out of scope / deferred

- Current-price / unrealised-P&L column, P&L charts, hit-rate-over-time
  chart — all need data the engine doesn't persist yet (live marks, exit
  prices, `daily_pnl`); they land with the engine-data-recording follow-up,
  not here.
- No refactor of `dashboard/app.py`'s inline-per-tab structure.
- No write path to the engine DB — read-only only (ADR-020).
