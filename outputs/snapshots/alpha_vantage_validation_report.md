# MHDE Source Validation Report

**Generated:** 2026-04-29  
**Ticker basket:** AAPL, NVDA, TSLA, JPM, UBER, RKLB, IWM, XLE  
**Sources tested:** 1  
**Use-case pairs:** 2

---

## Summary

| Source | Use Case | Access | Fields OK | Freshness | Score | Status |
|--------|----------|--------|-----------|-----------|-------|--------|
| alpha_vantage | transcripts | ok | Yes | 1w | 23/35 | **Useful but optional** |
| alpha_vantage | estimates | ok | Yes | 1d | 22/35 | **Useful but optional** |

---

## Alpha Vantage

### transcripts

- **Access:** ok
- **Tickers tested:** AAPL, NVDA, TSLA, JPM, UBER, RKLB
- **Required fields present:** Yes
- **Historical depth:** 2y
- **Freshness:** 1w
- **Parsing difficulty:** easy
- **Rate limit notes:** Free: 25 req/day or 5/min. Premium needed for production.
- **Fallback suggestion:** Earnings Whispers or Motley Fool for transcripts; Consensus from Bloomberg for estimates.
- **Notes:** 3 tickers with data. ETFs excluded from transcripts/estimates.

**Scores:** access=3 completeness=3 freshness=3 reliability=3 parsing_ease=4 cost=3 strategic=4 **total=23/35**

> **Final status: Useful but optional**

### estimates

- **Access:** ok
- **Tickers tested:** AAPL, NVDA, TSLA, JPM, UBER, RKLB, IWM, XLE
- **Required fields present:** Yes
- **Historical depth:** 31y
- **Freshness:** 1d
- **Parsing difficulty:** easy
- **Rate limit notes:** Free: 25 req/day or 5/min. Premium needed for production.
- **Fallback suggestion:** Earnings Whispers or Motley Fool for transcripts; Consensus from Bloomberg for estimates.
- **Notes:** 6 tickers with data. ETFs excluded from transcripts/estimates.

**Scores:** access=3 completeness=3 freshness=3 reliability=3 parsing_ease=4 cost=3 strategic=3 **total=22/35**

> **Final status: Useful but optional**
