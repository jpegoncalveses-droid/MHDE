# Post-Parabolic Exclusion Filter — Design Spec (Step 1: investigation only)

**Status:** proposed, awaiting operator review. No production code yet.
**Author:** Claude Code session, 2026-05-11.
**Related:** the documented structural bias — the crypto model re-emits buy signals on coins immediately after a parabolic crash (SKYAI: probabilities 0.72–0.88 across the crash window, confirmed on clean data; root cause = volatility-loving threshold label + momentum-lag features `return_60d` / `drawdown_from_90d_high`, see the SKYAI model diagnostic). This filter is a *risk gate* applied before order entry; it does not change the model.

---

## 1. Where the filter belongs — recommendation: **(b) MHDE prediction export**

Apply the filter inside `crypto/exports/write_daily_predictions.py::build_predictions`, immediately after Platt calibration (`cal`) and before the sort/rank step. The actual predicate lives in a new tiny module `crypto/ml/postparabolic_filter.py` so it is unit-testable and reusable.

Why (b) over the alternatives:

| Option | Verdict | Reason |
|---|---|---|
| **(a)** filter before writing `crypto_ml_predictions` | ✗ | Destroys the raw signal — violates "don't lose raw signal (diagnostics/backtest)". `crypto_ml_predictions` must keep the uncapped probabilities. |
| **(b)** filter at export | ✓ **chosen** | Single-repo (MHDE only). No file-interface change (the predictions JSON is already just a ranked list; we drop excluded symbols and renumber ranks — the engine's existing validation "ranks unique & consecutive from 1", "non-empty" still holds). `build_predictions` **already loads `crypto_ml_features` including `drawdown_from_90d_high` and `return_60d`** (both in `FEATURE_COLS`) — **zero new queries, zero schema change** for the read. `crypto_ml_predictions` (written by `score_universe`, untouched) keeps the raw signal. |
| **(c)** engine selection layer | ✗ for now | The file interface (`/home/jpcg/crypto-trading-engine/docs/INTERFACE.md` §3) exposes only `symbol`, `probability`, `rank`, `predicted_at` — **not** `drawdown_from_90d_high` / `return_60d`. Exposing them is a coordinated cross-repo change (allowed as backward-compatible optional fields per INTERFACE §6, but explicitly out of scope today). Also puts model-bias knowledge in the wrong repo. |

Scope note: the exporter scores the **10d** model only (that is what the engine consumes — `phase_1b_winner.horizon_days = 10`). The filter therefore affects the 10d export today. `crypto predict` / the `crypto_ml_predictions` table (5d + 10d) are not changed by this work; a 5d export, if ever added, reuses the same module.

## 2. Data availability

The MHDE→engine file interface (`predictions_YYYY-MM-DD.json`) does **not** carry `drawdown_from_90d_high` or `return_60d` — so option (c) would need an interface addition. For the chosen location (b), **nothing new is needed**: `build_predictions` already pulls the full `crypto_ml_features` row for every active-universe symbol on `export_date`, and both features are members of `FEATURE_COLS`. The filter reads them straight off `features_df`. (Definitions, from `crypto/ml/features.py`: `return_60d = close/close_60ago − 1`; `drawdown_from_90d_high = close/high_90d − 1`, i.e. ≤ 0, with 0 = at the 90-day high.)

## 3. Threshold pressure-test (historical scan, last 60 days)

Scan: every prediction row in the last 60 days joined to its feature row — 5 666 rows, 60 dates, 48 symbols (`crypto_ml_predictions ⋈ crypto_ml_features`). "Excluded" = `drawdown_from_90d_high < D AND return_60d > R`.

| Rule (dd90 < D, ret60 > R) | excluded rows | % of all preds | excluded hit-rate | excluded avg max-ret | excluded avg max-DD | retained hit-rate / avg max-DD |
|---|---|---|---|---|---|---|
| −0.15 / +1.5 | 105 | 1.9% | 62.7% | +97% | **−11.5%** | 34.5% / −4.1% |
| **−0.20 / +2.0** (proposed) | **46** (21 symbol-dates, 6 coins) | **0.8%** | 65.0% | +137% | **−25.3%** | 34.6% / −4.1% |
| −0.20 / +3.0 | 22 | 0.4% | 75.0% | +175% | −39.8% | 34.6% / −4.1% |
| −0.25 / +2.0 | 20 | 0.4% | 40.0% | +30% | −64.6% | 34.7% / −4.1% |
| −0.25 / +3.0 | 8 | 0.1% | 66.7% | +53% | −88.8% | 34.7% / −4.1% |

Symbols caught by −0.20/+2.0: SKYAI, LAB, RAVE, UB, SWARMS, NAORIS. **Impact on the daily top-6 (10d slice, 9 dates the live exporter has run):** never removed more than **2** of the top-6 on any day (mean 0.67/day); no day where the whole top-6 was removed. On the recent crash days: 05-07 removed SKYAI + UB, 05-08 SWARMS + UB, 05-09 SWARMS, 05-10 SKYAI — leaving 4–5 picks.

Read of the numbers: the excluded set is **high-conviction (avg prob ≈ 0.69) and the high-drawdown tail** — ~2–6× the retained-set drawdown. Its *hit rate is higher*, not lower (65% vs 35%) — which is the bias precisely: these coins do tag +10% (volatility-loving label), but they whipsaw you to −25%+ to do it. So this is not "the filter removes losers", it is "the filter removes a risk profile we don't want". Tightening dd90 to −0.25 starts catching genuinely-dead names (RAVE at dd90 ≈ −0.96), and the excluded hit rate collapses to 40% — i.e. −0.20 is the sweet spot that catches both the fresh post-parabolic (SKYAI ≈ −0.22..−0.26) and the deeply-broken-but-still-up-60d (RAVE).

**Recommended thresholds:** start at the proposed **`drawdown_from_90d_high < −0.20` AND `return_60d > 2.0`** — deliberately narrow (0.8% of signals, never wipes a day). Put `POSTPARABOLIC_DD90_MAX = -0.20` and `POSTPARABOLIC_RET60_MIN = 2.0` in `crypto/config.py`; widening to −0.15/+1.5 (still 1.9%, still the high-DD tail) is a one-line change once paper-trading evidence justifies it.

**Hard exclude vs probability haircut — recommend hard exclude.** The model's probability is *not wrong* (the coin really will tag +10%); it is optimising the wrong objective, so the right response is a binary risk gate, not a fudge factor. A haircut (e.g. `prob × 0.3`) (i) uses an arbitrary multiplier; (ii) interacts badly with the engine's top-N — a haircut coin can still make top-6 on a thin field, which is exactly the day you most want it gone; (iii) is hard to explain after the fact ("we entered a −25% post-parabolic because its haircut prob still won"); (iv) makes the calibration meaningless for that coin. Hard exclude is auditable and composes with the existing static `active_spec.universe.excluded` concept. If graceful degradation is ever wanted, the better lever is a cap on concurrent post-parabolic exposure, not a per-coin haircut.

**Same thresholds for 5d and 10d — yes.** `return_60d` / `drawdown_from_90d_high` are horizon-independent; the bias is too. The 5d label is *more* volatility-sensitive (shorter window to tag +10%), so 5d needs the gate at least as much. No horizon-specific tuning until evidence demands it. (Engine consumes 10d only today; identical logic if a 5d export is added.)

## 4. Edge cases

1. **Missing feature** (`drawdown_from_90d_high` or `return_60d` NULL/NaN — warmup symbol, brand-new listing): evaluate the predicate on the **raw** feature values *before* `build_predictions`' `fillna(medians)` step, and **if either is NULL/NaN → do not exclude** (fail-open; the model's other features still gate it). Log at DEBUG.
2. **Many/all of today's candidates excluded**: the exporter must **not crash**. It writes the file with whatever survives (possibly a short list, or — never observed in 60 days, max 2/day removed — an empty `predictions` array). An empty array trips the engine's existing "predictions non-empty" validation → engine skips entry that day + alerts (INTERFACE §5.3 behavior) — which is the *correct* outcome (don't trade if literally everything is post-parabolic). MHDE also fires its own notification in this case (it has the infra) so the operator knows it was the filter, not a pipeline failure. Log a WARNING with the full excluded list.
3. **Coin re-entering vs already past threshold**: the filter is **stateless / re-evaluated each day** — a coin that recovers (dd90 climbs back above −0.20, or ret60 decays below +2.0 as the 60-day window rolls) is eligible again next day; one that keeps falling stays excluded. No hysteresis/cooldown in v1 (YAGNI; the thresholds are the only state). Consequence: a coin hovering near a threshold can flicker in/out day-to-day — acceptable (the engine enters daily; a flicker is a genuinely marginal call), and the on/off transitions are visible in `crypto_signal_exclusions` (§5).
4. **Bad input data** (cf. the SKYAI ingestion incident): the filter is only as good as `crypto_ml_features` for `export_date`. Out of scope here (the ingestion fix + a proposed data-quality monitor address it) — noted so it isn't forgotten.

## 5. Observability

- **Log** — every excluded coin: `logger.warning("postparabolic_exclude symbol=%s export_date=%s model_id=%s drawdown_from_90d_high=%.3f return_60d=%.3f raw_probability=%.3f reason=%s", …)` in `build_predictions`. Lands in the `mhde-crypto-export-predictions.service` journal (`journalctl -u …`), alongside the existing export logs.
- **Persist (queryable)** — new table `crypto_signal_exclusions(export_date DATE, symbol VARCHAR, model_id VARCHAR, raw_probability DOUBLE, drawdown_from_90d_high DOUBLE, return_60d DOUBLE, reason VARCHAR, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(export_date, symbol))`, written by the export step (UPSERT, so a re-run is idempotent). Does **not** touch `crypto_ml_predictions`. One small `crypto/schema.py` + `DATABASE_SCHEMA.md` addition.
- **Dashboard** — the "Crypto predictions" tab gains a small `🚫 Post-parabolic exclusions (latest export)` expander reading `crypto_signal_exclusions` for the most recent `export_date` (symbol, raw prob, dd90, ret60, reason). ~25 LOC (`dashboard/app.py` + a query fn in `dashboard/services/queries.py`). Could slip to phase 2 — the log + table are sufficient for v1.
- **Optional phase-2, cross-repo** — add a backward-compatible `excluded_postparabolic: [{symbol, raw_probability, reason}]` array to `predictions_YYYY-MM-DD.json` (INTERFACE §6 permits new optional fields) so the engine can surface it too. Coordinate with the engine side first; not in scope today.

## 6. Implementation effort estimate (Step 2, separate session)

| Item | LOC | Notes |
|---|---|---|
| `crypto/ml/postparabolic_filter.py` | ~30 | one pure predicate `is_post_parabolic(dd90, ret60) -> (bool, reason\|None)` + a vectorized helper; NaN → `(False, None)` |
| `crypto/config.py` | ~3 | `POSTPARABOLIC_DD90_MAX = -0.20`, `POSTPARABOLIC_RET60_MIN = 2.0` |
| `crypto/exports/write_daily_predictions.py` | ~20 | apply filter after `cal`, before sort; collect exclusions; `build_predictions` returns `(payload, exclusions)`; `write()` persists exclusions + (unchanged) writes the file/symlink |
| `crypto/schema.py` + `DATABASE_SCHEMA.md` | ~18 | `crypto_signal_exclusions` table + `create_all_tables` + doc |
| dashboard expander | ~25 | `dashboard/app.py` + `dashboard/services/queries.py` — *phase-2-able* |
| tests | ~120 | `tests/crypto/test_postparabolic_filter.py` (predicate truth table; both conditions required; NaN→not-excluded) + extend the export tests (excluded symbols dropped & ranks renumbered; all-excluded → empty list, no crash; `crypto_signal_exclusions` row written) — ~8–10 tests |
| docs | ~30 | KNOWN_ISSUES (reference the structural-bias finding), DECISIONS (ADR for the gate + chosen thresholds), SESSION_LOG |

**Total ≈ 1 day.** No engine-repo work, no file-contract change. TDD throughout (per repo policy).

---

### Open decisions for the operator

1. **Thresholds:** ship at −0.20 / +2.0 (recommended), or start more aggressive at −0.15 / +1.5?
2. **Dashboard expander:** v1 or phase-2?
3. **Spec-doc home:** this file is at `crypto/ml/POSTPARABOLIC_FILTER_SPEC.md` (lives next to the code). The repo's other design docs live under `docs/superpowers/specs/YYYY-MM-DD-*-design.md` — move it there instead if you prefer that convention.
4. **Phase-2 `excluded_postparabolic` JSON field** — worth coordinating with the engine team, or skip?
