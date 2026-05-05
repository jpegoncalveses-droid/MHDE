"""FX prediction engine configuration constants."""

PIP_SIZE = 0.0001

TARGET_PIPS = 20
TARGET_PIPS_HIGH = 30

HORIZONS = ["24h", "48h"]
DIRECTIONS = ["up", "down"]

SIGNAL_BUY_THRESHOLD = 0.65
SIGNAL_SELL_THRESHOLD = 0.65
SIGNAL_COUNTER_MAX = 0.40

TRAIN_START_YEAR = 2015
CV_TEST_YEARS = [2020, 2021, 2022, 2023, 2024, 2025]

LONDON_OPEN = (7, 16)
NY_OPEN = (12, 21)
ASIAN_SESSION = (23, 7)

SOURCE_CSV = "/home/jpcg/ATSRP/research/gbpeur_personal_fx/data/gbpeur_1h.csv"

FRED_SERIES = {
    "boe_rate": "BOERUKM",
    "ecb_rate": "ECBMLFR",
    "eurusd": "DEXUSEU",
    "gbpusd": "DEXUSUK",
}

MODELS_DIR = "models/saved/fx"

FEATURE_COLS = [
    "return_1h", "return_4h", "return_8h", "return_24h", "return_5d", "return_20d",
    "rsi_14h", "rsi_48h",
    "price_vs_24h_ma", "price_vs_120h_ma", "price_vs_480h_ma",
    "bollinger_position_24h", "drawdown_from_480h_high", "rally_from_480h_low",
    "candle_body_pct", "upper_wick_pct", "lower_wick_pct",
    "candle_range_pips", "body_vs_avg_range",
    "realized_vol_24h", "realized_vol_120h", "vol_ratio",
    "atr_pips_24h", "range_expansion",
    "hour_sin", "hour_cos", "day_of_week",
    "is_london_open", "is_ny_open", "is_london_ny_overlap", "is_asian_session",
    "distance_from_daily_high", "distance_from_daily_low",
    "daily_range_pct_used", "prior_session_range_pips",
    "consecutive_up_hours", "consecutive_down_hours",
    "tick_count_vs_avg",
    "boe_rate", "ecb_rate", "rate_differential",
    "eurusd_return_24h", "gbpusd_return_24h",
]

DEFAULT_PARAMS = {
    "n_estimators": 400,
    "max_depth": 4,
    "learning_rate": 0.03,
    "min_child_weight": 20,
    "subsample": 0.8,
    "colsample_bytree": 0.7,
    "eval_metric": "logloss",
    "random_state": 42,
    "verbosity": 0,
    "n_jobs": -1,
}
