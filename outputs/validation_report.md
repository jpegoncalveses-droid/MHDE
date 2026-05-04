# MHDE Source Validation Report

**Generated:** 2026-04-30  
**Ticker basket:** AAPL, NVDA, TSLA, JPM, UBER, RKLB, IWM, XLE  
**Sources tested:** 1  
**Use-case pairs:** 2

---

## Summary

| Source | Use Case | Access | Fields OK | Freshness | Score | Status |
|--------|----------|--------|-----------|-----------|-------|--------|
| cftc | index_positioning | ok | Yes | weekly_current | 27/35 | **Useful but optional** |
| cftc | commodity_macro_positioning | ok | Yes | weekly_current | 27/35 | **Useful but optional** |

---

## Cftc

### index_positioning

- **Access:** ok
- **Tickers tested:** es_sp500, nq_nasdaq100, rty_russell2000, ust_10y
- **Required fields present:** Yes
- **Historical depth:** 5w
- **Freshness:** weekly_current
- **Parsing difficulty:** easy
- **Rate limit notes:** No auth required. CFTC Socrata API. No published rate limit; 0.5s courtesy delay.
- **Fallback suggestion:** Direct CFTC CSV file download as alternative ingestion path.
- **Notes:** Coverage: 3/3 core markets found. Index/commodity-futures-only; weekly cadence with ~3-day lag. Not per-stock data.

**Scores:** access=4 completeness=4 freshness=3 reliability=4 parsing_ease=5 cost=5 strategic=2 **total=27/35**

> **Final status: Useful but optional**

### commodity_macro_positioning

- **Access:** ok
- **Tickers tested:** wti_crude, gold
- **Required fields present:** Yes
- **Historical depth:** 2w
- **Freshness:** weekly_current
- **Parsing difficulty:** easy
- **Rate limit notes:** No auth required. CFTC Socrata API. No published rate limit; 0.5s courtesy delay.
- **Fallback suggestion:** Direct CFTC CSV file download as alternative ingestion path.
- **Notes:** Coverage: 2/2 core markets found. Index/commodity-futures-only; weekly cadence with ~3-day lag. Not per-stock data.

**Scores:** access=4 completeness=4 freshness=3 reliability=4 parsing_ease=5 cost=5 strategic=2 **total=27/35**

> **Final status: Useful but optional**
