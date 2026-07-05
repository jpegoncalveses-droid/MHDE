"""OKX series registry: the 7 as-of specs + the klines spec (Stage A).

Schema parity BY CONSTRUCTION: every spec carries the capture_core schema
OBJECT for its dataset (imported, not copied) and the same dataset name, so a
drift is impossible without breaking the pinning tests. Parsers normalize at
write time — Binance-style symbols, Binance field letters, coin units — per
the recon mapping (recon Q1):

  * open_interest      <- one bulk ``/public/open-interest?instType=SWAP`` call;
                          value = ``oiCcy`` (coin units, matching Binance).
  * premium_index      <- the ``join:premium_index`` composite (mark-price +
                          index-tickers + funding-rate ANY); estimatedSettlePrice
                          has no OKX equivalent -> "" (Binance parser convention);
                          Binance nextFundingTime <- OKX fundingTime.
  * 3 ratio series     <- per-instId rubik contract endpoints; OKX returns only
                          the ratio r -> longAccount = r/(1+r), shortAccount =
                          1/(1+r) (algebraic identities), longShortRatio = r
                          verbatim.
  * taker_ls_ratio     <- per-instId taker-volume-contract with ``unit=0``
                          (coin; the default 1 is CONTRACTS — a mapping trap);
                          rows are [ts, sellVol, buyVol] (SELL FIRST);
                          buySellRatio computed, "" when sellVol == 0.
  * basis              <- COMPUTED at poll time from the ``join:basis``
                          composite: basis = last - idx (Binance's last-vs-index
                          definition), basisRate = basis/idx, annualized "" —
                          OKX has no basis endpoint (recon gap 1b).
  * klines_1h          <- ``/market/candles?bar=1H``; closed bars only via the
                          ``confirm`` flag; volume <- volCcy (coin), quoteVolume
                          <- volCcyQuote; trades / takerBuyBase / takerBuyQuote
                          have NO OKX source -> honest NULLs (the brain klines
                          reader is null-tolerant; see
                          test_brain_klines_null_tolerance).

Raw venue strings are kept verbatim wherever a venue value maps 1:1; only the
DERIVED numbers (long/short split, buy/sell ratio, basis) are formatted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import pyarrow as pa

from crypto.research.capture_core import config as cc_cfg
from crypto.research.capture_core import klines_store as ks
from crypto.research.capture_core import rest_series as rs
from crypto.research.capture_core_okx import config as cfg
from crypto.research.capture_core_okx.symbols import SymbolMap, instid_to_symbol

_RUBIK = "/api/v5/rubik/stat"
_INDEX_SUFFIX_LEN = len("-SWAP")


def _fmt(x: float) -> str:
    """Format a derived number as a lossless-enough venue-style string."""
    return format(x, ".12g")


@dataclass(frozen=True)
class OkxSeriesSpec:
    """capture_core ``SeriesSpec`` twin whose per-symbol key is an OKX instId."""
    name: str                 # parquet dataset name (== the Binance dataset name)
    endpoint: str             # OKX REST path, or a client ``join:`` composite
    scope: str                # "per_symbol" (key = instId) | "all"
    pool: str                 # kept "fapi": no used-weight header -> pacer idle
    weight: int
    target_cadence_s: float
    priority: str
    schema: pa.Schema = field(repr=False)
    symbol_key: str
    time_key: str
    parse: Callable[[Any, Optional[str], int], list[dict]] = field(repr=False)
    params: dict = field(default_factory=dict)
    dedup_ts_field: Optional[str] = None

    def request(self, key: Optional[str]) -> tuple[str, dict]:
        if self.scope == "per_symbol":
            return self.endpoint, {**self.params, "instId": key}
        return self.endpoint, dict(self.params)


def _index_id(inst_id: str) -> str:
    """The index instId backing a swap: ``BTC-USDT-SWAP`` -> ``BTC-USDT``."""
    return inst_id[:-_INDEX_SUFFIX_LEN]


# -- parsers (closures over the shared SymbolMap so the hourly universe
#    re-resolve updates the all-scope filters in place) -------------------------

def _parse_open_interest(symbol_map: SymbolMap):
    def parse(data: Any, _key: Optional[str], recv_ns: int) -> list[dict]:
        rows = []
        for d in data:
            symbol = symbol_map.symbol_for(d.get("instId", ""))
            if symbol is None:
                continue
            rows.append({"recv_ts_ns": recv_ns, "s": symbol,
                         "openInterest": d["oiCcy"], "time": int(d["ts"])})
        return rows
    return parse


def _parse_premium_index(symbol_map: SymbolMap):
    def parse(data: Any, _key: Optional[str], recv_ns: int) -> list[dict]:
        idx_by = {d["instId"]: d for d in data.get("index", [])}
        fund_by = {d["instId"]: d for d in data.get("funding", [])}
        rows = []
        for m in data.get("mark", []):
            inst_id = m.get("instId", "")
            symbol = symbol_map.symbol_for(inst_id)
            if symbol is None:
                continue
            idx = idx_by.get(_index_id(inst_id), {})
            fund = fund_by.get(inst_id, {})
            rows.append({
                "recv_ts_ns": recv_ns, "s": symbol,
                "markPrice": m["markPx"],
                "indexPrice": idx.get("idxPx", ""),
                "estimatedSettlePrice": "",          # no OKX equivalent
                "lastFundingRate": fund.get("fundingRate", ""),
                "interestRate": fund.get("interestRate", ""),
                # OKX fundingTime (current settlement) = Binance nextFundingTime
                "nextFundingTime": int(fund.get("fundingTime", 0) or 0),
                "time": int(m["ts"]),
            })
        return rows
    return parse


def _parse_ls_ratio(data: Any, key: Optional[str], recv_ns: int) -> list[dict]:
    # per_symbol: the key is a universe instId by construction -> mechanical map.
    symbol = instid_to_symbol(key or "")
    if symbol is None:
        return []
    rows = []
    for ts, ratio in data:
        r = float(ratio)
        rows.append({"recv_ts_ns": recv_ns, "s": symbol,
                     "longAccount": _fmt(r / (1.0 + r)),
                     "shortAccount": _fmt(1.0 / (1.0 + r)),
                     "longShortRatio": ratio,
                     "timestamp": int(ts)})
    return rows


def _parse_taker(data: Any, key: Optional[str], recv_ns: int) -> list[dict]:
    symbol = instid_to_symbol(key or "")
    if symbol is None:
        return []
    rows = []
    for ts, sell_vol, buy_vol in data:       # SELL FIRST in the venue row
        sell = float(sell_vol)
        ratio = "" if sell == 0.0 else _fmt(float(buy_vol) / sell)
        rows.append({"recv_ts_ns": recv_ns, "s": symbol,
                     "buySellRatio": ratio, "buyVol": buy_vol,
                     "sellVol": sell_vol, "timestamp": int(ts)})
    return rows


def _parse_basis(symbol_map: SymbolMap):
    def parse(data: Any, _key: Optional[str], recv_ns: int) -> list[dict]:
        idx_by = {d["instId"]: d for d in data.get("index", [])}
        rows = []
        for t in data.get("tickers", []):
            inst_id = t.get("instId", "")
            symbol = symbol_map.symbol_for(inst_id)
            if symbol is None:
                continue
            idx = idx_by.get(_index_id(inst_id))
            last_s = t.get("last", "")
            if idx is None or not last_s or not idx.get("idxPx"):
                continue                      # no computable basis -> honest absence
            last, ip = float(last_s), float(idx["idxPx"])
            rows.append({
                "recv_ts_ns": recv_ns, "pair": symbol,
                "contractType": "PERPETUAL",
                "indexPrice": idx["idxPx"], "futuresPrice": last_s,
                "basis": _fmt(last - ip), "basisRate": _fmt((last - ip) / ip),
                "annualizedBasisRate": "",
                "timestamp": int(t["ts"]),
            })
        return rows
    return parse


def parse_candles(data: Any, key: Optional[str], recv_ns: int) -> list[dict]:
    """OKX candle arrays -> klines_1h rows; CLOSED bars only (confirm == "1").

    Array layout: [ts, o, h, l, c, vol(contracts), volCcy(coin),
    volCcyQuote(quote), confirm]. Newest-first order is irrelevant here (the
    collector's dedup cursor is order-independent). trades / takerBuy* have no
    OKX source -> None (nullable int64/string columns; the brain reader is
    null-tolerant).
    """
    symbol = instid_to_symbol(key or "")
    if symbol is None:
        return []
    rows = []
    for k in data:
        if k[8] != "1":                       # in-progress bar -> never persist
            continue
        open_time = int(k[0])
        rows.append({
            "recv_ts_ns": recv_ns, "s": symbol,
            "openTime": open_time, "open": k[1], "high": k[2], "low": k[3],
            "close": k[4],
            "volume": k[6],                   # volCcy = base-coin volume
            "closeTime": open_time + cc_cfg.HOUR_MS - 1,
            "quoteVolume": k[7],              # volCcyQuote
            "trades": None, "takerBuyBase": None, "takerBuyQuote": None,
        })
    return rows


_RUBIK_PARAMS = {"period": "5m", "limit": cfg.RUBIK_WINDOW_LIMIT}


def build_series(symbol_map: SymbolMap) -> list[OkxSeriesSpec]:
    """The 7 as-of specs, in ``CAPTURE_ASOF_DATASETS`` order."""
    ls = _parse_ls_ratio
    return [
        OkxSeriesSpec("open_interest", "/api/v5/public/open-interest", "all",
                      "fapi", 0, cfg.OI_CADENCE_S, "HIGH",
                      rs.OPEN_INTEREST_SCHEMA, "s", "time",
                      _parse_open_interest(symbol_map), {"instType": "SWAP"}),
        OkxSeriesSpec("premium_index", "join:premium_index", "all",
                      "fapi", 0, cfg.PREMIUM_INDEX_CADENCE_S, "HIGH",
                      rs.PREMIUM_INDEX_SCHEMA, "s", "time",
                      _parse_premium_index(symbol_map)),
        OkxSeriesSpec("global_ls_account",
                      f"{_RUBIK}/contracts/long-short-account-ratio-contract",
                      "per_symbol", "fapi", 0, cfg.RATIO_CADENCE_S, "MED",
                      rs.LS_RATIO_SCHEMA, "s", "timestamp", ls,
                      dict(_RUBIK_PARAMS), dedup_ts_field="timestamp"),
        OkxSeriesSpec("top_ls_account",
                      f"{_RUBIK}/contracts/long-short-account-ratio-contract-top-trader",
                      "per_symbol", "fapi", 0, cfg.RATIO_CADENCE_S, "MED",
                      rs.LS_RATIO_SCHEMA, "s", "timestamp", ls,
                      dict(_RUBIK_PARAMS), dedup_ts_field="timestamp"),
        OkxSeriesSpec("top_ls_position",
                      f"{_RUBIK}/contracts/long-short-position-ratio-contract-top-trader",
                      "per_symbol", "fapi", 0, cfg.RATIO_CADENCE_S, "MED",
                      rs.LS_RATIO_SCHEMA, "s", "timestamp", ls,
                      dict(_RUBIK_PARAMS), dedup_ts_field="timestamp"),
        OkxSeriesSpec("taker_ls_ratio", f"{_RUBIK}/taker-volume-contract",
                      "per_symbol", "fapi", 0, cfg.RATIO_CADENCE_S, "MED",
                      rs.TAKER_LS_SCHEMA, "s", "timestamp", _parse_taker,
                      {**_RUBIK_PARAMS, "unit": "0"}, dedup_ts_field="timestamp"),
        OkxSeriesSpec("basis", "join:basis", "all",
                      "fapi", 0, cfg.RATIO_CADENCE_S, "LOW",
                      rs.BASIS_SCHEMA, "pair", "timestamp",
                      _parse_basis(symbol_map)),
    ]


def build_klines_spec() -> OkxSeriesSpec:
    """The hourly klines maintenance spec (closed bars only, openTime dedup)."""
    return OkxSeriesSpec(
        cc_cfg.KLINES_DATASET, "/api/v5/market/candles", "per_symbol",
        "fapi", 0, cc_cfg.KLINES_MAINT_CADENCE_S, "HIGH",
        ks.KLINES_1H_SCHEMA, "s", "openTime", parse_candles,
        {"bar": cfg.OKX_KLINES_BAR, "limit": cc_cfg.KLINES_MAINT_LIMIT},
        dedup_ts_field="openTime",
    )
