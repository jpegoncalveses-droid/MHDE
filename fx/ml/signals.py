"""Signal generation and Telegram alerting for FX predictions."""
from __future__ import annotations

import logging
from datetime import datetime

import duckdb

from fx.config import SIGNAL_BUY_THRESHOLD, SIGNAL_SELL_THRESHOLD, SIGNAL_COUNTER_MAX
from fx.schema import create_all_tables

logger = logging.getLogger("mhde.fx.signals")


def generate_signal(predictions: dict, bar_datetime: datetime, price: float,
                    conn: duckdb.DuckDBPyConnection) -> dict | None:
    create_all_tables(conn)

    prob_up_24h = predictions.get("up_24h", {}).get("probability", 0)
    prob_down_24h = predictions.get("down_24h", {}).get("probability", 0)
    prob_up_48h = predictions.get("up_48h", {}).get("probability", 0)
    prob_down_48h = predictions.get("down_48h", {}).get("probability", 0)

    signal_type = "WAIT"
    if prob_up_24h >= SIGNAL_BUY_THRESHOLD and prob_down_24h < SIGNAL_COUNTER_MAX:
        signal_type = "BUY_GBP"
    elif prob_down_24h >= SIGNAL_SELL_THRESHOLD and prob_up_24h < SIGNAL_COUNTER_MAX:
        signal_type = "SELL_GBP"

    if signal_type == "WAIT":
        return None

    conn.execute("""
        INSERT INTO fx_signals (datetime_utc, signal_type, prob_up_24h, prob_down_24h,
                                prob_up_48h, prob_down_48h, gbpeur_price)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (datetime_utc, signal_type) DO NOTHING
    """, [bar_datetime, signal_type, prob_up_24h, prob_down_24h, prob_up_48h, prob_down_48h, price])

    return {
        "type": signal_type,
        "datetime": bar_datetime,
        "price": price,
        "prob_up_24h": prob_up_24h,
        "prob_down_24h": prob_down_24h,
        "prob_up_48h": prob_up_48h,
        "prob_down_48h": prob_down_48h,
    }


def send_telegram_alert(signal: dict, conn: duckdb.DuckDBPyConnection):
    """Route signal through fx.bot — applies alerts_enabled gate and 4h cooldown."""
    from fx.bot.telegram_bot import send_signal_alert
    send_signal_alert(signal)
