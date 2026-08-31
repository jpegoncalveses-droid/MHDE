# CONCLUSION — crypto brain discovery engine

**Status: CLOSED. Negative result. No viable edge found.**

Date of record: 2026-08-31. Final evidence state: discovery run 61 (run 62 in flight at the
time of writing and not counted). This document is the closing record for the brain
discovery workstream — the batch rule-discovery engine that searched Binance USDT-M
microstructure for tradeable conditional edges. It states the pre-registered bar, what was
found in-sample, what happened out-of-sample, the caveats that bear on reading the final
number, and the verdict.

The permanent scientific record is archived at
`/home/jpcg/crypto_teardown_archive_20260831/` (registry, ledger parquet, analysis
documents, pass-summary journal, SHA-256 manifest) and is outside every path scheduled for
deletion.

---

## 1. The pre-registered bar

Declared in `data/processed/economics_preview_20260828.md` before the economics read:

> Pre-registered cost model: **30bp/round-trip floor** (20 taker + 10 slippage).
> Bars: bootstrap (10k) **CI_low(net mean) > 0** *and* **point net ≥ +20bp**.

Tightened in `data/processed/spread_cost_test_20260828.md` to a deliberately pessimistic
bound that replaced the flat slippage constant with measured book state:

> Per trade: **20bp taker RT + the full quoted spread at entry** (crossing the book both
> ways). Deliberately overcounts vs maker or mid fills.

The out-of-sample confirmation requirement was that the rows be **post-PR-#99 salted
stage-4 samples** — deterministic rule-seeded sampling independent of the stage-2 draw the
rules were selected on — with entries after the run-46 frontier
(`CUT_B = 2026-08-27 12:24 UTC`), accumulated forward on every completed pass, over family
membership frozen to rules promoted **before run 47**. Every OOS number below uses that
membership rule and that cost model, with a 10,000-resample bootstrap at `seed=7`.

---

## 2. The in-sample result

15 spread-conditioned families, live members, entries 2026-08-08 → 2026-08-24, joined to
the brain-store `bookticker` snapshot at a **100.0% hit rate**:

```
n              8,570 joined trades
aggregate NET  +310.6 bp per round trip
worst family   CI_low = +38.9 bp
best family    CI_low = +411.8 bp
verdict        15 / 15 SURVIVE the pessimistic bound
```

Entry-spread distribution across those 8,570 entries: p50 **12.1bp**, p90 **71.4bp**, p99
**250.1bp**. The feared cost-scales-with-the-condition effect was real but an order of
magnitude smaller than the measured gross edge. On the in-sample evidence the families
cleared the pre-registered bar emphatically and by a wide margin.

The one family that had already been settled as a failure — `forceorder.liq_buy_ratio.z1440>`,
the graduation candidate — failed the preview outright on 50,973 trades: gross +10.1bp
against a 30bp floor, **net −19.9bp with CI_high = −16.7bp**, all four promoted members
individually negative.

---

## 3. The out-of-sample result

Accumulated over 15 completed passes (runs 47–61), spread join **780 / 780 = 100%**:

```
THE 15 SPREAD FAMILIES
n = 780    net = +2.0 bp/trip    bootstrap 95% CI [-25.3, +30.1]    win 52.9%
```

The interval contains zero. The interval also **excludes the in-sample aggregate of
+310.6bp by more than an order of magnitude**. Whatever the true out-of-sample mean is, it
is not the in-sample number; the honest reading is that the estimate is consistent with
zero and n remains too small to resolve its sign.

Trajectory of the cumulative estimate, one row per completed pass:

| after run | cum n | cum net | CI_low | CI_high | win% |
|---|--:|--:|--:|--:|--:|
| 47 | 121 | −2.7 | −67.5 | +58.2 | 49.6 |
| 48 | 144 | +24.4 | −31.6 | +76.0 | 54.9 |
| 49 | 176 | −49.2 | −117.7 | +16.4 | 51.1 |
| 50 | 328 | −75.4 | −120.6 | −30.4 | 49.1 |
| 51 | 407 | −72.8 | −110.9 | −35.8 | 46.9 |
| 52 | 452 | −59.6 | −95.0 | −24.8 | 49.8 |
| 53 | 476 | −55.4 | −89.7 | −21.9 | 50.8 |
| 54 | 541 | −34.1 | −68.3 | −0.6 | 51.4 |
| 55 | 584 | −29.6 | −61.9 | +2.5 | 51.5 |
| 56 | 612 | −28.6 | −59.6 | +2.1 | 50.3 |
| 57 | 629 | −32.7 | −62.9 | −1.9 | 49.4 |
| 58 | 657 | −21.3 | −50.9 | +9.1 | 50.5 |
| 59 | 678 | −19.8 | −48.0 | +9.5 | 51.2 |
| 60 | 762 | −8.4 | −35.5 | +18.8 | 51.8 |
| 61 | 780 | **+2.0** | −25.3 | +30.1 | 52.9 |

Fifteen passes of forward evidence never produced an interval excluding zero on the
positive side. The estimate spent runs 50–57 with the point estimate between −75 and −21bp
and drifted back toward zero over the final four passes — for reasons that are at least
partly compositional, see §5.

---

## 4. The single-feature forceorder result

The `forceorder` families carrying no book-state condition accumulated clean rows an order
of magnitude faster, and they are unambiguous:

```
ALL PROMOTED MEMBERS
forceorder.liq_total.z1440>       n =  8,967   net =  -7.3 bp   CI [-11.1,  -3.4]   win 15.7%
forceorder.liq_buy_ratio.xrank>   n = 11,098   net = -19.3 bp   CI [-22.5, -16.0]   win 12.5%
forceorder.liq_buy_ratio.z1440>   n =  4,478   net = -24.8 bp   CI [-34.6, -15.3]   win 38.7%
POOLED                            n = 24,543   net = -15.9 bp   CI [-18.6, -13.2]   win 18.5%
```

Under the same pre-run-47 membership rule applied to the 15: pooled **n = 5,080, net
−16.5bp, CI [−22.5, −10.3]**. Every pooled interval, on both membership definitions, lies
**entirely below zero**. On 24,543 out-of-sample round trips the liquidation-spike signal
loses roughly 16bp per trip after costs. This is a confirmed negative, not an inconclusive
one, and it confirms the economics preview's verdict on the graduation candidate on
independent forward data.

---

## 5. Composition drift — the caveat on reading the trajectory

The cumulative series in §3 is **not a fixed population sampled repeatedly**. Family
membership was frozen before run 47, but members were still subject to ordinary
confirmation-walk rejection, and a family whose clean members are all rejected can never
log another clean row. By run 61, **21 of the 45 clean member rules were rejected and 7 of
the 15 families were permanently closed.**

The closures are not random with respect to performance. The two most negative families are
frozen; the two largest positive families are open and absorbed most of the recent rows:

| family | n | net | status | rows added, runs 59–61 |
|---|--:|--:|---|--:|
| spread.xrank\|liq_tot\|fund.z< | 205 | +55.2 | open | +52 |
| spread\|liq_tot\|fund.z< | 157 | +54.0 | open | +47 |
| bimb.z\|spread\|liq_tot | 31 | +60.4 | open | +9 |
| spread\|liq_buy.xr\|liq_buy.z<\|mret.z | 62 | −148.7 | **frozen** | 0 |
| spread\|liq_buy.xr\|liq_buy.z<\|retco.z | 50 | −235.3 | **frozen** | 0 |

Of the 123 rows added over the final three passes, **99 went to the two largest positive
families and 108 to the three positive families; every frozen family added zero.** The
drift of the cumulative estimate from −21.3bp at run 58 to +2.0bp at run 61 is therefore
substantially a change in the *mix* of the sample, not evidence that a stable population's
mean is rising. Any future reader who sees the final number trending upward across the last
four passes should read this section before drawing an inference from it.

---

## 6. One family above zero is what 15 simultaneous tests produce

At run 61 exactly one of the 15 families has a bootstrap interval entirely above zero
(`spread.xrank|liq_tot|fund.z<`, n=205, +55.2bp, CI [+5.4, +104.7]). This is not evidence
of an edge.

Under the null hypothesis of no effect, with 15 families each tested at a nominal 95%
two-sided interval, the probability that **at least one** interval excludes zero on the
positive side is `1 − 0.975^15 ≈ 31.6%`; the probability that at least one excludes zero in
*either* direction is `1 − 0.95^15 ≈ 53.7%`. Observing one positive exclusion out of 15 is
the unremarkable outcome, not the surprising one.

Two further considerations push in the same direction:

- **The bootstrap understates variance.** The families are near-duplicates — twelve of the
  fifteen are variants of one `rel_spread>|liq_buy<` strategy — and different families fire
  on the same market episodes in the same minutes. The iid resample treats correlated
  observations as independent, so nominal intervals are too narrow and *all* exclusions,
  positive and negative, overstate their significance.
- **The three families whose intervals exclude zero on the negative side** are not
  cancelled out by the one positive; under the null one expects roughly 0.75 exclusions in
  total across 15 tests, and the count of 4 is inflated by exactly the correlation above.

No multiple-comparison correction was pre-registered, and none is applied here to rescue or
condemn a family after the fact. The point is only that a single clearing family at n=205,
selected post hoc from 15, is not a result.

---

## 7. Verdict

**No viable edge was found.**

- The in-sample result (+310.6bp on 8,570 trades, 15/15 families clearing a pessimistic
  measured-spread cost model) **did not survive contact with out-of-sample data.**
- After 15 forward passes the 15 spread families stand at **n=780, +2.0bp, CI [−25.3,
  +30.1]** — consistent with zero, far below the pre-registered +20bp point bar, and never
  once producing a positive interval exclusion. The bar was never met.
- The single-feature forceorder families, which accumulated 31× the evidence, are
  **confirmed negative**: n=24,543, −15.9bp, CI entirely below zero.
- The one family whose interval clears zero is what 15 simultaneous correlated tests
  produce by chance (§6), and the recent drift toward zero is substantially a change in
  sample composition (§5).

The engine did its job. It found conditions with genuine in-sample statistics, and the
forward-confirmation machinery that was built to check them did check them, and they did
not replicate. That is a working scientific apparatus reporting a negative result, which is
a real outcome and is recorded here as one. Nothing should be staked on any family
discovered by this engine.

**Out of scope of this conclusion:** capacity and depth beyond L1, exit-fill realism, and
maker-side execution were never measured. They are not open questions that could rescue the
result — the result is negative on the friendliest cost assumptions already tested — but
they are stated so no future reader mistakes silence for a finding.

---

## 8. Known defects, recorded at close

Found during the final evidence week and left unfixed. Recorded so they are not
rediscovered as mysteries, and so that anyone reading the archived pass summaries knows
which logged numbers to distrust.

### 8.1 `crypto/research/brain/discovery/confirmation.py:183` — double-walk

`run_confirmation` iterates `for state in (DISCOVERED, CONFIRMING, PROMOTED)`, calling
`RS.list_rules(conn, state=state)` fresh for each state. The `DISCOVERED` branch commits
each advanced rule into `CONFIRMING` (via `RS.set_state`, which commits through `with
conn:`) *before* the `CONFIRMING` list is queried. Every newly advanced rule is therefore
walked and counted a **second time in the same pass**.

- **Registry state is unaffected** — the second decision is idempotent on identical fresh
  statistics, and per-pass admission conservation was verified EXACT on every pass 47–61.
- **The logged `confirming` count is wrong**, overstating the registry by exactly the
  `advanced` count. Verified: run 61 logged `confirming: 1964`, `advanced: 78`; registry
  holds `1886 = 1964 − 78`. Same relation at runs 59 (1930 − 119 = 1811) and 60
  (1916 − 83 = 1833).
- **Cost:** one duplicate full forward walk per admitted rule per pass — 78 to 119 wasted
  walks on recent passes. A contributor to the wall-clock growth in §8.3, though not the
  dominant one.

Anyone analysing the archived `journal/discovery_pass_summaries.log` should take the
`confirming` field as `registry_confirming + advanced`, not as a registry count.

### 8.2 Observability gap — systemd stopped emitting per-run memory and swap peaks

From run 50 onward the `mhde-brain-discover.service` `Consumed` journal line carries CPU
time only; the memory-peak and swap-peak fields stopped appearing. No user-manager restart
or unit change explains the cutoff (systemd 255). The last runs with recorded peaks:

```
run 47   12.6 GiB peak / 0 B swap
run 48   13.0 GiB peak / 8.4 GiB swap
run 49   13.0 GiB peak / 2.7 GiB swap
```

The underlying cgroup counters remained live and readable throughout — sampled directly
from the in-flight run 62 on 2026-08-31: `memory.peak` 12.41 GiB (95.5% of the 13 GiB
`MemoryMax`), `memory.swap.peak` 2.84 GiB. The gap was in systemd's reporting, not in the
kernel accounting; a sampler reading `memory.peak` during the pass would have closed it.
Cause never identified. Consequence: for runs 50–61 there is **no recorded per-run memory
or swap peak in the archive**, and the engine was demonstrably running within 4.5% of its
memory cap at close.

### 8.3 Wall clock at 4h59m against a 6h cadence

Pass duration grew steadily as the promoted set grew and stage-4 logging scaled with it:

```
run 49   3h01m38s      trades_logged      33,116
run 58   4h20m52s      trades_logged     104,070
run 61   4h59m02s      trades_logged     157,745      margin vs 6h cadence: 1h00m58s
```

Ledger growth over the final three passes alone was 411,891 rows (total 1,239,990). The
schedule margin had halved in a single day and, on the observed trend, the engine would
have begun overrunning its own 6-hourly cadence within days. No overrun occurred before
close; zero OOM kills and zero failed passes were recorded across runs 47–61. The growth
was structural — `TRADELOG_MAX_INSTANCES = 5000` per promoted rule against a promoted set
that reached 104 rules across 49 families — and was never bounded.

---

## 9. Final state at close

```
discovery runs completed        61  (run 62 in flight, uncounted)
registry rows               44,616   benched 39,883 / confirming 1,886 / rejected 2,737
                                     promoted 104 / expired 6      CONSERVED
distinct families            1,837
simulated_trades          1,239,990
seat integrity                       0 over-quota, 0 duplicate-cohort, 0 sub-floor
bench hygiene                        0 stamped, 0 promoted-marked; all walk evidence
                                     confined to the pre-admission Aug-21 cohort
expiry                               HELD under KI-166 (frontier 2026-08-31 07:57 vs
                                     resume threshold 2026-09-03)
```

Every conservation and integrity invariant the engine was built with held to the last pass.
The books balance. The result is negative.
