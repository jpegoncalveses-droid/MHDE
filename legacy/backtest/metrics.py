from __future__ import annotations

import logging

logger = logging.getLogger("mhde.backtest.metrics")


def compute_metrics(labels: list[dict]) -> dict:
    if not labels:
        return {"tickers": 0, "hit_rate": None, "avg_return": None, "median_return": None}

    returns = [r["forward_return"] for r in labels if r.get("forward_return") is not None]
    if not returns:
        return {"tickers": len(labels), "hit_rate": None, "avg_return": None, "median_return": None}

    positive = sum(1 for r in returns if r > 0)
    avg_ret = sum(returns) / len(returns)
    sorted_ret = sorted(returns)
    mid = len(sorted_ret) // 2
    median_ret = sorted_ret[mid] if len(sorted_ret) % 2 else (
        (sorted_ret[mid - 1] + sorted_ret[mid]) / 2
    )

    return {
        "tickers": len(returns),
        "hit_rate": positive / len(returns),
        "avg_return": avg_ret,
        "median_return": median_ret,
        "best": max(returns),
        "worst": min(returns),
    }
