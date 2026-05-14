"""Crypto prediction engine configuration constants."""

UNIVERSE_SIZE = 50

STABLECOIN_EXCLUDE = {"USDCUSDT", "DAIUSDT", "TUSDUSDT", "BUSDUSDT", "FDUSDUSDT", "USDPUSDT"}
WRAPPED_EXCLUDE = {"WBTCUSDT", "WBETHUSDT"}

BINANCE_FUTURES_BASE = "https://fapi.binance.com"
BINANCE_SPOT_BASE = "https://api.binance.com"

REQUEST_DELAY_S = 0.1

# OHLCV ingestion safety window.
#
# The daily ``mhde-crypto-predict`` timer fires at 00:30 UTC. If ingestion
# fetched klines through ``date.today()`` it would store a ~30-minute *partial*
# candle for the in-progress UTC day; combined with an INSERT that never
# revisited an existing row, that partial candle was frozen forever (the
# 2026-05-05/07 SKYAIUSDT incident). Defense in depth:
#   * INGESTION_LAG_DAYS  — never request a candle for a day this recent; only
#     ingest fully-closed UTC days (today - INGESTION_LAG_DAYS and older).
#   * REFETCH_WINDOW_DAYS — every run re-fetches this many trailing completed
#     days and UPSERTs them, so any stale/partial/late-corrected row self-heals.
INGESTION_LAG_DAYS = 1
REFETCH_WINDOW_DAYS = 3

# Post-parabolic exclusion filter (a pre-order-entry risk gate applied in the
# prediction export step — see crypto/ml/postparabolic_filter.py and
# crypto/ml/POSTPARABOLIC_FILTER_SPEC.md). A coin is excluded from the daily
# export if EITHER rule fires (OR-combined):
#
#   Rule A — post-parabolic (SKYAI-class, original):
#     ``drawdown_from_90d_high < POSTPARABOLIC_DD90_THRESHOLD``
#     AND ``return_60d > POSTPARABOLIC_RET60_THRESHOLD``
#   Rule B — short-window momentum (SWARMSUSDT-class, added ADR-028 2026-05-14):
#     ``return_5d < POSTPARABOLIC_RET5_THRESHOLD``
#
# Rule B was added after a paired backtest validated it as Sharpe-positive
# (6.32 → 6.51) with unchanged max DD over the Phase-1B-winner config and the
# loser-characterization study confirmed the SWARMSUSDT-class as 30% of deep
# losses. Both rules are strict less-than / greater-than on the cited features
# and fail-open per-input on NULL/NaN. See ADR-028.
POSTPARABOLIC_DD90_THRESHOLD = -0.20
POSTPARABOLIC_RET60_THRESHOLD = 2.0
POSTPARABOLIC_RET5_THRESHOLD = -0.30

# OHLCV plausibility / volume-cliff guard (pipelines/data_quality_guard.py).
# Runs in the daily pipeline between backfill-prices and the downstream stages;
# would have caught the 2026-05-07 partial-candle bug immediately. A symbol is
# flagged for a day if today's value falls below the trailing-N-day median by
# more than the cliff/collapse ratio (each check independent):
#   * volume / trade count < {VOLUME_CLIFF_RATIO, TRADE_COUNT_CLIFF_RATIO} × median
#   * (high − low) range    < RANGE_COLLAPSE_RATIO × median range
# The day is *systemic* (→ block downstream, CRITICAL alert) if at least
# SYSTEMIC_MIN_SYMBOLS symbols are evaluable and more than SYSTEMIC_FLAG_RATIO
# of them are flagged. Per-symbol-only → WARN, no block. Thresholds tuned on a
# 90-day clean-data scan: zero systemic false positives at any combo (clean-day
# max ≈ 10% of universe flagged); the 2026-05-07 corruption flags ≈80–96% of
# the universe — huge margin. See DECISIONS.md (ADR) and SESSION_LOG.md.
OHLCV_PLAUSIBILITY_WINDOW_DAYS = 20
VOLUME_CLIFF_RATIO = 0.10
RANGE_COLLAPSE_RATIO = 0.20
TRADE_COUNT_CLIFF_RATIO = 0.10
SYSTEMIC_FLAG_RATIO = 0.30
SYSTEMIC_MIN_SYMBOLS = 10

# Knockout (triple-barrier) label parameters (crypto/ml/knockout_label.py,
# populated by crypto/ml/labels.py). A trade entered at close C is a WIN
# (label_Nd_knockout = True) iff, walking forward bar by bar over N trading
# days, the intraday HIGH reaches C·(1 + KNOCKOUT_TP) before the intraday LOW
# reaches C·(1 + KNOCKOUT_SL) (KNOCKOUT_SL is negative). Same-bar both-touch
# resolves SL-first (pessimistic); a window with no barrier touch ("neither")
# is classified as a LOSS. Horizons mirror the legacy labels (5d, 10d). The
# legacy label_Nd_10pct columns are kept alongside (backward compat). Retuning
# TP/SL requires a full label re-backfill. See crypto/ml/KNOCKOUT_LABEL_SPEC.md
# and DECISIONS.md (ADR).
KNOCKOUT_TP = 0.10
KNOCKOUT_SL = -0.05

FEATURE_COLS = [
    "return_1d", "return_3d", "return_5d", "return_10d", "return_20d", "return_60d",
    "rsi_14d", "drawdown_from_90d_high", "price_vs_20d_ma", "price_vs_50d_ma",
    "bollinger_position", "close_in_range",
    "realized_vol_10d", "realized_vol_30d", "vol_ratio", "atr_pct_14d",
    "relative_volume_20d", "volume_trend_5d", "taker_buy_ratio",
    "return_vs_btc_1d", "return_vs_btc_5d", "return_vs_btc_10d", "beta_to_btc_30d",
    "funding_rate_current", "funding_rate_avg_3d", "funding_rate_avg_7d", "funding_rate_zscore",
    "oi_change_1d", "oi_change_3d", "oi_change_7d", "oi_price_divergence_3d",
    "btc_dominance", "btc_return_7d", "btc_vol_30d",
    "market_cap_log",
]

MODELS_DIR = "models/saved/crypto"

DEFAULT_PARAMS = {
    "n_estimators": 300,
    "max_depth": 4,
    "learning_rate": 0.05,
    "min_child_weight": 15,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "eval_metric": "logloss",
    "random_state": 42,
    "verbosity": 0,
    "n_jobs": -1,
}
