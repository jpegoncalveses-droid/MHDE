# Finding 6 — SWARMSUSDT repeat-prediction deep dive (2026-05-15)

**Investigation date:** 2026-05-15
**Mode:** read-only
**Trigger:** SWARMSUSDT appearing as a top prediction on the dashboard
on 2026-05-15 despite (a) being the canonical "Variant D should have
caught this" candidate from 2026-05-14, (b) currently being held as an
open position from 2026-05-14 entry, (c) sitting at roughly -27%
unrealized.

This report answers the three operator hypotheses head-on:

1. **Valid forecast for a different reason?** Raw model output is
   legitimate (prob=0.907 10d, 0.840 5d), but the operator-facing
   appearance as a "top prediction" reflects the **pre-filter** view.
2. **Filter engaged and SWARMSUSDT genuinely passes today's cohort?**
   No — Variant D engaged, fired the short_momentum rule, and
   excluded SWARMSUSDT from today's export.
3. **Filter regression (bug)?** No. The filter ran end-to-end correctly:
   model → predictions table → exclusion rule → export. SWARMSUSDT is
   in `crypto_signal_exclusions` for `export_date=2026-05-15` with
   `reason='short_momentum'` and `ret5=-0.4372`. The exported
   `predictions_2026-05-15.json` does not contain SWARMSUSDT.

**Verdict: VALID PREDICTION, FILTER WORKING AS DESIGNED.** The
dashboard "appearance as top prediction" is a UX artifact — the
Crypto Predictions tab reads `crypto_ml_predictions` directly (raw
model output) and does not reflect the post-filter export state. No
code fix needed for the filter. A separate UX improvement could
overlay exclusion markers on the dashboard predictions table.

---

## 1. Prediction details (raw model output)

`crypto_ml_predictions` for SWARMSUSDT, prediction_date=2026-05-14:

| Column | 10d model | 5d model |
|---|---|---|
| `symbol` | SWARMSUSDT | SWARMSUSDT |
| `prediction_date` | 2026-05-14 | 2026-05-14 |
| `model_id` | `crypto_10d_7760a3f6` | `crypto_5d_ac900cbf` |
| `horizon` | 10d | 5d |
| `predicted_probability` | **0.9065** | **0.8402** |
| `prediction_threshold` | 0.10 | 0.10 |
| `market_cap_bucket` | mid_alt | mid_alt |
| `actual_max_return` | NULL | NULL |
| `actual_hit` | NULL | NULL |
| `outcome_filled_at` | NULL | NULL |
| `predicted_at` | **NULL** | **NULL** |

`predicted_at` is NULL for both rows because the prediction was
written on 2026-05-15 00:30 UTC — **before** today's migration v11
(KI-154) landed and added the column. Tomorrow's 00:30 fire will be
the first to populate it.

The 10d model is the production export target (per `active_spec.json`
`phase_1b_winner.horizon_days = 10`). The 5d model is computed and
persisted but not exported to the engine.

---

## 2. ret5 computation (SWARMSUSDT, anchored at 2026-05-14)

`crypto_prices_daily` strip, 2026-05-09 → 2026-05-14:

| Date | Open | High | Low | Close | Volume |
|---|---|---|---|---|---|
| 2026-05-09 | 0.026752 | 0.026783 | 0.022264 | **0.024508** | 31.2M |
| 2026-05-10 | 0.024515 | 0.024652 | 0.022138 | 0.022205 | 12.7M |
| 2026-05-11 | 0.022209 | 0.022606 | 0.019552 | 0.019948 | 19.4M |
| 2026-05-12 | 0.019948 | 0.020473 | 0.018422 | 0.018685 | 17.6M |
| 2026-05-13 | 0.018685 | 0.020720 | 0.016673 | 0.016914 | 23.7M |
| 2026-05-14 | 0.016916 | 0.016992 | **0.012817** | **0.013794** | 42.6M |

```
ret5 = (close_2026-05-14 / close_2026-05-09) - 1
     = (0.013794 / 0.024508) - 1
     = -0.4372  (-43.72%)
```

Variant D / `crypto/ml/postparabolic_filter.py` Rule B (short_momentum)
threshold: `POSTPARABOLIC_RET5_THRESHOLD = -0.30` (per `crypto/config.py:45`).

`-0.4372 < -0.30` → **Rule B fires → SWARMSUSDT must be excluded.**

Notable: 2026-05-14 also touched an intraday low of 0.012817 (cumulative
-47.7% from 5/9) before closing at 0.013794. The decline accelerated
on the very day of the prediction, not just over the 5-day window.

---

## 3. Variant D filter engagement — end-to-end trace

### 3a. Filter code path

- `crypto/ml/postparabolic_filter.py:74` — `should_exclude(dd90, ret60, ret5)`
  returns `(True, reason_token)` if either rule fires.
- Called from `crypto/exports/write_daily_predictions.py` (per ADR-028).
- Threshold constants live in `crypto/config.py`:
  - `POSTPARABOLIC_DD90_THRESHOLD = -0.20` (Rule A)
  - `POSTPARABOLIC_RET60_THRESHOLD = 2.0` (Rule A)
  - `POSTPARABOLIC_RET5_THRESHOLD = -0.30` (Rule B)
- Reason tokens: `"post_parabolic"` / `"short_momentum"` /
  `"post_parabolic_and_short_momentum"`.

### 3b. crypto_signal_exclusions row for SWARMSUSDT (today's export)

| Column | Value |
|---|---|
| `export_date` | **2026-05-15** |
| `symbol` | SWARMSUSDT |
| `model_id` | `crypto_10d_7760a3f6` |
| `raw_probability` | 0.9065 (matches the predictions table) |
| `dd90` | -0.5920 |
| `ret60` | 0.9808 (below the +2.0 threshold → Rule A would not fire) |
| `ret5` | **-0.4372** (matches my computation exactly) |
| `reason` | **short_momentum** |
| `created_at` | 2026-05-15 00:40:44.852114 UTC |

Filter engaged, fired Rule B (short_momentum), recorded the exclusion
with the exact ret5 value. SWARMSUSDT does not appear in
`crypto_signal_exclusions` for the 5d model — consistent with the
export only consuming the 10d model.

### 3c. All exclusions on export_date=2026-05-15

| Symbol | Reason |
|---|---|
| NAORISUSDT | short_momentum |
| SKYAIUSDT | post_parabolic_and_short_momentum |
| **SWARMSUSDT** | **short_momentum** |
| ZEREBROUSDT | post_parabolic_and_short_momentum |

Four exclusions today, all from the post-parabolic / short-momentum
filter chain. SWARMSUSDT is in the expected company.

### 3d. Export file `predictions_2026-05-15.json`

```
export_date           : 2026-05-15
features_as_of_date   : 2026-05-14
horizon_days          : 10
n_predictions         : 44
SWARMSUSDT entries    : 0   ← FILTERED OUT
```

Top entries by probability:

| Rank | Symbol | Probability |
|---:|---|---:|
| 1 | TAGUSDT | 0.8956 |
| 2 | 4USDT | 0.8732 |
| 3 | UBUSDT | 0.8637 |
| 4 | FHEUSDT | 0.8516 |
| 5 | LABUSDT | 0.8349 |
| 6 | TSTUSDT | 0.8340 |
| 7 | RAVEUSDT | 0.7984 |
| 8 | BUSDT | 0.7861 |

SWARMSUSDT, despite raw prob=0.9065 (which would have been rank #1
before filter), is not present.

---

## 4. Engine state — did the engine try to re-enter SWARMSUSDT today?

Engine DB (`/home/jpcg/crypto-trading-engine/data/trading_engine.duckdb`,
read-only) positions for SWARMSUSDT:

| id | entry_date | entry_price | qty | current_state | updated_at | realized_pnl |
|---|---|---|---|---|---|---|
| 4a0bde69… | 2026-05-14 | 0.01698 | 85,010 | **entry_filled** | 2026-05-14 00:45:55 | NULL (still open) |
| fede835f… | 2026-05-13 | 0.01877 | 76,523 | exit_filled | 2026-05-13 11:05:36 | +$25.25 |

Most recent SWARMSUSDT event: `2026-05-14 00:45:55` (entry fill). **No
events on 2026-05-15** — the engine made no attempt to enter SWARMSUSDT
today, consistent with SWARMSUSDT not being in the export.

The 5/14 open position (qty 85,010 @ 0.01698) is the one currently
showing -27% unrealized (5/15 latest mark 0.01603 per the
`price_snapshots` audit in finding5).

---

## 5. Decision tree end-to-end

```
2026-05-15 00:30 UTC — mhde-crypto-predict.service
    ├── score_universe uses features as of 2026-05-14
    ├── 10d model: SWARMSUSDT prob=0.9065
    └── 5d  model: SWARMSUSDT prob=0.8402
    ↓
    Written to crypto_ml_predictions (BOTH horizons; raw model output)

2026-05-15 00:40 UTC — mhde-crypto-export-predictions.service
    ├── Read crypto_ml_predictions (10d only)
    ├── For each candidate symbol:
    │   ├── Load dd90, ret60, ret5 from crypto_ml_features
    │   └── postparabolic_filter.should_exclude(dd90, ret60, ret5)
    │       ↓
    │       SWARMSUSDT: dd90=-0.59, ret60=+0.98, ret5=-0.4372
    │       ├── Rule A (post-parabolic):
    │       │   dd90 < -0.20 ✓ AND ret60 > 2.0 ✗  → does NOT fire
    │       └── Rule B (short-momentum):
    │           ret5 < -0.30 ✓  → FIRES
    │       ↓
    │       INSERT INTO crypto_signal_exclusions
    │              (..., reason='short_momentum', ret5=-0.4372)
    │       OMIT from predictions_2026-05-15.json
    ↓
    predictions_2026-05-15.json (44 entries, no SWARMSUSDT)

2026-05-15 00:45 UTC — crypto-trading-engine entry phase
    ├── Read predictions_2026-05-15.json
    ├── SWARMSUSDT not in input → never evaluated
    ├── Defense in depth (would have fired if filter had passed):
    │   └── "1 position per symbol" rule would block —
    │       existing 5/14 entry_filled position blocks re-entry
    └── No order placed for SWARMSUSDT
    ↓
    Engine state: 5/14 position remains in entry_filled
    No 5/15 events for SWARMSUSDT

Dashboard (continuous):
    ├── Crypto Predictions tab reads crypto_ml_predictions DIRECTLY
    │   (raw model output, pre-filter)
    └── SWARMSUSDT prob=0.9065 appears as a top entry
        → operator sees "top prediction" framing
        → does NOT reflect the export-side exclusion
```

---

## 6. Verdict and recommendation

### Verdict

**VALID PREDICTION (raw) + FILTER WORKING AS DESIGNED (export).** The
operator's three hypotheses resolve as:

| Hypothesis | Resolution |
|---|---|
| "Valid forecast for a different reason" | **Yes, raw.** The model produced prob=0.9065 honestly from the features; the technical setup it identified is a deep-pullback pattern. The model has not been retrained to suppress this class — that's what the filter is for. |
| "Filter engaged, SWARMSUSDT passes today's cohort" | **No.** Filter engaged. SWARMSUSDT does NOT pass. Excluded with `reason='short_momentum'`, `ret5=-0.4372`. Not in export. |
| "Filter regression (bug)" | **No.** Filter ran end-to-end correctly. `crypto_signal_exclusions` row exists with the exact computed ret5; export file omits SWARMSUSDT; engine made no entry attempt today. |

### Recommendation

**No code fix needed.** The crypto execution path is healthy: filter
engaged, exclusion logged, export clean, engine respected the export.
The fix-post-parabolic-filter + Variant D ship from 2026-05-11 → 14
worked exactly as intended for SWARMSUSDT today.

**Optional UX improvement (separate scope):** the dashboard Crypto
Predictions tab could read `crypto_signal_exclusions` for the
selected trading date and visually mark excluded rows ("EXCLUDED:
short_momentum" badge with the rule name). This eliminates the
operator-visible mismatch between dashboard "top predictions" and
actual engine input. Not urgent — the data exists; the UX gap is the
absence of an overlay, not a missing column.

**Defense-in-depth gap to file later:** if Variant D's filter ever
mis-fires (e.g. a code regression that returns `(False, None)` when
it should exclude), the only thing standing between the model output
and a duplicate-position entry attempt is the engine's "1 position
per symbol" rule. That rule blocks the re-entry, but does not log
loudly enough for the operator to notice that the upstream filter
failed. Worth filing as a separate KI for monitor-side alerting,
but not in scope for this finding.

---

## 7. References

- `crypto/ml/postparabolic_filter.py:74` — `should_exclude(dd90, ret60, ret5)`
  implementation and rule definitions.
- `crypto/config.py:43-45` — threshold constants (`-0.20`, `2.0`, `-0.30`).
- `crypto/exports/write_daily_predictions.py:208` — production call site.
- `crypto/schema.py:251` — `crypto_signal_exclusions` table schema.
- `data/exports/predictions_2026-05-15.json` — today's export (44 entries,
  SWARMSUSDT absent).
- `data/exports/active_spec.json` — `phase_1b_winner.exit_policy = 'D'`,
  `horizon_days = 10`.
- Engine DB `/home/jpcg/crypto-trading-engine/data/trading_engine.duckdb`
  positions table — SWARMSUSDT 5/14 entry, no 5/15 activity.
- `.claude/local_scripts/finding6_swarmsusdt_audit.py` — read-only audit
  script that produced sections 1–4.
- Related: `data/processed/finding5_pipeline_gap_and_t2_alignment.md`
  (T-2 / freshness investigation from earlier today; orthogonal but
  also touches SWARMSUSDT's open position).
