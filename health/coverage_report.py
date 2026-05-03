"""Generate data coverage report (MD + CSV) for MHDE active tickers."""
from __future__ import annotations
import csv
import datetime
import os

import duckdb

from health.data_freshness import compute_freshness, freshness_summary


def generate_coverage_report(db_path: str, output_dir: str) -> dict:
    """Write data_coverage_report.md and data_coverage_report.csv.

    Returns dict with keys: md, csv, summary.
    """
    os.makedirs(output_dir, exist_ok=True)
    conn = duckdb.connect(db_path, read_only=True)
    results = compute_freshness(conn)
    conn.close()

    summary = freshness_summary(results)
    total = summary["total"] or 1
    today = str(datetime.date.today())

    md_path = os.path.join(output_dir, "data_coverage_report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# MHDE Data Coverage Report\n\nGenerated: {today}\n\n")
        f.write(f"**Total active tickers:** {summary['total']}\n\n")
        f.write("| Metric | Count | % |\n|---|---|---|\n")
        for key, label in [
            ("has_prices", "Has prices"),
            ("has_fundamentals", "Has fundamentals"),
            ("has_market_cap", "Has market_cap"),
            ("fresh", "Fresh (price <= 10 days old)"),
            ("stale", "Stale (price > 10 days old)"),
            ("missing", "Missing prices entirely"),
        ]:
            count = summary[key]
            pct = count / total * 100
            f.write(f"| {label} | {count} | {pct:.1f}% |\n")

    csv_path = os.path.join(output_dir, "data_coverage_report.csv")
    fieldnames = [
        "ticker", "has_prices", "price_age_days",
        "has_fundamentals", "filing_age_days",
        "has_market_cap", "freshness_label",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow({
                "ticker": r.ticker,
                "has_prices": r.has_prices,
                "price_age_days": r.price_age_days,
                "has_fundamentals": r.has_fundamentals,
                "filing_age_days": r.filing_age_days,
                "has_market_cap": r.has_market_cap,
                "freshness_label": r.freshness_label,
            })

    return {"md": md_path, "csv": csv_path, "summary": summary}
