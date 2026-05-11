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
# export if BOTH hold: it sits more than 20% below its 90-day high
# (``drawdown_from_90d_high < POSTPARABOLIC_DD90_THRESHOLD``) AND it is still up
# more than 200% over 60 days (``return_60d > POSTPARABOLIC_RET60_THRESHOLD``).
# This suppresses the documented post-parabolic re-entry bias (SKYAI) without
# touching the model or the raw crypto_ml_predictions signal.
POSTPARABOLIC_DD90_THRESHOLD = -0.20
POSTPARABOLIC_RET60_THRESHOLD = 2.0

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
