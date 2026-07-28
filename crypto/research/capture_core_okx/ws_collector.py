"""OKX Stage B WS collector — channel router, markPrice merge, and the daemon loop.

Wires :class:`OkxWsClient` to the SHARED store writers under the OKX root. Routing
(instId->symbol + ctVal resolution) and the markPrice 3-channel merge live here; the
byte-identical row shapes come from :mod:`ws_normalize`. Never-exiting daemon with the
Stage A shutdown contract (SIGTERM/SIGINT -> stop -> flush_all before SIGKILL).

Deploy refinements (BUILT-NOT-DEPLOYED, tracked for the gate/deploy): chunk the subscribe
args across frames for a large universe, and re-subscribe newly-added instruments on the
hourly re-resolve (the re-resolve here keeps symbol/ctVal current for existing subscriptions).
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional, Sequence

from crypto.research.capture_core import store
from crypto.research.capture_core_okx import config as cfg
from crypto.research.capture_core_okx.client import OkxRestClient
from crypto.research.capture_core_okx.ctval import parse_ctval_table
from crypto.research.capture_core_okx.symbols import (
    SymbolMap, filter_universe, instid_to_symbol,
)
from crypto.research.capture_core_okx.book_okx import OkxBookMaintainer
from crypto.research.capture_core_okx.ws_client import WS_PUBLIC_BASE, OkxWsClient
from crypto.research.capture_core_okx.ws_normalize import (
    okx_bbo_row, okx_book_state_row, okx_books_row, okx_liquidation_rows,
    okx_markprice_merge_row, okx_trades_row,
)

logger = logging.getLogger("mhde.crypto.capture_core_okx.ws_collector")

_SWAP_SUFFIX = "-USDT-SWAP"
MARKPRICE_EMIT_INTERVAL_S = 1.0                 # D5: 1s per symbol, mirroring Binance @1s
DEPTH_STATE_CADENCE_S = 5.0                     # Stage C: sample the maintained book every 5s
DEPTH_STATE_TOP_N = 20                          # brain depth primitive reads through level 20


def build_books_sub_args(universe: Sequence[str]) -> list[dict]:
    """OKX subscribe args for the depth daemon: the per-instId `books` (400-level) channel."""
    return [{"channel": "books", "instId": inst_id} for inst_id in universe]


def index_pair_to_symbol(index_inst_id: str) -> Optional[str]:
    """OKX index pair (``BTC-USDT``) -> Binance-style symbol, via the swap normalizer."""
    return instid_to_symbol(index_inst_id + "-SWAP")


def build_sub_args(universe: Sequence[str]) -> list[dict]:
    """OKX subscribe args: per-instId fast channels + venue-wide liquidation-orders."""
    args: list[dict] = []
    for inst_id in universe:
        for channel in ("trades", "bbo-tbt", "mark-price", "funding-rate"):
            args.append({"channel": channel, "instId": inst_id})
        if inst_id.endswith(_SWAP_SUFFIX):
            args.append({"channel": "index-tickers", "instId": inst_id[: -len("-SWAP")]})
    args.append({"channel": "liquidation-orders", "instType": "SWAP"})
    return args


class MarkPriceMergeState:
    """Last-seen mark / index / funding per symbol; a symbol emits only once all three seen."""

    def __init__(self) -> None:
        self._mark: dict[str, dict] = {}
        self._index: dict[str, str] = {}
        self._funding: dict[str, dict] = {}
        self._last_emit_ts: dict[str, str] = {}

    def update_mark(self, symbol: str, mark_px: str, ts: str) -> None:
        self._mark[symbol] = {"markPx": mark_px, "ts": ts}

    def update_index(self, symbol: str, idx_px: str) -> None:
        self._index[symbol] = idx_px

    def update_funding(self, symbol: str, funding_rate: str, funding_time: str) -> None:
        self._funding[symbol] = {"fundingRate": funding_rate, "fundingTime": funding_time}

    def invalidate(self) -> None:
        """Drop all last-seen state (called on socket break — never stale-fill across a gap)."""
        self._mark.clear()
        self._index.clear()
        self._funding.clear()
        self._last_emit_ts.clear()

    def emit(self, recv_ns: int) -> list[dict]:
        """One row per symbol that has all three channels AND a NEW mark since its last emit.

        Gating on mark-advanced is what makes the 1s emitter honest: over a reconnect gap or
        for a delisted/quiet symbol no fresh mark arrives, so nothing is emitted with an
        advancing recv_ts_ns (which would defeat the gap manifest / freshness checks).
        """
        rows = []
        for symbol, mark in self._mark.items():
            if symbol not in self._index or symbol not in self._funding:
                continue
            if self._last_emit_ts.get(symbol) == mark["ts"]:
                continue                                   # no new mark -> no stale re-emit
            rows.append(okx_markprice_merge_row(
                symbol=symbol, mark=mark, index={"idxPx": self._index[symbol]},
                funding=self._funding[symbol], recv_ns=recv_ns))
            self._last_emit_ts[symbol] = mark["ts"]
        return rows


class OkxWsCollector:
    """Route decoded OKX frames to the shared store writers; own the markPrice merge."""

    def __init__(
        self, *,
        symbol_map: SymbolMap,
        ctval_table: Mapping[str, Decimal],
        writers: Optional[Mapping[str, Any]] = None,
        root: Optional[str] = None,
        url: str = WS_PUBLIC_BASE,
        client: Optional[OkxRestClient] = None,
        connect_fn: Optional[Callable[[str], Any]] = None,
        emit_interval_s: float = MARKPRICE_EMIT_INTERVAL_S,
        recv_clock: Callable[[], int] = time.time_ns,
        sub_args: Optional[Sequence[dict]] = None,
        persist_raw_depth: bool = False,
        depth_top_n: int = DEPTH_STATE_TOP_N,
        depth_state_cadence_s: float = DEPTH_STATE_CADENCE_S,
    ) -> None:
        if writers is None:
            if root is None:
                raise ValueError("OkxWsCollector needs either writers or a root")
            writers = {
                "aggTrade": store.aggtrade_writer(root),
                "bookTicker": store.bookticker_writer(root),
                "markPrice": store.markprice_writer(root),
                "forceOrder": store.forceorder_writer(root),
                "depth": store.depth_writer(root),
                "depth_state": store.depth_state_writer(root),
                "_gaps": store.gap_writer(root),
            }
        self._writers = writers
        self._symbol_map = symbol_map
        self._ctval = dict(ctval_table)
        self._merge = MarkPriceMergeState()
        self._books: dict[str, OkxBookMaintainer] = {}      # Stage C: per-instId book maintainers
        self._persist_raw_depth = persist_raw_depth
        self._depth_top_n = depth_top_n
        self._url = url
        self._client_factory_client = client
        self._connect_fn = connect_fn
        self._emit_interval_s = emit_interval_s
        self._depth_sample_ticks = max(1, round(depth_state_cadence_s / emit_interval_s))
        self._recv_clock = recv_clock
        self._stop = asyncio.Event()
        self._client: Optional[OkxWsClient] = None
        self._sub_args: list[dict] = (
            list(sub_args) if sub_args is not None else build_sub_args(symbol_map.inst_ids))

    # -- routing -------------------------------------------------------------

    def on_frame(self, channel: str, inst_id: Optional[str], data: list, recv_ns: int) -> None:
        if channel == "trades":
            self._append_per_instid(inst_id, data, "aggTrade", okx_trades_row, recv_ns)
        elif channel == "bbo-tbt":
            self._append_per_instid(inst_id, data, "bookTicker", okx_bbo_row, recv_ns)
        elif channel == "books":
            self._handle_books(inst_id, data, recv_ns)
        elif channel == "mark-price":
            for el in data:
                symbol = self._symbol_map.symbol_for(el["instId"])
                if symbol is not None:
                    self._merge.update_mark(symbol, el["markPx"], el["ts"])
        elif channel == "index-tickers":
            for el in data:
                symbol = index_pair_to_symbol(el["instId"])
                if symbol is not None:
                    self._merge.update_index(symbol, el["idxPx"])
        elif channel == "funding-rate":
            for el in data:
                symbol = self._symbol_map.symbol_for(el["instId"])
                if symbol is not None:
                    self._merge.update_funding(symbol, el["fundingRate"], el["fundingTime"])
        elif channel == "liquidation-orders":
            for el in data:
                inst = el["instId"]
                symbol = self._symbol_map.symbol_for(inst)
                ct = self._ctval.get(inst)
                if symbol is None or ct is None:
                    continue
                for row in okx_liquidation_rows(el, symbol=symbol, ct_val=ct, recv_ns=recv_ns):
                    self._writers["forceOrder"].append(row)

    def _append_per_instid(self, inst_id, data, dataset, row_fn, recv_ns):
        symbol = self._symbol_map.symbol_for(inst_id) if inst_id else None
        ct = self._ctval.get(inst_id) if inst_id else None
        if symbol is None or ct is None:
            return
        for el in data:
            self._writers[dataset].append(row_fn(el, symbol=symbol, ct_val=ct, recv_ns=recv_ns))

    def _handle_books(self, inst_id, data, recv_ns):
        """Stage C: feed the per-instId book maintainer (depth_state) and, only when enabled,
        write the raw depth ladder tape. Snapshot vs update is prevSeqId==-1 inside the element."""
        symbol = self._symbol_map.symbol_for(inst_id) if inst_id else None
        ct = self._ctval.get(inst_id) if inst_id else None
        if symbol is None or ct is None:
            return
        maintainer = self._books.get(inst_id)
        if maintainer is None:
            maintainer = self._books[inst_id] = OkxBookMaintainer(symbol)
        for el in data:
            if self._persist_raw_depth:
                self._writers["depth"].append(
                    okx_books_row(el, symbol=symbol, ct_val=ct, recv_ns=recv_ns))
            bids = [[lvl[0], lvl[1]] for lvl in el["bids"]]     # drop OKX liqOrders/numOrders
            asks = [[lvl[0], lvl[1]] for lvl in el["asks"]]
            if int(el["prevSeqId"]) == -1:
                maintainer.on_snapshot(int(el["seqId"]), bids, asks)
            else:
                maintainer.on_update(int(el["seqId"]), int(el["prevSeqId"]), bids, asks)

    def sample_depth_state(self, recv_ns: int) -> None:
        """Append one depth_state row per synced non-empty book (the 5s sample loop)."""
        writer = self._writers.get("depth_state")
        if writer is None:
            return
        for inst_id, maintainer in self._books.items():
            if not maintainer.synced:
                continue
            ct = self._ctval.get(inst_id)
            if ct is None:
                continue
            try:                                                # per-symbol isolation: one bad
                bids, asks = maintainer.top_levels(self._depth_top_n)   # book can't abort the tick
                if not bids or not asks:
                    continue                                    # skip one-sided / empty books
                writer.append(okx_book_state_row(
                    maintainer, symbol=maintainer.symbol, ct_val=ct,
                    recv_ns=recv_ns, top_n=self._depth_top_n))
            except Exception:                                   # noqa: BLE001
                logger.exception("okx depth_state sample failed for %s", inst_id)

    def emit_markprice(self, recv_ns: int) -> None:
        for row in self._merge.emit(recv_ns):
            self._writers["markPrice"].append(row)

    def flush_due(self) -> None:
        """Age/size-driven flush of EVERY writer (not just markPrice) — the fast trades/bbo
        firehose buffers to RAM otherwise and OOM-SIGKILLs the daemon, losing its buffer."""
        for w in self._writers.values():
            if hasattr(w, "flush_due"):
                w.flush_due()

    def flush_all(self) -> None:
        for w in self._writers.values():
            if hasattr(w, "flush_all"):
                w.flush_all()

    # -- daemon --------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            self._client.stop()

    async def run(self) -> None:
        """Never-exiting WS daemon: subscribe, route, 1s markPrice emit, clean shutdown."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop)
            except (NotImplementedError, ValueError):       # e.g. non-main thread
                pass
        self._client = OkxWsClient(
            sub_args=self._sub_args, on_frame=self.on_frame, url=self._url,
            connect_fn=self._connect_fn, on_gap=self._on_socket_gap)
        emit_task = asyncio.create_task(self._emit_loop())
        try:
            await self._client.run()
        finally:
            self._stop.set()
            emit_task.cancel()
            await asyncio.gather(emit_task, return_exceptions=True)   # supervise: no orphan task
            self.flush_all()
            logger.info("capture-okx-ws: stopped, buffers flushed")

    def _on_socket_gap(self, reason: str) -> None:
        """On a socket break: record the gap, invalidate the merge state (no markPrice stale-fill),
        and drop every book maintainer to unsynced (never apply a book across the outage)."""
        self._record_gap(reason)
        self._merge.invalidate()
        for maintainer in self._books.values():
            maintainer.reset()

    async def _emit_loop(self) -> None:
        """1s tick: emit markPrice + (every DEPTH_STATE_CADENCE_S) sample depth_state + flush ALL
        writers. Resilient — a transient writer error (e.g. ENOSPC) is logged and the loop
        continues, so the fast-writer flush contract is never silently lost; only stop/cancel ends it."""
        ticks = 0
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self._emit_interval_s)
                ticks += 1
                self.emit_markprice(self._recv_clock())
                if ticks % self._depth_sample_ticks == 0:
                    self.sample_depth_state(self._recv_clock())
                self.flush_due()
            except asyncio.CancelledError:
                raise
            except Exception:                              # noqa: BLE001 — never die silently
                logger.exception("okx ws emit/flush loop iteration failed; continuing")

    def _record_gap(self, reason: str) -> None:
        gap_writer = self._writers.get("_gaps")
        if gap_writer is None:
            return
        now_ms = self._recv_clock() // 1_000_000
        gap_writer.append({
            "symbol": "*", "stream": "ws", "gap_start_ms": now_ms, "gap_end_ms": now_ms,
            "reason": "socket_break", "recorded_recv_ts_ns": self._recv_clock(),
        })
        gap_writer.flush_all()


async def run_for_window(collector: "OkxWsCollector", duration_s: float, *,
                         sleep_fn: Callable[[float], Any] = asyncio.sleep) -> None:
    """Run the daemon for a bounded window then stop+flush — the live-gate path."""
    task = asyncio.create_task(collector.run())
    await sleep_fn(duration_s)
    collector.stop()
    await task


def build_okx_ws_collector(
    root: str, *,
    client: Optional[OkxRestClient] = None,
    universe: Optional[Sequence[str]] = None,
    url: str = WS_PUBLIC_BASE,
) -> OkxWsCollector:
    """Resolve the universe + ctVal from ``/public/instruments`` and wire a WS collector."""
    client = client or OkxRestClient()
    raw, _ = client.get_with_weight("/api/v5/public/instruments", {"instType": "SWAP"})
    inst_ids = list(universe) if universe is not None else filter_universe(raw)
    return OkxWsCollector(
        symbol_map=SymbolMap(inst_ids), ctval_table=parse_ctval_table(raw),
        root=root, url=url, client=client)


def build_okx_books_collector(
    root: str, *,
    client: Optional[OkxRestClient] = None,
    universe: Optional[Sequence[str]] = None,
    url: str = WS_PUBLIC_BASE,
    persist_raw_depth: bool = False,
) -> OkxWsCollector:
    """Stage C: a books-only depth collector (separate daemon). Subscribes only the `books`
    channel; feeds depth_state (always) + the raw depth tape (only when persist_raw_depth)."""
    client = client or OkxRestClient()
    raw, _ = client.get_with_weight("/api/v5/public/instruments", {"instType": "SWAP"})
    inst_ids = list(universe) if universe is not None else filter_universe(raw)
    return OkxWsCollector(
        symbol_map=SymbolMap(inst_ids), ctval_table=parse_ctval_table(raw),
        root=root, url=url, client=client,
        sub_args=build_books_sub_args(inst_ids), persist_raw_depth=persist_raw_depth)
