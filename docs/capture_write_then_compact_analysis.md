# Capture firehose writer: write-then-compact — design analysis

**Status:** analysis / proposal for operator review. **No implementation.** If
accepted this becomes **ADR-038** (supersedes the ADR-037 in-RAM hourly roll-up for
the firehose writer). Firehose stays **parked** until a measured trial confirms the
numbers below.

**Date:** 2026-06-14. **Author:** capture-core workstream.

---

## 0. Why this exists (the failure that triggered it)

ADR-037 replaced the old one-file-per-flush writer with an **in-RAM hourly
roll-up**: each `symbol=/date=` partition buffers rows in memory and flushes a
parquet part on the EARLIER of `FLUSH_MAX_BYTES` (64 MiB) or
`CAPTURE_FIREHOSE_ROLLUP_S` (3600 s). That fixed the inode-exhaustion incident
(verified: under live load inodes stayed flat at 26%, zero tiny-file regression).

But on the **Stage B live start (2026-06-14)** it OOM-looped:

| Observed | Value |
|---|---|
| Box RAM / swap | **15 GiB / 0 swap**; engine+OS ≈ 3.8 GiB used |
| Firehose `MemoryMax` | **8 GiB** (8,589,934,592 B) |
| Aggregate RSS inflow | **~0.8 GB/min** net climb |
| Result | OOM-killed **3×** in ~15 min (`result 'oom-kill'`, `status=9/KILL`) |

**Root cause:** with a 1-hour roll-up, *no* partition flushes by age for the first
hour, so RAM accumulates **every** partition's data until each hits 64 MiB or 60 min.
Across ~1,581 per-symbol WS partitions (527 symbols × {aggTrade, depth, bookTicker})
plus markPrice/forceOrder/depth_snapshot, the aggregate climbs ~0.8 GB/min and blows
the 8 GiB cap in ~10 min. The caps contained it (it OOM'd *itself*, not the engine —
ADR-036 priority working), but it captures nothing useful and loses up to an hour of
data per kill.

The roll-up couples two things that should be independent: **how often we flush**
(which sets RAM) and **how many files we keep** (which sets inodes). Shortening the
window to fix RAM multiplies files (see §7). **Write-then-compact decouples them.**

---

## 1. The proposal in one paragraph

Flush small parquet files **frequently** (every ~10–30 s) so RAM only ever holds one
flush-interval of data → low, bounded RAM. Then a **periodic (hourly) compaction**
merges the many small files of each **closed** past hour into ~one file per
partition-hour — reusing the **migration-proven** `compact_partition` (write→verify
row count→delete originals→rename). The result is the *same* low steady-state file
count as the hourly roll-up (~1 file/partition/hour) **without** holding an hour in
RAM. The open (current) hour is never touched by compaction, so writer and compactor
never share a file. Retention trims to 5–7 days. Depth stays @100 ms. The
`symbol=/date=` event-time layout and `recv_ts_ns` cursor are unchanged.

---

## 2. Grounding inputs (all from real measurements)

- **Box:** 15 GiB RAM, 0 swap; engine+OS ≈ 3.8 GiB resident (`free -h`).
- **Firehose `MemoryMax` = 8 GiB**; aggregate RSS inflow **~0.8 GB/min** (measured
  climb under the failed roll-up); OOM at 8 GiB.
- **Root fs inodes:** 9,732,496 total; **baseline 2.51 M used (26%)**; guard **WARN
  80% (≈7.79 M)**, **HALT 90% (≈8.76 M)**; ≈7.2 M free.
- **Migration (06-07..06-09 = 72 h):** **3,084,871 → 9,371 files**, **185,935,735
  rows**, ran **~53 min** under `nice -n 19 ionice -c3`. ⇒ compaction throughput
  ≈ **970 files/s** (≈ 3.5 M rows/min), i.e. **~81× faster than real-time data
  accrual**, and it preserved row parity with **0 mismatches**.
- **Live firehose:** 527 symbols, 1,583 streams, 9 routed shards. Active firehose
  partitions ≈ **1,581** per-symbol WS (aggTrade/depth/bookTicker) **+ ~527**
  markPrice + ~527 depth_snapshot + sparse forceOrder ≈ **~3,000/day** (matches the
  migration's 9,371 over 3 days ≈ 3,124/day). ~**2,100** are active *every* hour
  (the WS per-symbol set + markPrice).
- **Firehose disk volume** ≈ 44 GB/day (ADR-036, depth-dominated).

> All projections below are *estimates to be confirmed by one controlled trial*
> (§9). Where a number is derived, the arithmetic is shown so it can be checked.

---

## 3. (A) Concrete design

**Writer (existing `RawDatasetWriter`, unchanged mechanism):**
- Firehose flush interval `CAPTURE_FIREHOSE_FLUSH_S` ≈ **10–30 s** (recommend 30 s as
  the RAM/file balance; the trial picks the final value). This replaces the 3600 s
  roll-up for the firehose writers only.
- Keep a byte cap `FLUSH_MAX_BYTES`, **lowered to ~8–16 MiB**, so a hot partition
  still flushes mid-interval and per-partition RAM is doubly bounded. (At a 30 s
  interval most partitions flush on age before reaching even 16 MiB, so the cap is a
  backstop, not the primary trigger.)
- Depth stays **@100 ms**; `symbol=/date=` keyed on **event** time; `recv_ts_ns`
  preserved. **No layout or schema change.**

**Compaction (periodic, hourly, separate timer):**
- For each firehose `symbol=/date=` partition, merge the small parts belonging to
  **closed** hours into **~one file per closed hour**, **skipping the open hour**.
- Reuses `compact_partition`'s crash-safe core (concat → `recv_ts_ns` sort →
  write `.tmp` → verify row count == sum → delete originals → `os.replace`). The one
  change: it must operate on a **closed-hour subset** of a partition's files (today
  it merges the *whole* dir into one). Closed-hour selection by **file mtime older
  than the current hour boundary minus a small margin** (cheapest; no need to read
  contents), or by each file's `recv_ts_ns` hour.
- Compaction only ever touches files the writer has finished with (closed hours),
  so **writer and compactor never contend on a file**.

**Retention:** `CAPTURE_RAW_RETENTION_DAYS` **14 → 5–7**, oldest `date=` partitions
pruned (existing `expire_firehose_partitions`); the **free-space byte guard stays the
hard floor** (50 GiB soft / 10 GiB critical).

**Guards unchanged:** inode guard (WARN 80% / HALT 90%) and byte guard backstop both
small-file accumulation and disk pressure (§8).

---

## 4. (B) RAM vs flush interval

Under frequent flush, every partition flushes each interval, so resident data ≈
`inflow_rate × flush_interval + base`. Using the measured **0.8 GB/min** (and a
conservative band up to ~1.2 GB/min, since size-flushes were already releasing some
RAM during that measurement, so true gross is a little higher) + ~0.5 GiB base:

| Flush interval | RAM held (≈0.8 GB/min) | conservative (≈1.2) | vs 8 GiB cap |
|---|---|---|---|
| 10 s | ~0.6 GiB | ~0.7 GiB | **7%–9%** |
| 15 s | ~0.7 GiB | ~0.8 GiB | ~9%–10% |
| **30 s** | **~0.9 GiB** | **~1.1 GiB** | **~11%–14%** |
| 60 s | ~1.3 GiB | ~1.7 GiB | ~16%–21% |

At 30 s: firehose ~1 GiB + engine 3.8 GiB + OS ~1 GiB ≈ **~6 GiB of 15 GiB** → ~9 GiB
headroom, vs the roll-up's 8 GiB+ OOM. **RAM ceases to be the binding constraint.**

---

## 5. (C) Files and inodes

Small files per partition per hour at interval `f` = `3600/f`.

**(i) Transient peak** — the open (uncompacted) hour, across ~2,100 partitions
active each hour (past hours of today are already compacted):

| Interval | small files / partition·hr | transient files (×2,100) | as inodes | total inode % |
|---|---|---|---|---|
| 30 s | 120 | ~252 k | +2.6% | **~29%** |
| 15 s | 240 | ~504 k | +5.2% | ~31% |
| 10 s | 360 | ~756 k | +7.8% | ~34% |

**(ii) Steady state** — closed hours compacted to ~1 file/partition·hr =
~3,000 × 24 ≈ **72,000 files/day** (identical to the hourly roll-up's steady state):

| Retention | compacted files | + open-hour transient (30 s) | total firehose inodes | total inode % |
|---|---|---|---|---|
| 5 d | ~360 k | +252 k | ~612 k | **~32%** |
| 7 d | ~504 k | +252 k | ~756 k | **~34%** |

Both the transient peak (~29–34%) and the steady state (~32–34%) sit **far below the
80% WARN / 90% HALT** thresholds. Write-then-compact reaches the **same** low
steady-state file count as the roll-up while keeping RAM ~8× lower.

---

## 6. (D) Read contract, (E) crash behavior

**(D) Read contract holds throughout.** Both the small flushed files and the
compacted files are valid `part-*.parquet` under `symbol=/date=`, with identical
per-stream schema and `recv_ts_ns`; a hive-dataset read unions them transparently
(the migration's compacted files already verified as hive-readable on real data). No
layout change; depth stays @100 ms.

*Mid-compaction caveat (grounded in the code):* `compact_partition`
(`maintenance.py:106-109`) deletes the originals **before** `os.replace(tmp → final)`.
So for a sub-millisecond window a concurrent reader globbing `part-*.parquet` could
momentarily **under-count** one partition's just-compacted closed-hour data (it
reappears once the rename lands) — never double-count. For the batch brain reader,
which does not run concurrently with the hourly compaction, this is negligible. If it
ever matters, reorder to **rename-before-delete** (write the compacted file under a
fresh uuid, then delete originals), trading the under-count window for a brief
benign double-count of fully-overlapping rows. Flag as an implementation choice.

**(E) Crash loss is ~120× smaller.**

| | In-RAM hourly roll-up (ADR-037) | Write-then-compact (30 s flush) |
|---|---|---|
| Worst-case loss on crash/OOM | up to **~1 hour** of unflushed RAM | **≤ one flush interval (~30 s)** |
| Observed on 2026-06-14 | OOM every ~5 min → each window lost | (no OOM; see §4) |
| Compaction safety | n/a | crash-safe: `.tmp`→verify→delete→rename; an orphan `.tmp` is ignored next run (proven in the 3.08 M-file migration, 0 mismatches) |

---

## 7. (F) Does compaction keep up?

One hour's small files = ~2,100 partitions × (3600/f):
- 30 s → ~252 k files/hour; 10 s → ~756 k files/hour.

At the **measured migration throughput (~970 files/s, under idle `ionice`)**:
- 252 k → **~4.3 min**; 756 k → **~13 min** — both **far inside** the 1-hour budget.

The migration also showed the extreme case: **72 hours of backlog compacted in
53 min**, i.e. catch-up is ~81× faster than accrual. **If compaction ever falls
behind** (timer missed, host paused), small files accumulate and inode % rises — and
the **inode guard backstops**: WARN at 80%, HALT writes at 90% (forward-only). So a
compaction stall degrades gracefully and **cannot** re-exhaust inodes.

---

## 8. (G) Honest comparison vs the tuned-window buffer (Option 1)

Option 1 = keep the in-RAM roll-up but shorten the window to 3 or 5 min. Permanent
files (no compaction) = `1440/window_min × ~3,000 partitions`.

| Metric | Tuned window 3 min | Tuned window 5 min | **Write-then-compact (30 s flush)** |
|---|---|---|---|
| Peak RAM | ~2.4–4 GiB | ~4–5 GiB (matches the ~4.1 GiB seen early in the OOM run) | **~1 GiB** |
| Files/day | ~1.44 M | ~864 k | **~72 k** |
| Inodes @5 d | ~9.7 M ≈ **100% (exhausts/HALT)** | ~6.8 M ≈ 70% (tight) | ~32% |
| Inodes @7 d | >100% | ~8.55 M ≈ **88% (trips WARN, near HALT)** | ~34% |
| 2× volatility spike | 2× RAM (5 min → ~8 GiB, **OOM risk**) **and** 2× files (**guard trips**) | same, worse | RAM still capped by interval (~1–2 GiB); 2× transient files compacted away; compaction 2× work (still <30 min/hr) |
| +N symbols | +RAM **and** +permanent files | same | +transient files only; steady state still ~1/partition·hr |

**The numbers favour write-then-compact decisively** — on RAM (~4–8× lower), on files
(~12–20× fewer), and on spike-robustness. The tuned window cannot escape the
RAM↔files tradeoff: at 5 min it is *simultaneously* near the RAM ceiling **and** near
the inode ceiling at modest retention, and a volatility spike pushes it over both at
once. Write-then-compact breaks the tradeoff by decoupling flush frequency (RAM) from
file count (compaction).

**Honest costs of write-then-compact** (where it is *worse* than the tuned window):
- **~2× write amplification** — each row is written once as a small file and once in
  the compacted file → ~88 GB/day of writes vs ~44. Absorbed by `ionice` idle;
  worth an SSD-endurance note over months.
- **More moving parts** — a compaction timer + closed-hour selection logic (vs the
  tuned window's single knob). Mitigated by reusing `compact_partition`.
- **Brief reader under-count window** during compaction (§6) — negligible for the
  batch reader; fixable by rename-before-delete.

---

## 9. (H) Risks / open questions / failure modes

| Risk / open question | Mitigation / backstop |
|---|---|
| Closed-hour selection by mtime mis-classifies a boundary file | Only compact hours that closed **> ~5 min ago** (margin); or select by each file's `recv_ts_ns` hour. |
| Compaction process crashes / hangs and kills capture | Run as a **separate hourly systemd timer** (not in the capture loop), so a compaction failure can't take down capture. Reuses the `mhde-capture-firehose-expire` pattern. |
| Compaction falls behind under sustained 2×+ load | Inode guard HALTs writes at 90% (forward-only) before exhaustion; migration shows catch-up is ~81× real-time. |
| Reader during compaction under-counts (§6) | Negligible for batch reader; rename-before-delete if needed. |
| Write amplification stresses the disk | Byte guard (50/10 GiB) + `ionice` idle; monitor write rate in the trial. |
| Exact flush interval / gross inflow / compaction wall-time under live idle priority unknown | **Resolved by the §10 measured trial**, not by guesswork. |
| `compact_partition` today merges a *whole* partition dir | Add a closed-hour-subset variant (merge a given file list → one file); small, contained, same crash-safe core. |

The two guards are the **hard backstop**: even if every projection here is wrong, the
inode guard halts firehose writes at 90% and the byte guard prunes/halts on disk — so
**the 2026-06-09 box-down failure mode stays closed** regardless.

---

## 10. (I) Recommendation, implementation outline, trial plan

**Recommendation:** adopt **write-then-compact**. It is the only option that keeps
RAM ~1 GiB, files ~72 k/day, and inodes ~34% *simultaneously*, and it is robust to
volatility/symbol-count spikes — whereas the tuned window is cornered between the RAM
and inode ceilings. The cost (≈2× write amplification + a compaction timer) is
acceptable and `ionice`-absorbed.

**Cleanly implementable on the current writer?** Yes:
- The writer already flushes on an interval — shorten it (one default change).
- `compact_partition` already does the crash-safe merge/verify/delete — add
  closed-hour subset selection.
- No layout, schema, or `recv_ts_ns` change; the read contract is untouched.

**Implementation outline (LATER — not in this PR):**
- `config.py` — add `CAPTURE_FIREHOSE_FLUSH_S` (~30 s) for the firehose; lower
  `FLUSH_MAX_BYTES` (~16 MiB); `CAPTURE_RAW_RETENTION_DAYS` 14→7; add
  `CAPTURE_COMPACTION_CADENCE_S` (3600) + `CAPTURE_COMPACTION_CLOSED_MARGIN_S`.
- `service.py` — firehose writers use `CAPTURE_FIREHOSE_FLUSH_S` (already threaded
  through `flush_interval_s`; effectively a default change).
- `maintenance.py` — add `compact_firehose_closed_hours(root, now_ts)` that selects
  each partition's closed-hour file subset (by mtime margin) and merges via the
  existing crash-safe core; reuse row-parity verification.
- `main.py` — `crypto capture-firehose-compact-recent` CLI.
- `systemd/` — `mhde-capture-firehose-compact.{service,timer}` (hourly,
  built-not-deployed), with the existing resource caps.
- Tests (TDD) — writer bounded RAM proxy; closed-hour selection skips the open hour;
  compaction round-trip + hive-readability; row parity; retention; keep-up.

**One controlled measured trial before any re-enable:**
1. Apply the config (short flush + hourly compaction) on a branch; capture stays
   parked otherwise.
2. Run the firehose for a **bounded 60–90 min** window under the caps.
3. Measure: **peak RSS** (target < ~2.5 GiB), open-hour transient file count + inode
   %, post-compaction file count + inode %, and **compaction wall-time** for one hour.
4. **Re-enable Stage B only if** peak RSS and files/inodes land within the §4–§5
   projections; otherwise tune the flush interval / retention and re-measure.

---

## Appendix — what is NOT changing

- The `symbol=/date=` hive layout keyed on **event** time; per-stream schemas;
  `recv_ts_ns` as the monotonic incremental cursor.
- Depth capture cadence (**@100 ms** — the "no pre-coarsen" operator decision stands;
  write-then-compact does not require coarsening it).
- The inode guard (80/90%) and free-space byte guard (50/10 GiB).
- The completed one-shot migration of 06-07..06-09 (already compacted, verified).
- Stage A light collectors (klines maintenance, REST present-state, retention
  timers) — unaffected; they stay running.
