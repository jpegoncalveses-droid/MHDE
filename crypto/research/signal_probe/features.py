"""Causal raw-feature computation for the signal probe.

Every function here is **pure** (no I/O) and **causal**: it reads only bars
that closed at or before the cycle timestamp. All inputs are already-closed
bars (the collector excludes the in-progress minute). Outputs are **raw**
values — no thresholds, no flags. A feature that cannot be computed with the
available lookback returns ``None`` (stored as ``NULL``).

Bar dicts use the keys produced by ``ProbeBinanceClient.fetch_klines``:
``open_time, open, high, low, close, volume, quote_volume, trades,
taker_buy_base``. Lists are **ascending** by ``open_time`` (last = most
recent closed bar).
"""
from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Any, Mapping, Optional, Sequence

Num = Optional[float]
Bar = Mapping[str, Any]


def _div(a: float, b: float) -> Num:
    """Safe division: ``None`` when the denominator is zero."""
    return a / b if b else None


def _closes(bars: Sequence[Bar]) -> list[float]:
    return [float(b["close"]) for b in bars]


def _mean(xs: Sequence[float]) -> Num:
    return sum(xs) / len(xs) if xs else None


# ── single-window momentum ────────────────────────────────────────────────

def roc(closes: Sequence[float], n: int) -> Num:
    """Rate of change over ``n`` bars: ``close[-1] / close[-1-n] - 1``."""
    if len(closes) <= n:
        return None
    base = closes[-1 - n]
    return _div(closes[-1] - base, base)


def acceleration(closes: Sequence[float], n: int) -> Num:
    """ΔROC between two consecutive ``n``-bar windows.

    ``roc(now, n) - roc(n-bars-ago, n)`` — needs ``2n + 1`` closes.
    """
    if len(closes) <= 2 * n:
        return None
    now = roc(closes, n)
    prev = roc(closes[: len(closes) - n], n)
    if now is None or prev is None:
        return None
    return now - prev


def move_shape(closes: Sequence[float], window: int) -> Num:
    """Largest single 1m bar move / net move over ``window`` bars.

    ``max(|Δclose_i|) / |close[-1] - close[-1-window]|``. ``None`` if the
    net move is zero or there are too few bars.
    """
    if len(closes) <= window:
        return None
    seg = closes[-window - 1:]
    steps = [abs(seg[i] - seg[i - 1]) for i in range(1, len(seg))]
    if not steps:
        return None
    net = abs(seg[-1] - seg[0])
    return _div(max(steps), net)


# ── VWAP / MA distances ───────────────────────────────────────────────────

def dist_rolling_vwap(bars: Sequence[Bar], window: int = 60) -> Num:
    """Signed % of last close from the rolling typical-price VWAP."""
    if len(bars) < window:
        return None
    seg = bars[-window:]
    pv = 0.0
    vol = 0.0
    for b in seg:
        typ = (float(b["high"]) + float(b["low"]) + float(b["close"])) / 3.0
        v = float(b["volume"])
        pv += typ * v
        vol += v
    vwap = _div(pv, vol)
    if vwap is None:
        return None
    return _div(float(seg[-1]["close"]) - vwap, vwap)


def dist_session_vwap(
    bars_1h: Sequence[Bar], price: float, ts: datetime
) -> Num:
    """Signed % of ``price`` from the session (UTC-day) VWAP.

    Session VWAP is built from the 1h bars whose ``open_time`` falls on
    ``ts``'s UTC date (the finest session anchor available without a full
    day of 1m bars). ``None`` if the day has no bars or zero volume.
    """
    day = ts.date()
    seg = [b for b in bars_1h if b["open_time"].date() == day]
    if not seg:
        return None
    pv = 0.0
    vol = 0.0
    for b in seg:
        typ = (float(b["high"]) + float(b["low"]) + float(b["close"])) / 3.0
        v = float(b["volume"])
        pv += typ * v
        vol += v
    vwap = _div(pv, vol)
    if vwap is None:
        return None
    return _div(price - vwap, vwap)


def dist_sma(closes: Sequence[float], n: int) -> Num:
    """Signed % of last close from the ``n``-bar simple moving average."""
    if len(closes) < n:
        return None
    sma = _mean(closes[-n:])
    if sma is None:
        return None
    return _div(closes[-1] - sma, sma)


# ── breakout ──────────────────────────────────────────────────────────────

def breakout(bars: Sequence[Bar], window: int) -> Num:
    """Signed % of last close vs the prior ``window``-bar high (current bar
    excluded). ``> 0`` is a new high."""
    if len(bars) < window + 1:
        return None
    prior = bars[-window - 1:-1]
    prior_high = max(float(b["high"]) for b in prior)
    close = float(bars[-1]["close"])
    return _div(close - prior_high, prior_high)


# ── volume / relative volume ──────────────────────────────────────────────

def up_down_vol_ratio(bars: Sequence[Bar], window: int = 60) -> Num:
    """Up-bar volume / down-bar volume over ``window`` bars (close vs open)."""
    if len(bars) < window:
        return None
    seg = bars[-window:]
    up = sum(float(b["volume"]) for b in seg if float(b["close"]) > float(b["open"]))
    down = sum(float(b["volume"]) for b in seg if float(b["close"]) < float(b["open"]))
    return _div(up, down)


def rvol_1m(vols: Sequence[float], lookback: int) -> Num:
    """Last 1m volume / mean of the prior ``lookback`` 1m volumes."""
    if len(vols) < lookback + 1:
        return None
    base = _mean(vols[-lookback - 1:-1])
    if base is None:
        return None
    return _div(vols[-1], base)


def rvol_5m(vols: Sequence[float], lookback: int) -> Num:
    """Last-5m volume / (5 × mean of the ``lookback`` 1m volumes preceding the
    current 5-bar window). The baseline excludes the 5 bars being measured."""
    if len(vols) < lookback + 5:
        return None
    base = _mean(vols[-lookback - 5:-5])
    if base is None:
        return None
    return _div(sum(vols[-5:]), 5 * base)


# ── taker imbalance ───────────────────────────────────────────────────────

def taker_imbalance(bars: Sequence[Bar], window: int) -> Num:
    """Taker-buy base / total base volume over the last ``window`` bars."""
    if len(bars) < window:
        return None
    seg = bars[-window:]
    buy = sum(float(b["taker_buy_base"]) for b in seg)
    vol = sum(float(b["volume"]) for b in seg)
    return _div(buy, vol)


# ── trade count / size ────────────────────────────────────────────────────

def trade_count_ratio(bars: Sequence[Bar], lookback: int = 60) -> Num:
    """Last bar trade count / mean of the prior ``lookback`` counts."""
    if len(bars) < lookback + 1:
        return None
    counts = [float(b["trades"]) for b in bars]
    base = _mean(counts[-lookback - 1:-1])
    if base is None:
        return None
    return _div(counts[-1], base)


def avg_trade_size(bar: Bar) -> Num:
    """Average base size per trade for one bar (``volume / trades``)."""
    return _div(float(bar["volume"]), float(bar["trades"]))


def avg_trade_size_ratio(bars: Sequence[Bar], lookback: int = 60) -> Num:
    """Current avg trade size / mean avg trade size over the prior ``lookback``
    bars (bars with zero trades are skipped in the baseline)."""
    if len(bars) < lookback + 1:
        return None
    cur = avg_trade_size(bars[-1])
    if cur is None:
        return None
    prior_sizes = [
        s for s in (avg_trade_size(b) for b in bars[-lookback - 1:-1]) if s is not None
    ]
    base = _mean(prior_sizes)
    if base is None:
        return None
    return _div(cur, base)


# ── open interest ─────────────────────────────────────────────────────────

def oi_change(oi_series: Sequence[float], steps: int) -> Num:
    """Signed ΔOI % over ``steps`` points of the OI history series."""
    if len(oi_series) <= steps:
        return None
    base = oi_series[-1 - steps]
    return _div(oi_series[-1] - base, base)


# ── order-book depth ──────────────────────────────────────────────────────

def depth_imbalance_and_spread(depth: Mapping[str, Any]) -> tuple[Num, Num]:
    """(bid/ask depth within 0.5% of mid, spread in bps) from an order book.

    ``depth`` has ``bids`` / ``asks`` as ``[price, qty]`` pairs (strings or
    floats), best first. Returns ``(None, None)`` if the book is empty.
    """
    bids = depth.get("bids") or []
    asks = depth.get("asks") or []
    if not bids or not asks:
        return None, None
    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return None, None
    lo = mid * 0.995
    hi = mid * 1.005
    bid_depth = sum(float(q) for p, q in ((float(b[0]), b[1]) for b in bids) if p >= lo)
    ask_depth = sum(float(q) for p, q in ((float(a[0]), a[1]) for a in asks) if p <= hi)
    imbalance = _div(bid_depth, ask_depth)
    spread_bps = _div(best_ask - best_bid, mid)
    if spread_bps is not None:
        spread_bps *= 10_000.0
    return imbalance, spread_bps


# ── per-symbol assembly ───────────────────────────────────────────────────

def compute_base_features(
    bars_1m: Sequence[Bar],
    bars_1h: Sequence[Bar],
    *,
    ts: datetime,
    funding: Optional[Mapping[str, Any]],
    oi_series: Sequence[float],
    oi_live: Num,
    depth: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compute every non-cross-sectional raw feature for one symbol.

    Cross-sectional features (return vs BTC / vs universe) are added later by
    :func:`apply_cross_sectional` once all symbols' ROCs are known.
    """
    closes = _closes(bars_1m)
    vols = [float(b["volume"]) for b in bars_1m]
    last = bars_1m[-1] if bars_1m else None
    price = float(last["close"]) if last else None

    out: dict[str, Any] = {}

    # raw latest-minute OHLCV
    if last is not None:
        out.update(
            open=float(last["open"]), high=float(last["high"]),
            low=float(last["low"]), close=float(last["close"]),
            volume=float(last["volume"]),
            quote_volume=float(last.get("quote_volume")) if last.get("quote_volume") is not None else None,
            trades=int(last["trades"]),
            taker_buy_base=float(last["taker_buy_base"]),
        )

    # funding / OI raw
    if funding is not None:
        out.update(
            last_funding_rate=_as_float(funding.get("lastFundingRate")),
            mark_price=_as_float(funding.get("markPrice")),
            index_price=_as_float(funding.get("indexPrice")),
            interest_rate=_as_float(funding.get("interestRate")),
        )
    # Binance exposes no public "predicted/next" funding-rate field; left NULL.
    out["predicted_funding_rate"] = None
    out["open_interest"] = oi_live

    # ROC
    out["roc_1m"] = roc(closes, 1)
    out["roc_5m"] = roc(closes, 5)
    out["roc_15m"] = roc(closes, 15)
    out["roc_60m"] = roc(closes, 60)

    # acceleration
    out["accel_1m"] = acceleration(closes, 1)
    out["accel_5m"] = acceleration(closes, 5)
    out["accel_15m"] = acceleration(closes, 15)

    # move shape
    out["move_shape_15m"] = move_shape(closes, 15)
    out["move_shape_60m"] = move_shape(closes, 60)

    # VWAP
    out["dist_vwap_rolling"] = dist_rolling_vwap(bars_1m, 60)
    out["dist_vwap_session"] = (
        dist_session_vwap(bars_1h, price, ts) if price is not None else None
    )

    # SMA
    out["dist_sma_20"] = dist_sma(closes, 20)
    out["dist_sma_50"] = dist_sma(closes, 50)
    out["dist_sma_100"] = dist_sma(closes, 100)

    # breakout
    out["breakout_15m"] = breakout(bars_1m, 15)
    out["breakout_60m"] = breakout(bars_1m, 60)
    out["breakout_1d"] = breakout(bars_1h, 24)

    # volume
    out["up_down_vol_ratio_60m"] = up_down_vol_ratio(bars_1m, 60)

    # relative volume
    out["rvol_1m_20"] = rvol_1m(vols, 20)
    out["rvol_1m_60"] = rvol_1m(vols, 60)
    out["rvol_5m_20"] = rvol_5m(vols, 20)
    out["rvol_5m_60"] = rvol_5m(vols, 60)

    # taker imbalance
    out["taker_imbalance_1m"] = taker_imbalance(bars_1m, 1)
    out["taker_imbalance_5m"] = taker_imbalance(bars_1m, 5)
    out["taker_imbalance_15m"] = taker_imbalance(bars_1m, 15)

    # trade count / size
    out["trade_count_ratio_60"] = trade_count_ratio(bars_1m, 60)
    out["avg_trade_size"] = avg_trade_size(last) if last is not None else None
    out["avg_trade_size_ratio_60"] = avg_trade_size_ratio(bars_1m, 60)

    # OI change — 5m/15m/1h from the 5m-period history series.
    # oi_change_1m has no public sub-5m source; left NULL (derivable downstream
    # from consecutive open_interest rows).
    out["oi_change_1m"] = None
    out["oi_change_5m"] = oi_change(oi_series, 1)
    out["oi_change_15m"] = oi_change(oi_series, 3)
    out["oi_change_1h"] = oi_change(oi_series, 12)

    # trend alignment
    out["dist_sma50_1h"] = (
        _dist_to(price, _mean([float(b["close"]) for b in bars_1h[-50:]]))
        if price is not None and len(bars_1h) >= 50 else None
    )
    # "30d high" = max high over the fetched 1h lookback (LOOKBACK_1H = 730
    # bars ≈ 30.4d); reads fewer days early in a symbol's listing history.
    out["dist_30d_high"] = (
        _dist_to(price, max((float(b["high"]) for b in bars_1h), default=None))
        if price is not None and bars_1h else None
    )

    # depth (optional)
    if depth is not None:
        imb, spread = depth_imbalance_and_spread(depth)
        out["depth_imbalance"] = imb
        out["spread_bps"] = spread
    else:
        out["depth_imbalance"] = None
        out["spread_bps"] = None

    return out


def _dist_to(price: float, ref: Num) -> Num:
    if ref is None:
        return None
    return _div(price - ref, ref)


def _as_float(v: Any) -> Num:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── cross-sectional pass ──────────────────────────────────────────────────

_X_WINDOWS = {"5m": "roc_5m", "15m": "roc_15m", "60m": "roc_60m"}


def apply_cross_sectional(
    base_by_symbol: Mapping[str, dict[str, Any]],
    btc_symbol: str,
) -> None:
    """Add return-vs-BTC and return-vs-universe features in place.

    For each window the universe is the set of symbols with a non-``None``
    ROC. ``ret_pct_*`` is the fraction of peers strictly below this symbol
    (``None`` when fewer than two peers have a value).
    """
    for win, key in _X_WINDOWS.items():
        rocs = {s: f[key] for s, f in base_by_symbol.items() if f.get(key) is not None}
        btc = rocs.get(btc_symbol)
        med = median(rocs.values()) if rocs else None
        n = len(rocs)
        for sym, feats in base_by_symbol.items():
            r = feats.get(key)
            feats[f"ret_vs_btc_{win}"] = (
                r - btc if (r is not None and btc is not None) else None
            )
            feats[f"ret_spread_median_{win}"] = (
                r - med if (r is not None and med is not None) else None
            )
            if r is None or n < 2:
                feats[f"ret_pct_{win}"] = None
            else:
                below = sum(1 for v in rocs.values() if v < r)
                feats[f"ret_pct_{win}"] = below / (n - 1)
