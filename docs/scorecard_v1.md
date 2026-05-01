# MHDE Scorecard v1

## Formula

```
total = 0.30×cheap + 0.25×quality + 0.25×catalyst + 0.10×momentum + 0.10×sentiment − 0.20×risk
```

All components are 0–100. Risk penalty is subtracted. Total is clamped to [0, 100].

## Component Weights

| Component | Weight | Description |
|-----------|--------|-------------|
| cheap     | 30%    | Valuation signals (P/S proxy, price vs. 52-week high) |
| quality   | 25%    | Business quality (revenue growth, net margin, dilution) |
| catalyst  | 25%    | Near-term catalysts (filings, earnings, short interest) |
| momentum  | 10%    | Price momentum (20d/60d returns, volume, drawdown) |
| sentiment | 10%    | Short interest as contrarian proxy |
| risk      | -20%   | Risk penalty (missing data, negative income, low price, insufficient history) |

## Tier Thresholds

| Tier   | Criteria |
|--------|----------|
| A      | total ≥ 75 AND catalyst ≥ 50 AND risk ≤ 50 |
| B      | total ≥ 60 |
| C      | total ≥ 45 |
| Reject | total < 45 OR risk > 75 OR insufficient data flag |

## Known Limitations

- All components are NULL-safe. Missing data raises the risk penalty rather than crashing.
- The formula weights are v1 heuristics. They have not been validated against forward returns.
- XGBoost model is experimental and does not affect tier assignment or alerts in v1.

## Version History

| Version | Date | Changes |
|---------|------|---------|
| v1.0 | 2026-05-01 | Initial formula |
