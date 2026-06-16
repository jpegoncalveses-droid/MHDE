# ADR-039 Stage 2 — capture-core multi-process orchestration (design spec)

**Status:** ACCEPTED — Stage-2b code (owner/socket/throttle/header-gate/shard-run-path) merged,
and **gap 3 (sharded systemd units + sd_notify supervision) implemented BUILD-ONLY** (units
tracked, not enabled). Firehose stays **parked**; enable is deferred to gap 4 (cpuset) + the §G
re-enable. The leak that blocked the trial was root-caused and fixed (PR #39: bounded
`DepthMaintainer._buffer` + durable seeding), and the §G re-measure confirmed the core-budget
collision dissolves (true cost ~3–4 cores).

**As-built reconciliation (gap 3):** units are `mhde-capture-owner.service`,
`mhde-capture-core@.service`, `mhde-capture.target` (N pinned at **8** in the shard ExecStart; the
§B core-map of 3 is superseded — N is revisited at gap 4 before any enable). The shard→owner edge
is **soft `Wants=`/`After=`, NOT `Requires=`** (§A resilience: an owner crash-loop must not cycle
the shards). `Type=notify` is satisfied by a **raw `NOTIFY_SOCKET` datagram** (no `sdnotify` dep)
in `crypto/research/capture_core/sd_notify.py`: shard `READY` at manager-up + `WATCHDOG` fed from
the flush loop only while messages flow; owner `READY` at socket-bind + a steady time-based
`WATCHDOG` keepalive. `AllowedCPUs` is intentionally absent (gap 4). Capture-family `--user`
convention kept (no `User=`). REST stays mainnet (no env override).

**Date:** 2026-06-15. **Author:** capture-core workstream. Builds on **ADR-039** (accepted —
multi-process sharding, N a config parameter) and **ADR-038** (write-then-compact) and **ADR-037**
(inode/compaction) and **ADR-036** (resource model). Stage 1 (shard-aware writer + symbol splitter,
PR #32) is merged. This Stage 2 is the **orchestration**: N processes on disjoint cores, one
snapshot-owner for the shared REST budget, supervision, then the trial.

This spec was grounded in a **read-only host inspection (2026-06-15)** and an independent
design synthesis (three competing approaches judged); every host claim below was verified live and
is cited in §0. The single most consequential finding — that **`AllowedCPUs=` on a `--user`
capture unit is a silent no-op on this host** — was confirmed directly against the cgroup tree.

---

## 0. Host facts (measured this session, read-only — the design is grounded in these)

| Fact | Measured value | Source |
|---|---|---|
| Cores | **8** (0–7), single socket, 1 thread/core, **no `isolcpus`/`nohz_full`/`rcu_nocbs`** | `nproc`; `/proc/cmdline` |
| Engine units | `trading-engine-monitor` (live ~1s cycle), `trading-engine-entry` — **`system.slice`** | `systemctl list-units` |
| Engine pinning | **`AllowedCPUs=` EMPTY**, `CPUWeight` not set (=100), `MemoryMax=infinity` | `systemctl show trading-engine-*` |
| Capture units | `mhde-capture-core/klines/rest-collector` — **`--user`, `app.slice`**, `CPUWeight=20`, `OOMScoreAdjust=800` | `systemctl --user show mhde-capture-*` |
| Capture pinning | **`AllowedCPUs=` EMPTY** on every capture unit | same |
| ADR-036 drop-in | **installed**: `/etc/systemd/system/user.slice.d/10-capture-deprioritize.conf` → `[Slice] CPUWeight=50 IOWeight=50` | `cat` |
| **cpuset on engine side** | `system.slice/cgroup.controllers` **includes `cpuset`**; root `subtree_control` includes `cpuset` → **engine `AllowedCPUs` WILL enforce** | `/sys/fs/cgroup/system.slice/cgroup.controllers` |
| **cpuset on capture side** | `user@$(id -u).service` **`DelegateControllers=cpu memory pids`** (NO cpuset); `user.slice` + `app.slice` `cgroup.controllers = cpu memory pids` → **`AllowedCPUs` on a `--user` capture unit is a SILENT NO-OP** | `systemctl show user@.service -p DelegateControllers`; `/sys/fs/cgroup/.../app.slice/cgroup.controllers` |
| REST budget | `FAPI_WEIGHT_LIMIT=2400` **REQUEST_WEIGHT**/min (shared IP, engine + rest-collector + capture; the 1200 is the *separate* ORDERS bucket, **not** this cap); depth snapshot = `/fapi/v1/depth` weight **20** @ limit 1000; owner budget = cap − reserved headroom (Stage 2a default **~1400/min**); 527-symbol cold start = 527×20 = **10,540 weight ≈ 7.5 min** | `config.py:107`, stage-2a `CAPTURE_SNAPSHOT_RESERVED_HEADROOM_PER_MIN` |
| Raw-diff durability | `service.py` stores **every** raw depth diff unconditionally, independent of maintenance state | `service.py:255` |
| Snapshot seam | shard scheduler construction already gates on `enable_snapshots`/`snap_scheduler is not None`; the two `request()` call sites are resync `service.py:278-279` + seed `service.py:281-286` | `service.py:213-221` |

**The decisive constraint (verified):** today **nothing is cpuset-pinned** and the `--user`
manager **cannot** program `cpuset.cpus` on its children. So "provably disjoint cores" is not a
unit-file one-liner — it requires a **sudo, system-level change on both sides** (§B). Any design
that asserts `AllowedCPUs` on a `--user` unit "just works" is wrong on this host.

---

## A. Snapshot-owner + IPC — how N−1 shards seed without each hitting the REST budget

**Recommendation: a single dedicated snapshot-owner process, sole holder of `/fapi/v1/depth`
weight, brokering snapshots to shards over a request/response unix-domain socket.** Shards never
call REST.

### Mechanism
- A new unit `mhde-capture-snapshot-owner.service` (a **separate role, NOT shard 0**) runs the
  **only** `SnapshotScheduler` (`snapshot.py`) in the deployment, wrapping the existing
  `CaptureRestClient.fetch_depth_snapshot` (`snapshot.py:70-71`), paced by the unchanged
  `SNAPSHOT_MIN_INTERVAL_S=1.0s` and deduped by the existing `_pending` set (`snapshot.py:48,55`).
- Shards run `service.py` with `enable_snapshots=False` and a `snap_scheduler=<IPC stub>` that
  presents the same `.request(symbol)`/`.run()`/`.stop()` interface. This **reuses the existing
  injection seam** (`service.py:213-221`); the two request call sites (`service.py:278-279`,
  `281-286`) redirect to the stub with **no other `service.py` logic change**.

### IPC — a unix socket (not a spool dir, not a flock token-bucket)
- `SOCK_STREAM` unix socket at a fixed path under the capture root, e.g.
  `data/research/capture_core/.ipc/snapshot-owner.sock` (same filesystem the shards already write;
  recreated on owner start; no new mount). Newline-delimited JSON: request `{symbol}` →
  `{symbol, lastUpdateId, E, T, bids, asks}` | `{symbol, error}` | a `pending` ack.
- **Why socket over an on-disk spool:** the owner must **return** the ~1000-level depth payload so
  the shard writes its **own** `depth_snapshot` row (`service.py:266` → `self._snapshot.append(...)`)
  into **its own** `symbol=/date=` partition — preserving the one-writer-per-partition invariant and
  `part-<shard>-*` naming (`store.py:238,278`). A spool of ~1000-level JSON blobs reintroduces the
  transient-intermediate-file **inode pressure that caused the 2026-06-09 ADR-037 outage**; the
  socket keeps large transient payloads off disk. Auth = filesystem perms (same user, no network).
- **Why an owner over a shared flock token-bucket:** a bucket cannot return a payload (each shard
  would still issue its own HTTP GET → N processes holding REST weight, the exact hazard), and
  `flock` is **not FIFO** — an N-shard simultaneous-resync storm can starve one shard's symbols for
  an unbounded interval. A single global queue is naturally **fair** and **globally dedup'd**.

### Flow
1. Shard computes its subset via `symbols_for_shard(universe, shard_id, N)` (`sharding.py:36`).
2. On startup (`seed_universe`, `service.py:281-286`) and on every resync (`service.py:278-279`),
   the shard sends `{symbol}` over the socket instead of pacing locally.
3. Owner dedupes against its **global** `_pending` across all shards, enqueues.
4. Owner's single loop pops one symbol every ≥1.0s, fetches in a thread (`snapshot.py:70`), writes
   the payload back to the requesting connection.
5. Shard calls its local `_on_snapshot_arrived(symbol, snap, recv_ns)` (`service.py:266`) — writes
   the row to its own partition, seeds its `DepthMaintainer`. **The owner never writes parquet.**

### REST budget — the global cap is STRUCTURAL, independent of N
Every shard's snapshot funnels through the owner's one throttle → aggregate REST stays within the
owner's **budget = cap − reserved headroom (~1400 weight/min under the 2400 REQUEST_WEIGHT cap)** by
construction, for any N. With N in-process schedulers (no owner) the host would emit up to **N× that
budget**, blowing the shared **2400/min REQUEST_WEIGHT** cap (`config.py:107`) into a 429/ban that
would starve the **live engine** — the single biggest risk in the accepted ADR-039 §F. A full
527-symbol cold re-seed (527×20 = **10,540 weight**) is **~7.5 min regardless of N** at the ~1400
budget: sharding
parallelizes the CPU-bound firehose, **not** REST seeding (the floor is the shared IP budget). This
unchanged seed tail must be documented so it is not mistaken for a regression.

### Failure — owner-down NEVER causes a permanent gap (the worst outcome is avoided)
The verified fact `service.py:255` (store **every** raw diff unconditionally) means: while the owner
is down, every shard **keeps recording its raw firehose tape**; only book-reconstruction freshness
(seeding/resync) stalls — a `depth_snapshot` gap bounded by the existing `_record_gap` manifest
(`service.py:277,288`), **not** a data gap.
- **Owner down:** requests get connection-refused; the shard buffers diffs in its `DepthMaintainer`
  and retries the socket with backoff. **Owner slow:** requests queue behind the ≥1.0s floor;
  bounded, self-healing. **Owner restarted:** comes up with empty `_pending`; shards treat any
  **unanswered request as still-needed and re-send on reconnect** (idempotent — owner dedupes), so
  no symbol sits unsynced indefinitely. *This in-flight-replay-on-reconnect rule is mandatory.*
- Residual: the owner is a single point of failure for **book freshness** across all shards (a
  crash-looping owner = no resync box-wide). Mitigated by `Restart=on-failure` + `WatchdogSec` + a
  **mandatory** dead-owner Telegram alert, and bounded by the raw tape flowing throughout.

---

## B. cpuset — concrete cores, provably disjoint from the engine

**Core map (N=3, Option B; 8 cores, no isolcpus):**
- **Engine + OS + nightly pipeline → cores 0–4** (5 cores): the `trading-engine-monitor` ~1s cycle,
  `trading-engine-entry`, OS/IRQ, and the bursty 22:00–00:50 UTC prediction pipeline get guaranteed
  low-latency silicon.
- **Capture shards → cores 5, 6, 7** (3 cores), one shard per core: `@0`→5, `@1`→6, `@2`→7.
- **Snapshot-owner + light collectors** (klines, rest-collector, signal-probe, streamlit) share the
  capture band (5–7), near-idle, held back by `CPUWeight`. They never touch 0–4.
- Operator alternatives from ADR-039 §A: **N=2** → engine 0–5, capture 6–7; **N=4** → engine 0–3,
  capture 4–7. The cpuset width must equal N and be disjoint from the engine band.

**Disjoint proof — set-theoretic AND kernel-enforced.** `{0,1,2,3,4} ∩ {5,6,7} = ∅` by
construction. But arithmetic disjointness is meaningless unless the cpuset controller programs the
masks on **both** sides. The verified host reality:
- **Engine side enforces:** `system.slice` has the cpuset controller → `AllowedCPUs=0-4` on the
  engine units will be programmed by the kernel.
- **Capture side is a SILENT NO-OP today:** the `--user` manager has
  `DelegateControllers=cpu memory pids` (no cpuset); `app.slice cgroup.controllers = cpu memory pids`.
  `AllowedCPUs=5` on a `--user` capture unit appears set in the unit file but the kernel **will not
  enforce it** — a false sense of isolation.

**Engine changes needed (both sudo, system-level, gated in OPERATIONS.md like the existing ADR-036
drop-in; coordinate with the crypto-trading-engine operator — separate repo/service):**
1. **Pin the engine:** `AllowedCPUs=0-4` via per-unit drop-ins
   `/etc/systemd/system/trading-engine-{monitor,entry}.service.d/10-cpuset.conf` (or a shared
   `system.slice.d`). Without pinning the engine **off** 5–7, "disjoint" is meaningless — capture on
   5–7 does not stop the engine wandering onto 5–7. Net engine delta: +1 system drop-in, zero code.
2. **Make capture's cpuset actually enforce** — choose one:
   - **(preferred, less invasive)** keep capture as `--user` units and add a system drop-in on
     `user@.service` extending `Delegate` to include **cpuset** (sibling to the user.slice drop-in),
     then enable cpuset in `user.slice` subtree_control; **or**
   - **move the capture units to `system.slice`** as system units (cpuset already delegated there),
     at the cost of the `--user`/linger model.
3. **Fail-closed self-check:** the capture launch path reads `/proc/self/status` `Cpus_allowed_list`
   and **refuses to start** if it overlaps a configured `ENGINE_RESERVED_CPUS={0..4}` — important
   precisely because the empty-`AllowedCPUs` default would otherwise let a misconfigured deploy land
   capture on core 0 (the engine's 1s-cycle core).

ADR-036 weights **stay** as the soft within-band arbiter (cores 5–7, where capture's collectors +
pipeline contend); cpuset is the new **hard** cross-band wall layered on top. **"Provably disjoint"
is TRUE iff** (engine `AllowedCPUs=0-4` enforcing) **AND** (capture cpuset actually programmed) —
the latter requires fixing the delegation gap; until both land it is **FALSE**.

---

## C. Unit topology

- **`mhde-capture-core@.service` template** replaces the single `mhde-capture-core.service`.
  Instance `%i` = shard id. `ExecStart=.../venv/bin/python main.py crypto capture-core-run --shard
  %i --of ${CAPTURE_N_SHARDS}` — adding `--shard`/`--of` to the command (today `--root` only). N is
  set in **one** place (an EnvironmentFile or the target's `Wants=`), so changing N edits the
  instance set, not each unit. Caps inherited unchanged: `Slice=app.slice`, `CPUWeight=20`,
  `IOWeight=20`, `OOMScoreAdjust=800`, `MemoryMax` **per-shard** (total ceiling = N×MemoryMax; set
  each to ~peak-RSS×1.8 after the trial, where per-shard peak ≈ 1/N of the single-process peak).
- **Per-instance `AllowedCPUs`** via a tiny per-instance drop-in
  (`mhde-capture-core@0.service.d/cpuset.conf` → `AllowedCPUs=5`, `@1`→6, `@2`→7), not a single
  template line (`%i` can't index a non-trivial core map). These enforce only after the §B
  delegation fix.
- **Snapshot-owner = its own unit** `mhde-capture-snapshot-owner.service`, **not shard 0**: a shard
  crash-loop never takes down the REST authority; the owner can restart without dropping any shard's
  WS sockets; its core/caps differ (shares 5–7, near-idle).
- **`mhde-capture-core.target`** groups `@0..@(N-1)` **plus the owner** for one-shot enable/disable.
  Shards have **no `Requires=` chain** between each other (one crash-restart doesn't cycle the
  others). The owner is ordered `Before=` the shards (best-effort; a shard that races ahead retries
  the socket via the failure-path backoff). Changing N is a **deliberate whole-target restart**
  (re-shards every symbol via `blake2b % N`), never live — doing it live scatters a partition's
  history across shards.
- The single shared **ADR-038 compaction timer stays one global timer**; it already matches
  `part-<shard>-*` (`store.py:278`), so it compacts a closed hour's per-shard parts unchanged.
- **Boot-persistence (linger / `WantedBy=default.target`) is DEFERRED.** Units ship
  tracked-but-disabled (BUILT-NOT-DEPLOYED, the current `mhde-capture-core.service` posture) and are
  started manually for the trial; persistence is an OPERATIONS.md step **after** the trial passes.

---

## D. Supervision + dead-shard detection

- **Restart:** per-instance `Restart=on-failure`, `RestartSec=5` on every shard and the owner. Add
  `StartLimitIntervalSec`/`StartLimitBurst` so a crash-**loop** trips to `failed` (visible/alertable)
  rather than flapping silently.
- **Clean stop:** raise `TimeoutStopSec` from today's 30s (which caused SIGKILL-on-stop when the
  saturated single loop couldn't drain — ADR-039 §E) to **60–90s**, and make `CaptureService.stop()`
  cancel shard tasks **before** `flush_all` (`service.py:311` stop / `417-427` finally) so an
  un-saturated per-core loop drains fast. On restart a shard re-runs `seed_universe` for its symbols
  via the owner (idempotent) — it self-re-seeds and re-syncs rather than leaving books stale.
- **DEAD-SHARD DETECTION — the load-bearing gap.** With symbol sharding, **one dead shard = a
  permanent gap for its ~1/N (~176) symbols** (the others keep capturing), and the firehose is
  forward-only (never backfilled). systemd `Restart=` only catches a process that **exits** — a shard
  that **hangs** (event loop wedged, sockets silent, process alive) is invisible, and one
  silently-dead shard does not surface as a whole-service outage, only as missing partitions. Three
  layers:
  1. **`WatchdogSec=` + `sd_notify` heartbeats** from the existing flush loop (`_flush_loop` runs
     every `FLUSH_POLL_S=1s`, `service.py:345`). A wedged loop misses the watchdog → systemd
     kills+restarts it. **The only layer that catches an alive-but-wedged loop.**
  2. **Peer-asymmetry liveness:** each shard writes `{ts_ns, dispatched, bytes_in, rows_written}`
     (`conn_manager` tracks dispatched/bytes_in; writers track rows_written, `service.py:335`) every
     ~10s; a cheap `mhde-capture-watchdog.timer` (on the capture band, off the engine) Telegram-alerts
     if any shard is `failed`, its heartbeat is stale, or its rows/dispatched ≈ 0 while peers flow.
     The asymmetry (one shard's partitions stop advancing while others advance) is the dead-shard
     tell a single-process design never had.
  3. **Gap manifest:** a per-shard `_gaps` rate spike (`service.py:288`) is an alarmable symptom.
- The **owner gets the same treatment** (Restart + WatchdogSec + mandatory dead-owner alert).
- The **§G trial must explicitly kill one shard** (confirm the alert fires AND the other N−1 shards
  are undisturbed — cores untouched, seeding unaffected) and **kill the owner** (confirm the raw
  tape keeps flowing and in-flight requests replay on owner reconnect).

---

## E. N ≥ 1 guard (the Stage-1 reviewer carry-over)

`sharding.shard_for_symbol` already clamps `n_shards <= 1 → 0` (`sharding.py:30`), so the partition
math doesn't ZeroDivision at N=0 — but that is the **silent-failure trap**: `n_shards=0` collapses
the entire universe onto shard 0 and gives shards 1..N−1 **zero** symbols (zero-coverage shards that
look healthy). The value flow is `config.CAPTURE_N_SHARDS` (default 3) → `sharding` default arg →
the new `--of` CLI. Land the guard at **three independent layers** so `n_shards=0` is unreachable:
1. **CLI (primary):** `--of` validated `IntRange(min=1)` (reject `--of < 1`) plus a redundant
   `n = max(1, of)` before constructing `CaptureService`; also reject `--shard` outside `[0, of)`.
2. **Config source:** a module-load `assert CAPTURE_N_SHARDS >= 1` at `config.py`.
3. **Owner symmetry:** the owner is handed `--of N` and asserts `N >= 1` independently; the target
   generator refuses to emit a target with zero `Wants=`.

The existing `sharding.py:30` `<=1 → 0` branch stays as defense-in-depth and as the valid N=1
degenerate fallback (cleanly degrades to today's single-process behavior).

---

## F. Build decomposition — riskiest piece first, single-box testable

- **PR-A (riskiest first; single-box, no cpuset/systemd/engine): snapshot-owner + unix-socket IPC +
  the global REST-budget proof.** The owner as a standalone process wrapping the existing
  `SnapshotScheduler` + `CaptureRestClient`, plus the shard-side IPC client stub presenting
  `.request()/.run()/.stop()` (so `service.py:278-279,281-286` redirect with no logic change). TDD
  with a fake clock + fake REST client: (a) aggregate request rate across **M** simulated shard
  connections ≤ the owner budget (cap − headroom, ~1400 weight/min) **regardless of M** (the core safety property — a budget bug
  = a 429/ban that starves the live engine, the highest blast radius and zero prior art); (b) two
  connections requesting the same symbol → **one** REST call (global dedup); (c) payload round-trips
  back and drives `_on_snapshot_arrived`; (d) owner-down → shard backoff/retry, no crash, raw diffs
  still recorded (`service.py:255`); (e) owner restart → unanswered in-flight requests re-sent on
  reconnect, idempotently deduped, no symbol left unsynced.
- **PR-B: shard-aware run path (single-box, no systemd).** Add `--shard`/`--of` to
  `capture-core-run` **with the N≥1 / `0≤shard<of` clamps**; wire `symbols_for_shard`; set
  `enable_snapshots=False` and inject the PR-A IPC client as `snap_scheduler`. Raise `TimeoutStopSec`
  + `stop()` cancel-before-flush here. TDD: 3 local shards (N=3) → disjoint subsets covering the
  universe; each writes `part-<shard>-*`; the ADR-038 compactor merges them; `n_shards=0` / `shard≥of`
  rejected.
- **PR-C: systemd topology, BUILT-NOT-DEPLOYED (tracked, disabled).** `@.service` template +
  `mhde-capture-snapshot-owner.service` + `mhde-capture-core.target` + per-instance cpuset drop-ins
  (files only, **no enforcement claim**) + raised `TimeoutStopSec` + `WatchdogSec`/`sd_notify` +
  per-shard heartbeat + `mhde-capture-watchdog.timer` + dead-shard/owner alert wiring.
- **PR-D (operator-gated, sudo, cross-repo, LAST): cpuset enforcement + the trial.** (1) engine
  `AllowedCPUs=0-4` drop-ins (coordinate with the engine operator); (2) the `user@.service`
  cpuset-delegation drop-in **or** move capture to `system.slice` — **required** because the user
  manager doesn't delegate cpuset today, so capture-side `AllowedCPUs` is a silent no-op until this
  lands; (3) the launch-time `Cpus_allowed_list` self-check (fail-closed); (4) the disjointness
  assertion. Then the **§G measured trial** against the live engine. Last because it needs sudo + a
  live engine + measurement, and PR-A..C are all provable without it.

---

## G. What the measured trial must prove (before any re-enable)

1. **Per-core CPU < ~60%** on each shard core (5–7) at full 527-symbol load (the optimized hot path
   from PR #31 is already on master).
2. **Handshake-timeout reconnects ≈ 0** (the single-loop saturation storm gone) and **gap rate ≈ 0**.
3. **Global REST ≤ the owner budget** (cap − headroom, ~1400/min, well under the 2400 REQUEST_WEIGHT cap) across all shards (one owner) — no 429/418.
4. **Engine's 1s cycle on cores 0–4 undisturbed** under nightly-pipeline burst (cpuset enforcing on
   both sides; if the pipeline contends, fall back to N=2 and give the pipeline a core — N is the
   operator knob).
5. **Kill one shard** → the dead-shard alert fires AND the other N−1 shards are undisturbed.
6. **Kill the owner** → the raw tape keeps flowing AND in-flight requests replay on reconnect.

Re-enable (and boot-persistence) only if all six land within target.

---

## Open risks

- **HOST BLOCKER (verified):** the `--user` manager does not delegate cpuset → capture-side
  `AllowedCPUs` is a **silent no-op** today. PR-D must add the sudo delegation drop-in or move capture
  to `system.slice`; until then "provably disjoint" is FALSE and engine+capture share all 8 cores
  under ADR-036 weight only.
- **Engine cpuset is cross-repo + sudo:** `AllowedCPUs=0-4` on the live engine must be coordinated
  with the crypto-trading-engine operator (mirrors `active_spec.json` coordination). One-sided pinning
  (capture only) lets the engine wander onto 5–7.
- **Live-contention proof waits for a live engine:** §G item 4 needs a running engine (currently
  `trading-engine-monitor` inactive / `-entry` failed as observed this session).
- **Owner is a shared dependency for book freshness:** a crash-looping owner stalls resync box-wide;
  the dead-owner alert is essential, not optional (raw tape keeps flowing throughout).
- **Dead-shard = permanent gap for ~1/N symbols**, invisible to `Restart=` if the loop wedges;
  `WatchdogSec`+`sd_notify` and the peer-asymmetry alert are required.
- **Seeding does not scale with N:** ~8.8 min cold re-seed regardless of N (shared IP budget); a mass
  disconnect → ~8.8 min of stale books. Forward-only-acceptable; document so it isn't read as a bug.
- **`.ipc` socket dir** under `data/research/capture_core/` must be **excluded** from ADR-037
  retention / ADR-038 compaction / the disk+inode guards (not a `symbol=/date=` dataset).

---

## Appendix — alternatives considered (why owner+socket won)

Three independent designs were generated and judged:

| Dimension | **D1 owner + unix socket (WINNER)** | D2 owner + on-disk spool | D3 no owner + flock token-bucket |
|---|---|---|---|
| Global REST safety | structural (1 queue; payload in-band) | structural, but payload via disk | arithmetic; each shard still HTTP-GETs; new flock primitive |
| Simplicity | +1 process, socket; reuses `enable_snapshots` seam | +1 process + 2 spool dirs to glob/exclude | 0 extra processes, but a new file-locked bucket to get exactly right |
| Failure resilience | owner-down → freshness gap only; raw tape flows; in-flight replay | same firehose safety; **but ~1k-level JSON spool = inode pressure (ADR-037 class)** | no owner to fail; **but flock not FIFO → worst-case fairness unbounded** |
| cpuset honesty (this host) | **correct** (catches the no-op delegation) | **wrong** (asserts hard cpuset on user units) | **wrong** (same false claim) |

D1 is the only design that is **both** structurally safe on the global REST budget **and** factually
correct about this host's cpuset delegation. It reuses the existing snapshot-injection seam so the
multi-process change touches `service.py` minimally, and isolates the highest-blast-radius,
zero-prior-art piece (owner + socket + budget proof) as a single-box-testable PR-A.

## What is NOT changing
- ADR-038 write-then-compact (writer flush + closed-hour compaction); the single global compaction
  timer already matches `part-<shard>-*`.
- The `symbol=/date=` event-time layout, `recv_ts_ns` cursor, per-stream schemas.
- The inode + byte guards, depth @100ms, 7-day retention.
- ADR-036 weight/OOM model — it stays as the soft within-band arbiter under the new hard cpuset wall.
