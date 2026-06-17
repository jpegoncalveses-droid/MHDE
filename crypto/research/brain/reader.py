"""Brain capture reader: a READ-ONLY pyarrow consumer of capture's aggTrade tape.

Reads ``<capture_root>/aggTrade/symbol=*/date=*/*.parquet`` and returns clean
trade dicts in ``recv_ts_ns`` order, advancing past a caller-supplied cursor.
Capture stores venue numerics as VARCHAR (lossless); the reader casts ``p``/``q``
to float here. Field names are cleaned from the terse venue codes. Symbols are
UTF-8 (CJK / digit-leading exist on Binance USDT-M) — read straight through the
Hive partitioning / in-row ``s`` field, never an ASCII regex.

Part files within a ``symbol=/date=`` partition cover DISJOINT recv_ts_ns
windows but their (hash) filenames are not time-ordered, so the reader globally
sorts the result by ``recv_ts_ns`` (the canonical capture cursor field).

Strictly read-only: this module never writes anything, anywhere.
"""
from __future__ import annotations

import pathlib
from typing import Optional, Sequence

import pyarrow.compute as pc
import pyarrow.dataset as ds

from crypto.research.brain import config as cfg

# In-row venue columns we project (NOT the Hive ``symbol``/``date`` partition
# columns — we read the value from the in-row ``s`` to avoid a dictionary-vs-
# string collision and keep the read by physical schema).
_COLUMNS = ["recv_ts_ns", "E", "a", "s", "p", "q", "T", "m"]


def read_new_aggtrades(
    capture_root: str,
    after_recv_ts_ns: int = 0,
    symbols: Optional[Sequence[str]] = None,
) -> list[dict]:
    """Return clean aggTrade dicts with ``recv_ts_ns > after_recv_ts_ns``.

    Each dict: ``recv_ts_ns`` (int), ``symbol`` (str), ``event_time_ms`` (int),
    ``trade_time_ms`` (int), ``agg_id`` (int), ``price`` (float), ``qty`` (float),
    ``is_buyer_maker`` (bool), ``taker_buy`` (bool == not is_buyer_maker).
    Sorted ascending by ``recv_ts_ns``. Empty if the dataset is absent.
    """
    base = pathlib.Path(capture_root, cfg.AGGTRADE_DATASET)
    if not base.exists() or not any(base.rglob("*.parquet")):
        return []

    dataset = ds.dataset(str(base), format="parquet", partitioning="hive")
    flt = pc.field("recv_ts_ns") > after_recv_ts_ns
    if symbols is not None:
        # Filter on the in-row ``s`` (string) — robust regardless of partition
        # dictionary encoding; partition pruning still applies via the path.
        flt = flt & pc.field("s").isin(list(symbols))
    table = dataset.to_table(columns=_COLUMNS, filter=flt)
    table = table.sort_by([("recv_ts_ns", "ascending")])

    out: list[dict] = []
    for r in table.to_pylist():
        m = bool(r["m"])
        out.append({
            "recv_ts_ns": int(r["recv_ts_ns"]),
            "symbol": r["s"],
            "event_time_ms": int(r["E"]),
            "trade_time_ms": int(r["T"]),
            "agg_id": int(r["a"]),
            "price": float(r["p"]),   # VARCHAR -> float
            "qty": float(r["q"]),     # VARCHAR -> float
            "is_buyer_maker": m,
            "taker_buy": not m,       # m=False -> taker BUY
        })
    return out
