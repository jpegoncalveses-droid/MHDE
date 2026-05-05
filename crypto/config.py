"""Crypto prediction engine configuration constants."""

UNIVERSE_SIZE = 50

STABLECOIN_EXCLUDE = {"USDCUSDT", "DAIUSDT", "TUSDUSDT", "BUSDUSDT", "FDUSDUSDT", "USDPUSDT"}
WRAPPED_EXCLUDE = {"WBTCUSDT", "WBETHUSDT"}

BINANCE_FUTURES_BASE = "https://fapi.binance.com"
BINANCE_SPOT_BASE = "https://api.binance.com"

REQUEST_DELAY_S = 0.1

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
