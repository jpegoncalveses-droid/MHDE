"""§3 — coin-relative (engineered) primitive layer.

Raw per-(symbol, window) primitives are not comparable across coins (a threshold means
something different on BTC vs a thin alt), so raw-threshold rules degenerate into
coin-identity / volatility selection. The rule search must run over COIN-RELATIVE
expressions. For each base primitive this layer exposes, where applicable:

  * ``.z<W>``   — per-coin z-score vs that coin's OWN trailing ``W``-window history
                  ("unusually high for this coin"). LOOKAHEAD-FREE: the z for window t
                  uses only that coin's windows STRICTLY BEFORE t.
  * ``.xrank``  — cross-universe mid-rank percentile in [0,1] against all coins in the
                  SAME window ("stands out from the market right now"). Cross-sectional,
                  so lookahead-free by construction.
  * ``.raw``    — offered ONLY for inherently-comparable primitives (already-bounded
                  ratios / scale-free fractions, e.g. a 0-1 taker-buy ratio). Unbounded
                  primitives (volume, notional, counts) are EXCLUDED raw — only their z
                  and rank enter the search.

This is the "coin-agnostic engineered layer" prior brain docstrings deferred to Phase 3
prose, built here as executable code. It reads the existing raw primitive store and
computes the engineered features ON-READ (the spec allows persisted OR computed-on-read;
computed-on-read is a pure, reproducible function of the raw store + params — no second
forward-only store to keep settlement-consistent, and params can change without a
migration; the batch already reads the whole tape). NOTHING here looks forward.

``compute_engineered`` is the whole surface: pure, deterministic, no I/O.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

from crypto.research.brain.discovery import config as dcfg

RAW = "raw"
Z = "z"
XRANK = "xrank"


@dataclass(frozen=True)
class BaseFeature:
    """One base primitive + the transforms it is allowed to expose to the search."""
    feature_id: str                                   # e.g. "trades.taker_buy_ratio"
    dataset: str                                      # brain store dataset to read
    extract: Callable[[Mapping], Optional[float]]     # snapshot -> base scalar or None
    transforms: tuple                                 # subset of (RAW, Z, XRANK)


# -- None-safe arithmetic (a nullable venue field -> the feature is simply absent) ----

def _safe_ratio(num, den):
    if num is None or den is None or den == 0:
        return None
    return num / den


def _safe_sum(*xs):
    if any(x is None for x in xs):
        return None
    return float(sum(xs))


def _rel_change(close, open_):
    if close is None or open_ is None or open_ == 0:
        return None
    return close / open_ - 1.0


def _g(s, k):
    return s.get(k)


# -- the production base-feature registry (extensible: add an entry) -------------------
# RAW is granted ONLY to bounded ratios / scale-free fractions. Volumes, notionals and
# counts are unbounded across coins -> z + rank only.

BASE_FEATURES: list[BaseFeature] = [
    # trades
    BaseFeature("trades.total_vol", "trades",
                lambda s: _safe_sum(_g(s, "taker_buy_vol"), _g(s, "taker_sell_vol")),
                (Z, XRANK)),
    BaseFeature("trades.taker_buy_ratio", "trades",
                lambda s: _safe_ratio(_g(s, "taker_buy_vol"),
                                      _safe_sum(_g(s, "taker_buy_vol"), _g(s, "taker_sell_vol"))),
                (RAW, Z, XRANK)),
    BaseFeature("trades.trade_count", "trades", lambda s: _g(s, "trade_count"), (Z, XRANK)),
    BaseFeature("trades.notional", "trades",
                lambda s: _safe_sum(_g(s, "taker_buy_quote_vol"), _g(s, "taker_sell_quote_vol")),
                (Z, XRANK)),
    BaseFeature("trades.price_range", "trades",
                lambda s: _safe_ratio(_safe_sum(_g(s, "price_high"), -_g(s, "price_low"))
                                      if _g(s, "price_high") is not None and _g(s, "price_low") is not None
                                      else None, _g(s, "price_open")),
                (RAW, Z, XRANK)),
    BaseFeature("trades.ret_co", "trades",
                lambda s: _rel_change(_g(s, "price_close"), _g(s, "price_open")),
                (RAW, Z, XRANK)),
    # bookticker
    BaseFeature("bookticker.rel_spread", "bookticker",
                lambda s: _safe_ratio(_g(s, "spread_mean"),
                                      _safe_ratio(_safe_sum(_g(s, "bid_close"), _g(s, "ask_close")), 2.0)),
                (RAW, Z, XRANK)),
    BaseFeature("bookticker.book_imbalance", "bookticker",
                lambda s: _safe_ratio(_g(s, "bid_qty_mean"),
                                      _safe_sum(_g(s, "bid_qty_mean"), _g(s, "ask_qty_mean"))),
                (RAW, Z, XRANK)),
    # markprice
    BaseFeature("markprice.funding", "markprice", lambda s: _g(s, "funding_last"),
                (RAW, Z, XRANK)),
    BaseFeature("markprice.mark_ret", "markprice",
                lambda s: _rel_change(_g(s, "mark_close"), _g(s, "mark_open")),
                (RAW, Z, XRANK)),
    # depth
    BaseFeature("depth.notional_imbalance", "depth",
                lambda s: _safe_ratio(_g(s, "bid_total_notional_mean"),
                                      _safe_sum(_g(s, "bid_total_notional_mean"),
                                                _g(s, "ask_total_notional_mean"))),
                (RAW, Z, XRANK)),
    # forceorder
    BaseFeature("forceorder.liq_total", "forceorder",
                lambda s: _safe_sum(_g(s, "liq_buy_vol"), _g(s, "liq_sell_vol")), (Z, XRANK)),
    BaseFeature("forceorder.liq_buy_ratio", "forceorder",
                lambda s: _safe_ratio(_g(s, "liq_buy_vol"),
                                      _safe_sum(_g(s, "liq_buy_vol"), _g(s, "liq_sell_vol"))),
                (RAW, Z, XRANK)),
]


def engineered_feature_ids(base_features: Sequence[BaseFeature] = BASE_FEATURES,
                           *, zscore_windows: Sequence[int] = dcfg.ZSCORE_WINDOWS) -> list[str]:
    """Every engineered feature id the search may use, in a deterministic order."""
    ids: list[str] = []
    for bf in base_features:
        if RAW in bf.transforms:
            ids.append(f"{bf.feature_id}.raw")
        if Z in bf.transforms:
            ids.extend(f"{bf.feature_id}.z{w}" for w in zscore_windows)
        if XRANK in bf.transforms:
            ids.append(f"{bf.feature_id}.xrank")
    return ids


def _mid_rank_percentile(value: float, population: Sequence[float]) -> float:
    """Mid-rank percentile of ``value`` within ``population`` (which includes value), in
    [0,1]: (strictly-less + 0.5*equal) / n. Robust, bounded, scale-free."""
    n = len(population)
    less = sum(1 for x in population if x < value)
    equal = sum(1 for x in population if x == value)
    return (less + 0.5 * equal) / n


def compute_engineered(
    raw_by_dataset: Mapping[str, Sequence[Mapping]],
    *,
    zscore_windows: Sequence[int] = dcfg.ZSCORE_WINDOWS,
    zscore_min_history: int = dcfg.ZSCORE_MIN_HISTORY,
    xuniv_min_coins: int = dcfg.XUNIV_MIN_COINS,
    base_features: Sequence[BaseFeature] = BASE_FEATURES,
) -> dict[tuple[str, int], dict[str, float]]:
    """Engineered features keyed by ``(symbol, window_start_ns)``.

    ``raw_by_dataset`` maps a brain store dataset name -> its raw snapshot dicts (as
    returned by ``store.read_snapshots``). Only the datasets a feature needs are read;
    a dataset absent from the map contributes nothing. Values that cannot be computed
    (None inputs, sub-min-history z, zero-variance prior, sub-min-coins window) are
    omitted rather than faked.
    """
    out: dict[tuple[str, int], dict[str, float]] = {}

    def _put(symbol, window_ns, fid, value):
        if value is None:
            return
        out.setdefault((symbol, int(window_ns)), {})[fid] = float(value)

    for bf in base_features:
        rows = raw_by_dataset.get(bf.dataset) or []
        # (symbol, window) -> base value, skipping uncomputable
        base_vals: dict[tuple[str, int], float] = {}
        for s in rows:
            v = bf.extract(s)
            if v is not None:
                base_vals[(s["symbol"], int(s["window_start_ns"]))] = float(v)

        # RAW passthrough (bounded features only)
        if RAW in bf.transforms:
            for (sym, w), v in base_vals.items():
                _put(sym, w, f"{bf.feature_id}.raw", v)

        # per-coin z over each coin's strictly-prior trailing window
        if Z in bf.transforms:
            by_symbol: dict[str, list[tuple[int, float]]] = {}
            for (sym, w), v in base_vals.items():
                by_symbol.setdefault(sym, []).append((w, v))
            for sym, series in by_symbol.items():
                series.sort(key=lambda wv: wv[0])
                values = [v for _, v in series]
                for i, (w, v) in enumerate(series):
                    prior_all = values[:i]                      # STRICTLY before window i
                    for win in zscore_windows:
                        prior = prior_all[-win:]
                        if len(prior) < zscore_min_history:
                            continue
                        sd = statistics.pstdev(prior)
                        if sd == 0:
                            continue
                        z = (v - statistics.fmean(prior)) / sd
                        _put(sym, w, f"{bf.feature_id}.z{win}", z)

        # cross-universe mid-rank percentile, per window
        if XRANK in bf.transforms:
            by_window: dict[int, list[tuple[str, float]]] = {}
            for (sym, w), v in base_vals.items():
                by_window.setdefault(w, []).append((sym, v))
            for w, members in by_window.items():
                if len(members) < xuniv_min_coins:
                    continue
                pop = [v for _, v in members]
                for sym, v in members:
                    _put(sym, w, f"{bf.feature_id}.xrank", _mid_rank_percentile(v, pop))

    return out
