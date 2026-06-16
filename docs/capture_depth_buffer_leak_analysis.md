# capture-core depth-buffer leak — root cause + fix

**Date:** 2026-06-16
**Status:** Fixed (branch `fix/capture-depth-buffer-bound`); the real validation is the
post-merge re-measure (see below).
**Relates to:** ADR-039 (multi-process sharding), ADR-038 (write-then-compact).

## Symptom

In the §G sharded firehose trials, each shard's resident heap (cgroup `anon`) **and** CPU
grew **monotonically with uptime, with no plateau**, until all cores saturated and the
websocket keepalive pings timed out (`ConnectionClosedError 1011`) into a disconnect storm:

| Trial | anon trajectory | outcome |
|---|---|---|
| N=3 (06-15) | 0.72 → 1.12 GiB over ~50 min | 3 loops pegged 100%, storm began ~T+28 min |
| N=12 (06-16) | 1.61 → 3.11 GiB, ~+33 MiB/min over ~66 min | all 8 cores pegged at ~T+38 min, 86 disconnects |

The growth was **N-independent** (per-shard), so the cause is a **per-symbol / per-message
structure that accumulates without bound** — not an intrinsic per-symbol CPU cost. The cold
per-shard reading (T+0.5 min, before buffers accumulate) was ~25%/shard (~3 cores for all 527
symbols, `%wait`~1%).

## Root cause

`DepthMaintainer` (`crypto/research/capture_core/book.py`) is a **cursor-only** maintainer —
it records nothing itself (the service writes every raw diff to parquet at `service.py:291`)
and reconstructs no bid/ask levels; it only tracks update-id continuity to detect gaps. Its
one piece of growing state is `_buffer` — a plain `list` of diffs held **while awaiting a REST
snapshot to sync**.

Two holes let `_buffer` grow forever:

1. **Never-seeded → permanent leak.** A `DepthMaintainer` only leaves the unbounded-append
   state when a snapshot lands (`book.py` `on_snapshot` sets `last_update_id`). On the sharded
   path a failed seed was **counted and dropped, never retried** — all three failure branches
   of `SnapshotClientScheduler.run` hit `finally: self._pending.discard(symbol)`
   (`snapshot_owner.py`). The book then sits at `last_update_id is None` forever: every diff
   appends to `_buffer`, and the never-synced `on_diff` branch returned a bare `SyncResult()`,
   so it **never re-requested** a seed. The scheduler docstring's promise *"the book
   re-requests it on its next resync"* is vacuous — a never-synced book has no resync.

2. **Re-armed under the storm.** A continuity break (`pu != last_u`) nulls `last_update_id`
   again (`_enter_resync`), dropping even a previously-synced symbol back into the
   unbounded-append state. It *does* re-request (via `needs_snapshot`), but the re-snapshot is
   paced (~1/s) through the single shared owner, so during the 1011 storm the backlog can't
   keep up and the unsynced window — and the buffer — keeps stretching. Positive feedback:
   CPU saturation → late diffs → more continuity breaks → more resyncs → bigger snapshot
   backlog → bigger buffers → more heap → more GC → more CPU.

**Why CPU tracked memory:** for a symbol that has a snapshot but hasn't yet hit the sync
boundary, `_try_sync` runs every diff with two O(len(_buffer)) scans (the stale-drop
comprehension + the boundary `enumerate`/`next`) → O(n²); and the multi-million-object live
heap inflates gen-2 GC cost regardless.

## Fix (TDD, 3 parts)

1. **Bound the buffer (load-bearing).** `_buffer` is a `collections.deque(maxlen=…)`; appends
   auto-evict the oldest while unsynced. Only diffs near the `lastUpdateId` boundary can sync,
   so old entries are pure waste. `_try_sync` works on a list snapshot then stores back a
   bounded deque, preserving the exact sync behaviour (the 6 existing book tests still pass).
   `config.CAPTURE_DEPTH_BUFFER_MAXLEN = 5000`.
2. **Durable seeding.** `SnapshotClientScheduler.run` re-queues a failed seed with **capped
   exponential backoff** instead of discard-and-forget, **without blocking the loop** (a
   delayed re-enqueue task — a blocking sleep would stall other symbols' seeding, which the
   `test_seed_loop_survives_a_malformed_or_raising_owner_response` test forbids). The symbol
   stays in `_pending` so `request()` still dedups. `config.CAPTURE_SEED_RETRY_BACKOFF_INITIAL_S
   = 1.0`, `CAPTURE_SEED_RETRY_BACKOFF_MAX_S = 60.0`.
3. **Close the re-request hole.** The never-synced `on_diff` branch now raises
   `needs_snapshot=True` once the buffer passes `config.CAPTURE_UNSYNCED_RESEED_THRESHOLD =
   1000` (< maxlen, so the re-request fires before eviction starts; the scheduler dedups).

Worst-case maintainer RAM is now bounded: `CAPTURE_DEPTH_BUFFER_MAXLEN × symbols-per-shard ×
~200 B/_Diff` (e.g. 5000 × 44 × 200 B ≈ 42 MiB/shard, all symbols maxed) — versus the
unbounded multi-GiB leak it replaces.

## Validation

5 new tests (RED→GREEN), full capture suite green (287 passed). **But the decisive validation
is operational:** re-run the §G measurement with this fix and confirm per-shard heap **plateaus**
(no `+33 MiB/min` climb) and per-shard CPU stays flat at the cold ~25%/shard. If it does, the
firehose's true cost is ~3 cores for 527 symbols and the earlier "cost grows with uptime until
it saturates any N" conclusion was substantially this leak — which would dissolve the
core-budget collision rather than confirm an intrinsic wall. **Re-measure before spending cores
or raising N.**

## Known residual

The non-sharded `SnapshotScheduler` (`snapshot.py`, single-process legacy path) has the same
discard-on-failure shape; not fixed here because the firehose runs the sharded path. The
bounded buffer (part 1) protects both paths regardless, since it lives in `DepthMaintainer`.
