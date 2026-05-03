from __future__ import annotations

import logging
from datetime import date

import duckdb

from features.filer_utils import is_foreign_filer, get_reporting_currency
from features.industry_utils import detect_industry, bank_has_total_revenue

logger = logging.getLogger("mhde.features.valuation")

# Revenue concept priority: fetch the most-specific primary revenue concept first.
_REVENUE_CONCEPTS = [
    "us-gaap/Revenues",
    "us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap/SalesRevenueNet",
    "us-gaap/RevenueFromContractWithCustomerIncludingAssessedTax",
    "us-gaap/SalesRevenueGoodsNet",
    "us-gaap/SalesRevenueServicesNet",
]

# Shares concept priority: diluted weighted avg > basic weighted avg > outstanding issued
_SHARES_CONCEPTS = [
    "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding",
    "us-gaap/WeightedAverageNumberOfSharesOutstandingBasic",
    "us-gaap/CommonStockSharesOutstanding",
    "us-gaap/CommonStockSharesIssued",
]

# Sanity bounds — ratios outside these ranges indicate bad input data.
_MIN_SHARES = 1_000_000   # < 1M shares → XBRL denominator error
_PS_BOUNDS = (0.05, 100.0)
_PE_BOUNDS = (0.0, 150.0)   # exclusive lower (pe > 0 already guarded)
_PB_BOUNDS = (0.0, 50.0)    # exclusive lower (equity > 0 already guarded)

_INVALID_META = {"missing_reason": "valuation_denominator_invalid"}


def _ratio_valid(value: float, lo: float, hi: float) -> bool:
    return lo <= value <= hi


def _clamp(v: float | None) -> float | None:
    if v is None:
        return None
    return max(0.0, min(100.0, v))


def _latest(conn: duckdb.DuckDBPyConnection, ticker: str, concepts: list[str]) -> float | None:
    """Return the most-recent non-null value for the first matching concept."""
    placeholders = ",".join(["?"] * len(concepts))
    row = conn.execute(
        f"""
        SELECT value FROM fundamentals_raw
        WHERE ticker = ? AND concept IN ({placeholders}) AND value IS NOT NULL
        ORDER BY
            CASE concept {' '.join(f"WHEN ? THEN {i}" for i, _ in enumerate(concepts))} END,
            as_of_date DESC
        LIMIT 1
        """,
        [ticker] + concepts + concepts,
    ).fetchone()
    if row:
        return row[0]
    return None


def _ps_score(ps: float) -> float:
    """Discretised P/S score: lower P/S = more attractive."""
    if ps < 1:
        return 90.0
    if ps < 3:
        return 70.0
    if ps < 10:
        return 50.0
    return 20.0


def _pe_score(pe: float) -> float:
    """Discretised P/E score: lower P/E = more attractive (only positive earnings)."""
    if pe < 10:
        return 90.0
    if pe < 20:
        return 75.0
    if pe < 30:
        return 55.0
    if pe < 50:
        return 35.0
    return 15.0


def compute_valuation(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    ticker: str,
    as_of: date,
) -> list[dict]:
    features = []

    # Latest price and history depth from prices_daily
    price_row = conn.execute(
        "SELECT close FROM prices_daily WHERE ticker = ? ORDER BY trade_date DESC LIMIT 1",
        [ticker],
    ).fetchone()
    price = price_row[0] if price_row else None

    # 52w-high requires enough history to be meaningful.
    # With only 1-3 days from Stooq, max(high) ≈ today's high, making the
    # score misleadingly low. Require ≥ 20 trading days before computing.
    history_count = conn.execute(
        "SELECT COUNT(*) FROM prices_daily WHERE ticker = ?", [ticker]
    ).fetchone()[0]
    sufficient_history = history_count >= 20

    high_row = conn.execute(
        """
        SELECT MAX(high) FROM prices_daily
        WHERE ticker = ? AND trade_date >= CAST(? AS DATE) - INTERVAL '52 weeks'
        """,
        [ticker, as_of],
    ).fetchone()
    week52_high = high_row[0] if high_row and high_row[0] else None

    # ── price vs 52-week high ──────────────────────────────────────────────────
    if price and week52_high and week52_high > 0 and sufficient_history:
        pct_from_high = (price / week52_high) * 100
        features.append({
            "feature_group": "valuation",
            "feature_name": "price_vs_52w_high",
            "feature_value": round(pct_from_high, 4),
            "feature_score": _clamp(100 - pct_from_high),
            "confidence": "high",
            "source": "polygon",
        })
    else:
        features.append({
            "feature_group": "valuation",
            "feature_name": "price_vs_52w_high",
            "feature_value": None,
            "feature_score": None,
            "confidence": "low",
            "source": "polygon",
        })

    # ── Foreign filer guard ────────────────────────────────────────────────────
    # If this ticker files 20-F/6-K/40-F with a non-USD reporting currency,
    # all USD-denominated valuation ratios are meaningless (CNY/USD mismatch).
    _ff_null_reason: str | None = None
    if is_foreign_filer(conn, ticker):
        currency = get_reporting_currency(conn, ticker, _REVENUE_CONCEPTS)
        if currency is None:
            _ff_null_reason = "foreign_filer_currency_unknown"
        elif currency != "USD":
            _ff_null_reason = "foreign_currency_not_normalized"
        # else: USD-reporting foreign filer — proceed normally

    if _ff_null_reason is not None:
        logger.debug("%s: foreign filer guard — %s", ticker, _ff_null_reason)
        for name in ("ps_proxy", "pe_ratio", "pb_ratio"):
            features.append({
                "feature_group": "valuation",
                "feature_name": name,
                "feature_value": None,
                "feature_score": None,
                "confidence": "low",
                "source": "sec_edgar+polygon",
                "metadata": {"missing_reason": _ff_null_reason},
            })
        return features

    # ── Industry detection + shared fundamentals fetch ────────────────────────
    industry = detect_industry(conn, ticker)
    revenue = _latest(conn, ticker, _REVENUE_CONCEPTS)
    shares = _latest(conn, ticker, _SHARES_CONCEPTS)
    shares_valid = shares and shares >= _MIN_SHARES

    # ── P/S proxy: (price × shares) / revenue ─────────────────────────────────
    # Bank guard: fee-income-only concepts are not total bank revenue for P/S.
    if industry["is_bank"] and not bank_has_total_revenue(conn, ticker):
        logger.debug("%s: bank P/S guard — bank_revenue_concept_missing", ticker)
        features.append({
            "feature_group": "valuation",
            "feature_name": "ps_proxy",
            "feature_value": None,
            "feature_score": None,
            "confidence": "low",
            "source": "sec_edgar+polygon",
            "metadata": {"missing_reason": "bank_revenue_concept_missing"},
        })
    elif price and revenue and revenue > 0 and shares_valid:
        market_cap = price * shares
        ps = market_cap / revenue
        if _ratio_valid(ps, *_PS_BOUNDS):
            features.append({
                "feature_group": "valuation",
                "feature_name": "ps_proxy",
                "feature_value": round(ps, 4),
                "feature_score": _ps_score(ps),
                "confidence": "medium",
                "source": "sec_edgar+polygon",
            })
        else:
            logger.debug("%s: P/S=%.4f outside bounds %s — nulled", ticker, ps, _PS_BOUNDS)
            features.append({
                "feature_group": "valuation",
                "feature_name": "ps_proxy",
                "feature_value": None,
                "feature_score": None,
                "confidence": "low",
                "source": "sec_edgar+polygon",
                "metadata": _INVALID_META,
            })
    else:
        features.append({
            "feature_group": "valuation",
            "feature_name": "ps_proxy",
            "feature_value": None,
            "feature_score": None,
            "confidence": "low",
            "source": "sec_edgar+polygon",
            "metadata": _INVALID_META if (shares is not None and not shares_valid) else None,
        })

    # ── P/E: price / EPS (diluted) ────────────────────────────────────────────
    eps_row = conn.execute(
        """
        SELECT value FROM fundamentals_raw
        WHERE ticker = ?
          AND concept IN ('us-gaap/EarningsPerShareDiluted', 'us-gaap/EarningsPerShareBasic')
          AND value IS NOT NULL
        ORDER BY
            CASE concept
                WHEN 'us-gaap/EarningsPerShareDiluted' THEN 0
                WHEN 'us-gaap/EarningsPerShareBasic' THEN 1
            END,
            as_of_date DESC
        LIMIT 1
        """,
        [ticker],
    ).fetchone()
    eps = eps_row[0] if eps_row else None

    if price and eps and eps > 0:
        pe = price / eps
        if _ratio_valid(pe, _PE_BOUNDS[0], _PE_BOUNDS[1]):
            features.append({
                "feature_group": "valuation",
                "feature_name": "pe_ratio",
                "feature_value": round(pe, 4),
                "feature_score": _pe_score(pe),
                "confidence": "medium",
                "source": "sec_edgar+polygon",
            })
        else:
            logger.debug("%s: P/E=%.4f outside bounds %s — nulled", ticker, pe, _PE_BOUNDS)
            features.append({
                "feature_group": "valuation",
                "feature_name": "pe_ratio",
                "feature_value": None,
                "feature_score": None,
                "confidence": "low",
                "source": "sec_edgar+polygon",
                "metadata": _INVALID_META,
            })
    else:
        features.append({
            "feature_group": "valuation",
            "feature_name": "pe_ratio",
            "feature_value": None,
            "feature_score": None,
            "confidence": "low",
            "source": "sec_edgar+polygon",
        })

    # ── P/B: price / book_value_per_share ─────────────────────────────────────
    equity_row = conn.execute(
        """
        SELECT value FROM fundamentals_raw
        WHERE ticker = ?
          AND concept IN ('us-gaap/StockholdersEquity', 'us-gaap/LiabilitiesAndStockholdersEquity')
          AND value IS NOT NULL
        ORDER BY
            CASE concept
                WHEN 'us-gaap/StockholdersEquity' THEN 0
                ELSE 1
            END,
            as_of_date DESC
        LIMIT 1
        """,
        [ticker],
    ).fetchone()
    equity = equity_row[0] if equity_row else None

    if price and equity and equity > 0 and shares_valid:
        bvps = equity / shares
        pb = price / bvps if bvps > 0 else None
        if pb is not None and _ratio_valid(pb, _PB_BOUNDS[0], _PB_BOUNDS[1]):
            # P/B < 1 → likely value/distress, > 5 → expensive
            if pb < 1:
                pb_score = 80.0
            elif pb < 2:
                pb_score = 65.0
            elif pb < 5:
                pb_score = 45.0
            else:
                pb_score = 20.0
            features.append({
                "feature_group": "valuation",
                "feature_name": "pb_ratio",
                "feature_value": round(pb, 4),
                "feature_score": pb_score,
                "confidence": "medium",
                "source": "sec_edgar+polygon",
            })
        else:
            if pb is not None:
                logger.debug("%s: P/B=%.4f outside bounds %s — nulled", ticker, pb, _PB_BOUNDS)
            features.append({
                "feature_group": "valuation",
                "feature_name": "pb_ratio",
                "feature_value": None,
                "feature_score": None,
                "confidence": "low",
                "source": "sec_edgar+polygon",
                "metadata": _INVALID_META,
            })
    else:
        features.append({
            "feature_group": "valuation",
            "feature_name": "pb_ratio",
            "feature_value": None,
            "feature_score": None,
            "confidence": "low",
            "source": "sec_edgar+polygon",
            "metadata": _INVALID_META if (shares is not None and not shares_valid) else None,
        })

    return features
