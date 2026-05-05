"""FX Prediction Pipeline -- hourly orchestration."""
from __future__ import annotations

import logging
from datetime import datetime

import duckdb

logger = logging.getLogger("mhde.fx.pipeline")


def run_fx_prediction_pipeline(
    conn: duckdb.DuckDBPyConnection,
    bar_datetime: datetime | None = None,
    send_alerts: bool = True,
    skip_outcomes: bool = False,
) -> dict:
    from fx.ml.predict import score_bar, fill_outcomes
    from fx.ml.signals import generate_signal, send_telegram_alert

    logger.info("Starting FX prediction pipeline")

    result = score_bar(conn, bar_datetime)

    if not result["predictions"]:
        logger.warning("No predictions generated")
        return result

    signal = generate_signal(result["predictions"], result["datetime"], result["price"], conn)
    result["signal"] = signal

    if signal and send_alerts:
        send_telegram_alert(signal, conn)

    if not skip_outcomes:
        fill_outcomes(conn)

    print(f"\n{'='*50}")
    print(f"FX PREDICTION -- {result['datetime']}")
    print(f"GBP/EUR: {result['price']:.5f}")
    print(f"{'='*50}")
    for key, pred in sorted(result["predictions"].items()):
        print(f"  {pred['direction']:>5} {pred['horizon']}: {pred['probability']:.1%}")
    if signal:
        print(f"\n  SIGNAL: {signal['type']}")
    else:
        print(f"\n  Signal: WAIT")
    print(f"{'='*50}")

    return result
