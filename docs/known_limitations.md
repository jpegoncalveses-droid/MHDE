# MHDE Known Limitations — v1

## Universe Selection

- Universe is name-filtered only. Excludes ETFs, funds, trusts, and non-equity names by keyword.
- No market cap, liquidity, price, or volume filters at construction time.
- Includes micro-caps and illiquid names. Use candidates as research leads, not actionable signals.
- SEC company_tickers.json is the data source. No exchange filter is applied.

## Data Coverage

- Polygon (prices) and FRED (macro) require API keys. Without them, momentum and macro features
  are NULL for all tickers.
- SEC fundamentals are fetched per CIK. Rate-limited at ~8 req/sec. Large universes take time.
- FINRA short interest is CDN-based with settlement date gaps. Coverage is not daily.
- CFTC CoT data is aggregate/index-level, not stock-specific.
- Events ingestion (Nasdaq earnings calendar) is experimental and may break.

## Feature Scoring

- Many features will return NULL on the first run. NULLs raise the risk penalty score.
- Momentum features require ≥20 days of price history. New ingestions will not have this.
- P/S proxy is approximate: price × shares / revenue. Shares and revenue come from XBRL data
  which may be stale or missing for small companies.

## LLM Briefs

- Default provider is MockProvider. Configure OPENAI_API_KEY or NVIDIA_API_KEY for real analysis.
- LLM outputs are informational. They are not validated research.

## Candidate Outcome Tracking

- Forward returns are computed from `prices_daily` table. Without Polygon ingestion, all forward
  returns will be NULL.
- Outcome tracking does not constitute backtesting. It tracks what happened after a candidate
  was surfaced, which is subject to look-ahead bias if used to evaluate the scoring formula.

## No Paper Trading

MHDE does not include paper trading by design. Candidate outcome tracking is the evaluation
mechanism. The system tracks: when was a candidate surfaced, what was its score, what happened
afterward. It does not simulate positions, portfolio returns, stop losses, or exits.

## Backtest Smoke

- The backtest smoke test requires historical scores from multiple past runs.
- Results are unreliable without several weeks of accumulated daily runs.
- The smoke test is not validated for decision use.

## XGBoost Model

- Experimental only. Not used for alerts or rankings.
- Requires ≥30 labeled examples (run_id + forward_return_20d).
- Without weeks of data accumulation, the model will not train.

## Notifications

- Telegram and email require credentials. Without them, all notification channels are no-op.
- Deduplication window is 14 days by default. A-tier candidates will not re-alert within this window.

## Dashboard

- Dashboard is backed by DuckDB. Large universes (500+ tickers, many runs) may have slow queries.
- Auth is disabled only for local development. Always enable auth for VPS deployment.
