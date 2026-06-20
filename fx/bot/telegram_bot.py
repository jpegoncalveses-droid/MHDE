"""FX Telegram bot — long-polling command handler and shared alert sender.

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from the MHDE host env file
(storage.config.load_env_file; default ~/.config/mhde/telegram.env), falling
back to whatever is already in the environment.

Public entry points:
    run_bot()                   — long-polling loop, blocks forever.
    send_signal_alert(signal)   — used by fx/ml/signals.py to dispatch BUY/SELL alerts
                                  with alerts_enabled gating and 4h cooldown.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("mhde.fx.bot")

API_BASE = "https://api.telegram.org/bot{token}/{method}"
LONG_POLL_TIMEOUT_S = 25
HTTP_TIMEOUT_S = 30
COOLDOWN_HOURS = 4


def _load_env() -> None:
    """Load TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID from the MHDE host env file if
    not already set in the environment."""
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        return
    from storage.config import load_env_file

    load_env_file()


def _credentials() -> tuple[str, str]:
    _load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    return token, chat


# ─── Telegram API wrappers ────────────────────────────────────────────────────

def _api_post(method: str, payload: dict, token: str, timeout: int = HTTP_TIMEOUT_S) -> dict:
    url = API_BASE.format(token=token, method=method)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _api_get(method: str, params: dict, token: str, timeout: int = HTTP_TIMEOUT_S) -> dict:
    qs = urllib.parse.urlencode(params)
    url = API_BASE.format(token=token, method=method) + "?" + qs
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_message(text: str, *, parse_mode: str | None = None) -> int | None:
    """Send a message to the configured chat. Returns message_id or None on failure."""
    token, chat_id = _credentials()
    if not token or not chat_id:
        logger.warning("Telegram credentials missing — message not sent")
        return None
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        result = _api_post("sendMessage", payload, token)
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        logger.error("Telegram send failed: %s", e)
        return None
    if not result.get("ok"):
        logger.error("Telegram send rejected: %s", result)
        return None
    return result["result"]["message_id"]


# ─── DB access helpers (open fresh connection per call) ───────────────────────

def _open_conn(read_only: bool = False):
    """Open a DuckDB connection using the engine's configured path."""
    import duckdb
    from storage.config import load_engine_config
    from storage.db import get_connection
    from storage.migrations import run_migrations
    from fx.schema import create_all_tables

    cfg = load_engine_config()
    if read_only:
        return duckdb.connect(cfg["db_path"], read_only=True)
    conn = get_connection(cfg["db_path"])
    run_migrations(conn)
    create_all_tables(conn)
    return conn


def _ensure_alert_state_row(conn) -> None:
    conn.execute(
        "INSERT INTO fx_alert_state (id, alerts_enabled) VALUES (1, TRUE) "
        "ON CONFLICT (id) DO NOTHING"
    )


def get_alerts_enabled() -> bool:
    conn = _open_conn()
    try:
        _ensure_alert_state_row(conn)
        row = conn.execute(
            "SELECT alerts_enabled FROM fx_alert_state WHERE id = 1"
        ).fetchone()
        return bool(row[0]) if row else True
    finally:
        conn.close()


def set_alerts_enabled(enabled: bool) -> None:
    conn = _open_conn()
    try:
        _ensure_alert_state_row(conn)
        conn.execute(
            "UPDATE fx_alert_state SET alerts_enabled = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = 1",
            [enabled],
        )
    finally:
        conn.close()


def _last_alert_time(conn, signal_type: str) -> Optional[datetime]:
    col = "last_buy_alert_at" if signal_type == "BUY_GBP" else "last_sell_alert_at"
    row = conn.execute(
        f"SELECT {col} FROM fx_alert_state WHERE id = 1"
    ).fetchone()
    return row[0] if row and row[0] else None


def _mark_alert_sent(conn, signal_type: str) -> None:
    col = "last_buy_alert_at" if signal_type == "BUY_GBP" else "last_sell_alert_at"
    conn.execute(
        f"UPDATE fx_alert_state SET {col} = CURRENT_TIMESTAMP, "
        f"updated_at = CURRENT_TIMESTAMP WHERE id = 1"
    )


def _current_position(conn) -> Optional[str]:
    row = conn.execute(
        "SELECT position FROM fx_position ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def _is_actionable(position: Optional[str], signal_type: str) -> bool:
    """An alert is actionable only if the signal implies a conversion the user can actually make."""
    if position is None:
        return True
    if position == "HOLDING_EUR":
        return signal_type == "BUY_GBP"
    if position == "HOLDING_GBP":
        return signal_type == "SELL_GBP"
    return True


# ─── Public alert sender used by the prediction pipeline ──────────────────────

def send_signal_alert(signal: dict) -> bool:
    """Send a BUY_GBP/SELL_GBP alert subject to alerts_enabled, position, and 4h cooldown.

    Returns True if a message was sent. Skips silently for WAIT, disabled,
    non-actionable (given current position), or in-cooldown.
    """
    sig_type = signal.get("type")
    if sig_type not in ("BUY_GBP", "SELL_GBP"):
        return False

    conn = _open_conn()
    try:
        _ensure_alert_state_row(conn)
        enabled = conn.execute(
            "SELECT alerts_enabled FROM fx_alert_state WHERE id = 1"
        ).fetchone()[0]
        if not enabled:
            logger.info("Alerts disabled — skipping %s alert", sig_type)
            return False
        position = _current_position(conn)
        if not _is_actionable(position, sig_type):
            logger.info(
                "Suppressed %s alert: position=%s — not actionable",
                sig_type, position,
            )
            return False
        last = _last_alert_time(conn, sig_type)
        if last is not None:
            elapsed = datetime.utcnow() - last if last.tzinfo is None \
                else datetime.now(timezone.utc) - last
            if elapsed < timedelta(hours=COOLDOWN_HOURS):
                logger.info(
                    "Cooldown active for %s (%.1fh elapsed) — skipping",
                    sig_type, elapsed.total_seconds() / 3600,
                )
                return False

        text = _format_signal_message(signal)
        msg_id = send_message(text)
        if msg_id is None:
            return False

        _mark_alert_sent(conn, sig_type)
        conn.execute(
            "UPDATE fx_signals SET telegram_sent = TRUE, "
            "telegram_sent_at = CURRENT_TIMESTAMP "
            "WHERE datetime_utc = ? AND signal_type = ?",
            [signal["datetime"], sig_type],
        )
        logger.info("Alert sent: %s at %s", sig_type, signal["datetime"])
        return True
    finally:
        conn.close()


def _format_signal_message(signal: dict) -> str:
    direction = "GBP strength" if signal["type"] == "BUY_GBP" else "GBP weakness"
    return (
        f"FX Signal: {signal['type']}\n"
        f"GBP/EUR: {signal['price']:.5f}\n"
        f"Time: {signal['datetime'].strftime('%Y-%m-%d %H:%M')} UTC\n\n"
        f"Direction: {direction} expected\n"
        f"P(up 20pip 24h): {signal['prob_up_24h']:.1%}\n"
        f"P(down 20pip 24h): {signal['prob_down_24h']:.1%}\n"
        f"P(up 20pip 48h): {signal['prob_up_48h']:.1%}\n"
        f"P(down 20pip 48h): {signal['prob_down_48h']:.1%}"
    )


# ─── Command handlers ─────────────────────────────────────────────────────────

def _format_position_status() -> str:
    from pipelines.freshness import check_fx_freshness

    conn = _open_conn(read_only=False)
    try:
        pos_row = conn.execute(
            "SELECT position, entry_rate, entry_date FROM fx_position "
            "ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        latest = conn.execute(
            "SELECT datetime_utc, gbpeur_close FROM fx_prices_hourly "
            "ORDER BY datetime_utc DESC LIMIT 1"
        ).fetchone()
        sig_row = conn.execute(
            "SELECT datetime_utc, signal_type, gbpeur_price FROM fx_signals "
            "ORDER BY datetime_utc DESC LIMIT 1"
        ).fetchone()
        freshness = check_fx_freshness(conn)
    finally:
        conn.close()

    stale_flag = "" if freshness.is_fresh else " ⚠️ STALE"

    if pos_row is None:
        lines = [
            "No position recorded.",
            "Use /fx_sold_gbp <rate> or /fx_bought_gbp <rate> to set one.",
            "",
            f"Data freshness: latest bar {freshness.latest} "
            f"(age {freshness.age_str}, threshold {freshness.threshold}){stale_flag}",
        ]
        return "\n".join(lines)

    position, entry_rate, entry_date = pos_row
    lines = [
        f"Position: {position}",
        f"Entry rate: {entry_rate:.5f}",
        f"Entry date: {str(entry_date)[:10]}",
    ]
    if latest is not None:
        latest_dt, latest_price = latest
        lines.append(f"Current GBP/EUR: {latest_price:.5f} ({latest_dt})")
        pnl_pips = (latest_price - entry_rate) * 10000
        if position == "HOLDING_EUR":
            pnl_pips = -pnl_pips
        lines.append(f"P&L: {pnl_pips:+.1f} pips")
    else:
        lines.append("No price data available.")

    if sig_row is not None:
        sig_dt, sig_type, sig_price = sig_row
        lines.append(f"Latest signal: {sig_type} @ {sig_price:.5f} ({sig_dt})")
    else:
        lines.append("Latest signal: (none)")

    lines.append(
        f"Data freshness: age {freshness.age_str} "
        f"(threshold {freshness.threshold}){stale_flag}"
    )
    return "\n".join(lines)


def _set_position(holding: str, rate: float) -> str:
    conn = _open_conn()
    try:
        position = f"HOLDING_{holding}"
        now = datetime.utcnow()
        conn.execute("DELETE FROM fx_position")
        conn.execute(
            "INSERT INTO fx_position (position, entry_rate, entry_date, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            [position, rate, now],
        )
        return f"Position updated: {position} @ {rate:.5f} on {now.date()}"
    finally:
        conn.close()


def _format_forecast() -> str:
    conn = _open_conn(read_only=True)
    try:
        latest_dt = conn.execute(
            "SELECT MAX(datetime_utc) FROM fx_ml_predictions"
        ).fetchone()[0]
        if latest_dt is None:
            return "No predictions available yet. Run `python main.py fx predict`."
        preds = conn.execute(
            "SELECT direction, horizon, predicted_probability "
            "FROM fx_ml_predictions WHERE datetime_utc = ? "
            "ORDER BY direction, horizon",
            [latest_dt],
        ).fetchall()
        price_row = conn.execute(
            "SELECT gbpeur_close FROM fx_prices_hourly WHERE datetime_utc = ?",
            [latest_dt],
        ).fetchone()
        pos_row = conn.execute(
            "SELECT position FROM fx_position ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    probs: dict[str, float] = {}
    for direction, horizon, p in preds:
        probs[f"{direction}_{horizon}"] = float(p)

    from fx.config import SIGNAL_BUY_THRESHOLD, SIGNAL_SELL_THRESHOLD, SIGNAL_COUNTER_MAX
    p_up = probs.get("up_24h", 0)
    p_down = probs.get("down_24h", 0)
    if p_up >= SIGNAL_BUY_THRESHOLD and p_down < SIGNAL_COUNTER_MAX:
        signal = "BUY_GBP"
    elif p_down >= SIGNAL_SELL_THRESHOLD and p_up < SIGNAL_COUNTER_MAX:
        signal = "SELL_GBP"
    else:
        signal = "WAIT"

    lines = [f"Forecast for {latest_dt}"]
    if price_row:
        lines.append(f"GBP/EUR: {float(price_row[0]):.5f}")
    lines.append("")
    lines.append("Probabilities:")
    for key in ("up_24h", "down_24h", "up_48h", "down_48h"):
        if key in probs:
            lines.append(f"  {key}: {probs[key]:.1%}")
    lines.append("")
    lines.append(f"Signal: {signal}")
    if pos_row is not None:
        position = pos_row[0]
        rec = _contextualize(position, signal)
        if rec:
            lines.append(f"Recommendation: {rec}")
    return "\n".join(lines)


def _contextualize(position: str, signal: str) -> str:
    if signal == "WAIT":
        return ""
    if position == "HOLDING_EUR" and signal == "BUY_GBP":
        return "ACTIONABLE — Consider converting EUR→GBP. GBP/EUR expected to rise."
    if position == "HOLDING_GBP" and signal == "SELL_GBP":
        return "ACTIONABLE — Consider converting GBP→EUR. GBP/EUR expected to drop."
    if position == "HOLDING_EUR" and signal == "SELL_GBP":
        return "Not actionable (already in EUR). Hold position; GBP/EUR expected to drop."
    if position == "HOLDING_GBP" and signal == "BUY_GBP":
        return "Not actionable (already in GBP). Hold position; GBP/EUR expected to rise."
    return ""


def _format_performance() -> str:
    conn = _open_conn(read_only=True)
    try:
        rows = conn.execute(
            "SELECT direction, horizon, COUNT(*) AS n, "
            "SUM(CASE WHEN actual_hit THEN 1 ELSE 0 END) AS hits, "
            "AVG(actual_max_pips) AS avg_pips "
            "FROM fx_ml_predictions WHERE outcome_filled_at IS NOT NULL "
            "GROUP BY direction, horizon ORDER BY direction, horizon"
        ).fetchall()
        n_signals = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN telegram_sent THEN 1 ELSE 0 END) "
            "FROM fx_signals"
        ).fetchone()
    finally:
        conn.close()

    lines = ["FX Performance"]
    if not rows:
        lines.append("No outcomes filled yet.")
    else:
        lines.append("")
        lines.append("Per-model precision (filled outcomes):")
        for direction, horizon, n, hits, avg_pips in rows:
            prec = (hits / n * 100) if n else 0
            avg_p = avg_pips if avg_pips is not None else 0
            lines.append(
                f"  {direction} {horizon}: {hits}/{n} = {prec:.1f}% | avg {avg_p:.1f} pips"
            )
    if n_signals:
        total, sent = n_signals
        sent = sent or 0
        lines.append("")
        lines.append(f"Signals total: {total} (alerts sent: {sent})")
    return "\n".join(lines)


def _format_alert_status() -> str:
    enabled = get_alerts_enabled()
    return f"Alerts: {'ENABLED' if enabled else 'DISABLED'}"


# ─── Command dispatch ─────────────────────────────────────────────────────────

def _parse_rate(arg: str) -> Optional[float]:
    try:
        v = float(arg)
        if v <= 0 or v > 10:
            return None
        return v
    except (ValueError, TypeError):
        return None


def handle_command(text: str) -> str:
    parts = text.strip().split()
    if not parts:
        return "Empty command."
    cmd = parts[0].lower().split("@")[0]  # strip @botname suffix
    args = parts[1:]

    if cmd == "/fx_status":
        return _format_position_status()
    if cmd == "/fx_forecast":
        return _format_forecast()
    if cmd == "/fx_performance":
        return _format_performance()
    if cmd == "/fx_alert_status":
        return _format_alert_status()
    if cmd == "/fx_alerts_on":
        set_alerts_enabled(True)
        return "Alerts ENABLED."
    if cmd == "/fx_alerts_off":
        set_alerts_enabled(False)
        return "Alerts DISABLED."
    if cmd == "/fx_bought_gbp":
        if not args:
            return "Usage: /fx_bought_gbp <rate>"
        rate = _parse_rate(args[0])
        if rate is None:
            return f"Invalid rate: {args[0]}"
        return _set_position("GBP", rate)
    if cmd == "/fx_sold_gbp":
        if not args:
            return "Usage: /fx_sold_gbp <rate>"
        rate = _parse_rate(args[0])
        if rate is None:
            return f"Invalid rate: {args[0]}"
        return _set_position("EUR", rate)
    if cmd in ("/start", "/help"):
        return (
            "MHDE FX bot commands:\n"
            "/fx_status — current position and P&L\n"
            "/fx_forecast — latest signal and probabilities\n"
            "/fx_performance — model precision\n"
            "/fx_bought_gbp <rate> — record EUR→GBP conversion\n"
            "/fx_sold_gbp <rate> — record GBP→EUR conversion\n"
            "/fx_alerts_on /fx_alerts_off /fx_alert_status"
        )
    return f"Unknown command: {cmd}. Try /help."


# ─── Long-polling loop ────────────────────────────────────────────────────────

def run_bot() -> None:
    token, chat_id = _credentials()
    if not token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set "
            "(in env or ~/.config/mhde/telegram.env)"
        )
    allowed_chat = str(chat_id)

    me = _api_get("getMe", {}, token)
    if not me.get("ok"):
        raise RuntimeError(f"getMe failed: {me}")
    logger.info("Bot started: @%s", me["result"].get("username", "?"))

    offset: Optional[int] = None
    while True:
        try:
            params = {"timeout": LONG_POLL_TIMEOUT_S, "allowed_updates": json.dumps(["message"])}
            if offset is not None:
                params["offset"] = offset
            resp = _api_get(
                "getUpdates", params, token,
                timeout=LONG_POLL_TIMEOUT_S + 10,
            )
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            logger.warning("getUpdates failed: %s — retrying in 5s", e)
            time.sleep(5)
            continue

        if not resp.get("ok"):
            logger.warning("getUpdates not ok: %s — retrying in 5s", resp)
            time.sleep(5)
            continue

        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message")
            if not msg:
                continue
            chat = msg.get("chat", {})
            if str(chat.get("id")) != allowed_chat:
                logger.info("Ignored message from chat %s", chat.get("id"))
                continue
            text = msg.get("text", "")
            if not text.startswith("/"):
                continue
            try:
                reply = handle_command(text)
            except Exception as e:
                logger.exception("Command handler failed: %s", text)
                reply = f"Error handling command: {e}"
            send_message(reply)
