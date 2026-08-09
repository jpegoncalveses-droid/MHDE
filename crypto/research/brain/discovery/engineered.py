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
from collections.abc import Mapping as AbcMapping
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

import numpy as np
import pandas as pd
import pyarrow as pa

from crypto.research.brain.discovery import config as dcfg

RAW = "raw"
Z = "z"
XRANK = "xrank"


class _RowView(AbcMapping):
    """A read-only Mapping view of one instance's present features, over the tape's columnar
    store. Absent (NaN) features are simply not present — no key, exactly like the old sparse
    per-instance dict. Cheap: holds a row index, materialises nothing until asked."""
    __slots__ = ("_tape", "_row")

    def __init__(self, tape: "EngineeredTape", row: int):
        self._tape = tape
        self._row = row

    def __getitem__(self, fid):
        col = self._tape._feat_to_col.get(fid)
        if col is None:
            raise KeyError(fid)
        v = self._tape._values[self._row, col]
        if np.isnan(v):
            raise KeyError(fid)
        return float(v)

    def __iter__(self):
        row = self._tape._values[self._row]
        for fid, col in self._tape._feat_to_col.items():
            if not np.isnan(row[col]):
                yield fid

    def __len__(self):
        return int(np.count_nonzero(~np.isnan(self._tape._values[self._row])))


class EngineeredTape(AbcMapping):
    """Columnar engineered layer: a dense ``float64`` matrix (n_keys x n_features), NaN where a
    feature is absent, plus the key<->row and feature<->column maps. It IS a
    ``Mapping[(symbol, window_ns) -> Mapping[str, float]]`` (``eng[key][fid]``, ``.get``, ``in``,
    ``==``, iteration all work), so every consumer — the rule search, ``R.fires``, forward
    confirmation, continuations — reads it unchanged, while storage is ~8 bytes/value instead of
    the dict-of-dicts' per-entry Python overhead (the discovery memory-floor fix). The scorer
    takes the ``labeled_columns`` fast path for vectorised firing."""
    __slots__ = ("_keys", "_key_to_row", "_feature_ids", "_feat_to_col", "_values")

    def __init__(self, keys, key_to_row, feature_ids, feat_to_col, values):
        self._keys = keys
        self._key_to_row = key_to_row
        self._feature_ids = feature_ids
        self._feat_to_col = feat_to_col
        self._values = values

    def __getitem__(self, key):
        row = self._key_to_row.get(key)
        if row is None:
            raise KeyError(key)
        return _RowView(self, row)

    def __iter__(self):
        return iter(self._keys)

    def __len__(self):
        return len(self._keys)

    def __contains__(self, key):
        # fast membership straight off the row index (avoids minting a _RowView per probe)
        return key in self._key_to_row

    def feature_present_values(self, feature_ids: Sequence[str]) -> dict:
        """``feature_id -> array of its present (non-NaN) values`` over the whole tape — the
        columnar fast path for ``rules.build_atoms``' threshold discretisation."""
        out: dict = {}
        for fid in feature_ids:
            col_i = self._feat_to_col.get(fid)
            if col_i is None:
                out[fid] = []
                continue
            col = self._values[:, col_i]
            out[fid] = col[~np.isnan(col)]
        return out

    def fires_keys(self, rule) -> set:
        """Keys where ``rule`` holds — columnar AND of the conditions' masks (NaN compares False,
        so an absent feature never holds). The fast path for ``rules.fires``."""
        conds = rule.conditions
        if not conds:
            return set(self._keys)          # empty conjunction holds everywhere (all([]) is True)
        mask = None
        for c in conds:
            col_i = self._feat_to_col.get(c.feature)
            if col_i is None:
                return set()                # feature absent from the tape -> never holds
            col = self._values[:, col_i]
            m = col > c.threshold if c.op == ">" else col < c.threshold
            mask = m if mask is None else (mask & m)
        return {self._keys[i] for i in np.flatnonzero(mask).tolist()}

    def labeled_columns(self, feature_ids: Sequence[str], keys: Sequence[tuple]) -> dict:
        """``feature_id -> float64 column over ``keys`` (NaN where the key or feature is absent).

        The scorer's vectorised firing path — a pure columnar gather, no per-key Python dicts."""
        n = len(keys)
        rows = np.fromiter((self._key_to_row.get(k, -1) for k in keys), dtype=np.int64, count=n)
        present = rows >= 0
        safe = np.where(present, rows, 0)
        out: dict = {}
        for fid in feature_ids:
            col_i = self._feat_to_col.get(fid)
            if col_i is None:
                out[fid] = np.full(n, np.nan, dtype=np.float64)
                continue
            vals = self._values[safe, col_i]
            out[fid] = np.where(present, vals, np.nan)
        return out


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


# -- columnar (vectorized) extract path (§ option B: primitive-read floor) --------------
# The scalar ``bf.extract`` reads one snapshot dict at a time; these compute the SAME base
# scalar over a whole arrow column at once. None (a null cell) maps to NaN; every helper
# propagates NaN so an absent input yields an absent (NaN) output, exactly like the scalar
# None-guards. Zero denominators -> NaN (== the scalar ``den == 0 -> None``).


def _col(table: "pa.Table", field: str) -> np.ndarray:
    """One field as a float64 numpy column (nulls -> NaN); all-NaN if the field is absent."""
    if field not in table.column_names:
        return np.full(table.num_rows, np.nan, dtype=np.float64)
    return table.column(field).cast(pa.float64()).to_numpy(zero_copy_only=False)


def _rsum(*arrs: np.ndarray) -> np.ndarray:
    """``_safe_sum`` vectorized: NaN in any operand -> NaN (numpy add propagates it)."""
    out = np.asarray(arrs[0], dtype=np.float64).copy()
    for a in arrs[1:]:
        out = out + np.asarray(a, dtype=np.float64)
    return out


def _rratio(num: np.ndarray, den) -> np.ndarray:
    """``_safe_ratio`` vectorized: NaN if either side NaN or ``den == 0``, else ``num/den``."""
    num, den = np.broadcast_arrays(np.asarray(num, np.float64), np.asarray(den, np.float64))
    out = np.full(num.shape, np.nan, dtype=np.float64)
    ok = ~np.isnan(num) & ~np.isnan(den) & (den != 0.0)
    out[ok] = num[ok] / den[ok]
    return out


def _rrel(close: np.ndarray, open_: np.ndarray) -> np.ndarray:
    """``_rel_change`` vectorized: NaN if either side NaN or ``open == 0``, else ``close/open - 1``."""
    close, open_ = np.broadcast_arrays(np.asarray(close, np.float64), np.asarray(open_, np.float64))
    out = np.full(close.shape, np.nan, dtype=np.float64)
    ok = ~np.isnan(close) & ~np.isnan(open_) & (open_ != 0.0)
    out[ok] = close[ok] / open_[ok] - 1.0
    return out


#: feature_id -> vectorized base-value extractor over a columns dict; mirrors each scalar
#: ``BaseFeature.extract`` in BASE_FEATURES one-for-one (pinned by test_...engineered_columnar).
_VEC_EXTRACT = {
    "trades.total_vol": lambda c: _rsum(c["taker_buy_vol"], c["taker_sell_vol"]),
    "trades.taker_buy_ratio": lambda c: _rratio(c["taker_buy_vol"],
                                                _rsum(c["taker_buy_vol"], c["taker_sell_vol"])),
    "trades.trade_count": lambda c: c["trade_count"],
    "trades.notional": lambda c: _rsum(c["taker_buy_quote_vol"], c["taker_sell_quote_vol"]),
    "trades.price_range": lambda c: _rratio(c["price_high"] - c["price_low"], c["price_open"]),
    "trades.ret_co": lambda c: _rrel(c["price_close"], c["price_open"]),
    "bookticker.rel_spread": lambda c: _rratio(c["spread_mean"],
                                               _rratio(_rsum(c["bid_close"], c["ask_close"]), 2.0)),
    "bookticker.book_imbalance": lambda c: _rratio(c["bid_qty_mean"],
                                                   _rsum(c["bid_qty_mean"], c["ask_qty_mean"])),
    "markprice.funding": lambda c: c["funding_last"],
    "markprice.mark_ret": lambda c: _rrel(c["mark_close"], c["mark_open"]),
    "depth.notional_imbalance": lambda c: _rratio(c["bid_total_notional_mean"],
                                                  _rsum(c["bid_total_notional_mean"],
                                                        c["ask_total_notional_mean"])),
    "forceorder.liq_total": lambda c: _rsum(c["liq_buy_vol"], c["liq_sell_vol"]),
    "forceorder.liq_buy_ratio": lambda c: _rratio(c["liq_buy_vol"],
                                                  _rsum(c["liq_buy_vol"], c["liq_sell_vol"])),
}


def _columnar_base_values(bf: "BaseFeature", table: "pa.Table") -> np.ndarray:
    """The base scalar per row (NaN where absent), vectorized — equivalent to
    ``[bf.extract(s) for s in table.to_pylist()]`` but with no per-row Python dicts. Only the
    (numeric) input fields the feature needs are decoded (never the string key columns)."""
    cols = {f: _col(table, f) for f in _FEATURE_FIELDS[bf.feature_id]}
    return _VEC_EXTRACT[bf.feature_id](cols)


def _columnar_zscore(symbols: np.ndarray, windows: np.ndarray, vals: np.ndarray, *,
                     window: int, min_history: int) -> np.ndarray:
    """Per-coin trailing z-score, vectorized, matching the scalar ``statistics.pstdev``/``fmean``
    z to float-summation tolerance and lookahead-free by construction.

    For each coin, over its PRESENT base values (NaN = absent, excluded from the series, exactly
    like the scalar dict) sorted by window, the z at window i uses only that coin's STRICTLY-PRIOR
    up-to-``window`` values (``shift(1).rolling(window, min_periods=min_history)``); a sub-min-history
    or zero-std prior yields no z (NaN). Returns z per input row (NaN where absent)."""
    n = len(vals)
    out = np.full(n, np.nan, dtype=np.float64)
    finite = ~np.isnan(vals)
    idx = np.flatnonzero(finite)
    if idx.size == 0:
        return out
    codes = pd.factorize(symbols[idx])[0]                 # coin -> int code (fast grouping)
    order = np.lexsort((windows[idx], codes))             # sort by (coin, window)
    sidx = idx[order]
    scodes = codes[order]
    svals = vals[sidx]
    bnd = np.flatnonzero(np.concatenate(([True], scodes[1:] != scodes[:-1], [True])))
    for b in range(len(bnd) - 1):
        sl = slice(bnd[b], bnd[b + 1])
        v = svals[sl]
        roll = pd.Series(v).shift(1).rolling(window, min_periods=min_history)  # STRICTLY prior
        mean = roll.mean().to_numpy()
        std = roll.std(ddof=0).to_numpy()                 # ddof=0 == population == pstdev
        with np.errstate(divide="ignore", invalid="ignore"):
            z = (v - mean) / std
        z[~(std > 0.0) | np.isnan(mean)] = np.nan         # sub-min-history or zero-std -> absent
        out[sidx[sl]] = z
    return out


def _columnar_xrank(symbols: np.ndarray, windows: np.ndarray, vals: np.ndarray, *,
                    min_coins: int) -> np.ndarray:
    """Cross-universe mid-rank percentile per window, vectorized, matching the scalar
    ``_mid_rank_percentile`` exactly. Over each window's PRESENT base values (all coins), the
    mid-rank percentile ``(strictly_less + 0.5*equal)/n`` equals ``(avg_rank - 0.5)/n`` with the
    1-based average tie rank; windows with fewer than ``min_coins`` members yield no rank (NaN).
    Cross-sectional within a single window -> lookahead-free by construction. Returns rank per
    input row (NaN where absent)."""
    n = len(vals)
    out = np.full(n, np.nan, dtype=np.float64)
    finite = ~np.isnan(vals)
    idx = np.flatnonzero(finite)
    if idx.size == 0:
        return out
    df = pd.DataFrame({"w": windows[idx], "v": vals[idx]})
    grp = df.groupby("w", sort=False)["v"]
    avg_rank = grp.rank(method="average").to_numpy()     # 1-based average tie rank within window
    size = grp.transform("size").to_numpy()
    pct = (avg_rank - 0.5) / size
    pct[size < min_coins] = np.nan                        # window below min coins -> no rank
    out[idx] = pct
    return out


# RETAINED, NOT DEAD: production reads via compute_engineered_columnar (option B, the
# ~12-13G list-of-dicts floor fix). This scalar per-row implementation is deliberately kept
# as the load-bearing equivalence ORACLE — the columnar path is proven byte-identical
# against it, in-memory and through the parquet seam (test_brain_discovery_engineered_columnar).
def compute_engineered(
    raw_by_dataset: Mapping[str, Sequence[Mapping]],
    *,
    zscore_windows: Sequence[int] = dcfg.ZSCORE_WINDOWS,
    zscore_min_history: int = dcfg.ZSCORE_MIN_HISTORY,
    xuniv_min_coins: int = dcfg.XUNIV_MIN_COINS,
    base_features: Sequence[BaseFeature] = BASE_FEATURES,
) -> EngineeredTape:
    """Engineered features keyed by ``(symbol, window_start_ns)``, as an :class:`EngineeredTape`
    (a columnar float64 matrix behind the Mapping interface).

    ``raw_by_dataset`` maps a brain store dataset name -> its raw snapshot dicts (as
    returned by ``store.read_snapshots``). Only the datasets a feature needs are read;
    a dataset absent from the map contributes nothing. Values that cannot be computed
    (None inputs, sub-min-history z, zero-variance prior, sub-min-coins window) are
    absent (NaN in the matrix / not a key in the row view), never faked; an instance with
    no computable feature is dropped entirely (exact parity with the old sparse dict).
    """
    feature_ids = engineered_feature_ids(base_features, zscore_windows=zscore_windows)
    feat_to_col = {fid: i for i, fid in enumerate(feature_ids)}

    # row axis: every (symbol, window) seen in any needed dataset, in first-seen order.
    key_to_row: dict[tuple[str, int], int] = {}
    for bf in base_features:
        for s in raw_by_dataset.get(bf.dataset) or []:
            k = (s["symbol"], int(s["window_start_ns"]))
            if k not in key_to_row:
                key_to_row[k] = len(key_to_row)

    matrix = np.full((len(key_to_row), len(feature_ids)), np.nan, dtype=np.float64)

    def _scatter(symbol, window_ns, fid, value):
        if value is None:
            return
        matrix[key_to_row[(symbol, int(window_ns))], feat_to_col[fid]] = float(value)

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
                _scatter(sym, w, f"{bf.feature_id}.raw", v)

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
                        _scatter(sym, w, f"{bf.feature_id}.z{win}", z)

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
                    _scatter(sym, w, f"{bf.feature_id}.xrank", _mid_rank_percentile(v, pop))

    # drop instances with no computable feature -> exact key-set parity with the old sparse dict
    keys_list = list(key_to_row)
    if keys_list:
        keep = ~np.all(np.isnan(matrix), axis=1)
        if not keep.all():
            keep_idx = np.flatnonzero(keep)
            matrix = matrix[keep_idx]
            keys_list = [keys_list[i] for i in keep_idx]
            key_to_row = {k: i for i, k in enumerate(keys_list)}
    return EngineeredTape(keys_list, key_to_row, feature_ids, feat_to_col, matrix)


#: feature_id -> the raw snapshot fields its vectorized extract reads. The columnar reader
#: projects to these (plus the key columns), so e.g. the depth dataset's ~300-column ladder is
#: NEVER materialized — only the 2 notional-mean columns it needs. Must mirror _VEC_EXTRACT
#: (the oracle test catches any missing field: a feature would go silently NaN).
_FEATURE_FIELDS = {
    "trades.total_vol": ["taker_buy_vol", "taker_sell_vol"],
    "trades.taker_buy_ratio": ["taker_buy_vol", "taker_sell_vol"],
    "trades.trade_count": ["trade_count"],
    "trades.notional": ["taker_buy_quote_vol", "taker_sell_quote_vol"],
    "trades.price_range": ["price_high", "price_low", "price_open"],
    "trades.ret_co": ["price_close", "price_open"],
    "bookticker.rel_spread": ["spread_mean", "bid_close", "ask_close"],
    "bookticker.book_imbalance": ["bid_qty_mean", "ask_qty_mean"],
    "markprice.funding": ["funding_last"],
    "markprice.mark_ret": ["mark_close", "mark_open"],
    "depth.notional_imbalance": ["bid_total_notional_mean", "ask_total_notional_mean"],
    "forceorder.liq_total": ["liq_buy_vol", "liq_sell_vol"],
    "forceorder.liq_buy_ratio": ["liq_buy_vol", "liq_sell_vol"],
}


def columnar_needed_columns(base_features: Sequence[BaseFeature] = BASE_FEATURES) -> dict:
    """``dataset -> sorted list of raw fields`` the columnar path must read (feature inputs +
    the key columns). The projection that keeps the primitive read small."""
    out: dict = {}
    for bf in base_features:
        cols = out.setdefault(bf.dataset, {"symbol", "window_start_ns"})
        cols.update(_FEATURE_FIELDS[bf.feature_id])
    return {ds: sorted(cols) for ds, cols in out.items()}


def compute_engineered_columnar(
    read_dataset,
    *,
    zscore_windows: Sequence[int] = dcfg.ZSCORE_WINDOWS,
    zscore_min_history: int = dcfg.ZSCORE_MIN_HISTORY,
    xuniv_min_coins: int = dcfg.XUNIV_MIN_COINS,
    base_features: Sequence[BaseFeature] = BASE_FEATURES,
) -> EngineeredTape:
    """Columnar (option-B) build of the engineered tape — identical output to
    :func:`compute_engineered` (proven per-feature/-symbol/-window by the oracle test), but each
    dataset is read ONCE as a projected columnar arrow ``Table`` via
    ``read_dataset(dataset, columns) -> pa.Table`` and the extracts + per-coin z + cross-universe
    rank are vectorized, so NO list-of-dicts is ever materialized (the ~12-13G primitive-read
    floor fix). Peak ~ one dataset's projected columns + the output matrix, roughly
    window-independent beyond the irreducible tape itself.
    """
    feature_ids = engineered_feature_ids(base_features, zscore_windows=zscore_windows)
    feat_to_col = {fid: i for i, fid in enumerate(feature_ids)}
    needed = columnar_needed_columns(base_features)
    by_dataset: dict[str, list] = {}
    for bf in base_features:
        by_dataset.setdefault(bf.dataset, []).append(bf)

    # PASS 1 — key universe (cheap: symbol + window per dataset), first-seen order.
    key_to_row: dict[tuple[str, int], int] = {}
    for ds in by_dataset:
        tbl = read_dataset(ds, ["symbol", "window_start_ns"])
        if tbl is None or tbl.num_rows == 0:
            continue
        syms = tbl.column("symbol").to_pylist()
        wins = tbl.column("window_start_ns").to_numpy(zero_copy_only=False)
        for s, w in zip(syms, wins.tolist()):
            k = (s, int(w))
            if k not in key_to_row:
                key_to_row[k] = len(key_to_row)

    matrix = np.full((len(key_to_row), len(feature_ids)), np.nan, dtype=np.float64)

    # PASS 2 — read each dataset's needed columns, vectorize the extracts + z + rank, scatter.
    for ds, bfs in by_dataset.items():
        tbl = read_dataset(ds, needed[ds])
        if tbl is None or tbl.num_rows == 0:
            continue
        syms = np.asarray(tbl.column("symbol").to_pylist(), dtype=object)
        wins = tbl.column("window_start_ns").to_numpy(zero_copy_only=False).astype(np.int64)
        rows = np.fromiter((key_to_row.get((s, int(w)), -1)
                            for s, w in zip(syms.tolist(), wins.tolist())),
                           dtype=np.int64, count=len(syms))
        # A row whose (symbol, window) is absent from PASS 1's key universe was written by the
        # live tick BETWEEN the two reads (a concurrent-write TOCTOU — the 2026-08-08 gate
        # KeyError). Drop it here, forward-only (it lands in the next discovery run), BEFORE the
        # vectorized z / cross-universe rank — so a concurrent write can never perturb a
        # processed row's per-coin z or its rank population. On a static store nothing is
        # dropped and this is a no-op.
        if not (rows >= 0).all():
            keep = rows >= 0
            tbl = tbl.filter(pa.array(keep))
            syms, wins, rows = syms[keep], wins[keep], rows[keep]
        for bf in bfs:
            base = _columnar_base_values(bf, tbl)
            if RAW in bf.transforms:
                matrix[rows, feat_to_col[f"{bf.feature_id}.raw"]] = base
            if Z in bf.transforms:
                for win in zscore_windows:
                    z = _columnar_zscore(syms, wins, base, window=win, min_history=zscore_min_history)
                    matrix[rows, feat_to_col[f"{bf.feature_id}.z{win}"]] = z
            if XRANK in bf.transforms:
                xr = _columnar_xrank(syms, wins, base, min_coins=xuniv_min_coins)
                matrix[rows, feat_to_col[f"{bf.feature_id}.xrank"]] = xr
        del tbl

    keys_list = list(key_to_row)
    if keys_list:
        keep = ~np.all(np.isnan(matrix), axis=1)
        if not keep.all():
            keep_idx = np.flatnonzero(keep)
            matrix = matrix[keep_idx]
            keys_list = [keys_list[i] for i in keep_idx]
            key_to_row = {k: i for i, k in enumerate(keys_list)}
    return EngineeredTape(keys_list, key_to_row, feature_ids, feat_to_col, matrix)
