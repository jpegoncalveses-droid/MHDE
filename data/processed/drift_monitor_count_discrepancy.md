# Drift Monitor Count Discrepancy — Root Cause

_Investigation date: 2026-05-14. Read-only. Operator-triggered audit._

## Operator observation

Dashboard's paper-trading tab shows:

- `closed-trade win rate: insufficient sample (2/20 priced in last 14d)`
- `label hit rate: insufficient sample (0/20 settled in last 14d) (27 not settled yet)`

Operator believes more closed trades and settled outcomes should exist.
The "27 not settled yet" is plausible at face value, but "0 settled" is
suspicious given 28 closed positions exist in the engine DB.

## TL;DR

Both counts are **technically correct given the monitor's logic**, but
**misleading in their messages**. The root cause is the **strategy
baseline filter** in `monitoring/paper_trading_drift.py`. A new strategy
baseline was added to `config/monitoring.yaml` today (2026-05-14) for
the Variant D filter ship, and it clips the rolling window so
aggressively that:

1. **Check C (closed-trade win rate):** 25 of 27 priced closed trades
   (≈93%) are silently excluded as "pre-baseline". The
   `closed_trade_n_excluded_pre_baseline=25` metric IS tracked
   internally but **never surfaces in the operator-visible message**.
2. **Check D (label hit rate):** The baseline is so recent that
   `entry_lo > entry_hi` — the candidate-entry window is **inverted**
   (lo=2026-05-14, hi=2026-05-04). Zero trades can fall inside an
   inverted window. The 27 trades are then **all** attributed to "not
   settled yet" by the bucket logic, masking the real reason (excluded
   by baseline).

The bug is **observability**, not correctness of the underlying win-
rate / hit-rate math. The monitor isn't lying about its numerator and
denominator, but the human-readable message doesn't tell the operator
*why* the denominator is so small, so the operator interprets it as
"the engine hasn't run enough trades yet" when really "the baseline
filter just discarded most of them, by design."

## Verbatim queries

### Check C — `monitoring/paper_trading_drift.py:256-268`

```sql
SELECT p.entry_price, p.qty,
       SUM(o.price * o.qty) / NULLIF(SUM(o.qty), 0) AS sell_vwap,
       MAX(COALESCE(o.filled_at, p.updated_at))      AS exit_ts
FROM positions p
JOIN orders o
  ON o.position_id = p.id
 AND o.side = 'SELL' AND o.status = 'FILLED'
WHERE p.current_state = 'exit_filled' AND p.entry_price IS NOT NULL
GROUP BY p.id, p.entry_price, p.qty
```

Python-side filter chain (lines 274-291):

```python
if exit_ts < rolling_cutoff_ts:    # outside 14d window
    continue
if baseline_ts is not None and exit_ts < baseline_ts:
    n_excluded_pre_baseline += 1   # ← TRACKED IN METRICS BUT NOT IN MESSAGE
    continue
if entry_price is None or qty is None:
    continue
if sell_vwap is None:
    n_no_exit_price += 1
    continue
n += 1
# ... win-rate math
```

### Check D — `monitoring/paper_trading_drift.py:325-346`

```python
rolling_entry_lo = today - timedelta(days=ROLLING_WINDOW_DAYS + LABEL_SETTLE_DAYS)
entry_hi = today - timedelta(days=LABEL_SETTLE_DAYS)
baseline = _latest_baseline_date()
entry_lo = max(rolling_entry_lo, baseline) if baseline is not None else rolling_entry_lo

pos_rows = eng.execute(
    "SELECT symbol, entry_date FROM positions "
    "WHERE current_state = 'exit_filled' AND entry_price IS NOT NULL "
    "AND entry_date IS NOT NULL"
).fetchall()

candidates = [(s, d) for (s, d) in pos_rows if entry_lo <= d <= entry_hi]
n_unsettled = sum(1 for (_s, d) in pos_rows if d > entry_hi)
# Note: when entry_lo > entry_hi the candidates list is empty AND every
# position with d > entry_hi increments n_unsettled — including those
# that are actually pre-baseline.

# Label-row lookup on the (potentially empty) candidates
lab_rows = mhde.execute(
    "SELECT symbol, trade_date, label_10d_10pct FROM crypto_ml_labels "
    "WHERE trade_date BETWEEN ? AND ?",
    [entry_lo, entry_hi],
).fetchall()
```

## Actual counts from raw tables

Today is `2026-05-14`. Effective monitor parameters at run time:

| Constant | Value |
|---|---|
| `ROLLING_WINDOW_DAYS` | 14 |
| `LABEL_SETTLE_DAYS` | 10 |
| `MIN_CLOSED_FOR_HITRATE` | 20 |
| `rolling_cutoff_ts` (now − 14d) | 2026-04-30 22:08 UTC |
| `_latest_baseline_date()` | **2026-05-14** |
| `baseline_ts` | 2026-05-14 00:00 UTC |
| `effective_cutoff_ts` (max of two) | **2026-05-14 00:00 UTC** |
| `entry_lo = max(rolling_entry_lo, baseline)` | **2026-05-14** |
| `entry_hi = today - 10d` | **2026-05-04** |
| `entry_lo > entry_hi`? | **YES — window is inverted** |

### Engine `positions` table summary

| `current_state` | rows |
|---|---:|
| `entry_filled` | 4 |
| `exit_filled` | **28** |
| `failed` | 6 |

`exit_filled` entry_date range: `2026-05-10` → `2026-05-14`, 28 positions.

### Check C bucket counts (verbatim Python filter chain)

| Bucket | Count |
|---|---:|
| Joined `exit_filled` × SELL FILLED orders | 27 |
| `n_outside_window` (exit_ts < now − 14d) | 0 |
| `n_pre_baseline` (exit_ts < baseline_ts = 2026-05-14 00:00) | **25** |
| `n_no_exit_price` (sell_vwap NULL — KI-136) | 0 |
| **`n_priced` (counted toward win rate)** | **2** |

Both priced trades exited on 2026-05-14:

```
pos=287d0438-e77e-40e6-94b7-a1073a639802  sym=FHEUSDT  entry=2026-05-14  exit_ts=2026-05-14 02:25:54.593663  sell_vwap=0.02807
pos=761ad396-6021-4b4d-9a22-043b8d051c4e  sym=UBUSDT   entry=2026-05-14  exit_ts=2026-05-14 00:52:25.102312  sell_vwap=0.22358
```

The 25 missing trades all have `exit_ts < 2026-05-14 00:00` and are
**legitimately exit-priced** in the engine (`sell_vwap` is computable;
this is NOT a KI-136 case). They're discarded purely because of the
baseline cutoff. The dashboard message **does not say so**.

### Check D bucket counts

| Bucket | Count |
|---|---:|
| Total `exit_filled` with non-NULL `entry_date` | 27 |
| `candidates` (entry_date ∈ [`2026-05-14`, `2026-05-04`] — inverted!) | **0** |
| `n_unsettled` (entry_date > entry_hi = `2026-05-04`) | **27** |
| `n_before_window` (entry_date < entry_lo = `2026-05-14`) | 25 |
| `n_settled` (counted toward hit rate) | **0** |
| `n_no_label` | 0 |

Note: when the window is inverted, the `n_unsettled` and
`n_before_window` buckets are not mutually exclusive — a trade with
`entry_date = 2026-05-13` satisfies both `d > entry_hi` AND
`d < entry_lo`. The monitor reports `n_unsettled=27` to the operator
("27 not settled yet"); 25 of those 27 are actually
"excluded as pre-baseline", not "waiting for outcome".

### `crypto_ml_labels` coverage

Empty for the inverted window. The 27 trades' entry-dates
(2026-05-10 → 2026-05-14) overlap with the standard 10-day settlement
window, so there WOULD be label coverage for the older entries if the
baseline didn't clip them out. The label table is healthy; it's just
never queried for these (symbol, entry_date) pairs.

## Specific trades the monitor should arguably count

Of the 27 `exit_filled` × SELL FILLED positions, the 25 marked
`n_pre_baseline` are:

- Real, executed trades with computable `sell_vwap` (the engine DID
  record exit fill prices for these — they are NOT KI-136 victims).
- Within the 14-day rolling window (exit_ts > 2026-04-30).
- Excluded ONLY by `baseline_ts = 2026-05-14 00:00`.

The 2 trades currently counted are FHEUSDT (pos=287d0438…) and UBUSDT
(pos=761ad396…), both entered and exited today.

For Check D, all 27 closed trades have `entry_date ≤ 2026-05-04` =
`entry_hi` only **after the inverted window is fixed**. With the
baseline-clipped `entry_lo = 2026-05-14`, none of them fit. The
inverted window is the immediate cause of "0 settled".

## Root cause hypothesis

The bug is **two-layered**:

1. **Configured-baseline-too-recent (operational).** `config/monitoring.yaml`
   added a strategy_baseline entry for 2026-05-14 with reason
   *"Variant D filter shipped: added ret5 < -0.30 short-window
   momentum exclusion."* This baseline was added with the intent
   "trades from this date forward are the new regime; older trades
   used the prior strategy." Semantically valid. But because the
   baseline date is **today**, the rolling 14d window for win-rate is
   effectively a rolling **0d** window (it floors at today midnight),
   and the label-hit-rate window inverts entirely.

2. **Message hides the exclusion (observability).** The monitor's
   internal metrics already capture the exclusion
   (`closed_trade_n_excluded_pre_baseline=25` is in `metrics`), but
   the human-readable findings line says only
   `"closed-trade win rate: insufficient sample (2/20 priced in last 14d)"`
   — no mention of the 25 excluded trades. Same for Check D: the
   "27 not settled yet" message attributes the count to time-not-yet-
   elapsed when 25 of those 27 are actually pre-baseline.

The operational layer (1) is the *trigger*: on a normal day, the
baseline is older, the 14d window is genuine, and Check C/D look
reasonable. The observability layer (2) is the *bug class* — the
monitor will misrepresent its denominators every time a new baseline
is shipped, for at least 14 days (Check C) and 24 days (Check D)
after the baseline date.

## Recommended fix (not implementing)

Two-part fix, both in `monitoring/paper_trading_drift.py`:

### Part A — surface the pre-baseline exclusion in the message

The metric is already tracked
(`metrics["closed_trade_n_excluded_pre_baseline"]`). Append a clause
to the Check C "insufficient sample" branch (line ≈303-306):

```python
extras = []
if n_no_exit_price:
    extras.append(f"{n_no_exit_price} closed but exit price not recorded (KI-136)")
if n_excluded_pre_baseline:
    extras.append(
        f"{n_excluded_pre_baseline} excluded as pre-baseline "
        f"({baseline.isoformat()})"
    )
suffix = f"; {'; '.join(extras)}" if extras else ""
return [_Finding("ok",
    f"closed-trade win rate: insufficient sample "
    f"({n}/{MIN_CLOSED_FOR_HITRATE} priced in last {ROLLING_WINDOW_DAYS}d{suffix})")]
```

The operator can then read "2/20 priced; 25 excluded as pre-baseline
(2026-05-14)" and immediately understand the gap.

### Part B — fix Check D's inverted-window attribution

When `entry_lo > entry_hi`, the current code reports
`n_unsettled = sum(d > entry_hi)`, which double-counts pre-baseline
trades as "not settled yet". Split the bucket:

```python
candidates = [(s, d) for (s, d) in pos_rows if entry_lo <= d <= entry_hi]
n_unsettled    = sum(1 for (_s, d) in pos_rows if d > entry_hi)
n_pre_baseline = sum(1 for (_s, d) in pos_rows
                     if baseline is not None and d < baseline)
# Avoid double-counting: a trade is unsettled OR pre-baseline, not both.
# When baseline > today - LABEL_SETTLE_DAYS, EVERY post-baseline trade is
# necessarily unsettled (label hasn't had time to settle yet); when
# baseline < today - LABEL_SETTLE_DAYS, the two buckets are disjoint.
n_unsettled_post_baseline = max(0, n_unsettled - n_pre_baseline)
```

Then update the "insufficient sample" message:

```python
extras = []
if n_unsettled_post_baseline:
    extras.append(f"{n_unsettled_post_baseline} not settled yet")
if n_pre_baseline:
    extras.append(f"{n_pre_baseline} excluded as pre-baseline ({baseline.isoformat()})")
if n_no_label:
    extras.append(f"{n_no_label} with no label row")
```

### Optional Part C — config sanity-check (low priority)

Add a log warning (not a blocking alert) when
`baseline > today - LABEL_SETTLE_DAYS`, so the operator sees on the
next monitor run that the new baseline will produce inverted windows
until enough calendar days pass. One-time noise; useful for future
baseline ships.

### What this fix does NOT do

- It does not change the win-rate / hit-rate **math**. Both numerators
  and denominators stay correct.
- It does not change the **alerting** behavior (both checks are
  already gated by `MIN_CLOSED_FOR_HITRATE = 20`; alerts won't fire
  on small samples regardless).
- It does not change the **baseline mechanism** itself. The decision
  to floor at the most-recent baseline date is intentional and lives
  in `config/monitoring.yaml` with documented reason.

The fix is purely observability: the operator can read the dashboard
and understand that "27 not settled / 0 settled" means "25 pre-baseline
+ 2 just-entered today, label window hasn't opened" rather than "the
engine isn't running trades."

## References

- `monitoring/paper_trading_drift.py:233-316` — Check C
- `monitoring/paper_trading_drift.py:320-384` — Check D
- `monitoring/paper_trading_drift.py:67-93` — `_latest_baseline_date`
- `config/monitoring.yaml` — `paper_trading_drift.strategy_baselines`
  (most recent entry: 2026-05-14 / Variant D filter)
- KI-136 — separate defect class: engine doesn't persist exit fill
  prices on market exits (orthogonal to this discrepancy; today's two
  priced trades DO have non-NULL `sell_vwap`)
- Investigation script (preserved):
  `.claude/local_scripts/investigate_drift_monitor_counts.py`
