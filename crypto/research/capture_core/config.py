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
#: FIREHOSE roll-up window (Phase 0 compact-on-write). The firehose writers flush a
#: partition on the EARLIER of this age OR ``FLUSH_MAX_BYTES``. A 1-hour roll-up
#: collapses a low/idle partition to ~24 files/day (vs ~2,880/day under the old 30s
#: age cadence that exhausted the root inode table on 2026-06-09); a hot partition
#: still seals on the 64 MiB size cap (a handful of larger files), so the size cap
#: remains the per-partition MEMORY ceiling. Projected total: ~tens of thousands of
#: files/day across the firehose, not millions. The size cap means lengthening the
#: age window does NOT raise the per-partition memory ceiling. Tune toward DAILY
#: roll-ups only if the deploy-runbook RSS measurement (PR-3) leaves headroom.
CAPTURE_FIREHOSE_ROLLUP_S = 3600.0

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
KLINES_RETENTION_DAYS = 90

# -- Capture disk guard (PR-3 safety) -----------------------------------------
# Free-space-aware protection for the FIREHOSE datasets only. The caps express
# PRIORITY (the engine wins contention), not starvation; the guard protects the
# volume without ever pruning the small, long-lived stores.
#: Datasets the guard may prune (the big WS firehose writers), pruned oldest-first.
#: klines_1h, the REST present-state series, and the _gaps manifest (tiny / audit /
#: longer-lived) are NEVER pruned — they are simply absent from this list.
FIREHOSE_PRUNABLE_DATASETS = (
    "aggTrade", "depth", "bookTicker", "forceOrder", "markPrice", "depth_snapshot",
)
#: SOFT floor: below this free space, prune the OLDEST firehose date-partitions
#: (across the firehose datasets) until back above the floor. 50 GiB on the host's
#: ~107 GB free keeps ~50 GB free (~31h of firehose buffer ≥ the brain's ~24h need)
#: while leaving the engine more headroom. "Keep N GB free" — if free differs
#: materially at deploy, retune per the OPERATIONS.md runbook (target ~30h buffer,
#: never below ~20 GB free).
CAPTURE_DISK_SOFT_FLOOR_BYTES = 50 * 1024 ** 3   # 50 GiB
#: CRITICAL floor: below this, HALT firehose writes (forward-only — dropped, never
#: backfilled) and emit a CRITICAL log; resume once free recovers above the SOFT
#: floor (hysteresis, so it does not flap at the boundary).
CAPTURE_DISK_CRITICAL_FLOOR_BYTES = 10 * 1024 ** 3   # 10 GiB
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
#: dropped never backfilled) + Telegram. Resume once usage falls below the WARN
#: fraction (hysteresis). Recovery of inodes is by retention/compaction, not the halt.
CAPTURE_INODE_CRITICAL_FRACTION = 0.90

# -- Capture firehose retention (Phase 0) -------------------------------------
#: Rolling on-host raw window for the FIREHOSE datasets. Whole ``date=`` partitions
#: older than this are pruned oldest-first (never today's). Distinct from PR-3's
#: free-space byte guard (kept) and from the klines store's 90d window: this is a
#: TIME bound on the raw firehose tape (the brain Phase 1 reader needs ~24h; 14d is
#: a generous research buffer). Filesystem-only; never opens the production DB.
CAPTURE_RAW_RETENTION_DAYS = 14

# -- Stream cadences --
DEPTH_UPDATE_SPEED = "100ms"   # rawest diff cadence (operator GO: no pre-coarsen)
MARKPRICE_SPEED = "1s"

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
