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
from crypto.research.capture_core_okx.ws_client import WS_PUBLIC_BASE, OkxWsClient
from crypto.research.capture_core_okx.ws_normalize import (
    okx_bbo_row, okx_liquidation_rows, okx_markprice_merge_row, okx_trades_row,
)

logger = logging.getLogger("mhde.crypto.capture_core_okx.ws_collector")

_SWAP_SUFFIX = "-USDT-SWAP"
MARKPRICE_EMIT_INTERVAL_S = 1.0                 # D5: 1s per symbol, mirroring Binance @1s


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

    def update_mark(self, symbol: str, mark_px: str, ts: str) -> None:
        self._mark[symbol] = {"markPx": mark_px, "ts": ts}

    def update_index(self, symbol: str, idx_px: str) -> None:
        self._index[symbol] = idx_px

    def update_funding(self, symbol: str, funding_rate: str, funding_time: str) -> None:
        self._funding[symbol] = {"fundingRate": funding_rate, "fundingTime": funding_time}

    def emit(self, recv_ns: int) -> list[dict]:
        rows = []
        for symbol, mark in self._mark.items():
            if symbol in self._index and symbol in self._funding:
                rows.append(okx_markprice_merge_row(
                    symbol=symbol, mark=mark, index={"idxPx": self._index[symbol]},
                    funding=self._funding[symbol], recv_ns=recv_ns))
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
        recv_clock: Callable[[], int] = time.time_ns,
    ) -> None:
        if writers is None:
            if root is None:
                raise ValueError("OkxWsCollector needs either writers or a root")
            writers = {
                "aggTrade": store.aggtrade_writer(root),
                "bookTicker": store.bookticker_writer(root),
                "markPrice": store.markprice_writer(root),
                "forceOrder": store.forceorder_writer(root),
                "_gaps": store.gap_writer(root),
            }
        self._writers = writers
        self._symbol_map = symbol_map
        self._ctval = dict(ctval_table)
        self._merge = MarkPriceMergeState()
        self._url = url
        self._client_factory_client = client
        self._recv_clock = recv_clock
        self._stop = asyncio.Event()
        self._client: Optional[OkxWsClient] = None
        self._sub_args: list[dict] = build_sub_args(symbol_map.inst_ids)

    # -- routing -------------------------------------------------------------

    def on_frame(self, channel: str, inst_id: Optional[str], data: list, recv_ns: int) -> None:
        if channel == "trades":
            self._append_per_instid(inst_id, data, "aggTrade", okx_trades_row, recv_ns)
        elif channel == "bbo-tbt":
            self._append_per_instid(inst_id, data, "bookTicker", okx_bbo_row, recv_ns)
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

    def emit_markprice(self, recv_ns: int) -> None:
        for row in self._merge.emit(recv_ns):
            self._writers["markPrice"].append(row)

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
            on_gap=lambda reason: self._record_gap(reason))
        emit_task = asyncio.create_task(self._emit_loop())
        try:
            await self._client.run()
        finally:
            self._stop.set()
            emit_task.cancel()
            self.flush_all()
            logger.info("capture-okx-ws: stopped, buffers flushed")

    async def _emit_loop(self) -> None:
        try:
            while not self._stop.is_set():
                await asyncio.sleep(MARKPRICE_EMIT_INTERVAL_S)
                self.emit_markprice(self._recv_clock())
                if hasattr(self._writers["markPrice"], "flush_due"):
                    self._writers["markPrice"].flush_due()
        except asyncio.CancelledError:
            pass

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
