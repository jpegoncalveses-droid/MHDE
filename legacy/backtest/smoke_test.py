from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta

import duckdb

from backtest.historical_replay import replay
from backtest.labels import compute_labels
from backtest.metrics import compute_metrics

logger = logging.getLogger("mhde.backtest.smoke")

_WARNING = (
    "WARNING: Historical coverage is insufficient for reliable conclusions. "
    "This is a smoke test only. Accumulate multiple weeks of daily runs "
    "before interpreting results."
)


def run_smoke(conn: duckdb.DuckDBPyConnection, cfg: dict) -> dict:
    logger.warning(_WARNING)
    print(f"\n{_WARNING}\n")

    backtest_run_id = uuid.uuid4().hex[:16]
    as_of = date.today() - timedelta(days=1)

    historical = replay(conn, as_of_date=as_of, lookback_days=90)
    tickers = list({r["ticker"] for r in historical})

    forward_days = 20
    labels = compute_labels(conn, tickers, as_of, forward_days=forward_days)
    metrics = compute_metrics(labels)

    result = {
        "backtest_run_id": backtest_run_id,
        "as_of_date": as_of,
        "lookback_days": 90,
        "forward_days": forward_days,
        "tickers_tested": len(tickers),
        "labels_computed": len(labels),
        "hit_rate": metrics.get("hit_rate"),
        "avg_return": metrics.get("avg_return"),
        "warning": _WARNING,
    }

    _persist(conn, result)

    print("Backtest smoke results:")
    print(f"  As-of date:       {as_of}")
    print(f"  Tickers tested:   {len(tickers)}")
    print(f"  Labels computed:  {len(labels)}")
    if metrics.get("hit_rate") is not None:
        print(f"  Hit rate:         {metrics['hit_rate']:.1%}")
        print(f"  Avg forward ret:  {metrics['avg_return']:.2%}")
    else:
        print("  Hit rate:         insufficient data")
    print(f"\n  {_WARNING}")

    return result


def _persist(conn: duckdb.DuckDBPyConnection, result: dict) -> None:
    import json
    from datetime import date

    def _default(obj):
        if isinstance(obj, date):
            return obj.isoformat()
        return str(obj)

    try:
        conn.execute(
            """
            INSERT INTO backtest_runs
                (backtest_run_id, as_of_date, lookback_days, forward_days,
                 tickers_tested, hit_rate, avg_return, metrics_json, warning, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete')
            """,
            [
                result["backtest_run_id"], result["as_of_date"],
                result["lookback_days"], result["forward_days"],
                result["tickers_tested"], result["hit_rate"], result["avg_return"],
                json.dumps(result, default=_default), result["warning"],
            ],
        )
    except Exception as exc:
        logger.debug("Could not persist backtest run: %s", exc)
