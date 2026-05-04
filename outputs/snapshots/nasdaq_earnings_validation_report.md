# MHDE Source Validation Report

**Generated:** 2026-04-29  
**Ticker basket:** AAPL, NVDA, TSLA, JPM, UBER, RKLB, IWM, XLE  
**Sources tested:** 1  
**Use-case pairs:** 1

---

## Summary

| Source | Use Case | Access | Fields OK | Freshness | Score | Status |
|--------|----------|--------|-----------|-----------|-------|--------|
| nasdaq_earnings | earnings_calendar | ok | No (no_data) | 1d | 23/35 | **Useful but optional** |

---

## Nasdaq Earnings

### earnings_calendar

- **Access:** ok
- **Tickers tested:** AAPL, NVDA, TSLA, JPM, UBER, RKLB, IWM, XLE
- **Required fields present:** No
- **Missing fields:** no_data
- **Historical depth:** N/A
- **Freshness:** 1d
- **Parsing difficulty:** easy
- **Rate limit notes:** Unofficial API. No auth but may block scrapers.
- **Fallback suggestion:** Earnings Whispers API or Yahoo Finance earnings calendar.
- **Notes:** PLANNING ONLY — do not use as truth source. 0/8 tickers found.

**Scores:** access=3 completeness=1 freshness=4 reliability=3 parsing_ease=4 cost=5 strategic=3 **total=23/35**

> **Final status: Useful but optional**
