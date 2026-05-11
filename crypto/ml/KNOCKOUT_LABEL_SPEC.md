# Knockout (Triple-Barrier) Crypto Label — Design Spec (Step 1: investigation only)

**Status:** **APPROVED** by the operator 2026-05-11 — the three §6 open decisions are now locked: **TP=+10%, SL=−5%, horizon=10d (+ a 5d twin)**; **`neither`-hit → LOSS**; **same-bar tiebreak → SL-first (pessimistic)**. No production code yet — implementation (Step 2) is a separate task per the original brief.
**Author:** Claude Code session, 2026-05-11.
**Related:** KI-137 / ADR-021 (post-parabolic exclusion filter — the *symptom* guard). This spec is the *root-cause* fix: the current crypto label `label_Nd_10pct = (max forward daily close / close − 1) ≥ 0.10` is direction-agnostic and volatility-rewarding (a coin in freefall still "wins" because it tags +10% at some point), which is why post-parabolic names score 0.72–0.88. A knockout label scores what a trader cares about: **take-profit hit before stop-loss, within the horizon.**

---

## 0. The label

A trade entered at close `C` on date `T`: over the next `N` trading days (bars `T+1 … T+N`), walk forward bar by bar —

- if a bar's **intraday high** ≥ `C·(1+TP)` and that's the first barrier touched → **WIN** (`'tp'`);
- if a bar's **intraday low** ≤ `C·(1−SL)` first → **LOSS** (`'sl'`);
- if **both** in the same bar → tiebreak (see §2.1, recommend SL-first);
- if neither barrier is touched in `N` bars → **`'neither'`** → classified per §2.2 (recommend LOSS).

`label_Nd_knockout` (BOOLEAN) := outcome == `'tp'`. Reference price = the label-date **close** `C` (same as the current label; a trader's actual T+1-open entry differs slightly — noted, alternative available). Barriers use **intraday high/low** — the realistic fill basis for a TP/SL order — *not* the close, which is the basis the current label uses. TP/SL/`N` are config constants (`crypto/config.py`); retuning them requires a full label re-backfill (same as today).

## 1. Threshold pressure-test — full universe, last ~13 months (`crypto_prices_daily`, 50 symbols, 17.7k symbol-dates, 2025-04-06 → 2026-05-10)

TP fixed at +10%. SL-first (pessimistic) tiebreak. "win|resolved" excludes `neither`. Median resolve day shown.

| horizon | SL | n rows | win % (`tp`) | loss % (`sl`) | `neither` % | win\|resolved % | med win day | med loss day |
|---|---|---|---|---|---|---|---|---|
| 5d | −3% | 17,461 | 17.4% | 72.5% | 10.2% | 19.3% | 2 | 1 |
| 5d | −5% | 17,461 | 23.7% | 58.4% | 17.9% | 28.9% | 2 | 1 |
| 5d | −7% | 17,461 | 27.4% | 46.6% | 26.0% | 37.0% | 2 | 2 |
| 10d | −3% | 17,211 | 19.8% | 76.1% | 4.0% | 20.7% | 2 | 1 |
| **10d** | **−5%** | **17,211** | **28.3%** | **64.1%** | **7.6%** | **30.7%** | **3** | **2** |
| 10d | −7% | 17,211 | 34.0% | 54.5% | 11.5% | 38.4% | 3 | 2 |

(TP-first tiebreak shifts the win rate up ~3–5 pp, e.g. 10d/−5%: 28.3% → 31.8%; `neither` is unchanged.)

**vs the current close-based `label_Nd_10pct`** (base rates: `label_5d_10pct` 23.5%, `label_10d_10pct` 36.0%) — for each SL, of the current label's WINS, how many survive as knockout-wins vs flip to losses:

| | SL=−3% | SL=−5% | SL=−7% |
|---|---|---|---|
| **5d** current wins → still knockout-WIN | 52.2% | 70.2% | 80.2% |
| **5d** current wins → flip to knockout-LOSS | 47.8% | 29.8% | 19.8% |
| **10d** current wins → still knockout-WIN | 43.8% | **61.7%** | 73.0% |
| **10d** current wins → flip to knockout-LOSS | 56.2% | **38.3%** | 27.0% |
| (10d) current LOSSES that are knockout-WINS | 6.3% | 9.5% | 12.1% |

Reading: at 10d/−5%, **38% of the current label's "wins" are actually losses** under a realistic stop (they tag +10% only after — or while — bleeding through −5%); the knockout label purges them while keeping 62% of the genuine wins. Tighter (−3%) purges too aggressively (56% — a −3% stop is below typical crypto daily noise, so it's mostly noise-stopping, low signal); looser (−7%) keeps too many volatility-loving wins and a −7% realized loss is a big drawdown.

**SKYAI-class catch:** hypothetical entry at the 2026-05-10 SKYAI close (`C = 0.54185`); forward bar 2026-05-11 was O 0.54193 / H 0.55680 / **L 0.38260** / C 0.40527. The low pierces every SL level (−3% → 0.5256, −5% → 0.5148, −7% → 0.5039) on **day 1**, before the high (0.5568) ever reaches the +10% TP (0.5960). So **knockout = LOSS on day 1 for SL ∈ {−3%, −5%, −7%}, both horizons, both tiebreaks** — the post-parabolic entry is correctly classified as a loss. (For this *particular* entry the current label also says `False` — the forward closes never recover +10% — but the SKYAI class is the *crash-window* entries 05-07/08/09 where the current label said `True` at 0.72–0.88; those flip to knockout-losses too.) Net: a knockout-trained model would learn to down-rank exactly this profile organically — reducing reliance on the post-parabolic exclusion filter.

## 2. Edge cases (recommended handling)

1. **Same-bar high & low both touch their barriers (§2.1)** — you can't observe intra-bar order; assume **SL-first (pessimistic)** (`KNOCKOUT_SAME_BAR_SL_FIRST = True`). The conservative assumption avoids overstating signal quality — the exact failure mode we're fixing. Cost: ~3–5 pp lower measured win rate vs the optimistic read. (Runner-up: drop same-bar-ambiguous rows from training — cleaner statistics, loses data. Recommend SL-first.)
2. **Neither barrier hit by `N` days (§2.2)** — classify as **LOSS** (`KNOCKOUT_NEITHER_IS_LOSS = True`): a trade that goes nowhere ties up capital and is closed at the time stop; the conservative signal discourages picking sideways names. For 10d/−5% it's only 7.6 % of rows, so the choice is low-impact for the recommended config; for 5d it's 18–26 %, so a 5d model could reasonably use **exclude-from-training** instead. (Runner-up: exclude. Recommend LOSS uniformly for simplicity.)
3. **Day-1 gap through TP or SL** — no special handling: the bar-by-bar walk uses intraday high/low, so a gap-up open (≤ high) ≥ TP → WIN, a gap-down open (≥ low) ≤ SL → LOSS. A day-1 open that gaps *below* SL is still a LOSS (a trader would exit at the open, worse than SL — conservatively correct). A gap through *both* → §2.1 (SL-first).
4. **Missing OHLCV inside the forward window** — reuse the backtest harness's gap policy (`FORWARD_FILL_MAX_DAYS=2`, `DATA_GAP_EXIT_DAYS=3`): forward-fill gaps ≤ 2 days; on a gap ≥ 3 days, truncate the window at the gap and classify on the bars available (no barrier hit → `neither` → §2.2). A `(symbol, date)` without at least one forward bar is simply not labeled yet (the current label already does this — `WHERE close_1d IS NOT NULL`).
5. **Multiple entries on consecutive days for the same coin** — label each `(symbol, date)` **independently** (no dedup) — identical to the current label and to the backtest's "treat each prediction as an independent trade" stance. Duplicate-position avoidance is an *execution-layer* concern (the harness already handles it), not a labeling one.

## 3. Backward compatibility — keep both labels (recommend), additive schema

**Keep `label_Nd_10pct` alongside the new `label_Nd_knockout`** — do not replace. The current label is wired into trained models (`crypto_ml_model_runs.target_threshold`, the joblib bundle's `label_col`), `fill_outcomes` / `actual_hit`, the dashboard's "Crypto historical accuracy" panel, and `phase0_evaluate`; replacing it breaks all of those at once. Keeping it allows an A/B transition — train a knockout model, run it side-by-side, gate it on precision + calibration + a backtest verdict before flipping `is_active`. The `fwd_max_return_*` / `fwd_max_drawdown_*` columns stay regardless.

**Schema change (additive)** to `crypto_ml_labels`: `label_5d_knockout BOOLEAN`, `label_10d_knockout BOOLEAN`, plus diagnostics `knockout_outcome_5d VARCHAR` / `knockout_outcome_10d VARCHAR` (`'tp'|'sl'|'neither'`) and `knockout_resolve_day_5d INTEGER` / `knockout_resolve_day_10d INTEGER`. Idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS` migrations (the repo already does this for `crypto_ml_model_runs`). The TP/SL parameters live in `crypto/config.py`, not per-row (like the current label's implicit `0.10`).

## 4. Implementation outline (no code)

| Component | LOC | Notes |
|---|---|---|
| `crypto/ml/knockout_label.py` (new) | ~50 | pure `knockout_classify(forward_highs, forward_lows, entry_close, *, tp, sl, horizon, sl_first) -> (outcome, resolve_day)` + a vectorized helper — unit-testable in isolation, no DB |
| `crypto/ml/labels.py` glue | ~30 | `compute_labels` gains a knockout pass: load `crypto_prices_daily` per symbol, walk forward, UPDATE the new columns. (The current correlated-subquery SQL can't express "first touch"; a Python forward-walk is the natural fit.) |
| `crypto/config.py` | ~6 | `KNOCKOUT_TP = 0.10`, `KNOCKOUT_SL = 0.05`, `KNOCKOUT_HORIZONS = (5, 10)`, `KNOCKOUT_NEITHER_IS_LOSS = True`, `KNOCKOUT_SAME_BAR_SL_FIRST = True` |
| `crypto/schema.py` + migrations + `DATABASE_SCHEMA.md` | ~15 | the new columns |
| `crypto/ml/train.py` | ~30 | `--label-col` / `--label-kind` option (or a `crypto train --knockout` path); record `label_kind` + `knockout_tp`/`knockout_sl` in the joblib bundle and `crypto_ml_model_runs` |
| `crypto/ml/predict.py::fill_outcomes` | ~20 | knockout-aware `actual_hit` (TP-before-SL) when the active model is a knockout model — or a parallel `actual_knockout_hit` column |
| `crypto/ml/validation_gate.py` | ~15 | gate a knockout model against the *previous knockout model* (bootstrap-allowed for the first); cross-label comparison is meaningless |
| `dashboard/app.py` | ~10 | label-kind-aware caption on the historical-accuracy panel (arguably phase-2) |
| docs (ADR, KNOWN_ISSUES → close/reference KI-137 root cause, SESSION_LOG) | ~30 | |
| tests `tests/crypto/test_knockout_label.py` + extend `test_labels.py` | ~150 | classify truth table; same-bar tiebreak; neither→loss; gap-through; missing-bar truncation; backfill writes the columns; independent per-date labeling — ~12–15 tests |

**Backfill scope & runtime:** all `(symbol, trade_date)` in `crypto_prices_daily` with a forward bar — same scope as today's `crypto backfill-labels`. The forward-walk is ~50 symbols × ~700 days × ≤10 bars ≈ 350k bar-checks ≈ well under a second in pure Python; `backfill-labels` (already in the systemd chain, runs in ~1s) stays fast. Fold the knockout pass into `compute_labels` so the existing step covers it — no new pipeline stage, no systemd change.

**Downstream not affected:** features (label-independent); the prediction *inference* path and `write_daily_predictions.py` (they just score the active model — "probability" now means P(TP-before-SL), no code change); the `predictions_*.json` interface contract / engine (still `{symbol, probability, rank}`); the execution backtest harness (it replays *predictions* through *exit policies* — the training label is orthogonal; the `feat-backtest-postparabolic-toggle` filter still applies); Phase 0 calibration (label-agnostic — works once `fill_outcomes` populates the knockout outcome). **Total ≈ 2 days** (label + backfill ≈ ½ day; the rest = train/predict/gate wiring + tests + a side-by-side validation run). No engine-repo change, no interface change.

## 5. Promotion criteria for a retrained (knockout) model

1. **Walk-forward precision:** the knockout model's precision-at-emit-threshold, on its own (knockout) label, averaged over walk-forward folds, must clear an **absolute floor of ≥ 0.40** (well above the ~28% base rate at 10d/−5%) — *and* it cannot be compared to the current model's precision-on-the-old-label (apples-to-oranges).
2. **Calibration:** must pass the same Phase 0 calibration-bucket check the current model does (predicted P(TP-before-SL) ≈ realized rate within tolerance per bucket, `min_samples_per_bucket=10` guard) — a risk-aware label is only useful if its probabilities are calibrated.
3. **Backtest verdict (primary gate):** a paired execution backtest — knockout-model top-N picks vs current-model top-N picks, Phase-1B-winner config (Policy D, top_n n=6, trail 0.3), full walk-forward window, the post-parabolic filter toggle OFF — knockout must show **Sharpe ≥ current − 0.5**, **max-DD no worse**, and **cumulative return within ±10 %** of the current model. Bonus signal: fewer of the knockout model's daily top-N picks trip the post-parabolic `should_exclude` predicate (the label should organically down-rank them).
4. **Operator review gate:** the **first** knockout model bypasses the auto validation gate (no prior knockout model) and requires explicit operator sign-off on the precision/calibration/backtest evidence before `is_active` flips (manual promote). **Subsequent** knockout models go through the normal `validation_gate` (against the prior knockout model, hit-rate ≥ 0.9× per ADR-019).

## 6. Decisions (operator-approved 2026-05-11)

1. **TP/SL pair — DECIDED: TP=+10%, SL=−5%, horizon=10d, plus a 5d twin for parity.** −5% is the middle of the tested range — win rate 28.3%, `neither` only 7.6% (so the neither-rule barely matters), and it purges 38% of the current label's volatility-loving false wins while preserving 62% of genuine wins. −3% is too tight (56% purge — mostly noise-stopping below daily crypto vol); −7% is too loose (27% purge, large realized losses). 10d over 5d for the lower `neither` rate and the longer "be right" window. `KNOCKOUT_TP = 0.10`, `KNOCKOUT_SL = 0.05`, `KNOCKOUT_HORIZONS = (5, 10)`.
2. **`neither`-hit classification — DECIDED: LOSS** (`KNOCKOUT_NEITHER_IS_LOSS = True`, uniform for both horizons). Non-event = wasted capital; conservative training signal. Low-impact at 10d/−5% (7.6%).
3. **Same-bar tiebreak — DECIDED: SL-first / pessimistic** (`KNOCKOUT_SAME_BAR_SL_FIRST = True`). Can't observe intra-bar order; the conservative assumption avoids overstating signal quality. ~3–5 pp lower measured win rate than the optimistic read.

---

*Spec doc lives at `crypto/ml/KNOCKOUT_LABEL_SPEC.md` (next to the code, matching `POSTPARABOLIC_FILTER_SPEC.md`). The repo's other design docs are under `docs/superpowers/specs/YYYY-MM-DD-*-design.md` — move it there if you prefer that convention. Investigation script: `.claude/local_scripts/knockout_label_investigate.py`.*
