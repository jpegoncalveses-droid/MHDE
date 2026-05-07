"""FX ML table schema definitions for DuckDB."""

SCHEMA_FX_PRICES_HOURLY = """
CREATE TABLE IF NOT EXISTS fx_prices_hourly (
    datetime_utc TIMESTAMP NOT NULL PRIMARY KEY,
    date DATE NOT NULL,
    weekday VARCHAR NOT NULL,
    hour_utc INTEGER NOT NULL,
    gbpeur_open DOUBLE,
    gbpeur_high DOUBLE,
    gbpeur_low DOUBLE,
    gbpeur_close DOUBLE,
    tick_count INTEGER,
    data_quality VARCHAR DEFAULT 'good',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

SCHEMA_FX_MACRO = """
CREATE TABLE IF NOT EXISTS fx_macro (
    indicator VARCHAR NOT NULL,
    observation_date DATE NOT NULL,
    value DOUBLE,
    source VARCHAR DEFAULT 'fred',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (indicator, observation_date)
);
"""

SCHEMA_FX_ML_LABELS = """
CREATE TABLE IF NOT EXISTS fx_ml_labels (
    datetime_utc TIMESTAMP NOT NULL PRIMARY KEY,
    close_price DOUBLE,
    fwd_max_up_pips_24h DOUBLE,
    fwd_max_down_pips_24h DOUBLE,
    fwd_max_up_pips_48h DOUBLE,
    fwd_max_down_pips_48h DOUBLE,
    fwd_close_pips_24h DOUBLE,
    fwd_close_pips_48h DOUBLE,
    label_up_20pip_24h BOOLEAN,
    label_down_20pip_24h BOOLEAN,
    label_up_20pip_48h BOOLEAN,
    label_down_20pip_48h BOOLEAN,
    label_up_30pip_24h BOOLEAN,
    label_down_30pip_24h BOOLEAN,
    label_up_30pip_48h BOOLEAN,
    label_down_30pip_48h BOOLEAN
);
"""

SCHEMA_FX_ML_FEATURES = """
CREATE TABLE IF NOT EXISTS fx_ml_features (
    datetime_utc TIMESTAMP NOT NULL PRIMARY KEY,
    return_1h DOUBLE,
    return_4h DOUBLE,
    return_8h DOUBLE,
    return_24h DOUBLE,
    return_5d DOUBLE,
    return_20d DOUBLE,
    rsi_14h DOUBLE,
    rsi_48h DOUBLE,
    price_vs_24h_ma DOUBLE,
    price_vs_120h_ma DOUBLE,
    price_vs_480h_ma DOUBLE,
    bollinger_position_24h DOUBLE,
    drawdown_from_480h_high DOUBLE,
    rally_from_480h_low DOUBLE,
    candle_body_pct DOUBLE,
    upper_wick_pct DOUBLE,
    lower_wick_pct DOUBLE,
    candle_range_pips DOUBLE,
    body_vs_avg_range DOUBLE,
    realized_vol_24h DOUBLE,
    realized_vol_120h DOUBLE,
    vol_ratio DOUBLE,
    atr_pips_24h DOUBLE,
    range_expansion DOUBLE,
    hour_sin DOUBLE,
    hour_cos DOUBLE,
    day_of_week INTEGER,
    is_london_open BOOLEAN,
    is_ny_open BOOLEAN,
    is_london_ny_overlap BOOLEAN,
    is_asian_session BOOLEAN,
    distance_from_daily_high DOUBLE,
    distance_from_daily_low DOUBLE,
    daily_range_pct_used DOUBLE,
    prior_session_range_pips DOUBLE,
    consecutive_up_hours INTEGER,
    consecutive_down_hours INTEGER,
    tick_count_vs_avg DOUBLE,
    boe_rate DOUBLE,
    ecb_rate DOUBLE,
    rate_differential DOUBLE,
    eurusd_return_24h DOUBLE,
    gbpusd_return_24h DOUBLE
);
"""

SCHEMA_FX_ML_PREDICTIONS = """
CREATE TABLE IF NOT EXISTS fx_ml_predictions (
    datetime_utc TIMESTAMP NOT NULL,
    model_id VARCHAR NOT NULL,
    direction VARCHAR NOT NULL,
    horizon VARCHAR NOT NULL,
    predicted_probability DOUBLE,
    prediction_threshold DOUBLE,
    actual_max_pips DOUBLE,
    actual_hit BOOLEAN,
    outcome_filled_at TIMESTAMP,
    PRIMARY KEY (datetime_utc, model_id, direction, horizon)
);
"""

SCHEMA_FX_ML_MODEL_RUNS = """
CREATE TABLE IF NOT EXISTS fx_ml_model_runs (
    model_id VARCHAR PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    direction VARCHAR NOT NULL,
    horizon VARCHAR NOT NULL,
    target_pips DOUBLE NOT NULL,
    train_start TIMESTAMP,
    train_end TIMESTAMP,
    test_start TIMESTAMP,
    test_end TIMESTAMP,
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
    is_active BOOLEAN DEFAULT FALSE
);
"""

SCHEMA_FX_POSITION = """
CREATE TABLE IF NOT EXISTS fx_position (
    position VARCHAR NOT NULL,
    entry_rate DOUBLE NOT NULL,
    entry_date TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

SCHEMA_FX_ALERT_STATE = """
CREATE TABLE IF NOT EXISTS fx_alert_state (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    alerts_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_buy_alert_at TIMESTAMP,
    last_sell_alert_at TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

SCHEMA_FX_SIGNALS = """
CREATE TABLE IF NOT EXISTS fx_signals (
    datetime_utc TIMESTAMP NOT NULL,
    signal_type VARCHAR NOT NULL,
    prob_up_24h DOUBLE,
    prob_down_24h DOUBLE,
    prob_up_48h DOUBLE,
    prob_down_48h DOUBLE,
    gbpeur_price DOUBLE,
    telegram_sent BOOLEAN DEFAULT FALSE,
    telegram_sent_at TIMESTAMP,
    outcome_pips_24h DOUBLE,
    outcome_pips_48h DOUBLE,
    PRIMARY KEY (datetime_utc, signal_type)
);
"""

ALL_SCHEMAS = [
    SCHEMA_FX_PRICES_HOURLY,
    SCHEMA_FX_MACRO,
    SCHEMA_FX_ML_LABELS,
    SCHEMA_FX_ML_FEATURES,
    SCHEMA_FX_ML_PREDICTIONS,
    SCHEMA_FX_ML_MODEL_RUNS,
    SCHEMA_FX_SIGNALS,
    SCHEMA_FX_POSITION,
    SCHEMA_FX_ALERT_STATE,
]


def create_all_tables(conn):
    for schema in ALL_SCHEMAS:
        conn.execute(schema)
