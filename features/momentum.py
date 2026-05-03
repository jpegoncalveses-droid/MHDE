from __future__ import annotations

import logging
from datetime import date

import duckdb

logger = logging.getLogger("mhde.features.momentum")

_MIN_DAYS = 20


def compute_momentum(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    ticker: str,
    as_of: date,
) -> list[dict]:
    features = []

    rows = conn.execute(
        """
        SELECT trade_date, close, volume FROM prices_daily
        WHERE ticker = ? AND trade_date <= CAST(? AS DATE)
        ORDER BY trade_date DESC LIMIT 65
        """,
        [ticker, as_of],
    ).fetchall()

    if len(rows) < _MIN_DAYS:
        msg = f"Only {len(rows)} days of price history (need {_MIN_DAYS})"
        logger.debug("WARNING: %s %s — %s", ticker, as_of, msg)
        for name in ["return_20d", "return_60d", "volume_spike", "drawdown_from_high"]:
            features.append({
                "feature_group": "momentum",
                "feature_name": name,
                "feature_value": None,
                "feature_score": None,
                "confidence": "low",
                "source": "prices_daily",
                "metadata": {"warning": msg},
            })
        return features

    prices = [r[1] for r in rows]
    volumes = [r[2] for r in rows if r[2]]

    current = prices[0]
    p20 = prices[min(20, len(prices) - 1)]
    p60 = prices[min(60, len(prices) - 1)]

    ret_20 = (current - p20) / p20 * 100 if p20 else None
    ret_60 = (current - p60) / p60 * 100 if p60 else None

    # 20d return score
    if ret_20 is not None:
        if ret_20 > 15:
            score = 80.0
        elif ret_20 > 5:
            score = 65.0
        elif ret_20 >= -5:
            score = 50.0
        else:
            score = 25.0
        features.append({
            "feature_group": "momentum",
            "feature_name": "return_20d",
            "feature_value": round(ret_20, 2),
            "feature_score": score,
            "confidence": "high",
            "source": "prices_daily",
        })

    # 60d return score
    if ret_60 is not None:
        if ret_60 > 25:
            score = 80.0
        elif ret_60 > 10:
            score = 65.0
        elif ret_60 >= -10:
            score = 50.0
        else:
            score = 25.0
        features.append({
            "feature_group": "momentum",
            "feature_name": "return_60d",
            "feature_value": round(ret_60, 2),
            "feature_score": score,
            "confidence": "high",
            "source": "prices_daily",
        })

    # Volume spike (current volume vs 20d avg)
    if len(volumes) >= 5:
        avg_vol = sum(volumes[1:21]) / min(20, len(volumes) - 1) if len(volumes) > 1 else None
        cur_vol = volumes[0]
        if avg_vol and avg_vol > 0:
            vol_ratio = cur_vol / avg_vol
            score = min(90.0, 50.0 + (vol_ratio - 1) * 20)
            features.append({
                "feature_group": "momentum",
                "feature_name": "volume_spike",
                "feature_value": round(vol_ratio, 2),
                "feature_score": max(0.0, score),
                "confidence": "high",
                "source": "prices_daily",
            })

    # Drawdown from recent high
    high_20 = max(prices[:20]) if len(prices) >= 20 else max(prices)
    if high_20 > 0:
        drawdown = (current - high_20) / high_20 * 100
        score = max(0.0, 50.0 + drawdown * 2)  # drawdown is negative
        features.append({
            "feature_group": "momentum",
            "feature_name": "drawdown_from_high",
            "feature_value": round(drawdown, 2),
            "feature_score": min(100.0, score),
            "confidence": "high",
            "source": "prices_daily",
        })

    return features
