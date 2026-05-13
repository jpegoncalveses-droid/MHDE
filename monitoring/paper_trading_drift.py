"""Monitor: paper-trading drift + engine liveness (Gap 2).

Reads the crypto-trading-engine's DuckDB **read-only** (path from
``CRYPTO_ENGINE_DB_PATH``) and MHDE's ``crypto_ml_labels``, and runs four
checks:

  A. Engine liveness  — the monitor phase is ticking; the entry phase ran today.
  B. Stuck positions  — nothing wedged in ``entry_pending`` / ``exit_pending``.
  C. Closed-trade win rate (rolling 14d, post-cost P&L > 0) vs the Phase 1B
     walkfold band.  Sample-gated.  NOTE: the engine does not yet persist
     exit fill prices for market exits (``orders.price`` is NULL; the exit
     ``order_filled`` event has no price), so this arm currently reports
     "uncomputable" — it activates once the engine persists realized P&L.
     See KI-136.
  D. Label hit rate (top-K reaching +10% within 10d, rolling 14d by *label
     settlement date*) vs the walkfold band.  Sample-gated.

Checks C and D are suppressed (status stays OK, body notes "insufficient
sample") until ``MIN_CLOSED_FOR_HITRATE`` qualifying trades accumulate — a
14-day window has very little data at the start of paper trading and an
ungated alert there would be pure noise.

P&L-band / drawdown / monthly-return arms are intentionally **not** here:
the engine's ``daily_pnl`` table is empty while its reconcile timer is
disabled (RECONCILE-001). Those arms are deferred — see KNOWN_ISSUES "Gap 2.5".

Cross-repo note: reading the engine's DuckDB is a deliberate, scoped
exception to ``INTERFACE.md``'s "no database access between systems" rule —
read-only, monitoring-only, the engine remains the source of truth. See
DECISIONS.md ADR-020.

Bands are walkfold-derived; see ``docs/strategy_analysis_2026-05-10.md``.
Schedule: every 15 minutes (``mhde-monitor-paper-trading-drift.timer``).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from monitoring.alert import MonitorResult, send_alert

logger = logging.getLogger("mhde.monitoring.paper_trading_drift")

_MONITORING_YAML = Path(__file__).resolve().parent.parent / "config" / "monitoring.yaml"


def _load_monitoring_config() -> dict:
    """Read ``config/monitoring.yaml``; returns ``{}`` if absent or unreadable.

    Kept module-level so tests can monkey-patch a fixture dict in without
    touching the real config file.
    """
    try:
        import yaml
        if not _MONITORING_YAML.exists():
            return {}
        return yaml.safe_load(_MONITORING_YAML.read_text()) or {}
    except Exception:
        logger.exception("paper_trading_drift: failed to load %s", _MONITORING_YAML)
        return {}


def _latest_baseline_date() -> Optional[date]:
    """Return the most-recent ``strategy_baselines[*].date`` or ``None``.

    Schema (config/monitoring.yaml):
        paper_trading_drift:
          strategy_baselines:
            - date: "2026-05-12"
              reason: "..."

    Bad rows are skipped; trades before the latest valid date are excluded
    from Check C (closed win rate) and Check D (label hit rate).
    """
    cfg = _load_monitoring_config()
    items = (cfg.get("paper_trading_drift") or {}).get("strategy_baselines") or []
    parsed: list[date] = []
    for item in items:
        raw = item.get("date") if isinstance(item, dict) else None
        if isinstance(raw, date) and not isinstance(raw, datetime):
            parsed.append(raw)
        elif isinstance(raw, str):
            try:
                parsed.append(datetime.strptime(raw, "%Y-%m-%d").date())
            except ValueError:
                logger.warning(
                    "paper_trading_drift: skipping invalid baseline date %r", raw
                )
    return max(parsed) if parsed else None

# ── config ───────────────────────────────────────────────────────────
DEFAULT_ENGINE_DB = "/home/jpcg/crypto-trading-engine/data/trading_engine.duckdb"
ENGINE_DB_ENV = "CRYPTO_ENGINE_DB_PATH"

ROLLING_WINDOW_DAYS = 14
MIN_CLOSED_FOR_HITRATE = 20
LABEL_SETTLE_DAYS = 10  # crypto_ml_labels.label_10d_10pct settles 10 days out
ROUND_TRIP_FEE_PCT = 0.0009  # ≈ 0.045% taker × 2 legs; engine doesn't store per-trade fees

# Walkfold bands (docs/strategy_analysis_2026-05-10.md). NOTE: the engine
# entered against these; active_spec.json's `expected_hit_rate` (~0.871) IS the
# trade win rate below — it is NOT the label hit rate. Keep the two distinct.
TRADE_WIN_RATE_WARN = (0.74, 0.99)   # median ≈ 0.869
TRADE_WIN_RATE_CRIT_FLOOR = 0.60     # below this → critical (above 1.0 impossible)
LABEL_HIT_RATE_WARN = (0.32, 0.62)   # median ≈ 0.425, top-6
LABEL_HIT_RATE_CRIT = (0.20, 0.75)

ENGINE_MONITOR_STALE_WARN_MIN = 5    # engine's monitor phase runs every minute
ENGINE_MONITOR_STALE_CRIT_MIN = 20
PENDING_STALE_WARN_MIN = 10          # matches the 15-min monitor cadence
PENDING_STALE_CRIT_MIN = 30
ENTRY_GRACE_CUTOFF_UTC = time(8, 30)  # first 15-min cycle by which today's entry should have run

CHECK_RECONCILE = False  # engine's reconcile timer disabled pending RECONCILE-001

PENDING_STATES = ("entry_pending", "exit_pending")


# ── small result type ────────────────────────────────────────────────
@dataclass
class _Finding:
    severity: str  # "ok" | "warn" | "critical"
    line: str


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _open_engine_db():
    import duckdb
    path = os.environ.get(ENGINE_DB_ENV, DEFAULT_ENGINE_DB)
    return duckdb.connect(path, read_only=True)


def _open_mhde_db():
    import duckdb
    from storage.config import load_engine_config
    return duckdb.connect(load_engine_config()["db_path"], read_only=True)


# ── Check A — engine liveness ────────────────────────────────────────
def _check_engine_liveness(eng, now: datetime, metrics: dict) -> list[_Finding]:
    out: list[_Finding] = []

    row = eng.execute(
        "SELECT max(started_at) FROM engine_runs WHERE phase = 'monitor' AND success = true"
    ).fetchone()
    last_monitor = row[0] if row else None
    if last_monitor is None:
        metrics["engine_monitor_age_sec"] = None
        out.append(_Finding("critical", "engine: no successful 'monitor' cycle ever recorded"))
    else:
        age_min = (now - last_monitor).total_seconds() / 60.0
        metrics["engine_monitor_age_sec"] = round(age_min * 60.0, 1)
        if age_min > ENGINE_MONITOR_STALE_CRIT_MIN:
            out.append(_Finding("critical",
                f"engine: last 'monitor' cycle {age_min:.1f} min ago (> {ENGINE_MONITOR_STALE_CRIT_MIN} min) — engine looks down"))
        elif age_min > ENGINE_MONITOR_STALE_WARN_MIN:
            out.append(_Finding("warn",
                f"engine: last 'monitor' cycle {age_min:.1f} min ago (> {ENGINE_MONITOR_STALE_WARN_MIN} min)"))
        else:
            out.append(_Finding("ok", f"engine: monitor cycle healthy ({age_min:.1f} min ago)"))

    # entry phase ran today?
    if now.time() >= ENTRY_GRACE_CUTOFF_UTC:
        midnight = datetime.combine(now.date(), time.min)
        row = eng.execute(
            "SELECT max(started_at) FROM engine_runs "
            "WHERE phase = 'entry' AND success = true AND started_at >= ?",
            [midnight],
        ).fetchone()
        last_entry_today = row[0] if row else None
        metrics["entry_ran_today"] = last_entry_today is not None
        if last_entry_today is None:
            out.append(_Finding("warn",
                f"engine: no successful 'entry' run today (it's {now.time().strftime('%H:%M')} UTC, past the {ENTRY_GRACE_CUTOFF_UTC.strftime('%H:%M')} cutoff)"))
        else:
            out.append(_Finding("ok", f"engine: entry phase ran today at {last_entry_today.strftime('%H:%M')} UTC"))
    else:
        metrics["entry_ran_today"] = None
        out.append(_Finding("ok", f"engine: entry not due yet (before {ENTRY_GRACE_CUTOFF_UTC.strftime('%H:%M')} UTC cutoff)"))

    if CHECK_RECONCILE:  # pragma: no cover — disabled pending RECONCILE-001
        midnight = datetime.combine(now.date(), time.min)
        row = eng.execute(
            "SELECT max(started_at) FROM engine_runs "
            "WHERE phase = 'reconcile' AND success = true AND started_at >= ?",
            [midnight],
        ).fetchone()
        if (row[0] if row else None) is None:
            out.append(_Finding("warn", "engine: no successful 'reconcile' run today"))

    return out


# ── Check B — stuck positions ────────────────────────────────────────
def _check_stuck_positions(eng, now: datetime, metrics: dict) -> list[_Finding]:
    placeholders = ",".join("?" for _ in PENDING_STATES)
    rows = eng.execute(
        f"SELECT symbol, current_state, updated_at FROM positions "
        f"WHERE current_state IN ({placeholders})",
        list(PENDING_STATES),
    ).fetchall()

    stuck_warn: list[str] = []
    stuck_crit: list[str] = []
    for symbol, state, updated_at in rows:
        if updated_at is None:
            continue
        age_min = (now - updated_at).total_seconds() / 60.0
        if age_min > PENDING_STALE_CRIT_MIN:
            stuck_crit.append(f"{symbol} in {state} for {age_min:.0f} min")
        elif age_min > PENDING_STALE_WARN_MIN:
            stuck_warn.append(f"{symbol} in {state} for {age_min:.0f} min")

    metrics["n_stuck_positions"] = len(stuck_warn) + len(stuck_crit)
    out: list[_Finding] = []
    if stuck_crit:
        out.append(_Finding("critical", "stuck positions (critical): " + "; ".join(stuck_crit)))
    if stuck_warn:
        out.append(_Finding("warn", "stuck positions: " + "; ".join(stuck_warn)))
    if not out:
        out.append(_Finding("ok", "no positions stuck in a *_pending state"))
    return out


# ── Check C — closed-trade win rate ──────────────────────────────────
def _check_closed_win_rate(eng, now: datetime, metrics: dict) -> list[_Finding]:
    """Win rate over closed trades in the last ROLLING_WINDOW_DAYS.

    Needs the exit fill price. The engine records market exits with
    ``orders.price = NULL`` (no limit price) and the ``order_filled`` exit
    event payload carries no price either, so today the only readable exit
    price is a non-NULL ``orders.price`` (e.g. a limit exit) — currently
    zero of those. Trades with no readable exit price are counted under
    ``closed_trade_no_exit_price`` and the arm reports that it cannot
    compute the rate yet (informational, not an alert; see KI-136).
    """
    rolling_cutoff_ts = now - timedelta(days=ROLLING_WINDOW_DAYS)
    baseline = _latest_baseline_date()
    baseline_ts = (datetime.combine(baseline, time.min)
                   if baseline is not None else None)
    # Floor = the later of the rolling window and the strategy baseline.
    effective_cutoff_ts = (max(rolling_cutoff_ts, baseline_ts)
                           if baseline_ts is not None else rolling_cutoff_ts)
    metrics["active_strategy_baseline_date"] = (
        baseline.isoformat() if baseline is not None else None
    )
    metrics["effective_window_start"] = effective_cutoff_ts.date().isoformat()

    rows = eng.execute(
        """
        SELECT p.entry_price, p.qty,
               SUM(o.price * o.qty) / NULLIF(SUM(o.qty), 0) AS sell_vwap,
               MAX(COALESCE(o.filled_at, p.updated_at))      AS exit_ts
        FROM positions p
        JOIN orders o
          ON o.position_id = p.id
         AND o.side = 'SELL' AND o.status = 'FILLED'
        WHERE p.current_state = 'exit_filled' AND p.entry_price IS NOT NULL
        GROUP BY p.id, p.entry_price, p.qty
        """
    ).fetchall()

    winners = 0
    n = 0                 # closed trades in-window WITH a readable exit price
    n_no_exit_price = 0   # closed trades in-window with exit price not recorded
    n_excluded_pre_baseline = 0
    for entry_price, qty, sell_vwap, exit_ts in rows:
        if exit_ts is None:
            continue
        if exit_ts < rolling_cutoff_ts:
            continue  # outside the 14d window — same as before
        if baseline_ts is not None and exit_ts < baseline_ts:
            n_excluded_pre_baseline += 1
            continue
        if entry_price is None or qty is None:
            continue
        if sell_vwap is None:
            n_no_exit_price += 1
            continue
        n += 1
        notional = entry_price * qty
        net = (sell_vwap - entry_price) * qty - ROUND_TRIP_FEE_PCT * notional
        if net > 0:
            winners += 1

    metrics["closed_trade_n"] = n
    metrics["closed_trade_no_exit_price"] = n_no_exit_price
    metrics["closed_trade_n_excluded_pre_baseline"] = n_excluded_pre_baseline
    np_note = (f"; {n_no_exit_price} more closed but exit price not recorded by the engine (KI-136)"
               if n_no_exit_price else "")

    if n < MIN_CLOSED_FOR_HITRATE:
        metrics["closed_trade_win_rate"] = None
        if n == 0 and n_no_exit_price:
            return [_Finding("ok",
                f"closed-trade win rate: {n_no_exit_price} closed trade(s) in last {ROLLING_WINDOW_DAYS}d "
                f"but the engine doesn't record exit fill prices yet — uncomputable (KI-136)")]
        return [_Finding("ok",
            f"closed-trade win rate: insufficient sample ({n}/{MIN_CLOSED_FOR_HITRATE} priced in last {ROLLING_WINDOW_DAYS}d{np_note})")]

    rate = winners / n
    metrics["closed_trade_win_rate"] = round(rate, 4)
    lo, hi = TRADE_WIN_RATE_WARN
    detail = f"{rate:.1%} ({winners}/{n}, last {ROLLING_WINDOW_DAYS}d; expected {lo:.0%}–{hi:.0%}{np_note})"
    if rate < TRADE_WIN_RATE_CRIT_FLOOR:
        return [_Finding("critical", f"closed-trade win rate {detail} — below {TRADE_WIN_RATE_CRIT_FLOOR:.0%} critical floor")]
    if rate < lo or rate > hi:
        return [_Finding("warn", f"closed-trade win rate {detail} — outside walkfold band")]
    return [_Finding("ok", f"closed-trade win rate {detail}")]


# ── Check D — label hit rate ─────────────────────────────────────────
def _check_label_hit_rate(eng, mhde, now: datetime, metrics: dict) -> list[_Finding]:
    today = now.date()
    # qualifying = closed positions whose label settled within the last window:
    #   settle = entry_date + LABEL_SETTLE_DAYS  ∈  [today - WINDOW, today]
    #   ⇔ entry_date ∈ [today - (WINDOW + SETTLE), today - SETTLE]
    rolling_entry_lo = today - timedelta(days=ROLLING_WINDOW_DAYS + LABEL_SETTLE_DAYS)
    entry_hi = today - timedelta(days=LABEL_SETTLE_DAYS)
    baseline = _latest_baseline_date()
    entry_lo = max(rolling_entry_lo, baseline) if baseline is not None else rolling_entry_lo

    pos_rows = eng.execute(
        "SELECT symbol, entry_date FROM positions "
        "WHERE current_state = 'exit_filled' AND entry_price IS NOT NULL "
        "AND entry_date IS NOT NULL"
    ).fetchall()

    candidates = [(s, d) for (s, d) in pos_rows if entry_lo <= d <= entry_hi]
    n_unsettled = sum(1 for (_s, d) in pos_rows if d > entry_hi)

    label_map: dict[tuple[str, Any], Optional[int]] = {}
    if candidates:
        lab_rows = mhde.execute(
            "SELECT symbol, trade_date, label_10d_10pct FROM crypto_ml_labels "
            "WHERE trade_date BETWEEN ? AND ?",
            [entry_lo, entry_hi],
        ).fetchall()
        label_map = {(s, d): v for (s, d, v) in lab_rows}

    hits = 0
    n = 0
    n_no_label = 0
    for sym, d in candidates:
        v = label_map.get((sym, d))
        if v is None:
            n_no_label += 1
            continue
        n += 1
        if int(v) == 1:
            hits += 1

    metrics["label_n"] = n
    metrics["label_unsettled_skipped"] = n_unsettled
    metrics["label_no_label_skipped"] = n_no_label

    if n < MIN_CLOSED_FOR_HITRATE:
        metrics["label_hit_rate"] = None
        extra = []
        if n_unsettled:
            extra.append(f"{n_unsettled} not settled yet")
        if n_no_label:
            extra.append(f"{n_no_label} with no label row")
        suffix = f" ({'; '.join(extra)})" if extra else ""
        return [_Finding("ok",
            f"label hit rate: insufficient sample ({n}/{MIN_CLOSED_FOR_HITRATE} settled in last {ROLLING_WINDOW_DAYS}d){suffix}")]

    rate = hits / n
    metrics["label_hit_rate"] = round(rate, 4)
    lo, hi = LABEL_HIT_RATE_WARN
    clo, chi = LABEL_HIT_RATE_CRIT
    detail = f"{rate:.1%} ({hits}/{n} settled, last {ROLLING_WINDOW_DAYS}d; expected {lo:.0%}–{hi:.0%})"
    if rate < clo or rate > chi:
        return [_Finding("critical", f"label hit rate {detail} — outside {clo:.0%}–{chi:.0%} critical band")]
    if rate < lo or rate > hi:
        return [_Finding("warn", f"label hit rate {detail} — outside walkfold band")]
    return [_Finding("ok", f"label hit rate {detail}")]


# ── orchestration ────────────────────────────────────────────────────
def _run_check(name: str, fn) -> list[_Finding]:
    try:
        return fn()
    except Exception as exc:  # a monitor must not die on a query hiccup
        logger.exception("paper_trading_drift: check %s errored", name)
        return [_Finding("warn", f"{name} check errored: {exc}")]


def run(engine_conn=None, mhde_conn=None, now: datetime | None = None) -> MonitorResult:
    started = datetime.now(timezone.utc)
    now = now or _utcnow_naive()

    close_eng = close_mhde = False
    if engine_conn is None:
        engine_conn = _open_engine_db()
        close_eng = True
    if mhde_conn is None:
        mhde_conn = _open_mhde_db()
        close_mhde = True

    metrics: dict[str, Any] = {}
    try:
        findings: list[_Finding] = []
        findings += _run_check("engine_liveness",
                               lambda: _check_engine_liveness(engine_conn, now, metrics))
        findings += _run_check("stuck_positions",
                               lambda: _check_stuck_positions(engine_conn, now, metrics))
        findings += _run_check("closed_win_rate",
                               lambda: _check_closed_win_rate(engine_conn, now, metrics))
        findings += _run_check("label_hit_rate",
                               lambda: _check_label_hit_rate(engine_conn, mhde_conn, now, metrics))
    finally:
        if close_eng:
            engine_conn.close()
        if close_mhde:
            mhde_conn.close()

    n_crit = sum(1 for f in findings if f.severity == "critical")
    n_warn = sum(1 for f in findings if f.severity == "warn")
    if n_crit:
        status, severity = "fail", "critical"
        title = f"paper-trading: {n_crit} critical" + (f" / {n_warn} warn" if n_warn else "")
    elif n_warn:
        status, severity = "warn", "warn"
        title = f"paper-trading: {n_warn} warn"
    else:
        status, severity = "ok", "info"
        title = "paper-trading: engine alive, no drift"

    body = "\n".join(f"- {f.line}" for f in findings)
    return MonitorResult(
        monitor="paper_trading_drift",
        status=status, severity=severity,
        title=title, body=body, metrics=metrics,
        started_at=started, finished_at=datetime.now(timezone.utc),
    )


def main() -> int:
    result = run()
    send_alert(result)
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
