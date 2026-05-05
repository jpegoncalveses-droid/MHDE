"""Signal generation and Telegram alerting for FX predictions."""
from __future__ import annotations

import logging
import os
from datetime import datetime

import duckdb
import requests

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
    from dotenv import load_dotenv
    load_dotenv()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping alert")
        return

    direction = "GBP strength" if signal["type"] == "BUY_GBP" else "GBP weakness"

    message = (
        f"FX Signal: {signal['type']}\n"
        f"GBP/EUR: {signal['price']:.5f}\n"
        f"Time: {signal['datetime'].strftime('%Y-%m-%d %H:%M')} UTC\n\n"
        f"Direction: {direction} expected\n"
        f"P(up 20pip 24h): {signal['prob_up_24h']:.1%}\n"
        f"P(down 20pip 24h): {signal['prob_down_24h']:.1%}\n"
        f"P(up 20pip 48h): {signal['prob_up_48h']:.1%}\n"
        f"P(down 20pip 48h): {signal['prob_down_48h']:.1%}"
    )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
        resp.raise_for_status()
        conn.execute("""
            UPDATE fx_signals SET telegram_sent = true, telegram_sent_at = CURRENT_TIMESTAMP
            WHERE datetime_utc = ? AND signal_type = ?
        """, [signal["datetime"], signal["type"]])
        logger.info("Telegram alert sent: %s at %s", signal["type"], signal["datetime"])
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
