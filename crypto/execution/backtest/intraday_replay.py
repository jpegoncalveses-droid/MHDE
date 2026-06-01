"""Intraday faithful replay of Phase 1B walk-forward predictions.

Replays each daily prediction against **1-minute** klines (from the
separate research DB; see :mod:`crypto.execution.backtest.intraday_klines`)
using the live engine's exit stack — the deployed trailing stop
(:class:`~crypto.execution.backtest.policies.TrailingStopOnly`,
``trail_pct=0.30``, ``activation_pct=0.01``) wrapped in the engine-side
hard floor (:class:`~crypto.execution.backtest.policies.HardFloorOverlay`,
``hard_floor_pct=-0.05``) in **intraday arm-aware** mode. Costs come from
:mod:`crypto.execution.backtest.costs`.

This module holds the **pure** replay logic — entry rules and the 1-minute
exit walk — plus the DB-backed driver that loads predictions + klines and
aggregates the per-probability-bin report.

Entry anchor (KI-141, confirmed).  ``crypto_ml_predictions.prediction_date``
is the *features-as-of* day (``MAX(trade_date) FROM crypto_ml_features``,
i.e. T-1), **not** the day the trade entered, and the walk-forward rows
carry no ``predicted_at`` stamp (all NULL). The live engine enters at
00:45 UTC on the *export date* = ``prediction_date + 1`` (see
``crypto/exports/write_daily_predictions.py`` and engine
``INTERFACE.md §3.1``). :class:`DeployedEntry` therefore anchors at
``prediction_date + day_offset`` with ``day_offset=1`` by default. This is
the one load-bearing assumption of the replay; it is exposed as a
parameter so an alternative anchor is a config change, not a rebuild.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional, Sequence

from crypto.execution.backtest.costs import TradeCosts, compute_trade_costs
from crypto.execution.backtest.policies import HardFloorOverlay, TrailingStopOnly

logger = logging.getLogger("mhde.crypto.intraday_replay")

# Deployed exit-stack constants (Phase 1B winner + engine hard floor).
TRAIL_PCT = 0.30
ACTIVATION_PCT = 0.01
HARD_FLOOR_PCT = -0.05

# 10-day horizon for the walk-forward 10d models.
HORIZON_DAYS = 10


# ──────────────────────────────────────────────────────────────────────
# Data containers
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Prediction:
    """One daily prediction row to replay."""

    symbol: str
    prediction_date: date
    probability: float


@dataclass(frozen=True)
class Entry:
    """Resolved entry: timestamp + fill price."""

    entry_time: datetime
    entry_price: float


@dataclass(frozen=True)
class TradeResult:
    """One fully-costed replayed trade."""

    symbol: str
    prediction_date: date
    probability: float
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str
    hold_minutes: int
    peak_price: float
    up1_before_dn5: bool
    gross_return: float
    net_return: float
    volume_rank: Optional[int]
    traded: bool

    @property
    def prob_bin(self) -> float:
        import math
        return math.floor(self.probability * 10) / 10


@dataclass(frozen=True)
class ExitOutcome:
    """Result of walking the 1-minute bars for one trade."""

    exit_price: float
    exit_reason: str
    exit_time: datetime
    hold_minutes: int
    peak_price: float
    up1_before_dn5: bool
    gross_return: float


# ──────────────────────────────────────────────────────────────────────
# Pluggable entry rules
# ──────────────────────────────────────────────────────────────────────


def _bar_index(bars: Sequence[dict[str, Any]]) -> dict[datetime, dict[str, Any]]:
    return {b["open_time"]: b for b in bars}


class EntryRule(abc.ABC):
    """Given a prediction and that symbol's intraday bars, return the entry
    (timestamp + fill price), or ``None`` to skip the prediction."""

    @abc.abstractmethod
    def resolve(
        self, prediction: Prediction, bars: Sequence[dict[str, Any]]
    ) -> Optional[Entry]: ...


class DeployedEntry(EntryRule):
    """The **baseline** rule — the live engine's deployed behaviour.

    Enters at ``entry_hour:entry_minute`` UTC (default 00:45) on
    ``prediction_date + day_offset`` (default +1 day; see the module
    docstring / KI-141), filling at the **open** of that 1-minute bar.
    Returns ``None`` when that exact bar is missing (a data gap → the
    prediction is skipped and logged by the driver).
    """

    def __init__(self, *, entry_hour: int = 0, entry_minute: int = 45,
                 day_offset: int = 1) -> None:
        self.entry_hour = entry_hour
        self.entry_minute = entry_minute
        self.day_offset = day_offset

    def resolve(self, prediction, bars):
        entry_day = prediction.prediction_date + timedelta(days=self.day_offset)
        anchor = datetime.combine(
            entry_day, time(self.entry_hour, self.entry_minute), tzinfo=timezone.utc
        )
        bar = _bar_index(bars).get(anchor)
        if bar is None:
            return None
        return Entry(entry_time=anchor, entry_price=float(bar["open"]))


class FixedOffsetEntry(EntryRule):
    """Example alternative rule (wired to prove the interface; **not swept**
    in this dispatch).

    Enters ``hours`` after 00:00 UTC on ``prediction_date + day_offset``,
    filling at the open of that 1-minute bar. Conditional / intraday entry
    rules and fixed-hour sweeps are a config change for a follow-on.
    """

    def __init__(self, hours: int, *, day_offset: int = 1) -> None:
        self.hours = hours
        self.day_offset = day_offset

    def resolve(self, prediction, bars):
        entry_day = prediction.prediction_date + timedelta(days=self.day_offset)
        anchor = datetime.combine(
            entry_day, time(0, 0), tzinfo=timezone.utc
        ) + timedelta(hours=self.hours)
        bar = _bar_index(bars).get(anchor)
        if bar is None:
            return None
        return Entry(entry_time=anchor, entry_price=float(bar["open"]))


# ──────────────────────────────────────────────────────────────────────
# 1-minute exit walk
# ──────────────────────────────────────────────────────────────────────


def simulate_intraday_trade(
    entry_price: float,
    bars: Sequence[dict[str, Any]],
    *,
    trail_pct: float = TRAIL_PCT,
    activation_pct: float = ACTIVATION_PCT,
    hard_floor_pct: float = HARD_FLOOR_PCT,
) -> ExitOutcome:
    """Walk ``bars`` (the entry minute through the horizon, inclusive) under
    the deployed trail + arm-aware hard floor.

    ``bars`` must be sorted by ``open_time``; ``bars[0]`` is the entry
    minute (live from its open). The trail arms at ``peak ≥ entry × (1 +
    activation_pct)``; once armed the give-back trail wins, otherwise the
    −5% floor is the stop; if neither fires the time stop closes at the last
    bar's close. Within a single bar both the floor check and the
    ``up1_before_dn5`` path metric resolve **adverse-first** (the down-move
    is assumed to precede the up-move).

    The horizon is owned by the caller (it truncates ``bars`` to
    ``[entry, entry + 10d]``), so the time stop fires at the last supplied
    bar — robust to mid-window 1-minute gaps.
    """
    if not bars:
        raise ValueError("simulate_intraday_trade requires at least one bar")

    n = len(bars)
    inner = TrailingStopOnly(
        entry_price=entry_price, horizon_days=n,
        trail_pct=trail_pct, activation_pct=activation_pct,
    )
    policy = HardFloorOverlay(
        entry_price=entry_price, horizon_days=n, inner=inner,
        hard_floor_pct=hard_floor_pct, intraday_arm_aware=True,
    )

    up1_price = entry_price * (1.0 + activation_pct)
    dn5_price = entry_price * (1.0 + hard_floor_pct)
    up1_before_dn5: Optional[bool] = None

    entry_time = bars[0]["open_time"]

    for i, bar in enumerate(bars, start=1):
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
        open_ = float(bar["open"])

        # Path metric — adverse-first within the bar (down before up).
        if up1_before_dn5 is None:
            if low <= dn5_price:
                up1_before_dn5 = False
            elif high >= up1_price:
                up1_before_dn5 = True

        events = policy.step(i, high, low, close, open_)
        if events:
            ev = events[-1]
            exit_time = bar["open_time"]
            hold_minutes = int((exit_time - entry_time).total_seconds() // 60)
            if up1_before_dn5 is None:
                up1_before_dn5 = False
            return ExitOutcome(
                exit_price=float(ev.exit_price),
                exit_reason=ev.reason,
                exit_time=exit_time,
                hold_minutes=hold_minutes,
                peak_price=float(inner.peak_high),
                up1_before_dn5=bool(up1_before_dn5),
                gross_return=float(ev.exit_price) / entry_price - 1.0,
            )

    # Unreachable: the inner time stop fires on the final bar.
    raise AssertionError("exit walk completed without an exit event")


def compute_net_return(gross_return: float, costs: TradeCosts) -> float:
    """Net fractional return = gross − total round-trip costs."""
    return gross_return - costs.total


# ──────────────────────────────────────────────────────────────────────
# Aggregation
# ──────────────────────────────────────────────────────────────────────


def _median(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    if len(s) % 2:
        return s[m]
    return (s[m - 1] + s[m]) / 2.0


def stats_for(results: Sequence[TradeResult]) -> dict[str, Any]:
    """Summary metrics over an arbitrary set of trades (a bin, or the
    traded subset). All P&L metrics use **net** return.

    ``profit_factor`` is ``sum(gains) / sum(|losses|)``; it is ``None`` when
    there are no losing trades (undefined). ``win_rate`` /
    ``p_up1_before_dn5`` / averages are ``None`` for an empty set.
    """
    n = len(results)
    if n == 0:
        return {
            "n": 0, "win_rate": None, "p_up1_before_dn5": None,
            "avg_pnl": None, "median_pnl": None, "profit_factor": None,
            "avg_hold_hours": None, "exit_reason_mix": {},
        }
    nets = [r.net_return for r in results]
    gains = sum(x for x in nets if x > 0)
    losses = sum(-x for x in nets if x < 0)
    reason_mix: dict[str, int] = {}
    for r in results:
        reason_mix[r.exit_reason] = reason_mix.get(r.exit_reason, 0) + 1
    return {
        "n": n,
        "win_rate": sum(1 for x in nets if x > 0) / n,
        "p_up1_before_dn5": sum(1 for r in results if r.up1_before_dn5) / n,
        "avg_pnl": sum(nets) / n,
        "median_pnl": _median(nets),
        "profit_factor": (gains / losses) if losses > 0 else None,
        "avg_hold_hours": sum(r.hold_minutes for r in results) / n / 60.0,
        "exit_reason_mix": reason_mix,
    }


def aggregate_bins(results: Sequence[TradeResult]) -> list[dict[str, Any]]:
    """Group results into ``FLOOR(prob*10)/10`` bins; one stats dict per bin,
    sorted ascending by bin. Each dict is :func:`stats_for` plus ``"bin"``."""
    by_bin: dict[float, list[TradeResult]] = {}
    for r in results:
        by_bin.setdefault(r.prob_bin, []).append(r)
    out = []
    for b in sorted(by_bin):
        s = stats_for(by_bin[b])
        s["bin"] = b
        out.append(s)
    return out


# ──────────────────────────────────────────────────────────────────────
# DB-backed driver
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ReplayReport:
    window_start: date
    window_end: date
    entry_rule_desc: str
    n_predictions: int
    n_replayed: int
    n_skipped: int
    skipped: list[tuple[str, date, str]] = field(default_factory=list)
    results: list[TradeResult] = field(default_factory=list)
    bins: list[dict[str, Any]] = field(default_factory=list)
    traded_stats: dict[str, Any] = field(default_factory=dict)


def _load_predictions(conn, start_date, end_date, model_like) -> list[Prediction]:
    a, b = model_like
    rows = conn.execute(
        """
        SELECT symbol, prediction_date, predicted_probability
        FROM crypto_ml_predictions
        WHERE model_id LIKE ? AND model_id LIKE ?
          AND prediction_date BETWEEN ? AND ?
          AND predicted_probability IS NOT NULL
        ORDER BY prediction_date, symbol
        """,
        [a, b, start_date, end_date],
    ).fetchall()
    return [Prediction(symbol=s, prediction_date=d, probability=float(p))
            for (s, d, p) in rows]


def _compute_traded_keys(conn, predictions, top_n) -> set[tuple[str, date]]:
    """Faithful live selection: per prediction_date, drop post-parabolic /
    short-momentum exclusions (features as-of prediction_date), then keep the
    top-N survivors by probability. Returns the traded ``(symbol, date)`` set.
    """
    import pandas as pd

    from crypto.execution.backtest.selection import select_top_n
    from crypto.ml.postparabolic_filter import should_exclude

    if not predictions:
        return set()

    # Features for the (symbol, prediction_date) pairs needed by the filter.
    feats: dict[tuple[str, date], tuple] = {}
    rows = conn.execute(
        """
        SELECT symbol, trade_date, drawdown_from_90d_high, return_60d, return_5d
        FROM crypto_ml_features
        WHERE trade_date BETWEEN ? AND ?
        """,
        [min(p.prediction_date for p in predictions),
         max(p.prediction_date for p in predictions)],
    ).fetchall()
    for (sym, td, dd90, ret60, ret5) in rows:
        feats[(sym, td)] = (dd90, ret60, ret5)

    survivors = []
    for p in predictions:
        dd90, ret60, ret5 = feats.get((p.symbol, p.prediction_date), (None, None, None))
        excluded, _ = should_exclude(dd90, ret60, ret5)
        if not excluded:
            survivors.append({"coin": p.symbol, "date": p.prediction_date,
                              "probability": p.probability})
    if not survivors:
        return set()
    top = select_top_n(pd.DataFrame(survivors), top_n)
    return {(r["coin"], r["date"]) for _, r in top.iterrows()}


def _load_symbol_bars(conn, symbol, interval, win_start, win_end) -> list[dict]:
    rows = conn.execute(
        """
        SELECT open_time, open, high, low, close, volume
        FROM crypto_klines_intraday
        WHERE symbol = ? AND interval = ?
          AND open_time >= ? AND open_time <= ?
        ORDER BY open_time
        """,
        [symbol, interval, win_start, win_end],
    ).fetchall()
    # DuckDB returns naive UTC timestamps; normalise to tz-aware UTC so the
    # entry-rule anchor (tz-aware) matches.
    return [
        {"open_time": ot.replace(tzinfo=timezone.utc) if ot.tzinfo is None else ot,
         "open": o, "high": h, "low": lo, "close": c, "volume": v}
        for (ot, o, h, lo, c, v) in rows
    ]


def _load_symbol_funding(conn, symbol, win_start, win_end):
    import pandas as pd
    try:
        df = conn.execute(
            """
            SELECT funding_time, funding_rate
            FROM crypto_funding_rates
            WHERE symbol = ? AND funding_time >= ? AND funding_time <= ?
            ORDER BY funding_time
            """,
            [symbol, win_start, win_end],
        ).fetchdf()
    except Exception:
        return pd.DataFrame(columns=["funding_time", "funding_rate"])
    return df


def run_intraday_replay(
    mhde_conn,
    research_conn,
    *,
    start_date: date,
    end_date: date,
    model_like: tuple[str, str] = ("%walkfold%", "%10d%"),
    entry_rule: Optional[EntryRule] = None,
    top_n: int = 6,
    horizon_days: int = HORIZON_DAYS,
    interval: str = "1m",
) -> ReplayReport:
    """Replay every walk-forward prediction in ``[start_date, end_date]``.

    ``mhde_conn`` is the production DB opened **read-only** (predictions,
    features, prices for volume rank, funding); ``research_conn`` is the
    intraday klines research DB opened **read-only**. Returns a
    :class:`ReplayReport` with per-bin stats and the traded-subset line.
    """
    from crypto.execution.backtest.harness import compute_volume_ranks

    if entry_rule is None:
        entry_rule = DeployedEntry()

    predictions = _load_predictions(mhde_conn, start_date, end_date, model_like)
    traded_keys = _compute_traded_keys(mhde_conn, predictions, top_n)

    try:
        volume_ranks = compute_volume_ranks(mhde_conn, as_of=end_date)
    except Exception as exc:
        logger.warning("volume-rank computation failed (%s); using tier 3", exc)
        volume_ranks = {}

    results: list[TradeResult] = []
    skipped: list[tuple[str, date, str]] = []
    funding_cache: dict[str, Any] = {}

    for p in predictions:
        win_start = datetime.combine(p.prediction_date, time(0, 0), tzinfo=timezone.utc)
        win_end = win_start + timedelta(days=horizon_days + 3)
        bars = _load_symbol_bars(research_conn, p.symbol, interval, win_start, win_end)
        if not bars:
            skipped.append((p.symbol, p.prediction_date, "no_bars"))
            continue
        entry = entry_rule.resolve(p, bars)
        if entry is None:
            skipped.append((p.symbol, p.prediction_date, "no_entry_bar"))
            continue

        horizon_end = entry.entry_time + timedelta(days=horizon_days)
        walk = [b for b in bars if entry.entry_time <= b["open_time"] <= horizon_end]
        if not walk:
            skipped.append((p.symbol, p.prediction_date, "no_horizon_bars"))
            continue

        outcome = simulate_intraday_trade(entry.entry_price, walk)

        if p.symbol not in funding_cache:
            funding_cache[p.symbol] = _load_symbol_funding(
                mhde_conn, p.symbol,
                win_start.replace(tzinfo=None),
                (win_end + timedelta(days=1)).replace(tzinfo=None),
            )
        costs = compute_trade_costs(
            volume_rank=volume_ranks.get(p.symbol),
            entry_dt=entry.entry_time.replace(tzinfo=None),
            exit_dt=outcome.exit_time.replace(tzinfo=None),
            funding_rates=funding_cache[p.symbol],
        )
        net = compute_net_return(outcome.gross_return, costs)

        results.append(TradeResult(
            symbol=p.symbol, prediction_date=p.prediction_date,
            probability=p.probability, entry_time=entry.entry_time,
            entry_price=entry.entry_price, exit_time=outcome.exit_time,
            exit_price=outcome.exit_price, exit_reason=outcome.exit_reason,
            hold_minutes=outcome.hold_minutes, peak_price=outcome.peak_price,
            up1_before_dn5=outcome.up1_before_dn5, gross_return=outcome.gross_return,
            net_return=net, volume_rank=volume_ranks.get(p.symbol),
            traded=(p.symbol, p.prediction_date) in traded_keys,
        ))

    bins = aggregate_bins(results)
    traded_stats = stats_for([r for r in results if r.traded])

    return ReplayReport(
        window_start=start_date, window_end=end_date,
        entry_rule_desc=_describe_entry_rule(entry_rule),
        n_predictions=len(predictions), n_replayed=len(results),
        n_skipped=len(skipped), skipped=skipped, results=results,
        bins=bins, traded_stats=traded_stats,
    )


def _describe_entry_rule(rule: EntryRule) -> str:
    if isinstance(rule, DeployedEntry):
        return (f"DeployedEntry(prediction_date+{rule.day_offset}d @ "
                f"{rule.entry_hour:02d}:{rule.entry_minute:02d} UTC, fill=open)")
    if isinstance(rule, FixedOffsetEntry):
        return f"FixedOffsetEntry(+{rule.hours}h, day_offset={rule.day_offset})"
    return rule.__class__.__name__


# ──────────────────────────────────────────────────────────────────────
# Report rendering
# ──────────────────────────────────────────────────────────────────────


def _fmt(x: Optional[float], pct: bool = False, nd: int = 2) -> str:
    if x is None:
        return "—"
    if pct:
        return f"{x * 100:.{nd}f}%"
    return f"{x:.{nd}f}"


def _mix(d: dict[str, int]) -> str:
    if not d:
        return "—"
    return ", ".join(f"{k}:{v}" for k, v in sorted(d.items(), key=lambda kv: -kv[1]))


def render_report(report: ReplayReport, *, as_of: Optional[date] = None) -> str:
    """Render the replay report as Markdown (also printed to stdout by the CLI)."""
    as_of = as_of or datetime.now(tz=timezone.utc).date()
    lines: list[str] = []
    lines.append(f"# Intraday faithful replay — {as_of.isoformat()}")
    lines.append("")
    lines.append(f"- **Window (prediction_date):** {report.window_start} → {report.window_end}")
    lines.append(f"- **Entry rule:** {report.entry_rule_desc}")
    lines.append(f"- **Exit stack:** TrailingStopOnly(trail={TRAIL_PCT}, "
                 f"activation={ACTIVATION_PCT}) + HardFloorOverlay({HARD_FLOOR_PCT}, "
                 f"intraday arm-aware), {HORIZON_DAYS}d horizon")
    lines.append(f"- **Predictions:** {report.n_predictions} | "
                 f"**replayed:** {report.n_replayed} | **skipped:** {report.n_skipped}")
    if report.n_skipped:
        reasons: dict[str, int] = {}
        for (_, _, r) in report.skipped:
            reasons[r] = reasons.get(r, 0) + 1
        lines.append(f"  - skip reasons: {_mix(reasons)}")
    lines.append("")
    lines.append("> **Assumptions (flag):** entry anchored at prediction_date + "
                 "day_offset (KI-141: prediction_date is the features-as-of T-1 day; "
                 "live entry is the next day's 00:45 UTC export). Hard floor "
                 f"({HARD_FLOOR_PCT}) models the engine HARD_FLOOR_EXIT_PCT and is "
                 "NOT part of Phase 1B. Within-bar arm+floor ties resolve "
                 "adverse-first.")
    lines.append("")

    header = ("| prob bin | n | win% | p(up1<dn5) | avg pnl | median pnl | "
              "PF | avg hold (h) | exit mix |")
    sep = "|---|---|---|---|---|---|---|---|---|"
    lines.append(header)
    lines.append(sep)
    for b in report.bins:
        lines.append(
            f"| {b['bin']:.1f} | {b['n']} | {_fmt(b['win_rate'], pct=True)} | "
            f"{_fmt(b['p_up1_before_dn5'], pct=True)} | {_fmt(b['avg_pnl'], pct=True)} | "
            f"{_fmt(b['median_pnl'], pct=True)} | {_fmt(b['profit_factor'])} | "
            f"{_fmt(b['avg_hold_hours'])} | {_mix(b['exit_reason_mix'])} |"
        )
    lines.append("")

    t = report.traded_stats
    lines.append("### Actually-traded subset (daily top-6 post-parabolic-filter)")
    lines.append("")
    lines.append(header)
    lines.append(sep)
    lines.append(
        f"| traded | {t.get('n', 0)} | {_fmt(t.get('win_rate'), pct=True)} | "
        f"{_fmt(t.get('p_up1_before_dn5'), pct=True)} | {_fmt(t.get('avg_pnl'), pct=True)} | "
        f"{_fmt(t.get('median_pnl'), pct=True)} | {_fmt(t.get('profit_factor'))} | "
        f"{_fmt(t.get('avg_hold_hours'))} | {_mix(t.get('exit_reason_mix', {}))} |"
    )
    lines.append("")
    return "\n".join(lines)
