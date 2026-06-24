# Brain discovery engine

The discovery/evaluation layer built on the existing brain substrate (read loop, raw
primitives, forward-only MFE/MAE labels). Package: `crypto/research/brain/discovery/`.
Everything here is a research engine: it discovers and SIMULATES; it never touches real
capital and never wires to an executor (the brain↔executor loop stays open by design).

## What it does (the two-stage shape, §2)

1. **Engineered primitives (§3, `engineered.py`)** — coin-relative features computed
   on-read, lookahead-free: per-coin z-score over each coin's strictly-prior trailing
   window, cross-universe mid-rank percentile (cross-sectional), and `.raw` passthrough
   only for bounded ratios. The raw store stays untouched.
2. **Rules (§4, `rules.py`)** — a rule is a conjunction of `feature <op> quantile-threshold`
   conditions over **distinct** features; generation is depth-extensible (`extend_rule`).
3. **Stage-1 scoring + null (§5/§6.1, `scoring.py`)** — each firing instance's label is its
   coin-centered risk-adjusted excursion (`mfe + mae` minus the coin baseline). The search
   keeps a candidate at a depth only if its edge beats the **permutation null at that depth**
   (re-run the same search on label-shuffled tape). This is what makes unbounded depth safe
   without a constant cap (§1) — depth is capped by the data, not a number.
4. **Rule store + state machine (§8.1/§8.3, `rulestore.py`)** — `discovered → confirming →
   promoted | rejected`, SQLite-WAL (`discovery.sqlite`).
5. **Forward confirmation (§6.2, `confirmation.py`)** — promote only on ≥ M **fresh
   post-discovery** instances whose edge stays positive, distinguishable from zero, past the
   null bar. The fresh filter (`window_start_ns > discovery_window`) is the un-gameable gate.
6. **Stage-2 exit discovery (§7, `exits.py`)** — round-trip simulator (vol-multiple
   target/stop, primitive-condition, time cap), scored vol-normalised under a sign-flip null.
7. **Trade log (§8.2, `tradelog.py`)** — simulated round trips for promoted rules.
8. **Batch runner + systemd (§9, `runner.py`)** — `crypto brain-discover-run`, wired
   `mhde-brain-discover.{service,timer}` **BUILT-NOT-DEPLOYED** (enabling is the deploy).
9. **Streamlit surface (§10, `dashboard/brain_discovery_app.py`)** — read-only, 5 levels.

## Parameter choices (§14 — all operator-tunable, in `discovery/config.py`)

| Param | Default | Why |
|-------|---------|-----|
| `ZSCORE_WINDOWS` | `(1440,)` (24h) | Stable z (1440 samples) over a full daily cycle; batch job tolerates the coarser responsiveness. A list so a shorter regime can be added. |
| `ZSCORE_MIN_HISTORY` | 60 | Guards a degenerate z off a tiny sample. |
| `QUANTILE_BINS` | 10 (deciles) | Fine-but-enumerable grid; equal support per threshold. |
| `SCORE_HORIZON_MIN` | 60 | Long enough for a microstructure edge in MFE/MAE, short enough to accumulate instances. |
| `N_PERMUTATIONS` | 200 | Resolves the ~99th pct of best-on-noise; **the dominant cost — size against measured host run-cost.** |
| `NULL_QUANTILE` | 0.95 | Controls the search's ghost rate at each depth (1.0 = strictest, used by tests). |
| `CONFIRM_M` | 30 | **Conservative default, explicitly NOT final** — depends on observed firing rates; the operator re-tunes after watching live firing for a week or two. |
| `CONFIRM_Z` | 2.0 | Fresh edge must be distinguishable from zero. |
| exit vol-multiples / time caps | fav (1,1.5,2,3)×vol, adv (0.5,1,1.5,2)×vol, caps (5,15,30,60) | Vol-multiples so a target/stop means the same across coins. |
| `MIN_FIRING_INSTANCES` | 20 | Below this an edge estimate is noise. |
| `MAX_DEPTH` | 4 | **Runaway SAFETY ceiling only**, not the design cap — the null caps depth (§1). |
| batch cadence | every 6h (`OnCalendar`) | Accumulate fresh instances between runs; keep host load modest. |
| engineered features | computed-on-read | Reproducible pure function of the raw store + params; no second forward-only store; params change without migration. |
| stores | SQLite-WAL `discovery.sqlite` | Mutable + concurrent dashboard reads; the registry's proven choice; avoids DuckDB single-writer. |

## For reviewer attention (§13)

- **(a) lookahead-freedom** in engineered primitives — z uses strictly-prior windows, xrank
  is cross-sectional. Pinned: appending a future window moves no earlier z.
- **(b) permutation-null correctness** — `test_null_rejects_pure_noise` (the most important
  test): a pure-noise tape promotes nothing; a planted signal survives.
- **(c) forward confirmation uses only post-discovery instances** — `fresh_instances` filters
  on `window_start_ns > discovery_window`.
- **(d) promotion does not wire to any executor** — only the simulated trade log; no
  `crypto.exports` / `crypto.execution` import in the package.

## Deliberate scoping decisions (documented, not omissions)

- The rule store tracks **null-survivors** (the meaningful set); the per-run funnel including
  how many *missed* is in `discovery_runs` (not millions of per-candidate rows).
- Conjunctions use **distinct features** (a clean first discretisation; cross-feature /
  multi-bound boxes are a later relaxation, kept in the representation's generality).
- The exit null is a **sign-flip (direction-randomised)** null — artifact-free across coins
  (no vol-mismatch blow-up); the entry null remains the rigorous bar.
- Primitive-condition exits are represented + simulated; the default exit grid is the
  barrier + time-cap combos (primitive exits enter when feature-carrying continuations are
  supplied).

## Honest expectation (§11)

Early on: huge candidate counts, almost everything dying at the null, slow accumulation in
`confirming`, few or no promotions. **That is correct** — the null is designed to kill the
overwhelming majority. The metric that matters is whether anything survives forward
confirmation and **holds**. If nothing promotes after weeks, or promoted rules don't hold
forward, that is a valid honest result (no durable edge in the searched space) — **not** a
reason to loosen any bar.

## Running it (not enabled by this PR)

```
# one batch pass (reads the brain store, writes discovery.sqlite)
venv/bin/python main.py crypto brain-discover-run

# the dashboard (read-only)
MHDE_DASHBOARD_AUTH_ENABLED=false venv/bin/python -m streamlit run dashboard/brain_discovery_app.py
```
