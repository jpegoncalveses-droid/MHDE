from __future__ import annotations

import csv
import logging
from pathlib import Path

logger = logging.getLogger("mhde.universe.cik_validator")


def validate_cik_vs_sec(
    yaml_entries: list[dict],
    sec_map: dict[str, str],
) -> tuple[list[dict], list[dict]]:
    """Compare YAML CIKs against SEC authoritative CIKs.

    Args:
        yaml_entries: Tickers loaded from universe/sp500_tickers.yaml.
        sec_map: {ticker: zero-padded-cik} from SEC company_tickers.json.

    Returns:
        (corrected_entries, report_rows) where corrected_entries has CIK
        replaced by the SEC value whenever the ticker is found in sec_map.

    Status values:
        matched        — ticker found in SEC, CIKs agree (or YAML had no CIK)
        corrected      — ticker found in SEC, CIK differs → SEC CIK used
        missing_in_sec — ticker not in SEC → YAML CIK preserved (may be empty)
    """
    corrected: list[dict] = []
    report: list[dict] = []

    for entry in yaml_entries:
        ticker = entry.get("ticker", "").upper()
        yaml_cik = entry.get("cik") or ""
        sec_cik = sec_map.get(ticker, "")

        if sec_cik:
            chosen_cik = sec_cik
            status = "corrected" if (yaml_cik and yaml_cik != sec_cik) else "matched"
        else:
            chosen_cik = yaml_cik
            status = "missing_in_sec"

        corrected.append({**entry, "cik": chosen_cik or None})
        report.append({
            "ticker": ticker,
            "yaml_cik": yaml_cik,
            "sec_cik": sec_cik,
            "chosen_cik": chosen_cik,
            "status": status,
            "company_name": entry.get("company_name", ""),
        })

    corrections = sum(1 for r in report if r["status"] == "corrected")
    missing = sum(1 for r in report if r["status"] == "missing_in_sec")
    if corrections:
        logger.info("CIK validation: corrected %d mismatches (YAML → SEC CIK)", corrections)
    if missing:
        logger.debug("CIK validation: %d tickers not in SEC company_tickers.json", missing)

    return corrected, report


def write_validation_report(rows: list[dict], path: str | Path) -> None:
    """Write CIK validation rows to a CSV file. Creates parent dirs as needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["ticker", "yaml_cik", "sec_cik", "chosen_cik", "status", "company_name"]
    with p.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("CIK validation report: %s (%d rows)", p, len(rows))
