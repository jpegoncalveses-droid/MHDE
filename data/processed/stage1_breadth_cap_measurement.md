# Stage-1 breadth-cap cost measurement (read-only)

**Date:** 2026-08-10 · **Session:** breadth-cap measurement dispatch (post 3-PR hardening)
**Mandate:** quantify what a stage1 breadth cap would cost — survivor counts, lift
distributions, beam-coverage, per-depth memory — **no implementation, no cap**. Nothing in
production was modified; all runs were a standalone instrumented mirror of
`scoring.discover_entries` (same loading path as `runner.run_discovery`, same
`_scorable_firing`/`_fired_sum`/`_quantile` primitives, no discovery-DB writes), each under
its own cgroup cap (`MemorySwapMax=0`) so an overflow kills the measurement, never
capture/tick.

**Designed settings measured against:** `MAX_DEPTH=4`, `N_PERMUTATIONS=200`,
`NULL_QUANTILE=0.95`, `MIN_FIRING_INSTANCES=20`, `QUANTILE_BINS=10`,
`DISCOVERY_HISTORY_DAYS=14` (+2 lookback), horizon 60m long
(`crypto/research/brain/discovery/config.py:52-96`).

## TL;DR for the decision

0. **UPDATE (overnight run complete): the full designed-depth (MAX_DEPTH=4) pass was
   measured at TRUE SCALE end-to-end** — 8.55 M instances, funnel **3 → 4 → 772 → 31,760**,
   peak RSS **11.44 G**, wall 2.0 h, completed under a 13 G cap. The beam question is
   answered with true-scale numbers, not proxy numbers: **K=500 keeps 99.3% of ALL depth-4
   survivors** (100% of the top-100, 99.9% of the top-1k, 99.6% of the top-10k, 99.3% of
   total depth-4 lift mass). (Section 6b.)
1. **The full-universe funnel is NARROW at the top, not exploding.** Measured directly at
   true scale (896 symbols, 8.46 M instances, depths 1–2 complete): depth 1 passes
   **3 of 546** scorable; depth 2 passes **5 of 1,574** — and all five descend from the
   *same single* depth-1 parent (`forceorder.liq_buy_ratio.z1440>1.397`). At true scale a
   beam of even K=3 keeps 100% of depth-2 coverage. (Section 6.)
2. **The 2026-08-09 gate OOM decomposes into two walls, and neither is the depth loop
   anymore.** (a) The tape+lifts residency: 10.87 G at full scale — cap-independent,
   untouchable by any breadth cap. (b) A ~1.8 G *transient* in atom-bits prep
   (`_labeled_feature_columns` materializes all 35 feature columns at once) — building
   atom bits per-feature instead flattens it to zero spike (measured: RSS 10.87 → 10.95 G
   through prep). With that ~20-line change (candidate fix, NOT implemented), full-universe
   discovery ran depths 1–2 in 82 min, peak **11.17 G**, inside a 13 G box-safe cap. The
   post-PR#87 depth loop added ≤0.5 G even at a 1.08 M-candidate depth. (Section 5.)
3. **The survivor flood exists but is deep, flat, and redundant (300-symbol proxy,
   designed depth 4).** The null gate filters hard at depths 1–2 (pass ≲1%) and goes
   *permeable* at depth ≥3 (54%, then 46% pass) — selection-inflation through the depth
   structure. Depth 4 "passes" 262,168 candidates whose lift distribution is nearly flat
   (top-1% of survivors = 2.0% of total lift): near-duplicate variants, not distinct
   signal. (Sections 2–3.)
4. **THE DECIDING NUMBER: a top-K beam loses the tail, not the head — and at true scale
   K=500 loses almost nothing** (TL;DR-0 / Section 6b: 99.3% raw, 100% of the top-100).
   The 300-symbol proxy showed the same structure earlier and more harshly (raw 7.5% at
   K=100 yet 97% of the top-100 reachable): lift is heritable through extension — the best
   deep rules descend from the best parents — so a beam sheds redundant variants, not
   winners. (Sections 4, 6b.)
5. **Caveats.** The subset caveat resolved itself: the designed-depth run completed at
   TRUE scale (TL;DR-0), so no proxy stands between the numbers and the decision. The
   composition-sensitivity finding stands as a warning for future subset-based analysis
   (60 sym → dies at depth 1; 300 → cascades to 262 K; 450 → dies at depth 2; full →
   3 → 4 → 772 → 31,760). (Sections 6b–7.)
6. **Latent production hazard found (not fixed — read-only mandate):** `discover_entries`
   builds the next depth's extension pool *before* the `depth <= max_depth` loop condition
   can stop it (`scoring.py`, loop tail: "extend only the survivors (a small set)"). With a
   permeable depth-4 (262 K survivors × ~550 atoms ≈ 144 M rule objects) that final,
   never-scored extension build is itself an OOM — my md4 mirror died exactly there, *after*
   all scoring was flushed. The "a small set" design assumption is disproved at depth ≥ 3.
   Any future change should also skip the final-depth extension (2-line guard).

## 1. Measurement design

- Harness: `beam_measure.py` (scratchpad) — replicates `runner.run_discovery`'s exact
  loading (`_markprice_frontier_ns` → floors → `compute_engineered_columnar` →
  `compute_instance_lifts`, `runner.py:202-234`), then mirrors the stage1 depth loop with
  instrumentation: per-depth funnel + real-edge percentiles + RSS + wall-clock, and
  per-survivor `(canonical_id, edge, parent-set)` lineage streamed to JSONL, flushed per
  depth (an OOM at depth N preserves depths 1..N−1 — this paid off twice).
- Universe subsets read per-symbol through the same `read_snapshots_columnar` store API
  (symbol pushdown); 60 = curated liquid majors, 300/450 = evenly-spaced samples of the
  896 present symbol partitions, full = no filter.
- Full-scale runs used two harness-side deviations from production, both measurement-
  neutral: per-feature atom-bits build (kills the prep transient; identical bits) and a
  numpy null permutation (`BEAM_FAST_PERM=1`; the null needs only an exchangeable uniform
  shuffle — statistically identical bar, ~2× faster pass, draws not byte-equal to
  production's RNG stream).
- Beam coverage computed **adaptively**: keep top-K survivors by lift at depth d, generate
  depth d+1 candidates *only from kept*, keep top-K of those, etc. — what a real beam-K
  search does. Conservative: a real beam's candidate pool is a subset of the full pool, so
  its null bar (q95 of per-perm max over fewer candidates) is stochastically lower — every
  survivor counted reachable here would also pass the real beam's bar; true coverage ≥
  reported.

## 2. Per-depth funnel (survivor counts) — all runs

| run | universe | inst (M) | d1 cand→pass | d2 cand→pass | d3 cand→pass | d4 cand→pass | outcome |
|---|---|---|---|---|---|---|---|
| full-1 | 896 sym | 8.01 | — | — | — | — | **OOM in load** @12G (dict-path prep transient) |
| sub-60 | 60 majors | 0.57 | 548→**0** | — | — | — | complete; cascade dead at d1 |
| lad-300 | 300 spread | 2.75 | 548→3 | 1,588→11 | 5,641→**2,433** | — | complete (md3), peak 4.49G |
| lad-450 | 450 spread | 4.15 | 550→2 | 1,064→**0** | — | — | complete; cascade dead at d2 |
| md4-300 | 300 spread | 2.78 | 548→6 | 3,169→17 | 8,702→**2,818** | 1,084,097→**262,168** | all 4 depths measured; killed in the *never-scored* d5 extension build |
| **full-3** | **896 sym** | **8.46** | **548→3** | **1,587→5** | — | — | **complete @13G, peak 11.17G** (max_depth=2 by design) |
| **full-4** | **896 sym** | **8.55** | **548→3** | **1,587→4** | **2,057→772** | **291,230→31,760** | **complete @13G, peak 11.44G, 2.0 h — the designed-depth true-scale run** |

Null bars vs real edges (md4-300, the designed-depth proxy run):

| depth | n_scorable | null bar | real edge p50 | p99 | max | pass rate |
|---|---|---|---|---|---|---|
| 1 | 546 | 0.00080 | −0.000009 | 0.00081 | 0.00400 | 1.1% |
| 2 | 3,067 | 0.01123 | 0.00120 | 0.00813 | 0.02018 | 0.6% |
| 3 | 5,249 | 0.01455 | **0.01506** | 0.03692 | 0.04841 | **54%** |
| 4 | 563,780 | 0.01864 | 0.01795 | 0.04179 | 0.06619 | **46%** |

The null gate works as designed at depths 1–2 (pass ≲1%) and goes **permeable at depth ≥3**:
the *median* real candidate beats the bar. Mechanism: depth-d candidates exist only as
extensions of depth-(d−1) survivors *selected on the same data*, so their edges are
selection-inflated relative to a label-shuffled null over the whole pool. The depth-3/4
flood is multiple-testing leakage through the depth structure, not 262 K independent edges.

## 3. Survivor lift distributions (md4-300)

| depth | n | min | p50 | p90 | p99 | max | top-1% share of total lift |
|---|---|---|---|---|---|---|---|
| 1 | 6 | 0.00085 | 0.00129 | 0.00327 | 0.00392 | 0.00400 | 36.7% |
| 2 | 17 | 0.01165 | 0.01364 | 0.01976 | 0.02012 | 0.02018 | 8.0% |
| 3 | 2,818 | 0.01455 | 0.01942 | 0.02642 | 0.03933 | 0.04841 | 2.1% |
| 4 | 262,168 | 0.01864 | 0.02335 | 0.03321 | 0.04536 | 0.06619 | **2.0%** |

**Flat, not head-concentrated**: p99/p50 ≈ 1.9× at depths 3–4; the top 1% of survivors carry
only ~2% of total lift. No fat head of standout deep rules — a redundant near-duplicate
flood. Corroborated by parent structure: the 262 K depth-4 survivors descend from 2,768 of
2,818 depth-3 survivors (98%), links spread flat (top 32.5% of parents → only 50% of links).

## 4. THE DECIDING QUESTION — adaptive beam-K coverage (md4-300)

Fraction of *actual* depth-4 survivors still reachable if only the top-K survivors by lift
were kept at each depth (raw), coverage restricted to the TOP-X depth-4 survivors by lift
(what stage-2/confirmation actually consumes), and share of total depth-4 lift mass:

| K | raw d4 cov | of top-100 | of top-1k | of top-10k | d4 lift-mass reached |
|---|---|---|---|---|---|
| 50 | 3.5% | 84.0% | 80.8% | 65.3% | 5.8% |
| 100 | 7.5% | **97.0%** | **95.9%** | 86.6% | 11.6% |
| 200 | 15.6% | **100%** | **98.3%** | **94.2%** | 21.5% |
| 500 | 38.3% | 100% | 99.6% | **98.8%** | 45.6% |
| 1000 | 67.9% | 100% | 100% | 99.8% | 73.2% |
| 2000 | 97.9% | 100% | 100% | 100% | 98.3% |

(Depths 2–3 coverage is 100% at every K ≥ 50: their counts (17, 2,818) only make the beam
bite at the d3→d4 transition, and the kept heads regenerate the same top extensions.)

Reading: **the beam's loss is concentrated in exactly the flat redundant tail the null gate
already fails to filter.** K=200–500 keeps 94–99% of the top-10k deep survivors and 100% of
the top-100 while cutting the depth-4 candidate pool from 1.08 M to ~110–275 K.

## 5. Memory per depth — where the wall actually is

Measured RSS (GB):

| stage | sub-60 | lad-300 | lad-450 | full (true scale) |
|---|---|---|---|---|
| tape built | — | 2.67 | 4.09 | 6.78 |
| + lifts (load done) | 0.89 | 3.83 | 5.96 | **10.87** |
| + atom-bits prep peak | 1.01 | 4.12–4.32 (dict path) | 6.31–6.53 (dict path) | **10.95** (chunked — no spike) |
| depth-loop peak d1–d2 | 1.01 | 4.27–4.37 | 6.62 | **11.17** |
| depth-4 peak | — | 4.85 | — | **11.44** (full-4, complete) |

- Residency scales ~linearly with instances (≈1.3–1.7 kB/inst through the search phase).
  The **tape+lifts is the floor: 10.87 G at full scale, cap-independent** — no stage1
  breadth cap reduces it. Against the 22 G box with capture (~4 G) + tick (3 G) live,
  that leaves the search ~1–2 G of working room under a 13 G cap — which measured
  sufficient (peak 11.17 G through depth 2; the md4 proxy's 1.08 M-candidate depth added
  only +0.5 G).
- The full-1 OOM at a 12 G cap was the load residency (10.8 G) **plus the ~1.8 G transient
  from `_labeled_feature_columns` materializing all 35 feature columns at once** during
  atom-bits prep (`scoring.py`: `cols = _labeled_feature_columns(...)`). Building bits
  one feature at a time (one 8-byte column resident, ~68 MB) measured **zero spike**
  (10.87 → 10.95 G). This is the cheapest production unblock found: ~20 lines, oracle-
  preserving (identical bits), NOT implemented per the read-only mandate.
- The only true depth-loop OOM at designed settings is the **final-depth extension build**
  (TL;DR-6) — after all scoring, never scored, ~144 M rule objects at md4 scale.
- Wall-clock at true scale (full-3, nice 19 / idle-IO / CPUWeight=20 beside live capture):
  load 50.6 min; depth-1 null 18.8 min; depth-2 null 10.4 min; total 82 min.

## 6. Full universe, true scale, depths 1–2 (direct measurement, full-3)

Funnel: **548 → 546 scorable → 3 passed** (bar 0.000447) at depth 1;
**1,587 → 1,574 scorable → 5 passed** (bar 0.00729) at depth 2. Peak RSS 11.17 G.

The complete true-scale survivor set:

| depth | rule | edge | ×bar |
|---|---|---|---|
| 1 | `forceorder.liq_buy_ratio.z1440 > 1.397` | 0.00208 | 4.7× |
| 1 | `trades.trade_count.z1440 > 0.943` | 0.00055 | 1.2× |
| 1 | `trades.price_range.z1440 > 1.127` | 0.00045 | 1.0× |
| 2 | `bookticker.rel_spread.xrank > 0.901 AND liq_buy_ratio.z1440 > 1.397` | 0.01458 | 2.0× |
| 2 | `bookticker.rel_spread.raw > 0.00102 AND liq_buy_ratio.z1440 > 1.397` | 0.01379 | 1.9× |
| 2 | `forceorder.liq_buy_ratio.raw < 1 AND liq_buy_ratio.z1440 > 1.397` | 0.00816 | 1.1× |
| 2 | `bookticker.rel_spread.raw > 0.00083 AND liq_buy_ratio.z1440 > 1.397` | 0.00740 | 1.0× |
| 2 | `bookticker.rel_spread.xrank > 0.801 AND liq_buy_ratio.z1440 > 1.397` | 0.00731 | 1.0× |

Two structural facts, both favorable to a beam:

1. **All five depth-2 survivors descend from the single top depth-1 parent** (the
   liquidation buy-pressure z-spike; extended by wide relative spread, or by raw
   liq_buy_ratio<1 — i.e. the z-spike *without* an absolute buy-side excess). Beam-K
   coverage of depth 2 at true scale is 100% for any K ≥ 1 that keeps the top parent —
   trivially for K ≥ 3 = keep-everything.
2. The true-scale funnel is *narrower* than any proxy rung (3→5 vs 300-sym's 6→17): more
   instances tighten both the bar's variance and the real edges' noise. The explosion risk
   lives at depths 3–4 (if the permeability pattern holds at scale) — `beam-full4` is
   measuring exactly that overnight.

## 6b. Full universe, true scale, designed depth (full-4 — the definitive run)

Complete `MAX_DEPTH=4` funnel at 8.55 M instances (200 perms, fast-perm null), under a
13 G cap beside live capture/tick. **Peak RSS 11.44 G; wall 2.0 h** (load 49 min, depth-1
null 19 min, depth-2 10 min, depth-3 **66 s**, depth-4 38 min — sparse deep conjunctions
ride the int32-index firing fast path):

| depth | candidates | scorable | null bar | passed | pass rate | edge max |
|---|---|---|---|---|---|---|
| 1 | 548 | 546 | 0.00042 | 3 | 0.5% | 0.00221 |
| 2 | 1,587 | 1,574 | 0.00749 | 4 | 0.3% | 0.01453 |
| 3 | 2,057 | 1,711 | 0.01270 | **772** | **45%** | 0.03054 |
| 4 | 291,230 | 186,376 | 0.01951 | **31,760** | **17%** | 0.05269 |

Same shape as the proxy — hard gate at depths 1–2, permeable at depth ≥3 (45%), flat
survivor distributions at depth 3–4 (top-1% share 1.7–1.8% of total lift) — but the flood
is ~8× smaller than the proxy's (31.8 K vs 262 K) and the depth-4 null is notably less
permeable (17% vs 46%).

**TRUE-SCALE ADAPTIVE BEAM COVERAGE — the deciding table** (beam bites only at the
depth-3→4 transition, since d3's 772 > K is the first count above any candidate K):

| K | raw d4 cov | of top-100 | of top-1k | of top-10k | d4 lift-mass |
|---|---|---|---|---|---|
| 50 | 33.3% | 54.0% | 66.6% | 63.5% | 36.7% |
| 100 | 61.9% | 84.0% | 73.0% | 78.1% | 63.7% |
| 200 | 84.2% | 94.0% | 84.7% | 89.8% | 84.8% |
| **500** | **99.3%** | **100%** | **99.9%** | **99.6%** | **99.3%** |
| 1000 | 100% | 100% | 100% | 100% | 100% |

Structure: the 31,760 depth-4 survivors descend from 760 of the 772 depth-3 parents, but
the lineage is head-heavy — 90% of depth-4 parent-links flow through the top 483 depth-3
parents, i.e. almost exactly a 500-beam. **K=500 loses 0.7% of depth-4 survivors, none of
the top-100, and 0.7% of total lift mass, while cutting the depth-4 candidate pool from
291 K to ≤ 275 K worst-case (K × ~550) and, at depth 5-equivalent scales, preventing any
recurrence of the extension-pool OOM.** K=500 is confirmed as the default.

## 7. Universe-composition sensitivity (why the proxy caveat is severe)

Same code, same designed settings, only the symbol set varies:

- **60 liquid majors** (0.57 M inst): 0 depth-1 survivors — cascade never starts.
- **300 spread** (2.75 M): 3–6 depth-1 survivors → cascades to 2.4–2.8 K at depth 3,
  262 K at depth 4.
- **450 spread** (4.15 M): 2 depth-1 survivors → 0 at depth 2 (best edge 0.0067 vs bar
  0.00725).
- **896 full** (8.46–8.55 M): 3 → 4/5 at depths 1–2 (single-parent structure), then
  772 → 31,760 at depths 3–4 (designed-depth run).

Depth reached is not monotone in universe size (1 → 3/4 → 2 → ≥2 across 60→300→450→896).
Small universes raise the effective bar (higher per-candidate variance under shuffling);
composition moves the real edge head relative to it. **No subset is a trustworthy stand-in
for full-universe depth-3/4 behavior** — the md4-300 beam-coverage numbers are structural
evidence (lift heritability; flat redundant tail), not full-universe point estimates.
full-4 replaces them with true-scale numbers if it completes.

## 8. What was run (reproducibility)

- Harness + analyzers (scratchpad, session-local): `beam_measure.py` (instrumented mirror),
  `beam_analyze.py` (funnel/lifts/adaptive-beam tables), `beam_redundancy.py` (parent
  structure + top-X coverage), `run_ladder.sh`, `gen_subsets.py`.
- Raw JSONLs (scratchpad): `beam_full.jsonl` (full-1, OOM in load),
  `beam_subset.jsonl` (60), `beam_300.jsonl`, `beam_450.jsonl`, `beam_300_md4.jsonl`
  (~263 K survivor records with lineage), `beam_full2_load_only.jsonl` (load + flat-prep
  proof; killed externally), `beam_full3.jsonl` (true-scale d1–d2, complete),
  `beam_full4.jsonl` (designed-depth true-scale, COMPLETE — 31.8 K survivor records with lineage).
- Execution: `systemd-run --user` transient scope/service, `MemoryMax` 10–13 G,
  `MemorySwapMax=0`, `CPUWeight=20`, `IOWeight=20`, nice 19, idle IO — capture/tick
  undisturbed throughout (box 22 G, ~15 G available).
- Production code, discovery DB, brain store, registry: untouched (store/registry opened
  read-only through the standard read APIs).
