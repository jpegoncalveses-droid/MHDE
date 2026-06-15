# Multi-process capture sharding — design analysis

**Status:** **ACCEPTED — ADR-039** (2026-06-15). The multi-process approach is
accepted; the shard/core count **N is a configuration parameter** (`CAPTURE_N_SHARDS`,
default **3** — Option B below), set by the operator against the live engine's reserved
cores. **No implementation yet.** The firehose stays **parked** until the
implementation lands and the measured trial (§G) passes.

**Date:** 2026-06-14 (analysis); **accepted 2026-06-15** after the parse-vs-write CPU
profile (§0.1) confirmed multi-process is necessary and the thread-offload interim is
insufficient. **Author:** capture-core workstream. Builds on ADR-038
(write-then-compact, merged) — which fixed the file/inode and writer-RAM problems
but exposed the CPU-scaling limit resolved here.

---

## 0. The measured problem (grounded)

The ADR-038 Stage-B trial (2026-06-14, full 527-symbol firehose) showed:

| Signal | Measurement |
|---|---|
| Firehose CPU | **~110% = one full core, `R` state** (93:55 CPU-time / ~96 min) |
| Box | **8 cores**, load avg 2.76 — *7 cores idle* |
| Architecture | **single process, single asyncio event loop** (`conn_manager.run` → `asyncio.gather` over 9 shards, `conn_manager.py:173`) |
| Flush | parquet **zstd write runs synchronously on the event loop** (`service.py:355` `_flush_loop` → `w.flush_due()`) |
| Disconnects | **251 "timed out during opening handshake"**, 98 "no close frame", **zero 429/418** |
| Rate | ~4–5 reconnects/min, **sustained the whole run** |
| Memory | anon heap ~816 MB flat (not the issue — see ADR-038) |

**Diagnosis:** one event loop does message parse + depth-maintenance + synchronous
zstd writes for ~5k msg/s (depth@100ms × 527 + aggTrade + bookTicker + markPrice),
saturating **one core**. Binance is **not** throttling (no 429/418); the saturated
loop simply cannot service a reconnecting shard's TLS/WS opening handshake within the
timeout → handshake times out → reconnect → each gap triggers a depth **resync
snapshot** → *more* load → self-sustaining storm → continuous data gaps. The box has
7 idle cores the single process cannot use.

**Fix:** split the load across **multiple processes**, each with its own event loop
on its own core. This document designs that — and, per operator direction, presents
the **CPU core split as a choice**, because the live trading engine must keep
guaranteed cores.

### 0.1 Parse-vs-write CPU profile (2026-06-15) — resolves "what saturates the core?"

A py-spy self-time profile (**30,083 on-CPU samples, 150 s steady-state**; firehose run
as a py-spy child in a memory-capped transient unit, since `ptrace_scope=1` blocks
attach to the systemd-managed service + no passwordless sudo) breaks the saturated core
down:

| Category | Self-time | Nature |
|---|---:|---|
| ws recv / TLS / frame parse | 25.0% | event-loop thread, GIL-bound |
| write/buffer — `store.py` bookkeeping | 23.3% | pure-Python, GIL-bound |
| json parse (`raw_decode`) | 13.4% | GIL-bound |
| ws inbound decompress (permessage-deflate) | 10.3% | GIL-bound |
| pyarrow/zstd write | 11.1% | **releases the GIL** |
| asyncio loop | 10.2% | GIL-bound |
| depth-book / other | 6.8% | GIL-bound |

The process burned **~1.19 cores** (the >1.0 is the zstd/pyarrow GIL-release spilling
onto a second core). Grouped: **inbound receive (recv + decompress + json) ≈ 49%**, all
on the single event-loop thread; **write/serialize ≈ 34%**, of which **only ~11%
releases the GIL**.

**Verdict — this decides §F's open alternative:** **multi-process sharding is
necessary; a write-offload thread is NOT sufficient.** A thread can only relieve the
~11% that releases the GIL; the other **~72%** (≈49% inbound + ≈23% pure-Python
`store.py`) holds the GIL on one thread, and threads cannot parallelize GIL-bound
Python. Only separate processes (separate GILs) split the inbound recv/decompress/parse
across cores.

The profile also surfaced two near-free pure-Python wins on the writer hot path
(~13% of one core), **shipped pre-sharding in PR #31 (merged, `0354fe8`)**: `_date_str`
cached by epoch-day (17.9×) and `_estimate_row_bytes` repr-free typed sizing (1.6× on
depth rows). So the per-core sizing measured in the §G trial reflects the optimized hot
path.

---

## 1. (A) CPU budget — an operator choice, not an assumption

The 8 cores are shared by: the **live trading engine** (`crypto-trading-engine`, 1 s
decision cycle — needs guaranteed, low-latency cores), the **MHDE prediction
pipeline** (the daily 22:00–00:50 UTC crypto chain — bursty, CPU-heavy while it
runs), the **light capture collectors** (klines + REST present-state — minimal), the
**OS**, and the **firehose**. Capture must **never** starve trading.

So the question is: **how many cores does the firehose get, and how are they
isolated from the engine's?**

**Two isolation mechanisms (use both):**
- **`cpuset` pinning** (`AllowedCPUs=` per systemd unit) — capture processes run
  *only* on a designated set of cores, **disjoint from the engine's**. This is a
  *hard* guarantee: capture physically cannot touch the engine's cores, contention or
  not. Stronger than weights.
- **Compute deprioritization** (the existing `CPUWeight=20` + the cross-slice
  `user.slice` drop-in + `Nice`) — so even within capture's cores, anything sharing
  them yields to higher-priority work.

**The choice (rough per-core steady-state load — the trial confirms exact numbers):**
Steady-state load is **lower than today's saturated core**, because once no loop is
saturated the reconnect/resync storm disappears (the storm is *caused by* the
saturation; removing it removes the resync-snapshot + handshake-retry overhead).

| Option | Capture cores | ~symbols/core | Est. steady-state per-core load | Cores left for engine+pipeline+OS | Notes |
|---|---|---|---|---|---|
| **A — trading-first** | **2** | ~264 | ~60–80% | **6** | Breaks saturation, but tight; a volatility spike could re-saturate a core. |
| **B — balanced (recommended)** | **3** | ~176 | ~40–55% | **5** | Comfortable headroom, storm-free, absorbs spikes. |
| **C — capture headroom** | **4** | ~132 | ~30–40% | **4** | Room for universe growth / 2× spikes; only if trading needs ≤4 cores. |

> The estimates assume the message-processing cost scales ~linearly with symbol
> count and that ≥2 non-saturated loops eliminate the storm. **The measured trial
> (§G) confirms the real per-core load and the minimum core count** before any
> re-enable. **N is a configuration parameter** (`CAPTURE_N_SHARDS`), default **3**
> (Option B); the operator sets the final value and the matching cpuset core map
> against the engine's core needs. This table is the guidance for choosing it.

---

## 2. (B) Partition strategy — split symbols across processes

- **Split by symbol**, not by stream: each process owns a **disjoint subset of the
  universe** and runs that subset's per-symbol streams (`aggTrade`, `depth`,
  `bookTicker`). Because partitioning is `symbol=/date=`, a per-symbol partition is
  then written by **exactly one** process — no cross-process contention on a partition
  for those datasets.
- **Load-balance by volume, not symbol count.** `depth@100ms` dominates and varies
  by symbol liquidity. Assign symbols by a **stable hash** (`hash(symbol) % N`) for
  determinism across hourly universe re-resolves (so a symbol never silently migrates
  between processes), and optionally volume-weight the buckets using recent
  `bytes_in` per symbol (the conn_manager already tracks `bytes_in`).
- **Market-wide array streams** (`!markPrice@arr`, `!forceOrder@arr`) deliver **all**
  symbols on one stream and cannot be split. Assign them to **one** process (they are
  cheap: markPrice @1 s, forceOrder only on liquidations). That process writes the
  `markPrice`/`forceOrder` partitions for *all* symbols — a different *dataset* dir
  from the per-symbol owner's `aggTrade`/`depth`, so still no collision.
- **`depth_snapshot` REST seeding**: each process seeds **its own** symbols — but the
  REST weight is shared (see §F, a real coordination risk).

---

## 3. (C) Multi-process writer — `part-<shard>-*` naming, ADR-038 compatible

Each process constructs its own `RawDatasetWriter`s (`store.py`) writing under the
**same** `symbol=/date=` root. Two requirements:

1. **No filename collision.** The writer already names files `part-<uuid>.parquet`
   (`store.py:235`); uuids are globally unique, so even if two processes wrote the
   same partition they could not clobber a file. To make it **legible and
   debuggable**, thread a **shard/process id** into the name:
   `part-<shard>-<uuid>.parquet`. (One new constructor arg on the writer; the live
   write path is otherwise unchanged.)
2. **ADR-038 compaction stays compatible.** The closed-hour compactor selects writer
   files by prefix `part-` (`maintenance.py` `_writer_parts_with_mtime` →
   `n.startswith("part-")`). `part-<shard>-<uuid>` still matches `part-*`, so the
   compactor merges **all processes'** parts of a closed hour into one
   `compact-h<hour>-*` per partition. The three-way namespace from ADR-038 holds:
   writer `part-<shard>-*`, hourly `compact-h*`, offline migration
   `compact-migrated-*`.

Because the symbol split is clean (one process per per-symbol partition), most
partitions have a single writer anyway; the discriminator is belt-and-suspenders for
the array-stream datasets and for the brief window after a universe re-resolve.

---

## 4. (D) Compaction — one shared hourly timer

**Preferred: a single compaction timer** (`mhde-capture-firehose-compact`, unchanged
from ADR-038) that merges *all* processes' `part-*` per partition. Rationale:
- It already operates **per `symbol=/date=` partition**, bucketing by **flush
  (mtime) hour** with the `CAPTURE_COMPACTION_GRACE_S` (300 s) margin. That boundary
  safety holds **regardless of which process wrote a file** — the grace margin
  (≫ the 30 s flush) guarantees every writer is done with a closed hour before it is
  compacted, across all processes.
- One timer is simpler than per-process timers and avoids two compactors racing the
  same partition. (Per-process compaction would need per-process partition ownership,
  which the array streams break.)

No change to the compactor is required beyond accepting `part-<shard>-*` inputs
(already covered by the `part-*` prefix match).

---

## 5. (E) Supervision — systemd template + cpuset, per-process guards

- **`mhde-capture-core@.service` template**, one instance per shard-group
  (`@0`,`@1`,`@2`), each: `AllowedCPUs=<its core>`, the ADR-036 caps
  (`MemoryMax`, `CPUWeight=20`, `IOWeight=20`, `OOMScoreAdjust=800`), `Restart=on-failure`.
  Pass the instance's shard id + N (total) via `%i` so each computes its symbol
  subset by `hash(symbol) % N == i`.
- **`mhde-capture-core.target`** groups the instances for one-shot enable/disable.
- **Guards per-process.** The inode + byte guards read shared filesystem state, so
  each process can run its own (cheap `statvfs`); under pressure they all halt their
  own writes together (acceptable — forward-only). Compaction stays a **single**
  shared timer (not per-process).
- **Fix the shutdown timeout.** Raise `TimeoutStopSec` (currently 30 s — the trial's
  SIGKILL/`failed` on stop came from the saturated loop not draining in time) and/or
  make `CaptureService.stop()` promptly cancel shard tasks before `flush_all`. With
  un-saturated loops this is far less likely, but a clean stop matters for parking.

---

## 6. (F) Risks, open questions, honest alternatives

| Risk / open question | Take |
|---|---|
| **Shared-IP REST budget** (`CAPTURE_REST_WEIGHT_PER_MIN=1200`, `FAPI_WEIGHT_LIMIT=2400` shared with engine + rest-collector). N processes each seeding depth snapshots multiplies the request rate → 429/418 → the very throttling we don't have today. | **Must coordinate.** Options: (a) keep **all** depth-snapshot seeding in ONE process (a "snapshot owner"); (b) a shared token-bucket (file-lock / small coordinator) dividing the budget across processes. (a) is simplest; revisit if one process can't seed all symbols fast enough. **The biggest open question.** |
| Symbol→process stability across hourly re-resolve | Stable `hash(symbol) % N`; never migrate a live symbol. A new symbol lands deterministically; worst case a brief 2-writer window that compaction absorbs. |
| Changing N (re-sharding) re-maps every symbol | Treat N as fixed config; re-shard only on a deliberate restart of the whole target. |
| Array streams can't be split | One process owns them (cheap); that process carries slightly more load — account for it in the balance. |
| cpuset core map vs the engine | The operator sets `AllowedCPUs=` to cores **disjoint** from the engine's; this is the §A choice. |

**Honest alternatives (and why multi-process wins):**
- **Reduced-universe interim** — capture only the top-N liquid symbols on one process
  (fits one core, stable). *Pro:* trivial, immediate, stable data. *Con:* partial
  coverage — the brain loses the long tail. Good as a **stopgap** while sharding is
  built; not the end state.
- **Coarsen depth 100ms→500ms** — ~5× less load, likely fits one core. *Con:*
  reverses the "no pre-coarsen" operator GO and loses microstructure resolution.
- **Offload only the zstd writes to a thread pool** (`asyncio.to_thread` on
  `flush_due`; pyarrow/zstd releases the GIL during compression) — unblocks the loop
  for the *write* portion **without** multi-process. *Pro:* much smaller change.
  *Con:* **MEASURED INSUFFICIENT (§0.1).** Only ~11% of the core releases the GIL; the
  dominant **~72%** (inbound recv/decompress/parse + pure-Python `store.py`) holds the
  GIL on the loop thread, which threads cannot parallelize. A write-offload thread
  cannot break this saturation — **ruled out** as more than a marginal tweak. Only
  multi-process (separate GILs) or reduced load helps.

---

## 7. (G) Recommendation, implementation outline, trial plan

**Recommendation (ACCEPTED — ADR-039):** **multi-process sharding**, with **N a
configuration parameter** (`CAPTURE_N_SHARDS`, default **3** = Option B); the operator
sets the final N and the cpuset core map against the engine's needs. Split symbols by
stable volume-weighted hash; one process owns the array streams **and** all
depth-snapshot REST seeding (simplest budget coordination); single shared compaction
timer; systemd template + `AllowedCPUs` pinning disjoint from the engine; capture
compute-deprioritized so it can never starve trading. The parse-vs-write split is now
**measured (§0.1):** multi-process is required and the thread-offload interim is **ruled
out** (it relieves only the ~11% GIL-releasing writes). The two pure-Python hot-path
wins are already **shipped (PR #31, merged)**.

**Cleanly implementable on the current code?** Mostly:
- `conn_manager` / `service` already run a clean event loop per process — instantiate
  N of them with disjoint stream sets (a `shard_id`/`n_shards` arg selecting symbols).
- Writers need a `shard_id` for the `part-<shard>-*` filename (one arg).
- Compaction is unchanged (matches `part-*`).
- New: the systemd template + cpuset, the symbol-hash splitter, and the
  snapshot-owner/REST-budget coordination (the real new work).

**Implementation outline (LATER — not this PR):**
- `config.py` — `CAPTURE_N_SHARDS`, optional `CAPTURE_SHARD_CPUSETS`; raise
  `TimeoutStopSec`.
- `service.py` — accept `shard_id`/`n_shards`; resolve the symbol subset by stable
  hash; only the shard-0 (or designated) process runs array streams + snapshot seeding.
- `store.py` — `shard_id` → `part-<shard>-*` filename.
- `main.py` — `capture-core-run --shard I --of N`.
- `systemd/` — `mhde-capture-core@.service` template + `mhde-capture-core.target` +
  `AllowedCPUs`, built-not-deployed.
- Tests (TDD) — stable symbol partition (disjoint, covers universe); multi-writer
  compaction merges `part-<shard>-*`; one-snapshot-owner REST budget respected.

**One controlled measured trial before any re-enable:**
1. Start N processes pinned to N cores (capture's cpuset), light load first.
2. Measure **per-core CPU < ~60%**, **handshake-timeout reconnects ≈ 0** (the storm
   gone), **gap rate near zero**, anon RSS per process, and that the **engine's 1 s
   cycle is undisturbed** (its reserved cores untouched).
3. Re-enable only if per-core load and the gap rate land within target; else add a
   capture core (operator budget permitting) or trim the universe and re-measure.

---

## Appendix — what is NOT changing

- ADR-038 write-then-compact (writer flush + closed-hour compaction) — reused as-is;
  `part-<shard>-*` is compaction-compatible.
- `symbol=/date=` event-time layout, `recv_ts_ns` cursor, per-stream schemas.
- The inode + byte guards, depth @100 ms (unless the operator chooses the
  coarsen alternative), 7-day retention.
- The engine and prediction pipeline keep **reserved cores** — capture is pinned off
  them and stays compute-deprioritized.
