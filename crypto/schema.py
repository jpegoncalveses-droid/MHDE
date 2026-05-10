"""Crypto ML table schema definitions for DuckDB."""

SCHEMA_CRYPTO_PRICES_DAILY = """
CREATE TABLE IF NOT EXISTS crypto_prices_daily (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume DOUBLE,
    trades INTEGER,
    taker_buy_volume DOUBLE,
    source VARCHAR DEFAULT 'binance',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trade_date)
);
"""

SCHEMA_CRYPTO_FUNDING_RATES = """
CREATE TABLE IF NOT EXISTS crypto_funding_rates (
    symbol VARCHAR NOT NULL,
    funding_time TIMESTAMP NOT NULL,
    funding_rate DOUBLE,
    mark_price DOUBLE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, funding_time)
);
"""

SCHEMA_CRYPTO_OPEN_INTEREST = """
CREATE TABLE IF NOT EXISTS crypto_open_interest (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    open_interest DOUBLE,
    open_interest_value DOUBLE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trade_date)
);
"""

SCHEMA_CRYPTO_UNIVERSE = """
CREATE TABLE IF NOT EXISTS crypto_universe (
    symbol VARCHAR NOT NULL,
    base_asset VARCHAR NOT NULL,
    avg_daily_volume_30d DOUBLE,
    rank_by_volume INTEGER,
    is_active BOOLEAN DEFAULT TRUE,
    added_date DATE,
    removed_date DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol)
);
"""

SCHEMA_CRYPTO_ML_LABELS = """
CREATE TABLE IF NOT EXISTS crypto_ml_labels (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    close_price DOUBLE,
    fwd_return_1d DOUBLE,
    fwd_return_3d DOUBLE,
    fwd_return_5d DOUBLE,
    fwd_return_10d DOUBLE,
    fwd_max_return_1d DOUBLE,
    fwd_max_return_3d DOUBLE,
    fwd_max_return_5d DOUBLE,
    fwd_max_return_10d DOUBLE,
    fwd_max_drawdown_1d DOUBLE,
    fwd_max_drawdown_3d DOUBLE,
    fwd_max_drawdown_5d DOUBLE,
    fwd_max_drawdown_10d DOUBLE,
    label_1d_5pct BOOLEAN,
    label_1d_3pct BOOLEAN,
    label_3d_5pct BOOLEAN,
    label_3d_10pct BOOLEAN,
    label_5d_10pct BOOLEAN,
    label_5d_15pct BOOLEAN,
    label_10d_10pct BOOLEAN,
    label_10d_15pct BOOLEAN,
    label_10d_20pct BOOLEAN,
    PRIMARY KEY (symbol, trade_date)
);
"""

SCHEMA_CRYPTO_ML_FEATURES = """
CREATE TABLE IF NOT EXISTS crypto_ml_features (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    return_1d DOUBLE,
    return_3d DOUBLE,
    return_5d DOUBLE,
    return_10d DOUBLE,
    return_20d DOUBLE,
    return_60d DOUBLE,
    rsi_14d DOUBLE,
    drawdown_from_90d_high DOUBLE,
    price_vs_20d_ma DOUBLE,
    price_vs_50d_ma DOUBLE,
    bollinger_position DOUBLE,
    close_in_range DOUBLE,
    realized_vol_10d DOUBLE,
    realized_vol_30d DOUBLE,
    vol_ratio DOUBLE,
    atr_pct_14d DOUBLE,
    relative_volume_20d DOUBLE,
    volume_trend_5d DOUBLE,
    taker_buy_ratio DOUBLE,
    return_vs_btc_1d DOUBLE,
    return_vs_btc_5d DOUBLE,
    return_vs_btc_10d DOUBLE,
    beta_to_btc_30d DOUBLE,
    funding_rate_current DOUBLE,
    funding_rate_avg_3d DOUBLE,
    funding_rate_avg_7d DOUBLE,
    funding_rate_zscore DOUBLE,
    oi_change_1d DOUBLE,
    oi_change_3d DOUBLE,
    oi_change_7d DOUBLE,
    oi_price_divergence_3d DOUBLE,
    btc_dominance DOUBLE,
    btc_return_7d DOUBLE,
    btc_vol_30d DOUBLE,
    market_cap_log DOUBLE,
    PRIMARY KEY (symbol, trade_date)
);
"""

SCHEMA_CRYPTO_ML_PREDICTIONS = """
CREATE TABLE IF NOT EXISTS crypto_ml_predictions (
    symbol VARCHAR NOT NULL,
    prediction_date DATE NOT NULL,
    model_id VARCHAR NOT NULL,
    horizon VARCHAR NOT NULL,
    predicted_probability DOUBLE,
    prediction_threshold DOUBLE,
    market_cap_bucket VARCHAR,
    actual_max_return DOUBLE,
    actual_max_drawdown DOUBLE,
    actual_hit BOOLEAN,
    outcome_filled_at TIMESTAMP,
    PRIMARY KEY (symbol, prediction_date, model_id, horizon)
);
"""

SCHEMA_CRYPTO_ML_MODEL_RUNS = """
CREATE TABLE IF NOT EXISTS crypto_ml_model_runs (
    model_id VARCHAR PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    horizon VARCHAR NOT NULL,
    target_threshold DOUBLE NOT NULL,
    train_start DATE,
    train_end DATE,
    test_start DATE,
    test_end DATE,
    n_train_samples INTEGER,
    n_test_samples INTEGER,
    n_positive_train INTEGER,
    n_positive_test INTEGER,
    precision_at_threshold DOUBLE,
    recall_at_threshold DOUBLE,
    f1_score DOUBLE,
    auc_roc DOUBLE,
    base_rate DOUBLE,
    lift_over_base DOUBLE,
    feature_importance_json TEXT,
    model_path VARCHAR,
    is_active BOOLEAN DEFAULT FALSE,
    -- Valid values: 'pending' | 'promoted' | 'promotion_blocked'
    -- Set by the validation gate (crypto/ml/validation_gate.py) after each training run.
    promotion_status VARCHAR DEFAULT 'pending'
);
"""

SCHEMA_PHASE0_MILESTONES = """
CREATE TABLE IF NOT EXISTS phase0_milestones (
    engine VARCHAR NOT NULL,
    model_id VARCHAR NOT NULL,
    milestone VARCHAR NOT NULL,
    fired_at TIMESTAMP NOT NULL,
    detail VARCHAR,
    PRIMARY KEY (engine, model_id, milestone)
);
"""

ALL_SCHEMAS = [
    SCHEMA_CRYPTO_PRICES_DAILY,
    SCHEMA_CRYPTO_FUNDING_RATES,
    SCHEMA_CRYPTO_OPEN_INTEREST,
    SCHEMA_CRYPTO_UNIVERSE,
    SCHEMA_CRYPTO_ML_LABELS,
    SCHEMA_CRYPTO_ML_FEATURES,
    SCHEMA_CRYPTO_ML_PREDICTIONS,
    SCHEMA_CRYPTO_ML_MODEL_RUNS,
    SCHEMA_PHASE0_MILESTONES,
]


def create_all_tables(conn):
    for schema in ALL_SCHEMAS:
        conn.execute(schema)
