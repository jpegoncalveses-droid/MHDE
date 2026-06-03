"""Tests for the capture-core service (routing, gap mapping, universe rebuild)."""
from __future__ import annotations

import asyncio
import json
import pathlib

import pyarrow.parquet as pq

from crypto.research.capture_core import conn_manager as cm
from crypto.research.capture_core import service as svc


def _read(root, dataset):
    rows = []
    for fp in sorted(pathlib.Path(root, dataset).rglob("*.parquet")):
        rows.extend(pq.read_table(str(fp)).to_pylist())
    return rows


# -- pure helpers --

def test_aggtrade_streams_lowercases_and_suffixes():
    assert svc.aggtrade_streams(["BTCUSDT", "ETHUSDT"]) == ["btcusdt@aggTrade", "ethusdt@aggTrade"]


def test_aggtrade_row_coerces_types_and_keeps_price_strings():
    data = {"e": "aggTrade", "E": "1717", "a": "9", "s": "BTCUSDT",
            "p": "100.5", "q": "2.0", "f": "1", "l": "2", "T": "1716", "m": True}
    row = svc.aggtrade_row(data, recv_ns=42)
    assert row["recv_ts_ns"] == 42
    assert row["E"] == 1717 and row["a"] == 9 and row["T"] == 1716
    assert row["p"] == "100.5" and row["q"] == "2.0"   # lossless venue strings
    assert row["m"] is True


def test_universe_changed_is_order_insensitive():
    assert svc.universe_changed(["A", "B"], ["B", "A", "C"]) is True
    assert svc.universe_changed(["A", "B"], ["B", "A"]) is False


# -- gap mapping --

def test_on_gap_writes_one_row_per_symbol(tmp_path):
    s = svc.CaptureService(root=str(tmp_path), client=None)
    s._on_gap(["btcusdt@aggTrade", "ethusdt@aggTrade"], "reconnect", 1_748_563_200_000,
              1_748_563_205_000)
    s.flush_all()
    rows = _read(str(tmp_path), "_gaps")
    syms = {r["symbol"] for r in rows}
    assert syms == {"BTCUSDT", "ETHUSDT"}
    assert all(r["reason"] == "reconnect" for r in rows)


# -- end-to-end aggTrade capture through the real manager --

def _frame(symbol, price):
    return json.dumps({"stream": f"{symbol.lower()}@aggTrade",
                       "data": {"e": "aggTrade", "E": 1_748_563_200_000, "a": 1,
                                "s": symbol, "p": price, "q": "1.0", "f": 1, "l": 1,
                                "T": 1_748_563_200_000, "m": False}})


class _FakeConn:
    def __init__(self, messages):
        self._m = list(messages)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def recv(self):
        if self._m:
            return self._m.pop(0)
        raise ConnectionError("closed")


def test_service_captures_aggtrade_to_parquet_end_to_end(tmp_path):
    s = svc.CaptureService(root=str(tmp_path), client=None)
    conn = _FakeConn([_frame("BTCUSDT", "100.0"), _frame("ETHUSDT", "42.0")])

    count = [0]
    real = s._on_message

    def counting(stream, data, recv_ns):
        real(stream, data, recv_ns)
        count[0] += 1
        if count[0] == 2:
            mgr.stop()

    mgr = cm.ConnectionManager(
        streams=["btcusdt@aggTrade", "ethusdt@aggTrade"],
        on_message=counting, on_gap=s._on_gap,
        connect_fn=lambda url: conn,
        proactive_reconnect_s=10**9, sleep_fn=lambda x: asyncio.sleep(0),
        time_fn=lambda: 0.0,
    )
    asyncio.run(mgr.run())
    s.flush_all()

    rows = _read(str(tmp_path), "aggTrade")
    by = {r["s"]: r for r in rows}
    assert by["BTCUSDT"]["p"] == "100.0"
    assert by["ETHUSDT"]["p"] == "42.0"


# -- universe re-resolve supervisor rebuilds on change --

class _FakeMgr:
    def __init__(self, streams):
        self.streams = streams
        self._ev = asyncio.Event()
        self.stopped = False

    async def run(self):
        await self._ev.wait()

    def stop(self):
        self.stopped = True
        self._ev.set()


class _FakeClient:
    def __init__(self, sequence):
        self._seq = list(sequence)
        self._i = 0

    def fetch_usdtm_perp_universe(self):
        u = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return list(u)


def test_run_rebuilds_manager_when_universe_changes(tmp_path):
    created: list[_FakeMgr] = []

    def factory(streams):
        m = _FakeMgr(streams)
        created.append(m)
        return m

    client = _FakeClient([["BTCUSDT"], ["BTCUSDT", "ETHUSDT"]])
    s = svc.CaptureService(
        root=str(tmp_path), client=client, mgr_factory=factory,
        reresolve_interval_s=0.0, flush_interval_s=10**6, install_signals=False,
    )

    async def scenario():
        task = asyncio.create_task(s.run())
        for _ in range(2000):
            if len(created) >= 2:
                break
            await asyncio.sleep(0)
        s.stop()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(scenario())

    assert created[0].streams == ["btcusdt@aggTrade"]
    assert created[1].streams == ["btcusdt@aggTrade", "ethusdt@aggTrade"]
    assert created[0].stopped is True            # first manager was torn down on change
