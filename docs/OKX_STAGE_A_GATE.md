# OKX Capture — Stage A reader-parity gate report

**Branch:** `feat/capture-okx-stage-a` · **Date:** 2026-07-05 · **Status:** GATE PASSED (verified live).

Stage A of the OKX migration: the **7 as-of REST collectors** (open_interest, premium_index,
global_ls_account, top_ls_account, top_ls_position, taker_ls_ratio, basis) **+ klines_1h** forward
maintenance, written to a **separate OKX capture root** in the same capture-core store schema so the
**brain's existing readers consume them unchanged**. Keyless public REST; never opens `mhde.duckdb` or the
engine DB; the live Binance capture is untouched. The 6 systemd units are **staged, not installed** — deploy
is operator-gated after merge.

## Gate definition & result

Gate = *live OKX polls into a scratch root → the brain's ACTUAL readers parse them → cursors advance on a
second poll.* Run live on 2026-07-05 into a fresh scratch root (`/tmp/okx_gate_*`), universe resolved to 273
OKX linear-USDT SWAP instruments, gate driven on a 6-symbol slice to stay rate-limit-safe (`0G, 1INCH, 2Z, A,
AAVE, ACH`). Every criterion passed with fresh evidence:

### 1. Tests — 45 passed / 0 failed
`test_capture_okx_{collector,series,symbols,systemd}.py` + `test_brain_klines_null_tolerance.py` → `45 passed`.

### 2. Live polls into a fresh scratch root
POLL 1 = 30 HTTP requests; all 7 as-of datasets written and parsed by the brain readers
(`crypto.research.brain.sources.ASOF_SOURCES`):

| dataset | rows (poll 1) |
|---|---|
| open_interest | 6 |
| premium_index | 6 |
| global_ls_account / top_ls_account / top_ls_position / taker_ls_ratio | 48 each (8-pt rubik window × 6) |
| basis | 6 |

### 3. Cursors advance on a SECOND poll
POLL 2 (30 reqs): **every** dataset advanced — rows `6→12` / `48→96`, `max(recv_ts_ns)` strictly increased on
all 7. Present-state series (OI/premium_index/basis) append a fresh observation each poll; the rubik series
re-deliver their window (a fresh `--once` process has no in-session dedup state) and the brain's `bucket_asof`
collapses duplicate venue-timestamps by venue time (tiebreak `asof_event_time_ms`), so no bias.

### 4. Watch items
- **Join pseudo-endpoints (real joined rows, sane values):** `premium_index` mark≈index (dev 0.00–0.22%),
  `next_funding_time` in the future; `basis` index & futures present, basis 0.05% / −0.21% / −0.04%.
- **LS freshness (KI-161 contrast):** all three OKX rubik LS series returned **fresh, advancing** data —
  venue-age **1–6 min** (min) to ~41 min (window tail), ratios sane (`long+short≈1.0`). OKX's LS is live,
  unlike the Binance analogues (`top_ls_account` dead / `top_ls_position` stale under KI-161).

### 5. klines-null decision — option 1 verified end-to-end
- Seed (`~90d`, 6 symbols): 12,954 closed bars, 132 requests (~22/symbol). Forward `--once`: 6 requests.
- `read_new_klines`: 12,984 bars; `trades=None`, `taker_buy_base=None`, `taker_buy_quote=None` on **12,984/12,984**.
- `KLINES_1H_SCHEMA`: `trades int64 nullable=True`, `takerBuyBase string nullable=True`, `takerBuyQuote string nullable=True`.
- `KLINES.bucket_fn` on real None-trades rows → buckets fine, **`trades=None` preserved**, no error.
- So `None` propagates **parse (`series.parse_candles`) → primitive write (nullable columns) → `bucket_asof` →
  reader** — not a reader-side cast. Honest NULLs (missing ≠ 0).

### 6. Request count vs OKX rate limits
- As-of cycle: **30 requests** at N=6 → **~1,098/cycle projected at N=273** (present-state/join series are O(1);
  rubik LS is 4×N per-symbol).
- Client pace `REQUEST_DELAY_S=0.15` (~6.7 req/s). Averaged over the **20-min** ratio cadence, ~1,098 req/cycle
  ≈ **0.9 req/s — trivial**. The only pressure is an intra-cycle burst on the per-endpoint rubik limit
  (~5 req/2s); the client's 429 backoff absorbs it. Present-state endpoints have ample headroom.

## Scope / not-in-this-gate
- 6 systemd units are staged and **uninstalled** (operator-gated deploy post-merge).
- Full-universe (273) `--once` and the full `~90d` seed were **not** run live here (rate-limit-safe 6-symbol
  slice used); the mechanism is proven and scales linearly, with 429 backoff covering the rubik burst.
- Reproduce: `PYTHONPATH=<worktree> venv/bin/python main.py crypto capture-okx-rest-run --once --root <scratch>`
  (+ `capture-okx-klines-seed`, `capture-okx-klines-run --once`).
