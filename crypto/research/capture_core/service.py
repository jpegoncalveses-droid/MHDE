"""Capture-core service: resolve universe -> capture aggTrade -> parquet.

Orchestrates the PR-1 capture path:

  1. Resolve the full TRADING USDT-M perp universe from ``exchangeInfo`` and
     **re-resolve on a cadence** — when the set changes, rebuild the connection
     manager so newly-listed symbols enter the substrate (a change-only rebuild,
     so an unchanged universe keeps its sockets and incurs no gap).
  2. Subscribe to ``<symbol>@aggTrade`` for every symbol; route each frame to the
     aggTrade parquet writer.
  3. Periodically flush due partitions; flush everything on SIGTERM/SIGINT.

NEVER opens ``mhde.duckdb`` or the engine DB; writes only under ``root``.
Depth / bookTicker / markPrice capture and order-book reconstruction land in PR-2.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import pathlib
import signal
import time
from typing import Any, Callable, Optional, Sequence

from crypto.research.capture_core import config as cfg
from crypto.research.capture_core import conn_manager as cm
from crypto.research.capture_core import sd_notify
from crypto.research.capture_core import sharding
from crypto.research.capture_core import store
from crypto.research.capture_core.book import DepthMaintainer
from crypto.research.capture_core.disk_guard import DiskGuard, InodeGuard
from crypto.research.capture_core.snapshot import SnapshotScheduler
from crypto.research.capture_core.snapshot_owner import (
    SnapshotClient, SnapshotClientScheduler)

logger = logging.getLogger("mhde.crypto.capture_core.service")


# -- pure helpers --

def aggtrade_streams(universe: Sequence[str]) -> list[str]:
    """Combined-stream names for per-symbol aggTrade across ``universe``."""
    return [f"{s.lower()}@aggTrade" for s in universe]


#: Market-wide array streams (one connection covers every symbol).
MARKET_STREAMS = ["!forceOrder@arr", f"!markPrice@arr@{cfg.MARKPRICE_SPEED}"]


def per_symbol_streams(universe: Sequence[str]) -> list[str]:
    """Per-symbol capture streams: aggTrade + depth diff + bookTicker."""
    out: list[str] = []
    for s in universe:
        low = s.lower()
        out += [f"{low}@aggTrade", f"{low}@depth@{cfg.DEPTH_UPDATE_SPEED}",
                f"{low}@bookTicker"]
    return out


def capture_streams(universe: Sequence[str]) -> list[str]:
    """Full PR-2 capture set: per-symbol streams + market-wide array streams."""
    return per_symbol_streams(universe) + MARKET_STREAMS


def capture_streams_for_shard(universe: Sequence[str], *,
                              owns_array_streams: bool) -> list[str]:
    """Capture streams for ONE shard: its subset's per-symbol streams, plus the
    market-wide array streams ONLY if this shard owns them (ADR-039 §2 — the
    ``!markPrice@arr`` / ``!forceOrder@arr`` connections deliver every symbol and
    cannot be split, so exactly one process subscribes to them, else markPrice /
    forceOrder would be captured N times). ``owns_array_streams=True`` reproduces
    :func:`capture_streams` (the single-process default)."""
    streams = per_symbol_streams(universe)
    if owns_array_streams:
        streams += MARKET_STREAMS
    return streams


def depth_bookticker_streams(universe: Sequence[str]) -> list[str]:
    """Just depth + bookTicker (the streams that deliver from this host; used by
    the partial firehose load test)."""
    out: list[str] = []
    for s in universe:
        low = s.lower()
        out += [f"{low}@depth@{cfg.DEPTH_UPDATE_SPEED}", f"{low}@bookTicker"]
    return out


def depth_row(data: dict, recv_ns: int) -> dict:
    """Map a raw depthUpdate diff to a store row (ids->int, levels kept verbatim)."""
    return {
        "recv_ts_ns": recv_ns, "e": data.get("e"),
        "E": int(data["E"]), "T": int(data["T"]), "s": data["s"],
        "U": int(data["U"]), "u": int(data["u"]), "pu": int(data["pu"]),
        "b": data.get("b", []), "a": data.get("a", []),
    }


def bookticker_row(data: dict, recv_ns: int) -> dict:
    return {
        "recv_ts_ns": recv_ns, "e": data.get("e"), "u": int(data["u"]),
        "s": data["s"], "b": data["b"], "B": data["B"], "a": data["a"],
        "A": data["A"], "T": int(data["T"]), "E": int(data["E"]),
    }


def forceorder_rows(data: dict, recv_ns: int) -> list[dict]:
    """Liquidation event -> a single per-symbol row (symbol is in ``o``)."""
    o = data["o"]
    return [{
        "recv_ts_ns": recv_ns, "E": int(data["E"]), "s": o["s"], "S": o["S"],
        "o": o["o"], "f": o["f"], "q": o["q"], "p": o["p"], "ap": o["ap"],
        "X": o["X"], "l": o["l"], "z": o["z"], "T": int(o["T"]),
    }]


def markprice_rows(data: list, recv_ns: int) -> list[dict]:
    """markPrice ARRAY -> one row per symbol."""
    return [{
        "recv_ts_ns": recv_ns, "e": el.get("e"), "E": int(el["E"]), "s": el["s"],
        "p": el["p"], "i": el["i"], "P": el["P"], "r": el["r"], "T": int(el["T"]),
    } for el in data]


def snapshot_row(symbol: str, snap: dict, recv_ns: int) -> dict:
    """Map a REST /fapi/v1/depth snapshot to a depth_snapshot row."""
    return {
        "recv_ts_ns": recv_ns, "s": symbol,
        "lastUpdateId": int(snap["lastUpdateId"]),
        # Partition on event time E; if a snapshot ever lacks it, fall back to the
        # recv-derived ms so the row never lands in date=1970-01-01.
        "E": int(snap.get("E") or recv_ns // 1_000_000), "T": int(snap.get("T", 0)),
        "b": snap.get("bids", []), "a": snap.get("asks", []),
    }


def book_state_row(symbol: str, maintainer: DepthMaintainer, recv_ns: int, top_n: int) -> dict:
    """Map a maintained level book to a depth_state row: top-N per side + validity.

    ``valid`` is the maintainer's synced state at the sample instant (fully seeded
    and continuous); the periodic writer only emits synced books, so the brain
    consumes only valid states.
    """
    bids, asks = maintainer.top_levels(top_n)
    return {
        "recv_ts_ns": recv_ns, "s": symbol,
        "update_id": maintainer.last_u, "valid": maintainer.synced,
        "b": bids, "a": asks,
    }


def aggtrade_row(data: dict, recv_ns: int) -> dict:
    """Map an aggTrade ``data`` payload to a store row (ids->int, price->str)."""
    return {
        "recv_ts_ns": recv_ns,
        "e": data.get("e"),
        "E": int(data["E"]),
        "a": int(data["a"]),
        "s": data["s"],
        "p": data["p"],          # venue string, kept lossless
        "q": data["q"],          # venue string, kept lossless
        "f": int(data["f"]),
        "l": int(data["l"]),
        "T": int(data["T"]),
        "m": bool(data["m"]),
    }


def universe_changed(old: Sequence[str], new: Sequence[str]) -> bool:
    """True iff the two universes differ as sets (order-insensitive)."""
    return set(old) != set(new)


class CaptureService:
    """Long-running aggTrade capture service with universe re-resolution."""

    def __init__(
        self,
        *,
        root: str,
        client: Any,
        shard: Optional[int] = None,
        n_shards: int = cfg.CAPTURE_N_SHARDS,
        snapshot_socket_path: Optional[str] = None,
        snapshot_client_factory: Optional[Callable[[str], Any]] = None,
        connect_fn: Optional[Callable[[str], Any]] = None,
        mgr_factory: Optional[Callable[[list[str]], Any]] = None,
        streams_per_conn: int = cfg.STREAMS_PER_CONN,
        reresolve_interval_s: float = cfg.UNIVERSE_RERESOLVE_INTERVAL_S,
        flush_interval_s: float = cfg.CAPTURE_FIREHOSE_FLUSH_S,
        flush_max_bytes: int = cfg.CAPTURE_FIREHOSE_FLUSH_MAX_BYTES,
        flush_poll_s: float = cfg.FLUSH_POLL_S,
        install_signals: bool = True,
        enable_snapshots: bool = True,
        snap_scheduler: Optional[Any] = None,
        disk_guard: Optional[Any] = None,
        disk_guard_enabled: bool = True,
        inode_guard: Optional[Any] = None,
        inode_guard_enabled: bool = True,
        disk_check_interval_s: float = cfg.CAPTURE_DISK_CHECK_INTERVAL_S,
        depth_state_enabled: bool = cfg.DEPTH_STATE_ENABLED,
        notifier: Optional[Any] = None,
        watchdog_liveness_window_s: float = cfg.SOCKET_SILENCE_TIMEOUT_S,
        heartbeat_dir: Optional[str] = None,
        heartbeat_interval_s: float = cfg.CAPTURE_HEARTBEAT_INTERVAL_S,
    ) -> None:
        self._root = root
        self._client = client
        self._shard = shard
        self._n_shards = n_shards
        # The market-wide array streams (!markPrice@arr / !forceOrder@arr) deliver
        # EVERY symbol on one connection and cannot be split, so exactly ONE process
        # subscribes to them (ADR-039 §2): the single-process default (shard is None)
        # or shard 0. Every other shard captures only its per-symbol subset.
        self._owns_array = shard is None or shard == 0
        self._connect_fn = connect_fn
        self._mgr_factory = mgr_factory or self._default_mgr_factory
        self._per_conn = streams_per_conn
        self._reresolve_interval_s = reresolve_interval_s
        self._flush_poll_s = flush_poll_s
        self._install_signals = install_signals

        # PR-3 disk guard: protects the volume by pruning the OLDEST firehose
        # partitions under the soft floor and halting firehose writes under the
        # critical floor (forward-only: dropped, never backfilled).
        if disk_guard is not None:
            self._disk_guard: Any = disk_guard
        elif disk_guard_enabled:
            self._disk_guard = DiskGuard(root)
        else:
            self._disk_guard = None

        # Phase 0 inode guard: protects the ROOT FILESYSTEM's inode table — the
        # failure mode the byte guard cannot see (millions of tiny files while bytes
        # stay healthy). WARN at 80% used, HALT firehose writes at 90% (forward-only).
        if inode_guard is not None:
            self._inode_guard: Any = inode_guard
        elif inode_guard_enabled:
            self._inode_guard = InodeGuard(root)
        else:
            self._inode_guard = None

        self._disk_check_interval_s = disk_check_interval_s
        self._last_disk_check = 0.0
        # The guard enforce (scandir + rmtree of up-to-thousands of partitions) runs OFF
        # the flush loop as a background task; at most one in flight at a time.
        self._enforce_task: Optional[asyncio.Task] = None

        _wkw = dict(flush_interval_s=flush_interval_s, flush_max_bytes=flush_max_bytes)
        self._agg = store.aggtrade_writer(root, **_wkw)
        self._depth = store.depth_writer(root, **_wkw)
        self._bookticker = store.bookticker_writer(root, **_wkw)
        self._forceorder = store.forceorder_writer(root, **_wkw)
        self._markprice = store.markprice_writer(root, **_wkw)
        self._snapshot = store.depth_snapshot_writer(root, **_wkw)
        # Online book-state dataset: periodic top-N states from the level book. GATED
        # by DEPTH_STATE_ENABLED — when OFF the maintainer stays cursor-only (no level
        # feed, no book, no fat buffers) and this writer is never created or flushed,
        # so the raw firehose path is byte-identical to pre-#49.
        self._depth_state_enabled = depth_state_enabled
        self._depth_state = (store.depth_state_writer(root, **_wkw)
                             if self._depth_state_enabled else None)
        self._last_book_state_monotonic = 0.0
        self._gaps = store.gap_writer(root)
        self._writers = [self._agg, self._depth, self._bookticker, self._forceorder,
                         self._markprice, self._snapshot, self._gaps]
        if self._depth_state is not None:
            self._writers.append(self._depth_state)

        # Per-symbol depth sequence maintenance (cursor only; not a level book).
        self._maintainers: dict[str, DepthMaintainer] = {}

        # REST snapshot scheduler (paced/deduped). Injected in tests; built from
        # the client otherwise. None disables depth seeding/resync.
        if snap_scheduler is not None:
            self._snap_sched: Any = snap_scheduler
        elif snapshot_socket_path is not None:
            # SHARD process (ADR-039 2b): seed via the snapshot-owner over the socket,
            # so the owner is the SOLE REST caller and the global weight budget holds
            # regardless of N. The owner does the throttling/dedup.
            _make = snapshot_client_factory or SnapshotClient
            self._snap_sched = SnapshotClientScheduler(
                client=_make(snapshot_socket_path),
                on_snapshot=self._on_snapshot_arrived)
        elif enable_snapshots and client is not None:
            self._snap_sched = SnapshotScheduler(
                client=client, on_snapshot=self._on_snapshot_arrived,
                min_interval_s=cfg.SNAPSHOT_MIN_INTERVAL_S,
                limit=cfg.DEPTH_SNAPSHOT_LIMIT)
        else:
            self._snap_sched = None

        # sd_notify supervision (ADR-039 gap 3). Default = a disabled notifier so every
        # CLI / test run (NOTIFY_SOCKET unset) is a no-op. READY fires when the shard is
        # up; WATCHDOG is fed from the flush loop only while messages flow, so a wedged
        # loop OR a silently-stalled socket both let systemd's WatchdogSec escalate.
        self._notifier = notifier or sd_notify.SystemdNotifier(None)
        self._watchdog_liveness_window_s = watchdog_liveness_window_s
        self._last_msg_monotonic = 0.0

        # ADR-039 §D layer-2: this shard's heartbeat ({dispatched, bytes_in, rows, ts_ns})
        # written every interval; the stall-detector timer reads all shards' heartbeats to
        # catch a hung/asymmetric shard (cross-process — layer 1 sd_notify only catches a
        # wedged loop within THIS process).
        self._heartbeat_dir = (heartbeat_dir if heartbeat_dir is not None
                               else cfg.CAPTURE_HEARTBEAT_DIR)
        self._heartbeat_interval_s = heartbeat_interval_s
        self._last_hb_monotonic = 0.0

        self._stop = asyncio.Event()
        self._current_mgr: Any = None

    # -- routing --

    def _writes_halted(self) -> bool:
        """True iff EITHER guard is in a CRITICAL halt (free space OR inodes)."""
        return ((self._disk_guard is not None and self._disk_guard.halted)
                or (self._inode_guard is not None and self._inode_guard.halted))

    def _on_message(self, stream: str, data: Any, recv_ns: int) -> None:
        # Liveness for the systemd watchdog: stamp BEFORE the halt guard. A CRITICAL halt
        # intentionally DROPS data (forward-only) but the socket is alive and the process
        # is behaving correctly — it must keep feeding the watchdog. Use a monotonic clock
        # (not recv_ns wall-clock) so an NTP step can't corrupt the watchdog age.
        self._last_msg_monotonic = time.monotonic()
        # Guard CRITICAL (byte OR inode): drop incoming firehose data (forward-only —
        # a hole is recorded by absence; we never backfill). Resumes on recovery.
        if self._writes_halted():
            return
        if stream.endswith("@aggTrade"):
            self._agg.append(aggtrade_row(data, recv_ns))
        elif "@depth@" in stream:
            self._handle_depth(data, recv_ns)
        elif stream.endswith("@bookTicker"):
            self._bookticker.append(bookticker_row(data, recv_ns))
        elif stream == "!forceOrder@arr":
            for row in forceorder_rows(data, recv_ns):
                self._forceorder.append(row)
        elif stream.startswith("!markPrice@arr"):
            for row in markprice_rows(data, recv_ns):
                self._markprice.append(row)

    # -- depth maintenance --

    def _handle_depth(self, data: dict, recv_ns: int) -> None:
        # Store EVERY raw diff regardless of maintenance state.
        self._depth.append(depth_row(data, recv_ns))
        symbol = data["s"]
        m = self._maintainers.get(symbol)
        if m is None:
            m = self._maintainers[symbol] = DepthMaintainer(symbol)
        # Maintenance works in EVENT-time ms so gap bounds match the manifest's
        # *_ms columns (and the conn-manager gaps). Levels are fed ONLY when depth_state
        # is enabled — otherwise the maintainer stays cursor-only (no book, no fat buffer).
        b, a = (data.get("b"), data.get("a")) if self._depth_state_enabled else (None, None)
        res = m.on_diff(int(data["U"]), int(data["u"]), int(data["pu"]),
                        int(data["E"]), bids=b, asks=a)
        self._apply_depth_result(symbol, res)

    def _on_snapshot_arrived(self, symbol: str, snap: dict, recv_ns: int) -> None:
        self._snapshot.append(snapshot_row(symbol, snap, recv_ns))
        m = self._maintainers.get(symbol)
        if m is None:
            m = self._maintainers[symbol] = DepthMaintainer(symbol)
        # Seed the level book ONLY when depth_state is enabled; otherwise cursor-only.
        b, a = (snap.get("bids"), snap.get("asks")) if self._depth_state_enabled else (None, None)
        res = m.on_snapshot(int(snap["lastUpdateId"]), int(snap.get("E", 0)), bids=b, asks=a)
        self._apply_depth_result(symbol, res)

    def _apply_depth_result(self, symbol: str, res: Any) -> None:
        if res.gap is not None:
            start_ms, end_ms, reason = res.gap
            self._record_gap(symbol, "depth", start_ms, end_ms, reason)
        if res.needs_snapshot and self._snap_sched is not None:
            self._snap_sched.request(symbol)

    def seed_universe(self, symbols: Sequence[str]) -> None:
        """Request an initial depth snapshot for every symbol (paced by scheduler)."""
        if self._snap_sched is None:
            return
        for s in symbols:
            self._snap_sched.request(s)

    def _record_gap(self, symbol: str, stream: str, start_ms: int, end_ms: int,
                    reason: str) -> None:
        self._gaps.append({
            "symbol": symbol, "stream": stream, "gap_start_ms": start_ms,
            "gap_end_ms": end_ms, "reason": reason,
            "recorded_recv_ts_ns": time.time_ns(),
        })

    def _on_gap(self, streams: list[str], reason: str, start_ms: int, end_ms: int) -> None:
        # Connection-level gap (whole shard): one manifest row per per-symbol
        # stream. Array streams (! prefix) have no single symbol -> recorded as-is.
        for s in streams:
            symbol = s.split("@", 1)[0].upper() if not s.startswith("!") else s
            self._record_gap(symbol, s, start_ms, end_ms, reason)

    def _default_mgr_factory(self, streams: list[str]) -> cm.ConnectionManager:
        return cm.ConnectionManager(
            streams=streams, on_message=self._on_message, on_gap=self._on_gap,
            streams_per_conn=self._per_conn, connect_fn=self._connect_fn,
        )

    # -- lifecycle --

    def stop(self) -> None:
        """Request shutdown of the service and the live manager. Idempotent."""
        self._stop.set()
        if self._current_mgr is not None:
            self._current_mgr.stop()
        if self._snap_sched is not None:
            self._snap_sched.stop()

    def flush_all(self) -> None:
        for w in self._writers:
            w.flush_all()

    def flush_due(self) -> None:
        for w in self._writers:
            w.flush_due()

    def stats(self) -> dict:
        return {
            "agg_rows": self._agg.rows_written,
            "depth_rows": self._depth.rows_written,
            "bookticker_rows": self._bookticker.rows_written,
            "forceorder_rows": self._forceorder.rows_written,
            "markprice_rows": self._markprice.rows_written,
            "snapshot_rows": self._snapshot.rows_written,
            "gap_rows": self._gaps.rows_written,
        }

    async def _resolve_universe(self) -> list[str]:
        full = await asyncio.to_thread(self._client.fetch_usdtm_perp_universe)
        if self._shard is None:                       # single full-universe process
            return full
        return sharding.symbols_for_shard(full, self._shard, self._n_shards)

    async def _wait_stop_or_timeout(self, timeout: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=timeout)

    async def _flush_loop(self) -> None:
        # ADR-038 write-then-compact: flush every partition on the short
        # CAPTURE_FIREHOSE_FLUSH_S age (or the byte cap), so resident RAM only ever
        # holds ~one flush interval. Closed hours are merged later by the separate
        # compaction timer; the writer itself never buffers an hour (that OOM'd).
        while not self._stop.is_set():
            await self._wait_stop_or_timeout(self._flush_poll_s)
            if self._stop.is_set():
                break
            self._maybe_write_book_states()
            for w in self._writers:
                w.flush_due()
            self._maybe_enforce_guards()
            self._feed_watchdog_if_live()
            self._maybe_write_heartbeat()

    def _heartbeat_payload(self) -> dict:
        mgr = self._current_mgr
        return {
            "shard": "full" if self._shard is None else str(self._shard),
            "ts_ns": time.time_ns(),
            "dispatched": getattr(mgr, "dispatched", 0),
            "bytes_in": getattr(mgr, "bytes_in", 0),
            "rows": sum(w.rows_written for w in self._writers),
        }

    def _write_heartbeat(self) -> None:
        """Atomically write this shard's layer-2 heartbeat. Best-effort — a heartbeat write
        must NEVER take down the capture loop, so all errors degrade to a log line."""
        try:
            payload = self._heartbeat_payload()
            d = pathlib.Path(self._heartbeat_dir)
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"shard-{payload['shard']}.json"
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(path)                        # atomic rename
        except Exception:                            # noqa: BLE001
            logger.warning("capture-core heartbeat write failed", exc_info=True)

    def _maybe_write_book_states(self) -> None:
        """Every DEPTH_STATE_CADENCE_S, append a top-N book-state row for each
        SYNCED (valid) symbol to the depth_state dataset. Additive to the firehose;
        the writer flushes with the others. Buffered like any other writer. No-op
        while depth_state is disabled (no writer is created, and the maintainer is
        cursor-only so there is no book to sample)."""
        if self._depth_state is None:
            return
        now = time.monotonic()
        if now - self._last_book_state_monotonic < cfg.DEPTH_STATE_CADENCE_S:
            return
        self._last_book_state_monotonic = now
        recv_ns = time.time_ns()
        for symbol, m in self._maintainers.items():
            # Only fully-seeded-and-continuous (valid), non-empty books.
            if not (m.synced and m.bids and m.asks):
                continue
            # STRICTLY best-effort + per-symbol isolated: this runs on the SAME flush
            # loop that flushes the live firehose and runs the disk/inode guards +
            # watchdog, so a single bad book must NEVER take it down (mirrors
            # _write_heartbeat). One symbol's failure does not drop the others.
            try:
                self._depth_state.append(
                    book_state_row(symbol, m, recv_ns, cfg.DEPTH_STATE_TOP_N))
            except Exception:                        # noqa: BLE001
                logger.warning("capture-core book-state write failed for %s",
                               symbol, exc_info=True)

    def _maybe_write_heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._last_hb_monotonic < self._heartbeat_interval_s:
            return
        self._last_hb_monotonic = now
        self._write_heartbeat()

    def _feed_watchdog_if_live(self) -> None:
        """Heartbeat the systemd watchdog ONLY while messages are flowing. A wedged loop
        stops ticking this; a silently-stalled socket lets the liveness age exceed the
        window — both then let WatchdogSec escalate to a restart. Liveness keys on ANY
        stream: every shard (shard-0 included, which also owns the high-rate array
        streams) carries high-rate per-symbol streams, so a quiet moment never false-trips.
        """
        if (time.monotonic() - self._last_msg_monotonic) < self._watchdog_liveness_window_s:
            self._notifier.watchdog()

    def _maybe_enforce_guards(self) -> None:
        """Launch the disk + inode guard enforcement OFF the event loop on the guard
        cadence.

        The disk guard's scandir + rmtree (and the inode guard's notify) can take tens
        of seconds under a prune storm. Running them inline blocked the asyncio flush
        loop, so the WATCHDOG=1 feed on the next line was delayed past WatchdogSec=30s
        and systemd SIGABRT'd the shard. Enforcing in a worker thread as a BACKGROUND
        task keeps the loop iterating and feeding the watchdog — which still reflects
        GENUINE loop health (a truly wedged loop still misses the feed; see
        _feed_watchdog_if_live). At most ONE enforce in flight: skip while the previous
        is still running, so there are never overlapping prunes (no prune-vs-prune, and
        the off-loop pruner never races the writer — it also excludes the live date)."""
        if self._disk_guard is None and self._inode_guard is None:
            return
        if self._enforce_task is not None and not self._enforce_task.done():
            return                                       # previous enforce still running
        now = time.monotonic()
        if now - self._last_disk_check < self._disk_check_interval_s:
            return
        self._last_disk_check = now
        self._enforce_task = asyncio.create_task(self._run_guards_offloop())

    async def _run_guards_offloop(self) -> None:
        """Run the blocking guard enforcement in a worker thread (off the event loop).
        Best-effort: it runs as a detached task, so a guard failure degrades to a log
        line and never takes down the flush loop."""
        try:
            if self._disk_guard is not None:
                await asyncio.to_thread(self._disk_guard.enforce)
            if self._inode_guard is not None:
                await asyncio.to_thread(self._inode_guard.enforce)
        except Exception:                                # noqa: BLE001
            logger.warning("capture-core guard enforce failed", exc_info=True)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop)

    async def run(self) -> None:
        """Run until SIGTERM/SIGINT (or :meth:`stop`), re-resolving the universe."""
        if self._install_signals:
            self._install_signal_handlers()

        flush_task = asyncio.create_task(self._flush_loop())
        snap_task = (asyncio.create_task(self._snap_sched.run())
                     if self._snap_sched is not None else None)
        universe = await self._resolve_universe()
        self.seed_universe(universe)
        mgr = self._mgr_factory(capture_streams_for_shard(universe, owns_array_streams=self._owns_array))
        self._current_mgr = mgr
        mgr_task = asyncio.create_task(mgr.run())
        logger.info("capture-core service started: %d symbols", len(universe))
        # systemd READY=1: the shard is up (universe resolved, seed requested, manager
        # launched). Seed the liveness clock so the first watchdog feed isn't already stale
        # in the gap before the first frame arrives.
        self._last_msg_monotonic = time.monotonic()
        self._notifier.ready()

        try:
            while not self._stop.is_set():
                await self._wait_stop_or_timeout(self._reresolve_interval_s)
                if self._stop.is_set():
                    break
                new_universe = await self._resolve_universe()
                if universe_changed(universe, new_universe):
                    logger.info("capture-core: universe %d -> %d; rebuilding",
                                len(universe), len(new_universe))
                    mgr.stop()
                    await mgr_task
                    # Seed any newly-listed symbols (deduped by the scheduler).
                    self.seed_universe(sorted(set(new_universe) - set(universe)))
                    universe = new_universe
                    mgr = self._mgr_factory(capture_streams_for_shard(universe, owns_array_streams=self._owns_array))
                    self._current_mgr = mgr
                    mgr_task = asyncio.create_task(mgr.run())
                elif mgr_task.done():  # all shards exited unexpectedly -> restart
                    logger.warning("capture-core: manager ended; restarting")
                    mgr = self._mgr_factory(capture_streams_for_shard(universe, owns_array_streams=self._owns_array))
                    self._current_mgr = mgr
                    mgr_task = asyncio.create_task(mgr.run())
        finally:
            self._stop.set()
            mgr.stop()
            if self._snap_sched is not None:
                self._snap_sched.stop()
            with contextlib.suppress(Exception):
                await mgr_task
            for task in (flush_task, snap_task, self._enforce_task):
                if task is None:
                    continue
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self.flush_all()
            logger.info("capture-core service stopped: %s", self.stats())
