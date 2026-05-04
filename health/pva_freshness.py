"""PvA freshness guard — checks if prediction-vs-actual artifacts are stale relative to prices."""
from __future__ import annotations

import csv
import datetime
import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("mhde.health.pva_freshness")

PVA_CSV_DEFAULT = "data/processed/prediction_vs_actual_rows.csv"
DB_DEFAULT = "data/mhde.duckdb"


@dataclass
class PvaFreshnessResult:
    is_stale: bool
    latest_price_date: Optional[datetime.date]
    pva_max_event_date: Optional[datetime.date]
    pva_artifact_mtime: Optional[datetime.datetime]
    reason: str


def check_pva_freshness(
    db_path: str = DB_DEFAULT,
    pva_csv_path: str = PVA_CSV_DEFAULT,
) -> PvaFreshnessResult:
    """Compare latest prices_daily.trade_date against PvA artifact coverage.

    Returns PvaFreshnessResult with is_stale=True when prices are newer than
    the most recent event_date in the PvA CSV, indicating a re-run is needed.
    Returns is_stale=False when no determination can be made (no prices, no DB).
    """
    # ── Latest price date from DB ────────────────────────────────────────────
    latest_price_date: Optional[datetime.date] = None
    if os.path.exists(db_path):
        try:
            import duckdb as _duckdb
            conn = _duckdb.connect(db_path, read_only=True)
            row = conn.execute("SELECT MAX(trade_date) FROM prices_daily").fetchone()
            conn.close()
            if row and row[0]:
                latest_price_date = row[0]
                if isinstance(latest_price_date, str):
                    latest_price_date = datetime.date.fromisoformat(latest_price_date)
        except Exception as exc:
            logger.debug("pva_freshness: could not query prices_daily: %s", exc)

    if latest_price_date is None:
        return PvaFreshnessResult(
            is_stale=False,
            latest_price_date=None,
            pva_max_event_date=None,
            pva_artifact_mtime=None,
            reason="No price data available — cannot determine staleness.",
        )

    # ── PvA artifact ─────────────────────────────────────────────────────────
    if not os.path.exists(pva_csv_path):
        return PvaFreshnessResult(
            is_stale=True,
            latest_price_date=latest_price_date,
            pva_max_event_date=None,
            pva_artifact_mtime=None,
            reason=f"Prediction-vs-actual not found: {pva_csv_path}",
        )

    artifact_mtime = datetime.datetime.fromtimestamp(os.path.getmtime(pva_csv_path))

    # Find max event_date in PvA CSV
    pva_max_event_date: Optional[datetime.date] = None
    try:
        with open(pva_csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ed = row.get("event_date", "")
                if not ed:
                    continue
                try:
                    d = datetime.date.fromisoformat(ed)
                    if pva_max_event_date is None or d > pva_max_event_date:
                        pva_max_event_date = d
                except (ValueError, TypeError):
                    pass
    except Exception as exc:
        logger.debug("pva_freshness: could not read PvA CSV: %s", exc)

    if pva_max_event_date is None:
        return PvaFreshnessResult(
            is_stale=True,
            latest_price_date=latest_price_date,
            pva_max_event_date=None,
            pva_artifact_mtime=artifact_mtime,
            reason="Prediction-vs-actual CSV is empty or has no event dates.",
        )

    # ── Staleness check ──────────────────────────────────────────────────────
    if latest_price_date > pva_max_event_date:
        return PvaFreshnessResult(
            is_stale=True,
            latest_price_date=latest_price_date,
            pva_max_event_date=pva_max_event_date,
            pva_artifact_mtime=artifact_mtime,
            reason=(
                f"Prices updated to {latest_price_date} but PvA coverage only through "
                f"{pva_max_event_date}. Run: python main.py missed refresh-learning"
            ),
        )

    return PvaFreshnessResult(
        is_stale=False,
        latest_price_date=latest_price_date,
        pva_max_event_date=pva_max_event_date,
        pva_artifact_mtime=artifact_mtime,
        reason=f"PvA coverage ({pva_max_event_date}) matches latest prices ({latest_price_date}).",
    )
