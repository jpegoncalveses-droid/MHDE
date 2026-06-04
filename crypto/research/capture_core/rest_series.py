"""Declarative registry of REST present-state series for capture-core.

Each series is one :class:`SeriesSpec` — endpoint, scope, pool, request weight,
target cadence, priority, query params, parquet schema, and a pure parser
(``json, symbol_or_pair, recv_ns -> list[row]``). Adding a series later is one
entry here; the collector and store stay untouched.

Default-to-inclusion: this captures the *available* public REST present-state,
not a curated feature set. Raw values only — any change/delta/zscore is derived
DOWNSTREAM, so the collector stays pure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import pyarrow as pa

from crypto.research.capture_core import config as cfg

# -- per-series parquet schemas (venue numeric fields kept as strings, lossless) --

OPEN_INTEREST_SCHEMA = pa.schema([
    ("recv_ts_ns", pa.int64()), ("s", pa.string()),
    ("openInterest", pa.string()), ("time", pa.int64()),
])

PREMIUM_INDEX_SCHEMA = pa.schema([
    ("recv_ts_ns", pa.int64()), ("s", pa.string()),
    ("markPrice", pa.string()), ("indexPrice", pa.string()),
    ("estimatedSettlePrice", pa.string()), ("lastFundingRate", pa.string()),
    ("interestRate", pa.string()), ("nextFundingTime", pa.int64()),
    ("time", pa.int64()),
])

# global/top account + top position ratios share a shape.
LS_RATIO_SCHEMA = pa.schema([
    ("recv_ts_ns", pa.int64()), ("s", pa.string()),
    ("longAccount", pa.string()), ("shortAccount", pa.string()),
    ("longShortRatio", pa.string()), ("timestamp", pa.int64()),
])

TAKER_LS_SCHEMA = pa.schema([
    ("recv_ts_ns", pa.int64()), ("s", pa.string()),
    ("buySellRatio", pa.string()), ("buyVol", pa.string()),
    ("sellVol", pa.string()), ("timestamp", pa.int64()),
])

BASIS_SCHEMA = pa.schema([
    ("recv_ts_ns", pa.int64()), ("pair", pa.string()), ("contractType", pa.string()),
    ("indexPrice", pa.string()), ("futuresPrice", pa.string()),
    ("basis", pa.string()), ("basisRate", pa.string()),
    ("annualizedBasisRate", pa.string()), ("timestamp", pa.int64()),
])


# -- pure parsers (json -> rows) --

def _parse_open_interest(data: Any, symbol: Optional[str], recv_ns: int) -> list[dict]:
    return [{"recv_ts_ns": recv_ns, "s": data["symbol"],
             "openInterest": data["openInterest"], "time": int(data["time"])}]


def _parse_premium_index(data: Any, _symbol: Optional[str], recv_ns: int) -> list[dict]:
    rows = data if isinstance(data, list) else [data]
    return [{"recv_ts_ns": recv_ns, "s": d["symbol"], "markPrice": d["markPrice"],
             "indexPrice": d["indexPrice"],
             "estimatedSettlePrice": d.get("estimatedSettlePrice", ""),
             "lastFundingRate": d.get("lastFundingRate", ""),
             "interestRate": d.get("interestRate", ""),
             "nextFundingTime": int(d.get("nextFundingTime", 0)),
             "time": int(d["time"])} for d in rows]


def _parse_ls_ratio(data: Any, symbol: Optional[str], recv_ns: int) -> list[dict]:
    return [{"recv_ts_ns": recv_ns, "s": d.get("symbol", symbol),
             "longAccount": d.get("longAccount", ""),
             "shortAccount": d.get("shortAccount", ""),
             "longShortRatio": d.get("longShortRatio", ""),
             "timestamp": int(d["timestamp"])} for d in data]


def _parse_taker_ls(data: Any, symbol: Optional[str], recv_ns: int) -> list[dict]:
    return [{"recv_ts_ns": recv_ns, "s": symbol,
             "buySellRatio": d.get("buySellRatio", ""),
             "buyVol": d.get("buyVol", ""), "sellVol": d.get("sellVol", ""),
             "timestamp": int(d["timestamp"])} for d in data]


def _parse_basis(data: Any, pair: Optional[str], recv_ns: int) -> list[dict]:
    return [{"recv_ts_ns": recv_ns, "pair": d.get("pair", pair),
             "contractType": d.get("contractType", ""),
             "indexPrice": d.get("indexPrice", ""),
             "futuresPrice": d.get("futuresPrice", ""),
             "basis": d.get("basis", ""), "basisRate": d.get("basisRate", ""),
             "annualizedBasisRate": d.get("annualizedBasisRate", ""),
             "timestamp": int(d["timestamp"])} for d in data]


@dataclass(frozen=True)
class SeriesSpec:
    name: str                 # parquet dataset name
    endpoint: str             # REST path
    scope: str                # "per_symbol" | "all" | "per_pair"
    pool: str                 # "fapi" (weight-counted) | "futures_data" (separate pool)
    weight: int               # request weight (fapi pool); 0 for futures_data
    target_cadence_s: float   # desired seconds between full cycles
    priority: str             # "HIGH" | "MED" | "LOW"
    schema: pa.Schema = field(repr=False)
    symbol_key: str           # partition symbol field ("s" or "pair")
    time_key: str             # partition time field ("time" or "timestamp")
    parse: Callable[[Any, Optional[str], int], list[dict]] = field(repr=False)
    params: dict = field(default_factory=dict)

    def request(self, key: Optional[str]) -> tuple[str, dict]:
        """(path, params) for a per-symbol/per-pair ``key`` (or all-in-one)."""
        if self.scope == "per_symbol":
            return self.endpoint, {**self.params, "symbol": key}
        if self.scope == "per_pair":
            return self.endpoint, {**self.params, "pair": key}
        return self.endpoint, dict(self.params)


_FIVE_MIN = {"period": "5m", "limit": 1}

#: Default-to-inclusion present-state set. Cadence rule (anti-bias): open_interest
#: is real-time so its cadence is budget-driven (60s here, a safe default well
#: under budget — finer is a one-line change). The /futures/data ratio/basis series
#: are 5m-native, but their *sampling* cadence is COARSENED to FUTURES_DATA_CADENCE_S
#: (20 min): a full 529-symbol sweep of the 4 ratio series + basis ≈ 2,645 requests
#: cannot fit under the verified /futures/data IP ceiling (~700 req/5min) any faster
#: (see config.FUTURES_DATA_* and the rest_collector raw-count pacer).
_FD = cfg.FUTURES_DATA_CADENCE_S
SERIES: list[SeriesSpec] = [
    SeriesSpec("open_interest", "/fapi/v1/openInterest", "per_symbol", "fapi", 1,
               60.0, "HIGH", OPEN_INTEREST_SCHEMA, "s", "time", _parse_open_interest),
    SeriesSpec("premium_index", "/fapi/v1/premiumIndex", "all", "fapi", 10,
               60.0, "HIGH", PREMIUM_INDEX_SCHEMA, "s", "time", _parse_premium_index),
    SeriesSpec("global_ls_account", "/futures/data/globalLongShortAccountRatio",
               "per_symbol", "futures_data", 0, _FD, "MED", LS_RATIO_SCHEMA,
               "s", "timestamp", _parse_ls_ratio, dict(_FIVE_MIN)),
    SeriesSpec("top_ls_account", "/futures/data/topLongShortAccountRatio",
               "per_symbol", "futures_data", 0, _FD, "MED", LS_RATIO_SCHEMA,
               "s", "timestamp", _parse_ls_ratio, dict(_FIVE_MIN)),
    SeriesSpec("top_ls_position", "/futures/data/topLongShortPositionRatio",
               "per_symbol", "futures_data", 0, _FD, "MED", LS_RATIO_SCHEMA,
               "s", "timestamp", _parse_ls_ratio, dict(_FIVE_MIN)),
    SeriesSpec("taker_ls_ratio", "/futures/data/takerlongshortRatio",
               "per_symbol", "futures_data", 0, _FD, "MED", TAKER_LS_SCHEMA,
               "s", "timestamp", _parse_taker_ls, dict(_FIVE_MIN)),
    SeriesSpec("basis", "/futures/data/basis", "per_pair", "futures_data", 0,
               _FD, "LOW", BASIS_SCHEMA, "pair", "timestamp", _parse_basis,
               {**_FIVE_MIN, "contractType": "PERPETUAL"}),
]
