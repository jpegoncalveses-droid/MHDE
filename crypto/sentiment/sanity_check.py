"""Sanity check for Phase 3 Week 1 sentiment ingestion artifacts.

Reports:
  - F&G row count + value distribution (min/max/mean)
  - F&G gap days (missing dates between min and max)
  - Funding universe size
  - Funding aggregate row count
  - Aggregate spot-check: at least one day computed

Exits 0 if clean against thresholds, non-zero with summary otherwise.

Per docs/design/2026-05-16-phase3-amendment-regime-filter.md §"Week 1".
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

import duckdb

from crypto.schema import create_all_tables

logger = logging.getLogger("mhde.crypto.sentiment.sanity_check")

FNG_VALUE_MIN = 0
FNG_VALUE_MAX = 100


@dataclass
class Thresholds:
    min_fng_rows: int = 365            # at least 1 year of F&G history
    min_universe_size: int = 20         # 20 perps per amendment
    min_aggregate_rows: int = 365       # at least 1 year of aggregated funding


@dataclass
class SanityReport:
    fng_count: int = 0
    fng_min: int | None = None
    fng_max: int | None = None
    fng_mean: float | None = None
    fng_gaps: list = field(default_factory=list)
    universe_size: int = 0
    aggregate_count: int = 0
    thresholds: Thresholds = field(default_factory=Thresholds)

    @classmethod
    def collect(cls, conn, thresholds: Thresholds) -> "SanityReport":
        dist = fng_value_distribution(conn)
        return cls(
            fng_count=fng_row_count(conn),
            fng_min=dist.get("min"),
            fng_max=dist.get("max"),
            fng_mean=dist.get("mean"),
            fng_gaps=fng_gap_days(conn),
            universe_size=funding_universe_size(conn),
            aggregate_count=funding_aggregate_row_count(conn),
            thresholds=thresholds,
        )


def fng_row_count(conn: duckdb.DuckDBPyConnection) -> int:
    return conn.execute("SELECT COUNT(*) FROM sentiment_fear_greed").fetchone()[0]


def fng_value_distribution(conn: duckdb.DuckDBPyConnection) -> dict:
    row = conn.execute(
        "SELECT MIN(value), MAX(value), AVG(value) FROM sentiment_fear_greed"
    ).fetchone()
    if row is None or row[0] is None:
        return {"min": None, "max": None, "mean": None}
    return {"min": int(row[0]), "max": int(row[1]), "mean": float(row[2])}


def fng_gap_days(conn: duckdb.DuckDBPyConnection) -> list[date]:
    rows = conn.execute(
        "SELECT date FROM sentiment_fear_greed ORDER BY date"
    ).fetchall()
    if not rows:
        return []
    present = {(r[0].date() if hasattr(r[0], "date") else r[0]) for r in rows}
    start = min(present)
    end = max(present)
    gaps: list[date] = []
    d = start
    while d <= end:
        if d not in present:
            gaps.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return gaps


def funding_universe_size(conn: duckdb.DuckDBPyConnection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM sentiment_funding_universe"
    ).fetchone()[0]


def funding_aggregate_row_count(conn: duckdb.DuckDBPyConnection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM sentiment_funding_aggregate"
    ).fetchone()[0]


def is_clean(report: SanityReport) -> bool:
    t = report.thresholds
    if report.fng_count < t.min_fng_rows:
        return False
    if report.universe_size < t.min_universe_size:
        return False
    if report.aggregate_count < t.min_aggregate_rows:
        return False
    if report.fng_min is not None and report.fng_min < FNG_VALUE_MIN:
        return False
    if report.fng_max is not None and report.fng_max > FNG_VALUE_MAX:
        return False
    return True


def format_report(report: SanityReport) -> str:
    lines: list[str] = []
    lines.append("=== Phase 3 Week 1 sentiment sanity report ===")
    lines.append(f"F&G rows:            {report.fng_count:>6}  "
                 f"(min {report.thresholds.min_fng_rows})")
    if report.fng_count:
        lines.append(f"F&G value range:     [{report.fng_min}, {report.fng_max}]  "
                     f"mean={report.fng_mean:.2f}")
        lines.append(f"F&G gap days:        {len(report.fng_gaps)}")
        if report.fng_gaps[:5]:
            lines.append(f"  (first 5: {', '.join(str(d) for d in report.fng_gaps[:5])})")
    lines.append(f"Funding universe:    {report.universe_size:>6}  "
                 f"(min {report.thresholds.min_universe_size})")
    lines.append(f"Funding aggregate:   {report.aggregate_count:>6}  "
                 f"(min {report.thresholds.min_aggregate_rows})")
    lines.append("")
    if is_clean(report):
        lines.append("CLEAN.")
    else:
        lines.append("ISSUES FOUND.")
    return "\n".join(lines) + "\n"


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m crypto.sentiment.sanity_check")
    parser.add_argument("--db", required=True)
    parser.add_argument("--min-fng-rows", type=int, default=365)
    parser.add_argument("--min-universe-size", type=int, default=20)
    parser.add_argument("--min-aggregate-rows", type=int, default=365)
    args = parser.parse_args(argv)
    _setup_logging()

    from storage.db import get_connection
    from storage.migrations import run_migrations

    conn = get_connection(args.db)
    run_migrations(conn)
    create_all_tables(conn)
    thresholds = Thresholds(
        min_fng_rows=args.min_fng_rows,
        min_universe_size=args.min_universe_size,
        min_aggregate_rows=args.min_aggregate_rows,
    )
    report = SanityReport.collect(conn, thresholds)
    print(format_report(report))
    return 0 if is_clean(report) else 1


if __name__ == "__main__":
    sys.exit(main())
