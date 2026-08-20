# RCA — brain frontier stall 2026-08-15→08-20 (F1): dense-cursor marooning on a disk-guard-truncated tape

Date: 2026-08-20. Investigation: read-only (4 parallel investigators + direct probes);
no service touched, no DB opened writable. Repo master @ `3624e3f`.
Status at writing: **stall ongoing** — frontier frozen at 2026-08-15 06:04:00
(two-probe delta over 13.6 min: exactly 0), labels dead since 08-16 04:01, discovery
re-scoring a frozen substrate with byte-identical funnels for 4+ days.

## 0. Mechanism in one paragraph

Chronic disk pressure (free pinned below the capture disk-guard's 50 GiB soft floor
since ≥ Aug 12-13) collapsed effective raw firehose retention from the configured 7
days to **<13-24 hours** (the in-loop guard prunes oldest `date=` partitions
continuously, protecting only today; the nightly 7-day expire has deleted 0 partitions
since 08-14 because the guard already ate everything). Against that shrunken tape, the
brain tick loop's four dense cursors (markprice, bookticker, trades, forceorder — the
firehose-derived datasets, exactly the `FIREHOSE_PRUNABLE_DATASETS` set) fell >1 day
behind during the discovery-engine load spike of Aug 13-15 (6h discovery cadence
+ shadow-confirm + 11.3G passes on a 22G host; ticks stretched to 250-970s wall). The
tape was then **deleted out from under the cursors** (mass `FileNotFoundError` bursts
03:05-03:06 on 08-15 as the post-OOM catch-up compaction + pruner swept `date=2026-08-15`
partitions mid-read). When the cursors finished draining what still existed
(08-16 03:45-04:16 wall), they entered a void: every bounded read of `(cursor,
cursor+300s]` finds no files, the quiet-gap skip advances only `W − watermark −
cadence = +150 s of tape per tick` against a ~148 s mean tick wall — **~1.0×
real-time, a permanent treadmill** — while the surviving tape's oldest edge resets to
*today 00:00* every midnight. The cursor can never re-enter live tape; settlement is
gated on the cursor (not wall clock), so the registry frontier — and with it labels
and discovery's fresh evidence — froze at the last settled windows: bookticker/trades
**08-15 02:03**, forceorder **03:01**, markprice **06:04** (those staggered times are
DATA-time boundaries of the deleted region, not wall-clock stop times).

This is a recurrence — worse — of the documented **KI-165 chain** ("fan-out →
disk-soft-floor → capture-buffer-shrink → dense-cursor-starvation", July episode) on
top of **KI-164** (disk pinned at the soft floor).

## 1. Timeline (UTC; all journal/DB-sourced)

| when | event |
|---|---|
| ≥2026-08-01, mass from 08-13 00:00 | free < 50 GiB soft floor; in-loop DiskGuard prunes oldest firehose partitions continuously (08-13 00:00-12:00 alone: ~4,245 partitions; each midnight since: ~1,500/shard sweep as yesterday loses today-protection). Effective firehose retention → <13-24h. Nightly `mhde-capture-firehose-expire`: "0 partitions expired" every night since 08-14. |
| 08-12 20:59 | First **inode guard 90% write-HALT** (root inodes at the halt line); halts recur daily → raw capture itself has drop windows. 08-15 00:15 peaked at exactly 90.0%. Today (08-20 12:33) back at 81-82%, warning again. |
| 08-13→08-14 | Discovery load era begins (PR #89: 6h `mhde-brain-discover.timer` + compactor-pause + failsafe). Brain-tick ticks stretch 250-970s wall; dense cursor lag grows 17,409s (08-13 12:00) → 51,133s (08-15 01:31). Bookkeeping already shows holes (trades: 08-13 17:36→08-14 00:16; 08-14 09:53→08-15 00:16) — each aligned with a midnight prune sweeping tape below the lagging cursor. |
| 08-15 00:08-04:29 | shadow-confirm transient unit (~6.8G RSS) runs alongside. |
| 08-15 00:30 | Discover pass starts; its wrapper pauses 3 compaction timers. 01:32-03:06: memory/IO squeeze; fast ticks balloon 58→297s. |
| 08-15 03:00:08 | **Host kernel OOM kills the discover pass** (11.3G peak, below its 13G cap; systemd-oomd inactive). Failsafe releases all paused compactors at once. |
| 08-15 03:05-03:06 | **Brain reader loses ~100 raw markPrice fragments mid-read** ("skipping unreadable capture fragment (data absent for this partition)… FileNotFoundError", `date=2026-08-15`) — compaction/prune racing the reader. Per project rule, each skipped fragment = a gap; none was flagged. |
| 08-15 03:06:26 | **Brain-store compactor OOM-killed mid replace-then-delete**; 03:36-03:41 retry succeeds but reports **registry-mismatches 1,074,704** ("registry window MISSING from store — truncated/lost part before compaction"). |
| 08-15 06:00 | `mhde-health-check` (morning Telegram heartbeat) **fails status=1** — it has been crashing since ~06-18 (see §4); no morning summary went out. |
| 08-15 evening→08-16 03:55 | Cursors drain the surviving backlog (2,709/tick → 1,140 → 684 → 147 snapshots). **Last nonzero dense tick: 08-16 03:55:48**; last registry INSERTs: trades/bookticker/forceorder 08-15 23:43:43, markprice 08-16 03:50:37. Last labels: 08-16 04:01. Frontier frozen thereafter. |
| 08-16 06:30 → now | 16 discovery runs on the frozen window: survivors=1015 every time, 0 inserts, 0 advanced/promoted/rejected/demoted; frontier_ns identical. ~3h CPU/pass burned. |
| 08-18 23:02 | brain-tick itself OOM-killed at its 3G cap (restart #8). Since restart: **every fast tick snapshots=0** (37+ h), max_lag pinned ~2.6 days, sawtooth ~flat. |
| 08-20 (audit) | Two-probe check: registry MAX(window_end_ns) delta over 13.6 min = **0** for all four; cursors creep +750s tape/744s wall (1.008×) through the void; raw tape for the four = only `date=2026-08-20` exists. |

## 2. Per-hop verdict (audit probes 2026-08-20 ~12:40-13:09 UTC)

| dataset | raw capture | brain snapshot parquet | registry rows |
|---|---|---|---|
| markprice | FLOWING but truncated to `date=2026-08-20` only (older dates deleted by guard) | STOPPED (last data 08-16 03:50) | **DEAD** — MAX(window_end) 08-15 06:04 |
| bookticker | same | STOPPED 08-15 23:43 | **DEAD** — 08-15 02:03 |
| trades (raw `aggTrade`) | same | STOPPED 08-15 23:43 | **DEAD** — 08-15 02:03 |
| forceorder | same | STOPPED 08-15 23:43 | **DEAD** — 08-15 03:01 |
| basis / open_interest / klines_1h / taker_ls / top_ls / global_ls / premium (8 poll-based) | FLOWING (their capture dirs are NOT in `FIREHOSE_PRUNABLE_DATASETS` — the stall boundary is exactly the prunable-set boundary) | FLOWING | FLOWING (0.2-3.7h lags) |

Cursors (registry `reader_cursor`): four dense frozen ~08-17 21:35-08-18 00:01
data-time, +150s/tick creep, ~2.6d behind and 2.2-2.7d AHEAD of their own datasets'
last settled windows — walking through a void. `updated_at_ns` refreshes every tick
for all 12 rows regardless of progress (why "cursor lags looked healthy").

**Unrecoverable data:** raw tape for 08-15 ~02:00 → 08-19 23:59 is deleted. The 5-day
dense-primitive + label gap is permanent and must be **gap-flagged, never
zero-filled** (project no-bias rule; same class as KI-161/KI-162).

**Deadlines:** registry retention (10d by `window_start_ns`, daily 00:45) reaches the
frozen frontier rows on **2026-08-26 00:45** — after that run, `MAX(window_end_ns)`
for the four datasets becomes NULL (frontier evidence and the write-dedup roster
vanish). The last dense snapshot parquet (`date=2026-08-15`) is eaten by the 21-day
store retention ~09-05. Recovery should land before 08-26.

## 3. Why five days ran silent (monitoring autopsy)

1. **The tick log's `max_lag` measures the reader cursor, not settlement** — and the
   quiet-gap skip *advances* the cursor through the void, so max_lag looked stable or
   improving while zero rows were ingested. `sources=4/4 ok` because empty passes
   return ok. The one honest in-band signal (`snapshots=0` on every fast tick since
   08-16) has no alert wired to it.
2. **`mhde-monitor-substrate-freshness`** (the designed silent-outage guard; system
   scope, 5-min cadence, green, Telegram working) checks raw-capture parquet mtimes
   (raw WAS flowing) and `MAX(updated_at_ns) FROM reader_cursor` — bumped every tick
   regardless of progress. **Structurally blind to a per-dataset frontier stall.** No
   check anywhere in `monitoring/` touches `window_end_ns` or per-reader
   `last_recv_ts_ns` (grep: zero hits).
3. **The capture stall-detector** watches raw firehose shards only (heartbeats, unit
   failures, peer asymmetry) — correctly green; capture genuinely was fine.
4. **The wider monitor fleet is dead and has been since ~2026-06-18**: 8+ system
   monitors (pipeline, smoke, data-quality, model-perf, cross-artifact, dashboard,
   dashboard-synthetic, continuous) crash with `CatalogException` on the dropped
   `fx_*`/equity tables **before ever reaching send_alert** — including
   `mhde-health-check`, the operator's morning Telegram heartbeat. Additional
   breakage: DuckDB `InternalException` on `engine_runs` (continuous +
   paper-trading-drift, 972 occurrences since 05-21); Telegram **HTTP 400** on
   >4096-char trace-embedding bodies, with `alert.py` saving throttle state even on
   failed sends (a failed alert self-suppresses for 24h); crypto-retrain timeout
   (30-min `TimeoutStartSec` vs longer training); phase0 monitor opening the DuckDB
   writable and dying on the writer lock; streamlit-freshness legitimately red (running
   code 505h stale). **This fleet needs its own workstream** — it is a separate,
   older incident that turned this one silent.

## 4. Root-cause fix set (F1 scope)

**Fix A — maroon-jump with durable gap flag (code, the actual bug).** In the tick
pipeline: when a bounded pass reads zero rows AND the dataset's oldest surviving raw
partition begins beyond the read ceiling (the whole `(cursor, cursor+W]` range is
provably below the surviving tape), jump the cursor to the start of the oldest
surviving tape and append a **gap record to the capture `_gaps` manifest** (the
existing flag-don't-drop channel `capture_core/maintenance._persist_gaps`, already
consumed by the label builder) covering the jumped interval. The system then
self-heals from tape loss with an honest, downstream-visible gap instead of a silent
treadmill — including on deploy for the current stall (the four marooned cursors jump
on the first tick; no manual cursor surgery). The genuine-quiet-market path (tape
exists, no rows) is untouched and pinned by test.

**Fix B — frontier watch (code, the operator's explicit ask).** Extend
`monitoring/substrate_freshness.py` with per-dataset
`MAX(window_end_ns)` samples (`brain/frontier/<dataset>`) from `snapshot_bookkeeping`
— the exact signal discovery consumes — with per-dataset-class thresholds (dense
~2h; REST-derived ~6h — healthy REST frontiers lag up to ~3.5h, a single 900s
constant would flap), plus per-reader `last_recv_ts_ns` samples
(`brain/recv/<reader>`) as the earlier-warning symptom. Delivery rides the existing
green service (no unit change; next 5-min fire picks up merged code).

**Deploy steps (operator GO at the gate):** merge → restart `mhde-brain-tick`
(picks up Fix A; cursors jump; ingestion resumes at today's tape) → watch the
two-probe frontier delta go positive → substrate-freshness picks up Fix B on its
next fire.

**Not in this fix (operator decisions / separate workstreams):**
- **Disk headroom** — the chronic precondition stands (99G/150G used, free 45G < 50G
  floor; guard pruning daily; inodes 81% and warning). Without headroom the system
  now degrades to *honest daily gap flags* instead of silent stalls whenever lag
  exceeds the shrunken tape, but the pressure decision (grow volume / explicit
  retention cuts / move a tree — capture_core 30G, capture_core_okx 20G, brain 25G
  incl. 9.2G registry.sqlite) is the operator's (KI-164).
- **Monitor-fleet resurrection** (§3.4) — its own round.
- **Guard-side gap manifests** (the pruner recording what it deletes) + reader-skip
  gap flags — the tracked gap-handling workstream ("skipped fragment is a gap").
- The 08-15→08-20 dense gap is permanent; discovery/labels already structurally treat
  it as missing (no snapshots ⇒ no labels ⇒ no instances). F4's un-expire one-off
  (already queued) remedies the rules expiry wrongly killed during it.

## 5. Contributing causes, ranked

1. Disk free pinned below the guard's soft floor (KI-164) → retention collapse.
2. Quiet-gap skip advances slower than wall clock with no maroon detection (the
   treadmill; this fix).
3. Discovery-era memory load (6h cadence + 11.3G passes + shadow run on a 22G host)
   → OOM kills + tick stretch → cursor lag beyond the shrunken tape.
4. Monitoring blind spots: liveness-shaped signals (`updated_at_ns`, `max_lag`)
   instead of progress-shaped signals (frontier), plus a dead alert fleet.
5. Inode-guard write halts dropping raw data (separate loss windows).

Evidence index: registry/reader probes (scratchpad `probe_registry_frontier.py`,
investigator scripts), brain-tick + capture shard + discover journals 08-14→08-20,
`disk_guard.py:113-241`, `capture_core/config.py:258-326`, `pipeline.py:105-167`,
`runner.py:220-252`, `reader.py:225-416`, `labels.py:162-168`, `retention.py:92-113`,
KNOWN_ISSUES KI-164/KI-165, data/logs/*.log for the monitor fleet.
