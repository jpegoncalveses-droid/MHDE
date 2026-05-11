"""OHLCV plausibility / volume-cliff guard.

Catches the failure mode where a day's ingested candles look implausibly
different from the rolling baseline — the 2026-05-07 partial-candle bug,
where the ingestion wrote ~30-minute candles (≈2.5 % of normal volume)
for all 50 symbols and the existing monitors, which only check row
*presence*, said nothing. This module checks row *plausibility*.

``check_ohlcv_plausibility(conn, target_date)`` is pure — it reads
``crypto_prices_daily`` and returns a :class:`QualityReport`; it writes
nothing, sends no alerts, and never exits. Persistence
(``persist_report``) and alerting / pipeline-blocking live in the CLI
wrapper (``crypto check-data-quality``).

Thresholds: ``crypto/config.py`` (tuned on a 90-day clean-data scan; see
DECISIONS.md). The systemic flag — the one that blocks the pipeline —
had zero false positives at every threshold combo on clean data, while
the 2026-05-07 corruption tripped ≈80–96 % of the universe.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import duckdb

from crypto.config import (
    OHLCV_PLAUSIBILITY_WINDOW_DAYS,
    RANGE_COLLAPSE_RATIO,
    SYSTEMIC_FLAG_RATIO,
    SYSTEMIC_MIN_SYMBOLS,
    TRADE_COUNT_CLIFF_RATIO,
    VOLUME_CLIFF_RATIO,
)

logger = logging.getLogger("mhde.data_quality_guard")

#: sentinel symbol / check_name for the systemic summary row.
SYSTEMIC_SYMBOL = "__systemic__"
SYSTEMIC_CHECK_NAME = "systemic_corruption"


@dataclass
class SymbolFlag:
    """One tripped per-symbol check."""

    symbol: str
    check_name: str   # 'volume_cliff' | 'range_collapse' | 'trade_count_cliff'
    expected: float   # trailing-N-day median of the metric
    observed: float   # the metric's value on the target date
    ratio: float      # observed / expected


@dataclass
class QualityReport:
    target_date: date
    n_symbols_on_date: int             # symbols with a row on target_date
    n_evaluated: int                   # ... that also have a full N-day prior window
    n_flagged: int                     # ... that tripped >= 1 per-symbol check
    systemic_ratio: float              # n_flagged / n_evaluated (0.0 if n_evaluated == 0)
    is_systemic: bool
    per_symbol_flags: list[SymbolFlag] = field(default_factory=list)

    @property
    def severity(self) -> str:
        if self.is_systemic:
            return "critical"
        if self.n_flagged > 0:
            return "warn"
        return "ok"

    def to_rows(self) -> list[dict]:
        """Rows to UPSERT into ``crypto_data_quality_reports`` — every
        flagged per-symbol check, plus a summary row when systemic. A
        clean report yields ``[]`` (the table is an exceptions log)."""
        rows: list[dict] = [
            {"date": self.target_date, "symbol": f.symbol, "check_name": f.check_name,
             "expected": f.expected, "observed": f.observed, "flagged": True,
             "severity": "warn"}
            for f in self.per_symbol_flags
        ]
        if self.is_systemic:
            rows.append({
                "date": self.target_date, "symbol": SYSTEMIC_SYMBOL,
                "check_name": SYSTEMIC_CHECK_NAME,
                "expected": float(SYSTEMIC_FLAG_RATIO),
                "observed": float(self.systemic_ratio),
                "flagged": True, "severity": "critical",
            })
        return rows


def _median(values: list) -> Optional[float]:
    xs = sorted(
        float(v) for v in values
        if v is not None and not (isinstance(v, float) and math.isnan(v))
    )
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def check_ohlcv_plausibility(
    conn: duckdb.DuckDBPyConnection,
    target_date: date,
    *,
    window: int = OHLCV_PLAUSIBILITY_WINDOW_DAYS,
    volume_cliff_ratio: float = VOLUME_CLIFF_RATIO,
    range_collapse_ratio: float = RANGE_COLLAPSE_RATIO,
    trade_count_cliff_ratio: float = TRADE_COUNT_CLIFF_RATIO,
    systemic_ratio: float = SYSTEMIC_FLAG_RATIO,
    systemic_min_symbols: int = SYSTEMIC_MIN_SYMBOLS,
) -> QualityReport:
    """Evaluate every symbol with a ``crypto_prices_daily`` row on
    ``target_date`` against its trailing-``window``-day median (strictly
    prior days). A symbol is *flagged* if today's volume / (high−low)
    range / trade count is below the corresponding ratio threshold. The
    report is *systemic* iff at least ``systemic_min_symbols`` symbols
    are evaluable AND more than ``systemic_ratio`` of them are flagged.

    Fail-open: a symbol without a full prior window (warmup) is not
    evaluated and not flagged; an empty target date yields a clean,
    ``ok``-severity report. Pure — reads only.
    """
    lookback_start = target_date - timedelta(days=window * 3 + 7)  # cushion for calendar gaps
    rows = conn.execute(
        "SELECT symbol, trade_date, high, low, volume, trades FROM crypto_prices_daily "
        "WHERE trade_date >= ? AND trade_date <= ? ORDER BY symbol, trade_date",
        [lookback_start, target_date],
    ).fetchall()

    by_symbol: dict[str, list] = {}
    for sym, td, hi, lo, vol, trd in rows:
        d = td.date() if hasattr(td, "date") and not isinstance(td, date) else td
        by_symbol.setdefault(sym, []).append((d, hi, lo, vol, trd))

    flags: list[SymbolFlag] = []
    n_on_date = n_eval = n_flagged = 0
    for sym, recs in by_symbol.items():
        recs.sort(key=lambda r: r[0])
        target_rec = next((r for r in recs if r[0] == target_date), None)
        if target_rec is None:
            continue
        n_on_date += 1
        prior = [r for r in recs if r[0] < target_date][-window:]
        if len(prior) < window:
            continue  # warmup window — not evaluated, not flagged (fail open)
        n_eval += 1
        _, hi, lo, vol, trd = target_rec
        med_vol = _median([r[3] for r in prior])
        med_rng = _median([(r[1] - r[2]) for r in prior if r[1] is not None and r[2] is not None])
        med_trd = _median([r[4] for r in prior])
        sym_flagged = False
        if med_vol and vol is not None and med_vol > 0 and (vol / med_vol) < volume_cliff_ratio:
            flags.append(SymbolFlag(sym, "volume_cliff", float(med_vol), float(vol), vol / med_vol))
            sym_flagged = True
        rng = (hi - lo) if (hi is not None and lo is not None) else None
        if med_rng and rng is not None and med_rng > 0 and (rng / med_rng) < range_collapse_ratio:
            flags.append(SymbolFlag(sym, "range_collapse", float(med_rng), float(rng), rng / med_rng))
            sym_flagged = True
        if med_trd and trd is not None and med_trd > 0 and (float(trd) / med_trd) < trade_count_cliff_ratio:
            flags.append(SymbolFlag(sym, "trade_count_cliff", float(med_trd), float(trd), float(trd) / med_trd))
            sym_flagged = True
        if sym_flagged:
            n_flagged += 1

    ratio = (n_flagged / n_eval) if n_eval else 0.0
    is_systemic = (n_eval >= systemic_min_symbols) and (ratio > systemic_ratio)
    logger.info(
        "ohlcv plausibility %s: on_date=%d evaluated=%d flagged=%d ratio=%.2f systemic=%s",
        target_date, n_on_date, n_eval, n_flagged, ratio, is_systemic,
    )
    return QualityReport(
        target_date=target_date, n_symbols_on_date=n_on_date, n_evaluated=n_eval,
        n_flagged=n_flagged, systemic_ratio=ratio, is_systemic=is_systemic,
        per_symbol_flags=flags,
    )


def persist_report(conn: duckdb.DuckDBPyConnection, report: QualityReport) -> int:
    """UPSERT the report's flagged rows into ``crypto_data_quality_reports``;
    returns the number of rows written. A clean report writes nothing."""
    from crypto.schema import SCHEMA_CRYPTO_DATA_QUALITY_REPORTS
    conn.execute(SCHEMA_CRYPTO_DATA_QUALITY_REPORTS)
    out = report.to_rows()
    for r in out:
        conn.execute(
            """
            INSERT INTO crypto_data_quality_reports
                (date, symbol, check_name, expected, observed, flagged, severity)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (date, symbol, check_name) DO UPDATE SET
                expected = excluded.expected,
                observed = excluded.observed,
                flagged  = excluded.flagged,
                severity = excluded.severity
            """,
            [r["date"], r["symbol"], r["check_name"], r["expected"],
             r["observed"], r["flagged"], r["severity"]],
        )
    return len(out)
