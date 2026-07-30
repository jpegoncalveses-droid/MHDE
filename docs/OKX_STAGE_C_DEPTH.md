# OKX Capture — Stage C (depth / order book): design & retention

Stage C adds the **depth** source to `data/research/capture_core_okx` via the shared store,
byte-identical to the Binance depth schemas, venue seam in the WS client only. It is a **separate,
not-enabled** daemon (`mhde-capture-okx-books.service`) — Stage B's `mhde-capture-okx-ws.service`
is already live on the host, so folding depth in would make it live on the next restart.

## Two datasets, one channel

The OKX public per-instId **`books`** channel (400 levels) is **self-seeding in-band**: the first
push is `action:snapshot` (`prevSeqId == -1`) with the full ladder, then ~100 ms `action:update`
diffs carrying `seqId`/`prevSeqId`. This **eliminates the Binance REST snapshot broker entirely**
(no `/fapi/v1/depth` fetch, no snapshot-owner socket, no `depth_snapshot` dataset). It routes
through the Stage B seam with **zero `ws_client.py` change** (same per-instId `arg.instId` shape as
`bbo-tbt`; the `chunk_subscribe_args` 64 KiB chunking absorbs the extra args). Sizes are OKX
contracts → reuse Stage B `ctVal`.

| dataset | brain reads it? | source | bound |
|---|---|---|---|
| **`depth_state`** | **yes** (`read_new_depth_state`, top-20) | a seqId/prevSeqId maintainer (`book_okx.py`) sampled every 5 s | **2-day retention + hourly compaction** |
| `depth` (raw 400-level tape) | **no** | `okx_books_row` per frame | **not persisted by default** (byte monster) |

## Decision — maintainer-only (do NOT persist the raw `depth` tape by default)

The live gate measured the write rate (8 symbols, 120 s, then scaled to the full 273-symbol universe):

| dataset | rows/s (probed) | **GB/day (full universe)** | files/day (pre-compaction) |
|---|---|---|---|
| raw `depth` | 64 | **~26 GB/day** | ~0.78 M |
| `depth_state` | 1.5 | **~2.7 GB/day** | ~0.78 M |

The brain **never reads raw depth**. At ~26 GB/day, even a **1-day** raw tape is ~half the current
**52 G free** alongside Binance's live 39 G capture — it brushes/exceeds the 50 GiB disk soft-floor.
So the default is **`--persist-raw-depth` OFF**: process every `books` update in-memory for
`depth_state`, but do not write the raw ladder tape. `depth_state` alone satisfies "byte-identical
to the Binance depth schema" (it *is* `DEPTH_STATE_SCHEMA`). **Operator veto point:** the flag +
`DEPTH_RAW_RETENTION_DAYS=1` + `--depth-days 1` exist so a raw tape can be enabled later — but only
after a disk resize or on the 258-sym overlap scope (the gate number that gates it is above).

## Retention — two monsters, two bounds, wired from day one

- **`depth_state` = inode monster** (~0.78 M files/day uncompacted; Binance's own depth_state is
  1.97 M files at 2 d today — the KI-159 fragment bug that keeps the brain DEPTH source deferred).
  Bound: (a) **2-day retention** via the existing `expire_depth_state_partitions` (runs against
  whatever `--root`); (b) **hourly compaction** — Stage C adds `depth_state` to the closed-hour
  compactor via a new `--include-depth-state` flag, folding ~0.78 M files/day → ~1 file/partition-
  hour (~6.5 k/day). *This is also the fix that would re-admit the Binance DEPTH source — flagged
  as a follow-up, out of Stage C scope.*
- **raw `depth` = byte monster.** If ever persisted: `expire_depth_partitions` prunes only the
  `depth` dataset at `--depth-days 1` (Binance keeps depth at the shared 7 d, untouched).

**Cadence reuses the existing OKX timers** (expire daily 00:25, compact hourly :26) — the two
`ExecStart` lines just gain `--depth-days 1` / `--include-depth-state`; no new timer files.

## ⚠️ Pre-deploy: Stage B's OKX firehose maintenance is NOT enabled

Task-2 host check: `mhde-capture-okx-firehose-expire/compact` timers are `not-found`/inactive.
The **live Stage B OKX firehose is already fragmenting unbounded** (5.5 k+ files/day, no expiry).
Those timers (shipped BUILT-NOT-DEPLOYED in PR #80) **must be enabled regardless of Stage C**, and
Stage C's depth daemon must not be enabled until both its own and Stage B's maintenance are active.

## Gate result — PASS

8 symbols, 120 s, `--persist-raw-depth` ON, read back through the real `read_new_depth_state`:
**8/8 books synced, 0 reconnects, 0 frame_errors**; `depth_state` ≥ 20 levels/side, `update_id`
monotonic per symbol, `valid` all True, cursor advances. Write rate + projection as above.

## Resolved open questions
- **Persist raw depth?** No (maintainer-only default) — see decision above.
- **Separate daemon vs fold into Stage B?** Separate (`mhde-capture-okx-books.service`), not enabled
  — Stage B's unit is already live.
- **depth_state cadence:** keep Binance 5 s (parity).
- **`e` on raw depth:** constant `"depthUpdate"` (no consumer; cosmetic).
- **level shape:** `[px, sz, liqOrders, numOrders]` — trailing two dropped, confirmed at the gate.
- **Follow-ups (out of scope):** books-connection sharding for the full universe; periodic
  re-subscribe integrity cross-check (checksum is deprecated/0); KI-159 back-port of
  `--include-depth-state` to the Binance compact unit to re-admit the brain DEPTH source.
