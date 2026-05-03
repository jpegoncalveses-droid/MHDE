from __future__ import annotations

import logging
from datetime import date

import duckdb

from features.industry_utils import detect_industry
from features.period_utils import check_period_alignment

logger = logging.getLogger("mhde.features.quality")

_STALE_FUNDAMENTALS_DAYS = 180


def _fundamentals_age_days(conn, ticker: str, as_of: date) -> int | None:
    row = conn.execute(
        "SELECT MAX(as_of_date) FROM fundamentals_raw WHERE ticker = ? AND as_of_date IS NOT NULL",
        [ticker],
    ).fetchone()
    if not row or not row[0]:
        return None
    return (as_of - row[0]).days


def _apply_staleness_annotations(features: list[dict], age_days: int | None) -> list[dict]:
    if age_days is None or age_days <= _STALE_FUNDAMENTALS_DAYS:
        return features
    for f in features:
        f["confidence"] = "low"
        existing = f.get("metadata") or {}
        f["metadata"] = {**existing, "stale_fundamentals_days": age_days}
    return features


def compute_quality(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    ticker: str,
    as_of: date,
) -> list[dict]:
    features = []

    industry = detect_industry(conn, ticker)
    is_bank = industry["is_bank"]
    is_insurer = industry["is_insurer"]

    # Net income positive/negative
    ni_rows = conn.execute(
        """
        SELECT value, as_of_date FROM fundamentals_raw
        WHERE ticker = ? AND concept LIKE '%NetIncomeLoss%'
        ORDER BY as_of_date DESC LIMIT 2
        """,
        [ticker],
    ).fetchall()

    if ni_rows:
        latest_ni = ni_rows[0][0]
        ni_positive = latest_ni is not None and latest_ni > 0
        score = 70.0 if ni_positive else 30.0
        features.append({
            "feature_group": "quality",
            "feature_name": "net_income_positive",
            "feature_value": float(latest_ni) if latest_ni is not None else None,
            "feature_score": score,
            "confidence": "high",
            "source": "sec_edgar",
        })
    else:
        features.append({
            "feature_group": "quality",
            "feature_name": "net_income_positive",
            "feature_value": None,
            "feature_score": None,
            "confidence": "low",
            "source": "sec_edgar",
        })

    # Revenue growth YoY — requires same concept and aligned periods
    rev_rows = conn.execute(
        """
        SELECT concept, value, as_of_date, form FROM fundamentals_raw
        WHERE ticker = ? AND concept LIKE '%Revenues%'
        ORDER BY as_of_date DESC LIMIT 2
        """,
        [ticker],
    ).fetchall()

    same_concept = (
        len(rev_rows) >= 2
        and rev_rows[0][0] == rev_rows[1][0]
        and rev_rows[1][1] and rev_rows[1][1] != 0
    )
    if same_concept:
        alignment = check_period_alignment(
            rev_rows[0][2], rev_rows[0][3],
            rev_rows[1][2], rev_rows[1][3],
        )
        period_aligned = alignment["period_alignment_status"] == "aligned"
    else:
        alignment = None
        period_aligned = False

    rev_growth_ok = same_concept and period_aligned

    if same_concept and not period_aligned:
        features.append({
            "feature_group": "quality",
            "feature_name": "revenue_growth_yoy",
            "feature_value": None,
            "feature_score": None,
            "confidence": "low",
            "source": "sec_edgar",
            "metadata": {"missing_reason": "period_mismatch", **(alignment or {})},
        })
    elif rev_growth_ok:
        growth = (rev_rows[0][1] - rev_rows[1][1]) / abs(rev_rows[1][1]) * 100
        # Sanity: > 500% growth is almost certainly a concept mismatch or restatement artifact
        if abs(growth) > 500:
            features.append({
                "feature_group": "quality",
                "feature_name": "revenue_growth_yoy",
                "feature_value": None,
                "feature_score": None,
                "confidence": "low",
                "source": "sec_edgar",
                "metadata": {"missing_reason": "valuation_denominator_invalid"},
            })
        else:
            # Score: > 20% growth → 90, 10-20% → 75, 0-10% → 60, negative → 30
            if growth > 20:
                score = 90.0
            elif growth > 10:
                score = 75.0
            elif growth >= 0:
                score = 60.0
            else:
                score = 30.0
            features.append({
                "feature_group": "quality",
                "feature_name": "revenue_growth_yoy",
                "feature_value": round(growth, 2),
                "feature_score": score,
                "confidence": "low" if is_bank else "medium",
                "source": "sec_edgar",
                "metadata": alignment,
            })
    else:
        features.append({
            "feature_group": "quality",
            "feature_name": "revenue_growth_yoy",
            "feature_value": None,
            "feature_score": None,
            "confidence": "low",
            "source": "sec_edgar",
        })

    # Net margin
    ni_val = ni_rows[0][0] if ni_rows else None
    rev_val = rev_rows[0][1] if rev_rows else None
    if ni_val is not None and rev_val and rev_val != 0:
        # NI > revenue is financially impossible — mismatched XBRL concepts
        if ni_val > 0 and rev_val > 0 and ni_val > rev_val:
            features.append({
                "feature_group": "quality",
                "feature_name": "net_margin",
                "feature_value": None,
                "feature_score": None,
                "confidence": "low",
                "source": "sec_edgar",
                "metadata": {"missing_reason": "financial_concept_mismatch"},
            })
        else:
            margin = ni_val / rev_val * 100
            if margin > 20:
                score = 90.0
            elif margin > 10:
                score = 75.0
            elif margin > 0:
                score = 55.0
            else:
                score = 20.0
            if is_bank:
                quality_warning = "bank_specific_quality_required"
            elif is_insurer:
                quality_warning = "insurance_specific_quality_required"
            else:
                quality_warning = None
            meta = {"quality_warning": quality_warning} if quality_warning else None
            features.append({
                "feature_group": "quality",
                "feature_name": "net_margin",
                "feature_value": round(margin, 2),
                "feature_score": score,
                "confidence": "low" if (is_bank or is_insurer) else "medium",
                "source": "sec_edgar",
                "metadata": meta,
            })
    else:
        features.append({
            "feature_group": "quality",
            "feature_name": "net_margin",
            "feature_value": None,
            "feature_score": None,
            "confidence": "low",
            "source": "sec_edgar",
        })

    # Dilution proxy (shares change)
    shares_rows = conn.execute(
        """
        SELECT value FROM fundamentals_raw
        WHERE ticker = ? AND concept LIKE '%CommonStockSharesOutstanding%'
        ORDER BY as_of_date DESC LIMIT 2
        """,
        [ticker],
    ).fetchall()

    if len(shares_rows) >= 2 and shares_rows[1][0] and shares_rows[1][0] > 0:
        dilution_pct = (shares_rows[0][0] - shares_rows[1][0]) / shares_rows[1][0] * 100
        # Low dilution is good: < 2% → 85, 2-5% → 65, > 5% → 30
        if dilution_pct < 2:
            score = 85.0
        elif dilution_pct < 5:
            score = 65.0
        else:
            score = 30.0
        features.append({
            "feature_group": "quality",
            "feature_name": "dilution_rate",
            "feature_value": round(dilution_pct, 2),
            "feature_score": score,
            "confidence": "medium",
            "source": "sec_edgar",
        })
    else:
        features.append({
            "feature_group": "quality",
            "feature_name": "dilution_rate",
            "feature_value": None,
            "feature_score": None,
            "confidence": "low",
            "source": "sec_edgar",
        })

    age = _fundamentals_age_days(conn, ticker, as_of)
    return _apply_staleness_annotations(features, age)
