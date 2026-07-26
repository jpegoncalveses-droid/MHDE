"""OKX Stage B WS — on-disk byte-identical round-trip through the SHARED store.

Each OKX-normalized row, written via the shared capture_core writer under the OKX root, must
land under `symbol=<BASEUSDT>/date=<UTC event day>` with a parquet column set IDENTICAL to the
Binance dataset schema. The venue difference must not leak past normalization.
"""
from __future__ import annotations

import glob
from decimal import Decimal

import pyarrow.parquet as pq

from crypto.research.capture_core import store
from crypto.research.capture_core_okx import ws_normalize as wn

_RECV_NS = 1_700_000_000_123_456_789
_TS = 1_700_000_000_123           # -> UTC 2023-11-14
_EXPECT_DATE = "2023-11-14"
_CT = Decimal("0.01")


def _only_parquet(root, dataset, symbol="BTCUSDT"):
    files = glob.glob(f"{root}/{dataset}/symbol={symbol}/date=*/*.parquet")
    assert len(files) == 1, f"expected 1 part file, got {files}"
    assert f"/date={_EXPECT_DATE}/" in files[0], files[0]
    return files[0]


def _physical_columns(parquet_file):
    """Columns physically in the part file (Hive symbol=/date= are path-encoded, not columns)."""
    return pq.ParquetFile(parquet_file).schema_arrow.names


def test_aggtrade_roundtrip_byte_identical(tmp_path):
    root = str(tmp_path / "capture_core_okx")
    d = {"instId": "BTC-USDT-SWAP", "tradeId": "42", "px": "42219.9", "sz": "3",
         "side": "sell", "ts": str(_TS), "count": "3"}
    row = wn.okx_trades_row(d, symbol="BTCUSDT", ct_val=_CT, recv_ns=_RECV_NS)
    w = store.aggtrade_writer(root)
    w.append(row)
    w.flush_all()

    f = _only_parquet(root, "aggTrade")
    assert _physical_columns(f) == store.AGGTRADE_SCHEMA.names       # byte-identical columns
    got = pq.read_table(f).to_pylist()[0]
    assert got["s"] == "BTCUSDT" and got["q"] == "0.03" and got["a"] == 42 and got["m"] is True


def test_bookticker_roundtrip_byte_identical(tmp_path):
    root = str(tmp_path / "capture_core_okx")
    d = {"asks": [["42224.7", "5", "0", "2"]], "bids": [["42224.6", "1", "0", "1"]],
         "ts": str(_TS), "seqId": 987654}
    row = wn.okx_bbo_row(d, symbol="BTCUSDT", ct_val=_CT, recv_ns=_RECV_NS)
    w = store.bookticker_writer(root)
    w.append(row)
    w.flush_all()

    f = _only_parquet(root, "bookTicker")
    assert _physical_columns(f) == store.BOOKTICKER_SCHEMA.names
    got = pq.read_table(f).to_pylist()[0]
    assert got["u"] == 987654 and got["B"] == "0.01" and got["A"] == "0.05"


def test_markprice_roundtrip_byte_identical(tmp_path):
    root = str(tmp_path / "capture_core_okx")
    row = wn.okx_markprice_merge_row(
        symbol="BTCUSDT", mark={"markPx": "42310.6", "ts": str(_TS)}, index={"idxPx": "42309.1"},
        funding={"fundingRate": "0.00012", "fundingTime": "1700000800000"}, recv_ns=_RECV_NS)
    w = store.markprice_writer(root)
    w.append(row)
    w.flush_all()

    f = _only_parquet(root, "markPrice")
    assert _physical_columns(f) == store.MARKPRICE_SCHEMA.names
    got = pq.read_table(f).to_pylist()[0]
    assert got["p"] == "42310.6" and got["P"] == "42310.6"       # D1: settle proxy == mark


def test_forceorder_roundtrip_byte_identical(tmp_path):
    root = str(tmp_path / "capture_core_okx")
    d = {"instId": "BTC-USDT-SWAP",
         "details": [{"side": "sell", "posSide": "long", "bkPx": "41000", "sz": "5", "ts": str(_TS)}]}
    rows = wn.okx_liquidation_rows(d, symbol="BTCUSDT", ct_val=_CT, recv_ns=_RECV_NS)
    w = store.forceorder_writer(root)
    for r in rows:
        w.append(r)
    w.flush_all()

    f = _only_parquet(root, "forceOrder")
    assert _physical_columns(f) == store.FORCEORDER_SCHEMA.names     # incl. NO 'e' column
    got = pq.read_table(f).to_pylist()[0]
    assert got["p"] == "41000" and got["ap"] == "41000" and got["S"] == "SELL" and got["q"] == "0.05"
