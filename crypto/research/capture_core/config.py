"""Capture-core configuration constants.

Research infrastructure, isolated from the live path (see the package
docstring). Mirrors the ``signal_probe`` config style and re-exports the shared
Binance base URL + request delay from :mod:`crypto.config`.
"""
from __future__ import annotations

from crypto.config import BINANCE_FUTURES_BASE, REQUEST_DELAY_S  # noqa: F401 (re-exported)

#: Root of the raw capture tree (gitignored via the ``data/research/`` rule).
#: Layout: ``<RAW_DIR>/<stream>/symbol=<SYM>/date=<YYYY-MM-DD>/part-*.parquet``.
RAW_DIR = "data/research/capture_core"

# -- WebSocket endpoints (USDT-M futures, PUBLIC) --
# Binance migrated to ROUTED combined-stream paths on 2026-04-23: the legacy
# unrouted /stream (and /ws) were decommissioned and now serve the /public group
# only, so a single base cannot carry the full capture set. Streams MUST be split
# by group (this is global Binance behavior, not a host/proxy artifact):
#   /public -> bookTicker (+ !bookTicker) and depth (any level/interval)
#   /market -> aggTrade, markPrice (per-symbol + array), forceOrder (+ array)
WS_PUBLIC_BASE = "wss://fstream.binance.com/public/stream?streams="
WS_MARKET_BASE = "wss://fstream.binance.com/market/stream?streams="


def classify_endpoint(stream: str) -> str:
    """Route a stream name to its endpoint group: ``"public"`` or ``"market"``.

    Public = bookTicker / !bookTicker / any depth stream; everything else
    (aggTrade, markPrice, forceOrder, and their array forms) is market.
    """
    if stream.endswith("@bookTicker") or stream == "!bookTicker" or "@depth" in stream:
        return "public"
    return "market"

# -- Universe --
#: Re-resolve the TRADING USDT-M perp universe from ``exchangeInfo`` this often
#: so newly-listed symbols enter the substrate without a restart (operator GO
#: 2026-06-03: the universe is NOT frozen).
UNIVERSE_RERESOLVE_INTERVAL_S = 3600.0

# -- Sharding --
#: Streams per WS connection. Conservatively far under Binance's 1024/stream
#: connection cap, leaving message-throughput headroom for the 529-symbol
#: firehose. ~529 aggTrade streams -> 3 shards at this size.
STREAMS_PER_CONN = 200

# -- Parquet flush thresholds (flush on the EARLIER of the two) --
FLUSH_INTERVAL_S = 30.0
FLUSH_MAX_BYTES = 64 * 1024 * 1024  # 64 MiB
PARQUET_COMPRESSION = "zstd"
#: How often the service evaluates flush triggers. Must be << the age interval so
#: the 64 MiB size cap is a real ceiling, not a poll-granularity check (a hot
#: partition can blow past 64 MiB well within one age interval at firehose rates).
FLUSH_POLL_S = 1.0
# -- FIREHOSE write-then-compact (ADR-038, supersedes the Phase-0 in-RAM roll-up) --
# The Phase-0 1-hour in-RAM roll-up buffered every partition in memory until 64 MiB
# / 60 min and OOM-looped the 8 GiB-capped firehose on the 15 GiB box (Stage B,
# 2026-06-14). ADR-038 decouples RAM from file count: flush small files on a SHORT
# interval (RAM only ever holds ~one interval) and merge the closed hours later via a
# separate hourly compaction timer (see ``maintenance.compact_firehose_closed_hours``).
#: Short firehose flush interval — the writer flushes each partition on the EARLIER of
#: this age OR ``CAPTURE_FIREHOSE_FLUSH_MAX_BYTES``. Keeps resident RAM to roughly
#: (inflow_rate × this), i.e. ~1 GiB at the measured ~0.8 GB/min — vs >8 GiB under the
#: retired roll-up. (The CAPTURE_FIREHOSE_ROLLUP_S constant is intentionally removed.)
CAPTURE_FIREHOSE_FLUSH_S = 30.0
#: Per-partition byte ceiling for the firehose writer (a hot partition flushes
#: mid-interval too). Lower than the generic 64 MiB so RAM is doubly bounded; at a 30s
#: interval most partitions flush on age before reaching it, so it is a backstop.
CAPTURE_FIREHOSE_FLUSH_MAX_BYTES = 16 * 1024 * 1024  # 16 MiB
# (The hourly compaction cadence is the timer's OnCalendar — not a constant here.)
#: Grace margin past an hour's end before its small files may be compacted. Must be
#: >> the flush interval so the writer is provably done with that hour (no in-flight
#: file). The open hour and any hour still within this margin are NEVER touched.
CAPTURE_COMPACTION_GRACE_S = 300.0  # 5 min (10× the 30s flush)

#: Merge budget per compaction SUBPROCESS chunk. Closed-hour merging accrues anon memory
#: ~per-merge (pyarrow pool retention), so compaction runs in chunks of ~this many merges,
#: each in its own subprocess; process exit resets the pool between chunks, bounding peak
#: RSS by RUN SIZE not total tape. Measured: ~0.25 MiB/merge avg + ~520 MiB base, so a full
#: hour (~2000 merges) sits ON the 1G cap. Sized for the WORST-CASE busy merge (leaning low,
#: not the average) so a busy hour has headroom; tune up only after profiling a busy hour.
CAPTURE_COMPACT_MERGES_PER_CHUNK = 400

# -- FIREHOSE sharding (ADR-039) -----------------------------------------------
# The single-process firehose saturates ONE core (profile 2026-06-15: ~72% of the
# core is GIL-bound on the event-loop thread), starving reconnect handshakes. ADR-039
# splits the universe across N processes/cores. STAGE 1 (writer-level) uses this only
# to drive the symbol->shard splitter (``sharding.shard_for_symbol``) and the
# ``part-<shard>-*`` writer naming, so multiple shards can write the same
# ``symbol=/date=`` partition without filename collision and the closed-hour compactor
# still merges them. Default 3 (Option B); the operator sets the final N and the cpuset
# core map in Stage 2 (systemd template + process launch — NOT in this stage).
CAPTURE_N_SHARDS = 3

# -- FIREHOSE sharding stage 2: snapshot-owner REST budget (ADR-039) -----------
# In the multi-process firehose the snapshot-owner is the SOLE caller of
# /fapi/v1/depth across all shards, so the global REQUEST_WEIGHT cap is respected
# STRUCTURALLY (one throttle, any N). The throttle's budget reserves this much
# headroom UNDER the cap (read live from exchangeInfo, fallback FAPI_WEIGHT_LIMIT)
# so capture can never weight-starve the engine's entry path or the light collectors
# on the shared IP. budget = cap - headroom; default 1000 leaves a clear margin of a
# 2400 cap for everyone else and still seeds 527 symbols (527*20 = 10,540 weight) in
# ~7.5 min.
CAPTURE_SNAPSHOT_RESERVED_HEADROOM_PER_MIN = 1000
#: Unix socket the owner serves on / shards dial (under the capture root). The .ipc
#: dir is NOT a symbol=/date= dataset and must be excluded from retention/compaction.
CAPTURE_SNAPSHOT_SOCKET_PATH = f"{RAW_DIR}/.ipc/snapshot-owner.sock"
#: All-traffic header-gate margin (ADR-039 2b). The owner ALSO watches the live per-IP
#: X-MBX-USED-WEIGHT-1M (which reflects engine + collectors + owner) and backs off new
#: depth fetches once observed used-weight exceeds (cap - this margin), as a backstop ON
#: TOP OF the throttle — so it adapts to actual combined usage instead of assuming the
#: static reserved-headroom split holds. The margin must cover the owner's observation
#: lag (weight others can add between its own fetches) plus its in-flight/next request;
#: conservative default 400 (~17% of a 2400 cap, ~20 owner depth requests of headroom).
CAPTURE_HEADER_GATE_MARGIN = 400

# -- Reconnect (mirrors the engine ws_consumer discipline) --
RECONNECT_BACKOFF_BASE_S = 1.0
RECONNECT_BACKOFF_MAX_S = 60.0
RECONNECT_JITTER = 0.1  # ±10%

# -- Liveness / proactive reconnect --
#: websockets client ping cadence + pong deadline.
WS_PING_INTERVAL_S = 180.0
WS_PING_TIMEOUT_S = 30.0
#: No frame for this long => treat the socket as dead and reconnect.
SOCKET_SILENCE_TIMEOUT_S = 60.0
#: Binance force-closes a connection at 24h; reconnect each shard before then.
PROACTIVE_RECONNECT_S = 23.0 * 3600.0
#: Per-shard stagger on the proactive threshold so all shards don't reconnect at
#: the same instant (~daily near-total blackout). shard N waits N*frac longer.
PROACTIVE_STAGGER_FRAC = 0.02

# -- REST present-state collector (capture-completion piece) --
#: /fapi REQUEST_WEIGHT limit per minute (from exchangeInfo rateLimits, live-confirmed).
FAPI_WEIGHT_LIMIT = 2400
#: Stay under this fraction of a pool's limit (leaves headroom for the depth
#: SnapshotScheduler + engine + signal-probe collector sharing the IP).
REST_BUDGET_FRACTION = 0.70
#: /futures/data is a SEPARATE pool with NO used-weight header AND absent from
#: exchangeInfo.rateLimits (both live-confirmed 2026-06-04), so it cannot be
#: self-paced from any response signal. Binance documents a fixed IP ceiling of
#: 1000 requests / 5 min for /futures/data/* — the only ground truth available —
#: so this pool is paced by RAW REQUEST COUNT over a rolling window.
FUTURES_DATA_REQ_LIMIT = 1000           # Binance-documented /futures/data IP ceiling
FUTURES_DATA_REQ_WINDOW_S = 300.0       # ...measured over 5 minutes
#: Stay under this fraction of the documented ceiling so capture coexists with any
#: other /futures/data user on the IP (e.g. the signal-probe collector).
FUTURES_DATA_REQ_BUDGET = int(REST_BUDGET_FRACTION * FUTURES_DATA_REQ_LIMIT)  # 700
#: Even-pacing floor between /futures/data requests, DERIVED from the verified
#: budget (window / budget ≈ 0.43s) rather than guessed. Smooths the request
#: stream so the rolling-window raw-count cap is a backstop, not the primary brake.
FUTURES_DATA_MIN_INTERVAL_S = FUTURES_DATA_REQ_WINDOW_S / FUTURES_DATA_REQ_BUDGET
#: Coarsened cadence for the 5m-native /futures/data series. A full 529-symbol
#: sweep of the 4 per-symbol ratio series + per-pair basis ≈ 2,645 requests, which
#: at FUTURES_DATA_REQ_BUDGET (~700/5min) takes ~19 min — so the series are sampled
#: every 20 min, the honest rate under the IP ceiling. Finer than this would breach
#: the ceiling and draw 429s / an IP ban that would also starve the /fapi HIGH series.
FUTURES_DATA_CADENCE_S = 1200.0
#: When the live /fapi used-weight is over the budget fraction, wait this long
#: before re-checking (lets the 1-minute weight window roll off).
REST_BUDGET_BACKOFF_S = 2.0
#: On a /futures/data 429, suppress that priority tier for this long (degrade).
REST_DEGRADE_COOLDOWN_S = 60.0

# -- REST (order-book snapshot seeding; 429/418 aware) --
#: Snapshot depth. Maintenance itself only needs ``lastUpdateId`` to bridge the
#: diff stream, but the snapshot is ALSO stored for OFFLINE book reconstruction,
#: which needs the full book — so we seed deep (1000 -> request weight 20) and
#: pay for it with heavy pacing below, rather than seed shallow and lose the
#: ability to reconstruct the book offline.
DEPTH_SNAPSHOT_LIMIT = 1000
DEPTH_SNAPSHOT_WEIGHT = 20  # Binance futures /fapi/v1/depth weight at limit=1000
REST_MAX_RETRIES = 5

#: Capture's REST weight ceiling, kept WELL under the ~2400/min futures IP budget
#: because that budget is SHARED with the engine and the per-minute signal-probe
#: collector on the same IP — a capture-triggered 429/ban would starve them too.
#: 529 full re-seeds = 529*20 = 10,580 weight, so the initial seed is paced, not
#: bursted.
CAPTURE_REST_WEIGHT_PER_MIN = 1200
#: Minimum spacing between snapshot requests derived from the weight ceiling
#: (1200/min budget / 20 weight = 60 req/min => 1.0s apart). ~529 initial seeds
#: therefore stagger over ~9 minutes.
SNAPSHOT_MIN_INTERVAL_S = DEPTH_SNAPSHOT_WEIGHT * 60.0 / CAPTURE_REST_WEIGHT_PER_MIN

# -- Depth-maintenance memory safety (leak root cause, found in the §G N=12 trial) --
#: The per-symbol DepthMaintainer buffers raw depth diffs while AWAITING a REST
#: snapshot (book.py). If a symbol's seed never lands (a dropped seed, or repeated
#: resyncs faster than the paced owner can re-seed), that buffer otherwise grows one
#: diff per message FOREVER — the N-independent, monotonic per-shard heap+CPU climb
#: that saturated all 8 cores in the N=12 measurement run. Bound it: the buffer only
#: needs the diffs NEAR the lastUpdateId boundary to sync, so old entries are pure
#: waste and the oldest are dropped once the cap is hit.
#: SIZING (updated for the online level book): a buffered ``_Diff`` now carries the
#: diff's bid/ask level arrays (needed to apply the bracket + drained diffs onto the
#: seeded book at sync), so each entry is ~4-5 KiB at the measured ~12.5 levels/side
#: mean and up to ~35 KiB for a deep-book diff (vs ~230 B cursor-only). Worst case is
#: a STUCK-UNSYNCED symbol filling the cap: 5000 * ~5 KiB ~= 24 MiB/symbol (mean), and
#: a mass-resync (WS reconnect storm, all ~44 sym/shard stuck) ~= ~1 GiB/shard / ~12
#: GiB host at the mean — bounded (maxlen + the reseed threshold below re-seed before
#: the cap), NOT the unbounded leak it replaces. DEPLOY NOTE: tune this down (it gates
#: only the near-boundary diffs; must stay > the reseed threshold) and monitor RSS
#: under a reconnect storm before the depth_state online-book redeploy.
CAPTURE_DEPTH_BUFFER_MAXLEN = 1500
#: While a maintainer is stuck unsynced and its buffer has grown past this many diffs,
#: it proactively re-raises ``needs_snapshot`` so the service re-queues a seed (the
#: never-synced on_diff path otherwise never asks). Must be < the maxlen so the
#: re-request fires BEFORE the cap starts evicting. The snapshot scheduler dedups, so
#: re-raising every diff past the threshold is harmless.
CAPTURE_UNSYNCED_RESEED_THRESHOLD = 1000
#: Durable seed retry (SnapshotClientScheduler): a failed seed is re-queued (not
#: dropped) with exponential backoff between attempts, starting here and capped below,
#: so a perpetually-failing symbol cannot hammer the shared snapshot-owner.
CAPTURE_SEED_RETRY_BACKOFF_INITIAL_S = 1.0
CAPTURE_SEED_RETRY_BACKOFF_MAX_S = 60.0

# -- ADR-039 §D layer-2 dead-shard detector (peer-asymmetry heartbeats) --------
#: Each shard writes a heartbeat {ts_ns, dispatched, bytes_in, rows} here every
#: CAPTURE_HEARTBEAT_INTERVAL_S; the mhde-capture-stall-detector timer reads them ALL and
#: alerts on a `failed` unit, a stale/absent heartbeat, or a shard whose rows stop advancing
#: while peers flow (the dead-shard tell a single-process design never had — sd_notify layer 1
#: only catches a wedged loop within the SAME process). Lives under .ipc/, which is NOT a
#: symbol=/date= dataset, so retention/compaction/disk+inode guards never sweep it.
CAPTURE_HEARTBEAT_DIR = f"{RAW_DIR}/.ipc/heartbeats"
CAPTURE_HEARTBEAT_INTERVAL_S = 10.0
#: A heartbeat older than this many intervals => the shard is hung/gone.
CAPTURE_HEARTBEAT_STALE_FACTOR = 3

# -- Long-horizon 1h klines store (capture-completion piece 2; ADR-035 long-context
#    reference frame — distinct from the 24h firehose buffer). Seeded once, then
#    maintained forward hourly. Closed bars only. All on the weight-counted /fapi pool.
KLINES_INTERVAL = "1h"
KLINES_DATASET = "klines_1h"
HOUR_MS = 3_600_000
#: Maintenance fetch covers a few trailing bars so a single missed/late hourly poll
#: self-heals on the next run; the in-memory dedup cursor drops already-seen bars.
#: limit < 100 => /fapi/v1/klines weight 1 (live-confirmed weight-by-limit table).
KLINES_MAINT_LIMIT = 6
KLINES_MAINT_CADENCE_S = 3600.0
#: The maintenance loop is mostly idle (one sweep/hour), so it polls the due-check
#: coarsely rather than every second.
KLINES_MAINT_TICK_S = 60.0
#: One-time backfill horizon and page size. limit 1500 is the Binance max (weight 10);
#: 90d of 1h bars = 2160 => ~2 pages/symbol.
KLINES_SEED_DAYS = 90
KLINES_SEED_LIMIT = 1500
#: Rolling on-host retention for the klines store (piece-2-specific; separate from
#: PR-3's firehose buffer cap). Partitions older than this are expired.
#: KI-164: 90 -> 30d. EVIDENCE (C1): no consumer reads capture klines by deep DATE.
#: The brain tick consumes forward by ``recv_ts_ns > cursor`` (reader.read_new_klines) and
#: discovery reads the BRAIN store, not capture, over DISCOVERY_HISTORY_DAYS(14) +
#: PRIMITIVE_LOOKBACK(2). The deepest real requirement is a brain-store REBUILD, bounded by
#: BRAIN_STORE_RETENTION_DAYS = 21 -> 30d leaves 9 days of margin. NOTE: klines ``date=`` is
#: keyed on bar openTime, so a backfill writes old-dated partitions at recv=now; the brain
#: reader therefore excludes klines from its date prune (reader._RECV_DATED_DATASETS) and
#: never depends on old partitions surviving. KLINES_SEED_DAYS (90) now EXCEEDS this window:
#: a manual re-seed writes ~60 days that the next nightly expire reclaims (one-off churn).
KLINES_RETENTION_DAYS = 30

# -- Capture disk guard (PR-3 safety) -----------------------------------------
# Free-space-aware protection for the FIREHOSE datasets only. The caps express
# PRIORITY (the engine wins contention), not starvation; the guard protects the
# volume without ever pruning the small, long-lived stores.
#: Datasets the guard may prune (the big WS firehose writers), pruned oldest-first.
#: klines_1h, the REST present-state series, and the _gaps manifest (tiny / audit /
#: longer-lived) are NEVER pruned — they are simply absent from this list.
#: KI-164: the depth family (``depth``, ``depth_snapshot``, ``depth_state``) is RETIRED —
#: its writers are off, so it is neither written nor prunable. Dense == the four surviving
#: WS firehose writers.
CAPTURE_DENSE_DATASETS = ("aggTrade", "bookTicker", "forceOrder", "markPrice")
FIREHOSE_PRUNABLE_DATASETS = CAPTURE_DENSE_DATASETS
#: The 7 REST present-state ("as-of") series. Low-rate AND never date-pruned by the
#: brain reader (every date partition is read every tick), so the closed-hour
#: (flush-mtime-hour) compactor only buys ~1.5-2x; instead they get a DAILY WHOLE-
#: PARTITION seal-yesterday pass that collapses each sealed (symbol,date) to ~1 file.
#: MUST mirror ``rest_series.SERIES`` names (pinned by a test); kept as a literal here
#: because ``rest_series`` imports this module (a derive would be circular).
CAPTURE_ASOF_DATASETS = (
    "open_interest", "premium_index", "global_ls_account", "top_ls_account",
    "top_ls_position", "taker_ls_ratio", "basis",
)
#: Datasets the hourly CLOSED-HOUR compactor sweeps: the WS firehose set PLUS
#: ``klines_1h``. klines is compacted for fragment-count (~3x — it is low files/
#: partition) but is NOT firehose-prunable: it keeps its own 90-day retention, so it
#: is added to the COMPACTION coverage here, never to ``FIREHOSE_PRUNABLE_DATASETS``
#: (which the 7-day firehose expire reads — that would shorten klines to 7 days).
#: DEPLOY-ORDER SAFETY (same class as CAPTURE_RETIRED_RETENTION_DAYS): the retired depth
#: family stays in the COMPACTION coverage even though it is out of the prune set. This
#: list is read by the hourly compact unit, which runs main.py from the WORKING TREE — so
#: dropping the family here would stop compacting it at the next :06, while the live shards
#: still hold the old code and keep writing. Uncompacted, one open hour of `depth` is ~73k
#: part-files (measured 2026-08-27); compaction is ~100x, i.e. ~1.5M files/day vs ~13k. The
#: nightly 1d expire cannot cover it (today's partition grows all day) and the BYTE guard
#: cannot see it (73k tiny files ~= one compact file's bytes) — the only guard that would
#: react is the inode guard, by HALTING capture. Post-restart this costs nothing: the empty
#: dataset dirs scan to nothing.
CAPTURE_CLOSED_HOUR_COMPACT_DATASETS = (
    FIREHOSE_PRUNABLE_DATASETS + ("depth", "depth_snapshot", "depth_state")
    + (KLINES_DATASET,)
)
#: SOFT floor: below this free space, prune the OLDEST firehose date-partitions
#: (across the firehose datasets) until back above the floor. 50 GiB on the host's
#: ~107 GB free keeps ~50 GB free (~31h of firehose buffer ≥ the brain's ~24h need)
#: while leaving the engine more headroom. "Keep N GB free" — if free differs
#: materially at deploy, retune per the OPERATIONS.md runbook (target ~30h buffer,
#: never below ~20 GB free).
#: 2026-08-28: 50 -> 40 GiB. EVIDENCE: on 2026-08-28 00:00:13 the guard pruned the oldest
#: dense partitions with free measured at 43.7-48.5 GiB — below the 50 GiB floor. Because
#: only ~1-2 days of dense tape exist, "oldest" was YESTERDAY, which the lagging brain
#: cursor (KI-160, ~4.5h behind) still needed: it was MAROONED and force-jumped +5h,
#: punching a 19:24->24:00 hole in the tape that cost ~5h of the out-of-sample evidence
#: window. Free never fell below 43.7 GiB, so a 40 GiB floor prevents that prune entirely
#: while still leaving 30 GiB of headroom above the CRITICAL halt.
CAPTURE_DISK_SOFT_FLOOR_BYTES = 40 * 1024 ** 3   # 40 GiB
#: CRITICAL floor: below this, HALT firehose writes (forward-only — dropped, never
#: backfilled) and emit a CRITICAL log.
CAPTURE_DISK_CRITICAL_FLOOR_BYTES = 10 * 1024 ** 3   # 10 GiB
#: RESUME floor: writes resume once free recovers to/above this — DECOUPLED from the
#: SOFT prune target. The 2026-08-08 outage latched writes off for ~14h because resume
#: waited for the 50 GiB SOFT floor while retention could free only INTO the [10,50)
#: band (the guard never prunes today's data and the old partitions were already
#: expired). A resume floor just above CRITICAL lets retention/compaction self-recover
#: writes with no operator restart; the small [CRITICAL, RESUME) band still holds state
#: so it cannot flap. Pruning toward SOFT is unchanged.
CAPTURE_DISK_RESUME_FLOOR_BYTES = 15 * 1024 ** 3   # 15 GiB
#: How often the firehose flush loop runs the guard. statvfs is cheap; the prune
#: scan only runs when under the soft floor.
CAPTURE_DISK_CHECK_INTERVAL_S = 10.0

# -- Capture inode guard (Phase 0 safety) -------------------------------------
# The free-BYTES guard above cannot see the failure mode that took the box down on
# 2026-06-09: millions of tiny files exhausting the ROOT-FILESYSTEM inode table
# while bytes free stayed healthy. This guard tracks inode usage on the capture
# root's filesystem (the root fs) and makes capture fail ITSELF before it can
# starve the OS/engine again — WARN (Telegram) at 80% used, CRITICAL + HALT writes
# at 90% used, with hysteresis (resume below the WARN fraction so it does not flap).
#: Inode usage fraction at which to WARN via Telegram (edge-triggered).
CAPTURE_INODE_WARN_FRACTION = 0.80
#: Inode usage fraction at which to go CRITICAL: HALT firehose writes (forward-only,
#: dropped never backfilled) + Telegram. Recovery of inodes is by retention/compaction,
#: not the halt.
CAPTURE_INODE_CRITICAL_FRACTION = 0.90
#: RESUME fraction: writes resume once inode usage falls BELOW this — decoupled from the
#: WARN alert tier (same latch fix as the byte guard). Sits just below CRITICAL so the
#: [RESUME, CRITICAL) hold band is small; retention self-recovers without a restart.
CAPTURE_INODE_RESUME_FRACTION = 0.88

# -- Capture firehose retention (Phase 0) -------------------------------------
#: Rolling on-host raw window for the FIREHOSE datasets. Whole ``date=`` partitions
#: older than this are pruned oldest-first (never today's). Distinct from PR-3's
#: free-space byte guard (kept) and from the klines store's 90d window: this is a
#: TIME bound on the raw firehose tape (the brain Phase 1 reader needs ~24h). 7d is a
#: comfortable research buffer that keeps total file/inode count low under the
#: write-then-compact layout (ADR-038). Filesystem-only; never opens the production DB.
#: KI-164: 7 -> 3d, and now HONEST. The configured 7d was never achieved: the byte guard
#: had pruned the Binance dense set to a SINGLE day on disk (measured 2026-08-27), so
#: retention was guard-driven, not policy-driven. 3d is nightly-ENFORCED for both roots and
#: still exceeds the brain reader's ~24h need with margin.
CAPTURE_DENSE_RETENTION_DAYS = 3
#: Legacy alias (one source of truth) — kept because callers/tests reference it by name.
CAPTURE_RAW_RETENTION_DAYS = CAPTURE_DENSE_RETENTION_DAYS
#: The 7 REST as-of series had NO enforced ceiling at all: they are absent from
#: FIREHOSE_PRUNABLE_DATASETS, which is what the nightly expire read, so nothing ever
#: expired them on either root (measured 2026-08-27: 28 days resident and growing; OKX
#: premium_index/open_interest at ~242k files EACH). Now nightly-enforced.
CAPTURE_ASOF_RETENTION_DAYS = 21

# -- Stream cadences --
DEPTH_UPDATE_SPEED = "100ms"   # rawest diff cadence (operator GO: no pre-coarsen)
MARKPRICE_SPEED = "1s"

# -- Online book-state dataset (depth_state) --
# Periodic compact top-N book states reconstructed ONLINE from the depth diff
# stream (book.py level book) and written on the flush loop for the brain to
# consume read-only. A consumption BUFFER (short retention), not a history tape:
# the brain keeps only its own within-window summaries. Full-diff persistence is
# disk-infeasible (~11-12 GB/day vs the 50 GiB DiskGuard floor); this digested
# layer is ~15-50x fewer rows. INERT until a deliberate capture redeploy.
# DORMANCY GATE — default OFF (mandatory): the shards auto-restart on a crash, so the
# online level book + depth_state writer must NEVER activate on an unplanned restart.
# OFF keeps the maintainer CURSOR-ONLY (the proven pre-#49 path: no per-symbol book, no
# fat level-carrying _Diff buffers — the reconnect-storm OOM source). Flip ON only as a
# deliberate depth-state activation (alongside tuning CAPTURE_DEPTH_BUFFER_MAXLEN).
# KI-164 RETIREMENT (kill-switch, NOT deletion — Stage C revives by flag flip):
# depth_state regenerated ~870k files/day and was 30% of the filesystem's inodes when
# emergency-deleted 2026-08-26. The raw ``depth`` diff and the REST ``depth_snapshot``
# exist only to feed it (and each other): depth carries the sequence numbers the online
# book needs, depth_snapshot seeds/resyncs that book. With depth_state off they are pure
# cost, so all three are retired together. Readers (brain/depth.py, read_new_depth_state,
# the unwired DEPTH SourceSpec) are deliberately UNTOUCHED.
#: Raw depth diff stream: subscription AND writer. Off => the stream is not subscribed at
#: all (bandwidth + CPU + inodes), no sequence maintainer, and therefore no depth-derived
#: ``sequence_gap`` manifest rows. Connection-level gaps (conn_manager) are unaffected.
DEPTH_ENABLED = False
#: REST /fapi/v1/depth snapshot seeding. Off => no snapshot writer and no snapshot
#: scheduler, which idles the snapshot-owner's sole REST duty.
DEPTH_SNAPSHOT_ENABLED = False
DEPTH_STATE_ENABLED = False
DEPTH_STATE_DATASET = "depth_state"
DEPTH_STATE_TOP_N = 20            # levels per side (the signal-rich near book)
DEPTH_STATE_CADENCE_S = 5.0       # one state per synced symbol every 5s
DEPTH_STATE_RETENTION_DAYS = 2    # short consumption buffer (not the 7d firehose tape)

# -- Streams intentionally NOT captured (recorded decisions, not omissions) --
#: Losslessly derivable from a captured raw stream, or inapplicable to single-asset
#: USDT-M perps. Kept here so the exclusion is an auditable decision.
EXCLUDED_STREAMS = {
    "kline_*": "OHLCV is an aggregation of the captured @aggTrade tape (lossless).",
    "continuousKline_*": "Same as kline; derivable from @aggTrade.",
    "ticker / miniTicker / !ticker@arr / !miniTicker@arr":
        "24h rolling stats derivable from the captured tape.",
    "depth5/10/20 (partial book)":
        "Strict subset of the full @depth diff + snapshot already captured.",
    "!assetIndex@arr":
        "Multi-Assets-Mode collateral index prices — account-margin data, not "
        "single-asset USDT-M perp price discovery. Inapplicable.",
    "<symbol>@compositeIndex":
        "Only emits for composite-index symbols, not regular perps. Inapplicable "
        "to the perp universe.",
}


# -- KI-164: nightly-enforced retention policy (per CLASS, both roots) ---------
#: ``dataset -> retention days``, enforced every night by ``crypto capture-firehose-expire``
#: on WHICHEVER root it is pointed at (the OKX unit runs the same CLI with ``--root``), so
#: one policy governs both. Before this, only FIREHOSE_PRUNABLE_DATASETS had a ceiling and
#: the 7 as-of series had none — the unbounded-writer half of KI-164.
#:
#: ``_gaps`` is DELIBERATELY ABSENT and must never be added: it is the audit manifest of
#: capture outages (tiny, append-only) and a gap that expires is a silent no-bias violation.
#: The retired depth family is absent because it is no longer written at all.
#: Datasets retired by KI-164 — swept once by ``scripts/ki164_retire_depth_family.py``.
CAPTURE_RETIRED_DATASETS = ("depth", "depth_snapshot", "depth_state")
#: DEPLOY-ORDER SAFETY. The kill-switches only bite when the operator restarts the fleet;
#: until then the RUNNING processes hold the old code and keep writing. Dropping
#: depth_state's old 2-day expire in that window would leave it with NO ceiling at all —
#: strictly worse than before this change, and depth_state regrows ~1.1M files/day. The
#: retired datasets therefore keep the TIGHTEST nightly ceiling until the sweep removes
#: them, after which every pass is a no-op.
CAPTURE_RETIRED_RETENTION_DAYS = 1

CAPTURE_RETENTION_POLICY: dict = {
    **{d: CAPTURE_DENSE_RETENTION_DAYS for d in CAPTURE_DENSE_DATASETS},
    **{d: CAPTURE_ASOF_RETENTION_DAYS for d in CAPTURE_ASOF_DATASETS},
    KLINES_DATASET: KLINES_RETENTION_DAYS,
    # Retired-but-possibly-still-being-written (see CAPTURE_RETIRED_RETENTION_DAYS).
    **{d: CAPTURE_RETIRED_RETENTION_DAYS for d in CAPTURE_RETIRED_DATASETS},
}


# -- Wall-clock gap detection (2026-08-28) ------------------------------------
#: Seconds of wall-clock silence between consecutive observed messages that counts as a
#: capture HOLE. Replaces the depth-derived `sequence_gap` signal that KI-164 retired:
#: `depth` carried the only Binance sequence numbers, so restarts and stalls now produce
#: NO gap-manifest row at all (measured: the 2026-08-27 22:10 restart went unflagged,
#: while shard 2's 12:40 solo restart had been flagged retroactively via depth). 300s is
#: well above the 30s flush cadence and any normal reconnect, so it fires on real holes
#: only.
CAPTURE_GAP_ALERT_THRESHOLD_S = 300.0
